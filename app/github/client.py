import httpx

GITHUB_API = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def fetch_diff(self, diff_url: str) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                diff_url,
                headers={**self._headers, "Accept": "application/vnd.github.v3.diff"},
                follow_redirects=True,
            )
            resp.raise_for_status()
            return resp.text

    async def post_comment(self, comments_url: str, body: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(comments_url, headers=self._headers, json={"body": body})
            resp.raise_for_status()
