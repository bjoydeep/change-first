from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import yaml


class PlacementCase(Enum):
    # Placement spec is new or modified in this diff — must evaluate selector against live clusters
    EVALUATE = "evaluate"
    # PlacementBinding references an existing Placement not touched in this diff
    # PlacementDecision already exists on hub — just read it
    READ_DECISION = "read_decision"
    # No Placement or PlacementBinding found — Tier 2 scope resolution not possible
    ABSENT = "absent"


@dataclass
class PlacementResult:
    case: PlacementCase
    placement_name: str = ""
    placement_namespace: str = ""
    # Full label selector extracted from the Placement spec (for EVALUATE case)
    match_labels: dict = field(default_factory=dict)
    match_expressions: list = field(default_factory=list)


def _split_diff_by_file(diff: str) -> dict[str, str]:
    """Split a unified diff into per-file chunks keyed by filename."""
    files: dict[str, str] = {}
    current_file = ""
    current_lines: list[str] = []

    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            if current_file and current_lines:
                files[current_file] = "\n".join(current_lines)
            current_file = line[6:]  # strip "+++ b/"
            current_lines = []
        elif line.startswith("--- ") or line.startswith("+++ "):
            continue
        else:
            current_lines.append(line)

    if current_file and current_lines:
        files[current_file] = "\n".join(current_lines)

    return files


def _clean_chunk(chunk: str) -> str:
    """Strip diff markers from a single file's diff chunk."""
    lines = []
    for line in chunk.splitlines():
        if line.startswith("@@"):
            continue
        elif line.startswith("+"):
            lines.append(line[1:])
        elif line.startswith("-"):
            continue
        else:
            lines.append(line)
    return "\n".join(lines)


def _parse_yaml_docs(diff: str) -> list[dict]:
    """
    Extract and parse YAML documents only from .yaml/.yml files in the diff.
    Skips non-YAML files (e.g. .gitignore, shell scripts) to avoid parse errors.
    """
    docs = []
    for filename, chunk in _split_diff_by_file(diff).items():
        if not (filename.endswith(".yaml") or filename.endswith(".yml")):
            continue
        clean = _clean_chunk(chunk)
        try:
            for doc in yaml.safe_load_all(clean):
                if isinstance(doc, dict):
                    docs.append(doc)
        except yaml.YAMLError:
            pass
    return docs


def _extract_selector(placement: dict) -> tuple[dict, list]:
    """
    Extract matchLabels and matchExpressions from a Placement spec.
    Handles both old API (spec.clusterSelector) and new API (spec.predicates).
    """
    spec = placement.get("spec", {})
    match_labels: dict = {}
    match_expressions: list = []

    # Old API: spec.clusterSelector.matchLabels
    cluster_selector = spec.get("clusterSelector", {})
    if cluster_selector:
        match_labels = cluster_selector.get("matchLabels", {})
        match_expressions = cluster_selector.get("matchExpressions", [])

    # New API: spec.predicates[].requiredClusterSelector.labelSelector
    for predicate in spec.get("predicates", []):
        label_selector = (
            predicate.get("requiredClusterSelector", {}).get("labelSelector", {})
        )
        if label_selector:
            match_labels.update(label_selector.get("matchLabels", {}))
            match_expressions.extend(label_selector.get("matchExpressions", []))

    return match_labels, match_expressions


def parse_placement(diff: str) -> PlacementResult:
    """
    Detect Placement / PlacementBinding in a PR diff and classify into one of
    three cases (EVALUATE, READ_DECISION, ABSENT).
    """
    docs = _parse_yaml_docs(diff)

    placement_doc: dict | None = None
    binding_doc: dict | None = None

    for doc in docs:
        kind = doc.get("kind", "")
        if kind == "Placement":
            placement_doc = doc
        elif kind == "PlacementBinding":
            binding_doc = doc

    if placement_doc is None and binding_doc is None:
        return PlacementResult(case=PlacementCase.ABSENT)

    if placement_doc is not None:
        # Case A or B — Placement spec is in the diff, evaluate against live clusters
        name = placement_doc.get("metadata", {}).get("name", "")
        namespace = placement_doc.get("metadata", {}).get("namespace", "")
        match_labels, match_expressions = _extract_selector(placement_doc)
        return PlacementResult(
            case=PlacementCase.EVALUATE,
            placement_name=name,
            placement_namespace=namespace,
            match_labels=match_labels,
            match_expressions=match_expressions,
        )

    # Case C — only PlacementBinding in diff, references an existing Placement
    name = binding_doc.get("metadata", {}).get("name", "")  # type: ignore[union-attr]
    namespace = binding_doc.get("metadata", {}).get("namespace", "")
    ref = binding_doc.get("placementRef", {})  # type: ignore[union-attr]
    placement_name = ref.get("name", "")

    return PlacementResult(
        case=PlacementCase.READ_DECISION,
        placement_name=placement_name,
        placement_namespace=namespace,
    )
