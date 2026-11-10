from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    embedding_model: str = "gemini-embedding-001"

    jira_client_id: str = ""
    jira_client_secret: str = ""
    jira_redirect_uri: str = "http://localhost:8000/auth/jira/callback"

    backend_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:8501"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
