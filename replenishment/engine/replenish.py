"""Reorder policy and purchase recommendations.

Policy (periodic review, order-up-to), per SKU-store:
    SS  = ceil(z * sqrt((L + R) * sd_d^2 + d^2 * sd_L^2))
    ROP = ceil(d * L) + SS
    OUT = ceil(d * (L + R)) + SS
Decision on the recommendation date:
    IP = on_hand + on_order
    if SKU discontinued            -> blocked_discontinued (qty 0)
    elif no active supplier        -> blocked_no_active_supplier (qty 0)
    elif IP > ROP                  -> no_order_needed
    else qty = ceil(max(OUT - IP, MOQ) / pack) * pack
         then store capacity is allocated to the lowest days-of-cover lines first;
         a line that does not fit is cut to the largest pack multiple >= MOQ that fits
         (capacity_reduced) or to zero (capacity_exception).
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import NormalDist

import numpy as np
import pandas as pd

from ..config import ENGINE, Profile
from ..util import iso


def lead_time_stats(lto: pd.DataFrame, origin: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """Valid lead-time observations only; negative values are quarantined and counted."""
    lo = iso(origin - timedelta(days=ENGINE["lt_lookback_days"]))
    window = lto[(lto["receipt_date"] <= iso(origin)) & (lto["order_date"] >= lo)]
    valid = window[window["lead_time_days"] >= 0]
    rejected = lto[lto["lead_time_days"] < 0].groupby(["supplier_id", "sku_id"]).size()
    by_ss = valid.groupby(["supplier_id", "sku_id"])["lead_time_days"].agg(["count", "mean", "std"])
    by_sup = valid.groupby("supplier_id")["delay_days"].agg(["count", "mean", "std"])
    return by_ss, by_sup, rejected


def active_contracts(supplier_sku: pd.DataFrame, on: date) -> pd.DataFrame:
    d = iso(on)
    ss = supplier_sku[supplier_sku["is_active"].astype(bool)
                      & (supplier_sku["effective_from"] <= d)
                      & (supplier_sku["effective_to"].isna() | (supplier_sku["effective_to"] >= d))]
    return ss.sort_values(["sku_id", "priority"]).drop_duplicates("sku_id")


def build_policies(p: Profile, fc: pd.DataFrame, sku: pd.DataFrame, supplier_sku: pd.DataFrame,
                   lto: pd.DataFrame) -> pd.DataFrame:
    on = p.recommendation_date
    R = p.review_period_days
    df = fc[["forecast_id", "store_id", "sku_id", "predicted_daily_units", "predicted_daily_sd_units"]].copy()
    df = df.merge(sku[["sku_id", "unit_price", "lifecycle_state", "discontinue_date"]], on="sku_id", how="left")

    # ABC class by forecast revenue within each store (80% / 15% / 5%)
    df["_rev"] = df["predicted_daily_units"] * df["unit_price"]
    df = df.sort_values(["store_id", "_rev", "sku_id"], ascending=[True, False, True])
    tot = df.groupby("store_id")["_rev"].transform("sum")
    share = (df.groupby("store_id")["_rev"].cumsum() - df["_rev"]) / tot.replace(0, np.nan)
    a, b = ENGINE["abc_cutoffs"]
    df["abc_class"] = np.where(share.fillna(1) < a, "A", np.where(share.fillna(1) < b, "B", "C"))
    df["service_level_target"] = df["abc_class"].map(ENGINE["service_level_by_abc"])
    df["z_value"] = df["service_level_target"].map(lambda s: round(NormalDist().inv_cdf(s), 4))

    contracts = active_contracts(supplier_sku, on)
    df = df.merge(contracts[["sku_id", "supplier_sku_id", "supplier_id", "nominal_lead_time_days"]], on="sku_id", how="left")

    by_ss, by_sup, rejected = lead_time_stats(lto, p.history_end)
    key = list(zip(df["supplier_id"], df["sku_id"]))
    n_ss = np.array([by_ss["count"].get(k, 0) for k in key])
    m_ss = np.array([by_ss["mean"].get(k, np.nan) for k in key])
    s_ss = np.array([by_ss["std"].get(k, np.nan) for k in key])
    n_sup = df["supplier_id"].map(by_sup["count"]).fillna(0).values
    d_sup = df["supplier_id"].map(by_sup["mean"]).values
    sd_sup = df["supplier_id"].map(by_sup["std"]).values
    nominal = df["nominal_lead_time_days"].values.astype(float)
    min_obs = ENGINE["min_lt_observations"]
    has = df["supplier_sku_id"].notna().values
    src = np.where(~has, "none", np.where(n_ss >= min_obs, "supplier_sku_history",
                   np.where(n_sup >= min_obs, "supplier_history", "nominal_default")))
    L = np.select([src == "supplier_sku_history", src == "supplier_history", src == "nominal_default"],
                  [m_ss, nominal + np.nan_to_num(d_sup), nominal], 0.0)
    sL = np.select([src == "supplier_sku_history", src == "supplier_history", src == "nominal_default"],
                   [np.nan_to_num(s_ss), np.nan_to_num(sd_sup), 0.25 * nominal], 0.0)
    L = np.round(np.maximum(np.nan_to_num(L), 0), 3)
    sL = np.round(np.maximum(np.nan_to_num(sL), 0.5 * has), 3)

    d = df["predicted_daily_units"].values
    sd = df["predicted_daily_sd_units"].values
    z = df["z_value"].values
    ss_units = np.ceil(z * np.sqrt((L + R) * sd ** 2 + d ** 2 * sL ** 2) - 1e-9)
    df["lead_time_source"] = src
    df["lead_time_mean_days"] = L
    df["lead_time_sd_days"] = sL
    df["lead_time_obs_used"] = np.where(src == "supplier_sku_history", n_ss,
                                        np.where(src == "supplier_history", n_sup, 0)).astype(int)
    df["lead_time_obs_rejected"] = [int(rejected.get(k, 0)) for k in key]
    df["safety_stock_units"] = ss_units.astype(int)
    # ROP/OUT use the rounded L so downstream reconciliation is exact
    Lr = df["lead_time_mean_days"].values
    df["reorder_point_units"] = (np.ceil(d * Lr - 1e-9) + ss_units).astype(int)
    df["order_up_to_units"] = (np.ceil(d * (Lr + R) - 1e-9) + ss_units).astype(int)
    discontinued = (df["lifecycle_state"] == "discontinued") | (df["discontinue_date"].fillna("9999") < iso(on))
    df["policy_status"] = np.where(discontinued, "blocked_discontinued",
                                   np.where(has, "active", "blocked_no_active_supplier"))
    df["reorder_policy_id"] = "RP-" + df["store_id"] + "-" + df["sku_id"] + "-" + p.forecast_month
    df["effective_from"] = iso(on)
    df["effective_to"] = iso(p.forecast_end)
    df["review_period_days"] = R
    df["demand_daily_units"] = d
    df["demand_daily_sd_units"] = sd
    cols = ["reorder_policy_id", "store_id", "sku_id", "forecast_id", "supplier_sku_id", "effective_from",
            "effective_to", "abc_class", "service_level_target", "z_value", "review_period_days", "lead_time_source",
            "lead_time_mean_days", "lead_time_sd_days", "lead_time_obs_used", "lead_time_obs_rejected",
            "demand_daily_units", "demand_daily_sd_units", "safety_stock_units", "reorder_point_units",
            "order_up_to_units", "policy_status"]
    return df.sort_values(["store_id", "sku_id"])[cols].reset_index(drop=True)


def build_recommendations(p: Profile, policy: pd.DataFrame, fc: pd.DataFrame, store: pd.DataFrame,
                          supplier_sku: pd.DataFrame, position: pd.DataFrame) -> pd.DataFrame:
    """``position`` = end-of-history snapshot rows: store_id, sku_id, closing_on_hand_units, on_order_units."""
    on = p.recommendation_date
    df = policy.merge(position, on=["store_id", "sku_id"], how="left")
    df = df.merge(supplier_sku[["supplier_sku_id", "supplier_id", "pack_size_units", "moq_units", "unit_cost"]],
                  on="supplier_sku_id", how="left")
    df = df.merge(fc[["forecast_id", "method"]], on="forecast_id", how="left")
    df["on_hand_units"] = df["closing_on_hand_units"].fillna(0).astype(int)
    df["on_order_units"] = df["on_order_units"].fillna(0).astype(int)
    ip = (df["on_hand_units"] + df["on_order_units"]).values
    df["inventory_position_units"] = ip
    d = df["demand_daily_units"].values
    df["days_of_cover"] = np.round(np.minimum(np.where(d > 0, ip / np.maximum(d, 1e-9), 999.0), 999.0), 2)
    trig = ip <= df["reorder_point_units"].values
    df["reorder_triggered"] = trig
    need = np.where(trig, np.maximum(df["order_up_to_units"].values - ip, 0), 0)
    df["raw_need_units"] = need.astype(int)
    pack = df["pack_size_units"].fillna(1).values
    moq = df["moq_units"].fillna(0).values
    can = df["policy_status"].values == "active"
    want = trig & can & (need > 0)
    q = np.where(want, np.ceil(np.maximum(need, moq) / pack) * pack, 0).astype(int)
    df["qty_after_moq_pack_units"] = q

    status = np.select([df["policy_status"].values == "blocked_discontinued",
                        df["policy_status"].values == "blocked_no_active_supplier", ~want],
                       ["blocked_discontinued", "blocked_no_active_supplier", "no_order_needed"], "order_recommended").astype(object)
    final = q.copy()
    room_before = np.zeros(len(df), int)

    # capacity: units on hand + on order + new orders must fit the store
    cap = df["store_id"].map(store.set_index("store_id")["storage_capacity_units"]).values
    used = pd.Series(ip).groupby(df["store_id"].values).transform("sum").values
    df["_abc"] = df["abc_class"]
    order = df.assign(_i=np.arange(len(df)))[want].sort_values(["store_id", "days_of_cover", "_abc", "sku_id"])
    room = {s: max(0, int(c - u)) for s, c, u in zip(df["store_id"], cap, used)}
    for i, s in zip(order["_i"].values, order["store_id"].values):
        room_before[i] = room[s]
        if q[i] <= room[s]:
            room[s] -= q[i]
            continue
        cut = int(room[s] // pack[i] * pack[i])
        if cut >= max(moq[i], pack[i]):
            final[i], status[i] = cut, "capacity_reduced"
            room[s] -= cut
        else:
            final[i], status[i] = 0, "capacity_exception"
    # non-ordering lines still show the room available to them for traceability
    for i in np.where(~want)[0]:
        room_before[i] = room[df["store_id"].values[i]]

    df["capacity_room_units"] = room_before
    df["recommended_order_qty_units"] = final
    df["recommendation_status"] = status
    df["order_value"] = np.round(final * df["unit_cost"].fillna(0).values, 2)
    lt = np.ceil(df["lead_time_mean_days"].values - 1e-9).astype(int)
    df["expected_receipt_date"] = [iso(on + timedelta(days=int(x))) if f > 0 else None for x, f in zip(lt, final)]
    df["recommendation_date"] = iso(on)
    df["manual_review_flag"] = (df["method"].isin(["new_item_shrinkage", "censored_prior"])
                                | (df["lead_time_source"] == "nominal_default")
                                | (df["recommendation_status"] == "capacity_exception")).values
    df["rule_trace"] = [_trace(r) for r in df.itertuples(index=False)]
    df["moq_units"] = df["moq_units"].astype("Int64")
    df["pack_size_units"] = df["pack_size_units"].astype("Int64")
    df = df.sort_values(["store_id", "sku_id"]).reset_index(drop=True)
    df["purchase_recommendation_id"] = [f"PO-{on.year}-{i + 1:06d}" for i in range(len(df))]
    cols = ["purchase_recommendation_id", "reorder_policy_id", "forecast_id", "store_id", "sku_id", "supplier_id",
            "supplier_sku_id", "recommendation_date", "on_hand_units", "on_order_units", "inventory_position_units",
            "reorder_point_units", "order_up_to_units", "days_of_cover", "reorder_triggered", "raw_need_units",
            "moq_units", "pack_size_units", "qty_after_moq_pack_units", "capacity_room_units",
            "recommended_order_qty_units", "order_value", "expected_receipt_date", "recommendation_status",
            "manual_review_flag", "rule_trace"]
    return df[cols]


def _trace(r) -> str:
    steps = [f"FORECAST d={r.demand_daily_units:.2f}/day ({r.method})",
             f"POLICY L={r.lead_time_mean_days}d({r.lead_time_source}) SS={r.safety_stock_units} ROP={r.reorder_point_units} OUT={r.order_up_to_units}"]
    if r.policy_status == "blocked_discontinued":
        return " > ".join(steps + ["LIFECYCLE discontinued: BLOCK"])
    if r.policy_status == "blocked_no_active_supplier":
        return " > ".join(steps + ["SUPPLIER no active contract: BLOCK"])
    ip = r.inventory_position_units
    if not r.reorder_triggered:
        return " > ".join(steps + [f"IP={ip} > ROP: NO ORDER"])
    steps.append(f"IP={ip} <= ROP: TRIGGER need={r.raw_need_units}")
    if r.raw_need_units < r.moq_units:
        steps.append(f"MOQ {int(r.moq_units)}: RAISED")
    if r.qty_after_moq_pack_units != max(r.raw_need_units, r.moq_units):
        steps.append(f"PACK {int(r.pack_size_units)}: ROUNDED to {r.qty_after_moq_pack_units}")
    if r.recommendation_status == "capacity_reduced":
        steps.append(f"CAPACITY room={r.capacity_room_units}: CUT to {r.recommended_order_qty_units}")
    elif r.recommendation_status == "capacity_exception":
        steps.append(f"CAPACITY room={r.capacity_room_units} < MOQ/pack: EXCEPTION qty 0")
    else:
        steps.append(f"CAPACITY room={r.capacity_room_units}: OK qty {r.recommended_order_qty_units}")
    return " > ".join(steps)
