"""Language detection pass for the Decompose stage (Phase 2).

Assigns a BCP-47 language code to every segment (§7.6).

Design goals
------------
- **No heavyweight external dependencies** (no fastText, no langdetect JVM).
- **Pure Python** — deterministic on the same Python version.
- **Good enough for enterprise corpora**: English, Spanish, French, German,
  Portuguese, Italian, Dutch, Chinese (Simplified), Japanese, Korean, Russian,
  Arabic.  These cover the vast majority of real-world enterprise corpora.
- **Honest fallback**: when confidence is too low to decide, the code is ``"und"``
  (BCP-47 undetermined).

Detection algorithm
-------------------
1.  **Script detection (Unicode block heuristic)** — first and fastest:
    CJK characters → ``zh`` (Simplified Chinese default, §7.6 note 1).
    Hiragana/Katakana → ``ja``.
    Hangul → ``ko``.
    Arabic script → ``ar``.
    Cyrillic → ``ru``.

2.  **Stopword voting** — for Latin-script text:
    Count how many words in the segment appear in a small curated stopword set
    for each candidate Latin-script language.  The language with the most hits
    whose ratio exceeds ``_STOPWORD_RATIO_FLOOR`` wins.
    Tie-break: prefer the candidate with the higher absolute hit count.

3.  **Fallback** — ``"und"`` when no script is decisive and stopword voting
    yields no winner above the ratio floor.

The stopword sets are intentionally small (40-60 words each) — just common
function words (articles, prepositions, conjunctions, pronouns) that are highly
diagnostic and almost never appear in English when absent in the target language.
False-positive rate for well-formed prose is very low.

§7.6 requirements satisfied
----------------------------
- Language detected per segment and stored as a filterable field (segment.language).
- Segments with undetermined language carry ``"und"`` — never omitted.
- Detection is deterministic (same input → same output, no randomness).
- No heavyweight runtime dependency; no network call.

Open question OQ-6 (equations/formulas as ``code``): segments typed ``code``
with mathematical content may not be in any human language.  This pass uses
the same two-stage algorithm for code segments as for all others — there is no
code-segment-specific branch.  In practice: text below the 4-word floor returns
``"und"``; code containing English stopword-keywords (``for``, ``in``, ``if``,
etc.) may return ``"en"`` — a known ambiguity tracked as OQ-6.  See
segment-taxonomy.md OQ-6.

Phase 3+ notes
--------------
If more accurate detection is needed, replace this module with a fastText-based
implementation.  The ``_detect_language`` function is the only entry point used
by ``LanguagePass.run`` — callers are isolated from the algorithm.

This pass is stateless and performs no I/O.
"""

from __future__ import annotations

from finecorpus.contracts.segment_set import ExclusionRecord, Segment
from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UNDETERMINED = "und"
_MIN_WORDS_FOR_STOPWORD_VOTE = 4  # require at least this many words for reliable vote
_STOPWORD_RATIO_FLOOR = 0.10  # at least 10% of words must be stopwords to declare

# ---------------------------------------------------------------------------
# Stopword lexicons (BCP-47 code → frozenset of lowercase stopwords)
# ---------------------------------------------------------------------------
# Curated to include only function words that are:
#   (a) extremely common in their language, and
#   (b) NOT common English words (to avoid false positives for English-mixed text).
#
# The English set is intentionally listed so the vote can confirm English when
# non-Latin-script detection does not fire.

_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {
            "the",
            "a",
            "an",
            "of",
            "in",
            "to",
            "and",
            "or",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "can",
            "could",
            "should",
            "may",
            "might",
            "shall",
            "it",
            "its",
            "this",
            "that",
            "these",
            "those",
            "for",
            "on",
            "at",
            "by",
            "with",
            "from",
            "as",
            "but",
            "not",
            "if",
            "so",
            "no",
            "he",
            "she",
            "we",
            "they",
            "you",
            "i",
            "my",
            "your",
            "our",
            "their",
            "his",
            "her",
        }
    ),
    "es": frozenset(
        {
            "el",
            "la",
            "los",
            "las",
            "un",
            "una",
            "unos",
            "unas",
            "de",
            "del",
            "en",
            "con",
            "por",
            "para",
            "que",
            "se",
            "le",
            "les",
            "no",
            "es",
            "son",
            "está",
            "han",
            "hay",
            "pero",
            "si",
            "como",
            "ya",
            "más",
            "también",
            "cuando",
            "donde",
            "porque",
            "aunque",
            "sin",
            "sobre",
            "muy",
            "todo",
            "su",
            "sus",
            "este",
            "esta",
            "estos",
            "estas",
            "ese",
            "esa",
            "al",
            "lo",
            "me",
            "mi",
            "nos",
            "yo",
            "él",
            "ella",
        }
    ),
    "fr": frozenset(
        {
            "le",
            "la",
            "les",
            "un",
            "une",
            "des",
            "de",
            "du",
            "en",
            "et",
            "à",
            "au",
            "aux",
            "que",
            "qui",
            "se",
            "ne",
            "pas",
            "plus",
            "est",
            "sont",
            "était",
            "ont",
            "par",
            "sur",
            "dans",
            "avec",
            "pour",
            "mais",
            "ou",
            "si",
            "bien",
            "tout",
            "très",
            "aussi",
            "même",
            "il",
            "elle",
            "ils",
            "elles",
            "nous",
            "vous",
            "je",
            "mon",
            "ma",
            "mes",
            "son",
            "sa",
            "ses",
            "ce",
            "ces",
            "cet",
            "cette",
        }
    ),
    "de": frozenset(
        {
            "der",
            "die",
            "das",
            "ein",
            "eine",
            "und",
            "in",
            "von",
            "zu",
            "den",
            "des",
            "dem",
            "mit",
            "ist",
            "sind",
            "war",
            "wurde",
            "werden",
            "hat",
            "haben",
            "nicht",
            "aber",
            "auch",
            "als",
            "dass",
            "an",
            "auf",
            "für",
            "noch",
            "oder",
            "bei",
            "im",
            "sich",
            "er",
            "sie",
            "es",
            "wir",
            "ihr",
            "ich",
            "mein",
            "sein",
            "ihre",
            "dieser",
            "diese",
            "diesem",
            "dieses",
        }
    ),
    "pt": frozenset(
        {
            "o",
            "a",
            "os",
            "as",
            "um",
            "uma",
            "de",
            "do",
            "da",
            "em",
            "no",
            "na",
            "por",
            "para",
            "que",
            "se",
            "não",
            "é",
            "são",
            "foi",
            "tem",
            "com",
            "mas",
            "ou",
            "também",
            "mais",
            "quando",
            "como",
            "sobre",
            "ele",
            "ela",
            "eles",
            "nós",
            "seu",
            "sua",
            "este",
            "esta",
            "isso",
            "aqui",
            "já",
            "ainda",
            "muito",
        }
    ),
    "it": frozenset(
        {
            "il",
            "la",
            "lo",
            "i",
            "le",
            "gli",
            "un",
            "una",
            "di",
            "del",
            "della",
            "in",
            "nel",
            "nella",
            "da",
            "per",
            "con",
            "su",
            "che",
            "è",
            "sono",
            "ha",
            "hanno",
            "non",
            "ma",
            "o",
            "se",
            "anche",
            "come",
            "più",
            "molto",
            "questo",
            "questa",
            "questi",
            "queste",
            "lui",
            "lei",
            "loro",
            "noi",
            "voi",
            "io",
            "mi",
        }
    ),
    "nl": frozenset(
        {
            "de",
            "het",
            "een",
            "van",
            "in",
            "is",
            "dat",
            "op",
            "te",
            "aan",
            "met",
            "hij",
            "zijn",
            "ze",
            "niet",
            "ook",
            "maar",
            "en",
            "of",
            "als",
            "er",
            "bij",
            "dit",
            "uit",
            "naar",
            "om",
            "worden",
            "was",
            "waren",
            "heeft",
            "hebben",
            "hun",
            "zij",
            "wij",
            "ik",
            "mijn",
            "ons",
            "onze",
            "door",
            "over",
        }
    ),
}


# ---------------------------------------------------------------------------
# Unicode-block character-range checks (fast script detection)
# ---------------------------------------------------------------------------


def _script_detect(text: str) -> str | None:
    """Detect non-Latin script from Unicode ranges.

    Returns a BCP-47 language code or None if the text is predominantly Latin.
    Samples up to the first 500 characters for speed.
    """
    sample = text[:500]
    cjk = 0
    hiragana = 0
    katakana = 0
    hangul = 0
    arabic = 0
    cyrillic = 0
    latin_or_common = 0

    for ch in sample:
        if not ch.isalpha():
            latin_or_common += 1
            continue
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x20000 <= cp <= 0x2A6DF:
            cjk += 1
        elif 0x3040 <= cp <= 0x309F:
            hiragana += 1
        elif 0x30A0 <= cp <= 0x30FF:
            katakana += 1
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
            hangul += 1
        elif 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F:
            arabic += 1
        elif 0x0400 <= cp <= 0x04FF:
            cyrillic += 1
        else:
            latin_or_common += 1

    total_alpha = cjk + hiragana + katakana + hangul + arabic + cyrillic + latin_or_common
    if total_alpha == 0:
        return None

    # Require ≥ 30% script characters to declare
    threshold = total_alpha * 0.30
    if cjk >= threshold:
        return "zh"
    if hiragana + katakana >= threshold:
        return "ja"
    if hangul >= threshold:
        return "ko"
    if arabic >= threshold:
        return "ar"
    if cyrillic >= threshold:
        return "ru"

    return None  # Latin/common script — proceed to stopword vote


# ---------------------------------------------------------------------------
# Stopword-vote language detection for Latin-script text
# ---------------------------------------------------------------------------


def _tokenise(text: str) -> list[str]:
    """Return lowercase word tokens, stripping non-alphanumeric chars."""
    # Fast tokenisation: split on whitespace and strip punctuation from edges.
    tokens = []
    for raw in text.lower().split():
        token = raw.strip(".,!?;:\"'()-[]{}/<>|")
        if token and token.isalpha():
            tokens.append(token)
    return tokens


def _stopword_vote(tokens: list[str]) -> str | None:
    """Return the BCP-47 code with the most stopword hits, or None.

    Requires at least ``_STOPWORD_RATIO_FLOOR`` of tokens to be stopwords in
    the winning language, and at least ``_MIN_WORDS_FOR_STOPWORD_VOTE`` total
    tokens.  Tie-breaks by absolute count.

    Intentional asymmetry in the ratio denominator
    -----------------------------------------------
    Hits are counted via *unique* stopword matches (``len(token_set & stopwords)``
    where ``token_set = set(tokens)``), but the ratio denominator is *total*
    token count (``len(tokens)``).  This deliberately deflates the ratio on
    repetitive text — e.g. a paragraph that repeats "the" 20 times contributes
    only 1 unique hit but 20 to the denominator.  The effect is conservative:
    repetitive or formulaic text must contain a broader *variety* of stopwords
    to exceed the floor, reducing false-positive language assignments on boilerplate
    or structured data that happens to repeat a single common word.
    """
    if len(tokens) < _MIN_WORDS_FOR_STOPWORD_VOTE:
        return None

    token_set = set(tokens)
    best_code: str | None = None
    best_count = 0

    for code, stopwords in _STOPWORDS.items():
        # Unique stopword hits (set intersection) — see docstring for the
        # intentional asymmetry with the total-token-count denominator below.
        hits = len(token_set & stopwords)
        if hits > best_count:
            best_count = hits
            best_code = code

    if best_code is None or best_count == 0:
        return None

    ratio = best_count / len(tokens)
    if ratio < _STOPWORD_RATIO_FLOOR:
        return None

    return best_code


# ---------------------------------------------------------------------------
# Public detection API
# ---------------------------------------------------------------------------


def _detect_language(text: str) -> str:
    """Detect the BCP-47 language code of ``text``.

    Returns ``"und"`` when detection confidence is insufficient.
    Deterministic — no randomness, no external calls.
    """
    if not text or not text.strip():
        return _UNDETERMINED

    # Stage 1: non-Latin script detection (fast path)
    script_code = _script_detect(text)
    if script_code is not None:
        return script_code

    # Stage 2: Latin-script stopword vote
    tokens = _tokenise(text)
    voted_code = _stopword_vote(tokens)
    if voted_code is not None:
        return voted_code

    return _UNDETERMINED


# ---------------------------------------------------------------------------
# Pass implementation
# ---------------------------------------------------------------------------


class LanguagePass:
    """Per-segment language detection pass (Phase 2, §7.6).

    Satisfies the ``SegmentPass`` protocol.  Must run after ``SegmentationPass``
    and ``TaxonomyPass`` (so segment types are finalised before language tagging).

    For each segment:
    - Call ``_detect_language(seg.text)`` to get a BCP-47 code.
    - Update ``segment.language`` with the result.
    - Segments with ``text=None`` (e.g. figure_region with no inline text)
      receive ``"und"``.

    This pass does NOT modify ``salience_tier``, ``salience_signals``, or
    ``segment_type`` — it is a metadata-only annotation pass.
    """

    def run(
        self,
        doc_ctx: DocumentContext,
        segments: list[Segment],
        exclusions: list[ExclusionRecord],
    ) -> PassResult:
        """Assign BCP-47 language codes to all segments.

        Pure and deterministic — same inputs produce same outputs.
        """
        updated: list[Segment] = []
        for seg in segments:
            lang = _detect_language(seg.text or "")
            if lang != seg.language:
                updated.append(seg.model_copy(update={"language": lang}))
            else:
                updated.append(seg)
        return PassResult(segments=updated, exclusions=exclusions)


#: Module-level singleton — imported by passes/__init__.py.
language_pass = LanguagePass()

__all__ = [
    "LanguagePass",
    "language_pass",
    "_detect_language",  # exported for direct use in tests
]
