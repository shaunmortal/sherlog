"""Uploads that bring their own call graph (`calls`), and guards that the shop's agent is unchanged by them."""
import hashlib
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
from test_core import FakeClient, _submit, _tool_use  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402

client = TestClient(app.app)

# a small system that is not the shop: inventory slows down, and orders and gateway, which call it, slow down too
CALLS = {"gateway": ["auth", "orders"], "auth": [], "orders": ["inventory", "payments"], "inventory": [],
         "payments": []}
SERVICES = tuple(CALLS)
CAUSE = "inventory"
SLOWED = ("gateway", "orders", "inventory")
ONSET = 90
LATENCY_MS = {"gateway": 80, "auth": 30, "orders": 50, "inventory": 150, "payments": 40}
FAULT_MS = 400


def _latency(service: str) -> list[float]:
    """Steady callers and a noisy cause, so the z-score ranking blames a caller and only the call graph helps."""
    wobble = 50 if service == CAUSE else 0
    base = [LATENCY_MS[service] + (wobble if t % 2 else -wobble) for t in range(sim.WINDOW_TICKS)]
    return [v + (FAULT_MS if service in SLOWED and t >= ONSET else 0) for t, v in enumerate(base)]


def _series(service: str) -> dict:
    flat = lambda value: [float(value)] * sim.WINDOW_TICKS  # noqa: E731
    return {"latency_ms": _latency(service), "error_rate": flat(0.002), "cpu_pct": flat(20), "memory_mb": flat(300)}


def _doc() -> dict:
    complaints = [{"tick": t, "service": "orders", "level": "WARN",
                   "text": f"rpc inventory.Reserve slow: {550 + t} ms (deadline 500 ms)"} for t in range(ONSET, 110, 2)]
    return {"format": byo.FORMAT, "tick_seconds": sim.TICK_SECONDS, "calls": CALLS,
            "series": {s: _series(s) for s in SERVICES}, "logs": complaints}


def _incident(doc: dict | None = None) -> sim.Incident:
    return byo.to_incident(byo.IncidentDoc.model_validate(doc or _doc()))


def _post(path: str, body: dict):
    return client.post(path, content=json.dumps(body), headers={"content-type": "application/json"})


def _with(change) -> dict:
    doc = json.loads(json.dumps(_doc()))
    change(doc)
    return doc


# --- the document --------------------------------------------------------------------------------------------

def _rename_auth(doc):
    doc["calls"]["Auth"] = doc["calls"].pop("auth")


def _many_services(doc):
    doc["calls"] = {f"svc-{i}": [] for i in range(byo.MAX_SERVICES + 1)}


@pytest.mark.parametrize("change, loc, message", [
    (lambda d: d["series"].pop("payments"), ["body", "series", "payments"], "Field required"),
    (lambda d: d["series"].update(cart=d["series"]["auth"]), ["body", "series", "cart"], "Extra inputs"),
    (lambda d: d["calls"]["orders"].append("billing"), ["body", "calls"],
     "orders calls billing, which is not one of the services in calls"),
    (lambda d: d["calls"]["auth"].append("auth"), ["body", "calls"], "auth calls itself"),
    (lambda d: d["calls"]["orders"].append("payments"), ["body", "calls"], "orders lists payments twice"),
    (_rename_auth, ["body", "calls", "Auth", "[key]"], "'Auth' is not a valid service name"),
    (lambda d: d["calls"]["gateway"].append("pay ments"), ["body", "calls", "gateway", 2], "not a valid service name"),
    (_many_services, ["body", "calls"], f"at most {byo.MAX_SERVICES}"),
    (lambda d: d.update(calls={"gateway": []}), ["body", "calls"], "at least 2"),
    (lambda d: d["logs"][0].update(service="cart"), ["body", "logs", 0, "service"],
     "'cart' is not one of the 5 services in calls"),
    (lambda d: d.update(rpc={"billing": "Bill"}), ["body", "rpc"], "billing is not one of the services in calls"),
    (lambda d: d.update(rpc={"orders": "Get", "inventory": "Get"}), ["body", "rpc"], "Get names both"),
    (lambda d: d.update(rpc={"orders": "rpc call"}), ["body", "rpc", "orders"], "not a valid RPC name"),
])
def test_a_bad_call_graph_is_refused_with_a_useful_422(change, loc, message):
    res = _post("/api/triage", _with(change))
    assert res.status_code == 422
    assert any(e["loc"] == loc and message in e["msg"] for e in res.json()["errors"]), res.json()["errors"]


def test_a_huge_bad_name_is_not_echoed_whole_in_the_message():
    huge = "X" * 5000
    errors = _post("/api/triage", _with(lambda d: d["calls"].update({huge: []}))).json()["errors"]
    assert "not a valid service name" in errors[0]["msg"] and len(errors[0]["msg"]) < 300


def test_without_calls_the_shop_names_still_rule():
    shop = json.loads(json.dumps(byo.sample()))
    shop["logs"][0]["service"] = "inventory"
    errors = _post("/api/triage", shop).json()["errors"]
    assert [e["loc"] for e in errors] == [["body", "logs", 0, "service"]]
    assert "not one of the shop's 11 services" in errors[0]["msg"]
    rpc_alone = _post("/api/triage", {**byo.sample(), "rpc": {"cart": "Fetch"}}).json()["errors"]
    assert rpc_alone[0]["loc"] == ["body", "rpc"] and "needs calls" in rpc_alone[0]["msg"]


def test_a_valid_graph_becomes_an_incident_with_its_own_topology():
    inc = _incident()
    assert inc.services == SERVICES and inc.root is None
    assert inc.callees["orders"] == ("inventory", "payments") and inc.callers["inventory"] == ("orders",)
    assert inc.callers["gateway"] == () and set(inc.series) == set(SERVICES)
    # the shop keeps its own topology
    shop = sim.simulate(*byo.SAMPLE)
    assert shop.services == sim.SERVICES and shop.callers == sim.CALLERS


def test_log_lines_point_at_the_services_they_name_by_name_or_by_rpc():
    doc = _with(lambda d: d.update(rpc={"inventory": "Reserve"}))
    doc["logs"].append({"tick": 100, "service": "gateway", "level": "WARN", "text": "Reserve timed out"})
    abouts = {(x.service, x.text): x.about for x in _incident(doc).logs}
    assert abouts[("gateway", "Reserve timed out")] == CAUSE
    assert all(about == CAUSE for (writer, _), about in abouts.items() if writer == "orders")
    # without the rpc map, an RPC name alone points at nobody else
    assert byo.about("gateway", "Reserve timed out", byo.naming(SERVICES)) == "gateway"
    # the longest name wins where one contains another
    assert byo.about("x", "api-gateway down", byo.naming(("api", "api-gateway", "x"))) == "api-gateway"


# --- the no-AI checks and the agent's tools ------------------------------------------------------------------

def test_the_call_graph_rule_finds_the_deepest_abnormal_service_where_the_rankings_blame_a_caller():
    inc = _incident()
    assert inc.alarm == ONSET + sim.ALARM_SUSTAIN_TICKS - 1
    assert sim.zscore_ranking(inc)[:2] == ["gateway", "orders"] and sim.robust_ranking(inc)[0] != CAUSE
    assert inv.graph_ranking(inc)[0] == CAUSE
    blamed = inv.blast_radius(inc, "gateway")
    assert blamed["abnormal"] == ["gateway", "orders", "inventory"] and blamed["better"] == CAUSE
    assert inv.blast_radius(inc, CAUSE)["unexplained"] == [] and inv.blast_radius(inc, CAUSE)["better"] is None


def test_the_tools_read_the_uploads_own_call_graph():
    inc = _incident()
    deps = inv.run_tool(inc, "get_dependencies", {"service": CAUSE}, "c1")
    assert "calls (downstream): nothing" in deps.text and "called by (upstream): orders (latency_ms" in deps.text
    assert deps.shows == {CAUSE, "orders"}
    logs = inv.run_tool(inc, "get_logs", {"service": "orders"}, "c2")
    assert logs.shows == {CAUSE}
    top = inv.run_tool(inc, "top_anomalies", {}, "c0")
    assert {line.split()[0] for line in top.text.splitlines()[1:]} <= set(SERVICES)  # under the [c0] header
    wrong = inv.run_tool(inc, "get_logs", {"service": "cart"}, "c3")
    assert wrong.error and "known: gateway, auth, orders, inventory, payments" in wrong.text


def test_triage_runs_on_the_uploads_own_services():
    result = byo.triage(_incident())
    assert [z["service"] for z in result["zscore_ranking"]][:3] == ["gateway", "orders", "inventory"]
    assert sorted(result["robust_ranking"]) == sorted(SERVICES)
    assert result["blast"]["better"] == CAUSE


def test_the_agent_gets_the_uploads_services_in_its_prompt_alert_and_tool_schemas():
    inc = _incident()
    fake = FakeClient([
        NS(stop_reason="tool_use", content=[_tool_use("t1", "get_dependencies", {"service": CAUSE})]),
        _submit(CAUSE, 0.9, ["c1"]),
    ])
    events = list(inv.incident_events(inc, client=fake, model=inv.MODELS["haiku"]))
    verdict = next(e for e in events if e["type"] == "verdict")
    request = fake.requests[0]

    assert verdict["answer"] == CAUSE and verdict["causes"][0]["verified"]
    assert request["system"].startswith("You are the on-call engineer for a system built from 5 services. ")
    assert "shop" not in request["system"]
    assert "Services: gateway, auth, orders, inventory, payments." in request["messages"][0]["content"]
    enums = [t["input_schema"]["properties"]["service"]["enum"] for t in request["tools"]
             if "service" in t["input_schema"]["properties"]]
    assert enums and all(e == list(SERVICES) for e in enums)
    suspect = request["tools"][-1]["input_schema"]["properties"]["causes"]["items"]["properties"]["service"]
    assert suspect["enum"] == list(SERVICES)
    assert set(events[0]["series"]) == set(SERVICES) and events[-1]["type"] == "truth"


def test_ablations_keep_their_tools_on_an_upload():
    fake = FakeClient([_submit(CAUSE, 0.9, ["c0"])])
    list(inv.investigate(_incident(), client=fake, tools=inv.tools_without("get_logs")))
    assert [t["name"] for t in fake.requests[0]["tools"]] == [t["name"] for t in inv.tools_without("get_logs")]


def test_the_rollback_replay_says_it_needs_a_simulated_outage():
    with pytest.raises(ValueError, match="simulated outage"):
        inv.rollback_check(_incident(), CAUSE)


def test_export_keeps_the_call_graph():
    doc = byo.export(_incident())
    assert doc["calls"] == CALLS and list(doc["series"]) == list(SERVICES)
    assert "calls" not in byo.export(sim.simulate(*byo.SAMPLE))


# --- the routes ----------------------------------------------------------------------------------------------

@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(app, "_live_enabled", lambda: True)
    monkeypatch.setattr(app, "_runs_by_ip", {})
    monkeypatch.setattr(app, "_runs_today", {"day": "", "count": 0})


def test_triage_route_takes_an_upload_with_its_own_graph():
    result = _post("/api/triage", _doc()).json()
    assert result["alarm"] == ONSET + 2 and result["blast"]["service"] == "gateway"
    assert result["blast"]["better"] == CAUSE


def test_investigate_route_streams_the_agent_on_an_upload_with_its_own_graph(live, monkeypatch):
    fake = FakeClient([_submit(CAUSE, 0.9, ["c0"])])
    monkeypatch.setattr(inv.anthropic, "Anthropic", lambda **_: fake)
    res = _post("/api/investigate", {"incident": _doc(), "model": "haiku"})
    events = [json.loads(chunk.removeprefix("data: ")) for chunk in res.text.split("\n\n") if chunk]
    telemetry, verdict = events[0], events[-1]
    assert res.status_code == 200 and "truth" not in [e["type"] for e in events]
    assert set(telemetry["series"]) == set(telemetry["rest"]) == set(SERVICES)
    assert verdict["answer"] == CAUSE and verdict["blast"]["explained"] == ["gateway", "orders", "inventory"]
    assert "rollback" not in verdict


def test_the_custom_graph_sample_is_a_valid_upload_whose_rankings_need_the_graph():
    doc = client.get("/api/sample-incident", params={"graph": "custom"}).json()
    assert set(doc["calls"]) == set(doc["series"]) and not set(doc["calls"]) & set(sim.SERVICES)
    result = _post("/api/triage", doc).json()
    assert result["alarm"] is not None and result["blast"]["better"] == "postgres"
    assert result["zscore_ranking"][0]["service"] != "postgres"
    assert client.get("/api/sample-incident", params={"graph": "nope"}).status_code == 422


# --- the converter -------------------------------------------------------------------------------------------

T0 = 1_700_000_000


def _prom_answer(doc: dict, metric: str) -> dict:
    result = [{"metric": {"service": s},
               "values": [[T0 + k * sim.TICK_SECONDS, repr(v)] for k, v in enumerate(vs[metric])]}
              for s, vs in doc["series"].items()]
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


def test_converter_takes_the_teams_call_graph(tmp_path):
    doc = _doc()
    inputs = []
    for metric in sim.METRICS:
        path = tmp_path / f"{metric}.json"
        path.write_text(json.dumps(_prom_answer(doc, metric)))
        inputs.append(f"{metric}={path}")
    graph, out = tmp_path / "calls.json", tmp_path / "incident.json"
    graph.write_text(json.dumps(CALLS))

    prom.main([*inputs, "--calls", str(graph), "-o", str(out)])

    converted = json.loads(out.read_text())
    assert converted["calls"] == CALLS and converted["series"] == doc["series"]
    assert byo.triage(byo.to_incident(byo.IncidentDoc.model_validate(converted)))["blast"]["better"] == CAUSE


def test_converter_refuses_a_bad_graph_or_a_service_outside_it(tmp_path):
    answer = _prom_answer(_doc(), "latency_ms")
    with pytest.raises(ValueError, match="'gateway'.*--calls"):
        prom.load_matrix(answer, "latency_ms", "service", {}, services=("auth", "orders"))
    graph = tmp_path / "calls.json"
    graph.write_text(json.dumps({"gateway": ["gateway"], "auth": []}))
    path = tmp_path / "latency.json"
    path.write_text(json.dumps(answer))
    with pytest.raises(SystemExit, match="gateway calls itself"):
        prom.main([f"latency_ms={path}", "--calls", str(graph)])


# --- the shop's agent is unchanged -----------------------------------------------------------------------------

# sha256 of the prompt and tool schemas every recorded run used; the recorded runs reproduce only while these hold
SHOP_PROMPT_SHA256 = "ed359ad5326eef3ce41cdfdc7459afa619d8125598ec4536eb9194781407ed8b"
SHOP_TOOLS_SHA256 = "4f417f826ed736777c410e24355ff5eff2048100437eb3d27f551eae6d4a1cf2"
ONE_SHOT_TOOLS_SHA256 = "20891a23a5c0fca3854306ad4df782381b413932142f7e30a3d62a7cf127ae99"


def _sha(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_shops_prompt_and_tool_schemas_are_byte_identical_to_the_recorded_runs():
    assert _sha(inv.SYSTEM) == SHOP_PROMPT_SHA256
    assert _sha(inv.TOOLS) == SHOP_TOOLS_SHA256
    assert _sha(inv.tools_without("get_logs", "get_metric", "get_dependencies")) == ONE_SHOT_TOOLS_SHA256
    for inc in (sim.simulate("payment", "errors", seed=3), _shop_upload()):
        fake = FakeClient([_submit("payment", 0.9, ["c0"])])
        list(inv.investigate(inc, client=fake))
        assert fake.requests[0]["system"] is inv.SYSTEM and fake.requests[0]["tools"] is inv.TOOLS


def _shop_upload() -> sim.Incident:
    return byo.to_incident(byo.IncidentDoc.model_validate(byo.sample()))
