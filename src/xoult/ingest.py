import argparse
import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

from xoult import llm
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


# arXiv metadata keeps TeX accent escapes: Ga\"etan, Cl\'ement, Sch{\"o}lkopf, \c{c}.
ACCENTS = {
    '"': "̈", "'": "́", "`": "̀", "^": "̂", "~": "̃", "=": "̄",
    ".": "̇", "c": "̧", "v": "̌", "u": "̆", "H": "̋",
}  # fmt: skip
TEX_ACCENT = re.compile(r"""\{?\\(["'`^~=.]|[cvuH](?=[\s{]))\s*\{?\\?([A-Za-z])\}?\}?""")


def _clean(text: str | None) -> str:
    text = " ".join((text or "").split())
    text = TEX_ACCENT.sub(lambda m: m[2] + ACCENTS[m[1]], text)
    return unicodedata.normalize("NFC", text)


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
    log.info("feed had %d new papers", len(papers))
    return len(papers)


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


def summarize_missing(limit: int) -> int:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "select id, title, abstract from papers where tldr is null "
            "order by published_at desc limit %s",
            (limit,),
        ).fetchall()

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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "LiteLLM"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Fetch, embed and summarize new arXiv papers.")
    parser.add_argument("--tldrs", type=int, default=settings.tldrs_per_run)
    parser.add_argument(
        "--reembed",
        action="store_true",
        help="drop all embeddings first; required after changing EMBEDDING_MODEL",
    )
    args = parser.parse_args()

    if args.reembed:
        # Vectors from two models live in different spaces; mixing them silently
        # breaks every similarity score.
        with get_pool().connection() as conn:
            conn.execute("update papers set embedding = null")
    store_new_papers()
    embed_missing()
    summarize_missing(args.tldrs)


if __name__ == "__main__":
    main()
