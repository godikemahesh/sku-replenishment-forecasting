"""Step 8 - validation of the published files (read back from disk, chunked for large tables).

Each check reports violations. A check that is *supposed* to find the deliberately injected
anomalies passes only if the violating IDs are exactly the IDs labelled in ground truth
(status ``PASS_LABELLED``) - so injected defects are proven to be isolated and no
unlabelled corruption exists.
"""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Profile
from .io import read_table, table_path
from .schema import TABLES
from .util import iso

CHUNK = 2_000_000
BIG = {"sales_history", "inventory_snapshot"}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, check, category, table, violations, rows_checked, detail="", labelled=None, bad_ids=None):
        status = "PASS" if violations == 0 else "FAIL"
        if labelled is not None:
            ok = set(bad_ids or []) == set(labelled)
            status = ("PASS_LABELLED" if violations else "PASS") if ok else "FAIL"
            detail = (detail + f" | violations={violations}, labelled={len(labelled)}, ids match={ok}").strip(" |")
        self.rows.append({"check": check, "category": category, "table": table, "status": status,
                          "rows_checked": int(rows_checked), "violations": int(violations), "detail": detail})


def _chunks(root, name, usecols=None):
    if name in BIG:
        yield from read_table(root, name, usecols=usecols, chunksize=CHUNK)
    else:
        yield read_table(root, name, usecols=usecols)


def schema_checks(root: Path, rep: Report, small: dict, refs: dict):
    """Generic checks for every table: required, enums, ID patterns, PK uniqueness, foreign keys."""
    for name, t in TABLES.items():
        if not table_path(root, name).exists():
            rep.add(f"{name}: file present", "completeness", name, 1, 0, "missing")
            continue
        n = nulls = enum_bad = pat_bad = fk_bad = 0
        hashes = []
        for df in ([small[name]] if name in small else _chunks(root, name)):
            n += len(df)
            for c in t.cols:
                if c.required:
                    nulls += int(df[c.name].isna().sum())
                if c.allowed:
                    enum_bad += int((~df[c.name].isna() & ~df[c.name].isin(c.allowed)).sum())
                if c.pattern:
                    v = df[c.name].dropna().astype(str)
                    u = pd.Series(v.unique())
                    bad_vals = set(u[~u.str.match(c.pattern)])
                    pat_bad += int(v.isin(bad_vals).sum()) if bad_vals else 0
            hashes.append(pd.util.hash_pandas_object(df[list(t.pk)], index=False).values)
            for fk in t.fks:
                parent = refs[fk.ref_table][list(fk.ref_cols)]
                child = df[list(fk.cols)].dropna()
                if len(fk.cols) == 1:
                    fk_bad += int((~child[fk.cols[0]].isin(parent[fk.ref_cols[0]])).sum())
                else:
                    m = child.merge(parent.drop_duplicates(), left_on=list(fk.cols), right_on=list(fk.ref_cols),
                                    how="left", indicator=True)
                    fk_bad += int((m["_merge"] == "left_only").sum())
        h = np.concatenate(hashes) if hashes else np.array([])
        dup = len(h) - len(np.unique(h))
        rep.add(f"{name}: required fields not null", "completeness", name, nulls, n)
        rep.add(f"{name}: enum values allowed", "domain", name, enum_bad, n)
        rep.add(f"{name}: ID pattern", "identity", name, pat_bad, n)
        rep.add(f"{name}: primary key unique", "identity", name, dup, n, f"PK = {', '.join(t.pk)}")
        if t.fks:
            rep.add(f"{name}: foreign keys resolve", "referential", name, fk_bad, n,
                    "; ".join(f"{','.join(f.cols)}->{f.ref_table}" for f in t.fks))


def validate(p: Profile, root: Path) -> pd.DataFrame:
    rep = Report()
    small = {n: read_table(root, n) for n in TABLES if n not in BIG and table_path(root, n).exists()}
    gt = small["ground_truth"]
    lab = lambda issue: gt.loc[(gt["issue_type"] == issue) & (gt["origin"] == "injected"), "entity_id"].tolist()
    refs = dict(small)
    refs["sku_store_matrix"] = small["sku_store_matrix"]
    schema_checks(root, rep, small, refs)

    sku, store, ss, mat = small["sku"], small["store"], small["supplier_sku"], small["sku_store_matrix"]
    po, ev, lto = small["purchase_order"], small["purchase_order_event"], small["lead_time_observation"]
    fc, pol, rec = small["forecast"], small["reorder_policy"], small["purchase_recommendation"]
    end = iso(p.history_end)

    # ---------------- master data rules
    rep.add("sku: unit_price > unit_cost", "domain", "sku", int((sku["unit_price"] <= sku["unit_cost"]).sum()), len(sku))
    rep.add("sku: discontinue_date >= launch_date", "date_order", "sku",
            int((sku["discontinue_date"].notna() & (sku["discontinue_date"] < sku["launch_date"])).sum()), len(sku))
    exp_state = np.where(sku["discontinue_date"].fillna("9999") <= end, "discontinued",
                         np.where(sku["launch_date"] > iso(p.history_end - timedelta(days=90)), "new_launch", "active"))
    rep.add("sku: lifecycle_state consistent with dates", "lifecycle", "sku", int((exp_state != sku["lifecycle_state"]).sum()), len(sku))
    rep.add("supplier_sku: MOQ is a positive multiple of pack size", "domain", "supplier_sku",
            int((ss["moq_units"] % ss["pack_size_units"] != 0).sum()), len(ss))
    rep.add("supplier_sku: effective_to >= effective_from", "date_order", "supplier_sku",
            int((ss["effective_to"].notna() & (ss["effective_to"] < ss["effective_from"])).sum()), len(ss))
    rep.add("supplier_sku: one contract per supplier x SKU", "duplicates", "supplier_sku",
            int(ss.duplicated(["supplier_id", "sku_id"]).sum()), len(ss))
    rep.add("sku_store_matrix: complete SKU x store grid", "completeness", "sku_store_matrix",
            abs(len(mat) - len(sku) * len(store)) + int(mat.duplicated(["sku_id", "store_id"]).sum()), len(mat))
    m2 = mat.merge(sku[["sku_id", "launch_date", "lifecycle_state"]], on="sku_id").merge(store[["store_id", "open_date"]], on="store_id")
    rk = m2["assortment_status"] != "not_ranged"
    bad = int((rk & ((m2["ranged_from"] < m2["launch_date"]) | (m2["ranged_from"] < m2["open_date"]))).sum()
              + (rk == m2["ranged_from"].isna()).sum())
    rep.add("sku_store_matrix: ranged_from >= launch and store open; null iff not_ranged", "date_order", "sku_store_matrix", bad, len(mat))
    bad = int((m2["assortment_status"].isin(["phase_out", "delisted"]) & (m2["lifecycle_state"] != "discontinued")).sum())
    rep.add("sku_store_matrix: phase_out/delisted only for discontinued SKUs", "lifecycle", "sku_store_matrix", bad, len(mat))

    # ---------------- purchase orders / events / lead times
    pos = po.merge(ss[["supplier_sku_id", "pack_size_units", "moq_units", "unit_cost", "nominal_lead_time_days",
                       "effective_from", "effective_to"]], on="supplier_sku_id")
    rep.add("purchase_order: qty multiple of pack and >= MOQ", "constraint", "purchase_order",
            int(((pos["order_qty_units"] % pos["pack_size_units"] != 0) | (pos["order_qty_units"] < pos["moq_units"])).sum()), len(po))
    rep.add("purchase_order: order_value = qty x unit_cost", "arithmetic", "purchase_order",
            int((abs(pos["order_value"] - (pos["order_qty_units"] * pos["unit_cost"]).round(2)) > 0.011).sum()), len(po))
    od = pd.to_datetime(pos["order_date"])
    expd = (od + pd.to_timedelta(pos["nominal_lead_time_days"], "D")).dt.strftime("%Y-%m-%d")
    rep.add("purchase_order: expected_receipt = order_date + nominal lead time", "arithmetic", "purchase_order",
            int((expd != pos["expected_receipt_date"]).sum()), len(po))
    bad = ((pos["po_status"] == "received") != pos["actual_receipt_date"].notna()) | (pos["actual_receipt_date"] < pos["order_date"])
    rep.add("purchase_order: status/actual date consistent and receipt >= order", "lifecycle", "purchase_order", int(bad.sum()), len(po))
    repl = pos[pos["order_reason"] == "replenishment"].merge(sku[["sku_id", "discontinue_date"]], on="sku_id")
    bad = (repl["discontinue_date"].notna() & (repl["order_date"] >= repl["discontinue_date"])) \
        | (repl["order_date"] < repl["effective_from"]) | (repl["effective_to"].notna() & (repl["order_date"] > repl["effective_to"]))
    rep.add("purchase_order: ordered only while SKU orderable and contract effective", "lifecycle", "purchase_order", int(bad.sum()), len(repl))

    e = ev.merge(po[["po_id", "order_date", "expected_receipt_date", "actual_receipt_date", "po_status"]], on="po_id")
    created_ok = e[e["event_type"] == "CREATED"].set_index("po_id")
    bad = len(po) - int((created_ok["event_date"] == created_ok["order_date"]).sum())
    recv = e[e["event_type"] == "RECEIVED"]
    bad += int((recv["event_date"] != recv["actual_receipt_date"]).sum()) + abs(len(recv) - int((po["po_status"] == "received").sum()))
    should_delay = po[(po["expected_receipt_date"] <= end) & (po["actual_receipt_date"].fillna("9999") > po["expected_receipt_date"])]
    bad += abs(len(should_delay) - int((e["event_type"] == "DELAYED").sum()))
    same_po = ev["po_id"] == ev["po_id"].shift()
    bad += int((same_po & (ev["event_date"] < ev["event_date"].shift())).sum())   # events ordered by po_id, sequence
    rep.add("purchase_order_event: CREATED -> [DELAYED] -> RECEIVED sequence and dates", "lifecycle", "purchase_order_event", bad, len(ev))

    rep.add("lead_time_observation: 1:1 with received POs", "referential", "lead_time_observation",
            abs(len(lto) - int((po["po_status"] == "received").sum())) + int(lto["po_id"].duplicated().sum()), len(lto))
    ltd = (pd.to_datetime(lto["receipt_date"]) - pd.to_datetime(lto["order_date"])).dt.days
    rep.add("lead_time_observation: lead_time = receipt - order; delay = lead_time - nominal", "arithmetic", "lead_time_observation",
            int(((ltd != lto["lead_time_days"]) | (lto["delay_days"] != lto["lead_time_days"] - lto["nominal_lead_time_days"])).sum()), len(lto))
    neg = lto.loc[lto["lead_time_days"] < 0, "lead_time_observation_id"]
    rep.add("lead_time_observation: lead_time_days >= 0", "anomaly_isolation", "lead_time_observation", len(neg), len(lto),
            "invalid values must be labelled", lab("negative_lead_time"), neg.tolist())
    j = lto.merge(po[["po_id", "actual_receipt_date"]], on="po_id")
    mism = j.loc[j["receipt_date"] != j["actual_receipt_date"], "lead_time_observation_id"]
    rep.add("lead_time_observation: receipt_date matches purchase_order", "anomaly_isolation", "lead_time_observation",
            len(mism), len(lto), "", lab("negative_lead_time"), mism.tolist())

    # ---------------- sales + inventory ledger (chunked, read in lock-step)
    _ledger_checks(p, root, rep, sku, store, mat, po, gt)

    # ---------------- forecasts
    hs = (pd.to_datetime(fc["forecast_origin_date"]) + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    hd = (pd.to_datetime(fc["horizon_end_date"]) - pd.to_datetime(fc["horizon_start_date"])).dt.days + 1
    rep.add("forecast: horizon starts day after origin; horizon_days consistent", "date_order", "forecast",
            int(((hs != fc["horizon_start_date"]) | (hd != fc["horizon_days"])).sum()), len(fc))
    rep.add("forecast: ID month = horizon start month", "identity", "forecast",
            int((fc["forecast_id"].str[-7:] != fc["horizon_start_date"].str[:7]).sum()), len(fc))
    rep.add("forecast: horizon units = daily x days", "arithmetic", "forecast",
            int((abs(fc["predicted_daily_units"] * fc["horizon_days"] - fc["predicted_horizon_units"]) > 0.01 * fc["horizon_days"]).sum()), len(fc))
    leak = fc.loc[fc["training_window_end_date"] > fc["forecast_origin_date"], "forecast_id"]
    rep.add("forecast: no future data (training end <= origin)", "anomaly_isolation", "forecast", len(leak), len(fc),
            "temporal holdout integrity", lab("forecast_future_leakage"), leak.tolist())
    so = _stockout_days(root)
    w = fc.merge(so, on=["store_id", "sku_id"], how="left")
    inside = (w["sale_date"] >= w["training_window_start_date"]) & (w["sale_date"] <= w["training_window_end_date"])
    cnt = fc["forecast_id"].map(w[inside].groupby("forecast_id").size()).fillna(0)
    ign = fc.loc[cnt > fc["stockout_days_excluded"], "forecast_id"]
    rep.add("forecast: all stockout days in training window excluded (censoring)", "anomaly_isolation", "forecast",
            len(ign), len(fc), "stockout days recounted from sales_history", lab("forecast_stockout_as_zero_demand"), ign.tolist())

    # ---------------- policy reconciliation
    L, R, d, sd, sL, z = (pol[c] for c in ["lead_time_mean_days", "review_period_days", "demand_daily_units",
                                            "demand_daily_sd_units", "lead_time_sd_days", "z_value"])
    ssu = np.ceil(z * np.sqrt((L + R) * sd ** 2 + d ** 2 * sL ** 2) - 1e-9)
    bad = (ssu != pol["safety_stock_units"]) | (np.ceil(d * L - 1e-9) + ssu != pol["reorder_point_units"]) \
        | (np.ceil(d * (L + R) - 1e-9) + ssu != pol["order_up_to_units"])
    rep.add("reorder_policy: SS / ROP / order-up-to recompute exactly", "arithmetic", "reorder_policy", int(bad.sum()), len(pol))
    prod = fc[fc["forecast_type"] == "production"].set_index("forecast_id")
    bad = (pol["forecast_id"].map(prod["predicted_daily_units"]) != pol["demand_daily_units"]) | \
          (pol["forecast_id"].map(prod["store_id"]) != pol["store_id"]) | (pol["forecast_id"].map(prod["sku_id"]) != pol["sku_id"])
    rep.add("reorder_policy: demand equals linked production forecast", "referential", "reorder_policy", int(bad.sum()), len(pol))

    # ---------------- recommendations
    r = rec.merge(pol[["reorder_policy_id", "policy_status"]], on="reorder_policy_id")
    bad = (r["inventory_position_units"] != r["on_hand_units"] + r["on_order_units"]) \
        | (r["reorder_triggered"] != (r["inventory_position_units"] <= r["reorder_point_units"]))
    rep.add("purchase_recommendation: IP = on_hand + on_order; trigger = IP <= ROP", "arithmetic", "purchase_recommendation", int(bad.sum()), len(rec))
    pk_, mq = r["pack_size_units"].astype(float), r["moq_units"].astype(float)
    exp_q = np.where(r["reorder_triggered"] & (r["policy_status"] == "active") & (r["raw_need_units"] > 0),
                     np.ceil(np.maximum(r["raw_need_units"], mq.fillna(0)) / pk_.fillna(1)) * pk_.fillna(1), 0)
    rep.add("purchase_recommendation: qty_after_moq_pack = ceil(max(need, MOQ)/pack)*pack", "arithmetic", "purchase_recommendation",
            int((exp_q != r["qty_after_moq_pack_units"]).sum()), len(rec))
    q = r["recommended_order_qty_units"]
    v = r.loc[(q > 0) & (q % pk_.fillna(1) != 0), "purchase_recommendation_id"]
    rep.add("purchase_recommendation: positive qty is a multiple of pack size", "anomaly_isolation", "purchase_recommendation",
            len(v), int((q > 0).sum()), "", lab("reorder_qty_ignores_pack_size"), v.tolist())
    rep.add("purchase_recommendation: positive qty >= MOQ", "constraint", "purchase_recommendation", int(((q > 0) & (q < mq)).sum()), int((q > 0).sum()))
    disc = set(sku.loc[sku["lifecycle_state"] == "discontinued", "sku_id"])
    v = r.loc[(q > 0) & r["sku_id"].isin(disc), "purchase_recommendation_id"]
    rep.add("purchase_recommendation: no positive qty for discontinued SKUs", "anomaly_isolation", "purchase_recommendation",
            len(v), len(rec), "", lab("discontinued_sku_recommended"), v.tolist())
    load = r.groupby("store_id")[["inventory_position_units", "recommended_order_qty_units"]].sum()
    cap = store.set_index("store_id")["storage_capacity_units"]
    over = ((load["recommended_order_qty_units"] > 0) & (load.sum(1) > cap.reindex(load.index))).sum()
    rep.add("purchase_recommendation: store IP + orders <= storage capacity", "constraint", "purchase_recommendation", int(over), len(load))
    rep.add("purchase_recommendation: final qty <= qty after MOQ/pack (capacity only cuts)", "constraint", "purchase_recommendation",
            int(((q > r["qty_after_moq_pack_units"]) & ~r["purchase_recommendation_id"].isin(lab("reorder_qty_ignores_pack_size") + lab("discontinued_sku_recommended"))).sum()), len(rec))
    bad = (q > 0) != r["expected_receipt_date"].notna()
    rep.add("purchase_recommendation: expected_receipt_date present iff qty > 0", "lifecycle", "purchase_recommendation", int(bad.sum()), len(rec))
    rep.add("purchase_recommendation: ID sequence PO-<year>-###### contiguous", "identity", "purchase_recommendation",
            int((rec["purchase_recommendation_id"].str[-6:].astype(int).values != np.arange(1, len(rec) + 1)).sum()), len(rec))

    # ---------------- ground truth separation
    gt_cols = {"true_demand_units", "latent_demand", "issue_type", "expected_value", "true_demand_class", "base_daily_rate"}
    leaks = [f"{n}.{c.name}" for n, t in TABLES.items() if t.layer != "ground_truth" for c in t.cols if c.name in gt_cols]
    rep.add("ground truth not present in input/output tables", "separation", "*", len(leaks), len(TABLES), ", ".join(leaks))
    ids = {"lead_time_observation": set(lto["lead_time_observation_id"]), "forecast": set(fc["forecast_id"]),
           "purchase_recommendation": set(rec["purchase_recommendation_id"])}
    other = gt[gt["entity_type"] != "sales_history"]
    bad = int(sum(eid not in ids[et] for et, eid in zip(other["entity_type"], other["entity_id"])))
    rep.add("ground_truth: labelled entities exist", "referential", "ground_truth", bad, len(other))
    return pd.DataFrame(rep.rows)


def _stockout_days(root: Path) -> pd.DataFrame:
    cols = ["store_id", "sku_id", "sale_date", "stockout_flag"]
    parts = [c.loc[c["stockout_flag"], cols[:3]] for c in read_table(root, "sales_history", usecols=cols, chunksize=CHUNK)]
    return pd.concat(parts, ignore_index=True)


def _ledger_checks(p, root, rep, sku, store, mat, po, gt):
    start = np.datetime64(p.history_start)
    # dense on-order and receipt expectations from purchase orders
    keys = mat.loc[mat["assortment_status"] != "not_ranged", ["store_id", "sku_id"]].reset_index(drop=True)
    kidx = pd.Series(np.arange(len(keys)), index=pd.MultiIndex.from_frame(keys))
    D = p.n_days
    on_order = np.zeros((len(keys), D + 1), np.int64)
    receipts = np.zeros((len(keys), D), np.int64)
    pi = kidx.index.get_indexer(pd.MultiIndex.from_frame(po[["store_id", "sku_id"]]))
    o = (pd.to_datetime(po["order_date"]).values.astype("datetime64[D]") - start).astype(int)
    a = np.where(po["actual_receipt_date"].notna(),
                 (pd.to_datetime(po["actual_receipt_date"]).values.astype("datetime64[D]") - start).astype(int), D)
    np.add.at(on_order, (pi, np.clip(o, 0, D)), po["order_qty_units"].values)
    np.add.at(on_order, (pi, np.clip(a, 0, D)), -po["order_qty_units"].values)
    on_order = np.cumsum(on_order, axis=1)[:, :D]
    rcv = a < D
    np.add.at(receipts, (pi[rcv], a[rcv]), po["order_qty_units"].values[rcv])

    sku_i = sku.set_index("sku_id")
    gt_cens = set(gt.loc[gt["issue_type"] == "stockout_censored_demand", "entity_id"])
    rng_from = mat.set_index(["store_id", "sku_id"])["ranged_from"]
    rng_to = mat.set_index(["store_id", "sku_id"])["ranged_to"]
    c = {k: 0 for k in ["id", "arith", "cont", "nonneg", "recv", "onorder", "match", "status", "avail", "dow", "price", "window", "dup"]}
    n = 0
    prev = None
    s_iter = read_table(root, "sales_history", chunksize=CHUNK)
    i_iter = read_table(root, "inventory_snapshot", chunksize=CHUNK)
    for s, inv in zip(s_iter, i_iter):
        n += len(s)
        key = s["store_id"] + "-" + s["sku_id"] + "-" + s["sale_date"].str.replace("-", "", regex=False)
        c["id"] += int((s["sale_id"] != "SALE-" + key).sum() + (inv["inventory_snapshot_id"] != "INV-" + key).sum())
        c["match"] += int(((s["store_id"] != inv["store_id"]) | (s["sku_id"] != inv["sku_id"]) | (s["sale_date"] != inv["snapshot_date"])
                           | (s["units_sold"] != inv["units_sold"])).sum())
        c["arith"] += int((inv["closing_on_hand_units"] != inv["opening_on_hand_units"] + inv["receipts_units"]
                           - inv["units_sold"] + inv["adjustment_units"]).sum())
        c["nonneg"] += int(((inv[["opening_on_hand_units", "receipts_units", "units_sold", "closing_on_hand_units", "on_order_units"]] < 0).any(axis=1)
                            | (inv["adjustment_units"] > 0)).sum())
        # opening = previous day's closing for the same pair (carry last row across chunks)
        frame = inv[["store_id", "sku_id", "snapshot_date", "opening_on_hand_units", "closing_on_hand_units"]]
        if prev is not None:
            frame = pd.concat([prev, frame], ignore_index=True)
        same = (frame["store_id"] == frame["store_id"].shift()) & (frame["sku_id"] == frame["sku_id"].shift())
        nxt = (pd.to_datetime(frame["snapshot_date"]) - pd.to_datetime(frame["snapshot_date"].shift())).dt.days == 1
        c["cont"] += int((same & nxt & (frame["opening_on_hand_units"] != frame["closing_on_hand_units"].shift())).sum())
        prev = frame.iloc[[-1]]
        di = (pd.to_datetime(inv["snapshot_date"]).values.astype("datetime64[D]") - start).astype(int)
        ki = kidx.index.get_indexer(pd.MultiIndex.from_frame(inv[["store_id", "sku_id"]]))
        c["recv"] += int((receipts[ki, di] != inv["receipts_units"].values).sum())
        c["onorder"] += int((on_order[ki, di] != inv["on_order_units"].values).sum())
        avail = inv["opening_on_hand_units"] + inv["receipts_units"]
        exp = np.where(avail == 0, "out_of_stock_all_day", np.where(inv["closing_on_hand_units"] == 0, "sold_out_during_day", "in_stock"))
        c["status"] += int(((s["availability_status"] != exp) | (s["stockout_flag"] != (s["availability_status"] != "in_stock"))
                            | (inv["stockout_flag"] != (inv["closing_on_hand_units"] == 0))).sum())
        cens = s["sale_id"].isin(gt_cens)
        c["avail"] += int((cens & ~s["stockout_flag"]).sum())           # every truly censored day must be flagged
        c["dow"] += int((pd.to_datetime(s["sale_date"]).dt.day_name() != s["day_of_week"]).sum())
        price = s["sku_id"].map(sku_i["unit_price"])
        c["price"] += int(((abs(s["unit_price_paid"] - (price * (1 - s["promo_discount_pct"])).round(2)) > 0.011)
                           | (s["promotion_flag"] != (s["promo_discount_pct"] > 0))).sum())
        pair = pd.MultiIndex.from_frame(s[["store_id", "sku_id"]])
        rf, rt = rng_from.reindex(pair).values, rng_to.reindex(pair).fillna("9999").values
        c["window"] += int(((s["sale_date"].values < rf) | (s["sale_date"].values > rt) | (s["sale_date"] > iso(p.history_end))
                            | (s["sale_date"].values < s["sku_id"].map(sku_i["launch_date"]).values)).sum())
    rep.add("sales_history/inventory_snapshot: IDs derived from natural key", "identity", "sales_history", c["id"], n)
    rep.add("sales_history = inventory_snapshot (same SKU-store-day, units_sold)", "arithmetic", "sales_history", c["match"], n)
    rep.add("inventory_snapshot: closing = opening + receipts - sold + adjustment", "arithmetic", "inventory_snapshot", c["arith"], n)
    rep.add("inventory_snapshot: opening = previous closing", "arithmetic", "inventory_snapshot", c["cont"], n)
    rep.add("inventory_snapshot: quantities non-negative, adjustments <= 0", "domain", "inventory_snapshot", c["nonneg"], n)
    rep.add("inventory_snapshot: receipts = POs received that day", "arithmetic", "inventory_snapshot", c["recv"], n)
    rep.add("inventory_snapshot: on_order = open PO quantity", "arithmetic", "inventory_snapshot", c["onorder"], n)
    rep.add("sales_history: availability_status / stockout_flag derived from ledger", "derived", "sales_history", c["status"], n)
    rep.add("sales_history: every truly censored day is flagged as stockout", "censoring", "sales_history", c["avail"], n)
    rep.add("sales_history: day_of_week matches sale_date", "derived", "sales_history", c["dow"], n)
    rep.add("sales_history: price paid = price x (1 - discount); promo flag consistent", "arithmetic", "sales_history", c["price"], n)
    rep.add("sales_history: sales only while ranged and launched", "lifecycle", "sales_history", c["window"], n)
