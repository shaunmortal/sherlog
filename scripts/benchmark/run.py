"""Run a Sherlog method on the converted benchmark cases and record what it did, case by case.

    python scripts/benchmark/run.py DOCS_DIR --set dev --method zscore            # no AI, free
    uv run --env-file .env --with-requirements requirements.txt \\
        python scripts/benchmark/run.py DOCS_DIR --set test --method auto --budget-usd 6 --workers 4

DOCS_DIR holds convert.py's documents (<case id>.json). Each is read through the same upload checks as
POST /api/triage. Methods: zscore, robust and graph are Sherlog's no-AI rankings; auto (Claude Haiku 5.5 first,
Claude Opus 5.5 when Haiku's answer does not stand) and opus are the agent, live.

Two records per case: the full event stream under --full-dir (outside the repository: tool results quote the
telemetry), and a slim line in runs/real-benchmark/<method>.jsonl with no telemetry: the ranking, the verdict's
structure, the tool calls' names, arguments and the services each showed abnormal, usage and cost. Finished cases
are skipped on a re-run. The live methods stop starting new cases once spend plus a reserve reaches the budget.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import bench  # noqa: E402
import byo  # noqa: E402
import investigate  # noqa: E402
import sim  # noqa: E402

RUNS = ROOT / "runs" / "real-benchmark"
SPLIT = RUNS / "split.json"
NO_AI = {"zscore": sim.zscore_ranking, "robust": sim.robust_ranking, "graph": investigate.graph_ranking}
LIVE = {"auto": investigate.AUTO, "opus": investigate.MODELS["opus"]}
SEED = 2026  # the order of the recorded test runs: random.Random(2026).shuffle over split.json's test ids
RETRIES = 2  # whole-case retries after an API failure the SDK's own retries did not absorb
API_TIMEOUT_SECONDS, API_RETRIES = 300, 4


class Metered:
    """The API client, adding up what every response cost, so failed and retried investigations are paid for too."""

    def __init__(self, client):
        self._client, self._lock, self.usd = client, threading.Lock(), 0.0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        response = self._client.beta.messages.create(**request)
        model = response.model if response.model in bench.USD_PER_MTOK else request["model"]
        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        with self._lock:
            self.usd += bench.cost_usd(usage, model)
        return response


def load_incident(path: Path) -> sim.Incident:
    return byo.to_incident(byo.IncidentDoc.model_validate_json(path.read_text()))


def _usage_cost(verdict: dict) -> tuple[dict, float]:
    """Usage per model and the case's cost: the auto route pays for Haiku and, when it hands over, for Opus."""
    route = verdict.get("route") or [verdict.get("model")]
    usage = {route[-1]: verdict.get("usage")}
    if len(route) == 2:
        usage[route[0]] = verdict.get("first_usage")
    cost = sum(bench.cost_usd(u, m) or 0.0 for m, u in usage.items() if m)
    return usage, round(cost, 6)


def slim(case_id: str, method: str, inc: sim.Incident, events: list[dict], wall: float) -> dict:
    """What is committed: no tool text, no thinking, no free text from the model (it can quote the telemetry)."""
    verdict = next(e for e in events if e["type"] == "verdict")
    tools = []
    for e in (e for e in events if e["type"] == "tool"):
        shows = sorted(investigate.run_tool(inc, e["name"], e["args"], e["id"]).shows) if not e["error"] else []
        tools.append({"id": e["id"], "name": e["name"], "args": e["args"], "error": e["error"], "shows": shows})
    causes = [{k: c[k] for k in ("service", "fault_type", "confidence", "verified", "verify_note", "evidence")}
              for c in verdict["causes"]]
    usage, cost = _usage_cost(verdict)
    answer = verdict["answer"]
    blast = investigate.blast_radius(inc, answer) if answer else None
    return {
        "case_id": case_id, "method": method, "alarm": inc.alarm, "now": inc.now,
        "ranks": [c["service"] for c in causes if c["verified"]],  # scored: suspects the verifier accepted
        "submitted": [c["service"] for c in causes],
        "answer": answer, "escalation": verdict["escalation"], "causes": causes,
        "route": verdict.get("route") or [verdict.get("model")],
        "handoff": next((e["reason"] for e in events if e["type"] == "handoff"), None),
        "blast": blast and {k: blast[k] for k in ("abnormal", "explained", "unexplained", "better")},
        "tools": tools, "usage": usage, "cost_usd": cost, "wall_s": round(wall, 1),
        "errors": [e["message"] for e in events if e["type"] == "error"],
    }


def run_live(case_id: str, path: Path, method: str, client) -> tuple[dict, list[dict]]:
    inc = load_incident(path)
    for attempt in range(RETRIES + 1):
        started = time.monotonic()
        events = [e for e in investigate.incident_events(inc, client=client, model=LIVE[method])
                  if e["type"] not in ("telemetry", "truth")]  # telemetry is in the document; nobody scores truth
        record = slim(case_id, method, inc, events, time.monotonic() - started)
        failed = any(str(e).startswith(("Claude API", "Could not reach", "The Claude API")) for e in record["errors"])
        if not failed or attempt == RETRIES:
            return record, events
        time.sleep(20 * (attempt + 1))
    raise AssertionError("unreachable")


def run_no_ai(case_id: str, path: Path, method: str) -> dict:
    inc = load_incident(path)
    return {"case_id": case_id, "method": method, "alarm": inc.alarm, "now": inc.now,
            "ranks": list(NO_AI[method](inc))}


def seeded(ids: list[str], seed: int) -> list[str]:
    order = list(ids)
    random.Random(seed).shuffle(order)
    return order


def done_ids(path: Path) -> set[str]:
    return {json.loads(line)["case_id"] for line in path.read_text().splitlines()} if path.exists() else set()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("docs", type=Path)
    parser.add_argument("--set", choices=("dev", "test"), required=True)
    parser.add_argument("--method", choices=(*NO_AI, *LIVE), required=True)
    parser.add_argument("--cases", nargs="*", help="only these case ids (in this order)")
    parser.add_argument("--seed", type=int, default=SEED,
                        help="without --cases, the set's cases in this seeded random order, so a run the budget "
                             "stops leaves a random remainder")
    parser.add_argument("--limit", type=int, help="at most this many cases")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--budget-usd", type=float, default=0.0, help="live methods: stop before this much spend")
    parser.add_argument("--reserve-usd", type=float, default=0.25, help="spend kept back per case in flight")
    parser.add_argument("--full-dir", type=Path, help="where the full event streams go (required for live)")
    args = parser.parse_args()

    split = json.loads(SPLIT.read_text())
    ids = args.cases or seeded(split[args.set], args.seed)
    assert set(ids) <= set(split[args.set]), "a case outside the chosen set"
    out = RUNS / args.set / f"{args.method}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    todo = [c for c in ids if c not in done_ids(out)][:args.limit]

    if args.method in NO_AI:
        with out.open("a") as f:
            for case_id in todo:
                f.write(json.dumps(run_no_ai(case_id, args.docs / f"{case_id}.json", args.method)) + "\n")
        print(f"{len(todo)} cases -> {out}")
        return

    import anthropic
    if args.full_dir is None or args.budget_usd <= 0:
        parser.error("live methods need --full-dir and --budget-usd")
    args.full_dir.mkdir(parents=True, exist_ok=True)
    full = args.full_dir / f"{args.set}-{args.method}.jsonl"
    client = Metered(anthropic.Anthropic(timeout=API_TIMEOUT_SECONDS, max_retries=API_RETRIES))
    lock, in_flight = threading.Lock(), [0]
    pending = list(todo)

    def next_case() -> str | None:
        with lock:
            if not pending or client.usd + (in_flight[0] + 1) * args.reserve_usd > args.budget_usd:
                return None
            in_flight[0] += 1
            return pending.pop(0)

    def worker() -> int:
        n = 0
        while (case_id := next_case()) is not None:
            record, events = run_live(case_id, args.docs / f"{case_id}.json", args.method, client)
            with lock:
                in_flight[0] -= 1
                with out.open("a") as f:
                    f.write(json.dumps(record) + "\n")
                with full.open("a") as f:
                    f.write(json.dumps({"case_id": case_id, "method": args.method, "events": events}) + "\n")
                print(f"{case_id} {args.method}: ranks {record['ranks'][:3]} answer {record['answer']} "
                      f"route {record['route']} ${record['cost_usd']:.4f} (metered total ${client.usd:.4f}) "
                      f"{record['errors'] or ''}", flush=True)
            n += 1
        return n

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        finished = sum(f.result() for f in as_completed([pool.submit(worker) for _ in range(args.workers)]))
    print(f"{finished} of {len(todo)} cases, metered ${client.usd:.4f}; {len(pending)} not started (budget)")
    with (RUNS / "spend.jsonl").open("a") as f:  # every live session's metered spend, failed calls included
        f.write(json.dumps({"set": args.set, "method": args.method, "cases": finished, "usd": round(client.usd, 6),
                            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}) + "\n")


if __name__ == "__main__":
    main()
