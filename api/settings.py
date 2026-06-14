from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    database_url: str = "postgresql+psycopg://dealfinder:dealfinder@localhost:5432/dealfinder"
    redis_url: str = "redis://localhost:6379/0"

    ebay_env: str = "sandbox"
    ebay_client_id: str = ""
    ebay_client_secret: str = ""


settings = Settings()
