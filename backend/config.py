from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openai_api_key: str = ""
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    collection_name: str = "credit_card_knowledge"  # override with env COLLECTION_NAME
    embedding_provider: str = "openai"  # override with env EMBEDDING_PROVIDER (only openai supported)
    embed_model: str = "text-embedding-3-small"  # override with env EMBED_MODEL (OpenAI: 1536 dims)
    chat_model: str = "gpt-4o"
    chat_temperature: float = 0.3  # lower = more deterministic, better for FAQ
    chat_max_tokens: int = 600
    # Query rewrite: small model turns conversational queries into clear search phrases before RAG (e.g. "suggest me cards" → "credit card options benefits")
    enable_query_rewrite: bool = True
    query_rewrite_model: str = "gpt-4o-mini"  # cheap/fast for rewrite step
    retrieval_score_threshold: float = 0.55
    chunk_size: int = 700
    chunk_overlap: int = 150
    top_k: int = 10

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
