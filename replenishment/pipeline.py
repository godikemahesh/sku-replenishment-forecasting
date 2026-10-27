"""End-to-end run: generate -> engine -> inject labelled defects -> audit -> validate -> evaluate -> export."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Profile
from .engine import run_engine
from .engine.audit import run_audit
from .engine.forecast import naive_forecast
from .engine.replenish import build_policies, build_recommendations
from .evaluate import evaluate, to_markdown
from .export import write_docs, write_exports, write_manifest
from .generator import generate
from .generator.anomalies import inject_output_anomalies
from .io import read_table, write_table
from .validate import validate

OWNED = ["input", "output", "ground_truth", "json", "sql", "docs", "reports", "manifest.json"]


def _clean(out: Path) -> None:
    for name in OWNED:
        target = out / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def _naive_variant(p: Profile, eng: dict) -> pd.DataFrame:
    """Same policy fed with the censoring-naive forecast - evaluation benchmark only."""
    panel = eng["panel"].truncate(p.history_end)
    mean = pd.Series(naive_forecast(panel, 1), index=pd.MultiIndex.from_frame(panel.pairs[["store_id", "sku_id"]]))
    prod = eng["forecast"][eng["forecast"]["forecast_type"] == "production"].copy()
    m = pd.MultiIndex.from_frame(prod[["store_id", "sku_id"]]).map(mean).values
    prod["predicted_daily_units"] = np.round(m, 4)
    prod["predicted_daily_sd_units"] = np.round(np.sqrt(np.maximum(m, 0.05)), 4)
    t = eng["inputs"]
    pol = build_policies(p, prod, t["sku"], t["supplier_sku"], t["lead_time_observation"])
    return build_recommendations(p, pol, prod, t["store"], t["supplier_sku"], eng["position"])


def run(p: Profile, out: Path, command: str = "", log=print) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    _clean(out)
    timings = {}

    t0 = time.time()
    log(f"[1/7] generating {p.name}: {p.n_skus} SKUs x {p.n_stores} stores x {p.n_days} days (seed {p.seed})")
    gen = generate(p, out, log)
    timings["generate"] = time.time() - t0

    t0 = time.time()
    log("[2/7] engine: forecast (backtest + production) -> reorder policy -> recommendations")
    eng = run_engine(p, out)
    timings["engine"] = time.time() - t0

    log("[3/7] injecting labelled defects into published forecast / recommendation tables")
    t = eng["inputs"]
    fc_pub, rec_pub, gt_out = inject_output_anomalies(p, eng["panel"], eng["forecast"], eng["purchase_recommendation"],
                                                      t["store"], t["supplier_sku"], eng["reorder_policy"])
    write_table(out, "forecast", fc_pub)
    write_table(out, "reorder_policy", eng["reorder_policy"])
    write_table(out, "purchase_recommendation", rec_pub)
    gt = pd.concat(gen["gt_parts"] + [gt_out], ignore_index=True)
    gt = gt.sort_values(["origin", "issue_type", "entity_id"], kind="mergesort").reset_index(drop=True)
    gt.insert(0, "ground_truth_id", [f"GT-{i + 1:08d}" for i in range(len(gt))])
    write_table(out, "ground_truth", gt)

    t0 = time.time()
    log("[4/7] audit gate (rules only, no ground truth)")
    so = pd.concat([c.loc[c["stockout_flag"], ["store_id", "sku_id", "sale_date"]]
                    for c in read_table(out, "sales_history", usecols=["store_id", "sku_id", "sale_date", "stockout_flag"],
                                        chunksize=2_000_000)], ignore_index=True)
    published = {"sku": t["sku"], "store": t["store"], "lead_time_observation": t["lead_time_observation"],
                 "forecast": read_table(out, "forecast"), "purchase_recommendation": read_table(out, "purchase_recommendation"),
                 "purchase_order": read_table(out, "purchase_order"), "_stockout_days": so}
    audit = run_audit(published, p.history_end)
    write_table(out, "audit_exception", audit)
    timings["audit"] = time.time() - t0

    t0 = time.time()
    log("[5/7] validating published files")
    reports = out / "reports"
    reports.mkdir(exist_ok=True)
    val = validate(p, out)
    val.to_csv(reports / "validation_summary.csv", index=False)
    timings["validate"] = time.time() - t0

    t0 = time.time()
    log("[6/7] evaluating against ground truth")
    ev = evaluate(p, out, eng["purchase_recommendation"], _naive_variant(p, eng), eng["reorder_policy"])
    ev["validation"] = {"checks": int(len(val)), "passed": int(val["status"].str.startswith("PASS").sum()),
                        "failed": val.loc[~val["status"].str.startswith("PASS"), "check"].tolist()}
    (reports / "evaluation.json").write_text(json.dumps(ev, indent=2, default=str), encoding="utf-8")
    (reports / "evaluation.md").write_text(to_markdown(ev) + _validation_md(val), encoding="utf-8")
    timings["evaluate"] = time.time() - t0

    log("[7/7] exporting dictionary, schema, JSON/SQL seed and manifest")
    write_docs(out)
    exports = write_exports(out)
    manifest = write_manifest(p, out, command, exports)
    log(f"done: {len(manifest['files'])} files, validation {ev['validation']['passed']}/{ev['validation']['checks']} passed")
    return {"evaluation": ev, "validation": val, "manifest": manifest, "timings": timings}


def _validation_md(val: pd.DataFrame) -> str:
    lines = ["## 6. Validation summary", "",
             f"{int(val['status'].str.startswith('PASS').sum())} of {len(val)} checks passed "
             f"({int((val['status'] == 'PASS_LABELLED').sum())} found only the labelled anomalies).", "",
             "| Check | Category | Status | Rows | Violations |", "|---|---|---|---:|---:|"]
    lines += [f"| {r.check} | {r.category} | {r.status} | {r.rows_checked:,} | {r.violations:,} |" for r in val.itertuples()]
    return "\n".join(lines) + "\n"
