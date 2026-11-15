"""The docs on the site (/guide): the markdown renderer in guide.py, which must escape everything it does not render,
and the pages it builds from docs/*.md and the README."""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app
import guide

client = TestClient(app.app)
SITE = "https://sherlog-jade.vercel.app"
BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
REPO = "https://github.com/shaunmortal/sherlog"
DOCS = {doc.source: f"/guide/{doc.slug}" for doc in guide.DOCS if not doc.section}
PATHS = ["/guide", *(f"/guide/{doc.slug}" for doc in guide.DOCS)]


def render(markdown: str, source: str = "docs/test.md") -> str:
    return guide.Renderer(source, DOCS).render(markdown)


# --- escaping: nothing in a doc becomes markup unless the renderer made it ---------------------------------------

@pytest.mark.parametrize("markdown", [
    "<script>alert(1)</script>",
    "# <img src=x onerror=alert(1)>",
    "- <b>item</b>",
    "> <iframe src=x></iframe>",
    "| <i>head</i> |\n|---|\n| <svg onload=alert(1)> |",
    "**<b>bold</b>** and *<i>em</i>*",
    "[<b>label</b>](https://example.org)",
    "```\n<script>alert(1)</script>\n```",
    "`<script>`",
])
def test_raw_html_comes_out_escaped(markdown):
    html = render(markdown)
    assert not re.search(r"<(script|img|iframe|svg|b|i)\b", html), html
    assert "&lt;" in html


def test_a_link_cannot_break_out_of_its_attribute_or_run_script():
    html = render('[a](https://example.org/"onmouseover="steal) [b](javascript:steal) [c](data:text/html,x)')
    assert 'href="https://example.org/&quot;onmouseover=&quot;steal"' in html
    assert "javascript:" not in html and "data:" not in html
    assert html.count("<a ") == 1 and "b" in html and "c" in html  # the unsafe ones keep their text, lose the link


def test_markup_inside_code_spans_and_backslash_escapes_stays_literal():
    html = render(r"`**not bold**` and \*not em\* and `a | b`")
    assert "<code>**not bold**</code>" in html and "*not em*" in html and "<em>" not in html
    assert "<code>a | b</code>" in html


def test_a_nul_character_in_a_doc_cannot_forge_a_placeholder():
    assert render("a \x000\x00 b `c`") == "<p>a 0 b <code>c</code></p>"


# --- links: between docs to /guide, other repository files to the repository -------------------------------------

@pytest.mark.parametrize("url, href", [
    ("mcp.md", "/guide/mcp"),
    ("bring-your-own.md#the-format", "/guide/bring-your-own#the-format"),
    ("#the-format", "#the-format"),
    ("../runs/evidence.json", f"{REPO}/blob/main/runs/evidence.json"),
    ("../runs", f"{REPO}/tree/main/runs"),
    ("../scripts/claude_over_mcp.py", f"{REPO}/blob/main/scripts/claude_over_mcp.py"),
    ("https://sherlog-jade.vercel.app/console", "https://sherlog-jade.vercel.app/console"),
])
def test_links_are_rewritten_for_the_site(url, href):
    assert f'<a href="{href}">x</a>' in render(f"[x]({url})")


def test_the_readme_s_links_resolve_from_the_repository_root():
    assert guide.Renderer("README.md", DOCS).href("docs/mcp.md#connect") == "/guide/mcp#connect"


def test_an_autolink_becomes_a_link():
    assert '<a href="https://example.org/data">https://example.org/data</a>' in render("<https://example.org/data>")


def test_a_link_label_keeps_its_code_and_emphasis():
    assert '<a href="/guide/mcp"><code>mcp.md</code> <strong>now</strong></a>' in render("[`mcp.md` **now**](mcp.md)")


# --- blocks ---------------------------------------------------------------------------------------------------------

def test_tables_get_header_cells_alignment_and_a_scrolling_box():
    html = render("| Method | Cases | Note |\n|---|---:|:-:|\n| `a\\|b` | 252 | **x** |\n| short |")
    assert '<div class="table" tabindex="0"><table>' in html
    assert '<th scope="col">Method</th><th scope="col" class="right">Cases</th><th scope="col" class="center">' in html
    assert '<td><code>a|b</code></td><td class="right">252</td><td class="center"><strong>x</strong></td>' in html
    assert '<tr><td>short</td><td class="right"></td><td class="center"></td></tr>' in html  # padded to the header


def test_a_blank_header_cell_is_a_plain_cell():
    assert '<thead><tr><td></td><th scope="col">A</th></tr></thead>' in render("| | A |\n|---|---|\n| x | 1 |")


def test_code_blocks_are_escaped_labelled_and_focusable_for_scrolling():
    html = render('```json\n{"a": "<b>"}\n  indented\n```\nafter')
    assert '<pre tabindex="0"><code class="language-json">{&quot;a&quot;: &quot;&lt;b&gt;&quot;}\n  indented</code></pre>' in html
    assert html.endswith("<p>after</p>")


def test_a_mermaid_diagram_shows_as_its_source_with_a_link_to_the_drawn_one():
    html = guide.Renderer("docs/x.md", DOCS, f"{REPO}/blob/main/docs/x.md").render("```mermaid\nA --> B\n```")
    assert '<code class="language-mermaid">A --&gt; B</code>' in html
    assert f'<figcaption>Diagram source, in Mermaid notation. <a href="{REPO}/blob/main/docs/x.md">' in html


def test_headings_get_github_ids_and_a_link_to_themselves():
    renderer = guide.Renderer("docs/x.md", DOCS)
    html = renderer.render("# Title\n## Example: `get_recorded_verdict`\n## Results\n## Results\n## Main")
    assert '<h1 id="title">Title</h1>' in html  # the page title links nowhere
    assert ('<h2 id="example-get_recorded_verdict">Example: <code>get_recorded_verdict</code><a class="anchor" '
            'href="#example-get_recorded_verdict" aria-label="Link to this section">#</a></h2>') in html
    assert [anchor for _, anchor, _ in renderer.headings] == [
        "title", "example-get_recorded_verdict", "results", "results-1", "main-1"]  # the page's <main> keeps "main"


def test_lists_nest_and_tight_items_skip_paragraphs():
    html = render("- one\n  goes on\n  - inner `x`\n    still inner\n- two\n\n3. three\n4. four")
    assert html == ("<ul><li>one goes on\n<ul><li>inner <code>x</code> still inner</li></ul></li><li>two</li></ul>\n"
                    '<ol start="3"><li>three</li><li>four</li></ol>')


def test_a_loose_list_keeps_its_paragraphs():
    assert render("1. one\n\n2. two") == "<ol><li><p>one</p></li><li><p>two</p></li></ol>"


def test_paragraphs_join_their_lines_and_stop_at_a_new_block():
    html = render("first line\nsecond `code\nspan`\n- item\n> quote\n\n---\ntext")
    assert html.startswith("<p>first line second <code>code span</code></p>\n<ul><li>item</li></ul>")
    assert "<blockquote><p>quote</p></blockquote>" in html and "<hr>" in html and html.endswith("<p>text</p>")


def test_an_ordered_list_interrupts_a_paragraph_only_from_one():
    assert render("in\n2. no") == "<p>in 2. no</p>"
    assert render("in\n1. yes") == "<p>in</p>\n<ol><li>yes</li></ol>"


# --- the pages ------------------------------------------------------------------------------------------------------

def test_every_doc_in_docs_has_a_page():
    on_disk = {f"docs/{path.name}" for path in (guide.ROOT / "docs").glob("*.md")}
    assert on_disk == set(DOCS)


@pytest.mark.parametrize("path", PATHS)
def test_every_docs_page_answers_get_and_head_as_html(path):
    res = client.get(path)
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    assert '<html lang="en">' in res.text and 'href="/static/guide.css"' in res.text and res.text.count("<h1") == 1
    head = client.head(path)
    assert head.status_code == 200 and head.content == b""


def test_a_docs_page_names_its_source_and_marks_itself_in_the_page_list():
    page = client.get("/guide/mcp").text
    assert "<title>Use Sherlog from Claude: the MCP connector · Sherlog docs</title>" in page
    assert '<a href="/guide/mcp" aria-current="page">MCP connector</a><ul><li><a href="#connect">Connect</a>' in page
    assert f'<a href="{REPO}/blob/main/docs/mcp.md"><code>docs/mcp.md</code></a> in the repository' in page


def test_the_readme_section_page_leaves_out_the_raw_html_screenshot():
    page = client.get("/guide/how-it-works").text
    assert "<h1" in page and "Detect.</strong>" in page and "&lt;p align" not in page and "<hr>" not in page
    assert f'<a href="{REPO}/blob/main/README.md#how-it-works"><code>README.md</code></a>' in page


@pytest.mark.parametrize("path, headers, json", [("/guide/no-such-page", BROWSER, False),
                                                 ("/guide/no-such-page", {}, True)])
def test_an_unknown_docs_page_is_a_404(path, headers, json):
    res = client.get(path, headers=headers)
    assert res.status_code == 404
    assert (res.json() == {"detail": "No docs page with that name."}) if json else "Page not found" in res.text


def test_every_link_between_docs_pages_lands_on_a_heading_that_exists():
    pages = {path: client.get(path).text for path in PATHS}
    for path, page in pages.items():
        for target, anchor in re.findall(r'href="(/guide[^"#]*)#?([^"]*)"', page):
            assert target in pages, (path, target)
            if anchor:
                assert f'id="{anchor}"' in pages[target], (path, target, anchor)
        for anchor in re.findall(r'href="#([^"]+)"', page):
            assert f'id="{anchor}"' in page, (path, anchor)


def test_the_sitemap_lists_every_docs_page():
    root = ET.fromstring(client.get("/sitemap.xml").content)
    listed = {loc.text for loc in root.findall("s:url/s:loc", {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"})}
    assert {f"{SITE}{path}" for path in PATHS} <= listed


def test_llms_txt_lists_every_docs_page():
    text = client.get("/llms.txt").text
    assert [path for path in PATHS if f"({SITE}{path})" not in text] == []


def test_the_deploy_uploads_what_the_docs_pages_are_built_from():
    ignored = {line.strip() for line in Path(guide.ROOT / ".vercelignore").read_text().splitlines()}
    assert not {"docs", "docs/", "README.md", "*.md"} & ignored
