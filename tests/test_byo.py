"""Bring your own outage: the incident format, its routes and the Prometheus converter."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]  # the app and the converter live outside tests/

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import byo  # noqa: E402
import investigate as inv  # noqa: E402
import prom_to_sherlog as prom  # noqa: E402
import sim  # noqa: E402
from test_core import FakeClient, _submit  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "prom_small.json"
client = TestClient(app.app)


def _sample_with(change) -> str:
    """The sample document as JSON, after `change` edits a fresh copy of it."""
    doc = json.loads(json.dumps(byo.sample()))
    change(doc)
    return json.dumps(doc)


def _post(path: str, body: str):
    return client.post(path, content=body, headers={"content-type": "application/json"})


def test_sample_round_trips_with_the_culprit_removed():
    original = sim.simulate(*byo.SAMPLE)
    doc = client.get("/api/sample-incident").json()
    back = byo.to_incident(byo.IncidentDoc.model_validate_json(json.dumps(doc)))

    assert set(doc) == {"format", "tick_seconds", "series", "logs"} and "about" not in doc["logs"][0]
    assert (back.root, back.fault, back.decoy) == (None, None, None)
    assert (back.alarm, back.now) == (original.alarm, original.now)
    assert sim.zscore_ranking(back) == sim.zscore_ranking(original)
    assert sim.robust_ranking(back) == sim.robust_ranking(original)
    # every tool the agent can call, and so the verifier, sees exactly what it saw in the simulator
    for service in sim.SERVICES:
        for name, args in [("get_logs", {}), ("get_dependencies", {}), ("get_metric", {"metric": "latency_ms"})]:
            a, b = (inv.run_tool(i, name, {"service": service, **args}, "c1") for i in (original, back))
            assert (a.text, a.shows) == (b.text, b.shows)
    assert inv.blast_radius(back, "cache") == inv.blast_radius(original, "cache")


def test_derived_about_matches_the_simulator_on_every_normal_mode_line():
    lines = [line for s in sim.SERVICES for f in sim.FAULTS for line in sim.simulate(s, f, seed=1).logs]
    assert lines and all(byo.about(line.service, line.text) == line.about for line in lines)
    assert byo.about("checkout", "rpc payment.Charge failed: UNAVAILABLE") == "payment"
    assert byo.about("cart", "Get timed out after 200 ms") == "cache"       # an RPC alone names its service
    assert byo.about("db", "GC pause 140 ms") == "db"                       # names nobody: about its writer
    assert byo.about("db", "slow query log: 310 ms") == "db"                # case-sensitive: not db's Query RPC


def _set(path: tuple, value):
    def change(doc):
        *parents, last = path
        target = doc
        for key in parents:
            target = target[key]
        target[last] = value
    return change


CPU = ("series", "cart", "cpu_pct")


@pytest.mark.parametrize("change, loc, message", [
    (lambda d: d["series"].pop("email"), ["body", "series", "email"], "Field required"),
    (_set(("series", "adservice"), {}), ["body", "series", "adservice"], "Extra inputs are not permitted"),
    (_set(CPU, [20.0] * 119), ["body", *CPU], "at least 120 items"),
    (_set((*CPU, 3), 140), ["body", *CPU], "tick 3 is 140, but cpu_pct must be between 0 and 100"),
    (_set((*CPU, 3), float("nan")), ["body", *CPU, 3], "finite number"),
    (_set((*CPU, 3), "20"), ["body", *CPU, 3], "valid number"),
    (_set(("tick_seconds",), 10), ["body", "tick_seconds"], "Input should be 5"),
    (_set(("format",), "sherlog-incident/2"), ["body", "format"], "sherlog-incident/1"),
    (_set(("root",), "cache"), ["body", "root"], "Extra inputs are not permitted"),
    (_set(("logs", 0, "level"), "DEBUG"), ["body", "logs", 0, "level"], "'INFO', 'WARN' or 'ERROR'"),
    (_set(("logs", 0, "text"), "x" * (byo.MAX_LOG_CHARS + 1)), ["body", "logs", 0, "text"], "at most 500"),
    (_set(("logs", 0, "tick"), sim.WINDOW_TICKS), ["body", "logs", 0, "tick"], "less than 120"),
    (lambda d: d.update(logs=d["logs"][:1] * (byo.MAX_LOG_LINES + 1)), ["body", "logs"], "at most 2000"),
])
def test_invalid_documents_are_refused_with_a_useful_422(change, loc, message):
    res = _post("/api/triage", _sample_with(change))
    assert res.status_code == 422
    assert any(e["loc"] == loc and message in e["msg"] for e in res.json()["errors"]), res.json()["errors"]
    assert f"{'.'.join(map(str, loc[1:]))}: " in res.json()["detail"]


def test_bodies_over_the_cap_are_refused_with_413_declared_or_streamed():
    too_big = b" " * (app.MAX_BODY_MB * 1_000_000 + 1)
    assert client.post("/api/triage", content=too_big).status_code == 413
    streamed = client.post("/api/triage", content=iter([too_big[:1_000_000]] * (app.MAX_BODY_MB + 1)))
    assert streamed.status_code == 413 and "MB" in streamed.json()["detail"]
    assert _post("/api/triage", "{").status_code == 422


def test_triage_on_the_sample_sees_past_the_loud_victim():
    root = byo.SAMPLE[0]
    result = _post("/api/triage", json.dumps(byo.sample())).json()
    assert result["alarm"] == sim.simulate(*byo.SAMPLE).alarm
    assert result["zscore_ranking"][0]["service"] == "cart"  # the classic ranking blames a victim of the cache
    assert result["robust_ranking"][0] == root
    assert result["blast"]["service"] == "cart" and result["blast"]["better"] == root


def test_triage_of_a_healthy_shop_reports_no_alarm():
    result = _post("/api/triage", json.dumps(byo.export(sim.simulate(None, None, app.HEALTHY_SEED)))).json()
    assert result["alarm"] is None and len(result["robust_ranking"]) == len(sim.SERVICES)


def _investigate(incident: dict):
    return _post("/api/investigate", json.dumps({"incident": incident, "model": "opus"}))


def test_investigate_needs_live_mode(monkeypatch):
    monkeypatch.setattr(app, "_live_enabled", lambda: False)
    res = _investigate(byo.sample())
    assert res.status_code == 503 and "/api/triage" in res.json()["detail"]


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(app, "_live_enabled", lambda: True)
    monkeypatch.setattr(app, "_runs_by_ip", {})
    monkeypatch.setattr(app, "_runs_today", {"day": "", "count": 0})


def test_investigate_refuses_bad_or_quiet_telemetry_without_spending_a_run(live):
    healthy = byo.export(sim.simulate(None, None, app.HEALTHY_SEED))
    assert "No alarm" in _investigate(healthy).json()["detail"]
    assert _post("/api/investigate", json.dumps({"incident": byo.sample(), "model": "gpt-5"})).status_code == 422
    assert app._runs_today["count"] == 0


def test_investigate_streams_the_agent_on_uploaded_telemetry_with_no_truth_reveal(live, monkeypatch):
    fake = FakeClient([_submit(byo.SAMPLE[0], 0.9, ["c0"])])
    monkeypatch.setattr(inv.anthropic, "Anthropic", lambda: fake)
    res = _investigate(byo.sample())
    events = [json.loads(chunk.removeprefix("data: ")) for chunk in res.text.split("\n\n") if chunk]
    kinds = [e["type"] for e in events]
    verdict = events[-1]
    assert res.status_code == 200 and kinds[0] == "telemetry" and "truth" not in kinds
    assert verdict["type"] == "verdict" and verdict["answer"] == "cache"
    assert verdict["blast"]["unexplained"] == [] and app._runs_today["count"] == 1


# --- the Prometheus converter ----------------------------------------------------------------

T0 = 1_700_000_000  # the fixture's first sample, 22:13:20 UTC
FIXTURE_NAMES = {"cartservice": "cart", "paymentservice": "payment"}


def test_converter_maps_services_and_fills_a_single_missing_reading():
    series = prom.load_matrix(json.loads(FIXTURE.read_text()), None, "service", FIXTURE_NAMES)
    ticks = [T0 + k * sim.TICK_SECONDS for k in range(7)]
    assert set(series) == {("cart", "latency_ms"), ("payment", "cpu_pct")}
    # +10 s is missing and +20 s is NaN: each a straight line between its neighbours; +30 s holds the last sample
    assert prom.resample(series[("cart", "latency_ms")], ticks, "cart latency_ms") == [10, 12, 14, 16, 17, 18, 18]


def test_converter_refuses_long_gaps_missing_series_and_unmapped_services():
    series = prom.load_matrix(json.loads(FIXTURE.read_text()), None, "service", FIXTURE_NAMES)
    # two readings missing in a row, between samples or after the last one: refused, never invented
    with pytest.raises(ValueError, match=r"payment cpu_pct has no samples from 22:13:20 UTC to 22:13:35 UTC \(15 s\)"):
        prom.resample(series[("payment", "cpu_pct")], [T0 + 5], "payment cpu_pct")
    with pytest.raises(ValueError, match=r"step=5s"):
        prom.resample(series[("cart", "latency_ms")], [T0 + 35], "cart latency_ms")
    with pytest.raises(ValueError, match="no data for 42 series"):
        prom.convert(series)
    with pytest.raises(ValueError, match="'cartservice'.*--map"):
        prom.load_matrix(json.loads(FIXTURE.read_text()), None, "service", {})


def test_converter_command_line_turns_the_sample_back_into_the_same_incident(tmp_path):
    doc = byo.sample()
    inputs = []
    for metric in sim.METRICS:  # the sample as one query_range answer per metric, services under their own names
        result = [{"metric": {"service": f"{s}service"},
                   "values": [[T0 + k * sim.TICK_SECONDS, repr(v)] for k, v in enumerate(doc["series"][s][metric])]}
                  for s in sim.SERVICES]
        path = tmp_path / f"{metric}.json"
        path.write_text(json.dumps({"status": "success", "data": {"resultType": "matrix", "result": result}}))
        inputs.append(f"{metric}={path}")
    out = tmp_path / "incident.json"

    prom.main([*inputs, *(f"--map={s}service={s}" for s in sim.SERVICES), "-o", str(out)])

    converted = json.loads(out.read_text())
    assert converted["series"] == doc["series"]
    assert byo.triage(byo.to_incident(byo.IncidentDoc.model_validate(converted))) == \
        byo.triage(byo.to_incident(byo.IncidentDoc.model_validate(doc)))
