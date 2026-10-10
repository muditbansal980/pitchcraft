"""Multi-provider LLM router (WORKPLAN C6). Owner: Agent A.

Roles ("strategist", "writer", "editor", "vision") map to ordered
"provider:model" lists in ``settings.llm_roles`` (providers: ``groq``,
``gemini``). Groq rate limits are per model and Gemini has its own quota, so
the router spreads calls across models: each model gets its own token budget
(sliding 60 s window + requests/min + tokens/day), a model that is out of budget
or returns 429 is skipped in favour of the next one in the role list, and the
router only waits when every model for the role is busy.

Callers never pick a model directly; they ask for a role.
"""

from typing import Any, TypeVar

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMUnavailableError(RuntimeError):
    """No model for the role is configured, or every model is exhausted/failing."""


async def invoke_text(
    role: str,
    messages: list[BaseMessage],
    *,
    max_tokens: int,
    temperature: float = 0.7,
    prefer: int = 0,
) -> str:
    """Run ``messages`` on the first available model for ``role``; return the text.

    ``prefer`` rotates the start of the role's model list, so parallel writer
    groups (prefer=0, 1, 2) land on different models.
    """
    raise NotImplementedError


async def invoke_json(
    role: str,
    messages: list[BaseMessage],
    schema: type[T],
    *,
    max_tokens: int,
    temperature: float = 0.7,
    prefer: int = 0,
) -> T:
    """Like ``invoke_text`` but in JSON mode, validated into ``schema`` (one repair retry)."""
    raise NotImplementedError


async def invoke_vision_json(prompt: str, images: list[tuple[bytes, str]]) -> dict[str, Any] | None:
    """Ask the "vision" role about ``images`` (bytes, mime type); return parsed JSON.

    Returns None when no vision model is configured or available, so callers can
    fall back to a non-LLM choice.
    """
    return None
