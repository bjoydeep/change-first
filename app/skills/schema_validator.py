"""
Schema validation against the hub's live OpenAPI v3 schemas.

Flow:
  1. Parse YAML documents from the PR diff
  2. Unwrap embedded payloads (Policy → ConfigPolicy → objectDefinition)
  3. For each resource, fetch its OpenAPI schema from the hub API server
  4. Validate using jsonschema, report errors

Works for ANY resource kind — Policy, ConfigPolicy, ApplicationSet,
Placement, ImageDigestMirrorSet, ConfigMap, etc.

The hub's schema is used for all resources. Resources that run on managed
clusters (inside ConfigPolicy objectDefinition) may differ slightly if the
managed cluster is a different OCP version — noted in output.

Called deterministically by runner.py before the agent runs.
Requires in-cluster config (runs on hub). Fails gracefully if unavailable.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache

import httpx
import jsonschema
import jsonschema.exceptions

from app.skills.acm_placement import _parse_yaml_docs

log = logging.getLogger(__name__)

_K8S_HOST = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
_K8S_PORT = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
_K8S_API  = f"https://{_K8S_HOST}:{_K8S_PORT}"
_K8S_CA   = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_K8S_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SchemaFinding:
    kind: str
    name: str
    api_version: str
    source: str          # "top-level" | "Policy→ConfigPolicy" | "ConfigPolicy→payload"
    valid: bool
    errors: list[str] = field(default_factory=list)
    note: str = ""       # schema unavailable, skipped, etc.


# ---------------------------------------------------------------------------
# Hub API helpers
# ---------------------------------------------------------------------------

def _read_token() -> str:
    with open(_K8S_TOKEN) as f:
        return f.read().strip()


@lru_cache(maxsize=32)
def _fetch_openapi_schema(group: str, version: str) -> dict | None:
    """
    Fetch and cache the OpenAPI v3 schema document for a group/version.

    group=""  → core group  → /openapi/v3/api/{version}
    group=X   → named group → /openapi/v3/apis/{group}/{version}
    """
    path = (
        f"/openapi/v3/api/{version}"
        if not group
        else f"/openapi/v3/apis/{group}/{version}"
    )
    log.debug("[schema] fetching OpenAPI: %s", path)

    try:
        token = _read_token()
        resp = httpx.get(
            f"{_K8S_API}{path}",
            headers={"Authorization": f"Bearer {token}"},
            verify=_K8S_CA,
            timeout=15,
        )
        if resp.status_code == 404:
            log.debug("[schema] 404 for %s — group/version not on this hub", path)
            return None
        resp.raise_for_status()
        doc = resp.json()
        n = len(doc.get("components", {}).get("schemas", {}))
        log.debug("[schema] fetched schema for %s/%s — %d schema definitions", group, version, n)
        return doc

    except Exception as e:
        log.warning("[schema] failed to fetch %s: %s", path, e)
        return None


@lru_cache(maxsize=16)
def _fetch_group_versions(group: str) -> list[str]:
    """Discover all served versions for a named API group."""
    try:
        token = _read_token()
        resp = httpx.get(
            f"{_K8S_API}/apis/{group}",
            headers={"Authorization": f"Bearer {token}"},
            verify=_K8S_CA,
            timeout=10,
        )
        resp.raise_for_status()
        versions = [v["version"] for v in resp.json().get("versions", [])]
        log.debug("[schema] group %s serves versions: %s", group, versions)
        return versions
    except Exception as e:
        log.warning("[schema] could not discover versions for group %s: %s", group, e)
        return []


@lru_cache(maxsize=64)
def _kind_served_at_version(group: str, version: str, kind: str) -> bool:
    """
    Check whether the API server actually serves this kind at this group/version.
    Uses the resource discovery endpoint — the authoritative source.

    This distinguishes two cases when a schema fallback is found:
      - Kind IS served at the version (API conversion, schema just stored elsewhere) → valid
      - Kind is NOT served at the version (wrong apiVersion in the YAML) → error
    """
    try:
        token = _read_token()
        path = f"/apis/{group}/{version}" if group else f"/api/{version}"
        resp = httpx.get(
            f"{_K8S_API}{path}",
            headers={"Authorization": f"Bearer {token}"},
            verify=_K8S_CA,
            timeout=10,
        )
        if resp.status_code != 200:
            return False
        resources = resp.json().get("resources", [])
        served = any(r.get("kind") == kind for r in resources)
        log.debug("[schema] kind=%s served at %s/%s: %s", kind, group, version, served)
        return served
    except Exception as e:
        log.warning("[schema] discovery check failed for %s/%s/%s: %s", group, version, kind, e)
        return False


def _find_kind_schema(
    openapi_doc: dict, kind: str, group: str, version: str
) -> tuple[dict | None, str]:
    """
    Locate the JSON Schema for a specific kind within an OpenAPI v3 document.
    Uses x-kubernetes-group-version-kind for accurate matching.

    Kubernetes serves one resource across multiple API versions (e.g. Placement
    is stored as v1beta1 even when requested as v1beta2). If the kind is not
    found in the requested version's schema doc, we try all other served versions
    of the same group and return the schema with a note about the version used.

    Returns (schema_dict, version_actually_used) or (None, "").
    """
    def _search_doc(doc: dict, target_version: str) -> dict | None:
        for schema_def in doc.get("components", {}).get("schemas", {}).values():
            for gvk in schema_def.get("x-kubernetes-group-version-kind", []):
                if gvk.get("kind") != kind:
                    continue
                if group and gvk.get("group") != group:
                    continue
                if gvk.get("version") == target_version:
                    return schema_def
        return None

    # Try requested version first
    found = _search_doc(openapi_doc, version)
    if found:
        log.debug("[schema] matched kind=%s version=%s", kind, version)
        return found, version

    # Kind not in requested version — try other versions of the same group
    # (Kubernetes API conversion: hub may store v1beta1 internally for v1beta2 resources)
    if group:
        for alt_version in _fetch_group_versions(group):
            if alt_version == version:
                continue
            alt_doc = _fetch_openapi_schema(group, alt_version)
            if alt_doc is None:
                continue
            found = _search_doc(alt_doc, alt_version)
            if found:
                log.debug("[schema] kind=%s not in %s, found in %s (hub version conversion)",
                          kind, version, alt_version)
                return found, alt_version

    log.debug("[schema] no schema for kind=%s group=%s version=%s (tried all versions)", kind, group, version)
    return None, ""


def _parse_api_version(api_version: str) -> tuple[str, str]:
    """'apps/v1' → ('apps', 'v1'),  'v1' → ('', 'v1')"""
    if "/" in api_version:
        group, version = api_version.split("/", 1)
        return group, version
    return "", api_version


# ---------------------------------------------------------------------------
# Resource collection — unwraps nested ACM payloads
# ---------------------------------------------------------------------------

def _collect_resources(diff: str) -> list[tuple[dict, str]]:
    """
    Extract all resource dicts from the diff, including payloads embedded
    inside ACM Policy and ConfigPolicy wrappers.

    Returns list of (resource_dict, source_label).
    """
    top_docs = _parse_yaml_docs(diff)
    resources: list[tuple[dict, str]] = []

    for doc in top_docs:
        kind = doc.get("kind", "")
        name = doc.get("metadata", {}).get("name", "")
        log.debug("[collect] top-level kind=%s name=%s", kind, name)
        resources.append((doc, "top-level"))

        if kind == "Policy":
            for tpl in doc.get("spec", {}).get("policy-templates", []):
                inner = tpl.get("objectDefinition")
                if not isinstance(inner, dict):
                    continue
                inner_kind = inner.get("kind", "")
                inner_name = inner.get("metadata", {}).get("name", "")
                log.debug("[collect] unwrapped Policy→%s name=%s", inner_kind, inner_name)
                resources.append((inner, "Policy→ConfigPolicy"))

                if inner_kind == "ConfigPolicy":
                    for obj_tpl in inner.get("spec", {}).get("object-templates", []):
                        payload = obj_tpl.get("objectDefinition")
                        if not isinstance(payload, dict):
                            continue
                        p_kind = payload.get("kind", "")
                        p_name = payload.get("metadata", {}).get("name", "")
                        log.debug("[collect] unwrapped ConfigPolicy→%s name=%s", p_kind, p_name)
                        resources.append((payload, "ConfigPolicy→payload"))

    log.info("[schema] collected %d resource(s) to validate", len(resources))
    return resources


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_resource(doc: dict, source: str) -> SchemaFinding:
    kind        = doc.get("kind", "Unknown")
    name        = doc.get("metadata", {}).get("name", "(unnamed)")
    api_version = doc.get("apiVersion", "")
    prefix      = f"[schema {kind}/{name} src={source}]"

    if not api_version:
        log.debug("%s no apiVersion — skipping", prefix)
        return SchemaFinding(
            kind=kind, name=name, api_version="", source=source,
            valid=True, note="No apiVersion — skipping",
        )

    group, version = _parse_api_version(api_version)
    openapi_doc = _fetch_openapi_schema(group, version)

    if openapi_doc is None:
        note = f"Schema for {api_version} not available on this hub"
        if source == "ConfigPolicy→payload":
            note += " (managed-cluster resource — schema lives on spoke)"
        log.debug("%s %s", prefix, note)
        return SchemaFinding(
            kind=kind, name=name, api_version=api_version, source=source,
            valid=True, note=note,
        )

    kind_schema, matched_version = _find_kind_schema(openapi_doc, kind, group, version)

    if kind_schema is None:
        # Kind not found in any version's schema — definitely wrong apiVersion
        served = _fetch_group_versions(group)
        error_msg = (
            f"apiVersion {api_version!r} is not served by this hub for kind {kind!r}. "
            f"Hub serves {group!r} at: {', '.join(served) or 'none found'}. "
            "Fix the apiVersion before merging — the API server will reject this resource."
        )
        log.info("%s WRONG apiVersion: %s", prefix, error_msg)
        return SchemaFinding(
            kind=kind, name=name, api_version=api_version, source=source,
            valid=False, errors=[error_msg],
        )

    if matched_version != version:
        # Schema found in a fallback version — now check if the API server
        # actually serves this kind at the requested version (via API conversion).
        # If not, the PR has a wrong apiVersion — it will be rejected.
        if not _kind_served_at_version(group, version, kind):
            served_at = [
                v for v in _fetch_group_versions(group)
                if _kind_served_at_version(group, v, kind)
            ]
            error_msg = (
                f"apiVersion {api_version!r} is not served by this hub for kind {kind!r}. "
                f"This hub serves {kind!r} at: "
                f"{', '.join(f'{group}/{v}' for v in served_at) or 'unknown'}. "
                "Fix the apiVersion before merging."
            )
            log.info("%s WRONG apiVersion: %s", prefix, error_msg)
            return SchemaFinding(
                kind=kind, name=name, api_version=api_version, source=source,
                valid=False, errors=[error_msg],
            )
        # Kind IS served at the requested version (API conversion) — validate with note
        version_note = (
            f"validated against {group}/{matched_version} "
            f"(hub converts {version}→{matched_version} internally)"
        )
        log.debug("%s API conversion: %s→%s", prefix, version, matched_version)
    else:
        version_note = ""

    log.debug("%s running jsonschema validation", prefix)
    errors = []
    try:
        resolver = jsonschema.RefResolver.from_schema(openapi_doc)
        validator = jsonschema.Draft4Validator(kind_schema, resolver=resolver)

        for err in validator.iter_errors(doc):
            path = (
                " → ".join(str(p) for p in err.absolute_path)
                if err.absolute_path else "root"
            )
            msg = f"{path}: {err.message}"
            errors.append(msg)
            log.debug("%s error: %s", prefix, msg)

    except Exception as e:
        log.warning("%s jsonschema failed: %s", prefix, e)
        return SchemaFinding(
            kind=kind, name=name, api_version=api_version, source=source,
            valid=True, note=f"Validation could not run: {e}",
        )

    if errors:
        log.info("%s %d validation error(s)", prefix, len(errors))
    else:
        log.debug("%s valid", prefix)

    return SchemaFinding(
        kind=kind, name=name, api_version=api_version, source=source,
        valid=len(errors) == 0,
        errors=errors,
        note=version_note.strip("() ") if version_note and not errors else "",
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_findings(findings: list[SchemaFinding]) -> str:
    if not findings:
        return ""

    lines = ["**Schema validation:**"]
    has_errors = False

    for f in findings:
        label = f"`{f.kind}/{f.name}`"
        if f.source not in ("top-level",):
            label += f" *(from {f.source})*"

        if f.note:
            lines.append(f"  ⚪ {label} — {f.note}")
        elif f.valid:
            lines.append(f"  ✅ {label} — valid")
        else:
            has_errors = True
            lines.append(f"  ❌ {label} — {len(f.errors)} error(s):")
            for err in f.errors[:5]:
                lines.append(f"      • {err}")
            if len(f.errors) > 5:
                lines.append(f"      • … and {len(f.errors) - 5} more")

    if has_errors:
        lines.append(
            "\n  ⚠️ Schema errors are silently ignored at runtime — "
            "invalid fields cause policies to do nothing. Fix before merging."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate_schemas(diff: str) -> str:
    """
    Validate all YAML resources in the diff against the hub's OpenAPI schemas.
    Called by runner.py before the agent runs. Returns formatted findings string,
    or empty string if no YAML found or hub API unreachable.
    """
    log.debug("[schema] starting")

    try:
        resources = _collect_resources(diff)
    except Exception as e:
        log.warning("[schema] resource collection failed: %s", e)
        return ""

    if not resources:
        log.debug("[schema] no resources found")
        return ""

    schema_cache: dict[tuple[str, str], dict | None] = {}
    findings = []

    for doc, source in resources:
        api_version = doc.get("apiVersion", "")
        if not api_version:
            continue

        group, version = _parse_api_version(api_version)
        key = (group, version)
        if key not in schema_cache:
            schema_cache[key] = _fetch_openapi_schema(group, version)

        findings.append(_validate_resource(doc, source))

    result = _format_findings(findings)
    log.info("[schema] done — %d finding(s), errors=%s",
             len(findings), any(not f.valid for f in findings))
    return result
