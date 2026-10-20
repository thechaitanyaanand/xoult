"""Look up papers on the arXiv API, for readers adding papers to their library by hand.

Stdlib only: the API image has no httpx (see the Dockerfile).
"""

import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

from xoult.lexical import STOP

API_URL = "https://export.arxiv.org/api/query?"
ATOM = {"a": "http://www.w3.org/2005/Atom"}

# arXiv metadata keeps TeX accent escapes: Ga\"etan, Cl\'ement, Sch{\"o}lkopf, \c{c}.
ACCENTS = {
    '"': "̈", "'": "́", "`": "̀", "^": "̂", "~": "̃", "=": "̄",
    ".": "̇", "c": "̧", "v": "̌", "u": "̆", "H": "̋",
}  # fmt: skip
TEX_ACCENT = re.compile(r"""\{?\\(["'`^~=.]|[cvuH](?=[\s{]))\s*\{?\\?([A-Za-z])\}?\}?""")

# New-style (1706.03762) and old-style (cs/0112017) ids, bare or inside an abs/pdf/html
# link, with an optional version suffix, which is dropped like ingest does.
ARXIV_ID = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf|html)/|arxiv:\s*|^)"
    r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?/?$",
    re.IGNORECASE,
)


class Unavailable(Exception):
    """arXiv didn't answer usefully: rate-limited, down, or unreachable."""


def clean(text: str | None) -> str:
    text = " ".join((text or "").split())
    text = TEX_ACCENT.sub(lambda m: m[2] + ACCENTS[m[1]], text)
    return unicodedata.normalize("NFC", text)


def parse_id(text: str) -> str | None:
    """The arXiv id in a pasted link or id, or None if it's free text."""
    match = ARXIV_ID.search(text.strip().split("?")[0].split("#")[0])
    return match[1] if match else None


def parse_atom(xml: str) -> list[dict]:
    papers = []
    for entry in ET.fromstring(xml).iterfind("a:entry", ATOM):
        link = entry.findtext("a:id", "", ATOM)
        if "/abs/" not in link:  # the API reports a bad query as an entry with no paper
            continue
        papers.append(
            {
                "id": re.sub(r"v\d+$", "", link.split("/abs/", 1)[1]),
                "title": clean(entry.findtext("a:title", "", ATOM)),
                "abstract": clean(entry.findtext("a:summary", "", ATOM)),
                "authors": [clean(a.text) for a in entry.iterfind("a:author/a:name", ATOM)],
                "categories": [c.get("term") for c in entry.iterfind("a:category", ATOM)],
                "published_at": datetime.fromisoformat(entry.findtext("a:published", "", ATOM)),
                "tldr": None,
            }
        )
    return papers


def _query(params: dict) -> list[dict]:
    url = API_URL + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "xoult (paper library)"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return parse_atom(response.read().decode())
    except (urllib.error.URLError, TimeoutError, ET.ParseError) as e:
        raise Unavailable(str(e)) from None


def get(paper_id: str) -> dict | None:
    papers = _query({"id_list": paper_id, "max_results": 1})
    return papers[0] if papers else None


def search(text: str, limit: int = 5) -> list[dict]:
    """Papers for a pasted link or id, or else a title search ranked by arXiv."""
    paper_id = parse_id(text)
    if paper_id:
        paper = get(paper_id)
        return [paper] if paper else []
    # Every word must be in the title, so "attention is all you need" finds the paper
    # rather than everything mentioning attention. arXiv drops stop words from its index,
    # so requiring "is" or "all" would match nothing.
    words = [w for w in re.findall(r"\w+", text.lower()) if w not in STOP][:12]
    if not words:
        return []
    query = " AND ".join(f"ti:{json.dumps(w)}" for w in words)
    papers = _query({"search_query": query, "max_results": limit, "sortBy": "relevance"})
    # arXiv's relevance puts "Attention when you need" above "Attention Is All You Need".
    exact = " ".join(text.lower().split())
    return sorted(papers, key=lambda p: p["title"].lower() != exact)
