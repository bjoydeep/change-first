# Change-First: Pre-Merge Agentic Vetting — Design

> Working doc. Edit freely. This is not a README.

---

## What This System Does

A GitHub webhook handler that intercepts PRs **before merge** and runs an AI agent to analyze the blast radius and risk of platform configuration changes on ACM-managed clusters.

The PR author is a **platform engineer** pushing config via GitOps — not an ACM developer. They're asking: *"What will this change do to my clusters if I merge it?"*

---

## Core Insight: Two Domains of Knowledge

| Domain | What It Answers | Source |
|---|---|---|
| **ACM delivery mechanism** | How does this config get to clusters? What can go wrong in transit? | Hardcoded distillation from `agentic-ols/skills` SKILL.md files |
| **Payload risk** | What does this config actually DO when it lands on a cluster? | LLM general knowledge — model already knows what MachineConfig, cluster-monitoring-config, etcd encryption etc. do |

The LLM IS the platform knowledge base. We don't need to encode "MachineConfig causes node drain" anywhere — we just need good system prompt instructions to surface it. Tools exist only for what the model cannot know without querying live systems.

---

## Two Analysis Tiers

### Tier 1 — Static (always runs, no hub access needed)

- Generic PR hygiene (description, diff size, draft state)
- PR type classification — what kind of config change is this?
- LLM payload risk assessment — what does this config do when it lands?
- ACM delivery context — how does this type of change propagate?
- Placement presence detection — is blast radius resolvable?

Output: risk classification, payload warnings from LLM knowledge, delivery mechanism notes.

### Tier 2 — Live cluster (runs when hub creds configured + Placement resolvable)

- **Placement resolution** → which clusters are actually in scope
- **Object state fetch** → current state of the target object on affected clusters (strengthens LLM warnings with specifics)
- **Controller conflict detection** → is this object already managed by another controller?

Output: concrete cluster list, specific "remote-write is configured, this WILL affect metrics flow" type findings, hard conflict flags.

**Placement is the Tier 2 gate.** Without a `Placement` resolvable from the PR diff or the live hub, blast radius scope is unknown. Tier 1 still runs; Tier 2 simply cannot resolve scope. The agent surfaces this explicitly.

---

## PR Types: The Classifier

PRs are classified before analysis. Classification drives: what ACM context gets injected, what tools the agent gets, what it's specifically instructed to look for.

### Extensibility Pattern

Registry of detectors. Each PR type is a self-contained detector module. Adding Day N type = add one file + one line in `_DETECTORS`. Nothing else changes.

### Day 1 Types

| Type | Key Resources | Delivery Mechanism | Placement |
|---|---|---|---|
| `governance-policy` | `Policy`, `ConfigPolicy`, `CertPolicy` | ACM governance-framework → propagator → ManifestWork → managed clusters | `PlacementBinding` + `Placement` |
| `argocd-appset` | `ApplicationSet`, `Application` | ArgoCD GitOps addon → sync → managed clusters | Cluster generators / label selectors in the spec |

### Day 2+ (not designed yet, just named)

- `acm-subscription` — ACM Subscription delivering apps via app-manager
- `machineconfig` — MachineConfig causing node drain/reboot
- `olm-subscription` — OLM operator installation/upgrade
- `placement-change` — Placement scope change (blast radius IS the change)
- `kustomize-overlay` — Kustomize overlays targeting specific clusters
- `helm-values` — Helm values files changing deployed config

---

## File Structure

```
app/
├── config.py                        # TIER2_ENABLED, TIER2_LOG_LEVEL settings
├── security.py                      # webhook HMAC verification
├── main.py                          # FastAPI app entry point
├── webhook/                         # webhook router + event models
├── github/                          # GitHubClient (fetch diff, post comment)
│
├── agent/
│   ├── runner.py                    # orchestrates pre-agent pipeline + agent run
│   ├── prompt.py                    # system + user prompt builders
│   └── acm_context.py               # hardcoded ACM delivery mechanism knowledge
│
└── skills/
    ├── __init__.py                  # skill registry (base + tier2 + relationship)
    ├── pr_basics.py                 # generic PR hygiene (description, size, draft)
    ├── schema_validator.py          # OpenAPI v3 schema validation (hub API, pre-agent)
    ├── acm_placement.py             # Placement parser (YAML-aware, matchExpressions)
    │
    ├── acm_cluster.py               # cluster tools:
    │                                #   get_cluster_names()  — raw list for runner
    │                                #   resolve_clusters()   — formatted blast radius string
    │                                #   check_object_ownership() — Search-based
    │                                #   fetch_object_on_cluster() — Search-based
    │
    ├── acm_relationships.py         # relationship resolvers:
    │                                #   check_relationships_pre_agent() — ALWAYS runs
    │                                #   _resolve_configpolicy_conflict()
    │                                #   _resolve_argocd_conflict() — with dest cluster filter
    │                                #   _resolve_manifestwork()
    │                                #   _fetch_object_from_cluster() — cluster-proxy
    │
    └── classifier/
        ├── __init__.py              # classify() + _DETECTORS registry
        ├── base.py                  # PRType dataclass + Detector ABC
        ├── governance_policy.py     # Day 1: Policy, ConfigPolicy, CertPolicy
        └── argocd_appset.py         # Day 1: ApplicationSet, Application

deploy/
├── deployment.yaml                  # pr-analyzer Deployment (serviceAccountName: pr-analyzer)
├── service.yaml                     # Service
├── route.yaml                       # OCP Route
├── configmap.yaml                   # TIER2_ENABLED, TIER2_LOG_LEVEL, MODEL_NAME etc.
├── secret.template                  # GITHUB_TOKEN, OPENAI_API_KEY template
├── rbac.yaml                        # ClusterRole + bindings (hub-side RBAC)
│                                    # Includes: managedclusters, placementdecisions,
│                                    # manifestworks, configurationpolicies, applications
│                                    # + Role/RoleBinding per cluster ns for MSA token
└── managedserviceaccount.yaml       # ManagedServiceAccount + ManifestWork RBAC
                                     # per managed cluster (cluster-proxy auth)
```

---

## Component Details

### `app/agent/acm_context.py`

Hardcoded string constants distilled from `agentic-ols/skills` SKILL.md files.  
**Not** a runtime file read. **Not** a tool. Injected into the system prompt before the agent runs.

Content covers delivery mechanism knowledge only:
- How `ConfigPolicy` propagates: governance-framework → propagator → ManifestWork → managed cluster
- How `Subscription` delivers: placement resolution → app-manager on managed cluster
- How ArgoCD GitOps addon syncs: sync waves, phases, what controls ordering
- Cross-pillar impact chains from `acm-architecture` (e.g. governance propagator delay → stale policies fleet-wide)
- Placement resolution mechanics: cluster selectors, label matchers, ClusterSets

Organized by pillar so `prompt.py` can inject only the relevant sections based on classifier output.

Goes stale as agentic-ols evolves — acceptable tradeoff for a starter. Revisit when SKILL.md files reach `proven` maturity across more pillars.

### `app/skills/classifier/`

**`base.py`**
```python
@dataclass
class PRType:
    kind: str           # "governance-policy", "argocd-appset", ...
    pillar: str         # "governance", "app-lifecycle", ...
    resources: list     # ["Policy", "ConfigPolicy"] — what was found in diff
    has_placement: bool # whether Placement is present
```

**`__init__.py`**
```python
_DETECTORS = [
    GovernancePolicyDetector(),
    ArgocdAppSetDetector(),
    # Day 2: append here only
]

def classify(diff: str, changed_files: list[str]) -> list[PRType]:
    return [r for d in _DETECTORS if (r := d.detect(diff, changed_files))]
```

### `app/skills/acm_placement.py`

Parses the PR diff for Placement/PlacementBinding resources.  
Returns structured placement spec if found, or a `PlacementAbsent` signal.  
This is the gate between Tier 1 and Tier 2 — no external calls, pure parsing logic.

### `app/skills/acm_cluster.py`

Tier 2 tools. Use in-cluster credentials automatically — no connection config needed. If `TIER2_ENABLED=false`, these tools are not registered and the agent runs Tier 1 only — graceful degradation, no crash.

**`resolve_placement(placement_name, namespace, placement_spec_from_diff)`**  
Three cases — behaviour differs:

- **Case A — new Placement in diff**: no `PlacementDecision` exists yet. Evaluate the `clusterSelector`/`matchLabels` from the diff spec against live `ManagedCluster` objects on the hub. Return matches.
- **Case B — existing Placement modified in diff**: `PlacementDecision` exists but reflects the old spec. Same as Case A — evaluate the new spec from the diff against live `ManagedClusters`.
- **Case C — PlacementBinding references an existing Placement not changed in the diff**: `PlacementDecision` exists and is accurate. Read it directly.

For Case A/B, the tool lists `ManagedClusters` and applies the selector locally. Complex `predicates` (taints, tolerations, prioritizers) are out of scope for now — document the limitation, handle `matchLabels`/`matchExpressions` only.

**`fetch_object_on_cluster(cluster, namespace, kind, name)`**  
Fetches the current state of a target object on a managed cluster via the ACM Search API (indexed on the hub — no direct spoke connection needed).  
Used to strengthen LLM warnings: "does remote-write exist in current cluster-monitoring-config?"

**`check_object_ownership(cluster, namespace, kind, name)`**  
Checks `ownerReferences` and managing annotations on a target object:
- `argocd.argoproj.io/managed-by` → ArgoCD conflict
- ACM Policy ownerRef → another Policy conflict
- `app.kubernetes.io/managed-by: Helm` → Helm conflict  

Returns conflict list. Hard finding, not a soft warning.

---

## Agent Flow

```
GitHub webhook → PRContext
        │
        ▼
classify(diff)                         → pr_types (governance-policy, argocd-appset, ...)
        │
        ▼ [if TIER2_ENABLED]
parse_placement(diff)                  → PlacementResult (case, selector)
get_cluster_names(placement_result)    → ["dsf-mc", "dsf-mc-02"]
resolve_clusters(placement_result)     → "Blast radius: 2 clusters — dsf-mc, dsf-mc-02"
        │
        ▼
validate_schemas(diff)                 → schema findings (always runs, hub OpenAPI)
        │
        ▼ [if TIER2_ENABLED + clusters resolved]
check_relationships_pre_agent(         → relationship findings (ALWAYS runs, not LLM-discretionary)
  diff, clusters                         - configpolicy conflict (hub k8s API)
)                                        - argocd conflict (hub k8s API, dest cluster filter)
                                         - manifestwork per cluster (hub k8s API)
                                         - fetch_object_from_cluster per cluster (cluster-proxy)
        │
        ▼
build_system_prompt(pr_types, tier2)   → instructions + ACM delivery context
build_user_prompt(ctx, pr_types,       → PR metadata + blast_radius + schema_report
  blast_radius, schema_report,           + relationship_report + diff
  relationship_report)
        │
        ▼
skills = pr_basics + acm_cluster tools + acm_relationship tools (for agent deep-dive)
        │
        ▼
Agent.run(prompt, tools=skills)        → agent reasons over pre-computed evidence,
                                         may call tools for additional investigation
        │
        ▼
post PR comment
```

---

## Config Changes Needed

This app runs as a pod **on the ACM hub cluster itself**. No hub connection config needed — the Kubernetes client uses in-cluster credentials automatically (service account token + CA cert mounted by the runtime).

Add to `app/config.py` and `deploy/configmap.yaml`:

```
# Tier 2 behaviour
TIER2_ENABLED    # "true" to enable live cluster tools (default: true — we're on the hub)
```

What IS needed: a `ServiceAccount` in the deployment with RBAC on the hub:

```yaml
# read Placement decisions (already computed by ACM placement controller)
- apiGroups: ["cluster.open-cluster-management.io"]
  resources: ["placementdecisions"]
  verbs: ["get", "list"]

# read ManagedCluster metadata
- apiGroups: ["cluster.open-cluster-management.io"]
  resources: ["managedclusters"]
  verbs: ["get", "list"]

# query objects on managed clusters via ACM Search API
- apiGroups: ["search.open-cluster-management.io"]
  resources: ["searchqueries"]
  verbs: ["get", "create"]
```

`TIER2_ENABLED=false` → acm_cluster tools not registered → agent runs Tier 1 only.  
No config, no crash, no silent failure.

---

## What the Agent Says

### Tier 1 output (no Placement resolved)

```
## PR Analysis

**Type:** ACM Governance Policy — ConfigPolicy
**Pillar:** Governance
**Placement:** Not found in diff — blast radius scope unknown

⚠️ **Payload risk:** This policy modifies `cluster-monitoring-config`. 
   Changes here restart the in-cluster Prometheus stack. Verify with your 
   monitoring admin. If remote-write is configured, metrics flow to your 
   central observability stack may be interrupted during rollout.

⚠️ **No Placement detected.** Cannot determine which clusters this policy 
   targets. Add a PlacementBinding + Placement to scope this change, or 
   confirm scope via your GitOps repo structure.

✅ PR has a description.
✅ Manageable diff size (43 lines, 2 files).

**Overall:** Medium risk — payload is disruptive but scope is unresolved. 
Clarify targeting before merge.
```

### Tier 2 output (Placement resolved, hub connected)

```
## PR Analysis

**Type:** ACM Governance Policy — ConfigPolicy  
**Pillar:** Governance
**Blast radius:** 12 clusters (production-us-east ClusterSet)

❌ **Controller conflict:** `cluster-monitoring-config` on cluster 
   `prod-east-01` is managed by ArgoCD (app: platform-monitoring). 
   Two controllers will fight over this object. Resolve ownership first.

⚠️ **Payload risk:** remote-write to Thanos is configured on 9 of 12 
   affected clusters. Prometheus restart will cause a metrics gap. 
   Coordinate with observability team.

✅ PR has a description.
✅ Manageable diff size.

**Overall:** High risk — controller conflict is a hard blocker. 
Resolve ArgoCD vs ACM ownership before merge.
```

---

## Open Questions

- [x] What are the exact Day 1 types? **Confirmed: `governance-policy` and `argocd-appset`**
- [x] Hub access: **in-cluster ServiceAccount, no external creds needed. RBAC: list ManagedClusters + PlacementDecisions + Search API**
- [ ] For ArgoCD AppSet Tier 2: cluster generators use label selectors, not Placement — how do we resolve those to cluster names?
- [ ] When a PR has NO recognized type (plain YAML, scripts, docs) — Tier 1 generic only, no ACM context injected?
- [ ] Multi-type PRs (Policy + ApplicationSet in same PR) — run both analysis paths, merge findings?

## Lessons Learned (from live testing)

**PR #3 (ImageDigestMirrorSet) and PR #4 (cluster-monitoring-config):**

- **YAML parser must split diff by file first** — non-YAML files (`.gitignore`) use `!` which breaks `yaml.safe_load_all`. Split by file, parse only `.yaml`/`.yml`.
- **matchExpressions required** — real Placements use `operator: In/NotIn/Exists/DoesNotExist`. Convert to k8s label selector string `env in (production,staging)`.
- **0 clusters matched is useful signal** — LLM said "✅ Limited scope" (wrong). System says "0 clusters matched — verify labels" (actionable).
- **apiVersion wrong = hard error, not schema note** — Placement at v1beta2 doesn't exist on this hub (only v1beta1). Catch via discovery API, not just schema lookup.
- **ACM Search doesn't index managedFields** — `_ownerReferences` empty from Search ≠ no owner. cluster-proxy fetch of real object revealed `endpoint-monitoring-operator` manages `cluster-monitoring-config`. This is the critical finding Search cannot make.
- **resolve_placement as agent function_tool breaks matchExpressions** — agent passed Python list string, got 400 from k8s API. Removed from ALL_TIER2_SKILLS; blast radius is pre-computed deterministically.
- **ManagedServiceAccount is the right cluster-proxy auth** — not hub SA impersonation. SA created on managed cluster, token synced back to hub, clean lifecycle management by ACM.
- **Essential checks must be pre-agent** — agent may skip tools based on LLM reasoning. `check_relationships_pre_agent()` runs all checks before agent sees the PR regardless of LLM decisions.

---

## Code Flow

```
GitHub Webhook
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  POST /webhook   (router.py)                        │
│                                                     │
│  verify_signature()          → 401 if invalid       │
│  PullRequestEvent.validate() → parse payload        │
│  background_tasks.add_task() → return 202 immediately
└──────────────────┬──────────────────────────────────┘
                   │ (background)
                   ▼
┌─────────────────────────────────────────────────────┐
│  _process_pr()   (router.py)                        │
│                                                     │
│  GitHubClient.fetch_diff()   → raw unified diff     │
│  PRContext()                 → structured PR data   │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────┐
│  analyze_pr()    (runner.py)              ◄── CORE  │
│                                                     │
│  1. classify(diff)                                  │
│     ├── GovernancePolicyDetector.detect()           │
│     │     _KIND_RE.findall(diff)                    │
│     └── ArgocdAppSetDetector.detect()               │
│           _KIND_RE + _CLUSTER_GENERATOR_RE          │
│     → list[PRType]                                  │
│                                                     │
│  2. if TIER2_ENABLED:                               │
│     parse_placement(diff)                           │
│       _split_diff_by_file()  → per-file chunks      │
│       _clean_chunk()         → strip diff markers   │
│       yaml.safe_load_all()   → parsed YAML docs     │
│       _extract_selector()    → matchLabels          │
│                                 matchExpressions    │
│     → PlacementResult(case, name, ns, selector)     │
│                                                     │
│     if case != ABSENT:                              │
│       resolve_clusters()       ◄── DETERMINISTIC    │
│         EVALUATE case:                              │
│           _selector_to_k8s_string()                 │
│           _evaluate_placement_selector()            │
│             k8s.list_cluster_custom_object()        │
│             → ["dsf-mc", "dsf-mc-02"]               │
│         READ_DECISION case:                         │
│           _check_placement_decision()               │
│             k8s.list_namespaced_custom_object()     │
│             (reads PlacementDecision CRs)           │
│     → blast_radius string                           │
│                                                     │
│  3. build_system_prompt(pr_types, tier2_enabled)    │
│       get_context_for_pillars()                     │
│         _PILLAR_MAP[pillar] → GOVERNANCE or         │
│                               APP_LIFECYCLE text    │
│       + TIER2 or TIER1_ONLY instructions            │
│                                                     │
│  4. build_user_prompt(ctx, pr_types, blast_radius)  │
│       → PR metadata + pre-computed blast radius     │
│         + diff (first 8000 chars)                   │
│                                                     │
│  5. get_enabled_skills(tier2=True/False)            │
│       Base: check_pr_description                    │
│             check_diff_size                         │
│             check_draft_targeting_protected_branch  │
│       Tier2: resolve_placement (function_tool)      │
│              check_object_ownership (function_tool) │
│              fetch_object_on_cluster (function_tool)│
│                                                     │
│  6. Runner.run(agent, prompt, tools)  ◄── LLM LOOP  │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────┐
│  Agent tool calls (agent-driven, non-deterministic) │
│                                                     │
│  check_pr_description(body, title)                  │
│  check_diff_size(additions, deletions, files)       │
│  check_draft_targeting_protected_branch(draft, base)│
│                                                     │
│  [if Tier 2 tools registered]:                      │
│  check_object_ownership(cluster, ns, kind, name)    │
│    → ACM Search API query                           │
│    → ownerRefs + annotations check                  │
│    → CONFLICT or OK                                 │
│                                                     │
│  fetch_object_on_cluster(cluster, ns, kind, name)   │
│    → ACM Search API query                           │
│    → current object state JSON                      │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────┐
│  GitHubClient.post_comment()                        │
│  → PR comment posted                                │
└─────────────────────────────────────────────────────┘
```

**Key architectural split:**
- Steps 1–4 are **deterministic** — same input always gives same output, no LLM involved
- Step 5 hands the pre-computed facts (blast radius, type, ACM delivery context) to the LLM
- Step 6 is where the LLM reasons and decides which tools to call
- Tier 2 `function_tools` are **agent-driven** (LLM decides when/what to call), while `resolve_clusters()` is **pre-agent** (always runs if Placement is present)

---

## Not In Scope (yet)

- **Post-deploy anomaly correlation** → Phase 2, separate repo: `~/code/change-sentinel`
  - Detects changes that land on the fleet via ACM, infers blast radius, monitors
    for anomalies over a defined window, and warns before outages develop.
  - Design: `~/code/change-sentinel/design.md`
- GitHub App identity (v2) — current PAT approach is fine for now
- Interactive `@bot` flows — out of scope for this phase
- Remediating findings — analysis only, no auto-fix
