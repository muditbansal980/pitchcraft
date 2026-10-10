from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "PitchCraft API"
    database_url: str = "sqlite:///./pitchcraft.db"
    sql_echo: bool = False

    chroma_persist_dir: str = "./chroma_data"
    chroma_collection: str = "pitches"
    chroma_services_collection: str = "wldd_services"

    services_pdf_path: str = "../04-PitchCraft/Services Offered By WLDD.pdf"
    # Folder holding past_pitches.csv and decks/ (not in git).
    archive_dir: str = "../04-PitchCraft"

    tavily_api_key: str | None = None
    # Boosts results from this country for general (non-news) searches.
    research_country: str | None = "india"

    groq_api_key: str | None = None
    # Free-tier limit for this model is 8K tokens/minute (prompt + completion).
    groq_model: str = "openai/gpt-oss-120b"
    groq_reasoning_effort: str = "low"

    # Google AI Studio (Gemini): strategist, editor and image picking.
    google_api_key: str | None = None
    # Role -> ordered "provider:model" list (see app/services/llm_router.py). Groq limits are
    # per model and Gemini has its own quota, so spreading roles saves the free tiers.
    # Set as JSON in .env, e.g. LLM_ROLES='{"writer": ["groq:openai/gpt-oss-120b"]}'.
    llm_roles: dict[str, list[str]] = {
        "strategist": ["gemini:gemini-flash-latest", "groq:openai/gpt-oss-120b"],
        "writer": [
            "groq:openai/gpt-oss-120b",
            "groq:qwen/qwen3.8-27b",
            "groq:openai/gpt-oss-20b",
            "gemini:gemini-flash-lite-latest",
        ],
        "editor": ["gemini:gemini-flash-latest", "groq:openai/gpt-oss-120b"],
        "vision": ["gemini:gemini-flash-latest"],
    }
    # Optional per-model overrides of the router's built-in free-tier limits:
    # {"groq:openai/gpt-oss-120b": {"tpm": 8000, "rpm": 30, "tpd": 200000}}.
    llm_limits: dict[str, dict[str, int]] = {}

    # Reddit trend pulse (app-only OAuth; create a "script" app at reddit.com/prefs/apps).
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "pitchcraft/0.2 (meme trend research)"
    trend_subreddits: list[str] = ["IndianDankMemes", "indiameme", "bollywoodmemes", "IndiaCricket"]

    # Imgflip meme captioning (free account at imgflip.com).
    imgflip_username: str | None = None
    imgflip_password: str | None = None

    # AI mock visuals: "cloudflare" (Workers AI FLUX schnell), "pollinations" (no key) or "none".
    image_gen_provider: Literal["cloudflare", "pollinations", "none"] = "pollinations"
    # Anonymous Pollinations now returns 402 after ~1 image; a key (gen.pollinations.ai) lifts that.
    pollinations_api_key: str | None = None
    cloudflare_account_id: str | None = None
    cloudflare_api_token: str | None = None

    # Downloaded/generated images, served at /media.
    media_dir: str = "./media"

    # Canva Connect (integration at canva.dev, scope design:content:write).
    canva_client_id: str | None = None
    canva_client_secret: str | None = None
    # Must match a redirect URL registered on the integration (Canva rejects "localhost").
    canva_redirect_uri: str = "http://127.0.0.1:3000/api/canva/callback"

    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(64))"
    jwt_secret_key: str | None = None
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 180
    auth_cookie_name: str = "access_token"
    # Set to true in production (HTTPS only).
    auth_cookie_secure: bool = False


settings = Settings()
