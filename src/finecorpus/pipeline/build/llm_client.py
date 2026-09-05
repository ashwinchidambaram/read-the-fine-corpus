"""LLM-backed AugmentationClient adapter for the Build stage.

Satisfies the ``AugmentationClient`` Protocol from ``augment.py`` using the
real ``finecorpus.llm`` layer (``run_operation`` + ``AugmentationInput``).

Design
------
- ONE LLM call per table segment (not per chunk fragment).  Results are cached
  in the artifact store keyed ``(content_hash, segment_path, model_id)`` so
  resumed builds do not re-bill for the same table.
- Injection of ``FakeLLMProvider`` is the test path; see ``test_preview_dry_run``
  and ``test_t04_byte_identity``.
- Tier toggles: if ``tier2_enabled=False`` on the class rule, Build should not
  construct an ``LLMBuildClient`` at all (no client → no LLM construction).

Cache format
------------
The cache is a plain JSON file ``llm_cache.json`` in the run directory:
  { "<cache_key>": "<description_string>", ... }

Cache key: ``"<content_hash>|<segment_path>|<model_id>"``

The key encodes the content hash (document revision), segment path (which table
in the document), and the LLM model in use — a change in any of these must be
a cache miss to avoid stale descriptions.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from finecorpus.llm.base import LLMProvider
    from finecorpus.llm.operations import ResolvedOpConfig

logger = logging.getLogger(__name__)

# Characters per token approximation (same proxy used in augment.py heuristic)
_CHARS_PER_TOKEN_APPROX = 4


def _cache_key(content_hash: str, segment_path: str, model_id: str) -> str:
    """Canonical cache key for a table description."""
    return f"{content_hash}|{segment_path}|{model_id}"


class LLMBuildClient:
    """Real LLM-backed AugmentationClient for the Build stage.

    Satisfies the ``AugmentationClient`` Protocol (``describe_table`` method).

    Single LLM call per table segment across its fragments — the caller
    (Build stage) must pass the segment-level cache key so repeated calls
    for different chunk fragments of the same table are cache hits.

    Args:
        provider: Constructed ``LLMProvider`` instance.
        op_config: Fully-resolved op config for the augmentation operation.
        run_dir: Run directory for the persistent cache file (``llm_cache.json``).
            If ``None``, an in-memory cache is used (no persistence between runs).
        content_hash: Document content hash (for cache key construction).
        segment_path: Segment path within the document (for cache key).
    """

    def __init__(
        self,
        provider: LLMProvider,
        op_config: ResolvedOpConfig,
        run_dir: pathlib.Path | None = None,
        content_hash: str = "",
        segment_path: str = "",
        _shared_call_count: list[int] | None = None,
    ) -> None:
        self._provider = provider
        self._op_config = op_config
        self._run_dir = run_dir
        self._content_hash = content_hash
        self._segment_path = segment_path

        # In-memory cache (shared across all describe_table calls on this instance)
        self._mem_cache: dict[str, str] = {}

        # Load persistent cache if run_dir is set
        self._cache_path: pathlib.Path | None
        if run_dir is not None:
            self._cache_path = run_dir / "llm_cache.json"
            self._load_persistent_cache()
        else:
            self._cache_path = None

        # Shared mutable call counter — all clients created via for_segment share the
        # same list[int] so the root can always read the aggregate call count.
        self._shared_call_count: list[int] = (
            _shared_call_count if _shared_call_count is not None else [0]
        )

    @property
    def call_count(self) -> int:
        """Total LLM calls made (across this client and all for_segment children)."""
        return self._shared_call_count[0]

    def _load_persistent_cache(self) -> None:
        """Load the persistent cache from disk into _mem_cache.

        M-067: cached LLM output is still LLM output — validate on read.
        Only entries where BOTH key AND value are plain ``str`` are accepted.
        Non-conforming entries are dropped with a logged warning.
        """
        if self._cache_path is None or not self._cache_path.exists():
            return
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                dropped = 0
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str):
                        self._mem_cache[k] = v
                    else:
                        dropped += 1
                        logger.warning(
                            "M-067: LLM cache entry dropped — key or value is not a string "
                            "(key type=%s, value type=%s); re-calling LLM for this entry.",
                            type(k).__name__,
                            type(v).__name__,
                        )
                if dropped:
                    logger.warning(
                        "M-067: LLM cache: %d non-conforming entries dropped on read from %s",
                        dropped,
                        self._cache_path,
                    )
        except Exception as exc:
            logger.warning("LLM cache read failed (%s); starting with empty cache", exc)

    def _save_persistent_cache(self) -> None:
        """Persist the in-memory cache to disk."""
        if self._cache_path is None:
            return
        try:
            self._cache_path.write_text(
                json.dumps(self._mem_cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("LLM cache write failed (%s); cache will not persist", exc)

    def describe_table(self, shape: tuple[int, int], sample: str) -> str:
        """Return an NL description of the table, using the LLM with caching.

        ONE LLM call per unique (content_hash, segment_path, model_id) triple.
        Subsequent calls with the same key return the cached description.

        Args:
            shape: ``(rows, cols)`` of the table.
            sample: A short text representation of the table for the LLM.

        Returns:
            NL description string (verbatim from the LLM or cache).
        """
        key = _cache_key(
            self._content_hash,
            self._segment_path,
            self._op_config.model_id,
        )

        if key in self._mem_cache:
            logger.debug("LLM table description cache hit: %s", key)
            return self._mem_cache[key]

        # Cache miss — call the LLM
        self._shared_call_count[0] += 1
        logger.debug(
            "LLM table description cache miss: %s (call #%d)",
            key,
            self._shared_call_count[0],
        )

        description = self._call_llm(shape, sample)

        self._mem_cache[key] = description
        self._save_persistent_cache()
        return description

    def _call_llm(self, shape: tuple[int, int], sample: str) -> str:
        """Make the actual LLM call and return the description string."""
        from finecorpus.llm.operations import (
            AugmentationInput,
            ContentType,
            run_operation,
        )

        rows, cols = shape
        inp = AugmentationInput(
            content=sample,
            structural_path=[],
            class_description=None,
            content_type=ContentType.table,
        )

        try:
            result = run_operation(
                provider=self._provider,
                op_config=self._op_config,
                input_model=inp,
            )
            # result is AugmentationOutput; use natural_language_description
            from finecorpus.llm.operations import AugmentationOutput

            if isinstance(result, AugmentationOutput):
                desc = result.natural_language_description
                if desc:
                    return desc
            # Fallback: synthesize from shape
            return f"Table with {rows} rows and {cols} columns."
        except Exception as exc:
            logger.warning(
                "LLM table description failed for (%d, %d) — using fallback: %s",
                rows,
                cols,
                type(exc).__name__,
            )
            return f"Table with {rows} rows and {cols} columns."

    def for_segment(self, content_hash: str, segment_path: str) -> LLMBuildClient:
        """Return a new client bound to a different segment (same provider/cache).

        Used by the Build stage to create per-segment clients while sharing the
        same underlying provider and persistent cache file.
        """
        client = LLMBuildClient(
            provider=self._provider,
            op_config=self._op_config,
            run_dir=self._run_dir,
            content_hash=content_hash,
            segment_path=segment_path,
            _shared_call_count=self._shared_call_count,
        )
        # Share the in-memory cache and cache path so cross-segment lookups are fast
        client._mem_cache = self._mem_cache
        client._cache_path = self._cache_path
        return client


__all__ = [
    "LLMBuildClient",
]
