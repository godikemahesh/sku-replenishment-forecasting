"""Synthetic data generator: master data -> daily simulation -> derived event tables."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import Profile
from ..util import iso, sequence_ids, to_iso
from .anomalies import inject_input_anomalies
from .master import Master, generate_master
from .simulate import simulate_store, supply_arrays

STOCKOUT_EXPECTED = ("Observed sales are censored by availability: do not treat as demand; "
                     "exclude or impute when forecasting.")


def _append(df: pd.DataFrame, path: Path, first: bool) -> None:
    df.to_csv(path, mode="w" if first else "a", header=first, index=False)


def build_purchase_orders(p: Profile, m: Master, orders: pd.DataFrame):
    ss = m.supplier_sku
    row = orders["supplier_sku_row"].values
    start = np.datetime64(p.history_start)
    end_idx = p.n_days - 1
    o = orders.copy()
    o["supplier_sku_id"] = ss["supplier_sku_id"].values[row]
    o["supplier_id"] = ss["supplier_id"].values[row]
    o["order_value"] = np.round(o["order_qty_units"].values * ss["unit_cost"].values[row], 2)
    exp_day = o["order_day"].values + o["nominal"].values
    act_day = o["order_day"].values + o["lead_time"].values
    received = act_day <= end_idx
    o["expected_receipt_date"] = to_iso(start + exp_day.astype("timedelta64[D]"))
    o["actual_receipt_date"] = np.where(received, to_iso(start + act_day.astype("timedelta64[D]")), None)
    o["po_status"] = np.where(received, "received", "open")
    o["_exp"], o["_act"], o["_received"] = exp_day, act_day, received
    o = o.sort_values(["order_date", "store_id", "sku_id", "order_reason"], kind="mergesort").reset_index(drop=True)
    o["po_id"] = sequence_ids(o["order_date"].str[:4])
    po = o[["po_id", "store_id", "sku_id", "supplier_id", "supplier_sku_id", "order_reason", "order_date",
            "order_qty_units", "order_value", "expected_receipt_date", "actual_receipt_date", "po_status"]]

    created = pd.DataFrame({"po_id": o["po_id"], "event_type": "CREATED", "event_date": o["order_date"],
                            "quantity_units": o["order_qty_units"], "_k": 0})
    late = o[(o["_act"] > o["_exp"]) & (o["_exp"] <= end_idx)]
    delayed = pd.DataFrame({"po_id": late["po_id"], "event_type": "DELAYED", "event_date": late["expected_receipt_date"],
                            "quantity_units": late["order_qty_units"], "_k": 1})
    rec = o[o["_received"]]
    receivedev = pd.DataFrame({"po_id": rec["po_id"], "event_type": "RECEIVED", "event_date": rec["actual_receipt_date"],
                               "quantity_units": rec["order_qty_units"], "_k": 2})
    ev = pd.concat([created, delayed, receivedev]).sort_values(["po_id", "_k"], kind="mergesort").reset_index(drop=True)
    ev.insert(0, "event_id", ev["po_id"] + "-EV" + (ev.groupby("po_id").cumcount() + 1).astype(str))
    ev = ev.drop(columns="_k")

    lto = pd.DataFrame({
        "lead_time_observation_id": "LTO-" + rec["po_id"].str[3:], "po_id": rec["po_id"],
        "supplier_id": rec["supplier_id"], "sku_id": rec["sku_id"], "store_id": rec["store_id"],
        "order_date": rec["order_date"], "receipt_date": rec["actual_receipt_date"],
        "lead_time_days": rec["lead_time"], "nominal_lead_time_days": rec["nominal"],
        "delay_days": rec["lead_time"] - rec["nominal"],
    }).reset_index(drop=True)
    return po, ev, lto


def generate(p: Profile, out: Path, log=print) -> dict:
    """Write all input/ and ground_truth/ tables; return in-memory pieces the pipeline needs."""
    inp, gt_dir = out / "input", out / "ground_truth"
    inp.mkdir(parents=True, exist_ok=True)
    gt_dir.mkdir(parents=True, exist_ok=True)
    m = generate_master(p)
    sup = supply_arrays(p, m)
    orders, gt_stock, delists, profiles = [], [], [], []
    for j in range(p.n_stores):
        res = simulate_store(p, m, j, sup)
        first = j == 0
        _append(res["sales"], inp / "sales_history.csv", first)
        _append(res["snapshot"], inp / "inventory_snapshot.csv", first)
        _append(res["future"], gt_dir / "demand_actuals.csv", first)
        orders.append(res["orders"])
        gt_stock.append(res["gt_stockout"])
        delists.append(res["delist"])
        profiles.append(res["profile"])
        if (j + 1) % max(1, p.n_stores // 10) == 0 or j == p.n_stores - 1:
            log(f"  simulated store {j + 1}/{p.n_stores}")

    po, events, lto = build_purchase_orders(p, m, pd.concat(orders, ignore_index=True))
    lto, gt_lt = inject_input_anomalies(p, lto)

    # assortment lifecycle derived from the simulation (phase_out -> delisted)
    mat = m.matrix.copy()
    dl = pd.concat(delists, ignore_index=True)
    gone = dl[dl["delisted_on"] >= 0]
    mat.loc[dl.loc[dl["phase_out"], "matrix_row"], "assortment_status"] = "phase_out"
    mat.loc[gone["matrix_row"], "assortment_status"] = "delisted"
    mat.loc[gone["matrix_row"], "ranged_to"] = [iso(p.history_start + timedelta(days=int(d))) for d in gone["delisted_on"]]

    tables = {"sku": m.sku, "store": m.store, "supplier": m.supplier,
              "supplier_sku": m.supplier_sku.drop(columns=["_sku", "_sup"]), "sku_store_matrix": mat,
              "purchase_order": po, "purchase_order_event": events, "lead_time_observation": lto}
    for name, df in tables.items():
        df.to_csv(inp / f"{name}.csv", index=False)

    st = pd.concat(gt_stock, ignore_index=True)
    gt_natural = pd.DataFrame({"entity_type": "sales_history", "entity_id": st["entity_id"],
                               "issue_type": "stockout_censored_demand", "origin": "natural",
                               "expected_behavior": STOCKOUT_EXPECTED, "observed_value": st["observed_value"],
                               "expected_value": st["expected_value"], "severity": "medium"})
    pd.concat(profiles, ignore_index=True).to_csv(gt_dir / "demand_profile.csv", index=False)
    dis = pd.DataFrame([{"supplier_id": m.supplier["supplier_id"].iloc[s],
                         "start_date": iso(m.hidden["sim_start"] + timedelta(days=a)),
                         "end_date": iso(m.hidden["sim_start"] + timedelta(days=b))} for s, a, b in m.hidden["disruptions"]])
    dis.to_csv(gt_dir / "supplier_disruption.csv", index=False)
    return {"gt_parts": [gt_natural, gt_lt]}
