from __future__ import annotations

import re

from app.skills.classifier.base import Detector, PRType

# Kinds that signal an ACM governance policy PR
_POLICY_KINDS = {"Policy", "ConfigPolicy", "CertPolicy", "IamPolicy", "OperatorPolicy"}
_PLACEMENT_BINDING_KIND = "PlacementBinding"

_KIND_RE = re.compile(r"^\+?kind:\s*(\w+)\s*$", re.MULTILINE)


class GovernancePolicyDetector(Detector):
    kind = "governance-policy"
    pillar = "governance"

    def detect(self, diff: str, changed_files: list[str]) -> PRType | None:
        kinds_found = set(_KIND_RE.findall(diff))

        matched = kinds_found & _POLICY_KINDS
        if not matched:
            return None

        has_placement = _PLACEMENT_BINDING_KIND in kinds_found

        return PRType(
            kind=self.kind,
            pillar=self.pillar,
            resources=sorted(matched),
            has_placement=has_placement,
        )
