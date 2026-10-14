from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# Load .env into os.environ (not just into Settings) so provider SDKs like LiteLLM
# can find their API keys. Real environment variables still win.
load_dotenv()


class Settings(BaseSettings):
    database_url: str = "postgresql://xoult:xoult@localhost:5433/xoult"
    sentry_dsn: str | None = None

    arxiv_categories: list[str] = ["cs.CL", "cs.IR", "cs.LG"]

    # Picked by evals/rank_eval.py: best NDCG@10 of the 384-dim models tried, and the
    # fastest. Changing it means re-embedding every paper (ingest --reembed).
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # First model is primary, the rest are fallbacks in order.
    llm_models: list[str] = ["groq/openai/gpt-oss-20b", "gemini/gemini-3.5-flash-lite"]
    llm_rpm: int = 25
    tldrs_per_run: int = 300


settings = Settings()
