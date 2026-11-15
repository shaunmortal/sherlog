"""Every recorded run reproduces from its seed with today's code: no API calls, no key.

One test per recorded agent run (runs/*.jsonl), one per replay, and one for runs/evidence.json. See
docs/reproducibility.md. After a deliberate change to what a replay shows, re-pin the replay checks with
`python -m tests.test_reproducibility` and say why in the commit.
"""
import json
from functools import lru_cache
from pathlib import Path

import pytest

import app
import bench
import investigate
import sim

RUNS = [(variant, record) for variant in bench.VARIANTS for record in bench.load(bench.runs_path(variant))]
# the replays' blast-radius and rollback checks are computed on every request, never recorded with the run,
# so they are pinned here: generated from this code, not from the recordings (see docs/reproducibility.md)
REPLAY_CHECKS = Path(__file__).parent / "fixtures" / "replay_checks.json"
PINNED = json.loads(REPLAY_CHECKS.read_text()) if REPLAY_CHECKS.exists() else []  # missing: the count test fails
ENRICHED = ("rest", "blast", "rollback")  # what a replay adds to the recorded events
CAUSE_FIELDS = ("service", "fault_type", "reason", "confidence", "evidence")  # a suspect as submitted, cleaned


@lru_cache(maxsize=None)
def _incident(root: str, fault: str, seed: int, hard: bool) -> sim.Incident:
    # every variant ran the same 176 outages: simulate each once
    return sim.simulate(root, fault, seed, hard)


def _json(event: dict) -> dict:
    """An event as the run file stores it: tuples become lists, floats round-trip exactly."""
    return json.loads(json.dumps(event))


@pytest.mark.parametrize("variant, record", [pytest.param(v, r, id=f"{v}-seed-{r['seed']}") for v, r in RUNS])
def test_recorded_run_reproduces_from_its_seed(variant, record):
    inc = _incident(record["root"], record["fault"], record["seed"], record.get("hard", False))
    events = record["events"]
    offered = {t["name"] for t in bench.VARIANTS[variant][0]}

    # the telemetry the agent saw
    assert _json(investigate.telemetry_event(inc)) == events[0], "telemetry differs from the recording"

    # every tool call, re-run with its recorded arguments, gives the recorded text
    tools = [e for e in events if e["type"] == "tool"]
    assert [e["id"] for e in tools] == [f"c{i}" for i in range(len(tools))]
    calls = {}
    for event in tools:
        assert event["id"] == "c0" or event["name"] in offered, f"{event['id']} used a tool this variant lacks"
        call = investigate.run_tool(inc, event["name"], event["args"], event["id"])
        assert _json(investigate._tool_event(call)) == event, f"{event['id']} differs from the recording"
        calls = {**calls, call.id: call}

    # the recorded suspects, re-verified against those calls, are accepted and rejected as recorded
    verdict = next(e for e in events if e["type"] == "verdict")
    raw = {"causes": [{k: c[k] for k in CAUSE_FIELDS} for c in verdict["causes"]],
           "ticket_note": verdict["ticket_note"], "customer_update": verdict["customer_update"]}
    today = investigate.build_verdict(raw, calls)
    assert today == {k: verdict[k] for k in today}, "the verifier now judges this verdict differently"

    # and the scoring: the z-score control's ranking, AC@1 and AC@3 (runs recorded before hard mode have no decoy key)
    assert record["truth"] == events[-1]
    assert investigate.truth_event(inc, verdict) == {"decoy": None, **record["truth"]}


def test_every_replay_is_pinned():
    assert [p["replay"] for p in PINNED] == list(range(1, len(app._replays()) + 1))


def _replay_checks(number: int, replayed: list[dict]) -> dict:
    """What the console shows for one replay beyond the recording: its answer's blast radius and rollback."""
    entry = app._replays()[number - 1]
    verdict = next(e for e in replayed if e["type"] == "verdict")
    return {"replay": number, "id": entry["record"]["id"], "model": entry["record"].get("model", bench.OPUS),
            "caught": entry["caught"], "blast": verdict.get("blast"), "rollback": verdict.get("rollback")}


@pytest.mark.parametrize("pinned", [pytest.param(p, id=f"replay-{p['replay']}") for p in PINNED])
def test_replay_blast_radius_and_rollback_match_the_pinned_checks(pinned):
    number = pinned["replay"]
    record = app._replays()[number - 1]["record"]
    replayed = app.replay(number)

    assert _replay_checks(number, replayed) == pinned
    # a replay only adds to the recorded events, it never changes one
    assert [{k: v for k, v in e.items() if k not in ENRICHED} for e in replayed] == record["events"]
    if pinned["blast"]:
        assert pinned["blast"]["service"] == pinned["rollback"]["service"] == record["truth"]["ai_answer"]


def test_bench_evidence_reproduces_runs_evidence_json_byte_for_byte():
    # exactly what `python bench.py --report` writes, from the recorded runs alone
    assert json.dumps(bench.evidence()) == bench.EVIDENCE.read_text()


if __name__ == "__main__":
    lines = [json.dumps(_replay_checks(n, app.replay(n))) for n in range(1, len(app._replays()) + 1)]
    REPLAY_CHECKS.write_text("[\n" + ",\n".join(lines) + "\n]\n")
    print(f"pinned {len(lines)} replays in {REPLAY_CHECKS}")
