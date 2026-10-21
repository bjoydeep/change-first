# Relationship Checking — Design Notes

> Working doc. Captures thinking before implementation.

---

## The Problem

ACM Search returns an object's basic metadata when queried. It does NOT reliably
capture relationship information — ownerReferences are not indexed, managedFields
are not indexed, and data field content is not indexed.

Returning empty `_ownerReferences` from Search means "Search didn't index this
field", not "the object has no owner." Our current ownership check has a false
confidence problem.

---

## Three Distinct Questions

For any object a PR intends to modify, there are three fundamentally different
questions — each requires a different discovery mechanism:

| Question | What it means | How to answer |
|---|---|---|
| **Who created it?** | Provenance — GitOps, Helm, operator, user, policy | managedFields + labels/annotations |
| **Who can modify it?** | Authorization — RBAC principals with write access | RBAC resolution (out of scope for now) |
| **Who actively manages it?** | Reconciliation — who continuously enforces desired state | managedFields + ownerReferences + policy scan |

For pre-merge vetting, **question 3 is the most critical**. Two actors
simultaneously reconciling the same object = fight, flapping, instability.

---

## Design Principle: Raise Suspicion to Be Safe

Pre-merge vetting errs on the side of flagging potential issues.
A false positive causes a conversation. A false negative causes an outage.

Three outcome levels (not binary CONFLICT/NO CONFLICT):

| Level | Meaning |
|---|---|
| `SHARED_TARGET` | Another system has managed or declared this object |
| `POTENTIAL_CONFLICT` | Write scopes overlap — may or may not be incompatible |
| `CONFIRMED_CONFLICT` | Desired values are demonstrably incompatible |

Even `SHARED_TARGET` is worth surfacing. The PR author should know.

---

## Two Complementary Signals: `ownerReferences` and `managedFields`

These serve fundamentally different purposes and are NOT interchangeable:

| | `ownerReferences` | `managedFields` |
|---|---|---|
| **Purpose** | Object ownership + garbage collection | Field-level management tracking |
| **Answers** | Which Kubernetes object owns this? | Which field manager has written which fields? |
| **Set by** | Clients/controllers explicitly | API server automatically (Server-Side Apply) |
| **GC trigger** | Yes — owner deleted, object deleted | No |
| **Multiple entries** | Yes, but only one `controller: true` | Yes, multiple managers |

Both are needed as complementary signals. Neither alone is sufficient.

**`ownerReferences`** → "Who is the Kubernetes parent of this object?"
Many controllers (ArgoCD, ACM policy propagator) manage objects WITHOUT setting
ownerRefs — absence of ownerRef does NOT mean absence of a controller.

**`managedFields`** → "Which field managers have written to this object?"
Recorded by the API server via Server-Side Apply. Captures all writers that
used SSA. Manager names are client-supplied (not authenticated), but well-known
systems use consistent identifiable names.

**Critical nuance:** managedFields is SUPPORTING EVIDENCE, not proof of active
reconciliation. `endpoint-monitoring-operator` appearing in managedFields means
it has written to this object — not that it is continuously reconciling it.
It may have written once during initial setup and never again.

Use it to raise suspicion: "another system has touched this object" → SHARED_TARGET
Do NOT use it to assert: "this will cause a fight"

**Field-level granularity matters:** two managers on the same object are only
in conflict if they write to OVERLAPPING FIELDS. Extract `fieldsV1` from each
managedField entry and compare against what the PR proposes to change.
If ArgoCD manages `metadata.labels` and the policy manages `data.config.yaml`,
that is coexistence, not conflict.

```bash
kubectl get cm cluster-monitoring-config -n openshift-monitoring \
  -o json | jq '.metadata.managedFields | map({manager, operation, time})'
```

**The remaining gap:** Neither field answers "which controller is CURRENTLY
actively reconciling this object." Kubernetes has no universal API for that.
Combine ownerRefs + managedFields + policy scan + ManifestWork as complementary
signals — each raises suspicion, none alone is proof of conflict.

---

## What ACM Search Does and Does Not Give Us

**Does give us:**
- Object existence (is it deployed?)
- Basic metadata: kind, name, namespace, cluster, created timestamp
- Some labels (inconsistently indexed)

**Does NOT give us:**
- `ownerReferences` (not indexed)
- `managedFields` (not indexed)
- `data` field content (ConfigMap values not indexed)
- Relationship graph between resources (Search has its own ACM-level graph,
  not based on Kubernetes controller relationships)

**Conclusion:** Search answers "does this exist?" Relationship discovery requires
reading the object directly.

---

## Two Mechanisms for Reading Objects

### Mechanism 1: Hub k8s API (direct, in-cluster)

For objects on the hub cluster: standard kubernetes Python client, in-cluster
config. Already working. Authoritative and real-time.

**Use for:**
- Scanning existing `ConfigurationPolicy` resources for semantic conflicts
- Reading `ManifestWork` resources to see what's being delivered to clusters
- Any hub-resident resource

### Mechanism 2: cluster-proxy (for managed cluster objects)

The cluster-proxy addon proxies API calls from the hub to managed clusters.
This is the only way to read full object state (including `managedFields`
and data fields) from a managed cluster.

**Service:** TBD — check `oc get svc -n open-cluster-management-addon | grep proxy`

**URL pattern (to verify):**
```
https://{cluster-proxy-svc}/{cluster-name}/api/v1/namespaces/{ns}/configmaps/{name}
```

**Use for:**
- Reading `managedFields` on managed cluster objects
- Reading `ownerReferences` from the actual object (not Search)
- Reading ConfigMap `data` content (current state vs proposed)

---

## Deterministic Resolver Architecture

Each resolver answers one specific relationship question. They share a common
pattern: take a resource identity, return evidence-backed relationships.

```python
@dataclass
class Relationship:
    kind: str          # "MANAGES_FIELDS", "OWNS", "DECLARES", "RECONCILES"
    manager: str       # who
    target: str        # what (kind/name/namespace/cluster)
    evidence: str      # how we know (managedFields, ownerRef, policy spec, etc.)
    since: str         # timestamp if available

def resolve_managed_fields(cluster, namespace, kind, name) -> list[Relationship]: ...
def resolve_owner_references(cluster, namespace, kind, name) -> list[Relationship]: ...
def resolve_policy_conflict(kind, name, namespace) -> list[Relationship]: ...
def resolve_manifestwork(cluster, kind, name, namespace) -> list[Relationship]: ...
```

The pre-merge agent calls these resolvers for objects it identifies as targets
of the PR. Each returns relationships; the agent reasons about conflicts.

---

## Specific Checks to Implement

### Check 1: `managedFields` inspection (cluster-proxy)

**Question:** Who has ever written to this object? What fields do they control?

**Value:** Primary mechanism for discovering all active managers.
For `cluster-monitoring-config`: reveals if ArgoCD, CMO, another ACM policy,
or a human has been managing specific fields.

**How:** cluster-proxy GET of the full object → inspect `metadata.managedFields`

**Output example:**
```
managedFields on ConfigMap/cluster-monitoring-config (dsf-mc):
  - openshift-cluster-monitoring-operator → manages data.prometheus.yaml (2026-08-31)
  - argocd-application-controller → manages data.config.yaml (2026-09-15)
  [WARNING] ArgoCD also manages data.config.yaml — this policy will conflict with it
```

---

### Check 2a: Semantic conflict — ACM ConfigurationPolicy (Hub k8s API)

**Question:** Is another `ConfigurationPolicy` already targeting the same
object (same kind/name/namespace)?

**Value:** Catches the "two policies fighting" scenario before it happens.

**How:** List `ConfigurationPolicy` resources on hub → parse
`spec.object-templates[].objectDefinition` → match on kind/name/namespace

**Output example:**
```
CONFLICT: ConfigurationPolicy/enforce-monitoring-labels already targets
ConfigMap/cluster-monitoring-config in openshift-monitoring.
Two enforcing policies on the same object will flap.
```

---

### Check 2b: Semantic conflict — ArgoCD Application (Hub k8s API)

**Question:** Is an ArgoCD Application already managing the same object?

**Why it matters:** An ACM Policy enforcing an object that ArgoCD also syncs =
two controllers fighting. ArgoCD reverts what ACM sets (or vice versa),
causing continuous reconciliation loops.

**Where ArgoCD runs determines what we can see from the hub:**

| Scenario | Hub-side signal | Mechanism |
|---|---|---|
| ACM GitOps — ArgoCD on hub | `Application.status.resources` | Hub k8s API ✅ |
| ApplicationSet on hub | Generated `Application.status.resources` | Hub k8s API ✅ |
| Standalone ArgoCD per managed cluster | Object annotations + managedFields | cluster-proxy only |

**How (hub-side):**
List ArgoCD `Application` resources (typically `openshift-gitops` namespace,
but search all namespaces to be safe). For each Application, scan
`status.resources` for matching kind/name/namespace.

```python
apps = custom.list_cluster_custom_object(
    group="argoproj.io",
    version="v1alpha1",
    plural="applications",
)
for app in apps["items"]:
    for res in app.get("status", {}).get("resources", []):
        if res.get("kind") == kind and res.get("name") == name \
                and res.get("namespace") == namespace:
            # CONFLICT
```

**Output example:**
```
CONFLICT: ArgoCD Application/platform-monitoring (openshift-gitops) is already
managing ConfigMap/cluster-monitoring-config in openshift-monitoring
(status: Synced). ACM Policy enforcement will fight with ArgoCD sync.
```

**Limitation:** Only catches Applications that have been synced at least once
(status.resources is populated on first sync). Standalone ArgoCD on managed
clusters is invisible from hub — requires cluster-proxy.

---

### Check 3: ManifestWork inspection (Hub k8s API)

**Question:** Is there an active ManifestWork already delivering this object
to these clusters?

**Value:** Stronger signal than policy scan — ManifestWork exists only if
a policy is actively propagated. Confirms delivery is live, not just declared.

**How:** List `ManifestWork` in the cluster's namespace on hub → inspect
`spec.workload.manifests[]` → match kind/name/namespace

**Output example:**
```
ManifestWork/policy-enforce-monitoring-labels-dsf-mc is already delivering
ConfigMap/cluster-monitoring-config to dsf-mc. Another active policy controls
this object on this cluster.
```

---

### Check 4: Current object content (cluster-proxy)

**Question:** What is the current state of the object? What will be overwritten?

**Value:** Turns generic "may overwrite config" into specific "WILL delete your
remoteWrite config." Critical for `complianceType: musthave` on string fields.

**How:** cluster-proxy GET → inspect `data` fields → compare against PR diff

**Output example:**
```
Current config.yaml on dsf-mc contains remoteWrite config pointing to Thanos.
This policy will REPLACE the entire config.yaml value (musthave enforcement).
Your remoteWrite config will be deleted. Coordinate with observability team.
```

---

## Implementation Priority

| Check | Mechanism | Value | Status |
|---|---|---|---|
| ACM ConfigurationPolicy conflict (2a) | Hub k8s API | Highest | ✅ Done — pre-agent deterministic |
| ArgoCD Application conflict (2b) | Hub k8s API | Highest | ✅ Done — with destination cluster filter |
| ManifestWork inspection (3) | Hub k8s API | High | ✅ Done — pre-agent deterministic |
| `managedFields` + current content | cluster-proxy | High | ✅ Done — via fetch_object_from_cluster |
| Actual ownerReferences | cluster-proxy | Medium | ✅ Done — part of fetch_object_from_cluster |
| Field-level semantic comparison | Parse YAML-in-string | Highest | TODO — compare config.yaml keys |
| Desired-state conflict comparator | Parse PR diff + current state | Highest | TODO |

**cluster-proxy auth — IMPLEMENTED:**
`ManagedServiceAccount` CR creates `pr-analyzer` SA in `open-cluster-management-agent-addon`
on each managed cluster. Token synced back to hub as `Secret/pr-analyzer` in cluster namespace.
Hub-side `Role/RoleBinding` per cluster namespace grants `get` on the specific secret.
`deploy/managedserviceaccount.yaml` deploys this to dsf-mc, dsf-mc-02, subbarao-hub.

---

## Where This Code Lives

**`app/skills/acm_relationships.py`**

```python
# Pre-agent deterministic (called by runner.py before agent):
check_relationships_pre_agent(diff, clusters) -> str

# Internal resolvers (called by pre-agent and available as function_tools):
_resolve_configpolicy_conflict(kind, name, namespace) -> str
_resolve_argocd_conflict(kind, name, namespace, target_clusters) -> str
_resolve_manifestwork(cluster, kind, name, namespace) -> str
_fetch_object_from_cluster(cluster, namespace, kind, name) -> str  # cluster-proxy

# function_tools (agent can call for additional investigation):
resolve_configpolicy_conflict, resolve_argocd_conflict,
resolve_manifestwork, fetch_object_from_cluster
```

**Architecture:**
- `check_relationships_pre_agent` runs ALWAYS (deterministic, not LLM-discretionary)
- Extracts target objects from ConfigurationPolicy `object-templates` in the diff
- Runs all four checks for each target object × cluster
- Result passed to agent as pre-computed evidence in the user prompt
- Agent can call function_tools for ADDITIONAL investigation beyond pre-computed findings

---

## Key Architectural Insight

> The LLM should reason OVER a deterministically constructed relationship graph.
> Not discover the relationships itself.

The resolvers build the evidence. The agent correlates it with operational
context and explains the causal chain to the PR author.

**Essential safety checks run pre-agent (always).** The agent must not be able
to skip core relationship checks by not calling a tool. The following run
deterministically in runner.py regardless of LLM reasoning:
- ConfigurationPolicy conflict scan (hub k8s API)
- ArgoCD Application conflict scan (hub k8s API, with destination cluster filter)
- ManifestWork scan per cluster (hub k8s API)
- fetch_object_from_cluster per cluster (cluster-proxy, when object exists)

The agent can call tools for ADDITIONAL investigation, but cannot omit the above.

**ManifestWork caveat:** ManifestWork delivers policy framework artifacts, not
necessarily the final target object. The chain is:
  Hub Policy → ConfigurationPolicy → governance controller → target object
ManifestWork may deliver the ConfigurationPolicy itself, not the ConfigMap.
Finding a ManifestWork with the target ConfigMap is strong evidence of active
delivery; NOT finding one does not mean no policy is enforcing it.

**ArgoCD destination cluster:** When checking ArgoCD Applications, match the
destination cluster against the PR's targeted clusters (from blast radius).
An Application managing the same ConfigMap on a different cluster is NOT a
conflict for this PR. Filter by `spec.destination.name` or `spec.destination.server`.

---

## Resolved Questions

**cluster-proxy** ✅
```
Service:  cluster-proxy-addon-user.multicluster-engine.svc.cluster.local:9092
URL:      /{cluster}/{api-path}
TLS:      verify=False — TODO: use proper CA bundle for production
```

**Auth** ✅ — ManagedServiceAccount (not hub-impersonation)
`deploy/managedserviceaccount.yaml`:
- Creates `ManagedServiceAccount/pr-analyzer` in each cluster namespace on hub
- SA created in `open-cluster-management-agent-addon` on each managed cluster
- Token synced as `Secret/pr-analyzer` in the cluster namespace on hub
- `ManifestWork` deploys `ClusterRoleBinding` (SA → `view`) on each spoke
- Hub-side `Role/RoleBinding` per cluster namespace grants `get` on the secret

**ManifestWorks namespace** ✅ — in cluster's namespace on hub (`-n dsf-mc`)

**RBAC for ManifestWork listing** ✅ — in `deploy/rbac.yaml`:
`work.open-cluster-management.io/manifestworks: get, list`

**Does CMO appear in managedFields?** ✅ CONFIRMED NO.
`fetch_object_from_cluster` revealed `manager=endpoint-monitoring-operator`
(ACM observability endpoint operator) — NOT CMO. CMO reads cluster-monitoring-config
but writes to Prometheus/Alertmanager StatefulSets, not back to the ConfigMap.

## Remaining TODOs

- [ ] **Field-level semantic comparison** — parse YAML-in-string (e.g. `data.config.yaml`)
      to show which keys will be added/removed, not just "field will be replaced"
- [ ] **Desired-state conflict comparator** — compare PR's proposed values vs current values
      to escalate POTENTIAL_CONFLICT → CONFIRMED_CONFLICT
- [ ] **Production TLS** — replace `verify=False` with cluster-proxy CA bundle
- [ ] **Narrower cluster-proxy RBAC** — `view` is broad; scope to specific resource kinds
- [ ] **Standalone ArgoCD** — requires cluster-proxy to check annotations on managed objects
