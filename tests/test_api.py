"""The web API through FastAPI's TestClient: every route, every failure path of /api/break and the live stream's
format. Nothing here calls the Claude API: live runs use a dummy key and a fake investigation or a fake client."""
import json
import re
import subprocess
import time
from types import SimpleNamespace as NS

import anthropic
import pytest
from fastapi.testclient import TestClient

import app
import investigate
import sim

BREAK = {"service": "payment", "fault": "errors", "model": "opus"}
DUMMY_KEY = "test-key-not-real"


@pytest.fixture
def api():
    return TestClient(app.app)


@pytest.fixture
def offline(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def live(monkeypatch):
    """Live mode with fresh rate limits. The key is a dummy: each test using it fakes the agent or the client."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", DUMMY_KEY)
    monkeypatch.setattr(app, "_runs_by_ip", {})
    monkeypatch.setattr(app, "_runs_today", {"day": "", "count": 0})


def _fake_investigation(inc, **_):
    yield investigate.telemetry_event(inc)
    yield {"type": "thinking", "text": "Checkout only fails because payment does."}
    yield {"type": "verdict", "answer": inc.root, "causes": [], "escalation": None}


@pytest.fixture
def fake_agent(live, monkeypatch):
    monkeypatch.setattr(investigate, "incident_events", _fake_investigation)


def _events(res) -> list[dict]:
    """Parse a live stream, holding it to the exact "data: {json}\\n\\n" framing the console reads."""
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
    assert res.text.endswith("\n\n")
    chunks = res.text.split("\n\n")[:-1]
    assert all(chunk.startswith("data: ") for chunk in chunks)
    return [json.loads(chunk.removeprefix("data: ")) for chunk in chunks]


# --- pages and read-only routes ------------------------------------------------------------

@pytest.mark.parametrize("path, marker", [("/", 'href="/console"'), ("/console", 'id="telemetry"'),
                                          ("/status", 'role="status"')])
def test_every_page_is_served_as_html(api, path, marker):
    res = api.get(path)
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    assert marker in res.text


def test_status_page_explains_itself_without_javascript(api):
    page = api.get("/status").text
    assert "<noscript>" in page and 'href="/api/health"' in page


def test_meta_carries_what_the_console_draws_from(api):
    meta = api.get("/api/meta").json()
    assert {"services", "calls", "faults", "metrics", "live", "model", "models", "benchmark", "evidence",
            "baseline_ticks", "std_floor", "healthy"} <= set(meta)
    assert meta["services"] == list(sim.SERVICES) and set(meta["healthy"]) == set(sim.SERVICES)
    assert set(meta["models"]) == set(investigate.MODELS) and meta["evidence"]["agent"]["outages"] > 0


def test_health_without_a_key_says_replays_only_and_names_the_deployed_commit(api, offline, monkeypatch):
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "0123456789abcdef")
    health = api.get("/api/health").json()
    assert health == {
        "ok": True,
        "live": False,
        "models": [*investigate.MODELS, investigate.AUTO],
        "recorded_outages": len(api.get("/api/replays").json()),
        "live_runs_today": 0,
        "live_runs_per_day": app.LIVE_RUNS_PER_DAY,
        "live_runs_per_visitor_per_hour": app.LIVE_RUNS_PER_IP_PER_HOUR,
        "server_time": health["server_time"],
        "version": "0123456",
    }
    assert health["recorded_outages"] > 0
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", health["server_time"])


def test_health_counts_todays_live_runs_and_never_shows_the_key(api, fake_agent, monkeypatch):
    monkeypatch.delenv("VERCEL_GIT_COMMIT_SHA", raising=False)
    api.post("/api/break", json=BREAK)
    res = api.get("/api/health")
    assert res.json()["live"] and res.json()["live_runs_today"] == 1
    assert res.json()["version"] == app.GIT_VERSION
    assert DUMMY_KEY not in res.text


def test_version_falls_back_to_dev_without_git(monkeypatch):
    def no_git(*_, **__):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert app._git_version() == "dev"


def test_replays_are_numbered_and_each_plays_to_the_truth(api):
    listed = api.get("/api/replays").json()
    assert [r["id"] for r in listed] == list(range(1, len(listed) + 1))
    assert all(set(r) == {"id", "hard"} for r in listed)  # numbered, never named: the culprit stays hidden
    kinds = [e["type"] for e in api.get("/api/replays/1").json()]
    assert kinds[0] == "telemetry" and "verdict" in kinds and kinds[-1] == "truth"


@pytest.mark.parametrize("number", [0, -1, 10_000])
def test_a_missing_replay_is_404_in_plain_words(api, number):
    res = api.get(f"/api/replays/{number}")
    assert res.status_code == 404 and res.json() == {"detail": "No recorded outage with that number."}


def test_a_replay_number_that_is_not_a_number_is_422_in_plain_words(api):
    res = api.get("/api/replays/latest")
    assert res.status_code == 422 and res.json()["detail"].startswith("Bad input. number:")


# --- /api/break: every way it can say no -----------------------------------------------------

def test_break_without_a_key_is_503_and_points_to_the_replays(api, offline):
    res = api.post("/api/break", json=BREAK)
    assert res.status_code == 503 and "recorded outage" in res.json()["detail"]


def test_break_with_an_unknown_service_is_422_and_uses_up_no_live_run(api, live):
    res = api.post("/api/break", json={**BREAK, "service": "mainframe"})
    assert res.status_code == 422
    assert res.json()["detail"].startswith("Bad input. service:") and "payment" in res.json()["detail"]
    assert app._runs_today["count"] == 0


def test_break_is_429_with_the_reason_once_a_visitor_used_their_hour(api, fake_agent):
    for _ in range(app.LIVE_RUNS_PER_IP_PER_HOUR):
        assert api.post("/api/break", json=BREAK).status_code == 200
    res = api.post("/api/break", json=BREAK)
    assert res.status_code == 429 and "this hour" in res.json()["detail"]


def test_break_is_429_with_the_reason_once_the_day_is_used_up(api, fake_agent, monkeypatch):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    monkeypatch.setattr(app, "_runs_today", {"day": today, "count": app.LIVE_RUNS_PER_DAY})
    res = api.post("/api/break", json=BREAK)
    assert res.status_code == 429 and "Today's live runs are used up" in res.json()["detail"]


# --- the live stream ---------------------------------------------------------------------------

def test_live_stream_is_sse_lines_that_end_in_a_checked_verdict(api, fake_agent):
    events = _events(api.post("/api/break", json=BREAK))
    assert [e["type"] for e in events] == ["telemetry", "thinking", "verdict"]
    assert set(events[0]["rest"]) == set(sim.SERVICES)            # the charts keep running after the cut-off
    assert events[-1]["blast"]["service"] == BREAK["service"]   # the answer's blast-radius check rides along


def test_live_runs_use_a_client_that_gives_up_on_a_stuck_call(api, live, monkeypatch):
    used = []

    def remember_client(inc, client=None, **_):
        used.append(client)
        yield from _fake_investigation(inc)

    monkeypatch.setattr(investigate, "incident_events", remember_client)
    _events(api.post("/api/break", json=BREAK))
    assert used[0].timeout == app.LIVE_CALL_TIMEOUT_SECONDS and used[0].max_retries == app.LIVE_CALL_RETRIES


def _times_out_after(*turns):
    """A Claude client that gives these model turns, then times out like a stuck API call."""
    queue = list(turns)

    def create(**_):
        if not queue:
            raise anthropic.APITimeoutError(request=None)
        return queue.pop(0)

    return NS(beta=NS(messages=NS(create=create)))


def test_an_api_error_mid_investigation_is_an_error_event_and_the_stream_still_ends(api, live, monkeypatch):
    read_logs = NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="get_logs",
                                                       input={"service": "checkout"})])
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: _times_out_after(read_logs))
    events = _events(api.post("/api/break", json=BREAK))
    assert [e["type"] for e in events] == ["telemetry", "tool", "tool", "error", "verdict", "truth"]
    assert events[3]["message"] == investigate._api_error_message(anthropic.APITimeoutError(request=None))
    assert events[4]["answer"] is None and events[4]["escalation"].startswith("investigation failed")


def test_an_unexpected_error_outside_the_agent_still_ends_the_stream_with_a_reason(api, live, monkeypatch):
    def breaks_after_the_telemetry(inc, **_):
        yield investigate.telemetry_event(inc)
        raise RuntimeError("boom")

    monkeypatch.setattr(investigate, "incident_events", breaks_after_the_telemetry)
    events = _events(api.post("/api/break", json=BREAK))
    assert [e["type"] for e in events] == ["telemetry", "error"]
    assert events[-1]["message"] == app.STREAM_FAILED
