"""Recompute runs/real-benchmark/results.json from the run records: no API calls, no telemetry, standard library.

    python scripts/benchmark/report.py            # writes results.json and prints the tables

Scoring is the benchmark's: a ranking is cut to services (a `<service>_<metric>` entry counts as its service, a
`-db` container as the service it belongs to, first occurrence kept), AC@k says the root-cause service is in the
first k, Avg@5 is the mean of AC@1..AC@5. score.py runs the benchmark's own evaluator on the same records and
writes official.json; this script refuses to finish if its numbers differ from those. Test cases only, unless a
table says dev.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bench import mcnemar_p, wilson  # noqa: E402  (stdlib-only helpers shared with the shop's benchmark)

RUNS = ROOT / "runs" / "real-benchmark"
SYSTEMS = {"re2-ob": "A", "re2-ss": "B", "re2-tt": "C"}  # docs name the systems by letter and size
LABELS = {
    "zscore": "Sherlog no-AI: z-score ranking",
    "robust": "Sherlog no-AI: robust (median/MAD) z-score ranking",
    "graph": "Sherlog no-AI: call-graph rule",
    "auto": "Sherlog agent, auto route (Claude Haiku 5.5, Claude Opus 5.5 when needed)",
    "opus": "Sherlog agent, Claude Opus 5.5",
    "auto-submitted": "Sherlog agent, auto route, every submitted suspect (verified or not)",
    "baro": "Published baseline: BARO (metrics)",
    "nsigma": "Published baseline: n-sigma (metrics)",
    "mmbaro": "Published baseline: multi-source BARO (metrics, logs, traces)",
    "mmnsigma": "Published baseline: multi-source n-sigma (metrics, logs, traces)",
    "circa": "Published baseline: CIRCA (metrics)",
    "e_diagnosis": "Published baseline: epsilon-Diagnosis (metrics)",
}
LIVE = ("auto", "opus")
KS = (1, 3, 5)


def to_service(name: str) -> str:
    return name.split("_")[0].replace("-db", "")


def service_ranks(ranks: list[str]) -> list[str]:
    out: list[str] = []
    for r in ranks:
        s = to_service(r.strip().lower())
        if s not in out:
            out.append(s)
    return out


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def records(method: str, cases: set[str], subset: str) -> dict[str, dict]:
    """case id -> record, for one method on one set."""
    if method == "auto-submitted":
        return {r["case_id"]: {**r, "ranks": r["submitted"]} for r in records("auto", cases, subset).values()}
    path = RUNS / subset / f"{method}.jsonl"
    rows = load(path) if path.exists() else load(RUNS / "baselines" / f"{method}.jsonl")
    return {r["case_id"]: r for r in rows if r["case_id"] in cases}


def scores(cases: list[str], recs: dict[str, dict], answers: dict[str, dict]) -> dict:
    """AC@k and Avg@5 over the cases that have a record (a live run stopped by the budget has none)."""
    run = [c for c in cases if c in recs]
    if not run:
        return {"n": 0}
    ranks = {c: service_ranks(recs[c]["ranks"]) for c in run}
    hit = {k: [answers[c]["service"] in ranks[c][:k] for c in run] for k in range(1, 6)}
    ac = {k: sum(hit[k]) / len(run) for k in range(1, 6)}
    return {"n": len(run), "of": len(cases), **{f"AC@{k}": round(ac[k], 4) for k in KS},
            "Avg@5": round(sum(ac.values()) / 5, 4), "AC@1_ci95": wilson(sum(hit[1]), len(run))}


def agent_stats(recs: dict[str, dict], answers: dict[str, dict]) -> dict:
    rows = list(recs.values())
    if not rows:
        return {}
    answered = [r for r in rows if r["answer"]]
    wrong = [r for r in answered if to_service(r["answer"]) != answers[r["case_id"]]["service"]]
    suspects = [c for r in rows for c in r["causes"]]
    costs = sorted(r["cost_usd"] for r in rows)
    return {
        "cases": len(rows),
        "no_alarm": sum(r["alarm"] is None for r in rows),
        "answered": len(answered),
        "handed_to_human": len(rows) - len(answered),
        "wrong_when_answered": len(wrong),
        "right_when_answered": round(1 - len(wrong) / len(answered), 4) if answered else None,
        "suspects_submitted": len(suspects),
        "citation_check_pass": round(sum(c["verified"] for c in suspects) / len(suspects), 4) if suspects else None,
        "top_suspect_verified": round(sum(bool(r["causes"]) and r["causes"][0]["verified"] for r in rows) / len(rows), 4),
        "handed_to_opus": sum(len(r["route"]) == 2 for r in rows),
        "blast_better_than_answer": sum(bool(r["blast"] and r["blast"]["better"]) for r in answered),
        "blast_unexplained_left": sum(bool(r["blast"] and r["blast"]["unexplained"]) for r in answered),
        "failed_runs": sum(bool(r["errors"]) for r in rows),
        "cost_usd_total": round(sum(costs), 4),
        "cost_usd_mean": round(sum(costs) / len(costs), 5),
        "cost_usd_median": round(costs[len(costs) // 2], 5),
        "wall_s_mean": round(sum(r["wall_s"] for r in rows) / len(rows), 1),
    }


def paired(a: dict[str, dict], b: dict[str, dict], answers: dict[str, dict]) -> dict:
    """AC@1 of two methods on the cases both ran, and the exact McNemar test."""
    common = sorted(set(a) & set(b))
    top = lambda recs, c: (service_ranks(recs[c]["ranks"]) or [None])[0] == answers[c]["service"]  # noqa: E731
    x, y = [top(a, c) for c in common], [top(b, c) for c in common]
    return {"n": len(common), "only_first_right": sum(p and not q for p, q in zip(x, y)),
            "only_second_right": sum(q and not p for p, q in zip(x, y)), "mcnemar_p": round(mcnemar_p(x, y), 6)}


def build() -> dict:
    answers = {a["case_id"]: a for a in load(RUNS / "answers.jsonl")}
    split = json.loads((RUNS / "split.json").read_text())
    methods = [m for m in LABELS if records(m, set(answers), "test")]
    out = {"split": {k: len(split[k]) for k in ("dev", "test")}, "methods": {}, "paired": {}, "dev": {}}
    groups = {"all": split["test"], **{f"system {s}": [c for c in split["test"] if answers[c]["dataset"] == d]
                                      for d, s in SYSTEMS.items()}}
    faults = sorted({a["fault"] for a in answers.values()})
    for m in methods:
        recs = records(m, set(split["test"]), "test")
        out["methods"][m] = {
            "label": LABELS[m],
            **{g: scores(cases, recs, answers) for g, cases in groups.items()},
            "by_fault": {f: scores([c for c in split["test"] if answers[c]["fault"] == f], recs, answers)
                         for f in faults},
            **({"sherlog": agent_stats(recs, answers)} if m in LIVE else {}),
        }
    for agent in (m for m in LIVE if m in methods):
        a = records(agent, set(split["test"]), "test")
        out["paired"][agent] = {m: paired(a, records(m, set(split["test"]), "test"), answers)
                                for m in methods if m not in (agent, "auto-submitted")}
    for m in (*LIVE, "zscore", "graph"):
        recs = records(m, set(split["dev"]), "dev")
        if recs:
            out["dev"][m] = {"all": scores(split["dev"], recs, answers),
                             **({"sherlog": agent_stats(recs, answers)} if m in LIVE else {})}
    spend = load(RUNS / "spend.jsonl")
    out["metered_spend_usd"] = round(sum(s["usd"] for s in spend), 4)
    return out


def check_official(results: dict) -> None:
    """Every number the benchmark's own evaluator produced (score.py) must match ours."""
    path = RUNS / "official.json"
    if not path.exists():
        print("note: official.json missing; run score.py in the benchmark's environment to cross-check")
        return
    official = json.loads(path.read_text())
    for m, groups in official.items():
        for g, theirs in groups.items():
            ours = results["methods"][m][g]
            for key in ("AC@1", "AC@3", "AC@5", "Avg@5"):
                assert abs(ours[key] - theirs[key]) < 1e-3, f"{m} {g} {key}: ours {ours[key]}, evaluator {theirs[key]}"
            assert ours["n"] == theirs["n"], f"{m} {g}: {ours['n']} cases, evaluator {theirs['n']}"
    results["official_match"] = sorted(official)


def _pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def table(results: dict) -> str:
    rows = ["| Method | Cases | AC@1 (95% CI) | AC@3 | Avg@5 | A: AC@1 | B: AC@1 | C: AC@1 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for m, r in results["methods"].items():
        a = r["all"]
        ci = a["AC@1_ci95"]
        rows.append(f"| {r['label']} | {a['n']} | {_pct(a['AC@1'])} ({_pct(ci[0])}-{_pct(ci[1])}) | {_pct(a['AC@3'])} | "
                    f"{a['Avg@5']:.2f} | " + " | ".join(_pct(r[f"system {s}"].get("AC@1")) for s in "ABC") + " |")
    return "\n".join(rows)


def main() -> None:
    results = build()
    check_official(results)
    (RUNS / "results.json").write_text(json.dumps(results, indent=1) + "\n")
    print(table(results))
    for agent, vs in results["paired"].items():
        for m, p in vs.items():
            print(f"{agent} vs {m}: {p['n']} paired cases, only {agent} right {p['only_first_right']}, "
                  f"only {m} right {p['only_second_right']}, McNemar p={p['mcnemar_p']:.3g}")
    for agent in LIVE:
        if agent in results["methods"]:
            print(agent, json.dumps(results["methods"][agent]["sherlog"]))


if __name__ == "__main__":
    main()
