"""Runtime configuration, read from environment variables (or a local .env file)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Infrastructure -------------------------------------------------------
    database_url: str = "postgresql+asyncpg://chat:chat@postgres:5432/chat"
    ollama_base_url: str = "http://host.docker.internal:11434"
    searxng_url: str = "http://searxng:8080"
    static_dir: str = "/app/static"
    timezone: str = "America/Sao_Paulo"

    # --- Generation / context budget -----------------------------------------
    # Ollama's default num_ctx is small and silently truncates long prompts,
    # so we always send it explicitly. It is capped by the model's own limit.
    default_num_ctx: int = 8192
    response_reserve_tokens: int = 1024
    web_context_tokens: int = 2500
    keep_alive: str = "15m"

    # --- Web search pipeline --------------------------------------------------
    embed_model: str = "nomic-embed-text"
    embed_query_prefix: str = "search_query: "
    embed_doc_prefix: str = "search_document: "
    search_max_results: int = 8
    search_fetch_pages: int = 5
    search_top_chunks: int = 6
    # Drop passages scoring below this fraction of the best passage's score.
    search_min_relative_score: float = 0.6
    fetch_timeout_s: float = 8.0
    fetch_max_bytes: int = 2_000_000

    # --- Model catalog (auto-generated descriptions) --------------------------
    catalog_enabled: bool = True
    catalog_interval_s: int = 60
    # When true, the model's public page (ollama.com / huggingface.co) is fetched
    # to ground the description. Only the model name leaves the server.
    catalog_fetch_remote: bool = True
    describer_model: str = ""
    description_language: str = "English"


settings = Settings()
