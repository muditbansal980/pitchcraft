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

    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(64))"
    jwt_secret_key: str | None = None
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 180
    auth_cookie_name: str = "access_token"
    # Set to true in production (HTTPS only).
    auth_cookie_secure: bool = False


settings = Settings()
