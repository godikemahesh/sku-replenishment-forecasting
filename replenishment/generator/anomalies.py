"""Step 5 - inject labelled edge cases into otherwise valid records.

Every corrupted row gets a ground-truth label (issue_type, observed vs expected value) so
nothing is silently broken. Natural anomalies (stockout-censored sales) are labelled by
the simulator; the defects below are injected deliberately:

  negative_lead_time                 lead_time_observation  receipt recorded before the order (input data error)
  forecast_future_leakage            forecast (backtest)    model trained on holdout days it is scored on
  forecast_stockout_as_zero_demand   forecast (backtest)    model fitted on stockout zeros as if they were demand
  reorder_qty_ignores_pack_size      purchase_recommendation qty not rounded to the supplier pack
  discontinued_sku_recommended       purchase_recommendation positive order for a discontinued SKU
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from ..config import ANOMALY, Profile
from ..engine.forecast import Panel, forecast_panel, naive_forecast
from ..util import iso, rng

EXPECT = {
    "negative_lead_time": ("Reject/quarantine the observation; exclude it from lead-time mean and variance.", "high"),
    "forecast_future_leakage": ("Reject the forecast: training data must end on or before the forecast origin.", "high"),
    "forecast_stockout_as_zero_demand": ("Reject the forecast: stockout days are censored and must be excluded or imputed, not treated as zero demand.", "high"),
    "reorder_qty_ignores_pack_size": ("Block the order: quantity must be a multiple of the supplier pack size (and >= MOQ).", "high"),
    "discontinued_sku_recommended": ("Block the order: discontinued SKUs must never receive a positive replenishment quantity.", "high"),
}


def _n(issue: str, population: int) -> int:
    cfg = ANOMALY[issue]
    return min(population, max(cfg["min"], int(round(cfg["rate"] * population))))


def _gt(entity_type, ids, issue, observed, expected):
    exp, sev = EXPECT[issue]
    return pd.DataFrame({"entity_type": entity_type, "entity_id": list(ids), "issue_type": issue, "origin": "injected",
                         "expected_behavior": exp, "observed_value": list(observed), "expected_value": list(expected),
                         "severity": sev})


def inject_input_anomalies(p: Profile, lto: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    r = rng(p.seed, "anomaly", 1)
    lto = lto.copy()
    cand = np.where(lto["lead_time_days"].values > 0)[0]
    pick = np.sort(r.choice(cand, size=_n("negative_lead_time", len(cand)), replace=False))
    orig = lto.loc[pick, "lead_time_days"].values.copy()
    neg = -r.integers(1, 6, len(pick))
    lto.loc[pick, "lead_time_days"] = neg
    lto.loc[pick, "receipt_date"] = [iso(pd.Timestamp(d).date() + timedelta(days=int(x)))
                                     for d, x in zip(lto.loc[pick, "order_date"], neg)]
    lto.loc[pick, "delay_days"] = neg - lto.loc[pick, "nominal_lead_time_days"].values
    gt = _gt("lead_time_observation", lto.loc[pick, "lead_time_observation_id"], "negative_lead_time", neg, orig)
    return lto, gt


def inject_output_anomalies(p: Profile, panel: Panel, fc: pd.DataFrame, rec: pd.DataFrame, store: pd.DataFrame,
                            supplier_sku: pd.DataFrame, policy: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    r = rng(p.seed, "anomaly", 2)
    policy_lt = policy.set_index("reorder_policy_id")["lead_time_mean_days"].to_dict()
    fc, rec = fc.copy(), rec.copy()
    gts = []
    bt = fc.index[fc["forecast_type"] == "backtest"].values
    h0, h1 = p.backtest_origin + timedelta(days=1), p.history_end

    # --- future leakage: refit on data that includes the holdout itself
    leaked = forecast_panel(panel.truncate(p.history_end), p.history_end, (h0, h1), "backtest").set_index(["store_id", "sku_id"])
    alive = pd.MultiIndex.from_frame(fc.loc[bt, ["store_id", "sku_id"]]).isin(leaked.index)   # skip series delisted in the holdout
    cand = bt[alive]
    leak_rows = np.sort(r.choice(cand, size=_n("forecast_future_leakage", len(cand)), replace=False))
    key = list(zip(fc.loc[leak_rows, "store_id"], fc.loc[leak_rows, "sku_id"]))
    orig = fc.loc[leak_rows, "predicted_horizon_units"].values.copy()
    for col in ["predicted_daily_units", "predicted_daily_sd_units", "predicted_horizon_units",
                "train_days_used", "stockout_days_excluded", "promo_days_excluded"]:
        fc.loc[leak_rows, col] = leaked.loc[key, col].values
    fc.loc[leak_rows, "training_window_end_date"] = iso(p.history_end)
    gts.append(_gt("forecast", fc.loc[leak_rows, "forecast_id"], "forecast_future_leakage",
                   fc.loc[leak_rows, "predicted_horizon_units"], orig))

    # --- stockout treated as zero demand: censoring-naive fit on series that had stockouts
    remaining = np.setdiff1d(bt, leak_rows)
    cand = remaining[fc.loc[remaining, "stockout_days_excluded"].values >= 2]
    naive_rows = np.sort(r.choice(cand, size=_n("forecast_stockout_as_zero_demand", len(cand)), replace=False))
    train = panel.truncate(p.backtest_origin)
    pidx = pd.Series(np.arange(len(train.pairs)), index=pd.MultiIndex.from_frame(train.pairs[["store_id", "sku_id"]]))
    sel = pidx.loc[list(zip(fc.loc[naive_rows, "store_id"], fc.loc[naive_rows, "sku_id"]))].values
    H = int(fc.loc[naive_rows, "horizon_days"].iloc[0]) if len(naive_rows) else 1
    naive = naive_forecast(train, H)[sel]
    orig = fc.loc[naive_rows, "predicted_horizon_units"].values.copy()
    fc.loc[naive_rows, "predicted_horizon_units"] = np.round(naive, 3)
    fc.loc[naive_rows, "predicted_daily_units"] = np.round(naive / H, 4)
    fc.loc[naive_rows, "train_days_used"] = (~np.isnan(train.sales[sel])).sum(1)
    fc.loc[naive_rows, "stockout_days_excluded"] = 0
    gts.append(_gt("forecast", fc.loc[naive_rows, "forecast_id"], "forecast_stockout_as_zero_demand",
                   np.round(naive, 3), orig))

    # --- quantity not rounded to pack size (stays >= MOQ so only the pack rule is broken)
    q = rec["recommended_order_qty_units"]
    pk = rec["pack_size_units"].astype(float)
    cand = rec.index[(q > 0) & (pk > 1) & (q >= rec["moq_units"].astype(float) + pk)].values
    pack_rows = np.sort(r.choice(cand, size=_n("reorder_qty_ignores_pack_size", len(cand)), replace=False))
    orig = rec.loc[pack_rows, "recommended_order_qty_units"].values.copy()
    k = np.array([r.integers(1, int(x)) for x in rec.loc[pack_rows, "pack_size_units"]])
    rec.loc[pack_rows, "recommended_order_qty_units"] = orig - k
    gts.append(_gt("purchase_recommendation", rec.loc[pack_rows, "purchase_recommendation_id"],
                   "reorder_qty_ignores_pack_size", orig - k, orig))

    # --- discontinued SKU recommended (only where the store has room, so capacity stays valid)
    cap = store.set_index("store_id")["storage_capacity_units"]
    load = rec.groupby("store_id")[["inventory_position_units", "recommended_order_qty_units"]].sum().sum(1)
    room = (cap - load.reindex(cap.index, fill_value=0)).to_dict()
    cand = rec.index[(rec["recommendation_status"] == "blocked_discontinued") & rec["supplier_sku_id"].notna()].values
    cand = r.permutation(cand)
    want = _n("discontinued_sku_recommended", len(cand))
    disc_rows, qtys = [], []
    for i in cand:
        row = rec.loc[i]
        qty = int(max(row["moq_units"], np.ceil(max(row["order_up_to_units"] - row["inventory_position_units"], 1)
                                                 / row["pack_size_units"]) * row["pack_size_units"]))
        if qty <= room.get(row["store_id"], 0):
            room[row["store_id"]] -= qty
            disc_rows.append(i)
            qtys.append(qty)
        if len(disc_rows) == want:
            break
    if disc_rows:
        disc_rows = np.array(disc_rows)
        rec.loc[disc_rows, "recommended_order_qty_units"] = qtys
        rec.loc[disc_rows, "recommendation_status"] = "order_recommended"
        gts.append(_gt("purchase_recommendation", rec.loc[disc_rows, "purchase_recommendation_id"],
                       "discontinued_sku_recommended", qtys, [0] * len(qtys)))
    # keep derived fields (value, ETA) consistent with the defective quantity
    touched = np.concatenate([pack_rows, np.asarray(disc_rows, dtype=int)])
    if len(touched):
        unit_cost = rec.loc[touched, "supplier_sku_id"].map(supplier_sku.set_index("supplier_sku_id")["unit_cost"])
        rec.loc[touched, "order_value"] = np.round(rec.loc[touched, "recommended_order_qty_units"] * unit_cost, 2)
    for i in np.asarray(disc_rows, dtype=int):
        lt = int(np.ceil(policy_lt.get(rec.at[i, "reorder_policy_id"], 0) - 1e-9))
        rec.at[i, "expected_receipt_date"] = iso(p.recommendation_date + timedelta(days=lt))
    return fc, rec, pd.concat(gts, ignore_index=True)
