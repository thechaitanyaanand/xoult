from xoult.arxiv import parse_atom, parse_id

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <published>2017-06-12T17:57:34Z</published>
    <title>Attention Is All
      You Need</title>
    <summary>  The dominant sequence transduction models...  </summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <category term="cs.CL"/>
    <category term="cs.LG"/>
  </entry>
</feed>"""

ERROR = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
  <id>http://arxiv.org/api/errors#incorrect_id_format_for_1706</id><title>Error</title>
</entry></feed>"""


def test_parse_id_from_links_and_ids():
    assert parse_id("https://arxiv.org/abs/1706.03762v7") == "1706.03762"
    assert parse_id("arxiv.org/pdf/1706.03762.pdf") == "1706.03762"
    assert parse_id("https://arxiv.org/html/2410.01234v2/") == "2410.01234"
    assert parse_id("arXiv:2410.01234") == "2410.01234"
    assert parse_id(" 2410.01234 ") == "2410.01234"
    assert parse_id("https://arxiv.org/abs/cs/0112017?context=cs") == "cs/0112017"
    assert parse_id("https://arxiv.org/abs/hep-th/9901001v1") == "hep-th/9901001"
    assert parse_id("attention is all you need") is None
    assert parse_id("https://example.com/abs/1706.03762.evil") is None


def test_parse_atom():
    [paper] = parse_atom(ATOM)
    assert paper["id"] == "1706.03762"
    assert paper["title"] == "Attention Is All You Need"
    assert paper["abstract"] == "The dominant sequence transduction models..."
    assert paper["authors"][0] == "Ashish Vaswani"
    assert paper["categories"] == ["cs.CL", "cs.LG"]
    assert paper["published_at"].year == 2017
    assert parse_atom(ERROR) == []
