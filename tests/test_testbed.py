"""The real-system testbed's pure parts: the fault plan, telemetry -> document, the rollback decision and the
spend cap, plus a check of the recorded documents. No server is started; the experiment itself is
`python -m testbed.run` (docs/real-system.md)."""
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import byo  # noqa: E402
import investigate as inv  # noqa: E402
import sim  # noqa: E402
from testbed import outage as plan_  # noqa: E402
from testbed import run  # noqa: E402
from testbed.system import CALLS, SERVICES, callers_of  # noqa: E402

BASE_MS = {"gateway": 3.0, "auth": 0.3, "catalog": 0.2, "orders": 3.0, "inventory": 1.0, "payments": 2.0,
           "store": 0.4, "ledger": 0.3}
ONSET = 80
# a store slowed by 400 ms, and everything that calls it slowed with it, as on the real testbed
SLOWED = {"store": 400, "inventory": 400, "payments": 400, "orders": 640, "gateway": 190}


def _reading(service: str, t: int, slow: bool = False, cpu: float = 2.0) -> dict:
    wobble = 0.3 if t % 2 else -0.3  # a little noise, so the baseline is not flat
    extra = SLOWED.get(service, 0) if slow else 0
    return {"latency_ms": BASE_MS[service] + wobble + extra, "error_rate": 0.0, "cpu_pct": cpu + wobble,
            "memory_mb": 20.0 + wobble}


def _window(slow_from: int | None = ONSET) -> list[dict]:
    return [{s: _reading(s, t, slow_from is not None and t >= slow_from) for s in SERVICES}
            for t in range(sim.WINDOW_TICKS)]


def _after(slow: bool = False, settle_slow: bool = False, cpu: dict | None = None) -> dict:
    """The 2 minutes after a rollback, as series: healthy, or still slow, or slow only while it settles."""
    ticks = [{s: _reading(s, t, slow or (settle_slow and t < plan_.SETTLE_TICKS), (cpu or {}).get(s, 2.0))
              for s in SERVICES} for t in range(plan_.ROLLBACK_TICKS)]
    return plan_.series_of(ticks)[0]


def _doc(window: list[dict] | None = None, logs=()) -> dict:
    return plan_.to_document(window or _window(), list(logs), start=1000.0)[0]


# --- the plan --------------------------------------------------------------------------------

def test_the_plan_is_seeded_and_a_bigger_plan_extends_a_smaller_one():
    small, big = plan_.plan(20, 10), plan_.plan(32, 16)
    assert small == plan_.plan(20, 10)
    assert small == big[:len(small)]
    assert len({o.id for o in big}) == len(big)
    assert plan_.plan(20, 10, seed=1) != small


def test_the_plan_balances_faults_and_starts_them_in_the_onset_range():
    outages = plan_.plan(20, 10)
    normal = [o for o in outages if not o.hard]
    assert [o.hard for o in outages[:3]] == [False, False, True]  # two normal, then one hard
    assert all(sum(o.fault == f for o in normal) == 5 for f in plan_.FAULTS)
    assert all(sim.ONSET_RANGE[0] <= o.onset <= sim.ONSET_RANGE[1] for o in outages)
    first_32 = plan_.plan(32, 0)
    assert len({(o.service, o.fault) for o in first_32}) == 32  # every (service, fault) pair once


def test_a_hard_outage_decoy_is_harmless_by_construction():
    for o in plan_.plan(0, 32):
        assert o.decoy != o.service and o.decoy not in callers_of(o.service)  # the fault cannot reach it
        assert o.onset <= o.decoy_onset <= o.onset + 2
        lo, hi = plan_.DECOY_CPU_DUTY if o.decoy_kind == "cpu" else plan_.DECOY_WARMUP_MB
        assert o.decoy_kind in ("cpu", "memory") and lo <= o.decoy_level <= hi
    assert all(o.decoy is None for o in plan_.plan(8, 0))


def test_outage_ids_give_nothing_away():
    for o in plan_.plan(32, 16):
        assert not any(word in o.id for word in (*SERVICES, *plan_.FAULTS))


# --- telemetry -> document -------------------------------------------------------------------

def test_the_window_becomes_a_document_the_server_accepts_without_the_truth():
    doc = _doc()
    res = TestClient(app.app).post("/api/triage", json=doc)

    assert res.status_code == 200, res.text
    assert set(doc) == {"format", "tick_seconds", "calls", "rpc", "series", "logs"}  # no culprit, fault or onset
    assert doc["calls"] == {s: list(c) for s, c in CALLS.items()}
    assert res.json()["alarm"] == ONSET + sim.ALARM_SUSTAIN_TICKS - 1
    assert inv.graph_ranking(plan_.incident(doc))[0] == "store"  # the call graph sees past the slowed callers


def test_missing_readings_hold_the_last_one_and_are_counted():
    window = _window()
    window[10] = {**window[10], "auth": {**window[10]["auth"], "latency_ms": None}}
    window[0] = {**window[0], "ledger": {**window[0]["ledger"], "error_rate": None}}
    doc, filled = plan_.to_document(window, [], start=0.0)

    assert filled == 2
    assert doc["series"]["auth"]["latency_ms"][10] == doc["series"]["auth"]["latency_ms"][9]
    assert doc["series"]["ledger"]["error_rate"][0] == doc["series"]["ledger"]["error_rate"][1]


def test_readings_are_clamped_to_the_format_limits_and_a_silent_metric_is_refused():
    window = [{s: {**r, "cpu_pct": 130.0} for s, r in tick.items()} for tick in _window()]
    assert set(plan_.to_document(window, [], 0.0)[0]["series"]["store"]["cpu_pct"]) == {100.0}
    silent = [{s: {**r, "latency_ms": None} if s == "ledger" else r for s, r in tick.items()} for tick in _window()]
    with pytest.raises(ValueError, match="ledger reported no latency_ms"):
        plan_.to_document(silent, [], 0.0)
    with pytest.raises(ValueError, match="120 ticks"):
        plan_.to_document(_window()[:-1], [], 0.0)


def test_log_lines_land_on_their_tick_and_lines_outside_the_window_are_dropped():
    start = 1000.0
    logs = [(start + 5 * 90 + 2.5, "orders", "WARN", "rpc inventory.Reserve slow: 412 ms (threshold 250 ms)"),
            (start + 5 * 3, "store", "WARNING", "a WARNING from another logger"),
            (start - 1, "store", "INFO", "Query listening"),                      # before the window
            (start + 5 * sim.WINDOW_TICKS, "store", "INFO", "after the window"),
            (start + 7, "store", "DEBUG", "not a level the format has"),
            (start + 8, "store", "INFO", "   ")]
    lines = _doc(logs=logs)["logs"]

    assert lines == [{"tick": 3, "service": "store", "level": "WARN", "text": "a WARNING from another logger"},
                     {"tick": 90, "service": "orders", "level": "WARN",
                      "text": "rpc inventory.Reserve slow: 412 ms (threshold 250 ms)"}]
    assert byo.about("orders", lines[1]["text"], byo.naming(SERVICES, plan_.RPC)) == "inventory"


def test_too_many_log_lines_keep_every_warning_first():
    quiet = [(1000.0 + i * 0.1, "auth", "INFO", f"Verify: {i} requests") for i in range(1500)]
    loud = [(1001.0 + i * 0.1, "orders", "WARN", f"rpc payments.Charge failed: HTTP 500 ({i})") for i in range(900)]
    lines = plan_.window_logs(quiet + loud, start=1000.0)

    assert len(lines) == byo.MAX_LOG_LINES
    assert sum(line["level"] == "WARN" for line in lines) == 900
    kept = {line["text"] for line in lines}
    assert "Verify: 399 requests" not in kept and "Verify: 400 requests" in kept  # the oldest INFO lines go
    assert [line["tick"] for line in lines] == sorted(line["tick"] for line in lines)


# --- the rollback decision -------------------------------------------------------------------

def test_rolling_back_the_cause_stops_the_alarm():
    decision = plan_.rollback_decision(_doc(), _after())
    assert decision == {"alarm_after": False, "still_alarming": [], "harm_ended": True, "proves": True}


def test_rolling_back_a_victim_leaves_the_alarm_on():
    decision = plan_.rollback_decision(_doc(), _after(slow=True))
    assert decision["proves"] is False and "store.latency_ms" in decision["still_alarming"]


def test_the_first_ticks_after_a_rollback_are_not_judged():
    assert plan_.rollback_decision(_doc(), _after(settle_slow=True))["proves"] is True


def test_a_decoy_that_slows_nobody_cannot_keep_the_proof_away():
    decision = plan_.rollback_decision(_doc(), _after(cpu={"catalog": 90.0}))
    assert decision["alarm_after"] and decision["still_alarming"] == ["catalog.cpu_pct"]
    assert decision["harm_ended"] and decision["proves"]


def test_without_a_latency_or_error_alarm_a_lingering_cpu_alarm_rejects_the_rollback():
    """Like the simulator's rollback check: a memory leak slows no one, so if a CPU decoy keeps the alarm on
    after the leak is rolled back, nothing proves the leak was the cause."""
    window = [{s: {**r, "memory_mb": 20.0 + (40.0 if s == "auth" and t >= ONSET else 0.0)} for s, r in tick.items()}
              for t, tick in enumerate(_window(slow_from=None))]
    doc = _doc(window)
    assert plan_.rollback_decision(doc, _after())["proves"] is True
    assert plan_.rollback_decision(doc, _after(cpu={"catalog": 90.0}))["proves"] is False


# --- the recorded experiment -----------------------------------------------------------------

RECORDS = [json.loads(line) for line in (run.OUT / "outages.jsonl").read_text().splitlines()
           if line.strip()] if (run.OUT / "outages.jsonl").exists() else []


@pytest.mark.skipif(not RECORDS, reason="no real-system outages recorded")
def test_every_recorded_document_passes_the_server_and_reproduces_its_triage():
    client = TestClient(app.app)
    for record in RECORDS:
        text = (run.OUT / record["doc"]).read_text()
        res = client.post("/api/triage", content=text, headers={"content-type": "application/json"})
        assert res.status_code == 200, (record["id"], res.text)
        assert res.json() == record["triage"], record["id"]
        assert hashlib.sha256(text.encode()).hexdigest() == record["doc_sha256"]
        assert record["truth"]["service"] not in record["id"] and "truth" not in json.loads(text)


# --- the spend cap ---------------------------------------------------------------------------

def test_the_budget_never_lets_a_run_break_the_cap_and_keeps_room_for_auto():
    budget = run.Budget(cap=1.0, spent=0.0, auto_left=3)
    assert budget.hold("auto")                       # 0.15 held
    assert budget.hold("opus")                       # 0.15 + 0.20 + 2 autos still to come at 0.03: fits
    assert budget.settle("auto", 0.01) == 0.01
    assert budget.settle("opus", None) == run.HOLD_USD["opus"]  # unknown cost: charged the worst case
    assert budget.spent == pytest.approx(0.21) and budget.held == pytest.approx(0.0)
    tight = run.Budget(cap=0.30, spent=0.0, auto_left=2)
    assert tight.hold("auto") and not tight.hold("opus")  # Opus would leave no room for the next auto run
    assert not run.Budget(cap=0.10, spent=0.0, auto_left=1).hold("auto")


def test_a_run_costs_both_models_when_auto_hands_off_and_nothing_known_when_it_failed():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    haiku, opus = inv.MODELS["haiku"], inv.MODELS["opus"]
    assert run.run_cost({"route": [haiku], "usage": usage}) == 0.10
    assert run.run_cost({"route": [haiku, opus], "first_usage": usage, "usage": usage}) == 4.10
    assert run.run_cost({"model": opus, "usage": usage}) == 4.00
    assert run.run_cost({"causes": [], "escalation": "investigation failed"}) is None


# --- teardown --------------------------------------------------------------------------------

def test_stopping_a_bed_whose_service_already_exited_does_not_raise(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT)  # exited, not reaped: macOS refuses killpg with EPERM
    bed = run.Bed(0, tmp_path)
    bed.procs = {"gateway": p}
    bed.stop()
    assert p.returncode == 0 and bed.procs == {}
    run.signal_group(p, signal.SIGTERM)  # reaped: its pid may be someone else's now, so it is not signalled


def test_stopping_a_bed_ends_its_running_process_groups(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    bed = run.Bed(0, tmp_path)
    bed.procs = {"gateway": p}
    bed.stop()
    assert p.returncode == -signal.SIGTERM

