"""What a deployed site serves besides its pages and API: security headers and a content security policy that lets
every page's own scripts and styles run, robots.txt, the sitemap, security.txt, the 404 page and the API reference."""
import base64
import hashlib
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import app
import investigate
import sim

client = TestClient(app.app)
SITE = "https://sherlog-jade.vercel.app"
BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
PAGES = ["/", "/console", "/status", "/reference", "/docs"]
HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin",
}


def _policy(res) -> dict[str, list[str]]:
    """The content security policy as directive -> sources."""
    directives = [d.split() for d in res.headers["content-security-policy"].split(";") if d.strip()]
    return {name: sources for name, *sources in directives}


class _Page(HTMLParser):
    """The parts of a page a content security policy governs."""

    def __init__(self):
        super().__init__()
        self.inline = {"script": [], "style": []}  # the text of each inline <script> (import map too) and <style>
        self.loads = {"script": [], "style": []}   # where each <script src> and stylesheet comes from
        self.attributes = []                       # style="" and on...="" attributes, which a hash cannot allow
        self._open = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.attributes += [name for name in attrs if name == "style" or name.startswith("on")]
        if tag == "script" and "src" in attrs:
            self.loads["script"].append(attrs["src"])
        elif tag == "link" and attrs.get("rel") == "stylesheet":
            self.loads["style"].append(attrs["href"])
        elif tag in ("script", "style"):
            self._open = tag
            self.inline[tag].append("")

    def handle_data(self, data):
        if self._open:
            self.inline[self._open][-1] += data

    def handle_endtag(self, tag):
        if tag == self._open:
            self._open = None


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    return page


def _sha256(text: str) -> str:
    return f"'sha256-{base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()}'"


def _not_found_page():
    return client.get("/no-such-page", headers=BROWSER)


# --- security headers ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [*PAGES, "/api/health", "/api/replays/1", "/static/shared.css", "/llms.txt",
                                  "/robots.txt", "/no-such-page"])
def test_every_response_carries_the_security_headers(path):
    res = client.get(path, headers=BROWSER)
    assert {name: res.headers.get(name) for name in HEADERS} == HEADERS
    policy = _policy(res)
    assert policy["frame-ancestors"] == ["'none'"] and policy["object-src"] == ["'none'"]


def test_head_answers_carry_the_security_headers_too():
    res = client.head("/api/health")
    assert res.status_code == 200 and res.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in res.headers


def test_the_policy_allows_no_inline_code_or_eval_except_by_hash():
    policy = _policy(client.get("/"))
    for directive in ("default-src", "script-src", "style-src"):
        assert not {"'unsafe-inline'", "'unsafe-eval'", "*"} & set(policy[directive]), directive
    assert policy["default-src"] == ["'self'"] and policy["connect-src"] == ["'self'"]


@pytest.mark.parametrize("path", PAGES)
def test_every_inline_script_and_style_on_a_page_is_allowed_by_its_hash(path):
    res = client.get(path)
    page, policy = _parse(res.text), _policy(res)
    assert page.inline["script"] or path == "/reference"
    for tag, directive in (("script", "script-src"), ("style", "style-src")):
        for text in page.inline[tag]:
            assert _sha256(text) in policy[directive], f"{path}: an inline <{tag}> has no hash in {directive}"


def test_the_404_page_s_inline_code_is_allowed_too():
    res = _not_found_page()
    page, policy = _parse(res.text), _policy(res)
    assert all(_sha256(text) in policy["style-src"] for text in page.inline["style"])
    assert all(_sha256(text) in policy["script-src"] for text in page.inline["script"])


@pytest.mark.parametrize("path", ["/", "/console", "/status", "/reference"])
def test_pages_use_no_inline_style_or_handler_attributes(path):
    # a hash can allow a <style> or <script> block but not style="..." or onclick="...", which would need
    # 'unsafe-inline'; the console sets its dynamic styles through the DOM instead
    assert _parse(client.get(path).text).attributes == []


@pytest.mark.parametrize("path", PAGES)
def test_every_script_and_stylesheet_a_page_loads_comes_from_an_allowed_origin(path):
    res = client.get(path)
    page, policy = _parse(res.text), _policy(res)
    importmap = re.findall(r'"(https://[^"]+)"', "".join(page.inline["script"]))
    for kind, directive, urls in (("script", "script-src", page.loads["script"] + importmap),
                                  ("style", "style-src", page.loads["style"])):
        for url in urls:
            parts = urlsplit(url)
            origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else "'self'"
            assert origin in policy[directive], f"{path}: {url} is not allowed by {directive}"


def test_fonts_come_from_the_font_host_only():
    policy = _policy(client.get("/"))
    assert policy["font-src"] == ["https://fonts.gstatic.com"]
    assert "https://fonts.googleapis.com" in policy["style-src"]


# --- robots.txt, the sitemap and security.txt ------------------------------------------------------------------

@pytest.mark.parametrize("path, media", [("/robots.txt", "text/plain; charset=utf-8"),
                                         ("/sitemap.xml", "application/xml"),
                                         ("/.well-known/security.txt", "text/plain; charset=utf-8")])
def test_crawler_and_security_files_answer_get_and_head(path, media):
    res = client.get(path)
    assert res.status_code == 200 and res.headers["content-type"] == media and res.text
    head = client.head(path)
    assert head.status_code == 200 and head.content == b"" and head.headers["content-type"] == media


def test_the_crawler_files_take_no_parameters():
    # each route serves one fixed file; nothing a visitor sends can point it at another
    assert client.get("/robots.txt?name=../app.py").text == client.get("/robots.txt").text


def test_robots_txt_allows_the_pages_and_names_the_sitemap():
    lines = client.get("/robots.txt").text.splitlines()
    assert "User-agent: *" in lines and "Allow: /" in lines
    assert f"Sitemap: {SITE}/sitemap.xml" in lines


def test_the_sitemap_lists_every_page_and_llms_txt():
    root = ET.fromstring(client.get("/sitemap.xml").content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    listed = [loc.text for loc in root.findall("s:url/s:loc", ns)]
    assert listed == [f"{SITE}{path}" for path in ("/", "/console", "/status", "/reference", "/llms.txt")]
    for url in listed:  # each one is really served
        assert client.get(url.removeprefix(SITE)).status_code == 200


def test_security_txt_names_a_contact_and_has_not_expired():
    fields = dict(line.split(": ", 1) for line in client.get("/.well-known/security.txt").text.splitlines()
                  if ": " in line and not line.startswith("#"))
    assert fields["Contact"] == "https://github.com/shaunmortal/sherlog/issues"
    expires, now = datetime.fromisoformat(fields["Expires"]), datetime.now(timezone.utc)
    assert now < expires < now + timedelta(days=366)  # RFC 9116: renew it before it is a year old
    assert fields["Canonical"] == f"{SITE}/.well-known/security.txt"


# --- the 404 page ---------------------------------------------------------------------------------------------------

def test_a_browser_gets_a_styled_404_page_with_links_home_and_to_the_console():
    res = _not_found_page()
    assert res.status_code == 404 and res.headers["content-type"].startswith("text/html")
    page = res.text
    assert '<html lang="en">' in page and 'href="/static/shared.css"' in page
    assert 'href="/"' in page and 'href="/console"' in page and "<h1" in page


def test_a_404_page_answers_head_with_no_body():
    res = client.head("/no-such-page", headers=BROWSER)
    assert res.status_code == 404 and res.content == b"" and res.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path, headers", [
    ("/no-such-page", {}),                          # a script that accepts anything still gets JSON
    ("/no-such-page", {"accept": "application/json"}),
    ("/api/no-such-route", BROWSER),                # the API and the MCP endpoint always answer JSON
    ("/api", BROWSER),
    ("/static/no-such-file.js", {"accept": "*/*"}),
])
def test_api_clients_still_get_the_json_404(path, headers):
    res = client.get(path, headers=headers)
    assert res.status_code == 404 and res.json() == {"detail": "Not Found"}


def test_an_api_404_keeps_its_own_message_even_for_a_browser():
    res = client.get("/api/replays/99999", headers=BROWSER)
    assert res.status_code == 404 and res.json() == {"detail": "No recorded outage with that number."}


def test_other_errors_are_unchanged_for_browsers():
    res = client.get("/mcp", headers=BROWSER)
    assert res.status_code == 405 and res.json() == {"detail": "Method Not Allowed"}


# --- the API reference ----------------------------------------------------------------------------------------------

def test_the_api_reference_lists_every_route_in_the_schema():
    res = client.get("/reference")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    page = res.text
    assert '<html lang="en">' in page and 'href="/static/shared.css"' in page
    spec = client.get("/openapi.json").json()
    for path, operations in spec["paths"].items():
        for method in operations:
            assert re.search(rf">{method.upper()}</span>\s*<code>{re.escape(path)}</code>", page), (method, path)


def test_the_api_reference_describes_parameters_and_bodies():
    page = client.get("/reference").text
    assert "<code>graph</code>" in page and "&quot;shop&quot; | &quot;custom&quot;" in page  # a query parameter
    assert "<code>service</code>" in page and "&quot;payment&quot;" in page  # a JSON body's field and its choices


def test_the_api_reference_answers_head():
    head = client.head("/reference")
    assert head.status_code == 200 and head.content == b""


@pytest.mark.parametrize("path", ["/", "/console", "/status", "/reference"])
def test_every_footer_links_the_api_reference_page(path):
    page = client.get(path).text
    footer = page[page.index("<footer"):page.index("</footer>")]
    assert re.search(r'href="/reference"[^>]*>API</a>', footer) and 'href="/docs"' not in footer


def test_swagger_ui_stays_at_docs_for_developers():
    res = client.get("/docs")
    assert res.status_code == 200 and "swagger-ui" in res.text and "/openapi.json" in res.text


# --- the console's first paint matches what it shows once loaded, so nothing moves -------------------------------

def test_the_console_starts_with_the_text_it_shows_once_loaded():
    page = client.get("/console").text
    assert f'id="metaSystem">Simulated shop · {len(sim.SERVICES)} services<' in page
    assert f'id="metaModel">{investigate.MODELS["haiku"]} → {investigate.MODELS["opus"]}<' in page
    meta = client.get("/api/meta").json()
    # the stats strip, as renderStats writes it from the real-outage benchmark
    real = meta["real_benchmark"]
    first = {k: f"{math.floor(m['AC@1'] * 100 + 0.5)}%" for k, m in real["methods"].items()}  # JS Math.round
    vs, agent = real["auto_vs"]["mmnsigma"], real["agent"]
    assert (f'id="statReal">{first["auto"]}</div><div class="small" id="statRealSub">{real["cases"]} held-out outages<br>'
            f'on {real["system_count"]} real systems<br>no AI, same data: {first["graph"]}</div>') in page
    assert (f'id="statPublished">{first["mmnsigma"]}</div><div class="small" id="statPublishedSub">multi-source n-sigma<br>'
            f'case by case, Sherlog<br>leads {vs["only_first_right"]} to {vs["only_second_right"]}</div>') in page
    assert (f'id="statCost">${agent["cost_usd_median"]:.4f}</div><div class="small" id="statCostSub">median, auto route<br>'
            f'${agent["cost_usd_total"]:.2f} for all {real["cases"]}</div>') in page
    assert (f'id="statNote">{real["systems"][0].upper()}{real["systems"][1:]}; every method ran on the same cases. '
            'Details and the simulated benchmark under Evidence.</p>') in page
    # the default picks, as the console describes them: the latency fault and the auto investigator
    fault = meta["faults"][re.search(r"let pick = \{ service: '\w+', fault: '(\w+)'", page).group(1)]
    assert f'id="faultDesc">{fault[0].upper()}{fault[1:]}.</p>' in page
    auto = re.search(r"  auto: '([^']+)',", page).group(1)
    assert f'id="modelDesc">{auto} Numbers: cause named first, median cost per outage on the benchmark.</p>' in page


def test_the_console_grids_hold_a_blank_cell_for_every_choice_until_they_load():
    page = client.get("/console").text
    meta = client.get("/api/meta").json()
    for grid, count in (("svcGrid", len(meta["services"])), ("faultGrid", len(meta["faults"])),
                        ("modelGrid", len(meta["models"]) + 1), ("modeGrid", 2)):  # + 1: auto
        cells = re.search(rf'id="{grid}"[^>]*>(.*?)</div>', page).group(1)
        assert cells.count('class="cell wait"') == count, grid
