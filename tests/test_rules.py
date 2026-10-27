"""Unit tests for the business rules the brief calls out explicitly."""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from replenishment.config import get_profile
from replenishment.engine.forecast import LeakageError, Panel, forecast_panel, naive_forecast
from replenishment.engine.replenish import build_recommendations, lead_time_stats

P = get_profile("sample")


def _policy(**over):
    row = dict(reorder_policy_id="RP-STORE-000001-SKU-000001-2026-01", store_id="STORE-000001", sku_id="SKU-000001",
               forecast_id="FC-STORE-000001-SKU-000001-2026-01", supplier_sku_id="SS-000001", abc_class="A",
               lead_time_mean_days=3.0, lead_time_source="supplier_sku_history", safety_stock_units=5,
               reorder_point_units=20, order_up_to_units=40, policy_status="active", demand_daily_units=5.0)
    row.update(over)
    return pd.DataFrame([row])


def _recommend(policy, on_hand=5, on_order=0, pack=12, moq=24, capacity=1000):
    fc = pd.DataFrame({"forecast_id": policy["forecast_id"], "method": "censored_ewma_dow"})
    store = pd.DataFrame({"store_id": ["STORE-000001"], "storage_capacity_units": [capacity]})
    ss = pd.DataFrame({"supplier_sku_id": ["SS-000001"], "supplier_id": ["SUP-0001"], "pack_size_units": [pack],
                       "moq_units": [moq], "unit_cost": [10.0]})
    pos = policy[["store_id", "sku_id"]].assign(closing_on_hand_units=on_hand, on_order_units=on_order)
    return build_recommendations(P, policy, fc, store, ss, pos).iloc[0]


def test_order_rounded_up_to_pack_and_moq():
    r = _recommend(_policy(), on_hand=5, pack=12, moq=24)        # need = 40 - 5 = 35
    assert r.recommendation_status == "order_recommended"
    assert r.recommended_order_qty_units == 36                   # ceil(35 / 12) * 12, >= MOQ 24
    r = _recommend(_policy(), on_hand=18, pack=12, moq=48)       # need = 22 -> MOQ 48
    assert r.recommended_order_qty_units == 48
    assert "MOQ 48: RAISED" in r.rule_trace


def test_no_order_above_reorder_point():
    r = _recommend(_policy(), on_hand=21)
    assert r.recommendation_status == "no_order_needed" and r.recommended_order_qty_units == 0


def test_discontinued_and_no_supplier_are_blocked():
    r = _recommend(_policy(policy_status="blocked_discontinued"), on_hand=0)
    assert r.recommendation_status == "blocked_discontinued" and r.recommended_order_qty_units == 0
    r = _recommend(_policy(policy_status="blocked_no_active_supplier", supplier_sku_id=None), on_hand=0)
    assert r.recommendation_status == "blocked_no_active_supplier" and r.recommended_order_qty_units == 0


def test_capacity_cuts_to_pack_multiple_or_raises_exception():
    r = _recommend(_policy(), on_hand=5, pack=12, moq=12, capacity=5 + 30)    # room 30 -> 24 fits
    assert r.recommendation_status == "capacity_reduced" and r.recommended_order_qty_units == 24
    r = _recommend(_policy(), on_hand=5, pack=12, moq=24, capacity=5 + 20)    # room 20 < MOQ
    assert r.recommendation_status == "capacity_exception" and r.recommended_order_qty_units == 0


def test_negative_lead_times_are_quarantined():
    lto = pd.DataFrame({"supplier_id": ["SUP-0001"] * 4, "sku_id": ["SKU-000001"] * 4,
                        "order_date": ["2025-12-01"] * 4, "receipt_date": ["2025-12-04", "2025-12-05", "2025-12-06", "2025-11-28"],
                        "lead_time_days": [3, 4, 5, -3], "delay_days": [0, 1, 2, -6]})
    by_ss, _, rejected = lead_time_stats(lto, date(2025, 12, 31))
    assert by_ss.loc[("SUP-0001", "SKU-000001"), "mean"] == 4.0
    assert rejected[("SUP-0001", "SKU-000001")] == 1


def _panel(sales, stockout=None, start=date(2025, 11, 1)):
    sales = np.asarray(sales, float)[None, :]
    dates = np.arange(np.datetime64(start), np.datetime64(start) + sales.shape[1])
    so = np.zeros_like(sales, bool) if stockout is None else np.asarray(stockout, bool)[None, :]
    pairs = pd.DataFrame({"store_id": ["STORE-000001"], "sku_id": ["SKU-000001"], "cat_idx": [0], "store_format": ["supermarket"]})
    return Panel(pairs, dates, sales, so, np.zeros_like(sales, bool))


def test_forecast_rejects_future_data():
    panel = _panel([5] * 40)
    origin = date(2025, 11, 30)                                  # panel runs to 2025-12-10
    with pytest.raises(LeakageError):
        forecast_panel(panel, origin, (origin + timedelta(days=1), origin + timedelta(days=7)), "backtest")
    ok = forecast_panel(panel.truncate(origin), origin, (origin + timedelta(days=1), origin + timedelta(days=7)), "backtest")
    assert ok.loc[0, "training_window_end_date"] == "2025-11-30"


def test_stockout_zeros_are_not_zero_demand():
    sales = [6] * 30 + [0] * 10                                  # last 10 days out of stock
    panel = _panel(sales, stockout=[False] * 30 + [True] * 10)
    origin = date(2025, 12, 10)
    fc = forecast_panel(panel, origin, (date(2025, 12, 11), date(2025, 12, 17)), "backtest").iloc[0]
    assert fc.stockout_days_excluded == 10
    assert fc.predicted_daily_units == pytest.approx(6, rel=0.05)       # censoring-aware: true rate
    assert naive_forecast(panel, 1)[0] == pytest.approx(6 * 18 / 28)    # naive: dragged down by stockout zeros


def test_in_stock_zero_sales_give_zero_forecast():
    panel = _panel([0] * 40)                                     # never sold while in stock
    origin = date(2025, 12, 10)
    fc = forecast_panel(panel, origin, (date(2025, 12, 11), date(2025, 12, 17)), "backtest").iloc[0]
    assert fc.method == "zero_demand" and fc.predicted_daily_units == 0
