from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class PRType:
    kind: str           # "governance-policy", "argocd-appset"
    pillar: str         # "governance", "app-lifecycle"
    resources: list[str] = field(default_factory=list)  # kinds found in diff
    has_placement: bool = False  # Placement present in diff or referenced


class Detector(ABC):
    kind: str
    pillar: str

    @abstractmethod
    def detect(self, diff: str, changed_files: list[str]) -> PRType | None:
        """Return PRType if this detector matches the PR, else None."""
