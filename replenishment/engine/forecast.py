"""Censoring-aware demand forecast.

Key rules
* Stockout days are **censored observations**, not zero demand: they are excluded from
  fitting. A series that only ever sold zero *while in stock* is the only way to get a
  zero forecast (method ``zero_demand``).
* Promotion days are excluded so the forecast is a baseline (no promotions are planned
  for the forecast month).
* The model only receives data up to ``origin``; ``Panel.truncate`` slices the arrays and
  ``assert_no_future`` raises if anything later slips in (future-leakage guard).
* Model choice follows the Syntetos-Boylan demand classes: EWMA with weekday profile for
  smooth/erratic series, Croston-SBA for intermittent/lumpy, shrinkage to a category prior
  for new items or series with no clean history.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..config import ENGINE
from ..schema import CATEGORIES
from ..util import iso, to_iso


class LeakageError(ValueError):
    """Raised when training data contains observations after the forecast origin."""


@dataclass
class Panel:
    pairs: pd.DataFrame        # store_id, sku_id, cat_idx, store_format
    dates: np.ndarray          # datetime64[D], contiguous
    sales: np.ndarray          # float64 [P, D]; NaN = not sellable that day
    stockout: np.ndarray       # bool [P, D]
    promo: np.ndarray          # bool [P, D]

    def truncate(self, origin: date) -> "Panel":
        k = int((np.datetime64(origin) - self.dates[0]).astype(int)) + 1
        return replace(self, dates=self.dates[:k], sales=self.sales[:, :k],
                       stockout=self.stockout[:, :k], promo=self.promo[:, :k])


def assert_no_future(dates, origin: date) -> None:
    latest = pd.Timestamp(np.max(dates)).date()
    if latest > origin:
        raise LeakageError(f"training data ends {latest} which is after forecast origin {origin}")


def build_panel(sales: pd.DataFrame, sku: pd.DataFrame, store: pd.DataFrame) -> Panel:
    """Pivot long sales rows into dense [pair, day] arrays."""
    pairs = (sales[["store_id", "sku_id"]].drop_duplicates()
             .sort_values(["store_id", "sku_id"]).reset_index(drop=True))
    pairs["p"] = np.arange(len(pairs))
    idx = sales[["store_id", "sku_id"]].merge(pairs, on=["store_id", "sku_id"], how="left")["p"].values
    d = pd.to_datetime(sales["sale_date"].astype(str)).values.astype("datetime64[D]")
    d0, d1 = d.min(), d.max()
    di = (d - d0).astype(int)
    dates = np.arange(d0, d1 + 1)
    shape = (len(pairs), len(dates))
    S = np.full(shape, np.nan)
    S[idx, di] = sales["units_sold"].values
    O = np.zeros(shape, bool)
    O[idx, di] = sales["stockout_flag"].values.astype(bool)
    M = np.zeros(shape, bool)
    M[idx, di] = sales["promotion_flag"].values.astype(bool)
    pairs = pairs.drop(columns="p").merge(sku[["sku_id", "category"]], on="sku_id", how="left") \
                 .merge(store[["store_id", "store_format"]], on="store_id", how="left")
    pairs["cat_idx"] = pairs["category"].map({c: i for i, c in enumerate(CATEGORIES)}).values
    return Panel(pairs, dates, S, O, M)


def _weekday(dates: np.ndarray) -> np.ndarray:
    return (dates.astype("datetime64[D]").view("int64") + 3) % 7    # Monday = 0


def _month(dates: np.ndarray) -> np.ndarray:
    return dates.astype("datetime64[M]").astype(int) % 12             # Jan = 0


def _group_mean(values, groups_a, groups_b, na, nb, min_n):
    s = np.bincount(groups_a * nb + groups_b, weights=values, minlength=na * nb).reshape(na, nb)
    c = np.bincount(groups_a * nb + groups_b, minlength=na * nb).reshape(na, nb)
    return np.where(c >= min_n, s / np.maximum(c, 1), np.nan)


def forecast_panel(train: Panel, origin: date, horizon: tuple[date, date], forecast_type: str) -> pd.DataFrame:
    assert_no_future(train.dates, origin)
    P, Dt = train.sales.shape
    nc = len(CATEGORIES)
    act = ~np.isnan(train.sales)
    y = np.nan_to_num(train.sales)
    clean = act & ~train.stockout & ~train.promo
    cat = train.pairs["cat_idx"].values
    wd = _weekday(train.dates)
    mon = _month(train.dates)

    n_clean = clean.sum(1)
    pair_mean = np.where(n_clean > 0, (y * clean).sum(1) / np.maximum(n_clean, 1), np.nan)
    ok = clean & (pair_mean[:, None] > 0)
    pi, ti = np.nonzero(ok)
    rel = y[pi, ti] / pair_mean[pi]

    # weekday profile per category (pooled across pairs for stability)
    dow_idx = _group_mean(rel, cat[pi], wd[ti], nc, 7, 30)
    dow_idx = np.where(np.isnan(dow_idx), 1.0, dow_idx)
    dow_idx = dow_idx / dow_idx.mean(1, keepdims=True)
    y_ds = y / dow_idx[cat][:, wd]

    # level: exponentially weighted mean of de-weekdayed clean observations
    w = 0.5 ** ((Dt - 1 - np.arange(Dt)) / ENGINE["ewma_half_life_days"])
    wc = clean * w[None, :]
    level_ewma = np.where(wc.sum(1) > 0, (y_ds * wc).sum(1) / np.maximum(wc.sum(1), 1e-12), np.nan)

    # Croston-SBA on clean days (censored days are skipped, not counted as zero)
    a = ENGINE["croston_alpha"]
    z, intv, q = np.zeros(P), np.zeros(P), np.zeros(P)
    seen = np.zeros(P, bool)
    for t in range(Dt):
        c = clean[:, t]
        q[c] += 1
        nz = c & (y[:, t] > 0)
        first, upd = nz & ~seen, nz & seen
        z[first], intv[first] = y[first, t], q[first]
        z[upd] += a * (y[upd, t] - z[upd])
        intv[upd] += a * (q[upd] - intv[upd])
        seen |= nz
        q[nz] = 0
    level_croston = np.where(seen, (1 - a / 2) * z / np.maximum(intv, 1e-12), 0.0)

    # demand classification (Syntetos-Boylan cut-offs ADI 1.32, CV^2 0.49)
    nz_mask = clean & (y > 0)
    n_nz = nz_mask.sum(1)
    s1 = (y * nz_mask).sum(1)
    s2 = (y ** 2 * nz_mask).sum(1)
    mean_nz = s1 / np.maximum(n_nz, 1)
    var_nz = np.maximum(s2 / np.maximum(n_nz, 1) - mean_nz ** 2, 0)
    cv2 = np.where(n_nz > 1, var_nz / np.maximum(mean_nz, 1e-12) ** 2, 0)
    adi = np.where(n_nz > 0, n_clean / np.maximum(n_nz, 1), np.inf)
    first_day = np.argmax(act, axis=1)
    age = Dt - first_day
    cls = np.where(adi < 1.32, np.where(cv2 < 0.49, "smooth", "erratic"),
                   np.where(cv2 < 0.49, "intermittent", "lumpy")).astype(object)
    method = np.where(np.isin(cls, ["smooth", "erratic"]), "censored_ewma_dow", "croston_sba").astype(object)
    level = np.where(method == "censored_ewma_dow", level_ewma, level_croston)

    established = (age >= ENGINE["new_item_days"]) & (n_clean >= 3)
    zero = established & (n_clean >= 14) & (n_nz == 0)
    new = (age < ENGINE["new_item_days"])
    no_clean = ~new & (n_clean < 3)

    # prior = median established level in the same category x store format (fallback: category, global)
    prior_df = pd.DataFrame({"cat": cat, "fmt": train.pairs["store_format"].values, "lvl": level})
    est = prior_df[established & ~zero & np.isfinite(level)]
    by_cf = est.groupby(["cat", "fmt"])["lvl"].median()
    by_c = est.groupby("cat")["lvl"].median()
    glob = float(est["lvl"].median()) if len(est) else 0.5
    prior = np.array([by_cf.get((c, f), by_c.get(c, glob)) for c, f in zip(cat, prior_df["fmt"])])
    k = ENGINE["new_item_prior_weight_days"]
    mean_clean_ds = np.where(n_clean > 0, (y_ds * clean).sum(1) / np.maximum(n_clean, 1), 0.0)
    shrunk = (n_clean * mean_clean_ds + k * prior) / (n_clean + k)

    level = np.where(new, shrunk, level)
    cls[new], method[new] = "new_item", "new_item_shrinkage"
    level = np.where(no_clean, shrunk, level)
    cls[no_clean], method[no_clean] = "no_clean_history", "censored_prior"
    level = np.where(zero, 0.0, level)
    method[zero] = "zero_demand"
    level = np.nan_to_num(level)

    # seasonal index: category month profile, only when the target month exists in training data
    days_per_month = np.bincount(mon, minlength=12)
    cat_month = _group_mean(y_ds[pi, ti] / (pair_mean[pi] / 1.0), cat[pi], mon[ti], nc, 12, 50)
    cat_month = np.where(days_per_month[None, :] >= 20, cat_month, np.nan)
    h_dates = np.arange(np.datetime64(horizon[0]), np.datetime64(horizon[1]) + 1)
    lw_months = _month(train.dates[-28:])
    h_months = _month(h_dates)
    num = np.nanmean(cat_month[:, h_months], axis=1) if np.all(days_per_month[np.unique(h_months)] >= 20) else np.full(nc, np.nan)
    den = np.nanmean(cat_month[:, lw_months], axis=1)
    s_cat = np.clip(np.where(np.isfinite(num / den), num / den, 1.0), 0.6, 1.6)
    s = s_cat[cat]

    # dispersion from recent clean residual spread
    recent = clean.copy()
    recent[:, :-ENGINE["level_window_days"]] = False
    n_r = recent.sum(1)
    m_r = (y_ds * recent).sum(1) / np.maximum(n_r, 1)
    v_r = ((y_ds - m_r[:, None]) ** 2 * recent).sum(1) / np.maximum(n_r - 1, 1)
    sd = np.where(n_r >= 5, np.sqrt(v_r), np.sqrt(np.maximum(level, 0.05)))
    sd = np.where(zero, 0.0, sd) * s

    h_wd = _weekday(h_dates)
    horizon_units = level * s * dow_idx[cat][:, h_wd].sum(1)
    H = len(h_dates)

    sellable = act[:, -1]
    month = iso(horizon[0])[:7]
    out = pd.DataFrame({
        "forecast_id": "FC-" + train.pairs["store_id"] + "-" + train.pairs["sku_id"] + "-" + month,
        "store_id": train.pairs["store_id"], "sku_id": train.pairs["sku_id"],
        "forecast_type": forecast_type, "forecast_month": month,
        "forecast_origin_date": iso(origin),
        "training_window_start_date": to_iso(train.dates[first_day]),
        "training_window_end_date": iso(train.dates[-1]),
        "horizon_start_date": iso(horizon[0]), "horizon_end_date": iso(horizon[1]), "horizon_days": H,
        "method": method, "demand_class": cls, "train_days_used": n_clean,
        "stockout_days_excluded": (act & train.stockout).sum(1),
        "promo_days_excluded": (act & train.promo & ~train.stockout).sum(1),
        "seasonal_index": np.round(s, 4),
        "predicted_daily_units": np.round(horizon_units / H, 4),
        "predicted_daily_sd_units": np.round(sd, 4),
        "predicted_horizon_units": np.round(horizon_units, 3),
    })
    return out[sellable].reset_index(drop=True)


def naive_forecast(train: Panel, horizon_days: int, window: int = 28) -> np.ndarray:
    """Censoring-NAIVE benchmark: mean of observed non-promotion sales over the last ``window``
    days with stockout zeros counted as demand - identical to the engine except for censoring. Used only for evaluation and to build the
    labelled ``forecast_stockout_as_zero_demand`` defect."""
    recent = np.where(train.promo[:, -window:], np.nan, train.sales[:, -window:])   # promo days excluded, stockouts kept
    with np.errstate(invalid="ignore"):
        m = np.nanmean(recent, axis=1)
    return np.nan_to_num(m) * horizon_days


def run_forecasts(panel: Panel, history_end: date, backtest_origin: date, forecast_end: date) -> pd.DataFrame:
    """Backtest on the temporal holdout + production forecast for the next month."""
    bt = forecast_panel(panel.truncate(backtest_origin), backtest_origin,
                        (backtest_origin + timedelta(days=1), history_end), "backtest")
    prod = forecast_panel(panel.truncate(history_end), history_end,
                          (history_end + timedelta(days=1), forecast_end), "production")
    return pd.concat([bt, prod], ignore_index=True)
