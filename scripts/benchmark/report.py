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
SUITES = {"re2": RUNS, "re3": RUNS / "re3"}  # each suite's records (RE2's stay where its first run put them)


def systems(suite: str) -> dict[str, str]:
    """dataset -> the letter docs name its system by: every suite records the same three systems."""
    return {f"{suite}-{x}": s for x, s in zip(("ob", "ss", "tt"), "ABC")}


SYSTEMS = systems("re2")  # docs name the systems by letter and size
LABELS = {
    "zscore": "Sherlog no-AI: z-score ranking",
    "robust": "Sherlog no-AI: robust (median/MAD) z-score ranking",
    "graph": "Sherlog no-AI: call-graph rule",
    "auto": "Sherlog agent, auto route (Claude Haiku 5.5, Claude Opus 5.5 when needed)",
    "opus": "Sherlog agent, Claude Opus 5.5",
    "auto-submitted": "Sherlog agent, auto route, every submitted suspect (verified or not)",
    "baro": "Published baseline: BARO (metrics)",
    "nsigma": "Published baseline: n-sigma (metrics)",
    "nsigma-len10": "n-sigma given 5 min on each side of the injection (window check)",
    "nsigma-len2": "n-sigma given 1 min on each side of the injection (window check)",
    "mmbaro": "Published baseline: multi-source BARO (metrics, logs, traces)",
    "mmnsigma": "Published baseline: multi-source n-sigma (metrics, logs, traces)",
    "circa": "Published baseline: CIRCA (metrics; systems A and B only, too slow on C)",
    "e_diagnosis": "Published baseline: epsilon-Diagnosis (metrics)",
}
LIVE = ("auto", "opus")
WINDOW_CHECK = ("nsigma", "nsigma-len10", "nsigma-len2")  # the runner's default is 10 min on each side
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


def records(method: str, cases: set[str], subset: str, runs: Path = RUNS) -> dict[str, dict]:
    """case id -> record, for one method on one set."""
    if method == "auto-submitted":
        return {r["case_id"]: {**r, "ranks": r["submitted"]} for r in records("auto", cases, subset, runs).values()}
    path = runs / subset / f"{method}.jsonl"
    rows = load(path) if path.exists() else load(runs / "baselines" / f"{method}.jsonl")
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
        "top_suspect_verified": round(sum(bool(r["causes"]) and r["causes"][0]["verified"] for r in rows) / len(rows),
                                      4),
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
            "only_second_right": sum(q and not p for p, q in zip(x, y)), "mcnemar_p": float(f"{mcnemar_p(x, y):.3g}")}


def build(suite: str = "re2") -> dict:
    runs = SUITES[suite]
    answers = {a["case_id"]: a for a in load(runs / "answers.jsonl")}
    split = json.loads((runs / "split.json").read_text())
    rec = lambda m, subset: records(m, set(split[subset]), subset, runs)  # noqa: E731
    methods = [m for m in LABELS if records(m, set(answers), "test", runs)]
    out = {"split": {k: len(split[k]) for k in ("dev", "test")}, "methods": {}, "paired": {}, "dev": {}}
    groups = {"all": split["test"], **{f"system {s}": [c for c in split["test"] if answers[c]["dataset"] == d]
                                      for d, s in systems(suite).items()}}
    faults = sorted({a["fault"] for a in answers.values()})
    for m in methods:
        recs = rec(m, "test")
        out["methods"][m] = {
            "label": LABELS[m],
            **{g: scores(cases, recs, answers) for g, cases in groups.items()},
            "by_fault": {f: scores([c for c in split["test"] if answers[c]["fault"] == f], recs, answers)
                         for f in faults},
            **({"sherlog": agent_stats(recs, answers)} if m in LIVE else {}),
        }
    for agent in (m for m in LIVE if m in methods):
        a = rec(agent, "test")
        out["paired"][agent] = {m: paired(a, rec(m, "test"), answers)
                                for m in methods if m not in (agent, "auto-submitted")}
    for m in (*LIVE, "zscore", "graph"):
        recs = rec(m, "dev")
        if recs:
            out["dev"][m] = {"all": scores(split["dev"], recs, answers),
                             **({"sherlog": agent_stats(recs, answers)} if m in LIVE else {})}
    out["wrong_first_suspects"] = {m: wrong_first(rec(m, "test"), answers) for m in methods}
    spend = load(runs / "spend.jsonl")
    out["metered_spend_usd"] = round(sum(s["usd"] for s in spend), 4)
    return out


def wrong_first(recs: dict[str, dict], answers: dict[str, dict]) -> dict:
    """A memorisation check: when a method's first suspect is wrong, is it one of the services this benchmark
    breaks in some other case of the same system? A method that had learnt the benchmark's answers would name
    those more often than one that only reads the telemetry."""
    roots: dict[str, set[str]] = {}
    for a in answers.values():
        roots.setdefault(a["dataset"], set()).add(a["service"])
    firsts = {c: (service_ranks(r["ranks"]) or [None])[0] for c, r in recs.items()}
    wrong = [c for c, f in firsts.items() if f != answers[c]["service"]]
    return {"wrong": len(wrong), "names_a_root_cause_of_another_case": sum(firsts[c] in roots[answers[c]["dataset"]]
                                                                         for c in wrong)}


def check_official(results: dict, runs: Path = RUNS) -> None:
    """Every number the benchmark's own evaluator produced (score.py) must match ours."""
    path = runs / "official.json"
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
    rows = ["| Method | Cases | AC@1 (95% CI) | AC@3 | Avg@5 | A: AC@1 / Avg@5 | B: AC@1 / Avg@5 | C: AC@1 / Avg@5 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for m, r in results["methods"].items():
        if m in WINDOW_CHECK[1:]:
            continue
        a, ci = r["all"], r["all"]["AC@1_ci95"]
        per = " | ".join(f"{_pct(g['AC@1'])} / {g['Avg@5']:.2f}" if g["n"] else "not run"
                         for g in (r[f"system {s}"] for s in "ABC"))
        rows.append(f"| {r['label']} | {a['n']} | {_pct(a['AC@1'])} ({_pct(ci[0])}-{_pct(ci[1])}) | "
                    f"{_pct(a['AC@3'])} | "
                    f"{a['Avg@5']:.2f} | {per} |")
    return "\n".join(rows)


def fault_table(results: dict) -> str:
    faults = list(next(iter(results["methods"].values()))["by_fault"])
    rows = ["| Method (AC@1 by fault) | " + " | ".join(faults) + " |", "|---|" + "---:|" * len(faults)]
    for m, r in results["methods"].items():
        if m not in WINDOW_CHECK[1:]:
            rows.append(f"| {r['label']} | " + " | ".join(_pct(r["by_fault"][f].get("AC@1")) for f in faults) + " |")
    return "\n".join(rows)


def paired_table(results: dict) -> str:
    rows = ["| Agent vs | Paired cases | Only the agent right | Only the other right | McNemar p |",
            "|---|---:|---:|---:|---:|"]
    for agent, vs in results["paired"].items():
        for m, p in ((m, p) for m, p in vs.items() if m not in WINDOW_CHECK[1:]):
            rows.append(f"| {agent} vs {results['methods'][m]['label']} | {p['n']} | {p['only_first_right']} | "
                        f"{p['only_second_right']} | {p['mcnemar_p']:.2g} |")
    return "\n".join(rows)


def window_table(results: dict) -> str:
    rows = ["| n-sigma window | AC@1 | Only the auto route right | Only n-sigma right | McNemar p |",
            "|---|---:|---:|---:|---:|"]
    for m in WINDOW_CHECK:
        p = results["paired"]["auto"].get(m)
        if p is None:  # the window check ran on RE2 only
            return ""
        rows.append(f"| {LABELS[m]} | {results['methods'][m]['all']['AC@1']:.1%} | {p['only_first_right']} | "
                    f"{p['only_second_right']} | {p['mcnemar_p']:.2g} |")
    return "\n".join(rows)


def prior_table(results: dict) -> str:
    rows = ["| Method | First suspect wrong | ...and a service broken in another case |", "|---|---:|---:|"]
    for m, w in results["wrong_first_suspects"].items():
        if m not in WINDOW_CHECK[1:]:
            rows.append(f"| {results['methods'][m]['label']} | {w['wrong']} | "
                        f"{w['names_a_root_cause_of_another_case']} |")
    return "\n".join(rows)


def sherlog_table(results: dict) -> str:
    keys = ["cases", "no_alarm", "answered", "handed_to_human", "wrong_when_answered", "right_when_answered",
            "citation_check_pass", "top_suspect_verified", "handed_to_opus", "blast_better_than_answer",
            "blast_unexplained_left", "failed_runs", "cost_usd_total", "cost_usd_mean", "cost_usd_median",
            "wall_s_mean"]
    agents = [m for m in LIVE if m in results["methods"]]
    rows = ["| | " + " | ".join(agents) + " |", "|---|" + "---:|" * len(agents)]
    for k in keys:
        rows.append(f"| {k} | " + " | ".join(str(results["methods"][m]["sherlog"].get(k)) for m in agents) + " |")
    return "\n".join(rows)


def main() -> None:
    results = build()
    check_official(results)
    if (SUITES["re3"] / "split.json").exists():  # the code-level suite, under its own key; RE2's keys are unchanged
        re3 = build("re3")
        if re3["methods"]:
            check_official(re3, SUITES["re3"])
            results["re3"] = re3
    (RUNS / "results.json").write_text(json.dumps(results, indent=1) + "\n")
    for suite, res in (("RE2", results), ("RE3", results.get("re3"))):
        if not res:
            continue
        print(f"## {suite}\n")
        for part in (table, fault_table, paired_table, sherlog_table, window_table, prior_table):
            print(part(res), end="\n\n")
        print(f"{suite} metered live spend, dev and test: ${res['metered_spend_usd']}; official cross-check: "
              f"{res.get('official_match', 'not run')}\n")


if __name__ == "__main__":
    main()
