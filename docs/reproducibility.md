# Reproducibility: every recorded run, re-run from its seed

Every number Sherlog publishes comes from 1,100 recorded agent runs in `runs/*.jsonl`: 748 on Claude and 352 on
two open-weights models ([open-model.md](open-model.md)). An outage is fully
determined by `(service, fault, seed, hard mode)`, so the code side of each run can be re-run today with no API
call and no key. `tests/test_reproducibility.py` does that for every run, on every `pytest` run, in CI too.

## What is checked

| Test | Cases | What must match exactly |
|---|---:|---|
| `test_recorded_run_reproduces_from_its_seed[<variant>-seed-<n>]` | 1,100, one per recorded run | The telemetry the agent saw (every series value and every window z-score). Every tool call, re-run with its recorded arguments: the full result text, byte for byte, and its error flag. The recorded suspects, re-verified against those re-run calls: the same accepted and rejected suspects with the same verifier notes, the same answer or escalation. The scoring: the z-score control's top three, AC@1 and AC@3. Also that each variant used only the tools it was given (the one-prompt runs made no call beyond `c0`). |
| `test_replay_blast_radius_and_rollback_match_the_pinned_checks[replay-<n>]` | 183, one per replay | What `/api/replays/<n>` adds to the recording: the answer's blast-radius check and its rollback replay (abnormal, explained, unexplained, cleared, still abnormal, alarm after, proves), and whether the replay counts as a caught wrong answer. Also that a replay never changes a recorded event, only adds to it. |
| `test_every_replay_is_pinned` | 1 | The replay list still has exactly the 183 pinned replays, in the same order. |
| `test_bench_evidence_reproduces_runs_evidence_json_byte_for_byte` | 1 | `bench.evidence()`, which `python bench.py --report` writes, equals the committed `runs/evidence.json` byte for byte: every results table, the auto route, the call-graph rule and the rollback what-ifs. |

## Counts

| Recorded runs | Normal outages (seeds 1000–1131) | Hard outages (seeds 2000–2043) |
|---|---:|---:|
| Agent on Opus 5.5 | 132 | 44 |
| Agent on Sonnet 5.5 | 132 | 44 |
| Agent on Haiku 5.5 | 132 | 44 |
| Opus 5.5, one prompt | 132 | 44 |
| Opus agent without logs | 44 | — |
| Agent on GLM-5.3 (open weights) | 132 | 44 |
| Agent on gpt-oss-120b (open weights) | 132 | 44 |
| **Total: 1,100 runs** | 836 | 264 |

- 176 distinct outages (every variant ran the same seeds), each simulated once per test session.
- 8,565 tool calls re-run and compared as text (4,526 of them from the open-weights runs).
- 2,592 suspects re-verified: 2,282 accepted and 310 rejected, all exactly as recorded.
- 183 replays: the 176 Opus runs, then the 7 wrong hard-mode answers the rollback caught (Haiku 5, Sonnet 2).

**Result today: all 1,100 runs, all 183 replays and `runs/evidence.json` reproduce. No test is marked xfail.**
The same 1,285 tests pass on Python 3.12 (the version CI uses), 3.13, 3.14 and 3.15.

## Run it

```bash
uv run --with-requirements requirements.txt --with pytest --with httpx pytest -q tests/test_reproducibility.py
# one run, by its variant and seed
uv run --with-requirements requirements.txt --with pytest --with httpx pytest -q tests/test_reproducibility.py -k "hard-haiku-seed-2000"
```

On the development laptop this file takes about 19 s (the evidence test alone is about 9 s: it re-simulates
every outage and every rolled-back answer), and the whole suite went from 204 passed and 1 skipped in about
17 s to 1,137 passed and 1 skipped in about 32 s. Nothing needed a `slow` marker.

## Do the tests catch a change? (mutation check)

Changing one constant at a time and running only this file (measured on the 748 Claude runs, before the
open-weights runs were added):

| Change | Failing tests |
|---|---|
| Evidence threshold `EVIDENCE_Z` 3.0 → 3.5 | 54 runs, 11 replays, the evidence file |
| Noise floor for CPU 1.5 → 1.6 | 497 runs |
| Only latency, not errors, spreads to callers in the blast-radius check | 36 replays, the evidence file |
| Log lines shown 12 → 5 | 165 runs |
| Log lines shown 12 → 11 | none: no recorded `get_logs` call ever had more than 10 warning lines |

The last row is the honest limit: these tests pin what the recordings exercised, not every code path.

## What this does and does not show

- **It shows** that the code is deterministic: the simulator, the tools, the verifier, the scoring, the
  blast-radius check, the rollback replay and every results table give today exactly what they gave when the
  runs were recorded. A reader can re-derive every published number from the run files without a key.
- **It does not re-run the models.** A fresh run of a model can call other tools and answer differently; the
  recorded runs are one sample each, which is why the results come with confidence intervals and paired tests.
- **The blast-radius and rollback fields were never recorded with the runs.** `/api/replays/<n>` computes them
  on every request. They are pinned in `tests/fixtures/replay_checks.json`, generated by this code on
  2026-11-15, so the replay tests guard against change from here on; they are not a comparison with something
  recorded at run time. The rollback numbers that were recorded earlier, in `runs/evidence.json`, are checked by
  the evidence test.
- **Reproducible is not realistic.** The outages are simulated, and in the simulator rolling back the true
  cause is exactly what clears the fault. These tests prove the replay gives the same answer every time, not
  that a canary rollback in production would.
- **Python's random module only promises the same `random()` stream across versions**, not the same Gaussian,
  exponential or binomial draws. Those match on 3.12 to 3.15 today; if a future Python changes them, these tests
  are what will say so.

## After a deliberate change

If a change to the code is meant to change what a replay shows, re-pin the replay checks and say why in the
commit:

```bash
uv run --with-requirements requirements.txt python -m tests.test_reproducibility
```

A change meant to alter the results tables rewrites `runs/evidence.json` with `python bench.py --report`, and the
new file goes in the same commit. A change that alters a recorded run's telemetry, tool results or verdict has
no re-pin: the run files are the recordings the published results were measured on. Either the code goes back,
or the runs are recorded again (with API calls) and the results say so.
