from dataclasses import dataclass


@dataclass
class PRContext:
    repo_full_name: str
    pr_number: int
    pr_url: str
    comments_url: str
    title: str
    body: str | None
    author: str
    base_branch: str
    head_branch: str
    additions: int
    deletions: int
    changed_files: int
    draft: bool
    diff: str
