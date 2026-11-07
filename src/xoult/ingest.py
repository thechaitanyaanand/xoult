import argparse
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from xoult import bluesky, llm
from xoult.arxiv import clean as _clean
from xoult.config import settings
from xoult.db import get_pool
from xoult.embed import document_text, embed

log = logging.getLogger("xoult.ingest")

# The daily announcement feed, not the search API: one request covers every category,
# and export.arxiv.org rate-limits shared IPs like CI runners within a few requests.
RSS_URL = "https://rss.arxiv.org/rss/{}"
NS = {"arxiv": "http://arxiv.org/schemas/atom", "dc": "http://purl.org/dc/elements/1.1/"}
# "replace" items are revisions of old papers; treating them as new would resurface them.
KEEP = {"new", "cross"}


@dataclass
class Paper:
    id: str
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    published_at: datetime


def parse_feed(xml: str) -> list[Paper]:
    papers = []
    for item in ET.fromstring(xml).iter("item"):
        if item.findtext("arxiv:announce_type", "", NS) not in KEEP:
            continue
        link = item.findtext("link", "")
        _, _, abstract = item.findtext("description", "").partition("Abstract:")
        authors = item.findtext("dc:creator", "", NS)
        papers.append(
            Paper(
                id=re.sub(r"v\d+$", "", link.rsplit("/abs/", 1)[-1]),
                title=_clean(item.findtext("title")),
                abstract=_clean(abstract),
                authors=[_clean(a) for a in authors.split(",") if a.strip()],
                categories=[c.text for c in item.iterfind("category") if c.text],
                published_at=parsedate_to_datetime(item.findtext("pubDate", "")),
            )
        )
    return papers


def fetch_feed(client: httpx.Client, attempts: int = 5) -> str:
    url = RSS_URL.format("+".join(settings.arxiv_categories))
    for attempt in range(attempts):
        response = client.get(url)
        if response.status_code not in (429, 503) or attempt == attempts - 1:
            response.raise_for_status()
            return response.text
        retry_after = response.headers.get("retry-after", "")
        delay = int(retry_after) if retry_after.isdigit() else 10 * 2**attempt
        log.warning("arXiv returned %d, retrying in %ds", response.status_code, delay)
        time.sleep(delay)
    raise AssertionError("unreachable")


INSERT = """
insert into papers (id, title, abstract, authors, categories, published_at)
values (%(id)s, %(title)s, %(abstract)s, %(authors)s, %(categories)s, %(published_at)s)
on conflict (id) do nothing
"""


def store_new_papers() -> int:
    # The feed only holds today's announcements, so a skipped day is lost. Backfill
    # from the OAI-PMH endpoint if cron reliability ever becomes a problem.
    with httpx.Client(transport=httpx.HTTPTransport(retries=3), timeout=120) as client:
        papers = parse_feed(fetch_feed(client))
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(INSERT, [vars(p) for p in papers])
        new = cur.rowcount  # summed over the batch; papers already stored count 0
    log.info("feed had %d papers, %d new", len(papers), new)
    return new


# Upvotes and code links from Hugging Face's daily papers list: a "people are talking
# about this" signal and the repo, for the papers HF features (a few dozen a day, mostly
# ML). The endpoint isn't in HF's documented API, so any failure just skips the step.
HF_URL = "https://huggingface.co/api/daily_papers"


def parse_hf(items: list[dict]) -> dict[str, tuple[int, str | None]]:
    """arXiv id -> (upvotes, code link). Links come from a third party: https only."""
    out = {}
    for item in items:
        paper = item.get("paper") or {}
        if not paper.get("id"):
            continue
        links = [paper.get("githubRepo"), paper.get("projectPage")]
        code = next((u for u in links if isinstance(u, str) and u.startswith("https://")), None)
        out[paper["id"]] = (int(paper.get("upvotes") or 0), code)
    return out


def hf_signals(days: int = 7) -> int:
    # Upvotes keep growing for days after a paper is listed, so re-read the last week.
    try:
        found: dict[str, tuple[int, str | None]] = {}
        with httpx.Client(timeout=30) as client:
            for back in range(days):
                day = datetime.now(UTC).date() - timedelta(days=back)
                response = client.get(HF_URL, params={"date": day.isoformat(), "limit": 100})
                response.raise_for_status()
                found |= parse_hf(response.json())
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "update papers set hf_upvotes = %s, code_url = coalesce(%s, code_url) "
                "where id = %s",
                [(votes, code, pid) for pid, (votes, code) in found.items()],
            )
            updated = cur.rowcount
    except Exception as e:
        log.warning("skipped Hugging Face signals: %s", e)
        return 0
    log.info("Hugging Face listed %d papers, %d of them here", len(found), updated)
    return updated


def embed_missing(batch_size: int = 256) -> int:
    total = 0
    while True:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "select id, title, abstract from papers where embedding is null limit %s",
                (batch_size,),
            ).fetchall()
            if not rows:
                return total
            vectors = embed([document_text(title, abstract) for _, title, abstract in rows])
            with conn.cursor() as cur:
                cur.executemany(
                    "update papers set embedding = %s where id = %s",
                    [(v, row[0]) for v, row in zip(vectors, rows, strict=True)],
                )
        total += len(rows)
        log.info("embedded %d papers", total)


# The free LLM quota covers a few hundred TLDRs a day, fewer than arXiv announces, so
# spend it where readers will look: papers already shown in someone's feed or saved
# into their library by hand, then papers in categories someone follows, then the newest.
NEEDS_TLDR = """
select p.id, p.title, p.abstract from papers p
where p.tldr is null
order by
    exists (select 1 from events e where e.paper_id = p.id and e.kind in ('impression', 'save'))
        desc,
    p.categories && (select coalesce(array_agg(distinct c), '{}') from users, unnest(categories) c)
        desc,
    p.published_at desc
limit %s
"""


def summarize_missing(limit: int) -> int:
    with get_pool().connection() as conn:
        rows = conn.execute(NEEDS_TLDR, (limit,)).fetchall()

    done = 0
    for paper_id, title, abstract in rows:
        started = time.monotonic()
        try:
            text = llm.tldr(title, abstract)
        except Exception:
            # One bad paper or an exhausted quota shouldn't kill the run; it's retried tomorrow.
            log.exception("tldr failed for %s", paper_id)
            text = None
        if text:
            with get_pool().connection() as conn:
                conn.execute("update papers set tldr = %s where id = %s", (text, paper_id))
            done += 1
        time.sleep(max(0.0, 60 / settings.llm_rpm - (time.monotonic() - started)))
    log.info("wrote %d/%d tldrs", done, len(rows))
    return done


def prune() -> None:
    with get_pool().connection() as conn:
        # Saved and dismissed papers stay: they make up profiles and the saved list.
        papers = conn.execute(
            "delete from papers p where published_at < now() - make_interval(days => %s) "
            "and not exists (select 1 from events e where e.paper_id = p.id "
            "and e.kind in ('save', 'dismiss'))",
            (settings.keep_papers_days,),
        ).rowcount
        # Impressions are ~95% of all rows and only the last 14 days feed /stats.
        old = conn.execute(
            "delete from events where kind = 'impression' "
            "and created_at < now() - interval '90 days'"
        ).rowcount
        # Anonymous visitors who never saved anything and haven't been back in a month.
        idle = conn.execute(
            "delete from users u where google_sub is null "
            "and last_seen_at < now() - interval '30 days' "
            "and not exists (select 1 from events e where e.user_id = u.id and e.kind = 'save')"
        ).rowcount
    log.info("pruned %d old papers, %d old impressions, %d idle anonymous users", papers, old, idle)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "LiteLLM"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # LiteLLM starts a background logging task per sync call and abandons it, so asyncio
    # logs "Task was destroyed but it is pending!" every time. We run no asyncio of our own.
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Fetch, embed and summarize new arXiv papers.")
    parser.add_argument("--tldrs", type=int, default=settings.tldrs_per_run)
    parser.add_argument(
        "--reembed",
        action="store_true",
        help="drop all embeddings first; required after changing EMBEDDING_MODEL",
    )
    parser.add_argument(
        "--embed-only",
        action="store_true",
        help="only embed papers that lack a vector, e.g. ones a reader just saved by hand",
    )
    args = parser.parse_args()

    if args.embed_only:
        embed_missing()
        return

    if args.reembed:
        # Vectors from two models live in different spaces; mixing them silently
        # breaks every similarity score.
        with get_pool().connection() as conn:
            conn.execute("update papers set embedding = null")
    store_new_papers()
    hf_signals()
    embed_missing()
    summarize_missing(args.tldrs)
    prune()
    try:
        bluesky.post_daily()
    except Exception:
        log.exception("Bluesky post failed")  # never fails the ingest run


if __name__ == "__main__":
    main()
