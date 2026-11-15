"""The docs on the site (/guide): docs/*.md and the README's "How it works", rendered once at start-up into the site's
own style, so a visitor reads them without leaving the site.

The renderer covers the markdown these files use: headings, paragraphs, lists (nested too), tables, fenced code,
blockquotes, rules, and inline code, bold, italic and links. Everything else, raw HTML included, comes out escaped.
Links between the docs go to their /guide pages; links to other repository files go to the repository.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from html import escape, unescape
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).parent
REPO = "https://github.com/shaunmortal/sherlog"
MARKERS = ("<!-- title -->", "<!-- description -->", "<!-- nav -->", "<!-- article -->")  # template slots


@dataclass(frozen=True)
class Doc:
    slug: str
    source: str  # the file in the repository
    label: str  # its name in the page list
    summary: str  # the index's line about it, and the page's description
    section: str = ""  # one "## " section of the source, not all of it


DOCS = (
    Doc("how-it-works", "README.md", "How it works",
        "The pipeline: a no-AI alarm, a Claude agent with read-only tools, a verifier that checks every call it cites, "
        "the 80% rule, the blast-radius check and the rollback replay.", section="How it works"),
    Doc("real-benchmark", "docs/real-benchmark.md", "Real outages",
        "252 held-out real outages of a public benchmark: the protocol, every table, the published baselines re-run on "
        "the same cases, and the limitations."),
    Doc("real-system", "docs/real-system.md", "A real running system",
        "Eight real services, real faults and real rollbacks: 30 outages, what each method answered and what the "
        "rollback caught."),
    Doc("mcp", "docs/mcp.md", "MCP connector",
        "Investigate an outage with your own Claude over MCP while Sherlog's code checks the answer: how to connect, "
        "and every tool with an example."),
    Doc("bring-your-own", "docs/bring-your-own.md", "Bring your own outage",
        "The upload format, with or without your own call graph, what is checked, and a converter for metric "
        "range-query exports."),
    Doc("open-model", "docs/open-model.md", "Open-weights models",
        "Two open-weights models behind the same gate as Claude: wrong answers, coverage, cost and the adapter."),
    Doc("reproducibility", "docs/reproducibility.md", "Reproducibility",
        "Every recorded run re-run from its seed with no API call, and a mutation check that the tests catch a "
        "change."),
)

FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*([\w+-]*)")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
RULE = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
ITEM = re.compile(r"^( *)([-*+]|\d{1,9}[.)])(?: +|$)(.*)")
DELIMITER = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
CODE = re.compile(r"(`+)(.+?)\1")
ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")
AUTOLINK = re.compile(r"<(https?://[^\s<>]+)>")
LINK = re.compile(r"\[((?:[^\[\]]|\[[^\]]*\])+)\]\(([^()\s]+)\)")
STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
EM = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
STASHED = re.compile(r"\x00(\d+)\x00")
LINK_TAG = re.compile(r"</?a\b[^>]*>")  # a heading's links stay out of the page list, which links the heading
ALIGN = {"right": ' class="right"', "center": ' class="center"', "": ""}


class Renderer:
    """One markdown file to HTML. `docs` maps each rendered file's path to its page, for rewriting links."""

    def __init__(self, source: str, docs: dict[str, str], original: str = ""):
        self.source, self.docs = source, docs
        self.original = original or f"{REPO}/blob/main/{source}"  # the page as the repository shows it
        self.headings: list[tuple[int, str, str]] = []  # level, id, inline HTML: for the page list
        self._ids = {"main": 1}  # ids already taken: the page's <main>, the skip link's target

    # --- inline ----------------------------------------------------------------------------------------------------

    def href(self, url: str) -> str | None:
        """Where a link goes on the site: anchors and web links as they are, a rendered doc to its page, any other
        repository file to the repository. Other schemes (javascript: and the like) get no link."""
        scheme = urlsplit(url).scheme
        if url.startswith("#") or scheme in ("http", "https", "mailto"):
            return url
        if scheme:
            return None
        path, _, fragment = url.partition("#")
        target = posixpath.normpath(posixpath.join(posixpath.dirname(self.source), path))
        anchor = f"#{fragment}" if fragment else ""
        if target in self.docs:
            return f"{self.docs[target]}{anchor}"
        if target.startswith(".."):
            return REPO
        kind = "tree" if (ROOT / target).is_dir() else "blob"
        return f"{REPO}/{kind}/main/{target}{anchor}"

    def inline(self, text: str) -> str:
        """Code spans, escapes and links set aside first, so nothing inside them is read as markup; the rest escaped."""
        stash: list[str] = []

        def keep(html: str) -> str:
            stash.append(html)
            return f"\x00{len(stash) - 1}\x00"

        def link(m: re.Match) -> str:
            url, label = self.href(m[2]), emphasis(escape(m[1], quote=False))
            return keep(f'<a href="{escape(url)}">{label}</a>' if url else label)

        text = CODE.sub(lambda m: keep(f"<code>{escape(m[2].strip() or m[2])}</code>"), text.replace("\x00", ""))
        text = ESCAPED.sub(lambda m: keep(escape(m[1])), text)
        text = AUTOLINK.sub(lambda m: keep(f'<a href="{escape(m[1])}">{escape(m[1])}</a>'), text)
        text = emphasis(escape(LINK.sub(link, text), quote=False))
        while STASHED.search(text):  # a link's label can hold a code span
            text = STASHED.sub(lambda m: stash[int(m[1])], text)
        return text

    # --- blocks ----------------------------------------------------------------------------------------------------

    def render(self, markdown: str) -> str:
        return self.blocks(markdown.expandtabs(4).splitlines())

    def blocks(self, lines: list[str], tight: bool = False) -> str:
        """`tight`: a list item's paragraphs go without <p>, as in a list with no blank lines between items."""
        out, i = [], 0
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
            elif FENCE.match(line):
                html, i = self.fence(lines, i)
                out.append(html)
            elif m := HEADING.match(line):
                out.append(self.heading(len(m[1]), m[2]))
                i += 1
            elif RULE.match(line):
                out.append("<hr>")
                i += 1
            elif line.lstrip().startswith(">"):
                quoted = []
                while i < len(lines) and lines[i].lstrip().startswith(">"):
                    quoted.append(re.sub(r"^\s*> ?", "", lines[i]))
                    i += 1
                out.append(f"<blockquote>{self.blocks(quoted)}</blockquote>")
            elif self.table_starts(lines, i):
                html, i = self.table(lines, i)
                out.append(html)
            elif ITEM.match(line):
                html, i = self.list(lines, i)
                out.append(html)
            else:
                para = [line.strip()]
                i += 1
                while i < len(lines) and lines[i].strip() and not self.interrupts(lines, i):
                    para.append(lines[i].strip())
                    i += 1
                text = self.inline(" ".join(para))
                out.append(text if tight else f"<p>{text}</p>")
        return "\n".join(out)

    def interrupts(self, lines: list[str], i: int) -> bool:
        """Whether this line starts a new block in the middle of a paragraph (an ordered list only from 1)."""
        line, item = lines[i], ITEM.match(lines[i])
        return bool(FENCE.match(line) or HEADING.match(line) or RULE.match(line) or line.lstrip().startswith(">")
                    or self.table_starts(lines, i)
                    or (item and item[3].strip() and (not item[2][0].isdigit() or int(item[2][:-1]) == 1)))

    def heading(self, level: int, text: str) -> str:
        html = self.inline(text)
        base = _slug(_text(html))
        seen = self._ids.get(base, 0)
        self._ids[base] = seen + 1
        anchor = f"{base}-{seen}" if seen else base
        self.headings.append((level, anchor, html))
        link = f'<a class="anchor" href="#{anchor}" aria-label="Link to this section">#</a>' if level > 1 else ""
        return f'<h{level} id="{anchor}">{html}{link}</h{level}>'

    def fence(self, lines: list[str], i: int) -> tuple[str, int]:
        m = FENCE.match(lines[i])
        indent, mark, lang = len(m[1]), m[2], m[3].lower()
        body, i = [], i + 1
        while i < len(lines) and not re.match(rf"^ {{0,3}}{mark[0]}{{{len(mark)},}}\s*$", lines[i]):
            body.append(re.sub(rf"^ {{0,{indent}}}", "", lines[i]))
            i += 1
        code = escape("\n".join(body))
        cls = f' class="language-{lang}"' if lang else ""
        block = f'<pre tabindex="0"><code{cls}>{code}</code></pre>'
        if lang == "mermaid":  # shown as its source: the repository's copy of the page draws it
            note = (f'Diagram source, in Mermaid notation. <a href="{escape(self.original)}">The repository\'s copy '
                    "of this page</a> draws it.")
            block = f'<figure class="diagram">{block}<figcaption>{note}</figcaption></figure>'
        return block, i + 1

    @staticmethod
    def table_starts(lines: list[str], i: int) -> bool:
        return lines[i].lstrip().startswith("|") and i + 1 < len(lines) and bool(DELIMITER.match(lines[i + 1]))

    @staticmethod
    def cells(row: str) -> list[str]:
        """A table row's cells: split at each | outside a code span; \\| is a literal bar."""
        row = row.strip().removeprefix("|")
        row = row[:-1] if row.endswith("|") and not row.endswith("\\|") else row
        cells, cell, in_code, j = [], "", False, 0
        while j < len(row):
            ch = row[j]
            if ch == "\\" and row[j + 1:j + 2] == "|":
                cell, j = cell + "|", j + 2
                continue
            if ch == "`":
                in_code = not in_code
            if ch == "|" and not in_code:
                cells.append(cell.strip())
                cell = ""
            else:
                cell += ch
            j += 1
        return [*cells, cell.strip()]

    def table(self, lines: list[str], i: int) -> tuple[str, int]:
        head = self.cells(lines[i])
        align = ["center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else ""
                 for c in self.cells(lines[i + 1])]
        rows, i = [], i + 2
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            rows.append(self.cells(lines[i]))
            i += 1

        def cell(text: str, align: str, header: bool) -> str:
            # a blank header cell (over a column of row names) heads nothing, so it is a plain cell
            tag, scope = ("th", ' scope="col"') if header and text else ("td", "")
            return f"<{tag}{scope}{ALIGN[align]}>{self.inline(text)}</{tag}>"

        def row(cells: list[str], header: bool = False) -> str:
            cells = [*cells, *[""] * (len(head) - len(cells))][:len(head)]
            return "<tr>" + "".join(cell(c, a, header) for c, a in zip(cells, align)) + "</tr>"

        body = "".join(row(r) for r in rows)
        return (f'<div class="table" tabindex="0"><table><thead>{row(head, header=True)}</thead>'
                f"<tbody>{body}</tbody></table></div>"), i

    def list(self, lines: list[str], i: int) -> tuple[str, int]:
        first = ITEM.match(lines[i])
        indent, ordered = len(first[1]), first[2][0].isdigit()

        def sibling(line: str) -> bool:  # the next item of this list
            m = ITEM.match(line)
            return bool(m) and len(m[1]) == indent and m[2][0].isdigit() == ordered

        items, loose = [], False
        while i < len(lines) and sibling(lines[i]):
            m = ITEM.match(lines[i])
            inner = len(m[1]) + len(m[2]) + 1  # the column the item's text starts at
            body, i = [m[3]], i + 1
            while i < len(lines):
                line = lines[i]
                if not line.strip():
                    j = i
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and _indent(lines[j]) >= inner:  # the item goes on after the blank line
                        body, loose, i = [*body, *[""] * (j - i)], True, j
                        continue
                    if j < len(lines) and sibling(lines[j]):  # or the next item comes after it
                        loose, i = True, j
                    break
                if _indent(line) > indent:
                    body.append(line[min(inner, _indent(line)):])
                elif ITEM.match(line) or self.interrupts(lines, i):
                    break
                else:  # a lazy continuation of the item's paragraph
                    body.append(line)
                i += 1
            items.append(body)
        tag, start = ("ol", int(first[2][:-1])) if ordered else ("ul", 1)
        attrs = f' start="{start}"' if start != 1 else ""
        html = "".join(f"<li>{self.blocks(body, tight=not loose)}</li>" for body in items)
        return f"<{tag}{attrs}>{html}</{tag}>", i


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def emphasis(html: str) -> str:
    return EM.sub(r"<em>\1</em>", STRONG.sub(r"<strong>\1</strong>", html))


def _slug(text: str) -> str:
    """A heading's id as GitHub makes it, so the docs' own #anchors keep working."""
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-") or "section"


def _text(html: str) -> str:
    """Rendered inline HTML as plain text."""
    return unescape(re.sub(r"<[^>]+>", "", html))


# --- the pages --------------------------------------------------------------------------------------------------------

def markdown(doc: Doc) -> str:
    text = (ROOT / doc.source).read_text(encoding="utf-8")
    if not doc.section:
        return text
    # the section alone, its name as the page's title. Its screenshot is raw HTML, which would show as text here, so
    # those lines stay out, as does the rule that ends the section
    body = text.split(f"\n## {doc.section}\n", 1)[1].split("\n## ", 1)[0]
    kept = [line for line in body.splitlines() if not line.startswith("<")]
    return "\n".join([f"# {doc.section}", *kept]).rstrip().removesuffix("---")


def _nav(current: str, headings: list[tuple[int, str, str]]) -> str:
    """The page list, the open page's sections under it."""
    def link(href: str, label: str, slug: str) -> str:
        here = ' aria-current="page"' if slug == current else ""
        return f'<a href="{href}"{here}>{label}</a>'

    items = [f'<li>{link("/guide", "Overview", "")}</li>']
    for doc in DOCS:
        sections = "".join(f'<li><a href="#{anchor}">{LINK_TAG.sub("", html)}</a></li>'
                           for level, anchor, html in headings if level == 2 and doc.slug == current)
        below = f"<ul>{sections}</ul>" if sections else ""
        items.append(f'<li>{link(f"/guide/{doc.slug}", escape(doc.label), doc.slug)}{below}</li>')
    return f'<nav class="pages" aria-label="Docs pages"><ul>{"".join(items)}</ul></nav>'


def _fill(template: str, title: str, description: str, nav: str, article: str) -> str:
    for marker, value in zip(MARKERS, (escape(title), escape(description), nav, article)):
        template = template.replace(marker, value)
    return template


def _page(doc: Doc, template: str, docs: dict[str, str]) -> str:
    original = f"{REPO}/blob/main/{doc.source}{f'#{_slug(doc.section)}' if doc.section else ''}"
    renderer = Renderer(doc.source, docs, original)
    html = renderer.render(markdown(doc))
    title = _text(next(h for level, _, h in renderer.headings if level == 1))
    where = f"the “{doc.section}” section of " if doc.section else ""
    source = (f'<p class="source">This page is {where}<a href="{original}"><code>{doc.source}</code></a> in the '
              "repository, rendered by this server.</p>")
    return _fill(template, f"{title} · Sherlog docs", doc.summary, _nav(doc.slug, renderer.headings),
                 f"{html}\n{source}")


def _index(template: str) -> str:
    cards = "".join(f'<li><a href="/guide/{doc.slug}"><span class="name">{escape(doc.label)}</span>'
                    f'<span class="summary">{escape(doc.summary)}</span></a></li>' for doc in DOCS)
    article = (
        '<h1 id="docs">Sherlog docs</h1>'
        '<p class="lede">How Sherlog works, how it was measured, and how to use it on your own outages.</p>'
        f'<ul class="cards">{cards}</ul>'
        '<h2 id="more">More</h2><ul>'
        '<li><a href="/reference">API reference</a>: every route, built from the server\'s own schema.</li>'
        '<li><a href="/docs">Swagger UI</a>: try each route in the browser.</li>'
        '<li><a href="/llms.txt">llms.txt</a>: a map of Sherlog for AI tools.</li>'
        f'<li><a href="{REPO}">The code</a>: Python, MIT licence; the README has every results table.</li></ul>')
    return _fill(template, "Sherlog docs", "How Sherlog works, its benchmarks on real and simulated outages, the MCP "
                 "connector, the upload format and how every run is reproduced.", _nav("", []), article)


def build(template: str) -> dict[str, str]:
    """Every docs page, by its path under /guide ("" for the index)."""
    docs = {doc.source: f"/guide/{doc.slug}" for doc in DOCS if not doc.section}
    return {"": _index(template), **{doc.slug: _page(doc, template, docs) for doc in DOCS}}
