import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

SECRET = "test-secret"


def _sig(payload: bytes) -> str:
    digest = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")

    # clear lru_cache so patched env vars take effect
    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import app
    return TestClient(app)


def _pr_payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "number": 42,
        "pull_request": {
            "html_url": "https://github.com/org/repo/pull/42",
            "title": "Test PR",
            "body": "A meaningful description of the change.",
            "diff_url": "https://github.com/org/repo/pull/42.diff",
            "comments_url": "https://api.github.com/repos/org/repo/issues/42/comments",
            "additions": 10,
            "deletions": 5,
            "changed_files": 2,
            "head": {"ref": "feature/foo", "sha": "abc"},
            "base": {"ref": "main", "sha": "def"},
            "draft": False,
            "merged": False,
            "user": {"login": "testuser"},
        },
        "repository": {"full_name": "org/repo"},
    }


def test_valid_pr_opened(client):
    payload = json.dumps(_pr_payload("opened")).encode()
    resp = client.post(
        "/webhook",
        content=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sig(payload),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 202


def test_invalid_signature(client):
    payload = json.dumps(_pr_payload()).encode()
    resp = client.post(
        "/webhook",
        content=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=badhash",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_ignored_action(client):
    payload = json.dumps(_pr_payload("assigned")).encode()
    resp = client.post(
        "/webhook",
        content=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sig(payload),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"


def test_unknown_event_type(client):
    payload = b'{"zen": "hello"}'
    resp = client.post(
        "/webhook",
        content=payload,
        headers={
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": _sig(payload),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 202


def test_healthz(client):
    assert client.get("/healthz").status_code == 200
