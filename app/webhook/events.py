from pydantic import BaseModel


class BranchRef(BaseModel):
    ref: str
    sha: str


class PullRequestPayload(BaseModel):
    html_url: str
    title: str
    body: str | None = None
    diff_url: str
    comments_url: str
    additions: int
    deletions: int
    changed_files: int
    head: BranchRef
    base: BranchRef
    draft: bool = False
    merged: bool = False
    user: dict


class PullRequestEvent(BaseModel):
    action: str
    number: int
    pull_request: PullRequestPayload
    repository: dict


class PullRequestReviewEvent(BaseModel):
    action: str
    number: int = 0
    pull_request: PullRequestPayload
    repository: dict
