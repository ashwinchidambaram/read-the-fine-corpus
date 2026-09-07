"""CLI runner for the RTFC performance benchmark harness.

    uv run python -m benchmarks retrieval  [--mode fake|live] [--queries N] [--kb KB]
    uv run python -m benchmarks ingestion  [--mode fake|live]
    uv run python -m benchmarks coldstart  [--mode fake|live] [--docs N]
    uv run python -m benchmarks all        [--mode fake|live] [--docs N] [--write-results]

FAKE mode is always runnable (deterministic, no infra).  LIVE mode requires the
docker-compose integration overlay; when Qdrant is unreachable the LIVE
sub-benchmark reports NEEDS-OVERLAY instead of failing.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

from benchmarks import bench_coldstart, bench_ingestion, bench_retrieval
from benchmarks.corpus import env_description
from benchmarks.verdict import (
    COLD_START_TARGET_SECONDS,
    RETRIEVAL_P50_TARGET_MS,
    RETRIEVAL_P99_TARGET_MS,
)

_RESULTS_MD = Path(__file__).resolve().parent / "RESULTS.md"


# ---------------------------------------------------------------------------
# Individual benchmark drivers → (payload dict, verdict rows)
# ---------------------------------------------------------------------------

_FAKE_LATENCY_NOTE = (
    "service-path only; not reference latency (NEEDS-OVERLAY / NEEDS-REFERENCE-HW for gate)"
)
_FAKE_COLDSTART_NOTE = "PROXY — pipeline-stage wall-clock only; excludes bring-up + real infra"


def _retrieval(mode: str, *, queries: int, kb: str | None) -> dict[str, Any]:
    if mode == "fake":
        res = bench_retrieval.run_fake(queries=queries)
        s = res.summary
        p50_verdict = "MET-IN-HARNESS" if s.p50_ms < RETRIEVAL_P50_TARGET_MS else "UNMET"
        p99_verdict = "MET-IN-HARNESS" if s.p99_ms < RETRIEVAL_P99_TARGET_MS else "UNMET"
        return {
            "benchmark": "retrieval",
            "mode": "fake",
            "data": res.as_dict(),
            "verdicts": [
                {
                    "target": "Retrieval p50 latency (cache warm) < 150 ms",
                    "measured": f"{s.p50_ms:.3f} ms",
                    "verdict": p50_verdict,
                    "note": _FAKE_LATENCY_NOTE,
                },
                {
                    "target": "Retrieval p99 latency < 800 ms",
                    "measured": f"{s.p99_ms:.3f} ms",
                    "verdict": p99_verdict,
                    "note": _FAKE_LATENCY_NOTE,
                },
            ],
        }
    # live
    if kb is None:
        return {
            "benchmark": "retrieval",
            "mode": "live",
            "data": None,
            "verdicts": [
                {
                    "target": "Retrieval p50/p99 latency (LIVE)",
                    "measured": "n/a",
                    "verdict": "ENV-LIMITED",
                    "note": "LIVE retrieval requires --kb <promoted_kb_id>",
                }
            ],
        }
    res_l = bench_retrieval.run_live(kb_id=kb, queries=queries)
    if res_l is None:
        return {
            "benchmark": "retrieval",
            "mode": "live",
            "data": None,
            "verdicts": [
                {
                    "target": "Retrieval p50/p99 latency (LIVE)",
                    "measured": "n/a",
                    "verdict": "NEEDS-OVERLAY",
                    "note": "Qdrant unreachable — bring up the integration overlay",
                }
            ],
        }
    s = res_l.summary
    return {
        "benchmark": "retrieval",
        "mode": "live",
        "data": res_l.as_dict(),
        "verdicts": [
            {
                "target": "Retrieval p50 latency (cache warm) < 150 ms",
                "measured": f"{s.p50_ms:.3f} ms",
                "verdict": "MET" if s.p50_ms < RETRIEVAL_P50_TARGET_MS else "UNMET",
                "note": "live dev infra; still NOT reference hardware for the §19 gate",
            },
            {
                "target": "Retrieval p99 latency < 800 ms",
                "measured": f"{s.p99_ms:.3f} ms",
                "verdict": "MET" if s.p99_ms < RETRIEVAL_P99_TARGET_MS else "UNMET",
                "note": "live dev infra; still NOT reference hardware for the §19 gate",
            },
        ],
    }


def _ingestion(mode: str) -> dict[str, Any]:
    if mode == "fake":
        res = bench_ingestion.run_fake()
        return {
            "benchmark": "ingestion",
            "mode": "fake",
            "data": res.as_dict(),
            "verdicts": [
                {
                    "target": "Ingestion throughput MUST NOT regress the P1 baseline",
                    "measured": f"{res.docs_per_sec:.2f} docs/s, {res.chunks_per_sec:.1f} chunks/s",
                    "verdict": "NEEDS-OVERLAY",
                    "note": (
                        "FAKE uses a no-network embed transform — NOT comparable to the "
                        "real-provider P1 baseline. Gross-regression smoke only; the real "
                        "gate runs LIVE against docs/baselines/phase1-ingestion.md."
                    ),
                }
            ],
        }
    res_l = bench_ingestion.run_live()
    if res_l is None:
        return {
            "benchmark": "ingestion",
            "mode": "live",
            "data": None,
            "verdicts": [
                {
                    "target": "Ingestion throughput MUST NOT regress the P1 baseline",
                    "measured": "n/a",
                    "verdict": "NEEDS-OVERLAY",
                    "note": "Qdrant unreachable — bring up the integration overlay",
                }
            ],
        }
    cmp = bench_ingestion.compare_against_baseline(res_l)
    if cmp["verdict"] == "pass":
        verdict = "MET"
    elif cmp["verdict"] == "regressed":
        verdict = "UNMET"
    else:
        verdict = "ENV-LIMITED"  # no-baseline / provider-mismatch
    return {
        "benchmark": "ingestion",
        "mode": "live",
        "data": res_l.as_dict(),
        "comparison": cmp,
        "verdicts": [
            {
                "target": "Ingestion throughput MUST NOT regress the P1 baseline",
                "measured": f"{res_l.docs_per_sec:.2f} docs/s, {res_l.chunks_per_sec:.1f} chunks/s",
                "verdict": verdict,
                "note": f"baseline comparison: {cmp['verdict']}",
            }
        ],
    }


def _coldstart(mode: str, *, docs: int) -> dict[str, Any]:
    if mode == "fake":
        res = bench_coldstart.run_fake(docs=docs)
        within = res.total_seconds < COLD_START_TARGET_SECONDS
        return {
            "benchmark": "coldstart",
            "mode": "fake",
            "data": res.as_dict(),
            "verdicts": [
                {
                    "target": "Cold start to first query, 1k docs < 30 min",
                    "measured": f"{res.total_seconds:.2f} s (proxy)",
                    "verdict": "MET-IN-HARNESS" if within else "UNMET",
                    "note": _FAKE_COLDSTART_NOTE,
                }
            ],
        }
    res_l = bench_coldstart.run_live(docs=docs)
    if res_l is None:
        return {
            "benchmark": "coldstart",
            "mode": "live",
            "data": None,
            "verdicts": [
                {
                    "target": "Cold start to first query, 1k docs < 30 min",
                    "measured": "n/a",
                    "verdict": "NEEDS-OVERLAY",
                    "note": "Qdrant unreachable — bring up the integration overlay",
                }
            ],
        }
    within = res_l.total_seconds < COLD_START_TARGET_SECONDS
    return {
        "benchmark": "coldstart",
        "mode": "live",
        "data": res_l.as_dict(),
        "verdicts": [
            {
                "target": "Cold start to first query, 1k docs < 30 min",
                "measured": f"{res_l.total_seconds:.2f} s",
                "verdict": "MET" if within else "UNMET",
                "note": "excludes `docker compose up` bring-up; not reference hardware",
            }
        ],
    }


# ---------------------------------------------------------------------------
# RESULTS.md writer
# ---------------------------------------------------------------------------


def _render_results_md(runs: list[dict[str, Any]], *, mode: str) -> str:
    ts = datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append("# RTFC Performance Benchmark Results (§4.5)")
    lines.append("")
    lines.append(
        "Generated by `python -m benchmarks all --write-results`. "
        "These numbers are recorded honestly: the FAKE-mode numbers below are "
        "**service-path / proxy** measurements in a developer environment. They "
        "are **NOT** the reference-deployment numbers the §19 release gate "
        "(criterion 3) requires. See the verdict legend."
    )
    lines.append("")
    lines.append(f"- Measured at: `{ts}`")
    lines.append(f"- Environment: `{env_description()}`")
    lines.append(f"- Mode: `{mode}`")
    lines.append("")
    lines.append("## Verdict legend")
    lines.append("")
    lines.append("| Verdict | Meaning |")
    lines.append("|---|---|")
    lines.append("| MET | Measured in this environment and within target. |")
    lines.append(
        "| MET-IN-HARNESS | Within a loose/proxy threshold, but this environment is "
        "not the reference deployment — does NOT satisfy the §19 gate alone. |"
    )
    lines.append("| UNMET | Measured and outside target. |")
    lines.append("| NEEDS-OVERLAY | Requires the docker-compose integration overlay (LIVE mode). |")
    lines.append(
        "| NEEDS-REFERENCE-HW | Requires the reference deployment / commodity reference "
        "hardware the orchestrator/owner must run. |"
    )
    lines.append("| ENV-LIMITED | Could not be measured meaningfully here. |")
    lines.append("")
    lines.append("## Verdict table")
    lines.append("")
    lines.append("| §4.5 Target | Measured | Verdict | Note |")
    lines.append("|---|---|---|---|")
    for run in runs:
        for v in run["verdicts"]:
            lines.append(f"| {v['target']} | {v['measured']} | **{v['verdict']}** | {v['note']} |")
    lines.append("")
    lines.append("## Raw measurements")
    lines.append("")
    lines.append("```json")
    lines.append(
        json.dumps(
            [
                {k: r[k] for k in ("benchmark", "mode", "data") if k in r}
                | ({"comparison": r["comparison"]} if "comparison" in r else {})
                for r in runs
            ],
            indent=2,
        )
    )
    lines.append("```")
    lines.append("")
    lines.append("## What still has to be run elsewhere")
    lines.append("")
    lines.append(
        "- **Retrieval p50/p99 (release gate):** run `python -m benchmarks retrieval "
        "--mode live --kb <promoted_kb>` under the compose overlay, then on the "
        "reference deployment (single-node Docker Compose, commodity hardware) for the "
        "§19 criterion-3 sign-off. FAKE numbers do NOT satisfy the gate."
    )
    lines.append(
        "- **Ingestion throughput regression gate:** run `python -m benchmarks ingestion "
        "--mode live` under the overlay with a real provider; it compares against "
        "`docs/baselines/phase1-ingestion.md` (>20% drop = fail). Populate that baseline "
        "first via `tests/phase1/test_throughput_baseline.py` if it has no data rows."
    )
    lines.append(
        "- **Cold-start (1k docs < 30 min):** run `python -m benchmarks coldstart "
        "--mode live --docs 1000` under the overlay, and separately time "
        "`docker compose up` bring-up, on reference hardware."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks", description="RTFC §4.5 perf harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("retrieval", "ingestion", "coldstart", "all"):
        p = sub.add_parser(name)
        p.add_argument("--mode", choices=["fake", "live"], default="fake")
        p.add_argument("--queries", type=int, default=2000)
        p.add_argument("--docs", type=int, default=1000)
        p.add_argument("--kb", type=str, default=None, help="promoted KB id (LIVE retrieval)")
        p.add_argument("--write-results", action="store_true")

    args = parser.parse_args(argv)

    runs: list[dict[str, Any]] = []
    if args.cmd in ("retrieval", "all"):
        runs.append(_retrieval(args.mode, queries=args.queries, kb=args.kb))
    if args.cmd in ("ingestion", "all"):
        runs.append(_ingestion(args.mode))
    if args.cmd in ("coldstart", "all"):
        runs.append(_coldstart(args.mode, docs=args.docs))

    print(json.dumps(runs, indent=2))

    if getattr(args, "write_results", False):
        _RESULTS_MD.write_text(_render_results_md(runs, mode=args.mode))
        print(f"\nWrote {_RESULTS_MD}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
