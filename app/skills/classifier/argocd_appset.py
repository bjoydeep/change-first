from __future__ import annotations

import re

from app.skills.classifier.base import Detector, PRType

_ARGOCD_KINDS = {"ApplicationSet", "Application"}

_KIND_RE = re.compile(r"^\+?kind:\s*(\w+)\s*$", re.MULTILINE)
# ArgoCD ApplicationSet uses cluster generators — label selectors, not ACM Placement
_CLUSTER_GENERATOR_RE = re.compile(r"clusterDecisionResource|generators:", re.MULTILINE)


class ArgocdAppSetDetector(Detector):
    kind = "argocd-appset"
    pillar = "app-lifecycle"

    def detect(self, diff: str, changed_files: list[str]) -> PRType | None:
        kinds_found = set(_KIND_RE.findall(diff))

        matched = kinds_found & _ARGOCD_KINDS
        if not matched:
            return None

        # ApplicationSet with clusterDecisionResource generator uses ACM PlacementDecision
        # — treat that as having placement-equivalent scope resolution available
        has_placement = bool(_CLUSTER_GENERATOR_RE.search(diff))

        return PRType(
            kind=self.kind,
            pillar=self.pillar,
            resources=sorted(matched),
            has_placement=has_placement,
        )
