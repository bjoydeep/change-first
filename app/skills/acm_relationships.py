"""
Relationship resolvers — hub-side and cluster-proxy.

Five resolvers answer: "who else controls this object?"

  check_relationships_pre_agent  — DETERMINISTIC: always runs before agent, extracts
                                   target objects from diff and runs all hub-side +
                                   cluster-proxy checks. Essential safety checks are
                                   not left to LLM discretion.

  resolve_configpolicy_conflict  — another ACM ConfigurationPolicy targeting same object
  resolve_argocd_conflict        — ArgoCD Application already managing same object
                                   (with destination cluster filtering)
  resolve_manifestwork           — active ManifestWork already delivering object to cluster
  fetch_object_from_cluster      — full object from managed cluster via cluster-proxy:
                                   managedFields (who wrote it), ownerReferences, data content

Three outcome levels (not binary CONFLICT/NO CONFLICT):
  SHARED_TARGET      — another system has touched this object (raise suspicion)
  POTENTIAL_CONFLICT — overlapping write scope (warn)
  CONFIRMED_CONFLICT — incompatible desired values (block)

cluster-proxy uses the pr-analyzer ManagedServiceAccount token synced back to hub
as Secret/pr-analyzer in each cluster's namespace. Token is rotated automatically by ACM.

Logging is controlled by TIER2_LOG_LEVEL config.
Set TIER2_LOG_LEVEL=debug to see every object inspected, every match attempted.
"""

from __future__ import annotations

import base64
import json
import logging

import httpx
from agents import function_tool

log = logging.getLogger(__name__)

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
        log.debug("[relationships] log level set to %s", level)
    except Exception:
        pass
    _log_configured = True


def _k8s_client():
    from kubernetes import client, config  # type: ignore
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client


# ---------------------------------------------------------------------------
# Target object extraction from PR diff
# ---------------------------------------------------------------------------

def _extract_configpolicy_targets(diff: str) -> list[tuple[str, str, str]]:
    """
    Extract target objects from ConfigurationPolicy object-templates in the diff.

    For a ConfigurationPolicy like:
      spec.object-templates[].objectDefinition:
        kind: ConfigMap
        metadata: {name: cluster-monitoring-config, namespace: openshift-monitoring}

    Returns list of (kind, name, namespace) tuples.
    Namespace is "" for cluster-scoped resources.
    """
    from app.skills.acm_placement import _parse_yaml_docs
    docs = _parse_yaml_docs(diff)
    targets = []

    for doc in docs:
        if doc.get("kind") != "ConfigPolicy" and doc.get("kind") != "ConfigurationPolicy":
            continue
        for tpl in doc.get("spec", {}).get("object-templates", []):
            obj = tpl.get("objectDefinition", {})
            kind = obj.get("kind", "")
            name = obj.get("metadata", {}).get("name", "")
            namespace = obj.get("metadata", {}).get("namespace", "")
            if kind and name:
                log.debug("[extract_targets] found target: kind=%s name=%s ns=%s",
                          kind, name, namespace)
                targets.append((kind, name, namespace))

        # Also handle ConfigPolicy nested inside Policy (via policy-templates)
    for doc in docs:
        if doc.get("kind") != "Policy":
            continue
        for policy_tpl in doc.get("spec", {}).get("policy-templates", []):
            inner = policy_tpl.get("objectDefinition", {})
            if inner.get("kind") not in ("ConfigPolicy", "ConfigurationPolicy"):
                continue
            for tpl in inner.get("spec", {}).get("object-templates", []):
                obj = tpl.get("objectDefinition", {})
                kind = obj.get("kind", "")
                name = obj.get("metadata", {}).get("name", "")
                namespace = obj.get("metadata", {}).get("namespace", "")
                if kind and name:
                    log.debug("[extract_targets] found nested target: kind=%s name=%s ns=%s",
                              kind, name, namespace)
                    targets.append((kind, name, namespace))

    # Deduplicate
    seen = set()
    unique = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


# ---------------------------------------------------------------------------
# Resolver 1: ACM ConfigurationPolicy conflict
# ---------------------------------------------------------------------------

def _resolve_configpolicy_conflict(kind: str, name: str, namespace: str) -> str:
    """
    Scan ALL ConfigurationPolicy resources on the hub for ones that already
    target the same object (kind/name/namespace).
    Raises SHARED_TARGET suspicion even for inform-mode policies.
    """
    _ensure_log_level()
    prefix = f"[configpolicy_conflict kind={kind} name={name} ns={namespace!r}]"
    log.info("%s --- starting scan ---", prefix)

    try:
        k8s = _k8s_client()
        custom = k8s.CustomObjectsApi()

        cps = custom.list_cluster_custom_object(
            group="policy.open-cluster-management.io",
            version="v1",
            plural="configurationpolicies",
        )
        items = cps.get("items", [])
        log.info("%s found %d ConfigurationPolicy resource(s) on hub", prefix, len(items))

        findings = []
        for cp in items:
            cp_name     = cp.get("metadata", {}).get("name", "")
            cp_ns       = cp.get("metadata", {}).get("namespace", "")
            remediation = cp.get("spec", {}).get("remediationAction", "unknown")
            templates   = cp.get("spec", {}).get("object-templates", [])

            log.debug("%s checking ConfigurationPolicy/%s in ns=%s remediation=%s templates=%d",
                      prefix, cp_name, cp_ns, remediation, len(templates))

            for i, tpl in enumerate(templates):
                obj_def    = tpl.get("objectDefinition", {})
                obj_kind   = obj_def.get("kind", "")
                obj_name   = obj_def.get("metadata", {}).get("name", "")
                obj_ns     = obj_def.get("metadata", {}).get("namespace", "")
                compliance = tpl.get("complianceType", "unknown")

                log.debug("%s   template[%d]: kind=%s name=%s ns=%s complianceType=%s",
                          prefix, i, obj_kind, obj_name, obj_ns, compliance)

                if obj_kind == kind and obj_name == name and obj_ns == namespace:
                    level = "POTENTIAL_CONFLICT" if remediation == "enforce" else "SHARED_TARGET"
                    log.info("%s *** %s *** ConfigurationPolicy/%s ns=%s "
                             "remediation=%s complianceType=%s",
                             prefix, level, cp_name, cp_ns, remediation, compliance)
                    findings.append(
                        f"`ConfigurationPolicy/{cp_name}` "
                        f"(namespace: {cp_ns}, remediationAction: {remediation}, "
                        f"complianceType: {compliance}) [{level}]"
                    )

        if findings:
            has_enforce = any("enforce" in f for f in findings)
            severity = "❌ POTENTIAL_CONFLICT" if has_enforce else "⚠️ SHARED_TARGET"
            msg = (
                f"{severity}: The following ConfigurationPolicies already target "
                f"`{kind}/{name}` in `{namespace}`:\n"
                + "\n".join(f"  - {f}" for f in findings)
                + ("\n  Enforcing policies on the same object may fight (last-write-wins)."
                   if has_enforce else
                   "\n  Inform-mode policies share this object — monitor for unexpected enforcement.")
            )
            log.info("%s result: %d finding(s)", prefix, len(findings))
            return msg

        log.info("%s result: no conflicts found", prefix)
        return f"✅ No existing ConfigurationPolicy targets `{kind}/{name}` in `{namespace}`."

    except Exception as e:
        log.warning("%s scan failed: %s", prefix, e)
        return f"Could not scan ConfigurationPolicies: {e}"


# ---------------------------------------------------------------------------
# Resolver 2: ArgoCD Application conflict
# ---------------------------------------------------------------------------

def _dest_matches_clusters(dest: dict, target_clusters: list[str] | None) -> bool:
    """
    Check if an ArgoCD Application's destination matches any of the target clusters.
    Uses best-effort substring matching — ArgoCD cluster names may differ from ACM names.
    Errs on the side of inclusion (raise suspicion when uncertain).
    """
    if not target_clusters:
        return True
    dest_name   = dest.get("name", "")
    dest_server = dest.get("server", "")
    for cluster in target_clusters:
        if cluster in dest_name or cluster in dest_server:
            return True
        if dest_name and dest_name in cluster:
            return True
    # Could not confirm destination — still flag it (raise suspicion to be safe)
    if dest_name in ("in-cluster", "", "https://kubernetes.default.svc"):
        # local cluster — may still be relevant
        return True
    return False


def _resolve_argocd_conflict(
    kind: str, name: str, namespace: str,
    target_clusters: list[str] | None = None
) -> str:
    """
    Scan ArgoCD Application resources on the hub for ones that already manage
    the same object via Application.status.resources (populated after first sync).

    target_clusters: when provided, filter to Applications whose destination
    matches one of the targeted clusters. Best-effort name matching.
    When destination is ambiguous, Application is still flagged (raise suspicion).

    Only catches ArgoCD on the hub. Standalone ArgoCD per cluster needs cluster-proxy.
    """
    _ensure_log_level()
    prefix = f"[argocd_conflict kind={kind} name={name} ns={namespace!r}]"
    log.info("%s --- starting scan --- target_clusters=%s", prefix, target_clusters)

    try:
        k8s = _k8s_client()
        custom = k8s.CustomObjectsApi()

        try:
            apps = custom.list_cluster_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                plural="applications",
            )
        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower():
                log.info("%s ArgoCD CRDs not installed on hub — skipping", prefix)
                return "ℹ️ ArgoCD not installed on hub — Application conflict check skipped."
            raise

        items = apps.get("items", [])
        log.info("%s found %d ArgoCD Application(s) on hub", prefix, len(items))

        findings = []
        for app in items:
            app_name     = app.get("metadata", {}).get("name", "")
            app_ns       = app.get("metadata", {}).get("namespace", "")
            dest         = app.get("spec", {}).get("destination", {})
            dest_cluster = dest.get("name", dest.get("server", "unknown"))
            sync_status  = app.get("status", {}).get("sync", {}).get("status", "unknown")
            resources    = app.get("status", {}).get("resources", [])

            if not _dest_matches_clusters(dest, target_clusters):
                log.debug("%s skipping Application/%s — destination %s not in target clusters",
                          prefix, app_name, dest_cluster)
                continue

            log.debug("%s checking Application/%s ns=%s dest=%s sync=%s resources=%d",
                      prefix, app_name, app_ns, dest_cluster, sync_status, len(resources))

            for i, res in enumerate(resources):
                res_kind   = res.get("kind", "")
                res_name   = res.get("name", "")
                res_ns     = res.get("namespace", "")
                res_sync   = res.get("status", "unknown")
                res_health = res.get("health", {}).get("status", "unknown")

                log.debug("%s   resource[%d]: kind=%s name=%s ns=%s sync=%s health=%s",
                          prefix, i, res_kind, res_name, res_ns, res_sync, res_health)

                if res_kind == kind and res_name == name and res_ns == namespace:
                    log.info("%s *** SHARED_TARGET *** Application/%s ns=%s dest=%s "
                             "manages kind=%s name=%s sync=%s health=%s",
                             prefix, app_name, app_ns, dest_cluster,
                             res_kind, res_name, res_sync, res_health)
                    findings.append(
                        f"`Application/{app_name}` (namespace: {app_ns}, "
                        f"destination: {dest_cluster}, sync: {res_sync}, "
                        f"health: {res_health}) [SHARED_TARGET]"
                    )

        if findings:
            msg = (
                f"⚠️ SHARED_TARGET: ArgoCD Applications on hub also manage "
                f"`{kind}/{name}` in `{namespace}`:\n"
                + "\n".join(f"  - {f}" for f in findings)
                + "\n  ACM Policy enforcement may fight with ArgoCD sync."
                + "\n  ⚠️ Standalone ArgoCD on managed clusters not checked (requires cluster-proxy)."
            )
            log.info("%s result: %d finding(s)", prefix, len(findings))
            return msg

        log.info("%s result: no ArgoCD conflicts found", prefix)
        return (
            f"✅ No ArgoCD Applications on hub manage `{kind}/{name}` in `{namespace}`.\n"
            f"  ⚠️ Standalone ArgoCD on managed clusters not checked (requires cluster-proxy)."
        )

    except Exception as e:
        log.warning("%s scan failed: %s", prefix, e)
        return f"Could not scan ArgoCD Applications: {e}"


# ---------------------------------------------------------------------------
# Resolver 3: ManifestWork inspection
# ---------------------------------------------------------------------------

def _resolve_manifestwork(cluster: str, kind: str, name: str, namespace: str) -> str:
    """
    Check whether an active ManifestWork in the cluster's namespace on the hub
    is already delivering this object directly.

    Note: ManifestWork delivers policy framework artifacts, not always the target
    object itself. Finding a match is strong evidence; not finding one does not
    mean no policy enforces this object (governance controllers evaluate and
    apply policies without a ManifestWork per target object).
    """
    _ensure_log_level()
    prefix = f"[manifestwork cluster={cluster} kind={kind} name={name} ns={namespace!r}]"
    log.info("%s --- starting scan in namespace=%s ---", prefix, cluster)

    try:
        k8s = _k8s_client()
        custom = k8s.CustomObjectsApi()

        mws = custom.list_namespaced_custom_object(
            group="work.open-cluster-management.io",
            version="v1",
            namespace=cluster,
            plural="manifestworks",
        )
        items = mws.get("items", [])
        log.info("%s found %d ManifestWork(s) in namespace=%s", prefix, len(items), cluster)

        matches = []
        for mw in items:
            mw_name   = mw.get("metadata", {}).get("name", "")
            manifests = mw.get("spec", {}).get("workload", {}).get("manifests", [])

            log.debug("%s checking ManifestWork/%s manifests=%d", prefix, mw_name, len(manifests))

            for i, manifest in enumerate(manifests):
                m_kind = manifest.get("kind", "")
                m_name = manifest.get("metadata", {}).get("name", "")
                m_ns   = manifest.get("metadata", {}).get("namespace", "")

                log.debug("%s   manifest[%d]: kind=%s name=%s ns=%s",
                          prefix, i, m_kind, m_name, m_ns)

                if m_kind == kind and m_name == name and m_ns == namespace:
                    log.info("%s *** SHARED_TARGET *** ManifestWork/%s delivers %s/%s to %s",
                             prefix, mw_name, kind, name, cluster)
                    matches.append(f"`ManifestWork/{mw_name}` [SHARED_TARGET]")

        if matches:
            msg = (
                f"⚠️ SHARED_TARGET: "
                + ", ".join(matches)
                + f" is already delivering `{kind}/{name}` in `{namespace}` "
                  f"to cluster `{cluster}`. "
                  "Another ACM policy is actively managing this object. "
                  "This PR creates a second controller on the same object."
            )
            log.info("%s result: %d ManifestWork match(es)", prefix, len(matches))
            return msg

        log.info("%s result: no ManifestWork match found", prefix)
        return (
            f"✅ No ManifestWork is directly delivering `{kind}/{name}` in `{namespace}` "
            f"to cluster `{cluster}`. Note: governance controllers may still enforce "
            "this object without a direct ManifestWork delivery."
        )

    except Exception as e:
        log.warning("%s scan failed: %s", prefix, e)
        return f"Could not scan ManifestWorks for cluster `{cluster}`: {e}"


# ---------------------------------------------------------------------------
# Resolver 4: cluster-proxy — full object fetch (managedFields + data content)
# ---------------------------------------------------------------------------

_PROXY_BASE = (
    "https://cluster-proxy-addon-user.multicluster-engine.svc.cluster.local:9092"
)

# Kind → (api_group, api_version, plural, namespaced)
_KIND_API = {
    "ConfigMap":            ("",                                   "v1",  "configmaps",            True),
    "Secret":               ("",                                   "v1",  "secrets",               True),
    "Pod":                  ("",                                   "v1",  "pods",                  True),
    "Service":              ("",                                   "v1",  "services",              True),
    "Namespace":            ("",                                   "v1",  "namespaces",            False),
    "Deployment":           ("apps",                               "v1",  "deployments",           True),
    "DaemonSet":            ("apps",                               "v1",  "daemonsets",            True),
    "StatefulSet":          ("apps",                               "v1",  "statefulsets",          True),
    "ImageDigestMirrorSet": ("config.openshift.io",                "v1",  "imagedigestmirrorsets", False),
    "ImageTagMirrorSet":    ("config.openshift.io",                "v1",  "imagetagmirrorsets",    False),
    "APIServer":            ("config.openshift.io",                "v1",  "apiservers",            False),
    "OAuth":                ("config.openshift.io",                "v1",  "oauths",                False),
    "MachineConfig":        ("machineconfiguration.openshift.io",  "v1",  "machineconfigs",        False),
}


def _proxy_token(cluster: str) -> str:
    k8s = _k8s_client()
    core = k8s.CoreV1Api()
    secret = core.read_namespaced_secret(name="pr-analyzer", namespace=cluster)
    raw = secret.data.get("token", "")
    return base64.b64decode(raw).decode()


def _proxy_url(cluster: str, kind: str, name: str, namespace: str) -> str | None:
    entry = _KIND_API.get(kind)
    if not entry:
        return None
    api_group, api_version, plural, namespaced = entry
    base = (
        f"{_PROXY_BASE}/{cluster}/apis/{api_group}/{api_version}"
        if api_group
        else f"{_PROXY_BASE}/{cluster}/api/{api_version}"
    )
    if namespaced and namespace:
        return f"{base}/namespaces/{namespace}/{plural}/{name}"
    return f"{base}/{plural}/{name}"


def _fetch_object_from_cluster(cluster: str, namespace: str, kind: str, name: str) -> str:
    """
    Fetch the full object from a managed cluster via cluster-proxy.

    managedFields = supporting evidence (not proof of active reconciliation).
    Raises SHARED_TARGET suspicion when another manager appears.
    Returns current data/spec content for comparison with what the PR proposes.
    """
    _ensure_log_level()
    prefix = f"[fetch_from_cluster cluster={cluster} kind={kind} name={name} ns={namespace!r}]"
    log.info("%s --- starting cluster-proxy fetch ---", prefix)

    url = _proxy_url(cluster, kind, name, namespace)
    if url is None:
        msg = (
            f"Kind `{kind}` not in known API map — cannot build cluster-proxy URL. "
            "Add it to _KIND_API in acm_relationships.py."
        )
        log.warning("%s %s", prefix, msg)
        return msg

    log.debug("%s proxy URL: %s", prefix, url)

    try:
        token = _proxy_token(cluster)
        log.debug("%s token read from Secret/pr-analyzer in ns=%s (len=%d)",
                  prefix, cluster, len(token))
    except Exception as e:
        msg = (
            f"Could not read pr-analyzer token from Secret in namespace `{cluster}`: {e}. "
            "Ensure the ManagedServiceAccount is created and the token has been synced."
        )
        log.warning("%s %s", prefix, msg)
        return msg

    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            verify=False,
            timeout=15,
        )
        log.debug("%s HTTP status: %s", prefix, resp.status_code)

        if resp.status_code == 404:
            log.info("%s object not found on cluster — likely a new creation", prefix)
            return (
                f"`{kind}/{name}` not found on cluster `{cluster}`. "
                "Object does not exist yet — this PR creates it fresh."
            )
        if resp.status_code == 403:
            log.warning("%s 403 forbidden — RBAC not yet applied on spoke: %s",
                        prefix, resp.text[:200])
            return (
                f"403 Forbidden — pr-analyzer SA lacks read access on cluster `{cluster}`. "
                "The ManifestWork RBAC may still be propagating."
            )

        resp.raise_for_status()
        obj = resp.json()
        log.debug("%s raw object keys: %s", prefix, list(obj.keys()))

        meta          = obj.get("metadata", {})
        managed_fields = meta.get("managedFields", [])
        owner_refs    = meta.get("ownerReferences", [])
        data          = obj.get("data", obj.get("spec", {}))

        log.info("%s found — managedFields=%d ownerRefs=%d data_keys=%s",
                 prefix, len(managed_fields), len(owner_refs),
                 list(data.keys()) if data else [])

        for mf in managed_fields:
            log.debug("%s   managedField: manager=%s op=%s time=%s",
                      prefix, mf.get("manager"), mf.get("operation"), mf.get("time"))
        for ref in owner_refs:
            log.debug("%s   ownerRef: kind=%s name=%s controller=%s",
                      prefix, ref.get("kind"), ref.get("name"), ref.get("controller"))

        lines = [f"**Object:** `{kind}/{name}` on cluster `{cluster}`"]

        if managed_fields:
            lines.append(
                "\n**managedFields** (supporting evidence — who has written to this object; "
                "not proof of active reconciliation):"
            )
            for mf in managed_fields:
                lines.append(
                    f"  - `{mf.get('manager', 'unknown')}` "
                    f"({mf.get('operation', '?')}, {mf.get('time', '?')}) [SHARED_TARGET]"
                )
        else:
            lines.append("\n**managedFields:** none recorded")

        if owner_refs:
            lines.append("\n**ownerReferences:**")
            for ref in owner_refs:
                lines.append(
                    f"  - {ref.get('kind')}/{ref.get('name')} "
                    f"(controller={ref.get('controller', False)})"
                )
        else:
            lines.append("\n**ownerReferences:** none")

        if data:
            lines.append(f"\n**Current content keys:** {list(data.keys())}")
            for k, v in data.items():
                v_str = str(v)
                truncated = v_str[:500] + "..." if len(v_str) > 500 else v_str
                lines.append(f"\n**data.{k}:**\n```\n{truncated}\n```")

        result = "\n".join(lines)
        log.info("%s returning %d chars", prefix, len(result))
        return result

    except Exception as e:
        log.warning("%s cluster-proxy fetch failed: %s", prefix, e)
        return f"cluster-proxy fetch failed for `{kind}/{name}` on `{cluster}`: {e}"


# ---------------------------------------------------------------------------
# Pre-agent deterministic relationship check
# Runs before the agent — essential safety checks not left to LLM discretion
# ---------------------------------------------------------------------------

def check_relationships_pre_agent(diff: str, clusters: list[str]) -> str:
    """
    Run ALL relationship checks for target objects identified in the diff.
    Called by runner.py before the agent runs — always executes regardless
    of LLM reasoning.

    Extracts target objects from ConfigurationPolicy object-templates,
    then for each object:
      - Hub-side: configpolicy conflict, argocd conflict, manifestwork (per cluster)
      - cluster-proxy: full object fetch (managedFields, ownerRefs, current data)

    Returns a formatted findings string, empty if no targets found.
    """
    _ensure_log_level()
    log.info("[pre_agent_relationships] starting — clusters=%s", clusters)

    targets = _extract_configpolicy_targets(diff)
    if not targets:
        log.info("[pre_agent_relationships] no ConfigurationPolicy targets found in diff")
        return ""

    log.info("[pre_agent_relationships] found %d target object(s): %s", len(targets), targets)

    sections = []
    for kind, name, namespace in targets:
        obj_id = f"`{kind}/{name}`" + (f" in `{namespace}`" if namespace else " (cluster-scoped)")
        lines = [f"\n### Relationship check: {obj_id}"]

        # Hub-side checks (run once per object)
        lines.append(_resolve_configpolicy_conflict(kind, name, namespace))
        lines.append(_resolve_argocd_conflict(kind, name, namespace, target_clusters=clusters))

        # Per-cluster checks
        for cluster in clusters:
            lines.append(_resolve_manifestwork(cluster, kind, name, namespace))
            lines.append(_fetch_object_from_cluster(cluster, namespace, kind, name))

        sections.append("\n".join(lines))

    if not sections:
        return ""

    header = "## Pre-merge Relationship Findings\n"
    return header + "\n".join(sections)


# ---------------------------------------------------------------------------
# function_tool wrappers — available for agent deep-dive investigation
# ---------------------------------------------------------------------------

def _tool_resolve_configpolicy_conflict(kind: str, name: str, namespace: str) -> str:
    """
    Check whether another ACM ConfigurationPolicy on the hub already targets
    the same object (kind/name/namespace). Raises suspicion even for inform-mode.

    kind: Kubernetes resource kind (e.g. "ConfigMap")
    name: resource name (e.g. "cluster-monitoring-config")
    namespace: resource namespace (empty string for cluster-scoped resources)
    """
    return _resolve_configpolicy_conflict(kind, name, namespace)


def _tool_resolve_argocd_conflict(kind: str, name: str, namespace: str) -> str:
    """
    Check whether an ArgoCD Application on the hub already manages the same object.
    Filters by destination cluster when called from pre-agent context.

    kind: Kubernetes resource kind
    name: resource name
    namespace: resource namespace
    """
    return _resolve_argocd_conflict(kind, name, namespace)


def _tool_resolve_manifestwork(cluster: str, kind: str, name: str, namespace: str) -> str:
    """
    Check whether a ManifestWork in the cluster's namespace on the hub
    is already delivering this object to that cluster.

    cluster: managed cluster name (e.g. "dsf-mc")
    kind: Kubernetes resource kind
    name: resource name
    namespace: resource namespace
    """
    return _resolve_manifestwork(cluster, kind, name, namespace)


def _tool_fetch_object_from_cluster(
    cluster: str, namespace: str, kind: str, name: str
) -> str:
    """
    Fetch the full object from a managed cluster via cluster-proxy.
    Returns managedFields, ownerReferences, and current data/spec content.

    cluster: managed cluster name (e.g. "dsf-mc")
    namespace: object namespace (empty string for cluster-scoped resources)
    kind: resource kind — must be in known list (ConfigMap, Secret, Deployment,
          ImageDigestMirrorSet, MachineConfig, APIServer, OAuth, etc.)
    name: resource name
    """
    return _fetch_object_from_cluster(cluster, namespace, kind, name)


resolve_configpolicy_conflict = function_tool(_tool_resolve_configpolicy_conflict)
resolve_argocd_conflict       = function_tool(_tool_resolve_argocd_conflict)
resolve_manifestwork          = function_tool(_tool_resolve_manifestwork)
fetch_object_from_cluster     = function_tool(_tool_fetch_object_from_cluster)

ALL_RELATIONSHIP_SKILLS = [
    resolve_configpolicy_conflict,
    resolve_argocd_conflict,
    resolve_manifestwork,
    fetch_object_from_cluster,
]
