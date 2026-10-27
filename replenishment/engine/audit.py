"""Audit gate: rule-based detection of defective records and operational exceptions.

The audit only reads the published input/output tables - never ground truth - so its
detections can be scored honestly against ``ground_truth.csv``.

data_defect rules (record is wrong and must not be used)
  LT01  lead_time_days < 0                                   -> quarantine observation
  FC01  training_window_end_date > forecast_origin_date      -> quarantine forecast (future leakage)
  FC02  stockout days in training window (recounted from sales_history)
        > stockout_days_excluded                             -> quarantine forecast (censoring ignored)
  PR01  qty > 0 and qty not a multiple of pack size          -> block
  PR02  0 < qty < MOQ                                        -> block
  PR03  qty > 0 for a discontinued SKU                       -> block
  PR04  qty > 0 without an active supplier contract          -> block
  PR05  store on-hand + on-order + recommended > capacity    -> block (store-level)
operational rules (decision is valid but needs a human)
  OP01  capacity_exception     OP02 capacity_reduced     OP03 blocked_no_active_supplier
  OP04  manual_review_flag     OP05 supplier delay in open POs (expected date passed)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..util import iso


def _exc(df, category, rule, entity_type, id_col, severity, action, detail):
    if len(df) == 0:
        return pd.DataFrame()
    return pd.DataFrame({
        "exception_category": category, "rule_id": rule, "entity_type": entity_type,
        "entity_id": df[id_col].values,
        "store_id": df["store_id"].values if "store_id" in df else None,
        "sku_id": df["sku_id"].values if "sku_id" in df else None,
        "severity": severity, "action": action, "detail": detail(df) if callable(detail) else detail,
    })


def run_audit(t: dict[str, pd.DataFrame], history_end) -> pd.DataFrame:
    sku, store = t["sku"], t["store"]
    lto, fc, rec = t["lead_time_observation"], t["forecast"], t["purchase_recommendation"]
    sales_stockouts = t["_stockout_days"]   # store_id, sku_id, sale_date for stockout_flag rows
    out = []

    neg = lto[lto["lead_time_days"] < 0]
    out.append(_exc(neg, "data_defect", "LT01", "lead_time_observation", "lead_time_observation_id", "high", "quarantine",
                    lambda d: "lead_time_days=" + d["lead_time_days"].astype(str) + " (receipt before order); excluded from lead-time statistics"))

    leak = fc[fc["training_window_end_date"] > fc["forecast_origin_date"]]
    out.append(_exc(leak, "data_defect", "FC01", "forecast", "forecast_id", "high", "quarantine",
                    lambda d: "trained through " + d["training_window_end_date"] + " but origin is " + d["forecast_origin_date"]))

    # FC02: recount stockout days inside each forecast's own training window from source data
    w = fc[["forecast_id", "store_id", "sku_id", "training_window_start_date", "training_window_end_date",
            "stockout_days_excluded"]].merge(sales_stockouts, on=["store_id", "sku_id"], how="left")
    inside = (w["sale_date"] >= w["training_window_start_date"]) & (w["sale_date"] <= w["training_window_end_date"])
    actual = w[inside].groupby("forecast_id").size()
    fcx = fc.assign(_actual=fc["forecast_id"].map(actual).fillna(0).astype(int))
    ign = fcx[fcx["_actual"] > fcx["stockout_days_excluded"]]
    out.append(_exc(ign, "data_defect", "FC02", "forecast", "forecast_id", "high", "quarantine",
                    lambda d: d["_actual"].astype(str) + " stockout days in training window but only "
                    + d["stockout_days_excluded"].astype(str) + " excluded - censored sales treated as demand"))

    q = rec["recommended_order_qty_units"]
    pos = rec[q > 0]
    pack = pos["pack_size_units"].astype(float)
    moq = pos["moq_units"].astype(float)
    bad_pack = pos[(pack.notna()) & (pos["recommended_order_qty_units"] % pack.fillna(1) != 0)]
    out.append(_exc(bad_pack, "data_defect", "PR01", "purchase_recommendation", "purchase_recommendation_id", "high", "block",
                    lambda d: "qty " + d["recommended_order_qty_units"].astype(str) + " not a multiple of pack " + d["pack_size_units"].astype(str)))
    bad_moq = pos[moq.notna() & (pos["recommended_order_qty_units"] < moq)]
    out.append(_exc(bad_moq, "data_defect", "PR02", "purchase_recommendation", "purchase_recommendation_id", "high", "block",
                    lambda d: "qty " + d["recommended_order_qty_units"].astype(str) + " below MOQ " + d["moq_units"].astype(str)))
    disc_ids = set(sku.loc[sku["lifecycle_state"] == "discontinued", "sku_id"])
    bad_disc = pos[pos["sku_id"].isin(disc_ids)]
    out.append(_exc(bad_disc, "data_defect", "PR03", "purchase_recommendation", "purchase_recommendation_id", "high", "block",
                    lambda d: "positive order for discontinued SKU " + d["sku_id"]))
    no_sup = pos[pos["supplier_sku_id"].isna()]
    out.append(_exc(no_sup, "data_defect", "PR04", "purchase_recommendation", "purchase_recommendation_id", "high", "block",
                    "positive order without an active supplier contract"))
    load = rec.groupby("store_id")[["inventory_position_units", "recommended_order_qty_units"]].sum()
    load["cap"] = store.set_index("store_id")["storage_capacity_units"]
    over = load[(load["recommended_order_qty_units"] > 0)
                & (load["inventory_position_units"] + load["recommended_order_qty_units"] > load["cap"])].reset_index()
    over["entity"] = over["store_id"]
    out.append(_exc(over, "data_defect", "PR05", "store", "entity", "high", "block",
                    lambda d: "IP " + d["inventory_position_units"].astype(str) + " + orders " + d["recommended_order_qty_units"].astype(str)
                    + " > capacity " + d["cap"].astype(str)))

    # ---- operational exceptions (expected, need a planner)
    for rule, status, sev, action, text in [
        ("OP01", "capacity_exception", "high", "manual_review", "reorder triggered but store capacity has no room for one MOQ"),
        ("OP02", "capacity_reduced", "medium", "manual_review", "order cut to fit store capacity"),
        ("OP03", "blocked_no_active_supplier", "high", "manual_review", "SKU needs stock but has no active supplier contract"),
    ]:
        out.append(_exc(rec[rec["recommendation_status"] == status], "operational", rule, "purchase_recommendation",
                        "purchase_recommendation_id", sev, action, text))
    review = rec[rec["manual_review_flag"].astype(bool) & ~rec["recommendation_status"].isin(["capacity_exception"])]
    out.append(_exc(review, "operational", "OP04", "purchase_recommendation", "purchase_recommendation_id", "low", "manual_review",
                    lambda d: "low-confidence inputs: " + d["rule_trace"].str.extract(r"\((\w+)\)")[0].fillna("")
                    + " / " + d["rule_trace"].str.extract(r"L=[\d.]+d\((\w+)\)")[0].fillna("")))
    po = t["purchase_order"]
    late = po[(po["po_status"] == "open") & (po["expected_receipt_date"] < iso(history_end))]
    out.append(_exc(late, "operational", "OP05", "purchase_order", "po_id", "medium", "expedite",
                    lambda d: "open PO past expected receipt " + d["expected_receipt_date"]))

    res = pd.concat([o for o in out if len(o)], ignore_index=True)
    res = res.sort_values(["exception_category", "rule_id", "entity_id"]).reset_index(drop=True)
    res.insert(0, "exception_id", [f"EXC-{i + 1:08d}" for i in range(len(res))])
    return res
