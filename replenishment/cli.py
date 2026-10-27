"""Command line: run / validate / reproduce."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
from pathlib import Path

from .config import DEFAULT_SEED, PROFILES, get_profile


def _profile(args):
    return get_profile(args.profile, seed=args.seed, n_skus=args.skus, n_stores=args.stores, n_days=args.days)


def _out(args, p):
    return Path(args.out) if args.out else Path("data") / p.name


def cmd_run(args):
    from .pipeline import run
    p = _profile(args)
    command = "python main.py " + " ".join(shlex.quote(a) for a in sys.argv[1:])
    res = run(p, _out(args, p), command)
    print("timings (s): " + ", ".join(f"{k} {v:.1f}" for k, v in res["timings"].items()))
    failed = res["evaluation"]["validation"]["failed"]
    if failed:
        print("FAILED checks:\n  " + "\n  ".join(failed))
        sys.exit(1)


def cmd_validate(args):
    from .validate import validate
    p = _profile(args)
    val = validate(p, _out(args, p))
    print(val[["status", "check", "violations"]].to_string(index=False))
    sys.exit(0 if val["status"].str.startswith("PASS").all() else 1)


def cmd_reproduce(args):
    """Step 10: generate twice with the same seed into fresh folders and compare every file checksum."""
    from .pipeline import run
    p = _profile(args)
    with tempfile.TemporaryDirectory() as tmp:
        runs = [run(p, Path(tmp) / f"run{i}", "reproduce", log=lambda *_: None)["manifest"]["files"] for i in (1, 2)]
    a, b = runs
    diff = sorted(k for k in set(a) | set(b) if a.get(k, {}).get("sha256") != b.get(k, {}).get("sha256"))
    rows = sum(v.get("rows", 0) for v in a.values())
    report = {"profile": p.name, "seed": p.seed, "files_compared": len(set(a) | set(b)), "csv_rows_per_run": rows,
              "identical": not diff, "differences": diff,
              "files": {k: {"rows": v.get("rows"), "sha256": v["sha256"]} for k, v in sorted(a.items())}}
    lines = [f"# Reproducibility check - profile `{p.name}`, seed {p.seed}", "",
             f"Generated twice from scratch into separate folders; compared {report['files_compared']} files "
             f"({rows:,} CSV rows per run).", "", f"**Result: {'IDENTICAL' if not diff else 'DIFFERENT'}**", "",
             "| File | Rows | SHA-256 (both runs) |", "|---|---:|---|"]
    for k, v in sorted(a.items()):
        n = "" if v.get("rows") is None else f"{v['rows']:,}"
        lines.append(f"| {k} | {n} | `{v['sha256'][:16]}...` |")
    out = _out(args, p) / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "reproducibility.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "reproducibility.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{report['files_compared']} files compared, identical = {report['identical']}")
    if diff:
        print("differences:", diff)
        sys.exit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python main.py", description="SKU-location demand forecasting & replenishment")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in [("run", cmd_run, "generate data, run the engine, audit, validate, evaluate, export"),
                            ("validate", cmd_validate, "re-run validation on an existing output folder"),
                            ("reproduce", cmd_reproduce, "generate twice and compare checksums")]:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--profile", choices=list(PROFILES), default="sample")
        sp.add_argument("--out", help="output folder (default data/<profile>)")
        sp.add_argument("--seed", type=int, help=f"random seed (default {DEFAULT_SEED})")
        sp.add_argument("--skus", type=int, help="override number of SKUs")
        sp.add_argument("--stores", type=int, help="override number of stores")
        sp.add_argument("--days", type=int, help="override days of history")
        sp.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    args.fn(args)
