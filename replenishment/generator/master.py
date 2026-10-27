"""Step 2a - master / parent tables and the hidden parameters that drive demand.

Visible master tables go to ``input/``. Parameters a real planner would never see
(true demand rate, dispersion, promotion elasticity, supplier disruption windows)
stay in ``Master.hidden`` and are only exported to ``ground_truth/``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..config import GEN, Profile
from ..schema import CATEGORIES
from ..util import date_range, iso, rng, sku_id, store_id, supplier_id, supplier_sku_id

# Monthly seasonality (Jan..Dec) for an Indian grocery context: summer peak for drinks and
# frozen, festive (Dussehra/Diwali) peak for staples, snacks and household.
CATEGORY_SPEC = {
    "Staples":       dict(w=0.18, cost=60, mult=1.2, products=["Basmati Rice", "Toor Dal", "Wheat Atta", "Sugar", "Iodised Salt", "Moong Dal", "Poha", "Sooji"], sizes=["500g", "1kg", "5kg"],
                          season=[1.02, .97, .98, 1.0, .97, .96, .97, .99, 1.03, 1.15, 1.10, 1.0]),
    "Dairy":         dict(w=0.12, cost=35, mult=1.8, products=["Toned Milk", "Curd", "Paneer", "Butter", "Ghee", "Cheese Slices"], sizes=["200g", "500ml", "1L"],
                          season=[1.0, .98, .97, .95, .93, .95, 1.0, 1.02, 1.03, 1.08, 1.05, 1.02]),
    "Beverages":     dict(w=0.13, cost=45, mult=1.3, products=["Mango Drink", "Cola", "Packaged Water", "Green Tea", "Filter Coffee", "Lemon Soda"], sizes=["250ml", "1L", "2L"],
                          season=[.80, .88, 1.08, 1.30, 1.38, 1.20, .95, .90, .93, 1.0, .92, .95]),
    "Snacks":        dict(w=0.15, cost=25, mult=1.0, products=["Potato Chips", "Namkeen Mix", "Cream Biscuits", "Roasted Peanuts", "Chocolate Bar", "Murukku"], sizes=["50g", "150g", "400g"],
                          season=[.98, .95, .97, .98, .97, .95, 1.0, 1.0, 1.02, 1.18, 1.10, 1.08]),
    "Personal Care": dict(w=0.12, cost=80, mult=0.8, products=["Bath Soap", "Shampoo", "Toothpaste", "Hair Oil", "Face Wash", "Body Lotion"], sizes=["75g", "180ml", "400ml"],
                          season=[1.0, .98, 1.0, 1.02, 1.03, 1.0, .98, .98, 1.0, 1.08, 1.03, 1.0]),
    "Household":     dict(w=0.12, cost=90, mult=0.8, products=["Dishwash Bar", "Detergent Powder", "Floor Cleaner", "Toilet Cleaner", "Garbage Bags", "Agarbatti"], sizes=["500g", "1kg", "2L"],
                          season=[.97, .95, .98, 1.0, .98, .97, 1.0, 1.0, 1.02, 1.25, 1.05, .98]),
    "Packaged Food": dict(w=0.12, cost=50, mult=1.0, products=["Instant Noodles", "Tomato Ketchup", "Pickle", "Ready Upma Mix", "Corn Flakes", "Pasta"], sizes=["70g", "300g", "1kg"],
                          season=[1.03, 1.0, .97, .95, .93, .98, 1.08, 1.10, 1.05, 1.02, .98, 1.02]),
    "Frozen":        dict(w=0.06, cost=70, mult=0.6, products=["Ice Cream Tub", "Frozen Peas", "Kulfi", "Veg Nuggets", "Frozen Paratha"], sizes=["400g", "500ml", "1kg"],
                          season=[.75, .85, 1.10, 1.40, 1.50, 1.25, .95, .88, .90, .95, .85, .80]),
}
assert list(CATEGORY_SPEC) == CATEGORIES

# (start, end, multiplier, categories) - festival demand spikes.
EVENTS = [
    ("2025-01-12", "2025-01-15", 1.30, ["Staples", "Snacks"]),                  # Sankranti
    ("2025-03-13", "2025-03-14", 1.25, ["Beverages", "Snacks"]),                # Holi
    ("2025-03-28", "2025-03-31", 1.20, ["Staples", "Packaged Food"]),           # Ugadi / Eid
    ("2025-09-30", "2025-10-02", 1.20, ["Staples", "Snacks"]),                  # Dussehra
    ("2025-10-14", "2025-10-21", 1.45, ["Staples", "Snacks", "Household", "Personal Care", "Packaged Food"]),  # Diwali
    ("2025-12-28", "2025-12-31", 1.35, ["Beverages", "Snacks", "Frozen"]),      # New Year
    ("2026-01-12", "2026-01-15", 1.30, ["Staples", "Snacks"]),                  # Sankranti
]
DOW = np.array([0.92, 0.88, 0.90, 0.95, 1.05, 1.20, 1.10])
DOW = DOW / DOW.mean()

BRANDS = ["Annapurna", "Sunrise", "GreenLeaf", "DailyChoice", "Kaveri", "Swasthya", "UrbanNest", "Nirmal",
          "Godavari", "HappyHarvest", "PureFarm", "Tulsi", "Coastline", "Vasant", "BlueRiver", "Shakti"]
CITIES = [("Hyderabad", "Telangana"), ("Secunderabad", "Telangana"), ("Warangal", "Telangana"),
          ("Bengaluru", "Karnataka"), ("Mysuru", "Karnataka"), ("Chennai", "Tamil Nadu"),
          ("Coimbatore", "Tamil Nadu"), ("Vijayawada", "Andhra Pradesh"), ("Visakhapatnam", "Andhra Pradesh"),
          ("Pune", "Maharashtra"), ("Nagpur", "Maharashtra"), ("Kochi", "Kerala")]
LOCALITIES = ["Central", "Lake View", "Market Road", "Station Road", "Hill Side", "Ring Road", "Old Town",
              "Tech Park", "Garden City", "River Front", "Airport Road", "Main Bazaar"]
SUP_A = ["Deccan", "Coromandel", "Godavari", "Western Ghats", "Sahyadri", "Krishna", "Malabar", "Vindhya", "Satpura", "Nilgiri"]
SUP_B = ["Wholesale", "Distributors", "Agro Foods", "Consumer Goods", "Supply Co", "Traders", "Logistics", "FMCG"]
FORMAT_SPEC = {"hypermarket": dict(p=0.15, size=2.0, density=0.55),
               "supermarket": dict(p=0.55, size=1.0, density=0.32),
               "express": dict(p=0.30, size=0.5, density=0.15)}
SUPPLIER_LT = {"local": (2, 3), "regional": (4, 6), "national": (7, 12)}


@dataclass
class Master:
    sku: pd.DataFrame
    store: pd.DataFrame
    supplier: pd.DataFrame
    supplier_sku: pd.DataFrame
    matrix: pd.DataFrame            # assortment; status finalised after simulation
    pairs: pd.DataFrame             # hidden per ranged pair parameters
    hidden: dict = field(default_factory=dict)


def _season_curve(monthly, dates: np.ndarray) -> np.ndarray:
    doy = (dates - dates.astype("datetime64[Y]")).astype(int)
    mids = np.array([15, 45, 74, 105, 135, 166, 196, 227, 258, 288, 319, 349])
    x = np.concatenate([mids - 365, mids, mids + 365])
    return np.interp(doy, x, np.tile(monthly, 3))


def _pick(r, n_total, rate, minimum, candidates):
    k = min(len(candidates), max(minimum, int(round(rate * n_total))))
    return np.sort(r.choice(candidates, size=k, replace=False)) if k else np.array([], int)


def generate_master(p: Profile) -> Master:
    start, end = p.history_start, p.history_end
    D = p.n_days
    B = GEN["burn_in_days"]
    sim_start = start - timedelta(days=B)    # all hidden day indices are relative to this

    # ---------------------------------------------------------------- SKUs
    r = rng(p.seed, "sku")
    n = p.n_skus
    cat_w = np.array([CATEGORY_SPEC[c]["w"] for c in CATEGORIES])
    cat_idx = r.choice(len(CATEGORIES), size=n, p=cat_w / cat_w.sum())
    names, brands, costs = [], [], []
    for ci in cat_idx:
        spec = CATEGORY_SPEC[CATEGORIES[ci]]
        b = r.choice(BRANDS)
        brands.append(b)
        names.append(f"{b} {r.choice(spec['products'])} {r.choice(spec['sizes'])}")
        costs.append(round(float(r.lognormal(np.log(spec["cost"]), 0.45)), 2))
    costs = np.array(costs)
    prices = np.round(costs * r.uniform(1.15, 1.6, n), 2)
    cat_mult = np.array([CATEGORY_SPEC[CATEGORIES[c]]["mult"] for c in cat_idx])
    base = np.clip(r.lognormal(0.0, 1.0, n) * cat_mult, 0.03, 40.0)          # units/day in a 1.0-size store
    nb_k = r.choice([20.0, 5.0, 1.5, 0.6], size=n, p=[0.35, 0.35, 0.2, 0.1])  # dispersion -> smooth .. lumpy
    elasticity = r.uniform(1.5, 4.5, n)
    growth = r.normal(0.0, 0.08, n)                                           # annual trend

    launch = np.array([start - timedelta(days=int(x)) for x in r.integers(200, 2500, n)])
    lo, hi = int(0.25 * D), D - 10
    new_idx = _pick(r, n, GEN["new_launch_rate"], 1, np.arange(n))
    for i in new_idx:
        launch[i] = start + timedelta(days=int(r.integers(lo, hi + 1)))
    disc_idx = _pick(r, n, GEN["discontinue_rate"], 1, np.setdiff1d(np.arange(n), new_idx))
    discontinue = np.array([None] * n, dtype=object)
    win = min(60, D // 2)
    for i in disc_idx:
        discontinue[i] = end - timedelta(days=int(r.integers(3, win + 1)))
    lifecycle = np.where([d is not None and d <= end for d in discontinue], "discontinued",
                         np.where([l > end - timedelta(days=90) for l in launch], "new_launch", "active"))
    sku = pd.DataFrame({
        "sku_id": [sku_id(i + 1) for i in range(n)], "sku_name": names,
        "category": [CATEGORIES[c] for c in cat_idx], "brand": brands,
        "unit_cost": costs, "unit_price": prices,
        "launch_date": [iso(d) for d in launch],
        "discontinue_date": [iso(d) if d is not None else None for d in discontinue],
        "lifecycle_state": lifecycle,
    })

    # ---------------------------------------------------------------- stores
    r = rng(p.seed, "store")
    m = p.n_stores
    fmts = list(FORMAT_SPEC)
    fmt = r.choice(fmts, size=m, p=[FORMAT_SPEC[f]["p"] for f in fmts])
    size = np.array([FORMAT_SPEC[f]["size"] for f in fmt]) * r.uniform(0.85, 1.15, m)
    city = r.integers(0, len(CITIES), m)
    opened = np.array([start - timedelta(days=int(x)) for x in r.integers(100, 3000, m)])
    if D >= 180:
        for j in _pick(r, m, GEN["new_store_rate"], 1, np.arange(m)):
            opened[j] = start + timedelta(days=int(r.integers(60, D - 60)))
    store = pd.DataFrame({
        "store_id": [store_id(j + 1) for j in range(m)],
        "store_name": [f"{CITIES[c][0]} {r.choice(LOCALITIES)}" for c in city],
        "city": [CITIES[c][0] for c in city], "region": [CITIES[c][1] for c in city],
        "store_format": fmt, "open_date": [iso(d) for d in opened],
        "storage_capacity_units": 0,   # set below from the assortment
        "lifecycle_state": np.where([o > end - timedelta(days=90) for o in opened], "new_store", "active"),
    })

    # ---------------------------------------------------------------- suppliers
    r = rng(p.seed, "supplier")
    k = p.n_suppliers
    stype = r.choice(list(SUPPLIER_LT), size=k, p=[0.3, 0.4, 0.3])
    tier = r.choice(["A", "B", "C"], size=k, p=[0.4, 0.4, 0.2])
    base_lt = np.array([r.integers(SUPPLIER_LT[t][0], SUPPLIER_LT[t][1] + 1) for t in stype])
    supplier = pd.DataFrame({
        "supplier_id": [supplier_id(i + 1) for i in range(k)],
        "supplier_name": [f"{r.choice(SUP_A)} {r.choice(SUP_B)}" for _ in range(k)],
        "region": [CITIES[c][1] for c in r.integers(0, len(CITIES), k)],
        "supplier_type": stype, "reliability_tier": tier, "lifecycle_state": "active",
    })
    n_dis = max(1, int(round(0.05 * k)))
    dis_len = int(min(21, max(7, D // 3)))
    disruptions = []
    for s in np.sort(r.choice(k, size=n_dis, replace=False)):
        s0 = int(r.integers(0, max(1, D - dis_len - 3)))
        disruptions.append((int(s), s0 + B, s0 + B + dis_len - 1))

    # ---------------------------------------------------------------- supplier <-> SKU contracts
    r = rng(p.seed, "supplier_sku")
    rows = []
    has_secondary = r.random(n) < GEN["secondary_supplier_rate"]
    for i in range(n):
        prim = int(r.integers(k))
        sups = [prim]
        if has_secondary[i] and k > 1:
            sups.append(int(r.choice([s for s in range(k) if s != prim])))
        if base[i] < 0.5:
            pack = int(r.choice([1, 2, 4, 6]))
        elif base[i] < 3:
            pack = int(r.choice([4, 6, 8, 12]))
        else:
            pack = int(r.choice([12, 24, 48]))
        for pr, s in enumerate(sups, start=1):
            moq = pack * int(r.choice([1, 1, 1, 2, 3, 4]))
            rows.append({
                "supplier_id": supplier_id(s + 1), "sku_id": sku_id(i + 1), "priority": pr,
                "pack_size_units": pack, "moq_units": moq,
                "unit_cost": round(float(costs[i] * (r.uniform(0.96, 1.0) if pr == 1 else r.uniform(1.0, 1.06))), 2),
                "nominal_lead_time_days": int(max(1, base_lt[s] + r.integers(-1, 2))),
                "effective_from": iso(start - timedelta(days=int(r.integers(30, 400)))),
                "effective_to": None, "is_active": True, "_sku": i, "_sup": s,
            })
    ss = pd.DataFrame(rows)
    ss.insert(0, "supplier_sku_id", [supplier_sku_id(i + 1) for i in range(len(ss))])
    # Primary contracts that end shortly before the decision date (exception path).
    eligible = np.setdiff1d(np.arange(n), np.concatenate([new_idx, disc_idx]))
    with_sec = np.intersect1d(eligible, np.where(has_secondary)[0])
    without = np.setdiff1d(eligible, with_sec)
    n_end = max(2, int(round(GEN["supplier_link_end_rate"] * n)))
    ended = np.concatenate([_pick(r, n_end // 2, 1.0, 1, with_sec), _pick(r, n_end - n_end // 2, 1.0, 1, without)])
    for i in ended:
        row = (ss["_sku"] == i) & (ss["priority"] == 1)
        ss.loc[row, "effective_to"] = iso(end - timedelta(days=int(r.integers(1, max(2, min(20, D // 3))))))
        ss.loc[row, "is_active"] = False

    # ---------------------------------------------------------------- assortment matrix
    r = rng(p.seed, "matrix")
    pop = (np.argsort(np.argsort(base)) + 0.5) / n                            # popularity percentile
    density = np.array([FORMAT_SPEC[f]["density"] for f in fmt])
    ranged = r.random((n, m)) < np.clip(density[None, :] * (0.5 + pop[:, None]), 0.02, 0.97)
    for i in range(n):
        need = 2 if (i in new_idx or i in disc_idx) else 1
        if ranged[i].sum() < min(need, m):
            ranged[i, r.choice(m, size=min(need, m), replace=False)] = True
    si, sj = np.nonzero(ranged)
    pre = np.array([start - timedelta(days=int(x)) for x in r.integers(30, 500, len(si))])
    ranged_from = [max(pre[x], launch[si[x]], opened[sj[x]]) for x in range(len(si))]
    mat = pd.DataFrame({"sku_id": np.repeat(sku["sku_id"].values, m), "store_id": np.tile(store["store_id"].values, n),
                        "assortment_status": "not_ranged", "ranged_from": None, "ranged_to": None})
    flat = si * m + sj
    mat.loc[flat, "assortment_status"] = "ranged"
    mat.loc[flat, "ranged_from"] = [iso(d) for d in ranged_from]

    # hidden pair-level parameters
    r = rng(p.seed, "pair")
    pairs = pd.DataFrame({"sku_idx": si, "store_idx": sj, "matrix_row": flat})
    pairs["base_rate"] = base[si] * size[sj] * r.lognormal(0, 0.3, len(si))
    pairs["safety_factor"] = r.uniform(0.15, 0.6, len(si))                   # legacy policy over-cover
    pairs["promo_participates"] = r.random(len(si)) < GEN["promo_participation"]
    pairs["active_from_idx"] = [(d - sim_start).days for d in ranged_from]

    # capacity: what the assortment needs at ~L+R+1 week of cover, with store-specific slack
    prim = ss[ss["priority"] == 1].set_index("_sku")
    lt_by_sku = prim["nominal_lead_time_days"].reindex(range(n)).values
    need = pairs["base_rate"] * (lt_by_sku[si] + p.review_period_days + 14)
    cap = need.groupby(pairs["store_idx"]).sum().reindex(range(m), fill_value=0).values
    store["storage_capacity_units"] = (np.maximum(100, np.round(cap * r.uniform(0.85, 1.4, m) / 50) * 50)).astype(int)

    # hidden time-series drivers over history + forecast month
    all_dates = date_range(sim_start, p.forecast_end)
    season = np.stack([_season_curve(CATEGORY_SPEC[c]["season"], all_dates) for c in CATEGORIES])
    event = np.ones_like(season)
    for s0, e0, mult, cats in EVENTS:
        sel = (all_dates >= np.datetime64(s0)) & (all_dates <= np.datetime64(e0))
        for c in cats:
            event[CATEGORIES.index(c), sel] *= mult
    dow = DOW[(all_dates.astype("datetime64[D]").view("int64") - 4) % 7]     # 1970-01-01 was a Thursday

    # chain promotion calendar per SKU (history only - nothing planned in the forecast month)
    r = rng(p.seed, "promo")
    promo = np.zeros((n, B + D), dtype=np.float32)
    for i in range(n):
        if r.random() < GEN["promo_never_rate"] or launch[i] > end:
            continue
        t = int(r.exponential(GEN["promo_mean_gap_days"] / 2))
        while t < B + D:
            dur = int(r.integers(3, 8))
            promo[i, t:t + dur] = r.choice([0.10, 0.15, 0.20, 0.25, 0.30])
            t += dur + int(r.exponential(GEN["promo_mean_gap_days"])) + 7
    launch_idx = np.array([(d - sim_start).days for d in launch])
    disc_day = np.array([(d - sim_start).days if d is not None else 10 ** 6 for d in discontinue])

    hidden = dict(cat_idx=cat_idx, base=base, nb_k=nb_k, elasticity=elasticity, growth=growth,
                  launch_idx=launch_idx, disc_day=disc_day, season=season, event=event, dow=dow,
                  all_dates=all_dates, promo=promo, supplier_tier=tier, disruptions=disruptions,
                  burn_in=B, sim_start=sim_start)
    return Master(sku, store, supplier, ss, mat, pairs, hidden)
