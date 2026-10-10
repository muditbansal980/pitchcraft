import logging

import groq
from fastapi import APIRouter, HTTPException, status
from tavily import InvalidAPIKeyError, UsageLimitExceededError

from app.api.deps import CurrentUser
from app.core.config import settings
from app.schemas.plan import BrandPlanRequest, BrandPlanResponse
from app.services.brand_plan import PlanGenerationError, ServicesCatalogEmptyError, generate_brand_plan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["plan"])


@router.post("/brand-plan", response_model=BrandPlanResponse)
async def brand_plan(payload: BrandPlanRequest, _user: CurrentUser) -> BrandPlanResponse:
    brand = payload.brand.strip()
    if not brand:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Brand name is required")
    if not settings.tavily_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "TAVILY_API_KEY is not configured")
    if not settings.groq_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GROQ_API_KEY is not configured")

    try:
        return await generate_brand_plan(brand)
    except ServicesCatalogEmptyError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "WLDD services are not ingested yet (run `uv run python -m app.ingestion.services_pipeline`)",
        )
    except InvalidAPIKeyError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Tavily rejected the API key")
    except UsageLimitExceededError:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Tavily usage limit reached")
    except groq.RateLimitError:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Groq rate limit reached (free tier: 8K tokens/minute). Try again in a minute.",
        )
    except groq.AuthenticationError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Groq rejected the API key")
    except groq.APIStatusError as exc:
        logger.exception("Groq request failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Plan generation failed (Groq {exc.status_code}): {exc.message}")
    except groq.APIError as exc:
        logger.exception("Groq request failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Plan generation failed (Groq unreachable): {exc}")
    except PlanGenerationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Plan generation failed: {exc}")
    except Exception as exc:
        logger.exception("Brand plan failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Brand plan failed: {exc}")
