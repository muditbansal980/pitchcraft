from fastapi import APIRouter, HTTPException, status
from tavily import InvalidAPIKeyError, UsageLimitExceededError

from app.core.config import settings
from app.schemas.brand import BrandInfoRequest, BrandInfoResponse
from app.services.brand_research import research_brand

router = APIRouter(prefix="/api", tags=["brand"])


@router.post("/brand-info", response_model=BrandInfoResponse)
async def brand_info(payload: BrandInfoRequest) -> BrandInfoResponse:
    brand = payload.brand.strip()
    if not brand:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Brand name is required")
    if not settings.tavily_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "TAVILY_API_KEY is not configured")

    try:
        return await research_brand(brand)
    except InvalidAPIKeyError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Tavily rejected the API key")
    except UsageLimitExceededError:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Tavily usage limit reached")
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Web research failed: {exc}")
