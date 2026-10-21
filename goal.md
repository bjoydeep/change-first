# Goal of this repo

1. When a user makes a change to their repo and tries to push/merge a PR, the software that we create here needs to be called. 
1. Once this code (Github handler) is called - its passed the full contents of the PR.
1. The code will call an AI agent with the contents to do some analysis. See AI Agent Analysis section below.
1. Once AI agent gives results of the analysis back to the Git handler, it pushes it back to the PR for the user to take action.
1. This is the minimal loop. The user, after reading the analysis, may once again invoke the handler by `@` etc — those interactive flows are not yet fully designed.

## Scope Boundary

- **In scope**: Any activity on an open PR before it is merged — open, push to branch, comment, review submission, re-request, etc.
- **Out of scope**: Post-merge. Once a PR is merged, this system has no role. A separate agent will pick up when the change is actually deployed and track it from there.

## Github Handler

- **Platform**: GitHub only (GitLab is out of scope for now).
- Runs as a container in Red Hat ACM hub.
- Listens for GitHub webhook events on open PRs (all pre-merge activity).
- Calls the AI agent with PR contents and posts the analysis back as a PR comment.

### How GitHub calls this app

**v1 (first pass): Repository or Org Webhook + PAT**
- In GitHub repo/org → Settings → Webhooks → Add webhook
- Payload URL = the OCP Route URL (e.g. `https://pr-analyzer.apps.your-cluster.com/webhook`)
- Secret = value stored in `GITHUB_WEBHOOK_SECRET` (used for HMAC-SHA256 signature verification)
- Events: Pull requests, Pull request reviews
- The app posts comments back using a Personal Access Token (`GITHUB_TOKEN`) — comments appear as the token owner's account

**v2 (production): GitHub App**
- Register a GitHub App with its own identity; install it org-wide
- Uses App ID + Private Key → JWT → Installation Token flow for API calls
- Comments appear as `app-name[bot]` — clean bot identity, not tied to any user account
- Code change is isolated to `github/client.py` auth logic only; everything else stays the same

## AI Agent Analysis

- Analysis is **skill-driven** — the specific checks the agent performs are defined as pluggable skills/tools. The exact skill set will be designed iteratively.
- The agent may also connect to **MCP servers** to pull in additional context (e.g., cluster state, policy databases).
- **SDK**: [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) (open source, MIT). Chosen because:
  - Works with any OpenAI-compatible API endpoint — models can be hosted anywhere, no lock-in.
  - Native tool/function calling maps cleanly to the skills model.
  - First-class MCP server support.
  - Simpler and more focused than alternatives (LangGraph, CrewAI) for this use case.
- **Models**: Hosted externally (model provider is separate and interchangeable).

## Layout

These can all be a part of the same container. But keep in mind that there may be different ways of calling the AI system, or we may need to interface with other AI systems in the future. For a first pass, a single container keeps setup simple.
