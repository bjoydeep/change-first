import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.config import get_settings
from app.security import verify_signature
from app.webhook.events import PullRequestEvent

logger = logging.getLogger(__name__)

router = APIRouter()

# Actions on open PRs that are worth analyzing
HANDLED_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review", "edited"}


@router.post("/webhook", status_code=202)
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(None),
    x_hub_signature_256: str | None = Header(None),
):
    payload = await request.body()
    settings = get_settings()

    if not verify_signature(payload, x_hub_signature_256, settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    if x_github_event == "pull_request":
        data = await request.json() if not payload else __import__("json").loads(payload)
        event = PullRequestEvent.model_validate(data)

        if event.action not in HANDLED_ACTIONS:
            return {"status": "ignored", "action": event.action}

        if event.pull_request.merged or event.pull_request.draft and event.action != "ready_for_review":
            return {"status": "ignored", "reason": "merged or unready draft"}

        background_tasks.add_task(_process_pr, event)

    return {"status": "accepted"}


async def _process_pr(event: PullRequestEvent) -> None:
    from app.agent.runner import analyze_pr
    from app.github.client import GitHubClient
    from app.github.models import PRContext

    settings = get_settings()
    client = GitHubClient(settings.github_token)
    pr = event.pull_request
    repo_name = event.repository.get("full_name", "")

    try:
        diff = await client.fetch_diff(pr.diff_url)
        ctx = PRContext(
            repo_full_name=repo_name,
            pr_number=event.number,
            pr_url=pr.html_url,
            comments_url=pr.comments_url,
            title=pr.title,
            body=pr.body,
            author=pr.user.get("login", "unknown"),
            base_branch=pr.base.ref,
            head_branch=pr.head.ref,
            additions=pr.additions,
            deletions=pr.deletions,
            changed_files=pr.changed_files,
            draft=pr.draft,
            diff=diff,
        )
        analysis = await analyze_pr(ctx)
        await client.post_comment(pr.comments_url, analysis)
    except Exception:
        logger.exception("Failed to process PR #%s in %s", event.number, repo_name)
