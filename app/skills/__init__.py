from app.skills.pr_basics import (
    check_diff_size,
    check_draft_targeting_protected_branch,
    check_pr_description,
)

_BASE_SKILLS = [
    check_pr_description,
    check_diff_size,
    check_draft_targeting_protected_branch,
]

_SKILL_MAP = {fn.name: fn for fn in _BASE_SKILLS}


def get_enabled_skills(enabled_skills_setting: str, tier2: bool = False) -> list:
    """Return the skill list filtered by the ENABLED_SKILLS config value.

    When tier2=True, Tier 2 live cluster tools are appended (resolve_placement,
    check_object_ownership, fetch_object_on_cluster).
    """
    if enabled_skills_setting.strip().lower() == "all":
        skills = list(_BASE_SKILLS)
    else:
        names = [n.strip() for n in enabled_skills_setting.split(",") if n.strip()]
        skills = [_SKILL_MAP[n] for n in names if n in _SKILL_MAP]

    if tier2:
        from app.skills.acm_cluster import ALL_TIER2_SKILLS
        from app.skills.acm_relationships import ALL_RELATIONSHIP_SKILLS
        skills = skills + ALL_TIER2_SKILLS + ALL_RELATIONSHIP_SKILLS

    return skills
