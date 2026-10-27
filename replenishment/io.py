"""Schema-aware CSV reading so every consumer (engine, audit, validation) parses types identically."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .schema import TABLES

LAYER_DIR = {"input": "input", "output": "output", "ground_truth": "ground_truth"}


def table_path(root: Path, name: str) -> Path:
    return Path(root) / LAYER_DIR[TABLES[name].layer] / f"{name}.csv"


def _dtypes(name: str, usecols=None) -> dict:
    out = {}
    for c in TABLES[name].cols:
        if usecols is not None and c.name not in usecols:
            continue
        if c.type in ("string", "date", "enum"):
            out[c.name] = object
        elif c.type == "integer":
            out[c.name] = "int64" if c.required else "Int64"
        elif c.type == "decimal":
            out[c.name] = "float64"
        elif c.type == "boolean":
            out[c.name] = bool
    return out


def read_table(root: Path, name: str, usecols=None, chunksize=None):
    return pd.read_csv(table_path(root, name), usecols=usecols, dtype=_dtypes(name, usecols),
                       chunksize=chunksize, keep_default_na=False, na_values=[""])


def write_table(root: Path, name: str, df: pd.DataFrame) -> None:
    path = table_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [c.name for c in TABLES[name].cols]
    df[cols].to_csv(path, index=False)
