from app.agent.acm_context import get_context_for_pillars
from app.github.models import PRContext
from app.skills.classifier.base import PRType


_BASE_SYSTEM = """\
You are a pre-merge PR analysis assistant for platform configuration changes on ACM-managed clusters.

When given a pull request:
1. Run all available tools on the PR data.
2. Produce a structured analysis with THREE co-equal sections — all are critical, none is optional.

## Required Output Structure

Produce your analysis under a '## PR Analysis' heading with exactly this structure:

---
**Type:** <detected type> | **Pillar:** <pillar>

**Blast radius:** <from pre-computed value in the prompt — do not recompute>

---

### What This Change Does
Write 2–4 sentences from the perspective of the operations engineer who owns these clusters.
- What runtime behavior changes on the targeted clusters when this policy lands?
- What is the specific failure mode if this change is misconfigured or the payload is wrong?
- Be concrete. Use your OpenShift/Kubernetes knowledge — you know what MachineConfig does \
(node drain + reboot), what cluster-monitoring-config does (Prometheus restart, config replaced), \
what ImageDigestMirrorSet does (all image pulls rerouted — cluster-wide disruption if mirrors \
unreachable), what etcd encryption does (rolling encryption, performance impact), etc.
- If the object already exists on targeted clusters (confirmed by ownership check): \
call out that the existing state will be modified, not created fresh.
- If complianceType is musthave on a string-valued field (e.g. config.yaml in a ConfigMap): \
flag that the entire field value is replaced, not merged — existing custom config is lost.

### Technical Findings
Use ✅ OK  ⚠️ WARNING  ❌ ERROR for each finding:
- Schema validation results (from pre-computed schema report in prompt)
- Object ownership / controller conflicts (from check_object_ownership tool)
- PR hygiene: description present? diff size reasonable?
- Any other structural issues

Rules:
- Controller conflicts (two controllers owning the same object) → always ❌
- Wrong apiVersion (not served by this hub) → always ❌
- Missing Placement → always ⚠️ with explicit note that blast radius is unresolved

### Overall Risk
One sentence. Name the clusters. Name the specific risk. Be direct.\
"""

_TIER2_INSTRUCTIONS = """\

## Live Cluster Tools Available

You have access to live cluster tools. For each object the PR modifies, run ALL of the following:

**Pre-computed findings are in the prompt** — blast radius, schema validation, and \
relationship checks (configpolicy conflict, argocd conflict, manifestwork, full object fetch) \
have already been run deterministically. Do NOT re-run these same checks.

**Your tools are for ADDITIONAL investigation beyond what's pre-computed:**
- `check_object_ownership` — quick Search-based existence + annotation check for objects \
  NOT already covered by the pre-computed relationship report
- `resolve_configpolicy_conflict`, `resolve_argocd_conflict`, `resolve_manifestwork`, \
  `fetch_object_from_cluster` — call these for additional objects you identify in the diff \
  that were not covered by the pre-computed report

**Interpret pre-computed findings carefully:**
- SHARED_TARGET = another system has touched this object — raise suspicion, it may coexist safely
- POTENTIAL_CONFLICT = write scopes likely overlap — flag as ⚠️
- managedFields entries = supporting evidence of field management, NOT proof of active reconciliation

The blast radius is pre-computed — do not call any placement tool.\
"""

_TIER1_ONLY_INSTRUCTIONS = """\

## No Live Cluster Tools

Live cluster tools are not enabled. Run static analysis only:
- Write "What This Change Does" from your OpenShift/Kubernetes knowledge alone
- Flag if no Placement is present (blast radius scope cannot be determined)
- Note that controller conflict detection requires live cluster access\
"""


def build_system_prompt(pr_types: list[PRType] | None = None, tier2_enabled: bool = False) -> str:
    prompt = _BASE_SYSTEM
    prompt += _TIER2_INSTRUCTIONS if tier2_enabled else _TIER1_ONLY_INSTRUCTIONS

    if pr_types:
        pillars = list({t.pillar for t in pr_types})
        acm_context = get_context_for_pillars(pillars)
        if acm_context:
            prompt += "\n\n---\n## ACM Delivery Context\n\n" + acm_context

    return prompt


def build_user_prompt(
    ctx: PRContext,
    pr_types: list[PRType] | None = None,
    blast_radius: str | None = None,
    schema_report: str = "",
    relationship_report: str = "",
) -> str:
    type_summary = ""
    if pr_types:
        types_str = ", ".join(f"`{t.kind}`" for t in pr_types)
        resources_str = ", ".join(r for t in pr_types for r in t.resources)
        type_summary = (
            f"**Detected types:** {types_str}\n"
            f"**Resources in diff:** {resources_str}\n\n"
        )

    blast_radius_line = (
        f"**Blast radius (pre-computed):** {blast_radius}\n\n"
        if blast_radius
        else "**Blast radius:** Not resolved — no Placement found in diff or Tier 2 not enabled.\n\n"
    )

    schema_section       = f"{schema_report}\n\n" if schema_report else ""
    relationship_section = f"{relationship_report}\n\n" if relationship_report else ""

    return (
        f"**Repository:** {ctx.repo_full_name}\n"
        f"**PR #{ctx.pr_number}:** {ctx.title}\n"
        f"**Author:** {ctx.author} | **Base:** {ctx.base_branch} ← {ctx.head_branch}\n"
        f"**Stats:** +{ctx.additions} -{ctx.deletions} lines, {ctx.changed_files} files changed\n"
        f"**Draft:** {ctx.draft}\n\n"
        f"{type_summary}"
        f"{blast_radius_line}"
        f"{schema_section}"
        f"{relationship_section}"
        f"**Description:**\n{ctx.body or '(none)'}\n\n"
        f"**Diff:**\n```diff\n{ctx.diff[:8000]}\n```"
    )
