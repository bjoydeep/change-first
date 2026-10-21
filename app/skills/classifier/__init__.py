from app.skills.classifier.argocd_appset import ArgocdAppSetDetector
from app.skills.classifier.base import PRType
from app.skills.classifier.governance_policy import GovernancePolicyDetector

_DETECTORS = [
    GovernancePolicyDetector(),
    ArgocdAppSetDetector(),
    # Day 2: append new detector instances here only
]


def classify(diff: str, changed_files: list[str]) -> list[PRType]:
    """Run all detectors and return every type matched by this PR."""
    results = []
    for detector in _DETECTORS:
        result = detector.detect(diff, changed_files)
        if result is not None:
            results.append(result)
    return results
