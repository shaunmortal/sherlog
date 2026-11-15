"""Summarize the real-system experiment from its records, with no servers and no API calls.

    uv run --with-requirements requirements.txt python -m testbed.report [--out runs/real]

Cause first: the method's first pick is the true culprit (for the agent, also accepted by the verifier, as in
the benchmark's AC@1). Every outage counts, including those where the alarm fired before the fault or not at
all. A rollback "proves" an answer when, in the 2 minutes after rolling that service back, the alarm stops.
"""
from __future__ import annotations

import argparse
import statistics
from pathlib import Path

import bench
from testbed.run import OUT

METHODS = {
    "zscore": "No-AI: z-score ranking",
    "robust": "No-AI: robust z-score ranking",
    "graph": "No-AI: call-graph rule",
    "auto": "Sherlog auto: Haiku first, Opus if its answer does not stand",
    "opus": "Sherlog agent on Opus",
}
AGENTS = ("auto", "opus")
ROLLED_BACK = ("zscore", "graph", "auto", "opus")  # the robust ranking's pick is not rolled back on its own


def _verdict(record: dict, route: str) -> dict | None:
    events = record["runs"].get(route, {}).get("events", [])
    return next((e for e in reversed(events) if e["type"] == "verdict"), None)


def _answer(record: dict, method: str) -> str | None:
    """What the method would tell the on-call engineer: the no-AI rules always name their first pick; the
    agent names one only when its top suspect is verified and confident."""
    if method not in AGENTS:
        return record["tops"][method]
    verdict = _verdict(record, method)
    return verdict["answer"] if verdict else None


def cause_first(record: dict, method: str) -> bool:
    root = record["truth"]["service"]
    if method not in AGENTS:
        return record["tops"][method] == root
    causes = (_verdict(record, method) or {}).get("causes") or []
    return bool(causes) and causes[0]["service"] == root and causes[0]["verified"]


def _pct(hits: int, n: int) -> str:
    if not n:
        return "n/a"
    lo, hi = bench.wilson(hits, n)
    return f"{hits}/{n} = {hits / n:.0%} [{lo:.0%}, {hi:.0%}]"


def _rollback(record: dict, service: str | None) -> bool | None:
    test = record["rollbacks"].get(service) if service else None
    return None if test is None else test["proves"]


def accuracy_table(groups: dict[str, list[dict]]) -> str:
    head = "| Method | " + " | ".join(groups) + " |"
    rows = [head, "|---|" + "---:|" * len(groups)]
    for method, label in METHODS.items():
        cells = []
        for records in groups.values():
            ran = [r for r in records if method not in AGENTS or method in r["runs"]]
            cells.append(_pct(sum(cause_first(r, method) for r in ran), len(ran)))
        rows.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def agent_table(records: list[dict]) -> str:
    rows = ["| Agent | Ran | Answered | Right | Wrong | Escalated | Failed runs | Median cost | Mean cost | Total |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for route in AGENTS:
        ran = [r for r in records if route in r["runs"]]
        answered = [r for r in ran if _answer(r, route)]
        right = sum(_answer(r, route) == r["truth"]["service"] for r in answered)
        costs = [r["runs"][route]["cost_usd"] for r in ran if r["runs"][route].get("cost_usd") is not None]
        failed = sum(bool(r["runs"][route].get("errors")) for r in ran)
        money = (f"${statistics.median(costs):.4f} | ${statistics.fmean(costs):.4f} | ${sum(costs):.2f}"
                 if costs else "n/a | n/a | n/a")
        rows.append(f"| {METHODS[route]} | {len(ran)} | {len(answered)} | {right} | {len(answered) - right} | "
                    f"{len(ran) - len(answered)} | {failed} | {money} |")
    return "\n".join(rows)


def rollback_table(records: list[dict]) -> str:
    """Each method's answer rolled back for real: passed, wrong ones caught, right ones rejected, wrong ones
    that passed; and for the agent's escalations, whether rolling back its top suspect proves the cause."""
    rows = ["| Method | Answers tested | Proved | Wrong caught | Right rejected | Wrong passed | "
            "Escalations: top suspect tested / proved / was the cause |",
            "|---|---:|---:|---:|---:|---:|---:|"]
    for method in ROLLED_BACK:
        tested = [(r, _answer(r, method)) for r in records if _rollback(r, _answer(r, method)) is not None]
        proved = [(r, a) for r, a in tested if _rollback(r, a)]
        wrong = [(r, a) for r, a in tested if a != r["truth"]["service"]]
        caught = sum(not _rollback(r, a) for r, a in wrong)
        rejected = sum(not _rollback(r, a) for r, a in tested if a == r["truth"]["service"])
        escalations = "-"
        if method in AGENTS:
            esc = [(r, (_verdict(r, method)["causes"] or [{}])[0].get("service")) for r in records
                   if method in r["runs"] and _verdict(r, method) and not _answer(r, method)]
            esc = [(r, s) for r, s in esc if _rollback(r, s) is not None]
            esc_proved = [(r, s) for r, s in esc if _rollback(r, s)]
            escalations = (f"{len(esc)} / {len(esc_proved)} / "
                           f"{sum(s == r['truth']['service'] for r, s in esc_proved)}")
        rows.append(f"| {METHODS[method]} | {len(tested)} | {len(proved)} | {caught} | {rejected} | "
                    f"{len(wrong) - caught} | {escalations} |")
    return "\n".join(rows)


def rollback_facts(records: list[dict]) -> str:
    cause = [r["rollbacks"][r["truth"]["service"]]["proves"] for r in records
             if r["truth"]["service"] in r["rollbacks"]]
    other = [t["proves"] for r in records for s, t in r["rollbacks"].items() if s != r["truth"]["service"]]
    return (f"Rolling back the true culprit stopped the alarm in {sum(cause)} of {len(cause)} tests; rolling back "
            f"another service counted as proof in {sum(other)} of {len(other)} tests.")


def alarm_facts(records: list[dict]) -> str:
    early = [r["id"] for r in records if r["alarm_before_onset"]]
    silent = [r["id"] for r in records if r["alarm"] is None]
    delays = [r["alarm"] - r["truth"]["onset"] for r in records if r["alarm"] is not None and not r["alarm_before_onset"]]
    delay = f", median {statistics.median(delays) * 5:g} s after the fault began" if delays else ""
    return (f"Alarm fired after the fault began in {len(delays)} of {len(records)} outages{delay}; before it "
            f"(a false alarm) in {len(early)} {early or ''}; never in {len(silent)} {silent or ''}.")


def wrong_answers(records: list[dict]) -> str:
    lines = []
    for r in records:
        t = r["truth"]
        for method in ("graph", *AGENTS):
            answer = _answer(r, method)
            if answer and answer != t["service"]:
                proof = {True: "rollback proves it", False: "rollback caught it", None: "not rolled back"}
                decoy = f", decoy {t['decoy']} ({t['decoy_kind']})" if t["decoy"] else ""
                lines.append(f"- {r['id']}: {METHODS[method]} answered {answer}; the culprit was {t['service']} "
                             f"({t['fault']}){decoy}; {proof[_rollback(r, answer)]}")
    return "\n".join(lines) or "- none"


def report(records: list[dict], out: Path = OUT) -> str:
    if not records:
        return "No real-system outages recorded yet: run `python -m testbed.run` first."
    normal = [r for r in records if not r["hard"]]
    hard = [r for r in records if r["hard"]]
    groups = {f"Normal ({len(normal)})": normal, f"Hard ({len(hard)})": hard, f"All ({len(records)})": records}
    spent = sum(e["charged_usd"] for e in bench.load(out / "spend.jsonl"))
    sections = [f"{len(records)} real outages ({len(normal)} normal, {len(hard)} hard)", "",
                alarm_facts(records), "", "Cause first, with 95% Wilson intervals:", "", accuracy_table(groups)]
    for name, group in (("Normal", normal), ("Hard", hard)):
        if group:
            sections += ["", f"{name}: the agent's verdicts", "", agent_table(group),
                         "", f"{name}: real rollbacks", "", rollback_table(group), "", rollback_facts(group)]
    sections += ["", "Wrong answers (the call-graph rule always answers; the agent only when confident):", "",
                 wrong_answers(records), "", f"Live spend recorded in spend.jsonl: ${spent:.2f}"]
    return "\n".join(sections)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    print(report(bench.load(args.out / "outages.jsonl"), args.out))


if __name__ == "__main__":
    main()
