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


settings = Settings()
