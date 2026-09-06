"""Config <-> form binding — the ONE code path behind Easy and Proficient mode.

§3.3 (the discipline): "There MUST NOT be two pipelines. Mode is a UI-level
presentation concern over one execution engine. Any recommendation shown in Easy
mode is a concrete value in the same config object a Proficient user edits."

This module is that single seam.  It takes ONE ``IngestionConfig`` (as a dict —
the recommender's output) and produces an ordered list of :class:`ConfigField`
view objects.  Each field carries:

  - ``pointer``  — a JSON pointer into the config (the field's identity).
  - ``label``    — a human label.
  - ``value``    — the concrete value the recommender set.
  - ``basis`` / ``rationale`` — the recommender's provenance (§3.1 "why"), joined
    from the config's ``provenance`` list by matching JSON pointer.
  - ``advanced`` — whether Easy mode hides this field. Easy mode renders
    non-advanced fields as a read-only plain-language summary with the "why";
    Proficient mode renders EVERY field as an editable ``<input>``.

The editable field set is **derived from the config object itself**, not from a
hardcoded list of pointers.  :func:`build_fields`:

  1. Walks every ``provenance[].target`` pointer so that *every value the
     recommender set* becomes an editable field (§19 Phase-6 criterion 2), and
  2. Recursively walks the whole config dict and emits a field per editable leaf
     (per-class chunking/transformation/retrieval params, embedding
     model/provider/normalize, retrieval_defaults, ``default_rule/*``, salience
     weights, metadata schema).  Read-only/derived fields (``config_version``,
     ``schema_version``, ``secret_free_attestation``, provenance/rationale text,
     tenancy identity, ``created_at``, ``naive_baseline``) are excluded.

Because the set is derived from the config, adding a value to the recommender's
output automatically surfaces it in Proficient mode — the hardcoded-subset bug
(where recommender-set pointers with no matching hardcoded entry were silently
dropped) cannot recur.

Both modes call :func:`build_fields` on the *same* config and both post back
through :func:`apply_edits`.  Easy mode simply omits advanced inputs from the
form, so their recommender values survive untouched — it is literally Proficient
mode with the recommender's answers filled in (§3.1).

Class rules are addressed by their ``segment_class`` name in field pointers
(e.g. ``/class_rules/prose/chunking/max_tokens``) so the pointer matches the
recommender's provenance targets exactly; :func:`_resolve` / :func:`_set`
translate a class-name token into the underlying list index.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Field view model
# ---------------------------------------------------------------------------


@dataclass
class ConfigField:
    """One editable configuration value, shared by Easy and Proficient mode."""

    pointer: str
    label: str
    value: Any
    kind: str  # "text" | "number" | "bool" | "list" | "choice"
    basis: str | None = None
    rationale: str | None = None
    advanced: bool = False
    group: str = "general"
    choices: list[str] = field(default_factory=list)

    @property
    def value_str(self) -> str:
        """Render the value for an input's ``value=`` attribute / plain summary."""
        if isinstance(self.value, bool):
            return "true" if self.value else "false"
        if isinstance(self.value, list):
            return ", ".join(str(v) for v in self.value)
        return "" if self.value is None else str(self.value)


# ---------------------------------------------------------------------------
# Transformation operation vocabularies (from contracts.ingestion_config)
# ---------------------------------------------------------------------------
#
# Operation lists are rendered as membership booleans (one field per known
# operation) so that (a) the recommender's synthetic membership pointers such as
# ``/class_rules/prose/transformation/tier2_operations/breadcrumb_augment`` map
# to a real editable field, and (b) the full editable set (including operations
# the recommender did NOT enable, e.g. table_description) is surfaced.

_TIER_OPERATIONS: dict[str, list[str]] = {
    "tier1_operations": [
        "ocr_cleanup",
        "table_to_markdown",
        "whitespace_repair",
        "header_inference",
    ],
    "tier2_operations": [
        "breadcrumb_augment",
        "table_description",
        "class_context",
    ],
}


# ---------------------------------------------------------------------------
# Read-only / derived keys excluded from the editable surface
# ---------------------------------------------------------------------------
#
# These are identity, derived, or attestation fields the recommender computes;
# they are not user-editable config values (§3.2).  A top-level key here is
# skipped entirely; a leaf key here is skipped wherever it appears.

_EXCLUDED_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "config_version",
        "created_at",
        "tenancy",
        "naive_baseline",
        "provenance",
        "secret_free_attestation",
        "language_support",  # detected/observed, not user-set
        "exclusions_confirmed",
        # spreadsheet_triage and class_descriptions have dedicated UI surfaces
        # (M-023 triage select, M-027 description textareas) — not plain inputs.
        "spreadsheet_triage",
        "class_descriptions",
    }
)

_EXCLUDED_LEAF_KEYS = frozenset(
    {
        "segment_class",  # identity of a class rule, not an editable value
        "reference_id",
        "opt_in_ack",  # structural invariant, must stay True
        "tokenizer",  # derived, not surfaced as a free input
    }
)

# Fields shown in Easy mode's plain-language summary (everything else is
# "advanced" and hidden from Easy, but still present + editable in Proficient).
_EASY_LEAF_KEYS = frozenset({"max_tokens"})


# ---------------------------------------------------------------------------
# JSON-pointer helpers (RFC 6901 subset — no escaping needed for our keys)
# ---------------------------------------------------------------------------


def _class_index(node: list[Any], token: str) -> int | None:
    """Resolve a class_rules token to a list index.

    The token may be an integer index (legacy) or a ``segment_class`` name
    (the pointer form used throughout so pointers match provenance targets).
    """
    if token.lstrip("-").isdigit():
        idx = int(token)
        if -len(node) <= idx < len(node):
            return idx
        return None
    for i, entry in enumerate(node):
        if isinstance(entry, dict) and entry.get("segment_class") == token:
            return i
    return None


def _resolve(doc: Any, pointer: str) -> Any:
    node = doc
    parent_key: str | None = None
    for token in pointer.strip("/").split("/"):
        if token == "":
            continue
        if isinstance(node, list):
            if parent_key == "class_rules":
                idx = _class_index(node, token)
                if idx is None:
                    return None
                node = node[idx]
            else:
                try:
                    node = node[int(token)]
                except (ValueError, IndexError):
                    return None
        elif isinstance(node, dict):
            node = node.get(token)
        else:
            return None
        parent_key = token
    return node


def _set(doc: Any, pointer: str, value: Any) -> None:
    tokens = [t for t in pointer.strip("/").split("/") if t != ""]
    node = doc
    parent_key: str | None = None
    for token in tokens[:-1]:
        if isinstance(node, list):
            if parent_key == "class_rules":
                idx = _class_index(node, token)
                if idx is None:
                    return
                node = node[idx]
            else:
                node = node[int(token)]
        else:
            node = node[token]
        parent_key = token
    last = tokens[-1]
    if isinstance(node, list):
        if parent_key == "class_rules":
            idx = _class_index(node, last)
            if idx is not None:
                node[idx] = value
        else:
            node[int(last)] = value
    else:
        node[last] = value


# ---------------------------------------------------------------------------
# Provenance index
# ---------------------------------------------------------------------------


def _provenance_index(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Map each provenance ``target`` pointer → {basis, rationale}."""
    index: dict[str, dict[str, str]] = {}
    for p in config.get("provenance", []):
        target = p.get("target", "")
        index[target] = {
            "basis": p.get("basis", "heuristic"),
            "rationale": p.get("rationale", ""),
        }
    return index


def _lookup_provenance(
    index: dict[str, dict[str, str]], pointer: str
) -> tuple[str | None, str | None]:
    exact = index.get(pointer)
    if exact is not None:
        return exact["basis"], exact["rationale"]
    # Fall back to the nearest ancestor pointer that has provenance (e.g. a
    # /default_rule entry covering all fields under it, or a class-level entry).
    best: str | None = None
    for target in index:
        if pointer.startswith(target + "/") or pointer == target:
            if best is None or len(target) > len(best):
                best = target
    if best is not None:
        return index[best]["basis"], index[best]["rationale"]
    return None, None


# ---------------------------------------------------------------------------
# Coercion (form strings -> typed values)
# ---------------------------------------------------------------------------


def _coerce_like(existing: Any, raw: str) -> Any:
    """Coerce a submitted form string to the type of the existing value."""
    if isinstance(existing, bool):
        return raw.strip().lower() in ("true", "1", "on", "yes")
    if isinstance(existing, int) and not isinstance(existing, bool):
        try:
            return int(raw)
        except ValueError:
            return existing
    if isinstance(existing, float):
        try:
            return float(raw)
        except ValueError:
            return existing
    if isinstance(existing, list):
        items = [s.strip() for s in raw.split(",") if s.strip() != ""]
        return items
    if existing is None:
        # A previously-unset value: infer bool/number/text from the raw string.
        low = raw.strip().lower()
        if low in ("true", "false"):
            return low == "true"
        if raw.strip() == "":
            return None
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw
    return raw


# ---------------------------------------------------------------------------
# Human labels
# ---------------------------------------------------------------------------


def _humanise(token: str) -> str:
    return token.replace("_", " ").strip().capitalize()


def _leaf_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    return "text"


# ---------------------------------------------------------------------------
# Field enumeration (the ONE surface both modes render)
# ---------------------------------------------------------------------------


def _emit_membership_fields(
    ops_key: str,
    pointer_prefix: str,
    enabled: list[Any],
    prov: dict[str, dict[str, str]],
    group: str,
    out: list[ConfigField],
) -> None:
    """Render an operation list as one boolean membership field per known op."""
    enabled_set = {str(v) for v in enabled}
    vocab = list(_TIER_OPERATIONS[ops_key])
    # Include any operation actually present that isn't in the static vocab.
    for v in enabled_set:
        if v not in vocab:
            vocab.append(v)
    for op in vocab:
        ptr = f"{pointer_prefix}/{ops_key}/{op}"
        basis, rationale = _lookup_provenance(prov, ptr)
        out.append(
            ConfigField(
                pointer=ptr,
                label=f"{_humanise(ops_key)}: {op}",
                value=op in enabled_set,
                kind="bool",
                basis=basis,
                rationale=rationale,
                advanced=True,
                group=group,
            )
        )


def _walk(
    node: Any,
    pointer: str,
    prov: dict[str, dict[str, str]],
    group: str,
    out: list[ConfigField],
    *,
    label_prefix: str = "",
) -> None:
    """Recursively emit an editable field per leaf under ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _EXCLUDED_LEAF_KEYS:
                continue
            child_ptr = f"{pointer}/{key}"
            if key in _TIER_OPERATIONS:
                _emit_membership_fields(
                    key, pointer, value if isinstance(value, list) else [], prov, group, out
                )
                continue
            _walk(value, child_ptr, prov, group, out, label_prefix=label_prefix)
        return

    if isinstance(node, list):
        # Non-operation lists (e.g. salience filters, metadata_schema): render the
        # list as a single comma-joined editable value.
        _emit_leaf(node, pointer, prov, group, out, label_prefix=label_prefix)
        return

    _emit_leaf(node, pointer, prov, group, out, label_prefix=label_prefix)


def _emit_leaf(
    value: Any,
    pointer: str,
    prov: dict[str, dict[str, str]],
    group: str,
    out: list[ConfigField],
    *,
    label_prefix: str,
) -> None:
    tokens = [t for t in pointer.strip("/").split("/") if t]
    leaf = tokens[-1] if tokens else pointer
    basis, rationale = _lookup_provenance(prov, pointer)
    label = f"{label_prefix}{_humanise(leaf)}" if label_prefix else _humanise(leaf)
    advanced = leaf not in _EASY_LEAF_KEYS
    out.append(
        ConfigField(
            pointer=pointer,
            label=label,
            value=value,
            kind=_leaf_kind(value),
            basis=basis,
            rationale=rationale,
            advanced=advanced,
            group=group,
        )
    )


def build_fields(config: dict[str, Any]) -> list[ConfigField]:
    """Flatten an IngestionConfig dict into editable, provenance-joined fields.

    The field set is DERIVED FROM THE CONFIG ITSELF (not a hardcoded pointer
    list): every editable leaf of the config becomes a field, and — because the
    recursive walk covers the whole config — every ``provenance[].target`` the
    recommender set is guaranteed to have a corresponding editable field (§19
    criterion 2).  Read-only/derived fields are excluded (see ``_EXCLUDED_*``).
    """
    prov = _provenance_index(config)
    fields: list[ConfigField] = []
    seen: set[str] = set()

    # --- Per-class rules (addressed by segment_class name) ---
    for rule in config.get("class_rules", []):
        seg_class = rule.get("segment_class", "")
        prefix = f"/class_rules/{seg_class}"
        _walk(rule, prefix, prov, f"class:{seg_class}", fields, label_prefix=f"{seg_class}: ")

    # --- default_rule (the whole fallback subtree, §3.2) ---
    if isinstance(config.get("default_rule"), dict):
        _walk(
            config["default_rule"],
            "/default_rule",
            prov,
            "default_rule",
            fields,
            label_prefix="Default rule: ",
        )

    # --- KB-level config (embedding, retrieval_defaults) ---
    for top_key in ("embedding", "retrieval_defaults"):
        node = config.get(top_key)
        if node is None:
            continue
        _walk(node, f"/{top_key}", prov, top_key, fields, label_prefix=f"{_humanise(top_key)}: ")

    # --- Any other top-level editable scalar/list not excluded ---
    for key, value in config.items():
        if key in _EXCLUDED_TOP_LEVEL:
            continue
        if key in ("class_rules", "default_rule", "embedding", "retrieval_defaults"):
            continue
        if isinstance(value, dict):
            _walk(value, f"/{key}", prov, key, fields, label_prefix=f"{_humanise(key)}: ")
        else:
            _emit_leaf(value, f"/{key}", prov, key, fields, label_prefix="")

    # De-duplicate by pointer (defensive; walk order is already unique).
    unique: list[ConfigField] = []
    for f in fields:
        if f.pointer in seen:
            continue
        seen.add(f.pointer)
        unique.append(f)
    return unique


def easy_fields(config: dict[str, Any]) -> list[ConfigField]:
    """Fields shown in Easy mode: the non-advanced subset (read-only summary)."""
    return [f for f in build_fields(config) if not f.advanced]


def apply_edits(config: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
    """Return a copy of ``config`` with submitted field edits applied.

    ``form`` maps a *field key* to a submitted value.  The field key is the JSON
    pointer with ``/`` replaced by ``.`` and a ``fld.`` prefix (so it is a valid
    HTML form-field name), e.g. ``fld.class_rules.prose.chunking.max_tokens``.

    Only pointers that appear in :func:`build_fields` are writable — an Easy-mode
    post that omits advanced fields leaves their recommender values untouched
    (§3.1: Easy is Proficient with the recommender's answers filled in).  Unknown
    keys are ignored (defence against tampering); type coercion matches the
    existing value's type.  Operation-membership fields toggle membership of the
    operation in the underlying tier list.
    """
    updated = copy.deepcopy(config)
    known = {f.pointer: f for f in build_fields(config)}
    for key, raw in form.items():
        if not key.startswith("fld."):
            continue
        pointer = "/" + key[len("fld.") :].replace(".", "/")
        cfg_field = known.get(pointer)
        if cfg_field is None:
            continue
        if _apply_membership_edit(updated, pointer, raw):
            continue
        existing = _resolve(updated, pointer)
        _set(updated, pointer, _coerce_like(existing, raw))
    return updated


def _apply_membership_edit(doc: Any, pointer: str, raw: str) -> bool:
    """If ``pointer`` is an operation-membership field, toggle list membership.

    Returns True if handled (so the caller skips the scalar-set path).
    """
    tokens = [t for t in pointer.strip("/").split("/") if t]
    if len(tokens) < 2:
        return False
    ops_key, op = tokens[-2], tokens[-1]
    if ops_key not in _TIER_OPERATIONS:
        return False
    list_ptr = "/" + "/".join(tokens[:-1])
    current = _resolve(doc, list_ptr)
    if not isinstance(current, list):
        current = []
    present = op in {str(v) for v in current}
    want = raw.strip().lower() in ("true", "1", "on", "yes")
    if want and not present:
        current = [*current, op]
    elif not want and present:
        current = [v for v in current if str(v) != op]
    _set(doc, list_ptr, current)
    return True


def field_key(pointer: str) -> str:
    """Convert a JSON pointer to its HTML form-field name."""
    return "fld." + pointer.strip("/").replace("/", ".")


__all__ = [
    "ConfigField",
    "apply_edits",
    "build_fields",
    "easy_fields",
    "field_key",
]
