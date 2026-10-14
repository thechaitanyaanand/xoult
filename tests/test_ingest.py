from datetime import datetime, timedelta, timezone

from xoult.ingest import parse_feed
from xoult.llm import clean_tldr

ITEM = """<item>
  <title>Simple Linear Corrections for
    Compression in Late Interaction</title>
  <link>https://arxiv.org/abs/{id}</link>
  <description>arXiv:{id}v1 Announce Type: {kind}
Abstract: Late-interaction models represent documents
  using token-level vectors.</description>
  <category>cs.IR</category>
  <category>cs.CL</category>
  <pubDate>Wed, 14 Oct 2026 00:00:00 -0400</pubDate>
  <arxiv:announce_type>{kind}</arxiv:announce_type>
  <dc:creator>Mihir Agarwal, Sabyasachi Basu,  Viraj Agrawal</dc:creator>
</item>"""


def feed(*items: str) -> str:
    return (
        '<rss xmlns:arxiv="http://arxiv.org/schemas/atom" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel>'
        + "".join(items)
        + "</channel></rss>"
    )


def test_parse_feed_normalizes_item():
    [paper] = parse_feed(feed(ITEM.format(id="2610.14196", kind="new")))
    assert paper.id == "2610.14196"
    assert paper.title == "Simple Linear Corrections for Compression in Late Interaction"
    assert (
        paper.abstract == "Late-interaction models represent documents using token-level vectors."
    )
    assert paper.authors == ["Mihir Agarwal", "Sabyasachi Basu", "Viraj Agrawal"]
    assert paper.categories == ["cs.IR", "cs.CL"]
    assert paper.published_at == datetime(2026, 10, 14, tzinfo=timezone(timedelta(hours=-4)))


def test_parse_feed_skips_replacements():
    papers = parse_feed(
        feed(
            ITEM.format(id="2610.00001", kind="new"),
            ITEM.format(id="2610.00002", kind="cross"),
            ITEM.format(id="2401.00003", kind="replace"),
            ITEM.format(id="2401.00004", kind="replace-cross"),
        )
    )
    assert [p.id for p in papers] == ["2610.00001", "2610.00002"]


def test_clean_tldr():
    assert (
        clean_tldr('TL;DR: "Proposes X; beats Y by 3 points."')
        == "Proposes X; beats Y by 3 points."
    )
    assert clean_tldr("   ") is None
    assert clean_tldr("word " * 80) is None
