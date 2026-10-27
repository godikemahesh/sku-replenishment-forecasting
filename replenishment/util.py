"""Deterministic IDs, seeded random streams and small date helpers."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

# Stream identifiers - each component gets an independent, order-insensitive RNG.
STREAM = {"sku": 1, "store": 2, "supplier": 3, "supplier_sku": 4, "matrix": 5, "promo": 6,
          "pair": 7, "sim": 10, "anomaly": 20}


def rng(seed: int, stream: str, *keys: int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence([seed, STREAM[stream], *keys]))


def sku_id(n) -> str:
    return f"SKU-{int(n):06d}"


def store_id(n) -> str:
    return f"STORE-{int(n):06d}"


def supplier_id(n) -> str:
    return f"SUP-{int(n):04d}"


def supplier_sku_id(n) -> str:
    return f"SS-{int(n):06d}"


def compact(dates: pd.Series) -> pd.Series:
    """'2025-12-31' -> '20251231'."""
    return dates.str.replace("-", "", regex=False)


def iso(d) -> str:
    return pd.Timestamp(d).strftime("%Y-%m-%d")


def date_range(start: date, end: date) -> np.ndarray:
    return np.arange(np.datetime64(start, "D"), np.datetime64(end, "D") + 1)


def to_iso(arr: np.ndarray) -> np.ndarray:
    return np.datetime_as_string(arr.astype("datetime64[D]"), unit="D")


def sequence_ids(prefix_year: pd.Series, width: int = 6) -> pd.Series:
    """Per-year zero-padded counter: ['2025','2025','2026'] -> PO-2025-000001, PO-2025-000002, PO-2026-000001."""
    counter = prefix_year.groupby(prefix_year).cumcount() + 1
    return "PO-" + prefix_year + "-" + counter.astype(str).str.zfill(width)
