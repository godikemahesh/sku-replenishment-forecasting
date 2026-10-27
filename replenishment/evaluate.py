"""Evaluation: the only module (besides validation) allowed to read ground truth.

1. Forecast accuracy on the temporal holdout (backtest) against TRUE demand, versus the
   censoring-naive benchmark and versus the injected defective forecasts.
2. Production forecast accuracy against the forecast-month demand.
3. Stockout-censoring identification (flag vs true censoring).
4. Replenishment KPIs plus a forward check: does inventory position after the order cover
   true demand over the protection period (L + R)? Engine vs naive policy vs no order.
5. Audit-gate precision/recall per injected issue type.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Profile
from .io import read_table
from .util import iso

RULE_ISSUE = {"LT01": "negative_lead_time", "FC01": "forecast_future_leakage", "FC02": "forecast_stockout_as_zero_demand",
              "PR01": "reorder_qty_ignores_pack_size", "PR03": "discontinued_sku_recommended"}


def _acc(f, a) -> dict:
    f, a = np.asarray(f, float), np.asarray(a, float)
    tot = a.sum()
    return {"n": int(len(a)), "actual_units": round(float(tot), 1), "forecast_units": round(float(f.sum()), 1),
            "wape": round(float(np.abs(f - a).sum() / tot), 4) if tot else None,
            "bias": round(float((f - a).sum() / tot), 4) if tot else None,
            "mae_per_series": round(float(np.abs(f - a).mean()), 3) if len(a) else None}


def _sales(root, lo=None):
    cols = ["sale_id", "store_id", "sku_id", "sale_date", "units_sold", "promotion_flag", "stockout_flag"]
    parts = [c if lo is None else c[c["sale_date"] > lo] for c in read_table(root, "sales_history", usecols=cols, chunksize=2_000_000)]
    return pd.concat(parts, ignore_index=True)


def _naive(sales: pd.DataFrame, origin: str, window: int = 28) -> pd.Series:
    lo = iso(pd.Timestamp(origin).date() - timedelta(days=window - 1))
    w = sales[(sales["sale_date"] >= lo) & (sales["sale_date"] <= origin) & ~sales["promotion_flag"]]
    return w.groupby(["store_id", "sku_id"])["units_sold"].mean()


def evaluate(p: Profile, root: Path, clean_rec: pd.DataFrame, naive_rec: pd.DataFrame, policy: pd.DataFrame) -> dict:
    gt = read_table(root, "ground_truth")
    fc = read_table(root, "forecast")
    prof = read_table(root, "demand_profile")
    actual_m = read_table(root, "demand_actuals")
    audit = read_table(root, "audit_exception")
    res = {"profile": p.name, "seed": p.seed}

    # ------------------------------------------------------------ 1. backtest on temporal holdout
    bo = iso(p.backtest_origin)
    sales = _sales(root, lo=iso(p.backtest_origin - timedelta(days=27)))
    latent = gt[gt["issue_type"] == "stockout_censored_demand"].set_index("entity_id")["expected_value"]
    hold = sales[sales["sale_date"] > bo].copy()
    hold["true_demand"] = hold["sale_id"].map(latent).fillna(hold["units_sold"])
    hold = hold[~hold["promotion_flag"]]
    agg = hold.groupby(["store_id", "sku_id"]).agg(days=("sale_date", "size"), true=("true_demand", "sum"),
                                                   censored_days=("stockout_flag", "sum"))
    bt = fc[fc["forecast_type"] == "backtest"].merge(agg.reset_index(), on=["store_id", "sku_id"], how="inner")
    bt["f_engine"] = bt["predicted_daily_units"] * bt["days"]
    bt["f_naive"] = pd.MultiIndex.from_frame(bt[["store_id", "sku_id"]]).map(_naive(sales, bo)).fillna(0).values * bt["days"]
    bt = bt.merge(prof[["store_id", "sku_id", "true_demand_class"]], on=["store_id", "sku_id"], how="left")
    inj = gt[gt["origin"] == "injected"].set_index("entity_id")
    bt["issue"] = bt["forecast_id"].map(inj["issue_type"]).fillna("clean")
    clean = bt[bt["issue"] == "clean"]
    w28 = sales[(sales["sale_date"] > iso(p.backtest_origin - timedelta(days=28))) & (sales["sale_date"] <= bo)]
    recent_so = w28.groupby(["store_id", "sku_id"])["stockout_flag"].sum()
    clean = clean.assign(_so28=pd.MultiIndex.from_frame(clean[["store_id", "sku_id"]]).map(recent_so).fillna(0).values)
    stocky = clean[clean["_so28"] >= 3]
    res["backtest"] = {
        "holdout": f"{iso(p.backtest_origin + timedelta(days=1))}..{iso(p.history_end)} ({p.holdout_days} days, promotion days excluded)",
        "target": "true (unconstrained) demand = observed sales on in-stock days + latent demand on stockout days",
        "engine_censoring_aware": _acc(clean["f_engine"], clean["true"]),
        "naive_last28_mean_incl_stockouts": _acc(clean["f_naive"], clean["true"]),
        "series_with_3plus_stockout_days_in_last_28": {"engine": _acc(stocky["f_engine"], stocky["true"]),
                                            "naive": _acc(stocky["f_naive"], stocky["true"])},
        "by_engine_demand_class": {k: _acc(g["f_engine"], g["true"]) for k, g in clean.groupby("demand_class")},
        "by_true_demand_class": {k: _acc(g["f_engine"], g["true"]) for k, g in clean.groupby("true_demand_class")},
    }
    for issue in ["forecast_future_leakage", "forecast_stockout_as_zero_demand"]:
        g = bt[bt["issue"] == issue]
        orig = g["forecast_id"].map(inj["expected_value"]) / g["horizon_days"] * g["days"]
        res["backtest"][f"injected_{issue}"] = {"defective": _acc(g["f_engine"], g["true"]),
                                                "same_series_clean_engine": _acc(orig, g["true"])}

    # ------------------------------------------------------------ 2. production month
    am = actual_m.groupby(["store_id", "sku_id"])["true_demand_units"].sum().rename("true").reset_index()
    pr = fc[fc["forecast_type"] == "production"].merge(am, on=["store_id", "sku_id"], how="inner")
    full = _sales(root, lo=iso(p.history_end - timedelta(days=27)))
    pr["f_naive"] = pd.MultiIndex.from_frame(pr[["store_id", "sku_id"]]).map(_naive(full, iso(p.history_end))).fillna(0).values * pr["horizon_days"]
    res["production"] = {
        "month": p.forecast_month,
        "engine": _acc(pr["predicted_horizon_units"], pr["true"]),
        "naive_last28_mean_incl_stockouts": _acc(pr["f_naive"], pr["true"]),
        "by_method": {k: _acc(g["predicted_horizon_units"], g["true"]) for k, g in pr.groupby("method")},
        "seasonal_index_used": bool((pr["seasonal_index"] != 1).any()),
    }

    # ------------------------------------------------------------ 3. censoring identification
    tp = fp = fn = n = zero_in = zero_out = 0
    cens_ids = set(latent.index)
    for c in read_table(root, "sales_history", usecols=["sale_id", "units_sold", "stockout_flag"], chunksize=2_000_000):
        truth = c["sale_id"].isin(cens_ids)
        tp += int((truth & c["stockout_flag"]).sum())
        fp += int((~truth & c["stockout_flag"]).sum())
        fn += int((truth & ~c["stockout_flag"]).sum())
        zero_in += int(((c["units_sold"] == 0) & ~c["stockout_flag"]).sum())
        zero_out += int(((c["units_sold"] == 0) & c["stockout_flag"]).sum())
        n += len(c)
    res["censoring"] = {
        "sales_rows": n, "stockout_flagged_rows": tp + fp, "truly_censored_rows": tp + fn,
        "recall": round(tp / max(tp + fn, 1), 4), "precision": round(tp / max(tp + fp, 1), 4),
        "stockout_rate": round((tp + fp) / max(n, 1), 4),
        "zero_sales_in_stock_rows (true zero demand)": zero_in, "zero_sales_stockout_rows (censored, NOT zero demand)": zero_out,
        "lost_units_on_censored_days": int((gt.loc[gt["issue_type"] == "stockout_censored_demand", "expected_value"]
                                            - gt.loc[gt["issue_type"] == "stockout_censored_demand", "observed_value"]).sum()),
        "note": "precision < 1 is expected: a shelf can be empty while demand was zero or exactly equal to stock - flagged as possibly censored, not truly censored",
    }

    # ------------------------------------------------------------ 4. replenishment
    def coverage(rec):
        r = rec.merge(policy[["reorder_policy_id", "lead_time_mean_days", "review_period_days", "policy_status"]],
                      on="reorder_policy_id")
        r = r[r["policy_status"] == "active"].copy()
        r["P"] = np.minimum(np.ceil(r["lead_time_mean_days"] - 1e-9) + r["review_period_days"], 31).astype(int)
        a = actual_m.copy()
        a["day"] = (pd.to_datetime(a["demand_date"]) - pd.Timestamp(p.forecast_start)).dt.days + 1
        a = a.merge(r[["store_id", "sku_id", "P"]], on=["store_id", "sku_id"])
        dem = a[a["day"] <= a["P"]].groupby(["store_id", "sku_id"])["true_demand_units"].sum()
        r["demand_P"] = pd.MultiIndex.from_frame(r[["store_id", "sku_id"]]).map(dem).fillna(0).values
        after = r["inventory_position_units"] + r["recommended_order_qty_units"]
        return {"series": int(len(r)),
                "coverage_rate": round(float((after >= r["demand_P"]).mean()), 4),
                "shortfall_units": int(np.maximum(r["demand_P"] - after, 0).sum()),
                "units_ordered": int(r["recommended_order_qty_units"].sum()),
                "no_order_coverage_rate": round(float((r["inventory_position_units"] >= r["demand_P"]).mean()), 4)}

    q = clean_rec["recommended_order_qty_units"]
    pk = clean_rec["pack_size_units"].astype(float)
    mq = clean_rec["moq_units"].astype(float)
    store = read_table(root, "store").set_index("store_id")["storage_capacity_units"]
    by_store = clean_rec.groupby("store_id")[["inventory_position_units", "recommended_order_qty_units"]].sum()
    load = by_store.sum(1)
    ordering_over = (by_store["recommended_order_qty_units"] > 0) & (load > store.reindex(load.index))
    res["replenishment"] = {
        "recommendation_date": iso(p.recommendation_date),
        "lines": int(len(clean_rec)),
        "status_counts": clean_rec["recommendation_status"].value_counts().to_dict(),
        "units_recommended": int(q.sum()), "order_value_inr": round(float(clean_rec["order_value"].sum()), 2),
        "manual_review_lines": int(clean_rec["manual_review_flag"].sum()),
        "compliance": {"pack_size": round(float(((q == 0) | (q % pk.fillna(1) == 0)).mean()), 4),
                       "moq": round(float(((q == 0) | (q >= mq)).mean()), 4),
                       "capacity_ordering_stores_within_limit": round(1 - float(ordering_over.mean()), 4),
                       "stores_already_over_capacity_without_orders": int(((by_store["recommended_order_qty_units"] == 0)
                                                                          & (load > store.reindex(load.index))).sum()),
                       "discontinued_blocked": bool((clean_rec.loc[clean_rec["recommendation_status"] == "blocked_discontinued",
                                                                   "recommended_order_qty_units"] == 0).all())},
        "store_capacity_utilisation_after_orders": {"max": round(float((load / store.reindex(load.index)).max()), 3),
                                                    "median": round(float((load / store.reindex(load.index)).median()), 3)},
        "forward_check_engine": coverage(clean_rec),
        "forward_check_naive_forecast_policy": coverage(naive_rec),
        "forward_check_note": "coverage = share of orderable series whose inventory position after ordering >= true demand over L+R days of the forecast month",
    }

    # ------------------------------------------------------------ 5. audit detection vs ground truth
    det = {}
    defects = audit[audit["exception_category"] == "data_defect"]
    for rule, issue in RULE_ISSUE.items():
        labelled = set(inj.index[inj["issue_type"] == issue])
        flagged = set(defects.loc[defects["rule_id"] == rule, "entity_id"])
        tp_ = len(labelled & flagged)
        det[issue] = {"rule": rule, "labelled": len(labelled), "flagged": len(flagged), "true_positive": tp_,
                      "precision": round(tp_ / len(flagged), 4) if flagged else None,
                      "recall": round(tp_ / len(labelled), 4) if labelled else None}
    other = defects[~defects["rule_id"].isin(RULE_ISSUE)]
    res["audit_detection"] = {"by_issue": det, "unexpected_defect_exceptions": int(len(other)),
                              "operational_exceptions": audit.loc[audit["exception_category"] == "operational", "rule_id"].value_counts().to_dict()}
    return res


def _fmt(a):
    return "n/a" if a is None else f"{a:.1%}"


def to_markdown(r: dict) -> str:
    b, pr, c, rp, ad = r["backtest"], r["production"], r["censoring"], r["replenishment"], r["audit_detection"]
    L = [f"# Evaluation report - profile `{r['profile']}` (seed {r['seed']})", "",
         "## 1. Forecast accuracy on the temporal holdout", "",
         f"Holdout: {b['holdout']}. Target: {b['target']}.", "",
         "| Model | Series | WAPE | Bias |", "|---|---:|---:|---:|",
         f"| Engine (censoring-aware) | {b['engine_censoring_aware']['n']} | {_fmt(b['engine_censoring_aware']['wape'])} | {_fmt(b['engine_censoring_aware']['bias'])} |",
         f"| Naive 28-day mean (stockout zeros counted as demand) | {b['naive_last28_mean_incl_stockouts']['n']} | {_fmt(b['naive_last28_mean_incl_stockouts']['wape'])} | {_fmt(b['naive_last28_mean_incl_stockouts']['bias'])} |",
         f"| Engine - series with >=3 stockout days in the 28 days before origin | {b['series_with_3plus_stockout_days_in_last_28']['engine']['n']} | {_fmt(b['series_with_3plus_stockout_days_in_last_28']['engine']['wape'])} | {_fmt(b['series_with_3plus_stockout_days_in_last_28']['engine']['bias'])} |",
         f"| Naive - same series | {b['series_with_3plus_stockout_days_in_last_28']['naive']['n']} | {_fmt(b['series_with_3plus_stockout_days_in_last_28']['naive']['wape'])} | {_fmt(b['series_with_3plus_stockout_days_in_last_28']['naive']['bias'])} |",
         "", "Injected defective forecasts versus the clean engine forecast on the same series:", "",
         "| Defect | Series | Defective WAPE / bias | Clean WAPE / bias |", "|---|---:|---:|---:|"]
    for k in ["forecast_future_leakage", "forecast_stockout_as_zero_demand"]:
        d = b[f"injected_{k}"]
        L.append(f"| {k} | {d['defective']['n']} | {_fmt(d['defective']['wape'])} / {_fmt(d['defective']['bias'])} | "
                 f"{_fmt(d['same_series_clean_engine']['wape'])} / {_fmt(d['same_series_clean_engine']['bias'])} |")
    L += ["", "Engine accuracy by demand class (engine's classification):", "", "| Class | Series | WAPE | Bias |", "|---|---:|---:|---:|"]
    L += [f"| {k} | {v['n']} | {_fmt(v['wape'])} | {_fmt(v['bias'])} |" for k, v in b["by_engine_demand_class"].items()]
    L += ["", f"## 2. Production forecast ({pr['month']}) vs true demand", "",
          "| Model | Series | WAPE | Bias |", "|---|---:|---:|---:|",
          f"| Engine | {pr['engine']['n']} | {_fmt(pr['engine']['wape'])} | {_fmt(pr['engine']['bias'])} |",
          f"| Naive 28-day mean (stockout zeros counted as demand) | {pr['naive_last28_mean_incl_stockouts']['n']} | {_fmt(pr['naive_last28_mean_incl_stockouts']['wape'])} | {_fmt(pr['naive_last28_mean_incl_stockouts']['bias'])} |"]
    L += [f"| - method `{k}` | {v['n']} | {_fmt(v['wape'])} | {_fmt(v['bias'])} |" for k, v in pr["by_method"].items()]
    L += ["", f"Seasonal (year-over-year) index applied: {pr['seasonal_index_used']}", "",
          "## 3. Stockout censoring identification", "",
          f"- Sales rows: {c['sales_rows']:,}; stockout-flagged: {c['stockout_flagged_rows']:,} ({_fmt(c['stockout_rate'])})",
          f"- Truly censored rows (demand > available): {c['truly_censored_rows']:,} - recall **{_fmt(c['recall'])}**, precision {_fmt(c['precision'])}",
          f"- Zero-sales days in stock (genuine zero demand): {c['zero_sales_in_stock_rows (true zero demand)']:,}",
          f"- Zero-sales days during a stockout (censored, NOT zero demand): {c['zero_sales_stockout_rows (censored, NOT zero demand)']:,}",
          f"- Lost sales on censored days: {c['lost_units_on_censored_days']:,} units", "",
          f"## 4. Replenishment recommendations ({rp['recommendation_date']})", "",
          f"- Lines: {rp['lines']:,}; units recommended: {rp['units_recommended']:,}; value: INR {rp['order_value_inr']:,.0f}; manual review: {rp['manual_review_lines']}",
          "- Status: " + ", ".join(f"{k} {v}" for k, v in rp["status_counts"].items()),
          f"- Compliance: pack size {_fmt(rp['compliance']['pack_size'])}, MOQ {_fmt(rp['compliance']['moq'])}, "
          f"ordering stores within capacity {_fmt(rp['compliance']['capacity_ordering_stores_within_limit'])}, discontinued blocked: {rp['compliance']['discontinued_blocked']}",
          f"- Stores already over capacity before ordering (orders withheld, raised as exceptions): {rp['compliance']['stores_already_over_capacity_without_orders']}",
          f"- Store capacity utilisation after orders: median {rp['store_capacity_utilisation_after_orders']['median']:.0%}, max {rp['store_capacity_utilisation_after_orders']['max']:.0%}",
          "", "Forward check against true demand over each series' protection period (L + R):", "",
          "| Policy | Series | Coverage | Shortfall units | Units ordered |", "|---|---:|---:|---:|---:|"]
    for name, k in [("Engine (censoring-aware forecast)", "forward_check_engine"), ("Same policy, naive forecast", "forward_check_naive_forecast_policy")]:
        f = rp[k]
        L.append(f"| {name} | {f['series']} | {_fmt(f['coverage_rate'])} | {f['shortfall_units']:,} | {f['units_ordered']:,} |")
    L.append(f"| No order placed | {rp['forward_check_engine']['series']} | {_fmt(rp['forward_check_engine']['no_order_coverage_rate'])} | - | 0 |")
    L += ["", "## 5. Audit gate vs ground truth (injected defects)", "",
          "| Issue | Rule | Labelled | Flagged | Precision | Recall |", "|---|---|---:|---:|---:|---:|"]
    L += [f"| {k} | {v['rule']} | {v['labelled']} | {v['flagged']} | {_fmt(v['precision'])} | {_fmt(v['recall'])} |" for k, v in ad["by_issue"].items()]
    L += ["", f"Unexpected data-defect exceptions: {ad['unexpected_defect_exceptions']}. Operational exceptions: "
          + (", ".join(f"{k} {v}" for k, v in ad["operational_exceptions"].items()) or "none"), ""]
    return "\n".join(L)
