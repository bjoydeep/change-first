from agents import function_tool


def _check_pr_description(body: str, title: str) -> str:
    """Check whether the PR has a substantive description explaining the change."""
    if not body or len(body.strip()) < 30:
        return (
            "ERROR: PR has no description or a very short one. "
            "A good description explains the motivation and approach."
        )
    return "OK: PR has a description."


def _check_diff_size(additions: int, deletions: int, changed_files: int) -> str:
    """Check whether the PR diff is small enough to be meaningfully reviewed."""
    total = additions + deletions
    if total > 1000 or changed_files > 30:
        return (
            f"WARNING: Large PR — {total} lines changed across {changed_files} files. "
            "Consider splitting into smaller PRs."
        )
    if total > 500 or changed_files > 15:
        return (
            f"CAUTION: Medium-large PR — {total} lines, {changed_files} files. "
            "Reviewers should budget extra time."
        )
    return f"OK: Manageable size ({total} lines, {changed_files} files)."


def _check_draft_targeting_protected_branch(draft: bool, base_branch: str) -> str:
    """Check whether a draft PR is targeting a typically protected branch."""
    protected = {"main", "master", "release", "production", "prod"}
    if draft and base_branch.lower() in protected:
        return (
            f"WARNING: Draft PR targeting '{base_branch}'. "
            "Draft PRs must not be merged. Confirm this is intentional."
        )
    return "OK: Draft/branch targeting looks fine."


check_pr_description = function_tool(_check_pr_description)
check_diff_size = function_tool(_check_diff_size)
check_draft_targeting_protected_branch = function_tool(_check_draft_targeting_protected_branch)
