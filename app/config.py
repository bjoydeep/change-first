from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # GitHub
    github_webhook_secret: str
    github_token: str

    # AI model endpoint (OpenAI-compatible)
    openai_base_url: str
    openai_api_key: str
    model_name: str = "gpt-4o"
    agent_max_turns: int = 10

    # Skills — "all" or comma-separated function names
    enabled_skills: str = "all"

    # Tier 2 — live cluster tools (requires in-cluster ServiceAccount with ACM RBAC)
    # Defaults to false; set true when running on the ACM hub cluster
    tier2_enabled: bool = False

    # Log level specifically for Tier 2 cluster tool functions (acm_cluster.py)
    # Set to "debug" to see all Search API queries, raw responses, ownership parsing
    tier2_log_level: str = "info"

    # Server
    log_level: str = "info"


@lru_cache
def get_settings() -> Settings:
    return Settings()
