from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "PitchCraft API"
    database_url: str = "sqlite:///./pitchcraft.db"
    sql_echo: bool = False


settings = Settings()
