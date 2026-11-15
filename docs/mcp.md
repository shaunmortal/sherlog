# Use Sherlog from Claude: the MCP connector

Sherlog serves the Model Context Protocol (MCP) at `/mcp`, so Claude, or any other MCP client, can investigate
an outage itself and have Sherlog's code check the answer, read the recorded investigations, run the no-AI triage on
telemetry and quote the benchmark with its caveats. Every tool is free and read-only: none calls Claude and none
needs a key. Sherlog's own agent runs live only in the [console](https://sherlog-jade.vercel.app/console), where it
is rate limited.

## Connect

**Claude Code**

```sh
claude mcp add --transport http sherlog https://sherlog-jade.vercel.app/mcp
```

Then type `/mcp__sherlog__investigate_outage 41`, or ask, for example, *"Use sherlog: what did the agent conclude
in recorded outage 41, and did the rollback prove it?"* or *"Summarize Sherlog's benchmark, caveats included."*

**Any other MCP client** that supports remote servers over Streamable HTTP: give it the URL
`https://sherlog-jade.vercel.app/mcp`. There is no authentication. For a local server
(`uvicorn app:app`), the URL is `http://127.0.0.1:8000/mcp`.

**From code**, with the official MCP Python SDK (`pip install mcp`):

```python
import asyncio
from mcp import Client

async def main():
    async with Client("https://sherlog-jade.vercel.app/mcp") as client:
        print([tool.name for tool in (await client.list_tools()).tools])
        result = await client.call_tool("get_recorded_verdict", {"number": 41})
        print(result.structured_content["verdict"]["answer"])  # cache

asyncio.run(main())
```

**With curl**, one JSON-RPC message per POST, no handshake needed:

```sh
curl -s https://sherlog-jade.vercel.app/mcp -H 'content-type: application/json' \
     -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "get_recorded_verdict", "arguments": {"number": 41}}}'
```

## Investigate an outage with your own Claude

Sherlog's own agent proposes and plain code checks. The connector puts the same gate around any model you bring:
your Claude investigates with the agent's read-only tools, then `check_answer` re-runs every call it cites and says
whether the answer ships or goes to a human. It is free: no key, and no AI runs on the server.

1. Connect: `claude mcp add --transport http sherlog https://sherlog-jade.vercel.app/mcp`
2. In Claude Code, start the prompt with `/mcp__sherlog__investigate_outage 41` (or `shop`, `custom` for a sample;
   with no argument it is 41). It hands Claude the protocol: start with `top_anomalies`, dig in with `get_metric`,
   `get_logs` and `get_dependencies`, cite the call ids, call `check_answer` before answering, respect the 80% rule.
   Or just ask: *"Use sherlog to investigate recorded outage 41, and check your answer with check_answer."* The
   server's instructions carry the same steps.
3. Afterwards, `reveal_truth` gives the real cause and what Sherlog's own agent answered, so you can score your model.

Every tool takes the same `incident`: `{"recorded": 41}` (a recorded outage, its cause hidden), `{"sample": "shop"}`
or `{"sample": "custom"}`, or `{"document": ...}` (your own sherlog-incident/1 telemetry, checked as `POST
/api/triage` checks it). Nothing is stored between calls: a result's call id, such as `c7016c10ec0`, is a hash of the
incident, the tool and its arguments, so `check_answer` re-runs each cited call from its id alone. The text of each
result is exactly what Sherlog's own agent reads; only the id differs.

`check_answer` applies the agent's own checks: a suspect is verified only if a call it cites shows it abnormal; the
answer goes out only when the first suspect is verified and at least 80% likely; then the blast radius asks whether
it explains every abnormal service, and the rollback replay runs the same outage with it rolled back. Recorded
outages and the samples are simulated, so they get the replay; uploaded telemetry does not, and the verdict says so.

A scripted run of #41 with the official MCP Python client, [`scripts/investigate_over_mcp.py`](../scripts/investigate_over_mcp.py)
(`uv run --with mcp python scripts/investigate_over_mcp.py`), abridged:

```text
connected: protocol 2026-07-28
prompt investigate_outage(41) -> incident {'recorded': 41}
top_anomalies() -> c7016c10ec0
    cart       latency_ms  z=+200.1   normal 11.97 -> now 412.6
    cache      latency_ms  z=+200.0   normal 2.05 -> now 402.1
get_dependencies(cart) -> c8bcbae0903
    calls (downstream): cache (latency_ms z=+200.0)
get_metric(cache, latency_ms) -> c737c8cbeeb
    normal 2.05 ± 2; now 402.1; z over the alarm window +200.0; abnormal since t-10s
check_answer(cache, 90%) -> ship
    citations: c7016c10ec0, c737c8cbeeb, cfbbf5edf28 show cache abnormal
    rollback: alarm after = False, proves = True
check_answer(cart, 90%) -> hand to a human
    citations: c7016c10ec0, c8bcbae0903 show cart abnormal
    reasons: ['cache would explain more of the outage than cart', 'rolling back cart does not stop the alarm']
reveal_truth(41) -> cause cache (latency); Sherlog's own agent answered cache; the z-score ranking's first pick was cart
```

`cart` is the loudest service and its citations check out, since a victim is abnormal too; the blast radius and the
rollback are what catch it.

### Tried with a real model

On the live server, Claude Haiku 5.5, given only the `investigate_outage` prompt and the tool list in a plain tool-use loop ([`scripts/claude_over_mcp.py`](../scripts/claude_over_mcp.py)), made 11 read-only calls on recorded outage #41, named `cache` with 90% confidence, and called `check_answer`: its citations verified, the blast radius explained all four abnormal services, the rollback replay stopped the alarm, and the decision was **ship**. `reveal_truth` agreed: the cause was `cache`, which the z-score ranking had put second behind `cart`. The whole run cost $0.0067.

## Tools

| Tool | Arguments | Answers with |
|---|---|---|
| `list_recorded_outages` | none | Every recorded investigation that replays for free: `id`, `hard` (decoy mode), `caught` (a confident wrong answer the rollback replay caught) and `model`. Numbered, not named, so the culprit stays hidden. |
| `get_recorded_verdict` | `number` (1 to 183) | One investigation: the agent's tool calls with the text each returned, its verdict (suspects, confidence, cited call ids and whether plain code verified each citation, the answer or the hand-over, a ticket note and a customer update), the blast-radius check, the rollback replay, and the truth, including what the no-AI z-score ranking said. |
| `triage_incident` | `incident`: a [sherlog-incident/1 document](bring-your-own.md#the-format) | The no-AI checks, as `POST /api/triage` runs them: the alarm tick, the z-score and robust z-score rankings, and the blast-radius check of the z-score ranking's top suspect. The same checks and error messages (with each path under `incident`) and the same 2 MB cap. |
| `sample_incident` | `graph`: `shop` (default) or `custom` | A sample document, culprit removed, as `GET /api/sample-incident` gives it: the shop's 11 services (about 42 KB of JSON) or six renamed services with their own call graph (about 27 KB). |
| `top_anomalies` | `incident` | The strongest deviations right now, as the agent's first look shows them. Like every investigation tool, it answers `call_id` and `text`, the text starting with `[call_id]`. |
| `get_metric` | `incident`, `service`, `metric` | One metric of one service: its normal level, current value, z-score over the alarm window, how long it has been abnormal and its recent values. |
| `get_logs` | `incident`, `service` | Recent WARN and ERROR lines written by one service, newest last. |
| `get_dependencies` | `incident`, `service` | The services it calls and is called by, with the health of each. |
| `check_answer` | `incident`, `suspects` (up to 5, each `service`, `confidence`, `evidence`: up to 20 call ids, and optional `fault_type` and `reason`) | Each suspect verified or rejected with the reason (`verify_note`), the `answer` or the `escalation`, the answer's `blast` radius and `rollback` replay, the `decision` (`ship` or `hand to a human`) and its `reasons`, and for an upload a `note` that it has no rollback replay. |
| `reveal_truth` | `recorded` | For a recorded outage: the real cause, the fault, the decoy in hard mode, what Sherlog's own recorded agent answered and whether it was right, and the z-score ranking's first three. |
| `benchmark_summary` | none | The headline numbers. First the 252 real outages of a public benchmark, from [`runs/real-benchmark/results.json`](../runs/real-benchmark/results.json): each method's AC@1 (with its interval), AC@3 and Avg@5, the auto route against the published baselines and its own no-AI rule case by case, and its cost. Then, from [`runs/evidence.json`](../runs/evidence.json), the 132 normal and 44 hard-mode simulated outages: for each method, how often it named the cause first, answered alone and answered wrong, its median cost, what the rollback caught, and how it compares with the no-AI call-graph rule outage by outage; the no-AI baselines; and the caveats. |

Each tool's `inputSchema` is generated from the same models the server validates with, so it lists every field and
limit. Every answer comes twice, as the spec suggests: as JSON text in `content` and as `structuredContent`. A bad
argument is a tool error (`isError: true`) whose text names the field, so the model can correct it:
`Bad input. incident.series.cart.cpu_pct: Value error, tick 3 is 140, but cpu_pct must be between 0 and 100`.

### Example: `get_recorded_verdict`

Call:

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
 "params": {"name": "get_recorded_verdict", "arguments": {"number": 41}}}
```

Answer (`structuredContent`, trimmed to one tool call, one suspect and no ticket note):

```json
{
  "id": 41, "hard": false, "caught": false, "model": "claude-opus-5-5",
  "tool_calls": [
    {"id": "c3", "name": "get_metric", "args": {"service": "cache", "metric": "latency_ms"},
     "text": "[c3] get_metric(cache, latency_ms)\nnormal 2.05 ± 2; now 402.1; z over the alarm window +200.0; abnormal since t-10s\n..."}
  ],
  "verdict": {
    "causes": [{"service": "cache", "fault_type": "latency", "confidence": 0.9, "evidence": ["c0", "c1", "c3"],
                "verified": true, "verify_note": "c0, c1, c3 show cache abnormal",
                "reason": "Cache latency jumped from ~2 ms to 402 ms at t-10s, and cache calls nothing downstream, so no dependency explains it."}],
    "answer": "cache", "escalation": null,
    "blast": {"service": "cache", "abnormal": ["cart", "cache", "frontend", "checkout"], "unexplained": [], "better": null},
    "rollback": {"service": "cache", "alarm_after": false, "still_abnormal": [],
                 "cleared": ["cart", "cache", "frontend", "checkout"], "proves": true}
  },
  "truth": {"root": "cache", "fault": "latency", "seed": 1040, "ai_answer": "cache", "ai_top1": true,
            "baseline": ["cart", "cache", "frontend"], "baseline_top1": false}
}
```

The z-score ranking blamed `cart`, a victim; the agent named `cache`, its citations verified, and rolling `cache`
back cleared all four broken services. For a wrong answer the rollback caught, try the `caught` outages that
`list_recorded_outages` lists last.

### Example: `benchmark_summary`

Answer (trimmed to the real outages' auto route and hard mode's Opus agent and no-AI baselines):

```json
{
  "real_outages": {
    "cases": 252,
    "methods": {"auto": {"label": "Sherlog agent, auto route (Claude Haiku 5.5, Claude Opus 5.5 when needed)",
                         "AC@1": 0.9246, "AC@1_ci95": [0.885, 0.951], "AC@3": 0.9563, "Avg@5": 0.9508}},
    "auto_vs": {"nsigma": {"n": 252, "only_first_right": 40, "only_second_right": 9, "mcnemar_p": 9.26e-06}},
    "agent": {"cases": 252, "answered": 214, "cost_usd_median": 0.00242}
  },
  "hard": {
    "outages": 44, "seeds": "2000-2043",
    "no_ai": {"zscore_cause_first": 0.659, "robust_zscore_cause_first": 0.568,
              "call_graph_rule_cause_first": 0.909, "call_graph_rule_wrong": 4},
    "methods": {"agent": {"label": "Sherlog agent on Claude Opus 5.5", "cause_first": 0.977, "answered_alone": 29,
                          "wrong_answers": 0, "median_cost_usd": 0.056, "rollback_caught_wrong": 0,
                          "vs_call_graph_rule": {"only_method_right": 3, "only_rule_right": 0, "mcnemar_p": 0.25}}}
  },
  "caveats": ["Real outages: one benchmark suite of injected faults ...",
              "The controlled benchmark's telemetry is simulated: a shop this project wrote. ...", "..."]
}
```

## Protocol

Written by hand on FastAPI, with no MCP library on the server ([`mcp_server.py`](../mcp_server.py)). Stateless:
every POST stands on its own, no session id is issued, and every answer is one `application/json` object (no
server-sent events).

- **Two eras on one endpoint.** A POST whose `MCP-Protocol-Version` header is `2026-07-28` is served as that
  revision: the request carries its version and capabilities in `params._meta`, the `MCP-Protocol-Version`,
  `Mcp-Method` and (for `tools/call`) `Mcp-Name` headers must match the body, `server/discover` describes the
  server, and results carry `resultType`, `ttlMs` and `cacheScope` where the revision asks for them. Any other POST
  is the handshake era: `initialize` agrees `2025-11-25`, `2025-06-18` or `2025-03-26` (an unknown version is
  offered `2025-11-25`), then `notifications/initialized`, `ping`, `tools/list` and `tools/call`.
- **Answers.** A request gets 200 with its result. A notification gets 202 with no body. In the 2026-07-28 era an
  error gets the status its spec gives it: 400 for a parse error, an invalid request, invalid params, a header that
  does not match the body (`-32020`) or an unsupported version (`-32022`, listing the versions served); 404 for an
  unknown method (`-32601`). In the handshake era an error a well-formed request earned is 200 with the JSON-RPC
  error, as servers of that era answer.
- **Limits.** Bodies over 2 MB get 413, as on the upload routes. `GET` and `DELETE` get 405 with `Allow: POST`.
  A request from a browser page on another site (an `Origin` that is neither this server nor this machine) gets
  403, the check the spec asks for against DNS rebinding. Batches are refused.

**Checked with** the official MCP Python client, version 2.3.0 in both eras and 1.30.0 in the handshake era,
against a local server: it lists the tools and the prompt, calls them, and runs the whole scripted investigation
above. `tests/test_mcp.py` and `tests/test_mcp_investigate.py` cover the rest, among them a test that no tool but
`get_recorded_verdict` and `reveal_truth` names a recorded outage's cause, fault, seed or decoy, and that every
investigation tool answers the same about a recorded outage as about its telemetry uploaded with none of those.

## What it does not do

- Sherlog's own agent does not run here: it costs API credits, so it runs only in the console, rate limited.
  Over MCP, the model is yours.
- No resources or subscriptions; one prompt. The tool and prompt lists never change while a deploy runs.
- Being stateless, the server cannot tell whether a model made a call it cites, only that the call exists on this
  incident and shows what is claimed: an id is a public hash, so a model could compute one without calling. The
  evidence is re-checked either way.
- `check_answer`'s rollback replay is a real intervention, so a model that calls it on every service in turn will
  find the cause, as rolling back every service in production would. Score models on their first `check_answer`.
- `triage_incident` takes the whole document as an argument. A model writing out 11 services' 10 minutes of
  numbers is slow and costly (the shop sample is about 42 KB of JSON), so for real exports, `POST` the file to
  `/api/triage` instead; the tool suits small incidents and documents a script builds.
- The recorded outages and the controlled benchmark come from the simulated shop, and only simulated outages get
  the rollback replay; `benchmark_summary` says so in its caveats.
