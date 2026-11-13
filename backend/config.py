from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Ordered failover chain of "provider:model" entries, e.g.
    # "gemini:gemini-3.6-flash,openai:gpt-4.1-mini,anthropic:claude-sonnet-4-5".
    # Empty means: gemini_model first, then the Gemini Lite fallbacks.
    llm_chain: str = ""
    llm_timeout_seconds: float = 20.0
    llm_temperature: float = 0.1
    llm_circuit_failures: int = 2
    llm_circuit_cooldown_seconds: float = 60.0

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    anthropic_api_key: str = ""

    embedding_model: str = "gemini-embedding-001"

    jira_client_id: str = ""
    jira_client_secret: str = ""
    jira_redirect_uri: str = "http://localhost:8000/auth/jira/callback"

    backend_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:8501"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
