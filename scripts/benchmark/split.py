"""Freeze the dev/test split before any live run: runs/real-benchmark/split.json.

    python scripts/benchmark/split.py

Dev: for each system and each of its 6 faults, one case from repetition 1, the services taken in turn so all 5
appear (18 of 270 cases, 6.7%). Dev cases are the only ones the converter and the prompt wording may be adapted
on. Test: the other 252, reported on their own. The rule reads only the labels file prepare.py writes.
"""
from __future__ import annotations

import json
from pathlib import Path

RUNS = Path(__file__).resolve().parents[2] / "runs" / "real-benchmark"
RULE = ("dev: per system, for the i-th fault in sorted order, the repetition-1 case of the (i mod 5)-th root-cause "
        "service in sorted order; test: every other case")


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


def main() -> None:
    answers = [json.loads(line) for line in (RUNS / "answers.jsonl").read_text().splitlines()]
    out = split(answers)
    assert len(out["dev"]) == 18 and len(out["dev"]) + len(out["test"]) == len(answers)
    (RUNS / "split.json").write_text(json.dumps(out, indent=1) + "\n")
    print(f"dev {len(out['dev'])}, test {len(out['test'])}")


if __name__ == "__main__":
    main()
