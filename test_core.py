"""Core checks: simulator, alarm, tools, verifier and the agent loop (with a fake model)."""
from types import SimpleNamespace as NS

import pytest

import investigate as inv
import sim


def test_same_seed_gives_same_incident():
    a = sim.simulate("payment", "latency", seed=7)
    b = sim.simulate("payment", "latency", seed=7)
    assert (a.series, a.logs, a.alarm, a.onset) == (b.series, b.logs, b.alarm, b.onset)


def test_simulate_rejects_unknown_service_or_half_a_fault():
    with pytest.raises(ValueError):
        sim.simulate("nope", "latency", seed=1)
    with pytest.raises(ValueError):
        sim.simulate("payment", None, seed=1)


def test_healthy_shop_raises_no_alarm():
    assert all(sim.simulate(None, None, seed=s).alarm is None for s in range(30))


@pytest.mark.parametrize("service", sim.SERVICES)
@pytest.mark.parametrize("fault", sim.FAULTS)
def test_every_fault_raises_an_alarm_after_onset(service, fault):
    inc = sim.simulate(service, fault, seed=1)
    assert inc.alarm is not None and inc.alarm >= inc.onset


def test_latency_fault_spreads_to_callers_but_not_to_unrelated_services():
    inc = sim.simulate("payment", "latency", seed=3)
    assert inc.window_z("payment", "latency_ms") > sim.ALARM_Z
    assert inc.window_z("checkout", "latency_ms") > sim.ALARM_Z      # victim
    assert abs(inc.window_z("catalog", "latency_ms")) < sim.ALARM_Z  # unrelated


def test_callers_log_about_the_failing_callee():
    inc = sim.simulate("payment", "errors", seed=2)
    about = {line.about for line in inc.logs if line.service == "checkout" and line.level == "WARN"}
    assert "payment" in about


def test_zscore_ranking_lists_every_service_once():
    ranking = sim.zscore_ranking(sim.simulate("cache", "cpu", seed=4))
    assert sorted(ranking) == sorted(sim.SERVICES)


def test_verifier_accepts_cited_evidence_and_rejects_the_rest():
    inc = sim.simulate("payment", "errors", seed=3)
    logs = inv.run_tool(inc, "get_logs", {"service": "checkout"}, "c1")
    quiet = inv.run_tool(inc, "get_metric", {"service": "catalog", "metric": "error_rate"}, "c2")
    calls = {c.id: c for c in (logs, quiet)}

    assert inv.verify_cause({"service": "payment", "evidence": ["c1"]}, calls)[0]
    assert not inv.verify_cause({"service": "catalog", "evidence": ["c2"]}, calls)[0]  # cited, but normal
    assert not inv.verify_cause({"service": "payment", "evidence": ["c9"]}, calls)[0]  # never made
    assert not inv.verify_cause({"service": "payment", "evidence": []}, calls)[0]


def test_unknown_tool_input_returns_an_error_instead_of_raising():
    call = inv.run_tool(sim.simulate("payment", "errors", seed=2), "get_logs", {"service": "x"}, "c1")
    assert call.error and not call.shows


def _tool_use(tool_id, name, args):
    return NS(type="tool_use", id=tool_id, name=name, input=args)


class FakeClient:
    """Stands in for anthropic.Anthropic(): returns scripted model turns in order."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        return self.turns.pop(0)


def test_agent_runs_tools_then_its_submission_is_verified():
    inc = sim.simulate("payment", "errors", seed=3)
    submission = {
        "causes": [
            {"service": "payment", "fault_type": "errors", "reason": "r", "confidence": 0.9, "evidence": ["c1"]},
            {"service": "catalog", "fault_type": "unknown", "reason": "r", "confidence": 0.1, "evidence": ["c0"]},
        ],
        "ticket_note": "note",
        "customer_update": "update",
    }
    fake = FakeClient([
        NS(stop_reason="tool_use", content=[
            NS(type="thinking", thinking="Checkout looks like a victim."),
            _tool_use("t1", "get_logs", {"service": "checkout"}),
        ]),
        NS(stop_reason="tool_use", content=[_tool_use("t2", "submit_ranking", submission)]),
    ])

    events = list(inv.incident_events(inc, client=fake))
    kinds = [e["type"] for e in events]
    verdict = next(e for e in events if e["type"] == "verdict")
    truth = events[-1]

    assert kinds[0] == "telemetry" and "thinking" in kinds and kinds[-1] == "truth"
    assert verdict["answer"] == "payment"
    assert [c["verified"] for c in verdict["causes"]] == [True, False]
    assert truth["ai_top1"] and truth["answered"]
    # the second request carried the tool result back to the model
    assert fake.requests[1]["messages"][-1]["content"][0]["tool_use_id"] == "t1"


def test_low_confidence_answer_is_escalated():
    calls = {"c1": inv.Call("c1", "get_logs", {}, "", frozenset({"payment"}), False)}
    raw = {"causes": [{"service": "payment", "confidence": 0.3, "evidence": ["c1"]}]}
    verdict = inv.build_verdict(raw, calls)
    assert verdict["answer"] is None and "confidence" in verdict["escalation"]


def test_agent_that_never_submits_is_escalated():
    inc = sim.simulate("payment", "errors", seed=2)
    silent = NS(stop_reason="end_turn", content=[NS(type="text", text="hmm")])
    fake = FakeClient([silent] * (inv.MAX_TOOL_ROUNDS + 2))
    verdict = list(inv.investigate(inc, client=fake))[-1]
    assert verdict["type"] == "verdict" and verdict["answer"] is None


def test_unexpected_error_still_ends_with_an_escalated_verdict():
    class Broken:
        beta = NS(messages=NS(create=lambda **_: (_ for _ in ()).throw(RuntimeError("boom"))))

    events = list(inv.investigate(sim.simulate("payment", "errors", seed=3), client=Broken()))
    assert [e["type"] for e in events][-2:] == ["error", "verdict"]
    assert events[-1]["answer"] is None and "unexpected" in events[-1]["escalation"]


def test_replay_carries_the_ticks_after_the_cutoff_for_the_live_charts():
    from fastapi.testclient import TestClient

    import app
    import bench

    client = TestClient(app.app)
    meta = client.get("/api/meta").json()
    healthy = sim.simulate(None, None, app.HEALTHY_SEED)
    assert healthy.alarm is None
    assert meta["healthy"]["cache"]["latency_ms"] == list(healthy.series["cache"]["latency_ms"])

    if not bench.load():
        pytest.skip("no recorded outages")
    record = bench.load()[0]
    telemetry = client.get("/api/replays/1").json()[0]
    full = sim.simulate(record["root"], record["fault"], record["seed"]).series["cart"]["cpu_pct"]
    # the agent's view plus the rest is exactly the whole simulated window
    assert telemetry["series"]["cart"]["cpu_pct"] + telemetry["rest"]["cart"]["cpu_pct"] == list(full)
    assert len(telemetry["series"]["cart"]["cpu_pct"]) == telemetry["now"] + 1


def test_sparse_samples_are_joined_by_straight_lines():
    assert sim._join_samples([0, 9, 9, 6, 9], [0, 3, 4]) == [0, 2, 4, 6, 9]


def test_healthy_shops_rarely_raise_a_false_alarm():
    false_alarms = sum(sim.simulate(None, None, seed=s).alarm is not None for s in range(300))
    assert false_alarms <= 6  # about 0.4% measured over 1500 shops


def test_break_rejects_an_unknown_model_before_spending_anything(monkeypatch):
    from fastapi.testclient import TestClient

    import app

    monkeypatch.setattr(app, "_live_enabled", lambda: True)
    res = TestClient(app.app).post("/api/break", json={"service": "cart", "fault": "cpu", "model": "gpt-5"})
    assert res.status_code == 422


def _submit(service, confidence, evidence):
    cause = {"service": service, "fault_type": "errors", "reason": "r", "confidence": confidence, "evidence": evidence}
    return NS(stop_reason="tool_use", content=[_tool_use("s", "submit_ranking",
              {"causes": [cause], "ticket_note": "n", "customer_update": "u"})])


def test_auto_route_hands_over_to_the_strong_model_only_when_the_cheap_one_cannot_answer():
    inc = sim.simulate("payment", "errors", seed=3)
    # Haiku is unsure, so Opus investigates afresh and its verified answer is final
    unsure_then_sure = FakeClient([_submit("payment", 0.4, ["c0"]), _submit("payment", 0.9, ["c0"])])
    events = list(inv.incident_events(inc, client=unsure_then_sure, model=inv.AUTO))
    handoff = next(e for e in events if e["type"] == "handoff")
    verdict = next(e for e in events if e["type"] == "verdict")
    assert [r["model"] for r in unsure_then_sure.requests] == list(inv.AUTO_ROUTE)
    assert handoff["to"] == inv.MODELS["opus"] and verdict["route"] == list(inv.AUTO_ROUTE)
    assert verdict["answer"] == "payment" and sum(e["type"] == "verdict" for e in events) == 1
    # Haiku is sure: Opus is never called
    sure = FakeClient([_submit("payment", 0.9, ["c0"])])
    events = list(inv.incident_events(inc, client=sure, model=inv.AUTO))
    assert len(sure.requests) == 1 and not any(e["type"] == "handoff" for e in events)
    # Haiku gets no server-side fallback (it has none); Opus does
    assert "fallbacks" not in unsure_then_sure.requests[0] and unsure_then_sure.requests[1]["fallbacks"] == "default"


def test_blast_radius_flags_an_answer_that_cannot_explain_the_outage():
    inc = sim.simulate("cache", "latency", 1041)
    assert inv.blast_radius(inc, "cache")["unexplained"] == []
    assert inv.blast_radius(inc, "cart")["unexplained"] == ["cache"]  # cart's trouble can't slow the cache


def test_hard_mode_decoy_is_harmless_and_unrelated_to_the_real_fault():
    for seed in range(20):
        inc = sim.simulate("cart", "latency", seed, hard=True)
        assert inc.decoy not in {"cart", *sim.callers_of("cart")}
        assert inc.decoy is not None and inc.alarm is not None
    # normal mode is unchanged by the hard-mode code path: no decoy, same telemetry as always
    assert sim.simulate("cart", "latency", 5).decoy is None
    assert sim.simulate("cart", "latency", 5).series == sim.simulate("cart", "latency", 5).series


def test_hard_mode_callers_complain_less():
    def complaints(hard):
        return sum(1 for s in range(10) for line in sim.simulate("payment", "errors", s, hard=hard).logs
                   if "rpc payment" in line.text)
    assert complaints(True) < 0.6 * complaints(False)


def test_rate_limit_ignores_forged_forwarded_headers(monkeypatch):
    import app

    monkeypatch.setattr(app, "_runs_by_ip", {})
    monkeypatch.setattr(app, "_runs_today", {"day": "", "count": 0})
    ip = app._client_ip(NS(headers={"x-forwarded-for": "1.2.3.4"}, client=NS(host="9.9.9.9")))
    assert ip == "9.9.9.9"
    for _ in range(app.LIVE_RUNS_PER_IP_PER_HOUR):
        assert app._take_live_slot(ip) is None
    assert "this hour" in app._take_live_slot(ip)
    assert list(app._runs_by_ip) == ["9.9.9.9"]


def test_story_page_console_and_shared_assets_are_served():
    from fastapi.testclient import TestClient

    import app

    client = TestClient(app.app)
    story, console = client.get("/"), client.get("/console")
    assert story.status_code == console.status_code == 200
    assert "/console" in story.text and 'id="telemetry"' in console.text
    for asset in ("/static/shared.css", "/static/shared.js"):
        assert client.get(asset).status_code == 200
