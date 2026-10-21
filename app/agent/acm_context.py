"""
ACM delivery mechanism knowledge — distilled from agentic-ols/skills SKILL.md files.
Injected into the agent system prompt based on the detected PR pillar.
Not a tool. Not read from disk at runtime.
"""

GOVERNANCE = """
## ACM Governance Delivery Mechanism

**Propagation path:**
Policy → governance-propagator → ManifestWork (one per target cluster) → work-agent on managed cluster applies it

**Key resources:**
- `Policy` (policy.open-cluster-management.io/v1) — wraps one or more policy templates
- `ConfigPolicy` — enforces Kubernetes resource state; can Inform (audit) or Enforce (mutate)
- `CertPolicy`, `IamPolicy`, `OperatorPolicy` — specialised enforcement controllers
- `PlacementBinding` — binds a Policy to a Placement; without this binding the policy goes nowhere
- `Placement` (cluster.open-cluster-management.io/v1beta1) — selects target clusters via label selectors
- `PlacementDecision` — written by ACM placement controller; lists the clusters that matched

**Blast radius mechanics:**
- Enforcement happens simultaneously on ALL clusters matched by the Placement
- `remediationAction: enforce` on a ConfigPolicy will CREATE, UPDATE, or DELETE resources on managed clusters
- A ConfigPolicy with `pruneObjectBehavior: DeleteAll` will delete objects if the policy is removed
- Policy violations surface in the ACM governance dashboard and may trigger alerts

**Common disruption patterns:**
- Enforcing a ConfigPolicy that modifies a core resource (etcd config, monitoring config, MachineConfig) causes
  immediate reconciliation on all target clusters — no staged rollout unless explicitly gated by Placement scope
- Two policies enforcing conflicting state on the same object causes a fight — last-write-wins flapping
- Removing a PlacementBinding does not remove already-applied resources unless pruneObjectBehavior is set

**Cross-pillar cascade:**
- governance-propagator slow → stale policies on managed clusters → compliance status misleading
- work-agent disrupted on a managed cluster → ManifestWork stuck → policy enforcement silently failing
"""

APP_LIFECYCLE = """
## ACM App Lifecycle / ArgoCD Delivery Mechanism

**ArgoCD Application delivery path:**
ApplicationSet → templated Applications (one per target cluster) → ArgoCD sync → resources applied on cluster

**Key resources:**
- `Application` (argoproj.io/v1alpha1) — syncs a single Git source to a single destination cluster
- `ApplicationSet` (argoproj.io/v1alpha1) — generates Applications across multiple clusters via generators

**Cluster targeting (ApplicationSet generators):**
- `clusterDecisionResource` generator — reads ACM `PlacementDecision` CRs; integrates with ACM Placement
- `clusters` generator — uses label selectors on ArgoCD cluster secrets (independent of ACM Placement)
- Scope of a change = all clusters targeted by the generator at sync time

**Sync behaviour:**
- `argocd.argoproj.io/sync-wave` annotation controls ordering within a sync operation
- Sync policy `automated` with `prune: true` will DELETE resources removed from Git — high blast radius
- `selfHeal: true` means ArgoCD will revert any manual cluster changes — conflicts with other controllers

**Controller ownership — critical:**
- ArgoCD marks managed resources with label `app.kubernetes.io/instance` and annotation
  `argocd.argoproj.io/managed-by`
- If another controller (ACM Policy, Helm, an operator) also owns the same resource, the last writer wins
- Result: continuous flapping, sync failures, or silent resource drift

**Cross-pillar cascade:**
- ArgoCD sync failure → partial apply → cluster in indeterminate state
- ApplicationSet generator reading a stale PlacementDecision → targeting stale cluster list
"""

_PILLAR_MAP = {
    "governance": GOVERNANCE,
    "app-lifecycle": APP_LIFECYCLE,
}


def get_context_for_pillars(pillars: list[str]) -> str:
    """Return combined ACM delivery context for the detected pillars."""
    sections = [_PILLAR_MAP[p] for p in pillars if p in _PILLAR_MAP]
    if not sections:
        return ""
    return "\n---\n".join(sections)
