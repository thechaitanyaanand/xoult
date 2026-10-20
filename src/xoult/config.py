from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings

# Load .env into os.environ (not just into Settings) so provider SDKs like LiteLLM
# can find their API keys. Real environment variables still win.
load_dotenv()

DEV_SECRET = "dev-only-not-secret"


class Settings(BaseSettings):
    database_url: str = "postgresql://xoult:xoult@localhost:5433/xoult"
    sentry_dsn: str | None = None

    # Signs the session cookie. Anonymous users exist only through that cookie, so
    # rotating this key logs everyone out and strands their saves; set it once.
    secret_key: str = DEV_SECRET
    cookie_secure: bool = False  # true in production, where everything is HTTPS
    google_client_id: str | None = None  # sign-in stays hidden until this is set
    # Canonical address for links, previews, robots.txt and the sitemap. Unset, the
    # request's own host is used, which is fine locally.
    public_url: str | None = None

    # Wakes the ingest workflow to embed papers readers add by hand. A fine-grained token
    # for this one repo with "Actions: read and write". Unset, added papers wait for the
    # next scheduled run.
    github_token: str | None = None
    github_repo: str = "thechaitanyaanand/xoult"

    arxiv_categories: list[str] = ["cs.CL", "cs.IR", "cs.LG", "cs.AI", "cs.CV", "stat.ML"]

    # Picked by evals/rank_eval.py: best NDCG@10 of the 384-dim models tried, and the
    # fastest. Changing it means re-embedding every paper (ingest --reembed).
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # Tried in order; a model that is rate limited or broken is skipped for a while (llm.py).
    # Free tiers first, fastest first, alternating providers so one outage never blocks
    # two steps in a row. xAI bills per token, so it is the last resort.
    llm_models: list[str] = [
        "groq/openai/gpt-oss-20b",
        "gemini/gemini-3.5-flash-lite",
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
        "gemini/gemini-3.5-flash",
        "openrouter/google/gemma-4-31b-it:free",
        "meta_llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
        "xai/grok-4.20-non-reasoning",
    ]
    llm_rpm: int = 25
    tldrs_per_run: int = 300

    # Papers nobody saved or dismissed are deleted after this many days. The feed looks
    # back 7; at ~4.5 KB a paper this keeps the database near 190 MB of the free 500.
    keep_papers_days: int = 30

    @field_validator("secret_key")
    @classmethod
    def _empty_means_dev(cls, value: str) -> str:
        return value or DEV_SECRET  # `SECRET_KEY=` copied from .env.example


settings = Settings()
