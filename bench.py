"""Benchmark: the agent vs a no-AI z-score ranking on seeded outages (every service x fault).

    uv run --env-file .env python bench.py --n 44     # about $0.05 per outage on Claude Opus 5.5
    uv run python bench.py --report                   # print the tables from runs/bench.jsonl

Benchmark seeds start at 1000. The prompt and tools were developed on seeds below 1000, so these
outages were never looked at while building them. Finished outages are skipped on a re-run.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from itertools import product
from pathlib import Path

import investigate
import sim

RUNS = Path(__file__).parent / "runs" / "bench.jsonl"
SEED_BASE = 1000
# the full agent, and ablations that take capabilities away to show what each one adds
OPUS, SONNET, HAIKU = (investigate.MODELS[k] for k in ("opus", "sonnet", "haiku"))
ONE_SHOT_TOOLS = investigate.tools_without("get_logs", "get_metric", "get_dependencies")
HARD_SEED_BASE = 2000
VARIANTS = {  # name -> (tools, model, hard mode)
    "agent": (investigate.TOOLS, OPUS, False),
    "no-logs": (investigate.tools_without("get_logs"), OPUS, False),
    "one-shot": (ONE_SHOT_TOOLS, OPUS, False),
    "sonnet": (investigate.TOOLS, SONNET, False),
    "haiku": (investigate.TOOLS, HAIKU, False),
    # hard mode: a decoy anomaly at the same time, and callers that log far fewer complaints
    "hard-one-shot": (ONE_SHOT_TOOLS, OPUS, True),
    "hard-haiku": (investigate.TOOLS, HAIKU, True),
    "hard-sonnet": (investigate.TOOLS, SONNET, True),
    "hard-agent": (investigate.TOOLS, OPUS, True),
}
VARIANT_LABELS = {
    "one-shot": "Claude Opus 5.5, one prompt (sees only the top anomalies, no tools)",
    "no-logs": "Sherlog agent without log access",
    "haiku": "Sherlog agent on Claude Haiku 5.5",
    "sonnet": "Sherlog agent on Claude Sonnet 5.5",
    "agent": "Sherlog agent on Claude Opus 5.5",
    "auto": "Sherlog auto: Claude Haiku 5.5 first, Opus 5.5 only if Haiku's answer does not stand",
}
VARIANT_LABELS.update({f"hard-{k}": v for k, v in VARIANT_LABELS.items() if k in ("one-shot", "haiku", "sonnet", "agent", "auto")})
# list prices per million tokens (input, output), for prompts under 100K tokens
USD_PER_MTOK = {OPUS: (4.00, 20.00), SONNET: (2.00, 10.00), HAIKU: (0.10, 0.50)}
Z95 = 1.96


def runs_path(variant: str) -> Path:
    return RUNS if variant == "agent" else RUNS.with_name(f"bench-{variant}.jsonl")


def cases(n: int, seed_base: int = SEED_BASE) -> list[tuple[str, str, int]]:
    combos = list(product(sim.SERVICES, sim.FAULTS))
    return [(*combos[i % len(combos)], seed_base + i) for i in range(n)]


def run_case(root: str, fault: str, seed: int, tools: list[dict] = investigate.TOOLS, model: str = OPUS,
             hard: bool = False) -> dict:
    events = list(investigate.incident_events(sim.simulate(root, fault, seed, hard), tools=tools, model=model))
    verdict = next((e for e in events if e["type"] == "verdict"), {})
    return {
        "id": f"{root}-{fault}-{seed}",
        "root": root,
        "fault": fault,
        "seed": seed,
        "model": model,
        "hard": hard,
        "truth": events[-1],
        "usage": verdict.get("usage"),
        "errors": [e["message"] for e in events if e["type"] == "error"],
        "events": events,
    }


def load(path: Path = RUNS) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def cost_usd(usage: dict | None, model: str = OPUS) -> float | None:
    if not usage:
        return None
    usd_in, usd_out = USD_PER_MTOK[model]
    return (usage.get("input_tokens", 0) * usd_in + usage.get("output_tokens", 0) * usd_out) / 1e6


def wilson(hits: int, n: int) -> list[float] | None:
    """95% Wilson score interval for a share; unlike +-2 standard errors it stays sensible at 100%."""
    if not n:
        return None
    p = hits / n
    centre = (p + Z95**2 / (2 * n)) / (1 + Z95**2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95**2 / (4 * n * n)) / (1 + Z95**2 / n)
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def mcnemar_p(a: list[bool], b: list[bool]) -> float:
    """Exact two-sided McNemar test on paired outcomes: is A right more often than B by more than chance?"""
    only_a = sum(x and not y for x, y in zip(a, b))
    only_b = sum(y and not x for x, y in zip(a, b))
    n = only_a + only_b
    if not n:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(only_a, only_b) + 1)) / 2**n
    return min(1.0, 2 * tail)


def _share(flags: list[bool]) -> float | None:
    return round(sum(flags) / len(flags), 3) if flags else None


def summarize(records: list[dict]) -> dict:
    """Headline numbers. AC@1/AC@3: the true cause ranked first / in the top three (and, for the
    agent, accepted by the verifier)."""
    if not records:
        return {}
    truths = [r["truth"] for r in records]
    answered = [t for t in truths if t["answered"]]
    costs = [c for c in (r["cost_usd"] if "cost_usd" in r else cost_usd(r["usage"], r.get("model", OPUS))
                         for r in records) if c is not None]
    agent_hits = [t["ai_top1"] for t in truths]
    baseline_hits = [t["baseline_top1"] for t in truths]
    return {
        "outages": len(records),
        "agent_ac1": _share(agent_hits),
        "agent_ac1_ci": wilson(sum(agent_hits), len(agent_hits)),
        "baseline_ac1_ci": wilson(sum(baseline_hits), len(baseline_hits)),
        "only_agent_right": sum(a and not b for a, b in zip(agent_hits, baseline_hits)),
        "only_baseline_right": sum(b and not a for a, b in zip(agent_hits, baseline_hits)),
        "mcnemar_p": mcnemar_p(agent_hits, baseline_hits),
        "agent_ac3": _share([t["ai_top3"] for t in truths]),
        "baseline_ac1": _share(baseline_hits),
        "baseline_ac3": _share([t["baseline_top3"] for t in truths]),
        "answered": len(answered),
        "right_when_answered": _share([t["ai_answer"] == t["root"] for t in answered]),
        "failed_runs": sum(1 for r in records if r["errors"]),
        "escalated_to_strong": sum(1 for r in records if r.get("escalated")),
        "median_cost_usd": round(statistics.median(costs), 4) if costs else None,
        "by_fault": {
            f: {
                "outages": len(group),
                "agent_ac1": _share([t["ai_top1"] for t in group]),
                "baseline_ac1": _share([t["baseline_top1"] for t in group]),
            }
            for f in sim.FAULTS
            if (group := [t for t in truths if t["fault"] == f])
        },
    }


def auto_records(cheap_variant: str = "haiku", strong_variant: str = "agent") -> list[dict]:
    """The auto route on the benchmark, from the recorded Haiku and Opus runs on the same outages: Haiku's
    verdict when it stands (verified, confident, and no other service explains more of the outage),
    otherwise Opus's, paying for both. The live route applies exactly this rule."""
    strong = {r["id"]: r for r in load(runs_path(strong_variant))}
    out = []
    for cheap in load(runs_path(cheap_variant)):
        backup = strong.get(cheap["id"])
        if backup is None:
            continue
        inc = sim.simulate(cheap["root"], cheap["fault"], cheap["seed"], cheap.get("hard", False))
        verdict = next(e for e in cheap["events"] if e["type"] == "verdict")
        escalated = investigate.handoff_reason(inc, verdict) is not None
        cost = (cost_usd(cheap["usage"], HAIKU) or 0.0) + ((cost_usd(backup["usage"], OPUS) or 0.0) if escalated else 0.0)
        out.append({**(backup if escalated else cheap), "cost_usd": cost, "escalated": escalated})
    return out


def deep_stats(records: list[dict]) -> dict:
    """Checks that need each outage re-simulated (deterministic from its seed): a second no-AI control, and
    whether each answer explains every abnormal service through the call graph."""
    incidents = [sim.simulate(r["root"], r["fault"], r["seed"], r.get("hard", False)) for r in records]
    robust = [sim.robust_ranking(inc) for inc in incidents]
    answers = [(inc, r["truth"]["ai_answer"]) for inc, r in zip(incidents, records) if r["truth"]["ai_answer"]]
    explains_all = [not investigate.blast_radius(inc, a)["unexplained"] for inc, a in answers]
    baseline_wrong = [(inc, rk[0]) for inc, rk, r in zip(incidents, map(sim.zscore_ranking, incidents), records)
                      if rk[0] != r["root"]]
    return {
        "baseline_wrong": len(baseline_wrong),
        "baseline_wrong_caught": sum(bool(investigate.blast_radius(inc, a)["unexplained"]) for inc, a in baseline_wrong),
        "robust_ac1": _share([rk[0] == r["root"] for rk, r in zip(robust, records)]),
        "robust_ac3": _share([r["root"] in rk[:3] for rk, r in zip(robust, records)]),
        "robust_ac1_ci": wilson(sum(rk[0] == r["root"] for rk, r in zip(robust, records)), len(records)),
        "answers_explain_all": sum(explains_all),
        "answers_checked": len(explains_all),
    }


@lru_cache(maxsize=None)
def rollback_proves(root: str, fault: str, seed: int, hard: bool, answer: str) -> bool:
    # cached: every variant ran the same outages, and most answers name the same service
    return investigate.rollback_check(sim.simulate(root, fault, seed, hard), answer)["proves"]


def rollback_stats(records: list[dict]) -> dict:
    """The rollback check as an offline what-if on the recorded answers (re-simulated, no API calls): how many
    answers it confirms, the wrong ones it would have caught, the right ones it would have wrongly rejected,
    and how often the true cause goes out once answers that fail it become escalations."""
    answered = [(r, r["truth"]["ai_answer"]) for r in records if r["truth"]["ai_answer"]]
    if not answered:
        return {}
    passed = [rollback_proves(r["root"], r["fault"], r["seed"], r.get("hard", False), a) for r, a in answered]
    right = [a == r["root"] for r, a in answered]
    return {
        "rollback_checked": len(answered),
        "rollback_pass": _share(passed),
        "rollback_caught": sum(not p and not ok for p, ok in zip(passed, right)),
        "rollback_wrongly_rejected": sum(not p and ok for p, ok in zip(passed, right)),
        "rollback_gated_cause_first": round(sum(p and ok for p, ok in zip(passed, right)) / len(records), 3),
        "rollback_gated_wrong": sum(p and not ok for p, ok in zip(passed, right)),
    }


def _verified_top(record: dict) -> dict | None:
    """Suspect #1 of the recorded verdict if the verifier accepted it, else None. Only the first counts, as in
    build_verdict: a lower suspect that verified is never promoted, and a failed run has no suspects at all."""
    verdict = next((e for e in record["events"] if e["type"] == "verdict"), {})
    top = (verdict.get("causes") or [None])[0]
    return top if top and top["verified"] else None


def gate_stats(records: list[dict]) -> dict:
    """Rollback as the answer gate, an offline what-if on the recorded verdicts (re-simulated, no API calls):
    answer with the verified top suspect whenever rolling it back proves it, whatever its confidence, else
    escalate; and both gates together, answering when either the 80% confidence gate or the rollback lets it out.
    Right means the answer is the true cause."""
    if not records:
        return {}
    rows = []  # (answered by the confidence gate, rollback proves the verified top suspect, that suspect is right)
    for r in records:
        top = _verified_top(r)
        proves = top is not None and rollback_proves(r["root"], r["fault"], r["seed"], r.get("hard", False),
                                                     top["service"])
        rows.append((bool(r["truth"]["ai_answer"]), proves, top is not None and top["service"] == r["root"]))
    return {
        "rollback_gate_answered": sum(p for _, p, _ in rows),
        "rollback_gate_right": sum(p and ok for _, p, ok in rows),
        "confidence_or_rollback_answered": sum(a or p for a, p, _ in rows),
        "confidence_or_rollback_right": sum((a or p) and ok for a, p, ok in rows),
    }


@lru_cache(maxsize=None)
def _graph_ranking(root: str, fault: str, seed: int, hard: bool) -> tuple[str, ...]:
    # cached: every variant ran the same outages
    return tuple(investigate.graph_ranking(sim.simulate(root, fault, seed, hard)))


def graph_stats(records: list[dict]) -> dict:
    """The strongest no-AI control, the call-graph rule, on the same outages (re-simulated, no API calls), and the
    method against it outage by outage. The rule always answers, so every outage it misses is a wrong answer."""
    if not records:
        return {}
    ranks = [_graph_ranking(r["root"], r["fault"], r["seed"], r.get("hard", False)) for r in records]
    graph_hits = [rk[0] == r["root"] for rk, r in zip(ranks, records)]
    agent_hits = [r["truth"]["ai_top1"] for r in records]
    return {
        "graph_ac1": _share(graph_hits),
        "graph_ac1_ci": wilson(sum(graph_hits), len(graph_hits)),
        "graph_ac3": _share([r["root"] in rk[:3] for rk, r in zip(ranks, records)]),
        "graph_wrong": len(graph_hits) - sum(graph_hits),
        "only_agent_right_vs_graph": sum(a and not g for a, g in zip(agent_hits, graph_hits)),
        "only_graph_right": sum(g and not a for a, g in zip(agent_hits, graph_hits)),
        "graph_mcnemar_p": mcnemar_p(agent_hits, graph_hits),
    }


EVIDENCE = RUNS.with_name("evidence.json")


def evidence() -> dict:
    """Everything the console's Evidence view shows: every variant, the auto routes, the deep checks and the
    rollback what-ifs. The gate what-if covers only single-model runs: auto's handoff to Opus itself depends on
    the confidence gate, so swapping the gate would change which runs it is built from."""
    records = {v: load(runs_path(v)) for v in VARIANTS}
    records.update({f"{p}auto": auto_records(f"{p}haiku", f"{p}agent") for p in ("", "hard-")})
    out = {v: {"label": VARIANT_LABELS[v], **summarize(rs), **rollback_stats(rs),
               **(gate_stats(rs) if v in VARIANTS else {}), **graph_stats(rs)} for v, rs in records.items()}
    for prefix in ("", "hard-"):
        if out[f"{prefix}agent"]:
            out[f"{prefix}agent"] = {**out[f"{prefix}agent"], **deep_stats(records[f"{prefix}agent"])}
    return out


def evidence_cached() -> dict:
    """The Evidence tables from runs/evidence.json while it is newer than every run file (re-simulating
    176 outages takes seconds, too slow for a cold start); otherwise computed afresh."""
    newest = max((runs_path(v).stat().st_mtime for v in VARIANTS if runs_path(v).exists()), default=0.0)
    if EVIDENCE.exists() and EVIDENCE.stat().st_mtime >= newest:
        return json.loads(EVIDENCE.read_text())
    return evidence()


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def ladder() -> str:
    """One row per capability, on the same outages: what each one adds."""
    rows = []
    agent = summarize(load(runs_path("agent")))
    if agent:
        rows.append(f"| No-AI control: rank services by z-score | {_pct(agent['baseline_ac1'])} | "
                    f"{_pct(agent['baseline_ac3'])} | - | - |")
    for variant in ("one-shot", "haiku", "sonnet", "agent", "no-logs"):
        s = summarize(load(runs_path(variant)))
        if s:
            rows.append(f"| {VARIANT_LABELS[variant]} | {_pct(s['agent_ac1'])} | {_pct(s['agent_ac3'])} | "
                        f"{s['answered']} of {s['outages']} | ${s['median_cost_usd']} |")
    if not rows:
        return ""
    header = "| Method | AC@1 | AC@3 | Answered without a human | Median cost |"
    return "\n".join([header, "|---|---:|---:|---:|---:|", *rows])


def rollback_table(ev: dict) -> str:
    """The rollback what-if, one row per method that answered at least once."""
    rows = [
        f"| {v} | {s['rollback_checked']} | {_pct(s['rollback_pass'])} | {s['rollback_caught']} | "
        f"{s['rollback_wrongly_rejected']} | {s['rollback_gated_wrong']} | {_pct(s['rollback_gated_cause_first'])} |"
        for v, s in ev.items() if s.get("rollback_checked")
    ]
    if not rows:
        return ""
    header = ("| Rollback what-if | Answers | Pass | Wrong caught | Right rejected | Wrong left after gate | "
              "Gated cause first |")
    return "\n".join([header, "|---|---:|---:|---:|---:|---:|---:|", *rows])


def gate_table(ev: dict) -> str:
    """The answer gate what-if: answered and right under today's confidence gate, the rollback, and either."""
    rows = [
        f"| {v} | {s['outages']} | {s['answered']} / {round((s['right_when_answered'] or 0) * s['answered'])} | "
        f"{s['rollback_gate_answered']} / {s['rollback_gate_right']} | "
        f"{s['confidence_or_rollback_answered']} / {s['confidence_or_rollback_right']} |"
        for v, s in ev.items() if "rollback_gate_answered" in s
    ]
    if not rows:
        return ""
    header = "| Gate what-if | Outages | 80% confidence: answered / right | Rollback | Either |"
    return "\n".join([header, "|---|---:|---:|---:|---:|", *rows])


def graph_table(ev: dict) -> str:
    """Each method against the no-AI call-graph rule, on the same outages, paired outage by outage."""
    rows = [
        f"| {v} | {s['outages']} | {_pct(s['graph_ac1'])} | {_pct(s['graph_ac3'])} | {s['graph_wrong']} | "
        f"{_pct(s['agent_ac1'])} | {s['only_agent_right_vs_graph']} | {s['only_graph_right']} | "
        f"{s['graph_mcnemar_p']:.2g} |"
        for v, s in ev.items() if "graph_ac1" in s
    ]
    if not rows:
        return ""
    header = ("| Call-graph rule vs | Outages | Rule AC@1 | Rule AC@3 | Rule wrong | Method AC@1 | Only method right | "
              "Only rule right | McNemar p |")
    return "\n".join([header, "|---|---:|---:|---:|---:|---:|---:|---:|---:|", *rows])


def report(summary: dict) -> str:
    if not summary:
        return "No benchmark runs yet: run `python bench.py --n 44` first."
    pct = _pct
    lines = [
        f"{summary['outages']} outages",
        "",
        "| Method | AC@1 | AC@3 |",
        "|---|---:|---:|",
        f"| No-AI control: rank services by z-score | {pct(summary['baseline_ac1'])} | {pct(summary['baseline_ac3'])} |",
        f"| Sherlog agent (verified) | {pct(summary['agent_ac1'])} | {pct(summary['agent_ac3'])} |",
        "",
        "| Fault | Outages | No-AI AC@1 | Agent AC@1 |",
        "|---|---:|---:|---:|",
        *(f"| {f} | {g['outages']} | {pct(g['baseline_ac1'])} | {pct(g['agent_ac1'])} |"
          for f, g in summary["by_fault"].items()),
        "",
        f"Answered (verified and confident): {summary['answered']} of {summary['outages']}, "
        f"right {pct(summary['right_when_answered'])} of the time when it answered. "
        f"Failed runs: {summary['failed_runs']}. Median cost per outage: ${summary['median_cost_usd']}.",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=len(sim.SERVICES) * len(sim.FAULTS), help="outages to run")
    parser.add_argument("--workers", type=int, default=4, help="outages investigated in parallel")
    parser.add_argument("--variant", choices=VARIANTS, default="agent", help="full agent or an ablation")
    parser.add_argument("--report", action="store_true", help="only print the tables")
    args = parser.parse_args()
    path = runs_path(args.variant)

    if not args.report:
        done = {r["id"] for r in load(path)}
        seed_base = HARD_SEED_BASE if VARIANTS[args.variant][2] else SEED_BASE
        todo = [c for c in cases(args.n, seed_base) if f"{c[0]}-{c[1]}-{c[2]}" not in done]
        path.parent.mkdir(exist_ok=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool, path.open("a") as out:
            futures = [pool.submit(run_case, *c, *VARIANTS[args.variant]) for c in todo]
            for i, future in enumerate(as_completed(futures), 1):
                record = future.result()
                out.write(json.dumps(record) + "\n")
                out.flush()
                t = record["truth"]
                print(f"[{i}/{len(todo)}] {record['id']}: agent {'✓' if t['ai_top1'] else '✗'} "
                      f"no-AI {'✓' if t['baseline_top1'] else '✗'} {record['errors'] or ''}", flush=True)
    print(report(summarize(load(path))))
    print()
    print(ladder())
    ev = evidence()
    print()
    print(rollback_table(ev))
    print()
    print(gate_table(ev))
    print()
    print(graph_table(ev))
    EVIDENCE.write_text(json.dumps(ev))


if __name__ == "__main__":
    main()
