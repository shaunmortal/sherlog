"""Freeze the dev/test split before any live run: runs/real-benchmark/split.json (re3/split.json for RE3).

    python scripts/benchmark/split.py [SUITE]        # re2 (default) or re3

RE2 dev: for each system and each of its 6 faults, one case from repetition 1, the services taken in turn so all 5
appear (18 of 270 cases, 6.7%). RE3's grid is uneven (5 code-level faults on system A, 4 on B and C; 2 to 4
root-cause services; 3 to 10 cases per fault), so one case per system and fault would be 13 cases (14%). RE3 dev:
per system, three faults consecutive in sorted order, wrapping, so that all five fault types are in dev; for each,
the repetition-1 case of the system's services with that fault taken in turn (9 of 90 cases, 10%). Dev cases are
the only ones the converter and the prompt wording may be adapted on. Test: every other case, reported on its own.
The rule reads only the labels file prepare.py writes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RUNS = Path(__file__).resolve().parents[2] / "runs" / "real-benchmark"
RULE = ("dev: per system, for the i-th fault in sorted order, the repetition-1 case of the (i mod 5)-th root-cause "
        "service in sorted order; test: every other case")
RULE_RE3 = ("dev: for system k (A=0, B=1, C=2), the faults at sorted positions k+2, k+3 and k+4 modulo its number of "
            "faults; for the i-th of them, the repetition-1 case of the (i mod n)-th of the n services with that "
            "fault, in sorted order; test: every other case")
DEV_FAULTS_PER_SYSTEM = 3
DEV_CASES = {"re2": 18, "re3": 9}


def split(answers: list[dict]) -> dict:
    dev = []
    for dataset in sorted({a["dataset"] for a in answers}):
        cases = [a for a in answers if a["dataset"] == dataset]
        services = sorted({a["service"] for a in cases})
        for i, fault in enumerate(sorted({a["fault"] for a in cases})):
            service = services[i % len(services)]
            dev += [a["case_id"] for a in cases
                    if (a["service"], a["fault"], a["repetition"]) == (service, fault, 1)]
    test = sorted(a["case_id"] for a in answers if a["case_id"] not in set(dev))
    return {"rule": RULE, "dev": sorted(dev), "test": test}


def split_re3(answers: list[dict]) -> dict:
    dev = []
    for k, dataset in enumerate(sorted({a["dataset"] for a in answers})):
        cases = [a for a in answers if a["dataset"] == dataset]
        faults = sorted({a["fault"] for a in cases})
        for i in range(DEV_FAULTS_PER_SYSTEM):
            fault = faults[(k + 2 + i) % len(faults)]
            services = sorted({a["service"] for a in cases if a["fault"] == fault})
            service = services[i % len(services)]
            dev += [a["case_id"] for a in cases if (a["service"], a["fault"], a["repetition"]) == (service, fault, 1)]
    test = sorted(a["case_id"] for a in answers if a["case_id"] not in set(dev))
    return {"rule": RULE_RE3, "dev": sorted(dev), "test": test}


def main() -> None:
    suite = sys.argv[1] if len(sys.argv) > 1 else "re2"
    runs = RUNS if suite == "re2" else RUNS / suite  # as report.SUITES
    answers = [json.loads(line) for line in (runs / "answers.jsonl").read_text().splitlines()]
    out = split(answers) if suite == "re2" else split_re3(answers)
    assert len(out["dev"]) == DEV_CASES[suite] and len(out["dev"]) + len(out["test"]) == len(answers)
    (runs / "split.json").write_text(json.dumps(out, indent=1) + "\n")
    print(f"dev {len(out['dev'])}, test {len(out['test'])}")


if __name__ == "__main__":
    main()
