import logging

import groq
from fastapi import APIRouter, HTTPException, status
from langchain_core.exceptions import ModelAuthenticationError, ModelError, ModelRateLimitError
from tavily import InvalidAPIKeyError, UsageLimitExceededError

from app.api.deps import CurrentUser
from app.core.config import settings
from app.schemas.plan import BrandPlanRequest, BrandPlanResponse
from app.services.brand_plan import PlanGenerationError, ServicesCatalogEmptyError, generate_brand_plan
from app.services.llm_router import LLMOutputError, LLMUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["plan"])

_RATE_LIMIT_MESSAGE = (
    "Every AI model is at its free-tier limit right now (Groq allows ~8K tokens/minute per model). "
    "Try again in a minute."
)


@router.post("/brand-plan", response_model=BrandPlanResponse)
async def brand_plan(payload: BrandPlanRequest, _user: CurrentUser) -> BrandPlanResponse:
    brand = payload.brand.strip()
    if not brand:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Brand name is required")
    brief = (payload.brief or "").strip() or None
    if not settings.tavily_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "TAVILY_API_KEY is not configured")
    if not settings.groq_api_key and not settings.google_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No LLM key is configured (set GROQ_API_KEY or GOOGLE_API_KEY)")

    try:
        return await generate_brand_plan(brand, brief=brief, scale=payload.scale)
    except ServicesCatalogEmptyError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "WLDD services are not ingested yet (run `uv run python -m app.ingestion.services_pipeline`)",
        )
    except InvalidAPIKeyError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Tavily rejected the API key")
    except UsageLimitExceededError:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Tavily usage limit reached")
    except LLMUnavailableError as exc:
        logger.warning("Brand plan for %r: %s", brand, exc)
        if "No LLM is configured" in str(exc):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
        if "out of free-tier budget" in str(exc):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _RATE_LIMIT_MESSAGE)
        # Every model failed for other reasons (auth, outages): still nothing to retry right now.
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"{_RATE_LIMIT_MESSAGE} ({exc})")
    # The router handles provider errors itself; these only surface if something bypasses it.
    except (groq.RateLimitError, ModelRateLimitError):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _RATE_LIMIT_MESSAGE)
    except groq.AuthenticationError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Groq rejected the API key")
    except ModelAuthenticationError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The LLM provider (Groq/Gemini) rejected the API key")
    except groq.APIStatusError as exc:
        logger.exception("Groq request failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Plan generation failed (Groq {exc.status_code}): {exc.message}")
    except groq.APIError as exc:
        logger.exception("Groq request failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Plan generation failed (Groq unreachable): {exc}")
    except ModelError as exc:
        logger.exception("LLM request failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Plan generation failed (LLM error): {exc}")
    except (PlanGenerationError, LLMOutputError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Plan generation failed: {exc}")
    except Exception as exc:
        logger.exception("Brand plan failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Brand plan failed: {exc}")
