"""Step 2b - daily demand, inventory and legacy-replenishment simulation, one store at a time.

Latent demand is drawn first; observed sales are ``min(demand, available stock)``, so
stockouts censor sales exactly as in a real store. Stock is replenished by a simple
*legacy* policy (28-day moving average of observed sales, order-up-to with a fixed
over-cover) whose orders are delayed by supplier lead-time noise and disruptions.
Every derived value (receipts, closing stock, on-order, PO dates, lead times) is
computed from these events, never sampled independently.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..config import GEN, Profile
from ..schema import DAYS
from ..util import rng, to_iso
from .master import Master


@dataclass
class Supply:
    prim: np.ndarray        # per SKU: supplier_sku row of primary contract
    prim_end: np.ndarray    # per SKU: last day index the primary contract is usable
    sec: np.ndarray         # per SKU: supplier_sku row of backup contract or -1
    pack: np.ndarray        # per supplier_sku row
    moq: np.ndarray
    lnom: np.ndarray
    sup: np.ndarray
    delay_p: np.ndarray     # per supplier


def supply_arrays(p: Profile, m: Master) -> Supply:
    ss = m.supplier_sku
    n = len(m.sku)
    prim = np.full(n, -1)
    sec = np.full(n, -1)
    prim_end = np.full(n, 10 ** 6)
    for pos, (sku, pr, to) in enumerate(zip(ss["_sku"], ss["priority"], ss["effective_to"])):
        if pr == 1:
            prim[sku] = pos
            if to is not None:
                prim_end[sku] = (pd.Timestamp(to).date() - m.hidden["sim_start"]).days
        else:
            sec[sku] = pos
    tiers = m.supplier["reliability_tier"].map(GEN["delay_prob_by_tier"]).values
    return Supply(prim, prim_end, sec, ss["pack_size_units"].values, ss["moq_units"].values,
                  ss["nominal_lead_time_days"].values, ss["_sup"].values, tiers)


def _draw_lead_time(r, lnom, delay_p, disrupted):
    size = len(lnom)
    jitter = r.choice([-1, 0, 0, 0, 1], size=size)
    late = r.random(size) < np.where(disrupted, GEN["disruption_delay_prob"], delay_p)
    delay = np.where(disrupted, r.integers(5, 13, size), 1 + r.geometric(0.35, size))
    return np.clip(lnom + jitter + np.where(late, delay, 0), 1, 45)


def _ceil_pack(qty, pack):
    return np.ceil(qty / pack).astype(np.int64) * pack


def simulate_store(p: Profile, m: Master, j: int, sup: Supply) -> dict:
    h = m.hidden
    pr = m.pairs[m.pairs["store_idx"] == j].reset_index(drop=True)
    P = len(pr)
    B = h["burn_in"]
    D = B + p.n_days                      # simulated days = burn-in + history
    T = len(h["all_dates"])
    R = p.review_period_days
    r = rng(p.seed, "sim", j)
    si = pr["sku_idx"].values
    t_all = np.arange(T)

    # ---- latent demand mean: base x season x festival x weekday x trend x launch ramp x promo x post-promo dip
    cat = h["cat_idx"][si]
    age = t_all[None, :] - h["launch_idx"][si][:, None]
    ramp = np.where(age >= 0, 1.0 / (1.0 + np.exp(-(age - 10) / 4.0)), 0.0)
    trend = np.exp(h["growth"][si][:, None] * (t_all[None, :] - (D - 1)) / 365.0)
    disc = np.zeros((P, T))
    disc[:, :D] = h["promo"][si] * pr["promo_participates"].values[:, None]
    after = np.zeros((P, T), bool)
    for lag in (1, 2, 3):
        after[:, lag:] |= disc[:, :-lag] > 0
    dip = np.where(after & (disc == 0), 0.85, 1.0)
    mu = (pr["base_rate"].values[:, None] * h["season"][cat] * h["event"][cat] * h["dow"][None, :]
          * trend * ramp * (1 + h["elasticity"][si][:, None] * disc) * dip)
    k = np.broadcast_to(h["nb_k"][si][:, None], mu.shape)
    demand = r.poisson(r.gamma(k, mu / k)).astype(np.int64)        # negative binomial

    # ---- supply contract in force per pair per day
    p_ss, p_end, s_ss = sup.prim[si], sup.prim_end[si], sup.sec[si]
    offset = (sup.sup[p_ss] * 3) % R                                # weekly delivery slot per supplier
    disc_day = h["disc_day"][si]
    start = np.maximum(pr["active_from_idx"].values, 0)
    active = t_all[None, :D] >= start[:, None]
    dis_windows = h["disruptions"]

    def contract(t):
        return np.where(t <= p_end, p_ss, s_ss)

    def disrupted(sup_idx, t):
        out = np.zeros(len(sup_idx), bool)
        for s, a, b in dis_windows:
            out |= (sup_idx == s) & (a <= t) & (t <= b)
        return out

    on_hand = np.zeros(P, np.int64)
    init = pr["active_from_idx"].values <= 0
    on_hand[init] = np.round(mu[init, :28].mean(1) * r.uniform(6, 14, init.sum())).astype(np.int64)
    arrivals = np.zeros((P, D + 60), np.int64)
    on_order = np.zeros(P, np.int64)
    orders = []   # (pair, order_day, qty, ss_row, lead_time, reason)

    # launch allocation: first fill arrives exactly on the first sellable day
    la = np.where(~init)[0]
    if len(la):
        ssr = np.array([contract(start[q])[q] for q in la])
        ok = ssr >= 0
        la, ssr = la[ok], ssr[ok]
        qty = np.maximum(sup.moq[ssr], _ceil_pack(pr["base_rate"].values[la] * 14, sup.pack[ssr]))
        lt = sup.lnom[ssr]
        arrivals[la, start[la]] += qty
        on_order[la] += qty
        orders.append((la, start[la] - lt, qty, ssr, lt, np.full(len(la), "launch_allocation")))

    rec = {name: np.zeros((P, D), np.int64) for name in ("opening", "receipts", "sold", "adj", "closing", "on_order", "avail")}
    delisted_on = np.full(P, -1)
    shrink_p = GEN["shrink_daily_prob"]
    for t in range(D):
        act = active[:, t]
        receipts = arrivals[:, t]
        avail = on_hand + receipts
        sold = np.where(act, np.minimum(demand[:, t], avail), 0)
        shrink = np.where(act & (r.random(P) < shrink_p), np.minimum(r.integers(1, 4, P), avail - sold), 0)
        closing = avail - sold - shrink
        rec["opening"][:, t], rec["receipts"][:, t], rec["sold"][:, t] = on_hand, receipts, sold
        rec["adj"][:, t], rec["closing"][:, t], rec["avail"][:, t] = -shrink, closing, avail
        on_order -= receipts
        on_hand = closing

        # legacy policy: censoring-naive 28-day moving average of OBSERVED sales
        ssr = contract(t)
        review = act & ((t + offset) % R == 0) & (t < disc_day) & (ssr >= 0)
        if review.any():
            q = np.where(review)[0]
            w0 = max(0, t - 27)
            n_days = active[q, w0:t + 1].sum(1)
            avg = rec["sold"][q, w0:t + 1].sum(1) / np.maximum(n_days, 1)
            avg = np.where(n_days >= 7, avg, pr["base_rate"].values[q])
            s = ssr[q]
            target = np.maximum(avg * (sup.lnom[s] + R) * (1 + pr["safety_factor"].values[q]), 2)
            need = target - (on_hand[q] + on_order[q])
            go = need > 0
            q, s, need = q[go], s[go], need[go]
            if len(q):
                qty = _ceil_pack(np.maximum(need, sup.moq[s]), sup.pack[s])
                lt = _draw_lead_time(r, sup.lnom[s], sup.delay_p[sup.sup[s]], disrupted(sup.sup[s], t))
                np.add.at(arrivals, (q, t + lt), qty)
                on_order[q] += qty
                orders.append((q, np.full(len(q), t), qty, s, lt, np.full(len(q), "replenishment")))
        rec["on_order"][:, t] = on_order

        # phase-out: a discontinued SKU is delisted once its stock and pipeline are empty
        gone = act & (t >= disc_day) & (closing == 0) & (on_order == 0) & (t < D - 1)
        if gone.any():
            active[gone, t + 1:] = False
            delisted_on[gone] = t

    # drop the burn-in: history-relative day indices from here on
    rec = {k: v[:, B:] for k, v in rec.items()}
    orders = [(q, d - B, x, s, l, why) for q, d, x, s, l, why in orders]
    delisted_on = np.where(delisted_on >= B, delisted_on - B, -1)
    return _frames(p, m, j, pr, active[:, B:], demand[:, B:], disc[:, B:], rec, orders, delisted_on, sup)


def _frames(p, m, j, pr, active, demand, disc, rec, orders, delisted_on, sup):
    h = m.hidden
    D = p.n_days
    sku_ids = m.sku["sku_id"].values
    store = m.store["store_id"].values[j]
    B = h["burn_in"]
    hist = h["all_dates"][B:B + D]
    iso_dates = to_iso(hist)
    compact = np.char.replace(iso_dates, "-", "")
    dow_idx = (hist.view("int64") + 3) % 7

    pi, ti = np.nonzero(active)
    sku_col = sku_ids[pr["sku_idx"].values[pi]]
    key = pd.Series(store + "-" + pd.Series(sku_col) + "-" + pd.Series(compact[ti]))
    sold = rec["sold"][pi, ti]
    avail = rec["avail"][pi, ti]
    closing = rec["closing"][pi, ti]
    status = np.where(avail == 0, "out_of_stock_all_day", np.where(closing == 0, "sold_out_during_day", "in_stock"))
    disc_v = np.round(disc[pi, ti], 2)
    price = m.sku["unit_price"].values[pr["sku_idx"].values[pi]]

    sales = pd.DataFrame({
        "sale_id": "SALE-" + key, "store_id": store, "sku_id": sku_col, "sale_date": iso_dates[ti],
        "day_of_week": np.array(DAYS)[dow_idx[ti]], "units_sold": sold,
        "unit_price_paid": np.round(price * (1 - disc_v), 2), "promotion_flag": disc_v > 0,
        "promo_discount_pct": disc_v, "availability_status": status, "stockout_flag": status != "in_stock",
    })
    snap = pd.DataFrame({
        "inventory_snapshot_id": "INV-" + key, "store_id": store, "sku_id": sku_col, "snapshot_date": iso_dates[ti],
        "opening_on_hand_units": rec["opening"][pi, ti], "receipts_units": rec["receipts"][pi, ti],
        "units_sold": sold, "adjustment_units": rec["adj"][pi, ti], "closing_on_hand_units": closing,
        "on_order_units": rec["on_order"][pi, ti], "stockout_flag": closing == 0,
    })

    # ground truth: days where latent demand exceeded what was available (true censoring)
    true_dem = demand[pi, ti]
    cens = true_dem > avail
    gt_stockout = pd.DataFrame({"entity_id": sales["sale_id"].values[cens], "observed_value": sold[cens],
                                "expected_value": true_dem[cens]})

    # forecast-month demand for pairs still sellable after history end
    alive = active[:, D - 1] & (delisted_on < 0)
    fut_dates = to_iso(h["all_dates"][B + D:])
    fi, ft = np.nonzero(np.broadcast_to(alive[:, None], (len(pr), len(fut_dates))))
    future = pd.DataFrame({"store_id": store, "sku_id": sku_ids[pr["sku_idx"].values[fi]],
                           "demand_date": fut_dates[ft], "true_demand_units": demand[fi, D + ft]})

    # true demand class per pair from latent history demand on sellable days
    prof = []
    for q in range(len(pr)):
        d = demand[q, :D][active[q]]
        nz = d[d > 0]
        if len(nz) == 0:
            cls = "no_demand"
        else:
            adi = len(d) / len(nz)
            cv2 = (nz.std() / nz.mean()) ** 2 if len(nz) > 1 else 0.0
            cls = ("smooth" if cv2 < 0.49 else "erratic") if adi < 1.32 else ("intermittent" if cv2 < 0.49 else "lumpy")
        prof.append((cls, float(d.mean()) if len(d) else 0.0))
    profile = pd.DataFrame({"store_id": store, "sku_id": sku_ids[pr["sku_idx"].values],
                            "base_daily_rate": np.round(pr["base_rate"].values, 4),
                            "nb_dispersion_k": h["nb_k"][pr["sku_idx"].values],
                            "true_demand_class": [c for c, _ in prof],
                            "mean_true_daily_demand": np.round([v for _, v in prof], 4)})

    if orders:
        oq, od, oqty, oss, olt, oreason = (np.concatenate(x) for x in zip(*orders))
        keep = od + olt >= 0          # warm-up orders matter only if still in transit on day 1
        oq, od, oqty, oss, olt, oreason = oq[keep], od[keep], oqty[keep], oss[keep], olt[keep], oreason[keep]
    else:
        oq = od = oqty = oss = olt = np.array([], int)
        oreason = np.array([], str)
    start = p.history_start
    order_df = pd.DataFrame({
        "store_id": store, "sku_id": sku_ids[pr["sku_idx"].values[oq]],
        "supplier_sku_row": oss, "order_day": od, "order_qty_units": oqty,
        "lead_time": olt, "nominal": sup.lnom[oss], "order_reason": oreason,
    })
    order_df["order_date"] = to_iso(np.datetime64(start) + order_df["order_day"].values.astype("timedelta64[D]"))

    delist = pd.DataFrame({"matrix_row": pr["matrix_row"].values, "delisted_on": delisted_on,
                           "phase_out": h["disc_day"][pr["sku_idx"].values] < B + D})
    return dict(sales=sales, snapshot=snap, gt_stockout=gt_stockout, future=future, profile=profile,
                orders=order_df, delist=delist)
