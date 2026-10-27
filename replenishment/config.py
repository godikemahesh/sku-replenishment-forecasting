"""Run profiles and generator parameters.

Every profile uses the same generator code; only scale changes. All randomness is
derived from ``Profile.seed`` so a profile always regenerates byte-identical files.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, replace
from datetime import date, timedelta

DEFAULT_SEED = 20261022


@dataclass(frozen=True)
class Profile:
    name: str
    n_skus: int
    n_stores: int
    n_days: int
    n_suppliers: int
    seed: int = DEFAULT_SEED
    history_end: date = date(2025, 12, 31)
    forecast_month: str = "2026-01"
    review_period_days: int = 7

    @property
    def history_start(self) -> date:
        return self.history_end - timedelta(days=self.n_days - 1)

    @property
    def holdout_days(self) -> int:
        """Length of the temporal holdout used for the backtest forecast."""
        return 28 if self.n_days >= 120 else 7

    @property
    def backtest_origin(self) -> date:
        return self.history_end - timedelta(days=self.holdout_days)

    @property
    def forecast_start(self) -> date:
        return self.history_end + timedelta(days=1)

    @property
    def forecast_end(self) -> date:
        y, m = (int(x) for x in self.forecast_month.split("-"))
        return date(y, m, calendar.monthrange(y, m)[1])

    @property
    def recommendation_date(self) -> date:
        return self.forecast_start


PROFILES = {
    # 30-day sample committed to the repo (fast, small enough to read by hand).
    "sample": Profile("sample", n_skus=100, n_stores=10, n_days=30, n_suppliers=12),
    # One full year at reduced width - exercises seasonality and year-over-year index.
    "dev": Profile("dev", n_skus=300, n_stores=20, n_days=365, n_suppliers=25),
    # Baseline volume requested in the brief.
    "full": Profile("full", n_skus=2000, n_stores=100, n_days=365, n_suppliers=60),
}


def get_profile(name: str, **overrides) -> Profile:
    base = PROFILES[name]
    overrides = {k: v for k, v in overrides.items() if v is not None}
    if overrides:
        base = replace(base, name=overrides.pop("name", f"{name}-custom"), **overrides)
    return base


# Generator knobs (documented in README "How the data is generated").
GEN = {
    "burn_in_days": 35,               # simulated before history start and discarded (no cold-start stockouts)
    "new_launch_rate": 0.05,          # share of SKUs launched inside the history window
    "discontinue_rate": 0.03,         # share of SKUs discontinued near the end of history
    "secondary_supplier_rate": 0.30,  # share of SKUs with a second (backup) supplier
    "supplier_link_end_rate": 0.02,   # share of SKUs whose primary supplier contract ends
    "new_store_rate": 0.03,           # share of stores opened during the window (>=180 days only)
    "promo_never_rate": 0.30,         # share of SKUs that are never promoted
    "promo_mean_gap_days": 45,
    "promo_participation": 0.90,      # probability a store runs a chain promotion
    "shrink_daily_prob": 0.004,
    "delay_prob_by_tier": {"A": 0.04, "B": 0.10, "C": 0.22},
    "disruption_delay_prob": 0.85,
}

# Engine knobs (documented in README "Forecast and replenishment rules").
ENGINE = {
    "service_level_by_abc": {"A": 0.98, "B": 0.95, "C": 0.90},
    "abc_cutoffs": (0.80, 0.95),
    "ewma_half_life_days": 14,
    "level_window_days": 56,
    "croston_alpha": 0.10,
    "new_item_days": 21,
    "new_item_prior_weight_days": 7,
    "min_lt_observations": 3,
    "lt_lookback_days": 180,
}

# Labelled anomaly injection (Step 5). min counts guarantee coverage in small profiles.
ANOMALY = {
    "negative_lead_time": {"rate": 0.002, "min": 3},
    "forecast_future_leakage": {"rate": 0.005, "min": 3},
    "forecast_stockout_as_zero_demand": {"rate": 0.005, "min": 3},
    "reorder_qty_ignores_pack_size": {"rate": 0.01, "min": 3},
    "discontinued_sku_recommended": {"rate": 0.25, "min": 2},
}

# Tables above this row count are exported as CSV only (JSON / SQL INSERTs would be multi-GB).
JSON_SQL_ROW_LIMIT = 500_000
