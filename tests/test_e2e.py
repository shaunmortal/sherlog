"""Browser checks against a running server, skipped unless SHERLOG_E2E_URL is set. They play recorded outages
only, so no API key is needed. Needs Playwright for Python (pip install playwright) and Chrome, or Playwright's own
Chromium with SHERLOG_E2E_CHANNEL=chromium (python -m playwright install chromium), as CI runs them:

    uvicorn app:app --port 8000 &
    SHERLOG_E2E_URL=http://127.0.0.1:8000 pytest -q tests/test_e2e.py
"""
import os
import time

import pytest

URL = os.environ.get("SHERLOG_E2E_URL", "").rstrip("/")
if not URL:
    pytest.skip("set SHERLOG_E2E_URL to run the browser checks", allow_module_level=True)
sync_api = pytest.importorskip("playwright.sync_api")
CHANNEL = os.environ.get("SHERLOG_E2E_CHANNEL", "chrome")  # chrome: the installed browser; chromium: Playwright's

REPLAY_TIMEOUT_MS = 120_000  # a recorded outage plays back at reading speed


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=CHANNEL, headless=True)
        yield browser
        browser.close()


@pytest.fixture
def errors():
    return []


@pytest.fixture
def page(browser, errors):
    """A fresh page that collects every console error and uncaught exception into `errors`."""
    context = browser.new_context()
    page = context.new_page()
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    yield page
    context.close()


def test_story_page_loads_without_console_errors(page, errors):
    page.goto(f"{URL}/", wait_until="networkidle")
    sync_api.expect(page.locator("h1")).to_be_visible()
    assert errors == []


def test_the_story_pages_one_h1_is_its_visible_headline_and_results_lead_with_the_real_outages(page, errors):
    page.goto(f"{URL}/", wait_until="networkidle")
    heading = page.locator("h1")
    sync_api.expect(heading).to_have_count(1)
    sync_api.expect(heading).to_have_text("An AI on-call investigator that has to prove its answer.")
    sync_api.expect(heading).to_be_in_viewport()
    # it fades in on load (about a second), not when scrolled to
    page.wait_for_function("getComputedStyle(document.querySelector('h1')).opacity === '1'", timeout=3_000)
    real = page.request.get(f"{URL}/api/meta").json()["real_benchmark"]
    rows = page.locator("#realBars .bar-row")
    sync_api.expect(rows).to_have_count(len(real["methods"]))
    sync_api.expect(rows.first).to_contain_text(f"{round(real['methods']['auto']['AC@1'] * 100)}%")
    # the real outages come before the controlled benchmark
    assert page.evaluate("$('#realBars').compareDocumentPosition($('#bars')) & Node.DOCUMENT_POSITION_FOLLOWING")
    assert errors == []


def test_clicking_a_guess_card_reveals_the_cause(page, errors):
    page.goto(f"{URL}/")
    page.locator("#cards").scroll_into_view_if_needed()
    page.locator("#cards.ready .card").first.click()  # cards take guesses once the wall has lit up
    sync_api.expect(page.locator("#answerText")).to_contain_text("The cause was")
    assert errors == []


PAST_REVEAL_FALLBACK_MS = 6_000  # the story page shows unscrolled sections 4 s after load, for automated screenshots


@pytest.mark.parametrize("automated", [False, True])
def test_sections_wait_for_a_person_to_scroll_but_not_for_a_screenshot(browser, automated):
    context = browser.new_context()
    if not automated:  # a person's browser does not report automation
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => false })")
    page = context.new_page()
    page.goto(f"{URL}/", wait_until="networkidle")
    page.wait_for_timeout(PAST_REVEAL_FALLBACK_MS)
    waiting = page.locator(".pre").count()  # sections below the fold that have not revealed yet
    context.close()
    assert (waiting == 0) == automated


def test_console_plays_a_recorded_outage_to_its_verdict(page, errors):
    page.goto(f"{URL}/console")
    page.get_by_role("tab", name="REPLAY").click()
    page.locator("#allReplays > summary").click()  # every outage sits in a list that opens below the picks
    page.locator("#replayGrid button").first.click()
    sync_api.expect(page.locator("#status")).to_contain_text("Done.", timeout=REPLAY_TIMEOUT_MS)
    sync_api.expect(page.locator("#verdict")).to_contain_text("The culprit was")
    assert errors == []


def test_the_first_screen_button_replays_outage_41_with_precise_labels(page, errors):
    page.goto(f"{URL}/console", wait_until="networkidle")  # the button listens once the console has its data
    page.get_by_role("button", name="Replay outage #41").click()
    sync_api.expect(page.locator("#metaMode")).to_have_text("replay")
    sync_api.expect(page.locator("#status")).to_contain_text("Done.", timeout=REPLAY_TIMEOUT_MS)
    verdict = page.locator("#verdict")
    # the citation check is passed by victims too, so only the rollback is called proof
    sync_api.expect(verdict).to_contain_text("citations verified")
    sync_api.expect(verdict.locator(".suspects li").first).to_contain_text("rollback clears it")
    sync_api.expect(verdict).to_contain_text("victim of cache")
    page.get_by_role("tab", name="LIVE").click()
    sync_api.expect(page.locator("#metaMode")).not_to_have_text("replay")
    # the charts still play the recording's last ticks, so their badge must not call them live
    assert page.locator("#liveText").text_content() != "LIVE"
    assert errors == []


def test_the_first_screen_button_hands_keyboard_focus_to_the_flight_log(page, errors):
    # the run clears the button away with the rest of the empty log; focus must not drop to the page
    page.goto(f"{URL}/console", wait_until="networkidle")
    page.get_by_role("button", name="Replay outage #41").press("Enter")
    sync_api.expect(page.locator("#log")).to_be_focused()
    sync_api.expect(page.locator("#status")).to_contain_text("Done.", timeout=REPLAY_TIMEOUT_MS)
    assert errors == []


def test_the_phone_jump_to_the_verdict_takes_keyboard_focus_there(browser):
    # the pill hides itself once the verdict is in view; focus must land on the verdict, not drop to the page
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(f"{URL}/console#replay/41")
    sync_api.expect(page.locator("#status")).to_contain_text("Done.", timeout=REPLAY_TIMEOUT_MS)
    page.locator("#jumpVerdict").press("Enter")
    sync_api.expect(page.locator("#h-verdict")).to_be_focused()
    sync_api.expect(page.locator("#jumpVerdict")).to_be_hidden()
    context.close()


def test_a_caught_replay_shows_the_rollback_disproving_its_answer(page, errors):
    caught = [r for r in page.request.get(f"{URL}/api/replays").json() if r.get("caught")]
    if not caught:
        pytest.skip("this server lists no caught replays")
    page.goto(f"{URL}/console#replay", wait_until="networkidle")
    page.locator("#startHere button", has_text="caught").click()
    sync_api.expect(page.locator("#metaModel")).to_have_text(caught[0]["model"])
    sync_api.expect(page.locator("#status")).to_contain_text("Done.", timeout=REPLAY_TIMEOUT_MS)
    verdict = page.locator("#verdict")
    sync_api.expect(verdict).to_contain_text("does not stop the alarm")
    sync_api.expect(verdict.locator(".suspects li").first).to_contain_text("rollback does not clear it")
    sync_api.expect(verdict.locator("tr", has_text="Sherlog agent")).to_contain_text("wrong")
    assert errors == []


def test_triage_replaces_the_last_outage_on_screen_even_while_paused(page, errors):
    sample = page.request.get(f"{URL}/api/sample-incident").json()
    alarm = page.request.post(f"{URL}/api/triage", data=sample).json()["alarm"]
    page.goto(f"{URL}/console#replay/41")
    sync_api.expect(page.locator("#status")).to_contain_text("Done.", timeout=REPLAY_TIMEOUT_MS)
    page.locator("#pauseBtn").click()  # a paused console must still show what the triage is about
    page.get_by_role("tab", name="YOUR DATA").click()
    page.locator("#sampleUse").click()
    sync_api.expect(page.locator("#status")).to_contain_text("Loaded the sample")
    page.locator("#triageBtn").click()
    sync_api.expect(page.locator("#status")).to_contain_text("Triage done.")
    assert page.evaluate("frame.alarm") == alarm  # the charts show the upload, not the replay
    sync_api.expect(page.locator("#map .pill")).to_have_count(0)  # no YOU, AI or CULPRIT from outage 41
    sync_api.expect(page.locator("#verdict")).not_to_contain_text("The culprit was")
    assert errors == []


def test_triaging_an_upload_with_its_own_call_graph_draws_its_services_until_a_replay_brings_the_shop_back(page, errors):
    page.goto(f"{URL}/console#data", wait_until="networkidle")  # the buttons listen once the console has its data
    page.locator("#graphSampleUse").click()
    sync_api.expect(page.locator("#status")).to_contain_text("Loaded the custom-graph sample")
    page.locator("#triageBtn").click()
    sync_api.expect(page.locator("#status")).to_contain_text("Triage done.")
    sync_api.expect(page.locator("#map .node")).to_have_count(6)
    sync_api.expect(page.locator(".tele-row")).to_have_count(6)
    sync_api.expect(page.locator("#map [data-service='postgres']")).to_be_visible()
    sync_api.expect(page.locator("#metaSystem")).to_have_text("Your upload · 6 services")
    sync_api.expect(page.locator("#triage")).to_contain_text("postgres would explain more of the outage than inventory")
    page.get_by_role("tab", name="REPLAY").click()
    page.locator("#startHere button").first.click()
    sync_api.expect(page.locator("#map .node")).to_have_count(11)
    sync_api.expect(page.locator("#metaSystem")).to_have_text("Simulated shop · 11 services")
    # an upload on the shop's own 11 names is still an upload
    page.locator("#tab-data").click()
    page.locator("#sampleUse").click()
    page.locator("#triageBtn").click()
    sync_api.expect(page.locator("#status")).to_contain_text("Triage done.")
    sync_api.expect(page.locator("#metaSystem")).to_have_text("Your upload · 11 services")
    assert errors == []


def test_a_keyboard_triage_keeps_focus_on_its_button(page, errors):
    # the button is disabled while the triage runs, which on its own would drop focus to the page
    page.goto(f"{URL}/console#data", wait_until="networkidle")  # the buttons listen once the console has its data
    page.locator("#sampleUse").press("Enter")
    sync_api.expect(page.locator("#status")).to_contain_text("Loaded the sample")
    page.locator("#triageBtn").press("Enter")
    sync_api.expect(page.locator("#status")).to_contain_text("Triage done.")
    sync_api.expect(page.locator("#triageBtn")).to_be_focused()
    assert errors == []


def test_the_header_link_opens_the_real_outages_first_in_the_evidence_view(page, errors):
    page.goto(f"{URL}/console", wait_until="networkidle")  # the link listens once the console has its data
    page.get_by_role("link", name="also tested on 252 real outages").click()
    sync_api.expect(page.get_by_role("tab", name="EVIDENCE")).to_have_attribute("aria-selected", "true")
    sync_api.expect(page.locator("#h-real")).to_be_focused()
    sync_api.expect(page.locator("#evReal tbody tr").first).to_contain_text("92% [88–95]")
    assert errors == []


def test_skip_to_verdict_plays_the_rest_of_the_recording_without_the_waits(page, errors):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{URL}/console", wait_until="networkidle")
    page.get_by_role("button", name="Replay outage #41").click()
    skip = page.get_by_role("button", name="SKIP TO VERDICT")
    sync_api.expect(skip).to_be_visible()
    sync_api.expect(page.locator("#guessHint")).to_be_in_viewport()  # the prompt sits at the top of the charts
    skip.click()
    # a full replay takes about 11 s; the skipped one ends at once, with every recorded event and the same verdict
    sync_api.expect(page.locator("#status")).to_contain_text("Done.", timeout=5_000)
    sync_api.expect(page.locator("#h-verdict")).to_be_focused()
    sync_api.expect(page.locator("#log .log-item.tool")).to_have_count(5)
    sync_api.expect(page.locator("#verdict")).to_contain_text("Root cause: cache")
    sync_api.expect(page.locator("#verdict")).to_contain_text("The culprit was cache")
    sync_api.expect(skip).to_be_hidden()
    assert errors == []


def test_a_verdict_comes_into_view_and_is_ringed_when_it_lands(page, errors):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{URL}/console#replay/41")
    sync_api.expect(page.locator("#verdict .banner")).to_be_visible(timeout=REPLAY_TIMEOUT_MS)
    sync_api.expect(page.locator("#verdict")).to_have_class("landed")
    sync_api.expect(page.locator("#h-verdict")).to_be_in_viewport()
    sync_api.expect(page.locator("#verdict")).not_to_have_class("landed")  # only for a moment
    assert errors == []


# --- the content security policy, the 404 page, layout shift and accessibility -----------------------------------

VIOLATIONS = """window.__blocked = [];
addEventListener('securitypolicyviolation', (e) => window.__blocked.push(`${e.effectiveDirective} ${e.blockedURI}`));"""
SETTLE_MS = 2_500  # long enough for the CDN's modules, the fonts and the first data to arrive


@pytest.mark.parametrize("path, ready", [("/", "#hero3d canvas"), ("/console", "#map .node"),
                                         ("/status", "#rows:not([hidden])"), ("/reference", ".op"),
                                         ("/docs", ".opblock")])
def test_every_page_runs_under_the_content_security_policy(page, errors, path, ready):
    # "/" waits for the 3D shop, drawn by three.js from the CDN, and /docs for Swagger UI's operations
    page.add_init_script(VIOLATIONS)
    page.goto(f"{URL}{path}")
    page.locator(ready).first.wait_for()
    page.wait_for_timeout(SETTLE_MS)
    assert page.evaluate("window.__blocked") == [] and errors == []


def test_the_policy_blocks_an_inline_script_the_page_did_not_ship(page):
    page.add_init_script(VIOLATIONS)
    page.goto(f"{URL}/status")
    page.evaluate("() => document.head.append(Object.assign(document.createElement('script'), "
                  "{ textContent: 'window.injected = true' }))")
    page.wait_for_function("window.__blocked.length > 0")
    assert page.evaluate("window.injected") is None
    assert page.evaluate("window.__blocked") == ["script-src-elem inline"]


def test_a_dead_link_shows_the_404_page_with_a_way_back(page):
    page.add_init_script(VIOLATIONS)
    res = page.goto(f"{URL}/no-such-page")
    assert res.status == 404
    sync_api.expect(page.get_by_role("heading", level=1)).to_have_text("Page not found")
    page.get_by_role("link", name="The console").click()
    page.wait_for_url(f"{URL}/console")
    assert page.evaluate("window.__blocked") == []


LAYOUT_SHIFTS = """window.__shifts = [];
new PerformanceObserver((list) => { for (const e of list.getEntries()) window.__shifts.push(e.value); })
  .observe({ type: 'layout-shift', buffered: true });"""
API_DELAY_S = 0.8  # the deployed site's data arrives after the first paint; the local server's might not
GOOD_CLS = 0.1     # Lighthouse's line between good and needs improvement
SIZES = {"phone": {"width": 412, "height": 823}, "desktop": {"width": 1350, "height": 940}}  # Lighthouse's own


def _slowly(route):
    time.sleep(API_DELAY_S)
    route.continue_()


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("path", ["/", "/console", "/status"])
def test_pages_do_not_shift_while_they_load(browser, path, size):
    context = browser.new_context(viewport=SIZES[size])
    context.add_init_script(LAYOUT_SHIFTS)
    page = context.new_page()
    page.route("**/api/**", _slowly)
    page.goto(f"{URL}{path}")
    page.wait_for_timeout(PAST_REVEAL_FALLBACK_MS)
    shifts = page.evaluate("window.__shifts")
    context.close()
    assert sum(shifts) < GOOD_CLS, shifts  # every shift counted, a stricter sum than Lighthouse's worst window


AXE_URL = "https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js"
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
AXE_SIZES = {"desktop": {"width": 1440, "height": 900}, "phone": {"width": 390, "height": 844}}
# axe judges colours as they are painted, so it waits out every transition and entrance that ends (not the endless
# loops, nor the story page's scroll-driven progress bar): mid-way, a guess card turning red can read darker than either end
SETTLED = """() => Promise.allSettled(document.getAnimations()
  .filter((a) => Number.isFinite(a.effect?.getComputedTiming().endTime)).map((a) => a.finished))"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("size", AXE_SIZES)
@pytest.mark.parametrize("path", ["/", "/console", "/status", "/reference", "/no-such-page"])
def test_axe_finds_no_accessibility_violations(browser, path, size, scheme):
    # the policy would block axe's injected script, so this context bypasses it; the checks above run with it on
    context = browser.new_context(viewport=AXE_SIZES[size], color_scheme=scheme, bypass_csp=True)
    page = context.new_page()
    page.goto(f"{URL}{path}")
    page.wait_for_timeout(PAST_REVEAL_FALLBACK_MS)  # every section revealed, the data loaded
    if path == "/":
        page.locator("#cards.ready").wait_for()  # the guess cards light up one by one after their section shows
    page.evaluate(SETTLED)
    page.add_script_tag(url=AXE_URL)
    result = page.evaluate("(tags) => axe.run(document, { runOnly: { type: 'tag', values: tags } })", AXE_TAGS)
    context.close()
    assert [(v["id"], len(v["nodes"])) for v in result["violations"]] == []
