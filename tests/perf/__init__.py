"""Performance benchmark smoke tests (Phase 7, WU-D).

FAKE-mode regression smoke for the §4.5 performance harness under
``benchmarks/`` at the repo root.  Named ``tests/perf`` (not ``tests/benchmarks``)
to avoid shadowing the top-level ``benchmarks`` package on sys.path.  LIVE/overlay
benchmarks are exercised manually via ``python -m benchmarks ... --mode live``.
"""
