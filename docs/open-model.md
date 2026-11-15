# Open-weights models behind the same gate

**The question.** Why a paid Claude model, and not a free or open one? Instead of arguing, we measured it: two
open-weights models ran the same benchmark behind exactly the same gate. That means the same system prompt, the same
read-only tools and schemas, the same citation verifier, the same 80% confidence rule, and afterwards the same
blast-radius check and rollback replay, on the same seeds. Two things matter. Does the gate stop a cheaper model
from shipping wrong answers? And how much accuracy and coverage does that model lose?

**The answer, in numbers.** Behind the gate, **GLM-5.3** (open weights, 743B parameters) gave **0 wrong answers in
176 outages**. With no gate, shipping its first suspect every time, it would have shipped 3. The smaller
**gpt-oss-120b** gave **6 wrong answers** behind the gate, all in hard mode, and would have shipped 8 with no gate.
All 6 named the decoy at 90–97% confidence, with citations that really do show the decoy abnormal, so neither the
citation check nor the 80% rule could stop them. The rollback replay would have caught all 6. On cost, GLM-5.3 was a
third cheaper than the Opus agent per normal outage but dearer per hard one, and gpt-oss-120b cost a little more
than the Haiku agent in both modes.

All 352 runs finished with no failed run, and no tool call ever had malformed JSON. Recorded spend at list price:
$8.27.

## Results

"Cause first" means the true cause was ranked first *and* its citations were verified. Brackets are 95% Wilson
intervals. Every figure in the tables is in [`runs/evidence.json`](../runs/evidence.json) or printed by
`python bench.py --report`. The rest (confidences, tool-call counts, token counts, totals) is read straight from the
run files in [`runs/`](../runs).

### Hard mode: 44 outages with a decoy

| Method | Cause first | Answered alone · to a human | Wrong answers that passed the gate | …that the rollback replay catches | Wrong with no gate at all | Median cost per outage |
|---|---:|---:|---:|---:|---:|---:|
| No AI: call-graph rule | 91% [79–96] | always | 4 | – | 4 | – |
| Agent on Claude Haiku 5.5 | 86% [73–94] | 33 · 11 | 5 | 5 | 6 | $0.0023 |
| Agent on Claude Sonnet 5.5 | 86% [73–94] | 24 · 20 | 2 | 2 | 6 | $0.016 |
| Agent on Claude Opus 5.5 | 98% [88–100] | 29 · 15 | 0 | – | 1 | $0.056 |
| Auto: Haiku first, Opus if needed | 91% [79–96] | 34 · 10 | 4 | 4 | – | $0.0024 |
| **Agent on GLM-5.3** | 91% [79–96] | 27 · 17 | **0** | – | 3 | $0.071 |
| **Agent on gpt-oss-120b** | 80% [66–89] | 38 · 6 | **6** | 6 | 8 | $0.0027 |

"Wrong with no gate at all" ships suspect #1 of every recorded verdict, whatever its citations and confidence (a run
with no suspect ships nothing). The no-AI rule always ships its pick, so its two columns match. Auto has no single
ungated answer, since it is built from two models' runs.

### Normal: 132 outages

| Method | Cause first | Answered alone · to a human | Wrong answers | Median cost per outage |
|---|---:|---:|---:|---:|
| No AI: call-graph rule | 99% [96–100] | always | 1 | – |
| Agent on Claude Haiku 5.5 | 100% [97–100] | 129 · 3 | 0 | $0.0015 |
| Agent on Claude Sonnet 5.5 | 100% [97–100] | 130 · 2 | 0 | $0.014 |
| Agent on Claude Opus 5.5 | 100% [97–100] | 132 · 0 | 0 | $0.047 |
| Auto: Haiku first, Opus if needed | 100% [97–100] | 132 · 0 | 0 | $0.0015 |
| **Agent on GLM-5.3** | 100% [97–100] | 132 · 0 | 0 | $0.030 |
| **Agent on gpt-oss-120b** | 95% [90–97] | 125 · 7 | 0 | $0.0023 |

Neither open model answered a normal outage wrong, and with no gate neither would have.

### Paired outage by outage

Exact McNemar tests on cause first, over the same outages: the outages where only the open model was right · where
only the other method was right, and the two-sided p-value.

| | vs the Opus agent | vs auto | vs the no-AI call-graph rule |
|---|---:|---:|---:|
| GLM-5.3, normal | 0 · 0, p = 1.0 | 0 · 0, p = 1.0 | 1 · 0, p = 1.0 |
| GLM-5.3, hard | 0 · 3, p = 0.25 | 2 · 2, p = 1.0 | 3 · 3, p = 1.0 |
| gpt-oss-120b, normal | 0 · 7, p = 0.016 | 0 · 7, p = 0.016 | 1 · 7, p = 0.070 |
| gpt-oss-120b, hard | 0 · 8, p = 0.0078 | 0 · 5, p = 0.062 | 2 · 7, p = 0.18 |

GLM-5.3 is indistinguishable from auto and from the call-graph rule on these benchmarks. The Opus agent was right in 3
hard outages where GLM-5.3 was not, never the reverse, which on 44 outages could be chance. gpt-oss-120b is behind
Opus by more than chance in both modes, and never ahead of it.

### What if the rollback were the answer gate? (offline what-if, hard mode)

As in the README: answer whenever rolling back the verified top suspect proves it, whatever its confidence.
Answered · right:

| Method | Today: 80% confidence gate | Rollback gate | Either gate |
|---|---:|---:|---:|
| Agent on Claude Haiku 5.5 | 33 · 28 | 37 · 37 | 42 · 37 |
| Agent on Claude Sonnet 5.5 | 24 · 22 | 37 · 37 | 39 · 37 |
| Agent on Claude Opus 5.5 | 29 · 29 | 42 · 42 | 42 · 42 |
| **Agent on GLM-5.3** | 27 · 27 | 39 · 39 | 40 · 40 |
| **Agent on gpt-oss-120b** | 38 · 32 | 34 · 34 | 41 · 35 |

With the rollback as the gate, neither open model answers wrong in the simulator, just like the Claude models.
gpt-oss-120b drops from 38 answers with 6 wrong to 34, all right. Adding the rollback to today's gate removes none of
its wrong answers, because they were confident. Both open models answered hard seed 2031 right, GLM-5.3 at exactly
80%. That is the one outage where rolling back the true cause leaves the alarm on (README, Limitations), so the
rollback rejects one right answer from each. On normal outages the rollback gate answers 132 of 132 for GLM-5.3 and
125 of 132 for gpt-oss-120b, all right; its other 7 had no suspect to roll back.

### How the open models fail

- **GLM-5.3 holds back.** Its 3 wrong first suspects all named the decoy, at 72%, 78% and 45% confidence, so the
  80% rule escalated each one. It is also thorough: a median of 24 tool calls per hard outage, against the Opus
  agent's 7, and 14 per normal outage against 5. That is why it costs more per outage than its per-token price
  suggests. Once, in hard seed 2000, its last reply's reasoning ran to about 57,800 characters, and the
  `submit_ranking` call that followed carried no readable suspects: most likely cut off at the 16,000-token output
  budget, which the Claude runs have too. The outage went to a human.
- **gpt-oss-120b is sure and wrong.** All 6 wrong hard-mode answers were the decoy at 90%, 90%, 92%, 94%, 96% and
  97% confidence. Separately, 8 of its runs (7 normal, 1 hard) used every one of the loop's 10 model calls without
  calling `submit_ranking`, ignoring both nudges to submit. In one, its last reasoning reads "We need to submit
  final answer. Already submitted." All 8 went to a human.

### Cost

Per million tokens, GLM-5.3 lists at about a third of Opus's input price and under a quarter of its output price.
Per outage it saved a third on normal outages ($0.030 against $0.047 median) but cost more on hard ones ($0.071
against $0.056): more tool calls, more rounds and a median of 9,300 output tokens per hard outage. gpt-oss-120b cost
$0.0023 and $0.0027 per outage, against Haiku's $0.0015 and $0.0023; Haiku's list price ($0.10 and $0.50) is below
it. Totals for the 352 runs, at list price with no cache discount: GLM-5.3 $7.83 ($4.18 normal, $3.65 hard),
gpt-oss-120b $0.45 ($0.32 normal, $0.13 hard).

## Conclusion

- **The gate is not Claude-specific.** The same verifier, 80% rule, blast-radius check and rollback replay ran
  unchanged on two open-weights models, through an adapter that changes only the wire format.
- **With a strong open model, the gate is enough.** GLM-5.3 gave 0 wrong answers in 176 outages. With no gate it would
  have shipped 3. It named the cause first as often as auto and the no-AI call-graph rule (91% in hard mode), and 3
  hard outages fewer than Opus.
- **With a weak or overconfident model, the confidence gate is not enough.** gpt-oss-120b's 6 wrong answers passed
  the citation check and the 80% rule, as Haiku's 5 did. Only the rollback replay catches answers like these, and it
  caught all 6.
- **Why a paid model, then?** Not because an open model cannot do the job: behind this gate GLM-5.3 is a credible
  replacement for the Opus agent. It matched Opus's record of no wrong answers and was a third cheaper per normal
  outage. But it answered fewer hard outages alone (27 against 29), named the cause first in 3 fewer, and cost more
  per hard outage, where the models differ. Auto, the cheap Claude route, cost $0.0024 per hard outage against
  GLM-5.3's $0.071, but gave 4 wrong hard-mode answers that only the rollback catches. gpt-oss-120b costs about what
  Haiku costs and fails the same way, a little more often. So with a strong model, open or not, the choice is about
  price and coverage, not safety. With a weaker one, Claude or open, only the rollback gate keeps the confident
  wrong answers out.

## Limitations

- **One run per outage per model**, as for the Claude runs. A fresh run can answer differently; hence the intervals
  and the paired tests.
- **The prompt and tools were developed with Claude**, on seeds below 1000. They were deliberately not re-tuned for
  the open models, since the question was the same gate. A prompt tuned for them might do better.
- **Settings are not identical.** The models reason by their own defaults (effort could not be mapped onto GLM-5.3
  without switching its reasoning off), there is no strict tool mode, and temperature is each provider's default.
  The output budget (16,000 tokens per reply) and the loop's limit of 10 model calls are the same for every model;
  they cut short 1 GLM-5.3 run and 8 gpt-oss-120b runs.
- **Costs are list prices with no cache discount**, the same rule as for the Claude rows. The provider bills cached
  input for less, so the real bill was lower than the $8.27 recorded.
- **The shop is simulated** (README, Limitations), and in the simulator rolling back the true cause is exactly what
  clears the fault.

## Setup

| | |
|---|---|
| Provider | Fireworks AI, its chat completions API with tools, `https://api.fireworks.ai/inference/v1` |
| Main model | **GLM-5.3** (`accounts/fireworks/models/glm-5p3`): open weights, 743B-parameter mixture of experts, 1,048,576-token context, a reasoning model with function calling |
| Its price | $1.40 per million input tokens ($0.26 cached), $4.40 per million output tokens: the provider's serverless list price on Nov 15, 2026 |
| Smaller model | **gpt-oss-120b** (`accounts/fireworks/models/gpt-oss-120b`): open weights, 116B-parameter mixture of experts, 131,072-token context; $0.15 per million input tokens ($0.015 cached), $0.60 per million output tokens |
| Outages | The benchmark's: 132 normal (seeds 1000–1131) and 44 hard-mode outages with a decoy (seeds 2000–2043), never looked at while the prompt and tools were built |
| Runs | `runs/bench-open.jsonl`, `runs/bench-hard-open.jsonl` (GLM-5.3), `runs/bench-open-small.jsonl`, `runs/bench-hard-open-small.jsonl` (gpt-oss-120b), with every tool call and the model's reasoning |

**Why GLM-5.3.** Of the models this key can call with tools, it is the largest open-weights instruction-tuned one
that costs clearly less than Claude Opus 5.5 ($4 and $20 per million tokens). The one larger-looking alternative on
the list costs $3 and $15, close to Opus, so it would not answer "why not something cheaper". The second model is
the smaller, much cheaper end of the list.

**Cost** is computed exactly as for the Claude rows: recorded input and output tokens at the list price, with no
discount for cached input. For these models that is an upper bound; the bill is lower when the provider's prompt
cache hits.

## The adapter

[`openmodel.py`](../openmodel.py) implements the one call the agent loop makes, `client.beta.messages.create(...)`,
over chat completions. `investigate._agent_loop`, the prompt, the tool schemas, `build_verdict` and the gate are not
changed; the Claude prompt and tool schemas are still pinned byte for byte by `tests/test_topology.py`. It uses the
Python standard library only, since `app.py` imports `bench.py` and the deploy installs `requirements.txt` alone.

| Anthropic request or response | Chat completions | Note |
|---|---|---|
| `system` | first message, `role: system` | same text |
| `tools[].input_schema` | `tools[].function.parameters` | same JSON schemas, enums and required fields |
| `tools[].strict` | dropped | the provider has no strict tool mode; a bad argument comes back as the loop's usual error result |
| `thinking`, `output_config.effort` | dropped | the model reasons by its own default. On GLM-5.3, `reasoning_effort` set to medium or high switched its reasoning **off** in a probe (0 reasoning tokens), and `adaptive` is rejected, so effort is not mapped. gpt-oss-120b's default effort is medium, the Claude runs' setting |
| `betas`, `fallbacks` | dropped | server-side fallbacks are a Claude feature, used only on Opus |
| `max_tokens` 16,000 | `max_tokens` 16,000 | the same output budget per reply, reasoning included |
| temperature | not sent | the provider's default, as the Claude runs used theirs |
| `tool_use` block in an assistant turn | `tool_calls[]` with `arguments` as a JSON string | the arguments go back exactly as the model sent them |
| `tool_result` blocks in a user turn | one `role: tool` message each, by `tool_call_id`, then any note as a user message | `is_error` is dropped: the result text already starts with `error:` |
| `thinking` block | `reasoning_content` | both ways: the model's reasoning is recorded as thinking events, and sent back with its tool calls |
| `stop_reason` | from `finish_reason` | `tool_use` whenever tool calls came back; `stop` → `end_turn`, `length` → `max_tokens`, `content_filter` → `refusal` |
| `usage.input_tokens`, `output_tokens` | `usage.prompt_tokens`, `completion_tokens` | output includes reasoning tokens |

**Malformed tool calls.** Arguments that are not valid JSON reach the loop as a non-object, so it does what it
already does with bad tool input: a lookup gets an `error: arguments must be an object` result and the
investigation goes on; a submission with no readable suspects goes to a human. Nothing crashes.

**Retries.** Rate limits (429), server errors (500, 502, 503, 504, plus 408, 409, 425) and dropped connections are
retried up to 6 times, waiting 2, 4, 8, 16 and 32 s; any other error ends that outage's investigation, which then
goes to a human as a failed run, exactly as an API failure does on the Claude path. Each outage is written to its run
file as soon as it finishes, so a re-run resumes where it stopped.

**Tests.** [`tests/test_openmodel.py`](../tests/test_openmodel.py): 16 tests over a fake HTTP transport, with no
network and no key. They run the unchanged agent loop end to end through the adapter and check the translated request
and the tool-call round trip, malformed JSON, the nudge when a reply has no tool call, the stop-reason mapping, retries,
backoff and giving up. The recorded open-model runs are also re-run from their seeds by
[`tests/test_reproducibility.py`](../tests/test_reproducibility.py), like every Claude run: every tool result byte for
byte and every verdict re-verified (see [reproducibility.md](reproducibility.md)).

## Reproduce

```bash
# needs FIREWORKS_API_KEY in .env
uv run --env-file .env python bench.py --n 132 --variant open          # also: hard-open (--n 44), open-small, hard-open-small
uv run python bench.py --report                                        # every table, and runs/evidence.json
```

`runs/evidence.json` holds the open-model rows under `open`, `hard-open`, `open-small` and `hard-open-small`, with
three extras the Claude rows do not have: `wrong_answers`, the no-gate what-if (`ungated_answered`,
`ungated_wrong`), and paired exact McNemar tests against the Opus agent (`vs_agent`) and the auto route
(`vs_auto`). The no-gate what-if for every model is printed by `bench.py --report` as the last table.
