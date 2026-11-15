"""Uploads that bring their own metric names (`metrics`), where a service may report only some of them, and guards
that the shop, which always has the four, is unchanged by them."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT)]

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import byo  # noqa: E402
import investigate as inv  # noqa: E402
import sim  # noqa: E402
from test_core import FakeClient, _submit  # noqa: E402

client = TestClient(app.app)

# gateway -> orders -> db. db's disk saturates at ONSET: it slows orders (whose latency is measured) and gateway.
# db reports no latency of its own, only disk and cpu, as a database container often does.
CALLS = {"gateway": ["orders"], "orders": ["db"], "db": []}
METRICS = ["latency_ms", "cpu", "diskio"]
ONSET = 60
ALARM = ONSET + sim.ALARM_SUSTAIN_TICKS - 1  # the first tick with 3 abnormal readings in a row


def _wobble(level: float, share: float, t: int) -> float:
    return level * (1 + (share if t % 2 else -share))


def _series() -> dict:
    ticks = range(sim.WINDOW_TICKS)
    slow = lambda t: 300 if t >= ONSET else 0  # noqa: E731
    return {
        "gateway": {"latency_ms": [_wobble(80, 0.05, t) + slow(t) for t in ticks],
                    "cpu": [_wobble(0.2, 0.05, t) for t in ticks]},
        "orders": {"latency_ms": [_wobble(50, 0.05, t) + slow(t) for t in ticks],
                   "cpu": [_wobble(0.1, 0.05, t) for t in ticks]},
        "db": {"cpu": [_wobble(0.3, 0.05, t) for t in ticks],
               "diskio": [_wobble(2e6, 0.05, t) * (40 if t >= ONSET else 1) for t in ticks]},
    }


def _doc(**changes) -> dict:
    doc = {"format": byo.FORMAT, "tick_seconds": sim.TICK_SECONDS, "calls": CALLS, "metrics": METRICS,
           "series": _series(), "logs": []}
    return {**doc, **changes}


def _incident(doc: dict | None = None) -> sim.Incident:
    return byo.to_incident(byo.IncidentDoc.model_validate(doc or _doc()))


def _post(path: str, body: dict):
    return client.post(path, content=json.dumps(body), headers={"content-type": "application/json"})


def test_an_upload_brings_its_own_metrics_and_a_service_reports_only_some():
    inc = _incident()
    assert inc.metrics == tuple(METRICS)
    assert inc.measured == {"gateway": ("latency_ms", "cpu"), "orders": ("latency_ms", "cpu"), "db": ("cpu", "diskio")}
    assert set(inc.stats) == {(s, m) for s in inc.services for m in inc.measured[s]}
    assert inc.alarm == ALARM and inc.worst("db")[0] == "diskio"


def test_the_shop_and_an_upload_without_metrics_keep_the_four():
    shop = sim.simulate(*byo.SAMPLE)
    upload = byo.to_incident(byo.IncidentDoc.model_validate(byo.sample()))
    for inc in (shop, upload):
        assert inc.metrics == sim.METRICS and all(inc.measured[s] == sim.METRICS for s in inc.services)
    assert "metrics" not in byo.export(shop)


def test_a_metric_sherlog_has_no_floor_for_gets_one_scaled_to_its_level():
    assert sim.std_floor("latency_ms", 1e6) == sim.STD_FLOOR["latency_ms"]  # the four keep their own
    assert sim.std_floor("diskio", 2e6) == pytest.approx(2e6 * sim.GENERIC_FLOOR_SHARE)
    assert sim.std_floor("errors", 0.0) == sim.GENERIC_FLOOR_MIN  # a metric that is flat at zero still has one
    doc = _doc()
    doc["series"]["db"]["cpu"] = [0.3] * sim.WINDOW_TICKS  # perfectly flat: no division by zero
    assert _incident(doc).z("db", "cpu", 100) == 0.0


@pytest.mark.parametrize("change, loc, message", [
    (lambda d: d["series"]["db"].update(latency=d["series"]["db"]["cpu"]), ["body", "series", "db", "latency"],
     "Extra inputs are not permitted"),
    (lambda d: d["series"].update(db={}), ["body", "series", "db"], "reports none of the metrics"),
    (lambda d: d.update(metrics=["cpu", "cpu"]), ["body", "metrics"], "cpu is listed twice"),
    (lambda d: d.update(metrics=["Disk IO"]), ["body", "metrics", 0], "not a valid metric name"),
    (lambda d: d.update(metrics=[]), ["body", "metrics"], "at least 1"),
    (lambda d: d.update(metrics=[f"m{i}" for i in range(byo.MAX_METRICS + 1)]), ["body", "metrics"],
     f"at most {byo.MAX_METRICS}"),
    (lambda d: d["series"]["db"]["diskio"].pop(), ["body", "series", "db", "diskio"], "at least 120 items"),
    # one of the four keeps its meaning and its limits under its own name
    (lambda d: d["series"]["gateway"]["latency_ms"].__setitem__(5, -1.0), ["body", "series", "gateway", "latency_ms"],
     "latency_ms must be at least 0.1"),
])
def test_a_bad_metric_list_or_series_is_refused_with_a_useful_422(change, loc, message):
    doc = _doc()
    change(doc)
    res = _post("/api/triage", doc)
    assert res.status_code == 422
    assert any(e["loc"] == loc and message in e["msg"] for e in res.json()["errors"]), res.json()["errors"]


def test_without_metrics_every_service_still_needs_all_four():
    doc = json.loads(json.dumps(byo.sample()))
    doc["series"]["cart"].pop("cpu_pct")
    errors = _post("/api/triage", doc).json()["errors"]
    assert any(e["loc"] == ["body", "series", "cart", "cpu_pct"] and "Field required" in e["msg"] for e in errors)


def test_the_tools_offer_the_uploads_metrics_and_refuse_one_a_service_does_not_report():
    inc = _incident()
    tools = inv.tools_for(inc, inv.TOOLS)
    get_metric = next(t for t in tools if t["name"] == "get_metric")
    assert get_metric["input_schema"]["properties"]["metric"]["enum"] == METRICS
    call = inv.run_tool(inc, "get_metric", {"service": "db", "metric": "latency_ms"}, "c1")
    assert call.error and "unknown metric 'latency_ms'; known: cpu, diskio" in call.text
    shown = inv.run_tool(inc, "get_metric", {"service": "db", "metric": "diskio"}, "c2")
    assert not shown.error and shown.shows == {"db"}
    top = inv.run_tool(inc, "top_anomalies", {}, "c0")
    assert "diskio" in top.text and "db" in top.shows


def test_an_upload_with_its_own_metrics_but_the_shops_services_gets_the_metrics_in_its_tools():
    doc = json.loads(json.dumps(byo.sample()))
    doc["metrics"] = ["latency_ms", "socket"]
    doc["series"] = {s: {"latency_ms": m["latency_ms"], "socket": [10.0] * sim.WINDOW_TICKS}
                     for s, m in doc["series"].items()}
    inc = byo.to_incident(byo.IncidentDoc.model_validate(doc))
    tools = inv.tools_for(inc, inv.TOOLS)
    assert tools is not inv.TOOLS
    assert next(t for t in tools if t["name"] == "get_metric")["input_schema"]["properties"]["metric"]["enum"] == \
        ["latency_ms", "socket"]


def test_only_latency_and_errors_travel_up_to_callers():
    # db's disk is the only thing wrong with db; its callers are slow. db reports no latency, so the call graph
    # rule cannot say db explains its callers, and the blast-radius check reports them as unexplained.
    inc = _incident()
    blast = inv.blast_radius(inc, "db")
    assert set(blast["abnormal"]) == {"gateway", "orders", "db"} and set(blast["unexplained"]) == {"gateway", "orders"}
    # orders' own latency is abnormal, and that does travel: it explains gateway too
    assert set(inv.blast_radius(inc, "orders")["explained"]) == {"orders", "gateway"}


def test_the_telemetry_event_and_the_export_carry_only_what_each_service_reports():
    inc = _incident()
    event = inv.telemetry_event(inc)
    assert set(event["series"]["db"]) == {"cpu", "diskio"} and set(event["z"]["gateway"]) == {"latency_ms", "cpu"}
    doc = byo.export(inc)
    assert doc["metrics"] == METRICS and set(doc["series"]["db"]) == {"cpu", "diskio"}
    back = _incident(doc)
    assert (back.alarm, back.measured, sim.zscore_ranking(back)) == (inc.alarm, inc.measured, sim.zscore_ranking(inc))


def test_an_upload_naming_the_four_but_leaving_some_out_exports_its_metrics_and_round_trips():
    doc = json.loads(json.dumps(byo.sample()))
    doc["metrics"] = list(sim.METRICS)
    doc["series"]["db"].pop("latency_ms")
    inc = byo.to_incident(byo.IncidentDoc.model_validate(doc))
    back = byo.export(inc)
    assert back["metrics"] == list(sim.METRICS)
    assert byo.to_incident(byo.IncidentDoc.model_validate(back)).measured == inc.measured


def test_the_agent_is_told_the_uploads_metrics():
    fake = FakeClient([_submit("db", 0.9, ["c0"])])
    events = list(inv.investigate(_incident(), client=fake))
    alert = fake.requests[0]["messages"][0]["content"]
    assert "Metrics: latency_ms, cpu, diskio." in alert
    assert events[-1]["answer"] == "db"


def test_triage_route_takes_an_upload_with_its_own_metrics():
    body = _post("/api/triage", _doc()).json()
    assert body["alarm"] == ALARM and {r["service"] for r in body["zscore_ranking"]} == set(CALLS)
    assert next(r for r in body["zscore_ranking"] if r["service"] == "db")["metric"] == "diskio"


def test_a_large_system_fits_under_the_raised_service_cap():
    services = [f"svc-{i}" for i in range(byo.MAX_SERVICES)]
    calls = {s: ([services[i + 1]] if i + 1 < len(services) else []) for i, s in enumerate(services)}
    flat = [1.0] * sim.WINDOW_TICKS
    doc = _doc(calls=calls, metrics=["cpu"], series={s: {"cpu": flat} for s in services})
    assert byo.MAX_SERVICES >= 70
    res = _post("/api/triage", doc)
    assert res.status_code == 200 and len(json.dumps(doc)) < app.MAX_BODY_MB * 1_000_000
