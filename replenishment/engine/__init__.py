"""Forecast -> policy -> recommendation engine. Reads ONLY the input/ layer."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..config import Profile
from ..io import read_table
from ..util import iso
from .forecast import build_panel, run_forecasts
from .replenish import build_policies, build_recommendations


def load_engine_inputs(root: Path) -> dict:
    t = {n: read_table(root, n) for n in ["sku", "store", "supplier", "supplier_sku", "lead_time_observation"]}
    t["sales"] = read_table(root, "sales_history",
                            usecols=["store_id", "sku_id", "sale_date", "units_sold", "promotion_flag", "stockout_flag"])
    return t


def end_position(root: Path, history_end) -> pd.DataFrame:
    cols = ["store_id", "sku_id", "snapshot_date", "closing_on_hand_units", "on_order_units"]
    day = iso(history_end)
    parts = [c[c["snapshot_date"] == day] for c in read_table(root, "inventory_snapshot", usecols=cols, chunksize=2_000_000)]
    return pd.concat(parts, ignore_index=True).drop(columns="snapshot_date")


def run_engine(p: Profile, root: Path, t: dict | None = None) -> dict:
    t = t or load_engine_inputs(root)
    panel = build_panel(t["sales"], t["sku"], t["store"])
    fc = run_forecasts(panel, p.history_end, p.backtest_origin, p.forecast_end)
    prod = fc[fc["forecast_type"] == "production"]
    policy = build_policies(p, prod, t["sku"], t["supplier_sku"], t["lead_time_observation"])
    position = end_position(root, p.history_end)
    rec = build_recommendations(p, policy, prod, t["store"], t["supplier_sku"], position)
    return {"panel": panel, "forecast": fc, "reorder_policy": policy, "purchase_recommendation": rec,
            "position": position, "inputs": t}
