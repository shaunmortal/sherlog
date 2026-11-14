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


def test_console_plays_a_recorded_outage_to_its_verdict(page, errors):
    page.goto(f"{URL}/console")
    page.get_by_role("tab", name="REPLAY").click()
    page.locator("#replayGrid button").first.click()
    sync_api.expect(page.locator("#status")).to_contain_text("Done.", timeout=REPLAY_TIMEOUT_MS)
    sync_api.expect(page.locator("#verdict")).to_contain_text("The culprit was")
    assert errors == []
