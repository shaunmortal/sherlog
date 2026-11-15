# Submission kit (for the team, not part of the app)

Paste these sections into the submission form. Replace `VIDEO_URL` once the video is up.
Tracks to select: **Best AI Hack** (main), **Best Deployed / Shipped App**, **Best Design & UX**.

---

## Tagline (≤ 200 chars)

An AI on-call agent that must prove its answer: on 252 real outages it ranked the root cause first in 92%, against
80-81% for the best published methods. Break a service and watch it work.

## Inspiration

When one service in a modern app breaks, every service that calls it breaks too. The on-call
engineer gets a dashboard full of red at 3 a.m. and has to work out which service is the cause and
which are just victims. AI can help, but an AI that confidently blames the wrong service is worse
than none. We wanted an investigator that has to *prove* its answer, with every tool call it cites
checked by plain code, and we turned it into something anyone can play.

## What it does

**On 252 real outages of a public benchmark**, three real microservice systems (12, 15 and 68 services) each broken
on purpose by a known fault, Sherlog ranked the root cause first in **92%** (95% CI 88-95%), against 80% and 81% for
the best published methods (n-sigma and its multi-source version) re-run on the same cases with the benchmark's own
runner, and 83% for Sherlog's own call-graph rule on the same inputs without the AI. Case by case, it was right where
n-sigma was wrong in 40 outages, and the reverse in 9 (exact McNemar test, p = 9.3e-6), for a median $0.0024 an
outage. Every number is recomputed from the committed run records with no key by `scripts/benchmark/report.py`, and
scored by the benchmark's own evaluator; the protocol is in `docs/real-benchmark.md`.

Data: RCAEval benchmark, MIT licence, <https://huggingface.co/datasets/phamquiluan/RCAEval>

**Use it from your own Claude.** Sherlog is also an MCP server (`claude mcp add --transport http sherlog
https://sherlog-jade.vercel.app/mcp`): your model investigates an outage with Sherlog's own read-only tools, then
Sherlog's plain code re-runs every cited call, applies the citation check, the 80% rule, the blast radius and the
rollback replay, and says ship or hand to a human. Free, no key, no AI call on the server.

To play with, Sherlog runs a simulated online shop of 11 microservices. You pick a service and a fault (network
latency, a bad deploy that throws errors, CPU starvation, a memory leak), choose who investigates and
how hard it is, and break it.

- The telemetry scrolls live; an alarm fires and the charts turn red, on the cause *and* its victims.
- You lock in your guess on the service map or a telemetry row.
- A Claude agent investigates with read-only tools (metrics, logs, the call graph), and you watch
  every tool call and its reasoning stream into the flight log.
- It ranks up to five suspects, each citing the tool calls that support it.
- **Answers must prove themselves.** A deterministic verifier checks every citation. The agent answers
  only if its #1 suspect's citations are verified and it is at least 80% confident; otherwise it escalates
  to a human. A victim is abnormal too, so its citations pass: the next two checks tell cause from victim.
- **Proof by intervention.** Sherlog replays the same outage, with the same noise, and the answer rolled
  back. Roll back the cause and the outage stops; roll back a victim or a decoy and it carries on, so a
  decoy cannot fool it. This works because the shop is simulated, where rolling back the true cause is
  exactly what clears the fault; in production it would be a canary rollback, which is noisier.
- A **blast-radius check** asks whether the answer explains every broken service through the call graph.
- **Who investigates:** Claude Haiku 5.5, Sonnet 5.5, Opus 5.5, or **auto**: Haiku first, and a fresh
  Opus investigation only when Haiku's answer doesn't hold up.
- **Hard mode** adds a decoy: an unrelated service with a harmless anomaly at the same moment.
- It drafts a ticket note for engineers and a plain-language update for customers.
- The scoreboard reveals the culprit (and the decoy): you vs the agent vs the no-AI z-score ranking.
- **Bring your own outage, from any system:** upload 10 minutes of real telemetry with your own call
  graph (2 to 80 services under their own names), or mapped onto the shop's 11. Triage runs the alarm, the
  z-score and robust z-score rankings and the blast-radius check on your services with no AI and no key; the
  agent can investigate it too, with its tools and prompt set to your services. Uploads get no rollback test.
- A **status page** and a health endpoint show whether live mode is on, and the 183 recorded
  investigations replay for free whenever it is not: the Opus agent's 176 and the 7 wrong answers the
  rollback caught.

**Who it is for.** The on-call engineer facing a wall of red, who needs the one service that broke and the
tool calls that show it, or a clear hand-over. Teams deciding whether to trust an AI on-call agent, who can
reuse the verify-then-rollback gate and the benchmark protocol. People practising triage, through the guess game.

**Results on real outages.** On 252 held-out real outages, the auto route (Claude Haiku first, Claude Opus when
Haiku's answer does not stand) ranked the root cause first in 92%, in the top 3 in 96%. The published baselines,
re-run on the same cases: multi-source n-sigma 81%, n-sigma 80%, BARO 29%. Without the AI, Sherlog's call-graph rule
gets 83% on the same inputs; the agent was right where the rule was wrong in 26 outages, and the reverse in 2
(p = 3.0e-6). It answered 214 of the 252 on its own, 208 of them right, and handed the other 38 to a person; Opus
took over from Haiku in 63. The dev/test split was frozen before the first live call.

**Results on the controlled benchmark** (a simulated shop, which adds a decoy mode and the rollback replay). On 132
outages the agent named the true cause first in every one (100%, 95%
interval 97-100%), against 83% and 78% for two standard no-AI rankings (McNemar p < 0.001 against the
z-score ranking). A stronger no-AI control comes close: a call-graph rule, which names the abnormal service
that explains the most of the others, gets 131 of 132 (99%), and the agent's one extra outage is not a
significant difference (p = 1.0). Applied to the recorded Haiku and Opus runs, the auto route got the same
100% for a median $0.0015 per outage, needing Opus in only 4 of 132. In hard mode (44 outages with a decoy),
the z-score ranking fell to 66% and the call-graph rule to 91%; Haiku and Sonnet were fooled by the decoy a
few times, while Opus named the cause first in 43 of 44 (p = 0.25 against the rule) and never gave a wrong
answer. What the AI adds is knowing when it does not know: the rule always answers, so it was wrong in 4
hard outages, while Opus answered 29 on its own, all right, and handed the other 15 to a human with a
ticket note. Every answer also comes with reasoning and citations that code checks. Applied offline to every
recorded answer, the rollback replay confirmed all 498 normal-mode answers of the one-prompt and three agent
runs, and would have caught all 7 of their wrong hard-mode answers, rejecting none of the 83 right ones; the
7 replay in the console. In the simulator, rolling back the true cause is exactly what clears the fault, so
the rollback numbers show how the gate behaves, not field accuracy; in production the equivalent is a canary
rollback, which is noisier. As a what-if, gating on the rollback instead of confidence would have let
Opus answer 42 of 44 hard outages instead of 29, still never wrong; the same gate on the no-AI call-graph
rule's pick would answer 39, also never wrong, so the gate itself needs no AI.

## How we built it

- **Simulator (Python):** a seeded model of the shop's call graph. Latency and errors flow upward to
  callers, callers log about the callee they're waiting on, and there are red-herring log lines.
  Telemetry is noisy the way production is: dense jitter, flat lines with tall spikes, slow drift,
  sparse samples on quiet services, error counts, garbage-collection sawtooths. Hard mode adds a
  decoy and cuts caller logging by 60%. Every outage is reproducible from its seed, and can be
  re-run with any service rolled back.
- **Alarm:** a plain z-score detector (4σ for 15 s), deliberately not AI, so we can measure what the
  AI adds.
- **Agent:** Claude Haiku 5.5, Sonnet 5.5 or Opus 5.5 via the Anthropic Messages API with tool use:
  strict tool schemas, adaptive thinking (summaries are streamed into the UI), server-side fallbacks
  on Opus, in an 8-round loop. The auto route is a model cascade gated by the verifier.
- **Verifier:** every tool call records which services its result showed abnormal; a suspect's citations
  are verified only if a call it cites really showed it abnormal. The blast-radius check uses the call
  graph and knows that only slowness and failures travel to callers. The rollback check replays the
  outage with the answer rolled back and asks whether the alarm, or every latency and error alarm, stops.
- **Benchmark:** 132 normal and 44 hard outages on seeds never used during development; three no-AI
  baselines (z-score, robust median/MAD, and a call-graph rule that names the abnormal service explaining the
  most of the others), one-prompt Claude, the agent on three models, the auto
  route, an agent without logs; 95% Wilson intervals and an exact McNemar test; the rollback check and
  the rollback-as-gate what-if recomputed offline from the recorded runs.
- **Story page:** one recorded outage told as a scroll story: the shop's call graph in 3D (three.js) with
  requests flowing and the failure travelling up to the callers, then the alarm, the investigation, the
  verifier, the verdict and the rollback, with the benchmark filling in as you scroll; every number comes
  from the API.
- **Console:** FastAPI streaming server-sent events to a page with no framework: Live, Replay, Evidence
  and Your data views, canvas telemetry with a hover readout, an SVG service map, and replays of all
  183 recorded investigations (no API cost), the 7 wrong answers the rollback caught among them.
- **Accessibility:** built to WCAG 2.2 AA: skip links, radio groups and tabs with arrow keys, guessing
  by keyboard, a live region that announces each step, AA contrast in light and dark, a pause control and
  reduced motion, and **Skip to verdict** during a replay. axe-core 4.10.2 finds 0 violations on the story page,
  the console and the status page, in both themes, at desktop and phone widths, and Lighthouse scores accessibility
  100 on the same three pages.
- **Reliability:** a status page and `/api/health`, a 120 s timeout on every model call, a clear reason
  for every failure, and per-visitor and daily limits on live runs. Every page and the health check answer
  `HEAD` for uptime monitors, a workflow is scheduled every 30 minutes to check the live health endpoint, `/reference`
  lists every route (Swagger UI stays at `/docs`), every response carries a strict content security policy, `requirements.txt` pins the exact versions the tests pass with, and all three pages
  carry a link preview image.
- **Real outages:** a converter from the public benchmark's cases to Sherlog's upload format, a dev/test split
  frozen before any live call, the published baselines re-run with the benchmark's own runner, scoring by its own
  evaluator, Wilson intervals and exact McNemar tests, and checks of n-sigma's window and of a learned prior.
- **Tests:** 1,744 pytest tests in CI, 1,285 of which re-run every recorded run from its seed and compare its tool
  results exactly (the agent loop and the auto route against a scripted fake model, the
  call-graph rule, every API route and failure path, `HEAD` answers, the caught replays, the upload format,
  uploads with their own call graph, the MCP connector, and a guard that the shop's prompt and tool schemas never
  change),
  plus 49 browser end-to-end checks that run in CI against the app started on the runner (security policy,
  404 page, layout shift, axe-core on five pages in both themes).
- **AI tools used:** Claude Code as a pair programmer; Claude Haiku, Sonnet and Opus 5.5 inside the product.
  Claude Code wrote and tested code under our direction, several features at once in separate agent
  worktrees joined by the `merge:` commits. The verifier, the blast-radius check, the call-graph rule and the
  rollback check are plain code, with no AI.
- **Timeline** (IST, from file timestamps and `git log`; the first public push came after the first working
  version, so the history starts on Nov 14):
  - Nov 14, 04:21: the project and its Python environment are created; first test run at 04:24.
  - Nov 14, 09:03-09:24: the normal and then the hard-mode benchmarks are recorded (`runs/*.jsonl`).
  - Nov 14, 17:42: first commit, pushed within a minute: the simulator, agent, verifier, benchmark, console,
    story page, nine recorded run files and core tests. The deploy was linked at 17:43.
  - Nov 15, 00:43-02:45: the story page's ribbon; bring your own outage; the status page and health check;
    API tests and CI; the rollback replay and its what-if; WCAG 2.2 AA on the console and story page.
  - Nov 15, 03:16-04:58: docs led by what is new; fixes to triage, upload errors, focus and touch targets.
  - Nov 15, 05:54-06:19: the call-graph rule, caught-answer replays, `HEAD`, `/docs`, pinned dependencies,
    the link preview and uptime check; the console's first screen leads with replay #41 and hard mode.
  - Nov 15, 08:56-10:35: uploads with their own call graph, tests that re-run every recorded run, the MCP
    connector and `llms.txt`.
  - Nov 15, 10:47-12:31: the public benchmark of real outages, the split frozen at 11:09 before any live call.
  - Nov 15, 12:40-13:45: the real-outage result leads the README, the story page and the console's evidence.

## Challenges we ran into

- **Making the problem realistic.** If every service had the same relative noise, the broken
  service would almost always show the biggest deviation and a simple ranking would be enough. Real
  systems aren't like that: a quiet victim can look more abnormal than a noisy cause. So each
  service gets its own kind and amount of noise, and the no-AI baseline fails the way it does in
  practice.
- **Realistic noise raises false alarms.** Our first noisy telemetry set the alarm off far too often in
  perfectly healthy shops: a fast cache with big jitter got clipped at zero, and a held sample on a
  quiet service looked like a sustained spike. Making latency noise proportional to latency, never
  allowing two spikes in a row, and drawing straight lines between sparse samples brought it down to
  6 in 1,500 healthy shops (0.4%).
- **Every model scored 100%.** On normal outages Haiku, Sonnet and Opus were all perfect, which told
  us nothing about which to use. Hard mode, with a decoy, finally separated them: the cheap models
  were sometimes confidently wrong, which is exactly what a cascade has to guard against.
- **A check that only looks can be fooled.** Our first blast-radius check couldn't spot a decoy: a decoy
  that is a callee of the real cause "explains" everything through the call graph. Teaching it that a
  busy CPU slows nobody down helped, but when the real fault doesn't spread, a decoy still looks just as
  plausible on paper. Rolling the answer back and replaying the outage settles it, because only the
  real cause ends the outage.
- **Trusting the AI.** A ranking with no evidence isn't actionable. We designed the citation format
  (every tool result carries an id like `c3`) so plain code can check every citation, with no second AI.
- **Fair evaluation.** We kept benchmark seeds separate from development seeds, added a second no-AI
  baseline, and report intervals and a significance test, not just a headline score. The z-score ranking
  still turned out to be an easy target, so we added a third, a call-graph rule, which comes within a point
  of the agent on normal outages.

## Accomplishments that we're proud of

- 92% of 252 real outages ranked first, ahead of the best published methods on the same cases (80-81%), for a
  median $0.0024 an outage, with every number recomputable from the committed runs.
- An agent that shows its work: every claim links to a tool call you can open, and plain code checks each citation.
- A check a decoy cannot fool in the simulator: over the recorded runs, the rollback replay caught every
  wrong hard-mode answer and rejected no right one, and those 7 wrong answers replay in the console.
- Opus-level accuracy for $0.0015 an outage on normal outages, by letting Haiku go first and checking its answer.
- A benchmark with three no-AI baselines, three models, ablations, a hard mode, intervals and a significance
  test: one command recomputes every table from the recorded runs, and 183 recorded investigations replay
  in the browser.
- Turning incident response into a game anyone can play, even without knowing what a microservice is,
  built to WCAG 2.2 AA, with 0 axe-core violations.

## What we learned

- Investigation is what makes an AI answer usable: one prompt was as accurate on easy outages, but
  confident enough to answer only 107 of 132, and 4 of 44 hard ones.
- A cheap model is fine until it is confidently wrong; a cascade needs a check that doesn't trust the
  model's own confidence. Haiku's and Sonnet's wrong hard-mode answers were all confident, and the
  rollback replay caught every one.
- Verification doesn't need another LLM; often a few lines of deterministic code are enough.
- A good baseline, and a harder test when everything scores 100%, is what makes an AI result mean
  something. A no-AI call-graph rule named the cause first nearly as often as the agent; the AI's clearer
  gain was knowing when to hand over, with reasoning and citations a person can check.

## What's next for Sherlog

- Real outages with code-level faults, the public benchmark's next suite.
- More real-world confusion: two faults at once, flapping faults, retry storms.
- Plug into real systems: alerts in directly instead of an exported window, and a summary out to the
  team's chat.

## Built with

python, fastapi, anthropic-api, claude-opus-5.5, claude-sonnet-5.5, claude-haiku-5.5, claude-code,
model-context-protocol, fireworks-ai, glm-5.3, gpt-oss-120b, javascript, canvas, svg, three.js, server-sent-events, pytest, playwright, axe-core, uv, vercel

## Links

- Live demo: https://sherlog-jade.vercel.app (console: https://sherlog-jade.vercel.app/console,
  status: https://sherlog-jade.vercel.app/status)
- Code: https://github.com/shaunmortal/sherlog
- Video: VIDEO_URL

**Evaluate it in 5 minutes** (no key needed):

1. The video (2:48), then the real outages: https://github.com/shaunmortal/sherlog/blob/main/docs/real-benchmark.md
2. https://sherlog-jade.vercel.app/console#replay/41 : the z-score ranking blames cart; the agent names cache,
   citing the tool calls that show it, and rolling cache back clears all four broken services.
3. https://sherlog-jade.vercel.app/console#replay/177 (C1): a confident wrong answer the rollback stopped.
   The Haiku agent names the decoy, 85% sure, with its citations verified; rolling it back leaves the alarm on.
4. Console, Your data, **Use** the shop sample, **Triage (no AI)**: the alarm, the z-score and robust z-score
   rankings and the blast-radius check. Then **Use** a custom-graph sample and **Triage** again: an upload
   with its own call graph of six services replaces the shop on the charts and the map.
5. In a clone: `pytest -q` (1,744 passed, 1 skipped), `python bench.py --report`, which recomputes every
   controlled-benchmark table from the recorded runs with no API calls, and `python scripts/benchmark/report.py`,
   which recomputes every real-outage number.
