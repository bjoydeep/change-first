"""
Tier 2 live cluster tools — runs against the ACM hub using in-cluster credentials.
Only registered when TIER2_ENABLED=true.

resolve_clusters         — plain function called deterministically by runner (blast radius)
_search_resources()      — ACM Search GraphQL helper (primary data source)
_fetch_object_on_cluster — fetch current object state via Search
_check_object_ownership  — detect controller conflicts via Search ownerRefs + annotations

Fallback strategy (TODO):
  If Search returns empty:
    - hub object  → direct k8s API call (in-cluster config)
    - managed obj → cluster-proxy addon HTTP call
"""

from __future__ import annotations

import json
import logging

import httpx
from agents import function_tool

from app.skills.acm_placement import PlacementCase, PlacementResult

# Module-level logger — level controlled by TIER2_LOG_LEVEL config
log = logging.getLogger(__name__)

# ACM Search GraphQL endpoint (internal service, no TLS verification needed)
_SEARCH_URL = (
    "https://search-search-api.open-cluster-management.svc.cluster.local:4010"
    "/searchapi/graphql"
)
_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

_log_configured = False


def _ensure_log_level() -> None:
    """Apply TIER2_LOG_LEVEL to this module's logger on first call."""
    global _log_configured
    if _log_configured:
        return
    try:
        from app.config import get_settings
        level = get_settings().tier2_log_level.upper()
        log.setLevel(level)
        log.debug("acm_cluster log level set to %s", level)
    except Exception:
        pass
    _log_configured = True


# ---------------------------------------------------------------------------
# Kubernetes client (for Placement resolution — not Search)
# ---------------------------------------------------------------------------

def _k8s_client():
    from kubernetes import client, config  # type: ignore
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client


# ---------------------------------------------------------------------------
# ACM Search GraphQL helper
# ---------------------------------------------------------------------------

def _read_token() -> str:
    with open(_SA_TOKEN_PATH) as f:
        return f.read().strip()


def _search_resources(filters: list[dict], caller: str = "") -> list[dict]:
    """
    Query ACM Search GraphQL API and return matching resource items.

    Each item is a flat dict containing resource fields, plus:
      _ownerReferences  — JSON string of ownerRef list
      _annotations      — list of "key=value" strings

    Search enforces the requesting SA's RBAC — returns only resources
    the SA has permission to read.

    Args:
        filters: list of {"property": str, "values": [str]} dicts
        caller:  short label for log messages (e.g. "check_ownership")
    """
    _ensure_log_level()
    prefix = f"[Search/{caller}]" if caller else "[Search]"

    try:
        token = _read_token()
    except Exception as e:
        log.warning("%s Cannot read SA token: %s", prefix, e)
        return []

    gql = {
        "query": """
        query Search($input: [SearchInput]) {
          searchResult: search(input: $input) {
            items
            count
          }
        }
        """,
        "variables": {
            "input": [{"filters": filters, "limit": 20}]
        },
    }

    log.debug("%s GraphQL request to %s", prefix, _SEARCH_URL)
    log.debug("%s filters: %s", prefix, json.dumps(filters))

    try:
        resp = httpx.post(
            _SEARCH_URL,
            json=gql,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            verify=False,   # internal cluster service, self-signed cert
            timeout=10,
        )
        log.debug("%s HTTP status: %s", prefix, resp.status_code)
        resp.raise_for_status()

        body = resp.json()
        log.debug("%s raw response: %s", prefix, json.dumps(body))

        errors = body.get("errors")
        if errors:
            log.warning("%s GraphQL errors: %s", prefix, errors)

        results = body.get("data", {}).get("searchResult", [])
        if not results:
            log.debug("%s searchResult list empty", prefix)
            return []

        items = results[0].get("items") or []
        count = results[0].get("count", 0)
        log.debug("%s returned %d item(s) (Search count=%s)", prefix, len(items), count)

        for i, item in enumerate(items):
            log.debug("%s item[%d]: %s", prefix, i, json.dumps(item))

        return items

    except httpx.HTTPStatusError as e:
        log.warning("%s HTTP error %s: %s", prefix, e.response.status_code, e)
        return []
    except Exception as e:
        log.warning("%s Search query failed: %s", prefix, e)
        return []


def _parse_annotations(raw: list | None) -> dict[str, str]:
    """Parse Search _annotations list ['key=value', ...] into a dict."""
    if not raw:
        return {}
    result = {}
    for entry in raw:
        if "=" in entry:
            k, _, v = entry.partition("=")
            result[k] = v
    return result


def _parse_owner_refs(raw: str | list | None) -> list[dict]:
    """Parse Search _ownerReferences (JSON string or list) into a list of dicts."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Placement resolution (deterministic, called by runner.py pre-agent)
# ---------------------------------------------------------------------------

def _selector_to_k8s_string(match_labels: dict, match_expressions: list) -> str:
    parts = [f"{k}={v}" for k, v in match_labels.items()]
    for expr in match_expressions:
        key = expr.get("key", "")
        op = expr.get("operator", "")
        values = expr.get("values", [])
        if op == "In":
            parts.append(f"{key} in ({','.join(values)})")
        elif op == "NotIn":
            parts.append(f"{key} notin ({','.join(values)})")
        elif op == "Exists":
            parts.append(key)
        elif op == "DoesNotExist":
            parts.append(f"!{key}")
    return ",".join(parts)


def _check_placement_decision(placement_name: str, namespace: str) -> list[str]:
    """Read PlacementDecision CRs for an existing Placement (Case C)."""
    _ensure_log_level()
    log.debug("[PlacementDecision] reading decisions for placement=%s ns=%s",
              placement_name, namespace)
    k8s = _k8s_client()
    custom = k8s.CustomObjectsApi()
    decisions = custom.list_namespaced_custom_object(
        group="cluster.open-cluster-management.io",
        version="v1beta1",
        namespace=namespace,
        plural="placementdecisions",
        label_selector=f"cluster.open-cluster-management.io/placement={placement_name}",
    )
    clusters = []
    for item in decisions.get("items", []):
        for decision in item.get("status", {}).get("decisions", []):
            name = decision.get("clusterName")
            if name:
                clusters.append(name)
    log.debug("[PlacementDecision] resolved clusters: %s", clusters)
    return clusters


def _evaluate_placement_selector(match_labels: dict, match_expressions: list) -> list[str]:
    """Evaluate matchLabels + matchExpressions against live ManagedClusters (Cases A/B)."""
    _ensure_log_level()
    selector = _selector_to_k8s_string(match_labels, match_expressions)
    log.debug("[PlacementEval] k8s selector string: %r", selector)
    if not selector:
        return []
    k8s = _k8s_client()
    custom = k8s.CustomObjectsApi()
    result = custom.list_cluster_custom_object(
        group="cluster.open-cluster-management.io",
        version="v1",
        plural="managedclusters",
        label_selector=selector,
    )
    clusters = [item["metadata"]["name"] for item in result.get("items", [])]
    log.debug("[PlacementEval] matched clusters: %s", clusters)
    return clusters


def get_cluster_names(placement_result: PlacementResult) -> list[str]:
    """
    Return the raw list of targeted cluster names — used by runner.py to pass
    clusters into pre-agent relationship checks.
    """
    _ensure_log_level()
    if placement_result.case == PlacementCase.ABSENT:
        return []
    try:
        if placement_result.case == PlacementCase.EVALUATE:
            return _evaluate_placement_selector(
                placement_result.match_labels,
                placement_result.match_expressions,
            )
        return _check_placement_decision(
            placement_result.placement_name,
            placement_result.placement_namespace,
        )
    except Exception as e:
        log.warning("[get_cluster_names] failed: %s", e)
        return []


def resolve_clusters(placement_result: PlacementResult) -> str:
    """
    Deterministic blast radius resolution — called by runner before the agent runs.
    Returns a human-readable string summarising which clusters are in scope.
    """
    _ensure_log_level()
    log.debug("[resolve_clusters] case=%s name=%s ns=%s",
              placement_result.case, placement_result.placement_name,
              placement_result.placement_namespace)

    if placement_result.case == PlacementCase.ABSENT:
        return "No Placement found in diff — blast radius scope unknown."

    try:
        if placement_result.case == PlacementCase.EVALUATE:
            selector = _selector_to_k8s_string(
                placement_result.match_labels,
                placement_result.match_expressions,
            )
            if not selector:
                return (
                    f"Placement '{placement_result.placement_name}' found in diff but "
                    "no label selector could be extracted (may use unsupported predicates "
                    "such as taints/tolerations). Blast radius scope unknown."
                )
            clusters = _evaluate_placement_selector(
                placement_result.match_labels,
                placement_result.match_expressions,
            )
            scope_desc = f"selector `{selector}`"
        else:
            clusters = _check_placement_decision(
                placement_result.placement_name,
                placement_result.placement_namespace,
            )
            scope_desc = f"PlacementDecision for `{placement_result.placement_name}`"

        if not clusters:
            return (
                f"**0 clusters matched** ({scope_desc}). "
                "No managed clusters currently satisfy this Placement. "
                "Verify cluster labels are correct before merging."
            )

        cluster_list = ", ".join(f"`{c}`" for c in sorted(clusters))
        return f"**{len(clusters)} cluster(s)** ({scope_desc}) — {cluster_list}"

    except Exception as e:
        log.warning("[resolve_clusters] failed: %s", e)
        return f"Blast radius resolution failed: {e}"


# ---------------------------------------------------------------------------
# Tier 2 tools — called by agent at its discretion
# ---------------------------------------------------------------------------

def _fetch_object_on_cluster(cluster: str, namespace: str, kind: str, name: str) -> str:
    """
    Fetch current state of an object on a cluster via ACM Search.

    ACM Search indexes resources from all clusters (hub + managed).
    If Search returns empty → object not yet deployed (or not indexed yet).

    TODO fallback:
      cluster == local-cluster → direct k8s API call
      cluster == managed       → cluster-proxy HTTP call
    """
    _ensure_log_level()
    prefix = f"[fetch_object cluster={cluster} kind={kind} name={name}]"
    log.info("%s starting fetch", prefix)

    filters = [
        {"property": "kind",    "values": [kind]},
        {"property": "name",    "values": [name]},
        {"property": "cluster", "values": [cluster]},
    ]
    if namespace:
        filters.append({"property": "namespace", "values": [namespace]})
        log.debug("%s namespace filter: %s", prefix, namespace)

    items = _search_resources(filters, caller=f"fetch/{kind}/{name}")

    if not items:
        msg = (
            f"{kind}/{name} not found in ACM Search on cluster '{cluster}'. "
            "Object may not be deployed yet, or Search index is still updating."
            "\n[TODO] Fallback: direct k8s API (hub) or cluster-proxy (managed) not yet implemented."
        )
        log.info("%s %s", prefix, msg)
        return msg

    obj = items[0]
    log.info("%s found object in Search", prefix)
    return json.dumps(obj, indent=2)


def _check_object_ownership(cluster: str, namespace: str, kind: str, name: str) -> str:
    """
    Check whether an object on a cluster is already owned by another controller.

    Queries ACM Search for the object, then inspects:
      _ownerReferences  → ManifestWork = another ACM Policy owns it
      _annotations      → argocd.argoproj.io/managed-by = ArgoCD owns it
                          app.kubernetes.io/managed-by=Helm = Helm owns it

    TODO fallback:
      If Search returns empty → direct k8s API (hub) or cluster-proxy (managed)
    """
    _ensure_log_level()
    prefix = f"[check_ownership cluster={cluster} kind={kind} name={name}]"
    log.info("%s starting ownership check", prefix)

    filters = [
        {"property": "kind",    "values": [kind]},
        {"property": "name",    "values": [name]},
        {"property": "cluster", "values": [cluster]},
    ]
    if namespace:
        filters.append({"property": "namespace", "values": [namespace]})
        log.debug("%s namespace filter: %s", prefix, namespace)

    items = _search_resources(filters, caller=f"ownership/{kind}/{name}")

    if not items:
        msg = (
            f"{kind}/{name} not found in ACM Search on cluster '{cluster}'. "
            "Cannot determine ownership — object may not exist yet."
            "\n[TODO] Fallback: direct k8s API (hub) or cluster-proxy (managed) not yet implemented."
        )
        log.info("%s %s", prefix, msg)
        return msg

    obj = items[0]
    log.debug("%s raw Search item: %s", prefix, json.dumps(obj))

    # Parse annotations: Search returns them as ["key=value", ...]
    raw_annotations = obj.get("_annotations") or []
    annotations = _parse_annotations(raw_annotations)
    log.debug("%s parsed annotations: %s", prefix, annotations)

    # Parse ownerReferences: Search stores as JSON string
    raw_owner_refs = obj.get("_ownerReferences")
    owner_refs = _parse_owner_refs(raw_owner_refs)
    log.debug("%s parsed ownerReferences: %s", prefix, owner_refs)

    conflicts = []

    argocd_app = annotations.get("argocd.argoproj.io/managed-by")
    if argocd_app:
        conflicts.append(f"ArgoCD (app: {argocd_app})")
        log.info("%s detected ArgoCD ownership: app=%s", prefix, argocd_app)

    if annotations.get("app.kubernetes.io/managed-by") == "Helm":
        conflicts.append("Helm")
        log.info("%s detected Helm ownership", prefix)

    for ref in owner_refs:
        if ref.get("kind") == "ManifestWork":
            mw_name = ref.get("name", "")
            conflicts.append(f"ACM ManifestWork ({mw_name})")
            log.info("%s detected ACM ManifestWork ownership: %s", prefix, mw_name)

    if conflicts:
        msg = (
            f"CONFLICT: {kind}/{name} on cluster '{cluster}' is already managed by: "
            + ", ".join(conflicts)
            + ". Two controllers owning the same object will fight — hard blocker."
        )
        log.info("%s CONFLICT detected: %s", prefix, conflicts)
        return msg

    msg = f"OK: No controller conflicts detected for {kind}/{name} on cluster '{cluster}'."
    log.debug("%s %s", prefix, msg)
    return msg


# ---------------------------------------------------------------------------
# function_tool wrappers — exposed to the agent
# ---------------------------------------------------------------------------

def _tool_resolve_placement(
    placement_name: str,
    namespace: str,
    placement_in_diff: bool,
    match_labels: str = "{}",
) -> str:
    """
    Resolve which managed clusters are targeted by a Placement.

    placement_name: name of the Placement resource
    namespace: namespace of the Placement
    placement_in_diff: True if Placement spec was added/modified in this PR;
                       False if PR only references an existing Placement via PlacementBinding
    match_labels: JSON string of clusterSelector.matchLabels from diff

    NOTE: This tool is NOT registered in ALL_TIER2_SKILLS and is NOT exposed to the agent.
    Blast radius is pre-computed deterministically by runner.py via resolve_clusters() before
    the agent runs. The agent reads the result from the prompt — it should not re-compute it.
    This tool only accepts matchLabels, not matchExpressions — calling it for Placements that
    use matchExpressions causes a Kubernetes API 400 error and corrupts the agent's analysis.
    Re-enable (with matchExpressions support added) only if there is a clear reason for the
    agent to re-resolve placement at runtime.
    """
    _ensure_log_level()
    labels = json.loads(match_labels) if match_labels else {}
    from app.skills.acm_placement import PlacementCase, PlacementResult
    pr = PlacementResult(
        case=PlacementCase.EVALUATE if placement_in_diff else PlacementCase.READ_DECISION,
        placement_name=placement_name,
        placement_namespace=namespace,
        match_labels=labels,
    )
    return resolve_clusters(pr)


def _tool_check_object_ownership(cluster: str, namespace: str, kind: str, name: str) -> str:
    """
    Check whether an object on a managed cluster is already owned by another controller
    (ArgoCD, Helm, another ACM Policy). Use this to detect controller conflicts before merge.

    cluster: managed cluster name (e.g. "dsf-mc")
    namespace: object namespace (empty string for cluster-scoped resources)
    kind: resource kind (e.g. "ConfigMap", "ImageDigestMirrorSet")
    name: resource name
    """
    return _check_object_ownership(cluster, namespace, kind, name)


def _tool_fetch_object_on_cluster(cluster: str, namespace: str, kind: str, name: str) -> str:
    """
    Fetch the current state of an object on a cluster via ACM Search.
    Use this to confirm whether a risky config already exists on a cluster
    (e.g. is remoteWrite configured in cluster-monitoring-config?).

    cluster: cluster name
    namespace: object namespace (empty string for cluster-scoped resources)
    kind: resource kind
    name: resource name
    """
    return _fetch_object_on_cluster(cluster, namespace, kind, name)


resolve_placement     = function_tool(_tool_resolve_placement)
check_object_ownership = function_tool(_tool_check_object_ownership)
fetch_object_on_cluster = function_tool(_tool_fetch_object_on_cluster)

# resolve_placement intentionally excluded — blast radius is pre-computed by runner.py.
# See _tool_resolve_placement docstring for full explanation.
ALL_TIER2_SKILLS = [check_object_ownership, fetch_object_on_cluster]
