# PR Analyzer

Listens for GitHub PR events and posts AI-driven analysis as a PR comment. Runs as a container in Red Hat ACM hub (OpenShift).

## How it works

1. GitHub sends a webhook event when a PR is opened, updated, or marked ready for review
2. The app fetches the PR diff and metadata
3. An OpenAI Agents SDK agent runs configured skills against the PR
4. The analysis is posted back as a PR comment

Scope: **open PRs only**. No action is taken after a PR is merged.

---

## GitHub PAT permissions

The `GITHUB_TOKEN` secret must be a Personal Access Token with the following permissions:

### Fine-grained PAT (recommended)
Go to GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens

Under **Repository permissions**:
| Permission | Access | Reason |
|---|---|---|
| Pull requests | Write | Read PR data/diffs + post comments (PR comments share the Issues API but Write on Pull requests covers it) |

That's it — one permission only.

### Classic PAT
Go to GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)

| Scope | Reason |
|---|---|
| `repo` | Read PR data and post comments on private repos |
| `public_repo` | Sufficient if the target repo is public |

Fine-grained is preferred — it limits the token to specific repos and uses least-privilege permissions.

---

## Setup

### 1. Build and push the image
```bash
make build-push
```

### 2. Deploy to OpenShift
```bash
oc apply -f deploy/namespace.yaml
cp deploy/secret.template deploy/secret.yaml
# fill in real values in secret.yaml
oc apply -f deploy/secret.yaml
oc apply -f deploy/
```

### 3. Get the webhook URL
```bash
oc get route pr-analyzer -n change-first
```

### 4. Register the GitHub webhook
- Repo → Settings → Webhooks → Add webhook
- **Payload URL**: `https://<route-host>/webhook`
- **Content type**: `application/json`
- **Secret**: value you set as `GITHUB_WEBHOOK_SECRET`
- **SSL verification**: Disable if your cluster uses a self-signed or internal CA (common on dev clusters). Enable only if the Route has a publicly trusted certificate.
- **Events**: Pull requests only

---

## Local development

```bash
make venv
source .venv/bin/activate
cp .env.example .env   # fill in values
make run-local
make test
```

---

## Skills

Skills are the checks the agent runs on each PR. Enabled skills are controlled by `ENABLED_SKILLS` in the ConfigMap — set to `all` or a comma-separated list of function names.

| Skill | What it checks |
|---|---|
| `check_pr_description` | PR has a meaningful description (≥ 30 chars) |
| `check_diff_size` | Diff is not too large to review meaningfully |
| `check_draft_targeting_protected_branch` | Draft PRs not targeting main/master/release |

To add a new skill: create a file in `app/skills/`, define `@function_tool` functions, add an import to `app/skills/__init__.py`, rebuild the image.

---

## GitHub App (v2 upgrade path)

The current setup uses a PAT — comments appear as the token owner's account. To get a proper bot identity (`pr-analyzer[bot]`), register a GitHub App and swap the auth logic in `app/github/client.py`. No other code changes needed.
