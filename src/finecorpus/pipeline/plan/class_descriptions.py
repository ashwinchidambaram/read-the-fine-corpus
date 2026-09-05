"""Class description loader for the Plan stage.

Loads per-class description text from a YAML file and validates it against
the ClassDescription contract model.

YAML schema (list of mappings):

    - class: <segment_type>   # one of the SegmentType enum values (e.g. "prose")
      description: <text>     # plain-language description of what this class is

Example:

    - class: prose
      description: >
        Narrative paragraphs from policy documents. Users will ask questions
        about rules, requirements, and definitions.
    - class: table
      description: >
        Data tables showing metrics and benchmarks. Users need specific numbers.

Validation errors are clear and name the offending entry (item index + class value).
"""

from __future__ import annotations

import pathlib

from pydantic import ValidationError

from finecorpus.contracts.ingestion_config import ClassDescription
from finecorpus.contracts.shared.blocks import SegmentType


class ClassDescriptionError(ValueError):
    """Raised when a class description file fails validation."""


def load_class_descriptions(path: str | pathlib.Path) -> list[ClassDescription]:
    """Load class descriptions from a YAML file.

    Args:
        path: Path to the YAML file. The file must contain a YAML list of
            ``{class: <segment_type>, description: <text>}`` mappings.
            The ``class`` key must be a valid SegmentType enum value.

    Returns:
        List of validated ClassDescription objects. Empty list if the file
        contains an empty list.

    Raises:
        ClassDescriptionError: If the file cannot be read, parsed, or validated.
            Error messages name the offending entry by index and class value.
    """
    try:
        import yaml
    except ImportError as exc:
        raise ClassDescriptionError(
            "PyYAML is required to load class descriptions. Install it with: pip install pyyaml"
        ) from exc

    path = pathlib.Path(path)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ClassDescriptionError(
            f"Could not read class descriptions file {path}: {exc}"
        ) from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ClassDescriptionError(f"Could not parse {path} as YAML: {exc}") from exc

    if raw is None:
        return []

    if not isinstance(raw, list):
        raise ClassDescriptionError(
            f"Class descriptions file {path} must contain a YAML list "
            f"(got {type(raw).__name__}). "
            "Each item must be a mapping with 'class' and 'description' keys. "
            "See the module docstring for the expected schema."
        )

    results: list[ClassDescription] = []
    for idx, item in enumerate(raw):
        entry_label = f"entry {idx}"
        if not isinstance(item, dict):
            raise ClassDescriptionError(
                f"Class descriptions file {path}: {entry_label} is not a mapping "
                f"(got {type(item).__name__}). "
                "Each item must have 'class' and 'description' keys."
            )

        class_val = item.get("class")
        desc_text = item.get("description")

        if class_val is None:
            raise ClassDescriptionError(
                f"Class descriptions file {path}: {entry_label} is missing the 'class' key. "
                f"Got keys: {sorted(item.keys())}. "
                "Each entry must have both 'class' and 'description'."
            )
        if desc_text is None:
            raise ClassDescriptionError(
                f"Class descriptions file {path}: {entry_label} (class={class_val!r}) "
                "is missing the 'description' key. "
                "Each entry must have both 'class' and 'description'."
            )
        if not isinstance(desc_text, str) or not desc_text.strip():
            raise ClassDescriptionError(
                f"Class descriptions file {path}: {entry_label} (class={class_val!r}) "
                "has an empty or non-string description. "
                "Provide a non-empty plain-language description."
            )

        # Validate segment type
        try:
            seg_type = SegmentType(class_val)
        except ValueError:
            valid = sorted(v.value for v in SegmentType)
            raise ClassDescriptionError(
                f"Class descriptions file {path}: {entry_label} has an unknown class "
                f"value {class_val!r}. "
                f"Valid segment types: {valid}"
            ) from None

        # Build and validate the ClassDescription model
        try:
            cd = ClassDescription(
                segment_class=seg_type,
                class_id=seg_type.value,
                description=desc_text.strip(),
            )
        except ValidationError as exc:
            raise ClassDescriptionError(
                f"Class descriptions file {path}: {entry_label} (class={class_val!r}) "
                f"failed contract validation: {exc}"
            ) from exc

        results.append(cd)

    # Check for duplicate class values (the IngestionConfig validator catches it too,
    # but we give a clearer error here pointing at the file)
    seen_classes: set[str] = set()
    for cd in results:
        if cd.class_id in seen_classes:
            raise ClassDescriptionError(
                f"Class descriptions file {path}: duplicate class {cd.class_id!r}. "
                "Each class may appear at most once. "
                "Remove or merge the duplicate entries."
            )
        seen_classes.add(cd.class_id)

    return results


__all__ = [
    "ClassDescriptionError",
    "load_class_descriptions",
]
