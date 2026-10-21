import openai
from agents import Agent, OpenAIChatCompletionsModel, Runner

from app.agent.prompt import build_system_prompt, build_user_prompt
from app.config import get_settings
from app.github.models import PRContext
from app.skills import get_enabled_skills
from app.skills.acm_placement import PlacementCase, parse_placement
from app.skills.classifier import classify
from app.skills.schema_validator import validate_schemas


async def analyze_pr(ctx: PRContext) -> str:
    settings = get_settings()

    pr_types = classify(ctx.diff, [])

    # Deterministic blast radius — resolved before the agent runs
    blast_radius: str | None = None
    cluster_names: list[str] = []
    if settings.tier2_enabled:
        placement_result = parse_placement(ctx.diff)
        if placement_result.case != PlacementCase.ABSENT:
            from app.skills.acm_cluster import resolve_clusters, get_cluster_names
            blast_radius = resolve_clusters(placement_result)
            cluster_names = get_cluster_names(placement_result)

    # Schema validation — always runs
    schema_report = validate_schemas(ctx.diff)

    # Relationship checks — always run when tier2 enabled and clusters resolved.
    # Essential safety checks run deterministically; agent cannot skip them.
    relationship_report: str = ""
    if settings.tier2_enabled and cluster_names:
        from app.skills.acm_relationships import check_relationships_pre_agent
        relationship_report = check_relationships_pre_agent(ctx.diff, cluster_names)

    client = openai.AsyncOpenAI(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
    )

    skills = get_enabled_skills(settings.enabled_skills, tier2=settings.tier2_enabled)

    agent = Agent(
        name="pr-analyzer",
        instructions=build_system_prompt(pr_types, tier2_enabled=settings.tier2_enabled),
        tools=skills,
        model=OpenAIChatCompletionsModel(
            model=settings.model_name,
            openai_client=client,
        ),
    )

    result = await Runner.run(
        agent,
        input=build_user_prompt(
            ctx, pr_types,
            blast_radius=blast_radius,
            schema_report=schema_report,
            relationship_report=relationship_report,
        ),
        max_turns=settings.agent_max_turns,
    )

    return result.final_output or "No analysis produced."
