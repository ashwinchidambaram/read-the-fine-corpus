"""Ingest worker stub.

Pipeline stages Collect -> Assess -> Decompose -> Plan -> Build.
Queue-driven, checkpointed, resumable. Never in the request path.
Business logic will live in finecorpus.pipeline; this module is wiring only.
"""

import logging
import time

logger = logging.getLogger("finecorpus.ingest_worker")


def run() -> None:
    """Main worker loop: claim jobs from the queue and execute pipeline stages.

    Phase 0 stub: emits a heartbeat every 30 seconds to confirm the process is alive.
    Real job-claim logic arrives in Phase 1.
    """
    import finecorpus

    logger.info(
        "ingest-worker starting",
        extra={"service": "ingest-worker", "version": finecorpus.__version__},
    )
    while True:
        logger.info(
            "ingest-worker heartbeat — no jobs queued (Phase 0 stub)",
            extra={"service": "ingest-worker", "status": "idle"},
        )
        time.sleep(30)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    run()
