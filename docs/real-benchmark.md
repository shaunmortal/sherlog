# Sherlog on real outages: a public benchmark

Data: RCAEval benchmark, MIT licence, <https://huggingface.co/datasets/phamquiluan/RCAEval>.

Everything else Sherlog reports was measured on its simulated shop. This page measures it on real telemetry: 270
outages recorded on three real, deployed microservice systems, each broken on purpose by a known fault (the
benchmark's RE2 suite). The cases are scored with the benchmark's own evaluator, against published baselines
re-run on the same cases with the benchmark's own runner. Every number here is in
[`runs/real-benchmark/results.json`](../runs/real-benchmark/results.json), recomputed with no API calls by
`scripts/benchmark/report.py` from the run records next to it.

A second suite, 90 code-level faults on the same three systems (the benchmark's RE3 suite), was run afterwards
with the same protocol and is reported on its own, under the key `re3` of the same file:
[Code-level faults (RE3)](#a-second-suite-code-level-faults-re3). Everything above that section is RE2.

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
- BARO's first suspect is a datastore's disk-I/O metric in 149 of the 252 test cases, so its AC@1 is low; its
  Avg@5 is what the benchmark's runner itself prints for these cases (`runs/real-benchmark/baselines/baro-*.txt`).
  epsilon-Diagnosis, as the benchmark ships it, returns three candidates per case and none on system C is right;
  the runner's own report agrees (0.00 on every fault there).

**What Sherlog reads that the baselines do not.** Sherlog reasons over the system's call graph, who calls whom:
that is its design. The call-graph rule ranks by it, the auto route's hand-off uses it (Haiku's answer does not
stand if another service explains more of the outage through the graph), and the agent's dependency tool shows
it. Here the graph comes from traces recorded before the injection, a naming rule for datastores, and calls
written by hand from each system's published architecture: all of system B's, which has no traces, and those of
system A's five untraced services. None of the baselines gets a graph. n-sigma, BARO and epsilon-Diagnosis read
metrics only; the multi-source versions also read logs and traces, but only as time series (log template counts,
trace error and latency series), not as a graph. The agent also reads the text of WARN and ERROR log lines, which
the metric-only baselines never see. Like for like, on the metrics alone with no graph and no log text, Sherlog's
z-score ranking names the cause first in 78% of test cases, against n-sigma's 80%. The graph lifts that to 83%
(the call-graph rule), and the investigation to 92%.

## The cases

| System | Services in the telemetry | Cases | Faults | Telemetry Sherlog reads |
|---|---:|---:|---|---|
| A | 12 (11 to 14 in a few cases) | 90 | cpu, mem, disk, delay, loss, socket; 5 root-cause services x 3 repetitions each | metrics, logs, a call graph from traces and by hand |
| B | 15 (16 in two cases) | 90 | the same | metrics, logs, a call graph by hand (no traces exist) |
| C | 68 (69 in five cases) | 90 | the same | metrics, logs (one case has none), a call graph from traces |

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

**The split, and what its freeze protects.** `scripts/benchmark/split.py` puts 18 cases in a dev set: per
system, one case per fault, all from repetition 1, the root-cause services taken in turn (6.7% of the cases). The
other 252 are the test set. The split was committed at 11:09:50 IST (`chore: freeze the benchmark's dev/test split
before any live run`). The first live call was made at about 11:14:36 (the first dev session ended at 11:15:29
after 53 s), on dev cases, and no test case went to a model before the dev runs were committed at 11:18:55.

The freeze protects the split and every change made after it. It does not protect what came before it, while the
test cases' telemetry was already on disk:

- Sherlog's own changes for this benchmark: the per-incident metrics and the 1% noise floor for a metric with no
  floor of its own (commit 71cf3d9, 10:47).
- The converter's rules: the log-level rules, the datastore naming rule, the hand-written calls
  (`adapter.HAND_CALLS`), the gateway rule and the lower-case names (commit 97a3c14, 11:09:50, the same minute as
  the split).
- The conversion of every case of systems A and B. Label-free counts were looked at for every case (alarm tick,
  log lines, services); rankings and tool output only for the six dev cases of system B.
- Timing runs of the baselines on system B (BARO, n-sigma, their multi-source versions, epsilon-Diagnosis and
  CIRCA), whose printed per-fault Avg@5 covers its test cases too.
- One test case of system A failed the upload checks at 11:05 (a CamelCase proxy name); the lower-case names came
  from that.

After the split, one converter change was made: an idle service's 0 ms latency is raised to the 0.1 ms floor,
after a test case of system C failed the upload checks. It is in the commit that recorded the dev runs, before
any test-set live call. No change came from a result on a test case, and the dev runs showed nothing that needed
changing in the prompt or in Sherlog. (A later fix to how an upload is exported, 103aa52, changes nothing the runs
used.) The times are this machine's local clock and are self-attested: the split commit was not pushed anywhere
before the runs. Only test-set numbers are reported below; dev is separate.

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
  it. Traces cover 7 of system A's 12 services and 27 of system C's 68 (20 or 21 in 13 of its 90 cases), so three
  rules fill the rest, all from
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
  CIRCA ran on systems A and B only: on system C, a run of the runner on a single case was stopped by a 600 s
  timeout before it finished (`runs/real-benchmark/baselines/circa-re2-tt.txt`), so its 90 cases would take more
  than 15 hours here. RCD and its multi-source version need a separate Python 3.8 environment and were not run;
  the methods that need a GPU-sized install (CausalRCA and others built on torch) were not run on this 8 GB
  machine.
- The Sherlog agent, live: the auto route (Claude Haiku 5.5 first; Claude Opus 5.5 investigates afresh when
  Haiku's answer does not stand) on every test case, and Claude Opus 5.5 alone on as many test cases as the spend
  cap left room for. Both took the test cases in one seeded random order, `random.Random(2026).shuffle` over
  `split.json`'s test ids (`run.py --seed`, default 2026), so the cases the cap left out are a random remainder.

**Scoring.** The benchmark's own evaluator (run by `scripts/benchmark/score.py`), at the service level: AC@k
says the root-cause service is among a method's first k, Avg@5 is the mean of AC@1 to AC@5. A `-db` container
counts as its service, as the evaluator folds it. For the agent, the ranking scored is its
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

**n-sigma with other windows.** The runner gives every method 10 minutes on each side of the injection.
Given 5 or 1 minute instead (`baselines.py ... nsigma/10 nsigma/2`, scored like the rest), n-sigma does about the
same, and the agent's lead holds at each:

| n-sigma window | AC@1 | Only the auto route right | Only n-sigma right | McNemar p |
|---|---:|---:|---:|---:|
| Published baseline: n-sigma (metrics) | 80.2% | 40 | 9 | 9.3e-06 |
| n-sigma given 5 min on each side of the injection (window check) | 81.3% | 39 | 11 | 9e-05 |
| n-sigma given 1 min on each side of the injection (window check) | 77.8% | 48 | 11 | 1.2e-06 |

**When the first suspect is wrong** (the check for a learned prior, see Limitations): how often it is a service
the benchmark breaks in some other case of the same system.

| Method | First suspect wrong | ...and a service broken in another case |
|---|---:|---:|
| Sherlog no-AI: z-score ranking | 55 | 13 |
| Sherlog no-AI: robust (median/MAD) z-score ranking | 85 | 15 |
| Sherlog no-AI: call-graph rule | 43 | 12 |
| Sherlog agent, auto route (Claude Haiku 5.5, Claude Opus 5.5 when needed) | 19 | 6 |
| Sherlog agent, Claude Opus 5.5 | 2 | 0 |
| Sherlog agent, auto route, every submitted suspect (verified or not) | 20 | 7 |
| Published baseline: BARO (metrics) | 179 | 5 |
| Published baseline: n-sigma (metrics) | 50 | 20 |
| Published baseline: multi-source BARO (metrics, logs, traces) | 175 | 6 |
| Published baseline: multi-source n-sigma (metrics, logs, traces) | 48 | 18 |
| Published baseline: CIRCA (metrics; systems A and B only, too slow on C) | 52 | 33 |
| Published baseline: epsilon-Diagnosis (metrics) | 236 | 83 |

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
  the baselines do. n-sigma given 5 or 1 minute on each side scores 81% and 78% (the window check above), so the
  window is not what separates it from the agent.
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

- One live run per case. The agent's run-to-run variance was not measured; the confidence intervals cover the
  sampling of cases only.
- The benchmark is public, so its cases or its answers may be in the models' training data. A check for a learned
  prior: when a method's first suspect is wrong, how often is it a service the benchmark breaks in some other case
  of the same system? The agent: 6 of its 19 wrong first suspects; n-sigma: 20 of 50; the call-graph rule: 12 of
  43 (`wrong_first_suspects` in `results.json`). The agent picks such a service no more often than methods that
  cannot have memorised anything, so there is no sign of a learned prior; this does not rule one out.
- One benchmark suite, three systems, six injected fault types. Injected faults are cleaner than real
  incidents: one root cause each, injected at a known time, on a system under synthetic load. Code-level faults
  (the benchmark's RE3 suite) were run afterwards and are reported apart, below.
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

## A second suite: code-level faults (RE3)

The benchmark's RE3 suite breaks the same three systems a different way: instead of starving a service of CPU,
memory, disk or network, a faulty code change is deployed into one service. The dataset labels its five
code-level fault types F1 to F5 and does not describe them further in the data. The suite has 90 cases, 30 per
system, and is not a uniform grid: F5 exists only on system A, and the root cause is one of 4 services on
system A (12 services in the telemetry, 28 in two cases), 3 on system B (15, 26 in one case) and only 2 on
system C (68, 69 in seven cases). It was run with the protocol above, unchanged except where this section
says so. Its records are in [`runs/real-benchmark/re3/`](../runs/real-benchmark/re3/) and its numbers under the
key `re3` of `results.json`; RE2's keys and values there are unchanged.

### Results on the RE3 test set (81 cases)

| Method | Cases | AC@1 (95% CI) | AC@3 | Avg@5 | A: AC@1 / Avg@5 | B: AC@1 / Avg@5 | C: AC@1 / Avg@5 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Sherlog no-AI: z-score ranking | 81 | 74% (64%-82%) | 84% | 0.83 | 96% / 0.96 | 96% / 0.99 | 30% / 0.53 |
| Sherlog no-AI: robust (median/MAD) z-score ranking | 81 | 52% (41%-62%) | 65% | 0.64 | 74% / 0.92 | 81% / 0.94 | 0% / 0.07 |
| Sherlog no-AI: call-graph rule | 81 | 59% (48%-69%) | 80% | 0.76 | 74% / 0.90 | 89% / 0.97 | 15% / 0.40 |
| Sherlog agent, auto route (Claude Haiku 5.5, Claude Opus 5.5 when needed) | 71 | 80% (70%-88%) | 92% | 0.89 | 100% / 1.00 | 96% / 0.99 | 41% / 0.66 |
| Sherlog agent, auto route, every submitted suspect (verified or not) | 71 | 80% (70%-88%) | 92% | 0.89 | 100% / 1.00 | 96% / 0.99 | 41% / 0.66 |
| Published baseline: BARO (metrics) | 81 | 26% (18%-36%) | 98% | 0.82 | 0% / 0.76 | 0% / 0.74 | 78% / 0.96 |
| Published baseline: n-sigma (metrics) | 81 | 73% (62%-81%) | 94% | 0.89 | 81% / 0.88 | 59% / 0.84 | 78% / 0.96 |
| Published baseline: multi-source BARO (metrics, logs, traces) | 81 | 25% (17%-35%) | 96% | 0.81 | 0% / 0.76 | 0% / 0.74 | 74% / 0.94 |
| Published baseline: multi-source n-sigma (metrics, logs, traces) | 81 | 69% (58%-78%) | 93% | 0.88 | 78% / 0.88 | 52% / 0.82 | 78% / 0.95 |
| Published baseline: CIRCA (metrics; systems A and B only, too slow on C) | 54 | 74% (61%-84%) | 98% | 0.93 | 81% / 0.96 | 67% / 0.90 | not run |
| Published baseline: epsilon-Diagnosis (metrics) | 81 | 2% (1%-9%) | 21% | 0.16 | 4% / 0.24 | 4% / 0.23 | 0% / 0.00 |

27 test cases per system. The auto route ran on 71 of the 81: the spend cap stopped it before the last 10 in its
seeded order (2 of system A, 3 of B, 5 of C), so its row is over those 71 and its paired tests are on the cases
both methods ran.

- **On systems A and B the auto route named the cause first in 48 of 49 test cases** (25 of 25 and 23 of 24),
  where n-sigma did in 81% and 59% of their 27 cases each, and answered 44 of them on its own with no wrong answer.
- **Overall, 57 of 71 (80%, 95% CI 70% to 88%).** On the same 71 cases n-sigma named the cause first in 51 and
  multi-source n-sigma in 48. Paired, the agent was right where n-sigma was wrong 17 times and the reverse 11
  times (exact McNemar p = 0.34; against multi-source n-sigma 18 and 9, p = 0.12): not a significant difference.
  Against Sherlog's own call-graph rule, 14 and 1 (p = 0.00098); against the z-score ranking, 6 and 5 (p = 1).
- **System C is where it fails: 9 of 22 first (41%), against 78% for n-sigma.** It handed 14 of those 22 to a
  human, and all 3 of its wrong answers in the suite were there (F1, F2 and F4). See below for why.
- **n-sigma does better with less data here, and then beats the agent.** Given 5 or 1 minute on each side of the
  injection instead of 10, n-sigma names the cause first in 83% and 88% of the 81 test cases (93% on system C with
  1 minute). Paired with the 1-minute version, the agent was right alone 7 times and n-sigma 13 times (p = 0.26).
  On RE2 the agent's lead held at every window; on RE3 it does not.
- **It answered 52 of the 71 on its own, and 3 of those answers were wrong (5.8%).** The other 19 went to a human
  (14 of them on system C); in 8 of those 19 its top verified suspect was in fact the cause.
- **Cost: $4.46 for the 71 cases, $0.063 a case on average, $0.058 at the median**, much more than on RE2
  because Claude Opus 5.5 took over in 40 of 71 (19 of system C's 22). All RE3 live runs, dev set included,
  metered $4.75 against a $5 cap.

**Why system C goes wrong.** In none of system C's 27 test cases did the faulty service's own latency or error
rate stay abnormal in the window the agent sees (on system A in 11 of 27, on B in 18). On system C only a few
services report failed requests at all (the faulty one in 4 of its 27 test cases), and its traces carry no
status codes (none in any of its 30 cases), so its failures barely reach Sherlog as errors. What moves is the
faulty service's memory or CPU (its strongest deviation in 23 of the 27), while services that call it slow down
or fail; in all three system-C dev cases its logs show it shutting down about 20 s after the injection, as the
faulty version is deployed. Sherlog's call-graph reasoning (the call-graph rule, the blast-radius check and the
auto route's hand-off) credits a service with its callers' trouble only when its own latency or errors are
abnormal, so on system C it points away from the cause: the call-graph rule names it first in 15% of the cases
there and the z-score ranking in 30%. n-sigma reads the same metric file and ranks the cause first in 78% of
them, and in 93% with 1 minute on each side, where its first entry is the cause's memory in 25 of 27 cases.
Sherlog's z-score scoring, with its 5-minute baseline and noise floors, ranks that memory jump below other
services' deviations; why exactly was not established.

**By fault (AC@1).** Test cases per fault: F1 24, F2 11, F3 24, F4 17, F5 5 (system A only); the auto route ran
on 23, 8, 20, 16 and 4 of them.

| Method (AC@1 by fault) | f1 | f2 | f3 | f4 | f5 |
|---|---:|---:|---:|---:|---:|
| Sherlog no-AI: z-score ranking | 88% | 55% | 71% | 71% | 80% |
| Sherlog no-AI: robust (median/MAD) z-score ranking | 67% | 45% | 42% | 41% | 80% |
| Sherlog no-AI: call-graph rule | 50% | 55% | 67% | 59% | 80% |
| Sherlog agent, auto route (Claude Haiku 5.5, Claude Opus 5.5 when needed) | 83% | 75% | 80% | 75% | 100% |
| Sherlog agent, auto route, every submitted suspect (verified or not) | 83% | 75% | 80% | 75% | 100% |
| Published baseline: BARO (metrics) | 17% | 55% | 25% | 29% | 0% |
| Published baseline: n-sigma (metrics) | 83% | 100% | 62% | 59% | 60% |
| Published baseline: multi-source BARO (metrics, logs, traces) | 21% | 55% | 21% | 24% | 0% |
| Published baseline: multi-source n-sigma (metrics, logs, traces) | 75% | 100% | 62% | 53% | 60% |
| Published baseline: CIRCA (metrics; systems A and B only, too slow on C) | 67% | 100% | 67% | 73% | 100% |
| Published baseline: epsilon-Diagnosis (metrics) | 4% | 0% | 0% | 6% | 0% |

**The agent against each method, case by case (AC@1).**

| Agent vs | Paired cases | Only the agent right | Only the other right | McNemar p |
|---|---:|---:|---:|---:|
| auto vs Sherlog no-AI: z-score ranking | 71 | 6 | 5 | 1 |
| auto vs Sherlog no-AI: robust (median/MAD) z-score ranking | 71 | 16 | 0 | 3e-05 |
| auto vs Sherlog no-AI: call-graph rule | 71 | 14 | 1 | 0.00098 |
| auto vs Published baseline: BARO (metrics) | 71 | 48 | 8 | 4.7e-08 |
| auto vs Published baseline: n-sigma (metrics) | 71 | 17 | 11 | 0.34 |
| auto vs Published baseline: multi-source BARO (metrics, logs, traces) | 71 | 50 | 9 | 5.3e-08 |
| auto vs Published baseline: multi-source n-sigma (metrics, logs, traces) | 71 | 18 | 9 | 0.12 |
| auto vs Published baseline: CIRCA (metrics; systems A and B only, too slow on C) | 49 | 13 | 1 | 0.0018 |
| auto vs Published baseline: epsilon-Diagnosis (metrics) | 71 | 55 | 0 | 5.6e-17 |

**n-sigma with other windows** (`baselines.py ... --suite re3 nsigma/10 nsigma/2`):

| n-sigma window | AC@1 | Only the auto route right | Only n-sigma right | McNemar p |
|---|---:|---:|---:|---:|
| Published baseline: n-sigma (metrics) | 72.8% | 17 | 11 | 0.34 |
| n-sigma given 5 min on each side of the injection (window check) | 82.7% | 12 | 13 | 1 |
| n-sigma given 1 min on each side of the injection (window check) | 87.6% | 7 | 13 | 0.26 |

**When the first suspect is wrong** (the check for a learned prior): each system has only 2 to 4 root-cause
services here, so the second column has few services to land on.

| Method | First suspect wrong | ...and a service broken in another case |
|---|---:|---:|
| Sherlog no-AI: z-score ranking | 21 | 1 |
| Sherlog no-AI: robust (median/MAD) z-score ranking | 39 | 0 |
| Sherlog no-AI: call-graph rule | 33 | 3 |
| Sherlog agent, auto route (Claude Haiku 5.5, Claude Opus 5.5 when needed) | 14 | 1 |
| Sherlog agent, auto route, every submitted suspect (verified or not) | 14 | 1 |
| Published baseline: BARO (metrics) | 60 | 0 |
| Published baseline: n-sigma (metrics) | 22 | 7 |
| Published baseline: multi-source BARO (metrics, logs, traces) | 61 | 0 |
| Published baseline: multi-source n-sigma (metrics, logs, traces) | 25 | 10 |
| Published baseline: CIRCA (metrics; systems A and B only, too slow on C) | 14 | 2 |
| Published baseline: epsilon-Diagnosis (metrics) | 79 | 16 |

**What the agent did** (the same measures as for RE2):

| | auto |
|---|---:|
| cases | 71 |
| no_alarm | 0 |
| answered | 52 |
| handed_to_human | 19 |
| wrong_when_answered | 3 |
| right_when_answered | 0.9423 |
| citation_check_pass | 0.8641 |
| top_suspect_verified | 0.9859 |
| handed_to_opus | 40 |
| blast_better_than_answer | 10 |
| blast_unexplained_left | 21 |
| failed_runs | 0 |
| cost_usd_total | 4.4616 |
| cost_usd_mean | 0.06284 |
| cost_usd_median | 0.05765 |
| wall_s_mean | 23.6 |

| System | Test cases | Run live | First | Answered | Wrong answers | To a human | Handed to Opus | Cost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A (12 services) | 27 | 25 | 25 | 24 | 0 | 1 | 6 | $0.37 |
| B (15 services) | 27 | 24 | 23 | 20 | 0 | 4 | 15 | $1.19 |
| C (68 services) | 27 | 22 | 9 | 8 | 3 (f1, f2, f4) | 14 | 19 | $2.90 |

### The RE3 dev set (9 cases, reported apart)

The auto route named the cause first in all 9 dev cases, answered 8 with no wrong answer, and cost $0.286; the
z-score ranking named it first in 8 and the call-graph rule in 7. The dev set (3 of system C) did not show the
system-C weakness the test set did, at least not in the agent's first suspect.

### RE3 protocol: what differs from RE2

- **Data: the dataset's Parquet copy only** (about 330 MB for the suite). Its `metrics.parquet` holds the
  archive's `simple_metrics.csv` value for value (checked on two RE2 cases, every column and timestamp), so
  `prepare.py` writes it back as that file, for the benchmark's runner and exporter. RE3 ships none of the log and
  trace series the multi-source methods read (`logts.csv`, `tracets_err.csv`, `tracets_lat.csv`), so
  `prepare.py` builds them from each case's logs and traces with the benchmark package's own derive step (15 s
  bins, log templates mined with its default masks). The benchmark documents that this step differs in details
  from the scripts behind the series it released for RE2 (it counts error spans where those summed status codes,
  for one), so multi-source BARO and n-sigma read series built as any new user's would be.
- **Blind cases**: the benchmark's exporter, unchanged (`re3-ob-0001` ...); labels in `re3/answers.jsonl`.
- **The split**: 9 dev cases, 10% (`split.py re3`). One case per system and fault would be 13 (14%), so per
  system three faults consecutive in sorted order, chosen so that all five fault types are in dev; per fault, the
  repetition-1 case of the services with that fault taken in turn. It was committed at 15:53:23 IST
  (`chore: freeze RE3's dev/test split before any live run`), together with the scripts' suite option one commit
  earlier at the same second. The first live call was made at about 15:56:50, on dev cases; the dev runs were
  committed at 15:58:13, and the test session ran from 15:59:09 to 16:07:28.
- **What the freeze does not protect**: before it, the suite was downloaded and laid out; the file layout and
  time units of one case per system and the case index's grid (systems, faults, services, repetitions, which
  the split rule is written for) were looked at. No ranking, tool output, converted document or telemetry
  content of any RE3 case was looked at before the split.
- **The converter was checked on the dev cases only, and not changed.** Code-level faults are said to show
  mostly in logs and error codes, so the dev cases' raw telemetry was read for what the conversion drops. Up to
  the agent's cut-off, the level rules missed 3 stack-trace lines in all 9 dev cases (2 of them by a root cause
  that already showed 6 WARN and ERROR lines). System C's traces record no status codes at all, system B has no
  traces, and on system A the error codes sit on the callers' spans while the root cause's own failed requests
  already reach the document through its metrics. INFO lines would only change a count the log tool prints. So
  RE3 is converted exactly as RE2 was; the hand-written calls and the gateway rule are keyed by system, since both
  suites record the same three systems. The test cases were converted after the dev runs were committed; their
  label-free counts were then looked at (the alarm fired in all 90 cases, between the injection tick and 9 ticks
  after it; one case has no WARN or ERROR line).
- **Published baselines**: BARO, n-sigma, their multi-source versions and epsilon-Diagnosis on all 90 cases (none
  crashed on any); CIRCA on systems A and B, since a run on a single case of system C was again stopped by a 600 s
  timeout (`re3/baselines/circa-re3-tt.txt`); and the n-sigma window check. The runner's own printed summary groups
  results by RE2's fault kinds and prints nothing for code-level faults (`re3/baselines/*.txt`), so for RE3 the
  benchmark's evaluator (`score.py ... re3`, `re3/official.json`) is the only cross-check, and `report.py` matches
  it on every method.
- **Live runs**: the auto route only, in the seeded order, with a hard cap of $5 for every RE3 live call, dev
  included. The dev set metered $0.286 and projected about $2.60 for the test set; the test cases cost more
  (system C's averaged $0.13, with Opus taking over in 19 of 22), and the session stopped starting cases at
  $4.46 with 10 left. The next case in the order was one of system C's, which ran up to $0.26 a case, and $0.25
  was left under the cap, so no more were run. Had the 10 left out gone at the agent's per-system rates, its
  overall AC@1 would be about 79% rather than 80%, since 5 of them are from system C, its weakest. Claude Opus
  5.5 alone was not run on RE3.

Everything in RE2's deviations applies unchanged: the agent's 5-minute baseline and 20-second look, WARN and
ERROR lines only, the resampling and units, the proxy names, CIRCA's fallback and at most 5 submitted suspects.

### RE3 limitations

- The agent ran on 71 of the 81 test cases; 27 per system, and system C has only 2 root-cause services, so
  per-system and per-fault numbers rest on few cases and few services.
- In the dev cases, about two thirds of the WARN and ERROR lines in the 10-minute window came after the agent's
  cut-off (1,636 of 5,098 were before it), so this suite mostly tests what a code-level fault looks like in its
  first seconds, when the redeployed service's restart dominates the metrics, not the errors that follow.
- One live run per case, as on RE2. The benchmark is public; the learned-prior check finds nothing (the agent's
  wrong first suspects name another case's root cause once in 14), but with 2 to 4 root causes per system it says
  little here.

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

# 4. Run Sherlog: no AI, then live (a key in .env; cases in the --seed order, default 2026; the budget stops
#    new cases before the cap)
for m in zscore robust graph; do
  uv run --with-requirements requirements.txt python scripts/benchmark/run.py $W/converted --set test --method $m
done
uv run --env-file .env --with-requirements requirements.txt python scripts/benchmark/run.py $W/converted \
    --set test --method auto --budget-usd 9 --workers 4 --full-dir $W/full

# 5. The published baselines, with the benchmark's runner
python -I scripts/benchmark/baselines.py <benchmark source>/main.py $W/data $W/tasks $W/baselines \
    baro nsigma mmbaro mmnsigma e_diagnosis circa:re2-ob,re2-ss nsigma/10 nsigma/2

# 6. Score with the benchmark's evaluator, then recompute and cross-check everything
python -I scripts/benchmark/score.py $W
uv run --with-requirements requirements.txt python scripts/benchmark/report.py
```

RE3 takes the same steps with the suite named, from the Parquet copy alone (about 330 MB); its layout step also
needs `drain3==0.9.11` in the benchmark's environment. Its live run had a $5 cap for dev and test together.

```sh
python -I scripts/benchmark/fetch.py parquet $DATASET_ID $W/data/parquet 're3*/metrics.parquet' \
    're3*/inject_time.txt' 're3*/logs.parquet' 're3*/traces.parquet'
python -I scripts/benchmark/prepare.py $W/data/RE3 $W/data/parquet $W/tasks re3-ob re3-ss re3-tt
python scripts/benchmark/split.py re3
for d in re3-ob re3-ss re3-tt; do python -I scripts/benchmark/convert.py $W/tasks/$d $W/converted-re3; done
for m in zscore robust graph; do
  uv run --with-requirements requirements.txt python scripts/benchmark/run.py $W/converted-re3 --suite re3 \
      --set test --method $m
done
uv run --env-file .env --with-requirements requirements.txt python scripts/benchmark/run.py $W/converted-re3 \
    --suite re3 --set test --method auto --budget-usd 4.6 --workers 4 --full-dir $W/full-re3
python -I scripts/benchmark/baselines.py <benchmark source>/main.py $W/data $W/tasks $W/baselines --suite re3 \
    baro nsigma mmbaro mmnsigma e_diagnosis circa:re3-ob,re3-ss nsigma/10 nsigma/2
python -I scripts/benchmark/score.py $W re3
uv run --with-requirements requirements.txt python scripts/benchmark/report.py
```

`report.py` alone (the last line) reproduces `results.json`, both suites, from the committed records, with no
data, no key and no network.
