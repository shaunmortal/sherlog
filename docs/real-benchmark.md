# Sherlog on real outages: a public benchmark

Data: RCAEval benchmark, MIT licence, <https://huggingface.co/datasets/phamquiluan/RCAEval>.

Everything else Sherlog reports was measured on its simulated shop. This page measures it on real telemetry: 270
outages recorded on three real, deployed microservice systems, each broken on purpose by a known fault (the
benchmark's RE2 suite). The cases are scored with the benchmark's own evaluator, against published baselines
re-run on the same cases with the benchmark's own runner. Every number here is in
[`runs/real-benchmark/results.json`](../runs/real-benchmark/results.json), recomputed with no API calls by
`scripts/benchmark/report.py` from the run records next to it.

## Results on the test set (252 cases)

| Method | Cases | AC@1 (95% CI) | AC@3 | Avg@5 | A: AC@1 / Avg@5 | B: AC@1 / Avg@5 | C: AC@1 / Avg@5 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Sherlog no-AI: z-score ranking | 252 | 78% (73%-83%) | 92% | 0.91 | 89% / 0.95 | 89% / 0.97 | 56% / 0.80 |
| Sherlog no-AI: robust (median/MAD) z-score ranking | 252 | 66% (60%-72%) | 89% | 0.85 | 77% / 0.93 | 81% / 0.95 | 40% / 0.67 |
| Sherlog no-AI: call-graph rule | 252 | 83% (78%-87%) | 91% | 0.90 | 93% / 0.96 | 94% / 0.97 | 62% / 0.78 |
| Sherlog agent, auto route (Claude Haiku 5.5, Claude Opus 5.5 when needed) | 252 | 92% (88%-95%) | 96% | 0.95 | 98% / 0.98 | 98% / 0.99 | 82% / 0.88 |
| Sherlog agent, Claude Opus 5.5 | 14 | 86% (60%-96%) | 93% | 0.91 | 100% / 1.00 | 100% / 1.00 | 50% / 0.70 |
| Sherlog agent, auto route, every submitted suspect (verified or not) | 252 | 92% (88%-95%) | 96% | 0.95 | 98% / 0.98 | 98% / 0.99 | 81% / 0.89 |
| Published baseline: BARO (metrics) | 252 | 29% (24%-35%) | 88% | 0.77 | 14% / 0.74 | 6% / 0.77 | 67% / 0.79 |
| Published baseline: n-sigma (metrics) | 252 | 80% (75%-85%) | 91% | 0.90 | 79% / 0.91 | 87% / 0.95 | 75% / 0.83 |
| Published baseline: multi-source BARO (metrics, logs, traces) | 252 | 31% (25%-36%) | 88% | 0.77 | 17% / 0.76 | 6% / 0.76 | 69% / 0.79 |
| Published baseline: multi-source n-sigma (metrics, logs, traces) | 252 | 81% (76%-85%) | 93% | 0.91 | 80% / 0.94 | 88% / 0.95 | 75% / 0.83 |
| Published baseline: CIRCA (metrics; systems A and B only, too slow on C) | 168 | 69% (62%-76%) | 91% | 0.87 | 58% / 0.81 | 80% / 0.93 | not run |
| Published baseline: epsilon-Diagnosis (metrics) | 252 | 6% (4%-10%) | 22% | 0.17 | 4% / 0.15 | 15% / 0.38 | 0% / 0.00 |

AC@k: the root-cause service is among the method's first k. Avg@5: the mean of AC@1 to AC@5. A, B, C: the
12-, 15- and 68-service systems, 84 test cases each. The agent's ranking is its verified suspects (at most 5).

- **The auto route named the root-cause service first in 233 of 252 test cases (92%, 95% CI 88% to 95%).** The
  strongest published baselines here, n-sigma and its multi-source version, did in 80% and 81%; Sherlog's own
  no-AI call-graph rule in 83%. Paired case by case, the agent was right where n-sigma was wrong 40 times, and
  n-sigma right where the agent was wrong 9 times (exact McNemar p = 9.3e-6; against multi-source n-sigma 39 and
  10, p = 3.9e-5).
- **It is weakest on the largest system and on network faults.** System C (68 services): 82% first, against 75%
  for n-sigma, and 26 of its 84 cases went to a human. Delay faults: 83%; packet loss: 74%.
- **It answered 214 of the 252 cases on its own, and 6 of those answers were wrong (2.8%).** The other 38 went to
  a human because its top suspect's confidence was under 80% (37) or failed the citation check (1); in 25 of
  those 38 its top verified suspect was in fact the cause.
- **Cost: $8.01 for all 252 cases, $0.032 a case on average, $0.0024 at the median.** Claude Haiku 5.5's answer
  stood in 189 cases; Claude Opus 5.5 took over in 63 (43 of them on system C). The live runs of this whole
  evaluation, dev set and Opus included, metered $9.70 against a $10 cap.
- **Claude Opus 5.5 alone ran on only 14 test cases**, the most the cap left room for, in a seeded random order:
  12 of 14 first, too few to compare with anything (its paired tests are in `results.json`).
- BARO's first suspect is a datastore's disk-I/O metric in 149 of the 252 test cases, so its AC@1 is low; its Avg@5 is what the
  benchmark's runner itself prints for these cases (`runs/real-benchmark/baselines/baro-*.txt`).
  epsilon-Diagnosis, as the benchmark ships it, returns three candidates per case and none on system C is right;
  the runner's own report agrees (0.00 on every fault there).

## The cases

| System | Services in the telemetry | Cases | Faults | Telemetry Sherlog reads |
|---|---:|---:|---|---|
| A | 12 (11 to 14 in a few cases) | 90 | cpu, mem, disk, delay, loss, socket; 5 root-cause services x 3 repetitions each | metrics, logs, traces |
| B | 15 (16 in two cases) | 90 | the same | metrics, logs (no traces exist) |
| C | 68 (69 in five cases) | 90 | the same | metrics, logs (one case has none), traces |

Per service the benchmark records up to seven metric kinds: CPU, memory, disk I/O, open sockets, request rate,
failed requests and request latency (p50 and p90), once per second, from 12 minutes before the injection to 12
after it (3.5 minutes after it in the shortest case).

## Protocol

**Blind cases.** The benchmark's own exporter writes each case under an anonymised id
(`re2-ob-0042`) with its telemetry, injection time and candidate services, and keeps the labels in a separate
answers file. `scripts/benchmark/prepare.py` runs it, then hardlinks each case's logs and traces from the
benchmark's Parquet copy into the case directory (it reads the answers file only to find those files).
The converter reads nothing but the case directory. The raw case directories are named after their root cause,
so neither their names nor the answers file reach Sherlog.

**Split, frozen before any live call.** `scripts/benchmark/split.py` puts 18 cases in a dev set: per system, one
case per fault, all from repetition 1, the root-cause services taken in turn (6.7% of the cases). The other 252
are the test set. The split was committed at 11:09 IST (`chore: freeze the benchmark's dev/test split before any
live run`); the first live call was at 11:15, on dev cases. Only dev cases' telemetry and results were looked at
while adapting the converter, and they showed nothing that needed changing in the prompt or in Sherlog. Three
converter fixes came from documents that failed the upload checks, some of them test cases (a CamelCase proxy
name, an idle service's 0 ms latency), or from reading system C's traces (its untraced gateway); none came from
a result. They are in the commit that recorded the dev runs, made before any test-set live call. Only test-set
numbers are reported below; dev is separate.

**The window, as the benchmark hands it to every method.** Every method in the benchmark is told the injection
time. Sherlog gets it the same way: the converter cuts a 10-minute window with the injection exactly at tick 60
of Sherlog's 120 ticks of 5 s. The 5 minutes before it are Sherlog's healthy baseline, the 5 minutes after it the
outage. From there Sherlog runs as it always does: its alarm fires at the first tick where some metric has been 4
standard deviations above normal for 3 ticks (it fired in all 270 cases, between the injection tick and 11
ticks after it; in 9 cases already on the injection tick, from drift in the last baseline ticks), and the agent
sees the telemetry up to 4 ticks (20 s) after the alarm.

**Conversion** (`scripts/benchmark/adapter.py`, tested on synthetic rows in `tests/test_benchmark_adapter.py`):

- Metrics are the file the benchmark's runner feeds every method (`simple_metrics.csv`), prepared the way it
  prepares them (infinities as missing, missing readings carried forward, then zero), then averaged per 5 s tick.
  p50 latency is dropped and p90 kept, as the runner does. Latency becomes `latency_ms`; failed requests over all
  requests becomes `error_rate` where a service reports both; CPU, memory, disk I/O, sockets and request rate keep
  their own names (`cpu`, `mem`, `diskio`, `socket`, `workload`) and units. Each service reports only the metrics
  it has: a database has no request latency.
- Log lines keep their own level where they state one (a timestamp-then-level prefix, logfmt or JSON), and
  otherwise get one guessed from their words (`exception`, `error`: ERROR; `failed`, `timeout`, `refused`: WARN).
  Only WARN and ERROR lines go into the document, since the log tool shows only those; at most 2000, the
  earliest. Which service a line is about is derived from its text, as for any upload.
- The call graph: in systems A and C, from the spans that started before the injection, so the fault cannot shape
  it. Traces cover 7 of system A's 12 services and 27 of system C's 68, so three rules fill the rest, all from
  names and each system's published architecture, never from a label: a datastore named after a service is that
  service's (`carts` calls `carts-db`); in system C, the untraced gateway calls every service whose spans start a
  trace; and a short hand-written list (`adapter.HAND_CALLS`) adds the calls to and from system A's five untraced
  services and all of system B's calls (system B has no traces at all).
- Each document passes the same checks as an upload to `POST /api/triage` before Sherlog sees it.

**Methods.**

- Sherlog without AI: the z-score ranking, the robust (median and MAD) z-score ranking, and the call-graph rule
  (`investigate.graph_ranking`: rank services by how many abnormal services each explains through the call graph).
- Published baselines, re-run with the benchmark's `main.py` on every case (`scripts/benchmark/baselines.py`):
  BARO and n-sigma on metrics, their multi-source versions on metrics, logs and traces, and epsilon-Diagnosis.
  CIRCA ran on systems A and B only: a single case of system C did not finish in 10 minutes on this machine, so
  its 90 cases would take many hours. RCD and its multi-source version need a separate Python 3.8 environment and were not run;
  the methods that need a GPU-sized install (CausalRCA and others built on torch) were not run on this 8 GB
  machine.
- The Sherlog agent, live: the auto route (Claude Haiku 5.5 first; Claude Opus 5.5 investigates afresh when
  Haiku's answer does not stand) on every test case, and Claude Opus 5.5 alone on as many test cases as the spend
  cap left room for, in a seeded random order.

**Scoring.** The benchmark's own evaluator (run by `scripts/benchmark/score.py`), at the service level: AC@k says the root-cause service is among a method's first k, Avg@5 is the mean of AC@1 to AC@5. A
`-db` container counts as its service, as the evaluator folds it. For the agent, the ranking scored is its
submitted suspects that the verifier accepted, in its order, as Sherlog scores itself on the shop; the row
"every submitted suspect" scores all of them. `report.py` recomputes every number from the run records and stops
if one differs from the evaluator's. 95% intervals for AC@1 are Wilson score intervals; agent-versus-method
comparisons are exact McNemar tests on AC@1, paired case by case.

## More detail (test set)

**By fault (AC@1).** 42 test cases per fault (28 for CIRCA; Opus alone ran 1 to 5 per fault and no loss case).

| Method (AC@1 by fault) | cpu | delay | disk | loss | mem | socket |
|---|---:|---:|---:|---:|---:|---:|
| Sherlog no-AI: z-score ranking | 83% | 57% | 100% | 50% | 98% | 81% |
| Sherlog no-AI: robust (median/MAD) z-score ranking | 60% | 52% | 100% | 38% | 88% | 60% |
| Sherlog no-AI: call-graph rule | 86% | 71% | 93% | 67% | 93% | 88% |
| Sherlog agent, auto route (Claude Haiku 5.5, Claude Opus 5.5 when needed) | 98% | 83% | 100% | 74% | 100% | 100% |
| Sherlog agent, Claude Opus 5.5 | 100% | 60% | 100% | - | 100% | 100% |
| Sherlog agent, auto route, every submitted suspect (verified or not) | 98% | 83% | 100% | 71% | 100% | 100% |
| Published baseline: BARO (metrics) | 14% | 17% | 38% | 33% | 50% | 21% |
| Published baseline: n-sigma (metrics) | 86% | 64% | 98% | 64% | 95% | 74% |
| Published baseline: multi-source BARO (metrics, logs, traces) | 14% | 17% | 36% | 40% | 55% | 21% |
| Published baseline: multi-source n-sigma (metrics, logs, traces) | 86% | 64% | 98% | 69% | 98% | 71% |
| Published baseline: CIRCA (metrics; systems A and B only, too slow on C) | 71% | 50% | 86% | 57% | 82% | 68% |
| Published baseline: epsilon-Diagnosis (metrics) | 7% | 12% | 7% | 2% | 7% | 2% |

**The agent against each method, case by case (AC@1).**

| Agent vs | Paired cases | Only the agent right | Only the other right | McNemar p |
|---|---:|---:|---:|---:|
| auto vs Sherlog no-AI: z-score ranking | 252 | 39 | 3 | 5.6e-09 |
| auto vs Sherlog no-AI: robust (median/MAD) z-score ranking | 252 | 67 | 1 | 4.7e-19 |
| auto vs Sherlog no-AI: call-graph rule | 252 | 26 | 2 | 3e-06 |
| auto vs Sherlog agent, Claude Opus 5.5 | 14 | 0 | 0 | 1 |
| auto vs Published baseline: BARO (metrics) | 252 | 168 | 8 | 4.3e-40 |
| auto vs Published baseline: n-sigma (metrics) | 252 | 40 | 9 | 9.3e-06 |
| auto vs Published baseline: multi-source BARO (metrics, logs, traces) | 252 | 165 | 9 | 2.9e-38 |
| auto vs Published baseline: multi-source n-sigma (metrics, logs, traces) | 252 | 39 | 10 | 3.9e-05 |
| auto vs Published baseline: CIRCA (metrics; systems A and B only, too slow on C) | 168 | 51 | 3 | 2.9e-12 |
| auto vs Published baseline: epsilon-Diagnosis (metrics) | 252 | 218 | 1 | 5.2e-64 |

**What the agent did.** `answered`: a verified suspect with confidence of 80% or more; otherwise
`handed_to_human`. `citation_check_pass`: the share of submitted suspects whose cited tool calls really showed
them abnormal. `blast_better_than_answer`: answers for which another service explains more of the abnormal
services through the call graph; `blast_unexplained_left`: answers that leave some abnormal service unexplained
(real telemetry has unrelated anomalies, so this is common). Costs are in US dollars.

| | auto | opus |
|---|---:|---:|
| cases | 252 | 14 |
| no_alarm | 0 | 0 |
| answered | 214 | 11 |
| handed_to_human | 38 | 3 |
| wrong_when_answered | 6 | 0 |
| right_when_answered | 0.972 | 1.0 |
| citation_check_pass | 0.9143 | 0.8095 |
| top_suspect_verified | 0.996 | 1.0 |
| handed_to_opus | 63 | 0 |
| blast_better_than_answer | 12 | 0 |
| blast_unexplained_left | 111 | 3 |
| failed_runs | 0 | 0 |
| cost_usd_total | 8.0081 | 1.0482 |
| cost_usd_mean | 0.03178 | 0.07487 |
| cost_usd_median | 0.00242 | 0.05062 |
| wall_s_mean | 18.4 | 13.7 |

| System | Test cases | Answered | Wrong answers | To a human | Handed to Opus | Cost |
|---|---:|---:|---:|---:|---:|---:|
| A (12 services) | 84 | 81 | 2 (loss) | 3 | 7 | $0.64 |
| B (15 services) | 84 | 75 | 0 | 9 | 13 | $1.15 |
| C (68 services) | 84 | 58 | 4 (3 loss, 1 cpu) | 26 | 43 | $6.22 |

## The dev set (18 cases, reported apart)

The auto route named the cause first in 17 of 18 dev cases, answered 15 with no wrong answer, and cost $0.646;
the z-score ranking and the call-graph rule named it first in 13 of 18 each.

## Deviations from the benchmark's protocol

- **A 5-minute baseline and a 20-second look, not 10 minutes on each side.** The runner gives methods 10 minutes
  before and after the injection. Sherlog keeps its own window: 5 minutes before as its baseline, and only up to
  20 s after its alarm for the agent (the no-AI rankings average from the alarm to that point too). This is
  Sherlog's design, an investigator that starts while the outage is young, and it sees less of each outage than
  the baselines do.
- **Logs: WARN and ERROR lines only.** INFO lines are left out of the documents, so the log tool reports zero
  INFO lines; the baselines' multi-source versions read the benchmark's own log template counts instead.
- **Resampling and units.** Readings are averaged per 5 s tick, latency is in milliseconds, and an idle
  service's 0 ms latency is raised to Sherlog's 0.1 ms floor (as Sherlog's Prometheus converter clamps values to a
  metric's limits). A tick with no reading holds the previous one (2 cases of system A end before the window
  does).
- **Proxy pseudo-services keep their place under lower-case names.** A few cases' metrics include mesh-proxy
  entries spelt in CamelCase (`PassthroughCluster`); uploads need lower-case names, and the evaluator lower-cases
  every name, so nothing it scores changes.
- **CIRCA falls back to column order on a case where its independence test fails** (a singular correlation
  matrix, seen on system B); the runner prints the error and goes on, and that ranking is scored as given.
- **A baseline crash is a miss.** The runner stops at a method's first exception. Multi-source BARO and
  multi-source n-sigma raise on one case of system C (an empty window in its derived log or trace series); that
  case was left out, the method re-run on the rest, and the case scored as a miss for it (`*-re2-tt.txt` in
  `runs/real-benchmark/baselines/` records the error).
- **The agent submits at most 5 suspects**, and only verified ones are scored, so its AC@5 and Avg@5 can only be
  lower than a full ranking's would be; the baselines rank every candidate.
- **Live runs that hit an API error** were retried up to twice; the records count failed runs.

## Limitations

- One benchmark suite, three systems, six injected fault types. Injected faults are cleaner than real
  incidents: one root cause each, injected at a known time, on a system under synthetic load. Code-level faults
  (the benchmark's RE3 suite) were not run.
- The test set is 252 cases, 84 per system, so per-system and per-fault numbers have wide intervals; differences
  of a few cases between methods are not significant (see the McNemar column).
- Parts of the call graph are hand-written from each system's published architecture (systems A and B), and part
  is a naming rule. A wrong edge there would mislead the call-graph rule and the blast-radius check.
- The log level of lines that state none is guessed from their words. Most cases of systems A and C have no
  WARN or ERROR line at all in the window (71 and 66 of 90), so there the log tool rarely has anything to show.
- Prices are list prices per million tokens from `bench.py` (Opus 5.5 $4 in / $20 out, Haiku 5.5 $0.10 / $0.50),
  applied to every response's token counts, including failed and retried calls.
- The agent's committed run records leave out tool results, thinking and free text (they quote the telemetry);
  the full event streams were kept outside the repository.

## Reproduce

Every step but the live runs is free and needs no key. The benchmark's code runs in its own environment, never
the app's: install its package from its source (the dataset page links to it) into a separate virtual
environment with `pip install -e .[default]`, plus `pydantic==2.14.3 anthropic==2.8.0` so the converter can
import Sherlog's checks. `$W` is any scratch folder outside the repository.

```sh
# 1. Download only what is needed (a few hundred MB, not the 5 GB archives). ARCHIVE_URL: the RE2-OB, RE2-SS
#    and RE2-TT archive links on the dataset page; DATASET_ID: the owner/name in the dataset page's link.
# Run the archive line once per archive URL.
python -I scripts/benchmark/fetch.py archive "$ARCHIVE_URL" $W/data/RE2 '*/simple_metrics.csv' \
    '*/inject_time.txt' '*/logts.csv' '*/tracets_err.csv' '*/tracets_lat.csv' '*/cluster_info.json' 
python -I scripts/benchmark/fetch.py parquet $DATASET_ID $W/data/parquet 're2*/logs.parquet' \
    're2ob*/traces.parquet' 're2tt*/traces.parquet'

# 2. The blind task view, its labels (runs/real-benchmark/answers.jsonl) and the split (already committed)
python -I scripts/benchmark/prepare.py $W/data/RE2 $W/data/parquet $W/tasks
python scripts/benchmark/split.py

# 3. Convert each case to a Sherlog incident document (a few seconds per case, one at a time)
for d in re2-ob re2-ss re2-tt; do python -I scripts/benchmark/convert.py $W/tasks/$d $W/converted; done

# 4. Run Sherlog: no AI, then live (a key in .env; the budget stops new cases before the cap)
for m in zscore robust graph; do
  uv run --with-requirements requirements.txt python scripts/benchmark/run.py $W/converted --set test --method $m
done
uv run --env-file .env --with-requirements requirements.txt python scripts/benchmark/run.py $W/converted \
    --set test --method auto --budget-usd 9 --workers 4 --full-dir $W/full

# 5. The published baselines, with the benchmark's runner
python -I scripts/benchmark/baselines.py <benchmark source>/main.py $W/data $W/tasks $W/baselines \
    baro nsigma mmbaro mmnsigma e_diagnosis circa:re2-ob,re2-ss

# 6. Score with the benchmark's evaluator, then recompute and cross-check everything
python -I scripts/benchmark/score.py $W
uv run --with-requirements requirements.txt python scripts/benchmark/report.py
```

`report.py` alone (the last line) reproduces `results.json` from the committed records, with no data, no key
and no network.
