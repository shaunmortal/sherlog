# Bring your own outage

The console breaks a simulated shop. This page is for a real one. Export 10 minutes of your own telemetry, and
Sherlog runs the same alarm, the same no-AI rankings, the same agent tools, the same evidence verifier and the
same blast-radius check on it.

There is one condition: your services must fit the shop's shape. Sherlog reasons over this call graph, so each
of your services has to be mapped onto one of these 11 names by the job it does:

| service | calls |
|---|---|
| frontend | catalog, recommend, cart, checkout |
| recommend | catalog |
| checkout | cart, payment, shipping, currency, email |
| shipping | currency |
| catalog | db |
| cart | cache |
| payment, email, currency, db, cache | nothing |

A shop built like the common open-source microservices demo already fits (`productcatalogservice` → `catalog`,
`cartservice` → `cart`, and so on).

## Try it with the sample

```sh
curl -s localhost:8000/api/sample-incident > incident.json        # one simulated outage, culprit removed
curl -s -X POST localhost:8000/api/triage \
     -H 'content-type: application/json' --data-binary @incident.json
```

`/api/triage` needs no API key and is not rate limited. On the sample it answers (trimmed):

```json
{"alarm": 91, "now": 95, "tick_seconds": 5,
 "zscore_ranking": [{"service": "cart", "metric": "latency_ms", "z": 200.0}, {"service": "cache", ...}, ...],
 "robust_ranking": ["cache", "cart", "checkout", ...],
 "blast": {"service": "cart", "abnormal": ["cart", "cache", "checkout", "frontend"],
           "unexplained": ["cache"], "better": "cache"}}
```

The classic z-score ranking blames `cart`, but `cart` cannot explain why `cache` is slow. The blast-radius check
says so and names `cache`, which explains everything abnormal. The answer is in `byo.py` (`SAMPLE`).

To run the agent on it, the server needs live mode (an API key). Each run uses one of the visitor's live runs,
the same as `/api/break`:

```sh
jq '{incident: ., model: "opus"}' incident.json | curl -N -X POST localhost:8000/api/investigate \
     -H 'content-type: application/json' --data-binary @-
```

`model` is `opus`, `sonnet`, `haiku` or `auto`. The answer streams the same server-sent events as
`/api/break`: `telemetry`, then `tool`, `thinking`, `handoff` and `error` events, then a `verdict` carrying
`blast`. Nobody knows the true culprit of an uploaded outage, so there is no `truth` event.

## The format

```json
{
  "format": "sherlog-incident/1",
  "tick_seconds": 5,
  "series": {
    "frontend": {"latency_ms": [120 numbers], "error_rate": [...], "cpu_pct": [...], "memory_mb": [...]},
    "...": "all 11 services"
  },
  "logs": [{"tick": 93, "service": "checkout", "level": "WARN", "text": "rpc payment.Charge failed: UNAVAILABLE"}]
}
```

- **series**: every service, every metric, exactly 120 finite numbers, oldest first, one every 5 s.
  The units and limits are `latency_ms` in milliseconds, at least 0.1; `error_rate` as the share of failed
  requests, 0 to 1; `cpu_pct` 0 to 100; and `memory_mb` in megabytes, at least 1.
- **the window**: the first 60 ticks (5 minutes) are the baseline that defines "normal", so they must be
  healthy. The alarm fires at the first tick from 60 on where some metric has been 4 standard deviations above
  normal for 3 ticks in a row. The agent sees the telemetry up to 4 ticks after the alarm. If no alarm fires,
  `/api/triage` still ranks the services, and `/api/investigate` refuses with 422.
- **logs** are optional: at most 2000 lines, each with a `tick` from 0 to 119, the `service` that wrote it,
  a `level` of `INFO`, `WARN` or `ERROR`, and a `text` of 1 to 500 characters. The log tool shows only
  `WARN` and `ERROR` lines.
- **which service a line is about** is derived from the text, never sent. A line is about the first other
  service it names, as a whole word, case-sensitive, either by name (`payment`) or by RPC (`Charge`). A line
  that names no other service is about its writer. So `checkout` writing `rpc payment.Charge failed` points
  at `payment`. The RPC names are `Render`, `ListRecommendations`, `PlaceOrder`, `GetQuote`, `ListProducts`,
  `GetCart`, `Charge`, `SendConfirmation`, `Convert`, `Query` and `Get`, in the order of the table above.
  The rule matches the simulator on every line of every normal-mode outage (`tests/test_byo.py`). It
  misreads a line that uses a service name as an ordinary word, such as `cache warm-up` written by `db`.

Anything else is refused with 422, and the message points at the problem. Two examples:
`series.cart.cpu_pct: Value error, tick 3 is 140, but cpu_pct must be between 0 and 100`, and
`series.email: Field required`. `detail` names the first three problems in one sentence and counts the rest;
`errors` lists every one with its `loc` and `msg`. Unknown fields are refused too, so a typo cannot be silently dropped.
Bodies over 2 MB get a 413.

## From Prometheus

`scripts/prom_to_sherlog.py` turns `query_range` answers into this format. Query the 10 minutes you care about
with `step=5s`, one query per metric, already in Sherlog's units and aggregated to one series per service, for
example `1000 * avg by (service) (...)` for latency in milliseconds. Then:

```sh
python scripts/prom_to_sherlog.py latency_ms=latency.json error_rate=errors.json \
    cpu_pct=cpu.json memory_mb=memory.json \
    --map productcatalogservice=catalog --map cartservice=cart ... -o incident.json
```

- One combined file works too: give it without `METRIC=`, and set a `metric` label on every series with
  `label_replace`.
- `--service-label app` reads the service from another label. `--end UNIX_TIME` picks the window's last tick.
  The default is the newest sample.
- NaN samples count as missing. A single missing reading is filled with a straight line between its
  neighbours, and at the window's edges the nearest sample is held for at most 5 s. Two or more missing in a row
  are refused, with the series and the times named. The alarm needs 3 abnormal readings in a row, and invented
  ones could fake that.
- Values outside a metric's limits are clamped to them, as the simulator's own monitoring does.
- The script refuses unknown services (map them, or filter them out of the query), two series for the same
  service and metric (aggregate them), and missing services or metrics. Its output passes the server's checks,
  so a file it writes is always accepted. It does not convert logs. Add those yourself in the shape above.

**Scrape every 5 s.** Prometheus fills a `step=5s` query from slower scrapes by repeating each sample, and then
one abnormal reading looks like 3 in a row. On the sample outage (fault from tick 89, alarm at 91), holding each
reading for 15 s or 30 s fired the alarm at tick 68, during healthy traffic. Holding for 10 s fired it at 92.

## What is checked, and what is not

**Checked:**
- the document's shape, names, lengths and limits
- that every suspect the agent names is backed by a tool call that really showed it abnormal (the verifier)
- whether the answer explains everything abnormal (the blast-radius check)

**Not checked:**
- that the numbers are real, in the right units, or really 5 s apart
- that your services are mapped onto the right roles
- that the first 5 minutes were healthy

Nothing scores the answer, because nobody knows the culprit.

**Privacy:** the server keeps nothing it is sent. During `/api/investigate`, the agent sends your telemetry and
log lines to the Claude API, so leave secrets and personal data out of the log text.

**Limits:**
- the same 11-service shape only
- these 4 metrics
- one 10-minute window at 5 s
- no traces
