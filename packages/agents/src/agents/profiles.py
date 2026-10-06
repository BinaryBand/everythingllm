"""The roles a delegated task can have. Each is an AnythingLLM workspace whose name starts
with `agents-`, so every skill of ours that writes, acts or delegates refuses it
(anythingllm/agent-skills/_lib/delegated.js); its model and system prompt are set here and
kept so by `ensure`, through the developer API.

  planner   plans, reviews and writes up: GLM 5.3 on Z.AI (AnythingLLM's generic-openai)
  worker    searches and reads: GLM 5 Turbo, also on Z.AI, so a reading-heavy task draws
            on the GLM plan rather than DeepSeek's per-token price. Of the plan's models it
            thinks least (465 thinking tokens for a 250-word answer, where 5.3 Flash spent
            2,156), so it's the quickest and leaves the most of AnythingLLM's reply cap
            (GENERIC_OPEN_AI_MAX_TOKENS, 4,096 here) for the answer.
"""

from dataclasses import dataclass
from pathlib import Path

from agents.anythingllm import AnythingLLM

PROMPTS = Path(__file__).with_name("prompts")


@dataclass(frozen=True)
class Profile:
    workspace: str  # its name and slug
    provider: str  # AnythingLLM's name for the model's provider
    model: str

    def prompt(self, role: str) -> str:
        return (PROMPTS / f"{role}.md").read_text()

    def settings(self, role: str) -> dict:
        return {
            "chatProvider": self.provider,
            "chatModel": self.model,
            "agentProvider": self.provider,
            "agentModel": self.model,
            "openAiPrompt": self.prompt(role),
        }


PROFILES = {
    "planner": Profile("agents-planner", "generic-openai", "glm-5.3"),
    "worker": Profile("agents-worker", "generic-openai", "glm-5-turbo"),
}


async def ensure(client: AnythingLLM) -> None:
    """Make every profile's workspace if it's missing, and set its model and prompt."""
    have = {w.get("slug") for w in await client.workspaces()}
    for role, profile in PROFILES.items():
        slug = profile.workspace
        if slug not in have:
            slug = (await client.workspace_new(profile.workspace))["slug"]
            if slug != profile.workspace:
                raise RuntimeError(
                    f"AnythingLLM named the {role} workspace '{slug}', not '{profile.workspace}'"
                )
        await client.workspace_update(slug, profile.settings(role))
