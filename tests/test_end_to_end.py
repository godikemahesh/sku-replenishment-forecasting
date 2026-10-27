"""Full pipeline on a tiny profile: everything validates, every injected defect is caught, output is reproducible."""
import json
import sqlite3

import pytest

from replenishment.config import get_profile
from replenishment.pipeline import run

TINY = dict(n_skus=40, n_stores=4, n_days=45)


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    p = get_profile("sample", **TINY)
    out = tmp_path_factory.mktemp("tiny")
    return p, out, run(p, out, "pytest", log=lambda *_: None)


def test_all_validation_checks_pass(tiny):
    _, _, res = tiny
    val = res["validation"]
    assert val["status"].str.startswith("PASS").all(), val[~val["status"].str.startswith("PASS")]


def test_audit_catches_every_injected_defect(tiny):
    _, _, res = tiny
    for issue, d in res["evaluation"]["audit_detection"]["by_issue"].items():
        assert d["labelled"] > 0, issue
        assert d["precision"] == 1.0 and d["recall"] == 1.0, issue
    assert res["evaluation"]["audit_detection"]["unexpected_defect_exceptions"] == 0


def test_recommendations_respect_constraints(tiny):
    c = tiny[2]["evaluation"]["replenishment"]["compliance"]
    assert c["pack_size"] == 1.0 and c["moq"] == 1.0 and c["capacity_ordering_stores_within_limit"] == 1.0
    assert c["discontinued_blocked"]


def test_stockout_censoring_fully_identified(tiny):
    assert tiny[2]["evaluation"]["censoring"]["recall"] == 1.0


def test_sql_seed_loads_with_foreign_keys(tiny):
    _, out, _ = tiny
    con = sqlite3.connect(":memory:")
    con.executescript((out / "sql" / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((out / "sql" / "seed.sql").read_text(encoding="utf-8"))
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("SELECT COUNT(*) FROM sales_history").fetchone()[0] > 0


def test_same_seed_same_bytes(tiny, tmp_path):
    p, _, first = tiny
    second = run(p, tmp_path / "again", "pytest", log=lambda *_: None)
    a, b = first["manifest"]["files"], second["manifest"]["files"]
    assert {k: v["sha256"] for k, v in a.items()} == {k: v["sha256"] for k, v in b.items()}


def test_different_seed_different_data(tiny, tmp_path):
    p = get_profile("sample", seed=7, **TINY)
    other = run(p, tmp_path / "seed7", "pytest", log=lambda *_: None)
    assert other["manifest"]["files"]["input/sales_history.csv"]["sha256"] != \
        tiny[2]["manifest"]["files"]["input/sales_history.csv"]["sha256"]
