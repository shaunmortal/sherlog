"""Browser checks against a running server, skipped unless SHERLOG_E2E_URL is set. They play recorded outages
only, so no API key is needed. Needs Playwright for Python (pip install playwright) and Chrome:

    uvicorn app:app --port 8000 &
    SHERLOG_E2E_URL=http://127.0.0.1:8000 pytest -q tests/test_e2e.py
"""
import os

import pytest

URL = os.environ.get("SHERLOG_E2E_URL", "").rstrip("/")
if not URL:
    pytest.skip("set SHERLOG_E2E_URL to run the browser checks", allow_module_level=True)
sync_api = pytest.importorskip("playwright.sync_api")

REPLAY_TIMEOUT_MS = 120_000  # a recorded outage plays back at reading speed


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
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
    sync_api.expect(page.locator("#heroTitle")).to_be_visible()
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


def test_a_keyboard_triage_keeps_focus_on_its_button(page, errors):
    # the button is disabled while the triage runs, which on its own would drop focus to the page
    page.goto(f"{URL}/console#data", wait_until="networkidle")  # the buttons listen once the console has its data
    page.locator("#sampleUse").press("Enter")
    sync_api.expect(page.locator("#status")).to_contain_text("Loaded the sample")
    page.locator("#triageBtn").press("Enter")
    sync_api.expect(page.locator("#status")).to_contain_text("Triage done.")
    sync_api.expect(page.locator("#triageBtn")).to_be_focused()
    assert errors == []
