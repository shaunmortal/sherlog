# Bring your own outage

The console breaks a simulated shop. This page is for your own system. Export 10 minutes of its telemetry, and
Sherlog runs the same alarm, the same no-AI rankings, the same agent tools, the same evidence verifier and the
same blast-radius check on it.

Sherlog reasons over a call graph: who calls whom. Give it yours in `calls`, with 2 to 40 services under their
own names:

```json
"calls": {
  "gateway":   ["auth", "orders"],
  "orders":    ["inventory", "payments"],
  "inventory": ["postgres"],
  "auth": [], "payments": [], "postgres": []
}
```

Every service is a key, listing the services it calls (`[]` for none), and every service it calls is a key too.
The full format is [below](#the-format).

Or leave `calls` out and map each of your services onto one of the simulated shop's 11 names by the job it does.
Then Sherlog uses the shop's call graph:

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

## Try it with the samples

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

`/api/sample-incident?graph=custom` is a sample with its own call graph: six services of the same outage
renamed, wired as in the example above except that `gateway` also calls `inventory`, with an `rpc` map. Its
telemetry is still simulated. Triage blames `inventory`, and the blast-radius check names `postgres`,
which has `cache`'s place. In the console, **Your data** → **Use** a custom-graph sample → **Triage (no AI)**
draws its six services on the charts and the map.

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
  "calls": {"gateway": ["auth", "orders"], "orders": ["inventory", "payments"], "...": "every service"},
  "rpc": {"inventory": "Reserve"},
  "series": {
    "gateway": {"latency_ms": [120 numbers], "error_rate": [...], "cpu_pct": [...], "memory_mb": [...]},
    "...": "every service in calls"
  },
  "logs": [{"tick": 93, "service": "orders", "level": "WARN", "text": "rpc inventory.Reserve failed: UNAVAILABLE"}]
}
```

- **calls** is optional. With it, its keys are the services: 2 to 40 of them, each a name of 1 to 40 lowercase
  letters, digits and dashes that starts with a letter and does not end with a dash (`api-gateway`, `postgres`).
  Each lists the services it calls. Every one it lists must be a key too, a service cannot call itself, and a
  list cannot name a service twice. Cycles are allowed. Without `calls`, the services are the shop's 11 names
  above, as before.
- **rpc** is optional and needs `calls`: a service's RPC name, so a log line that names only the RPC
  (`Reserve timed out`) points at the right service. Each is 1 to 40 letters, digits and underscores starting with
  a letter, and no two services, nor an RPC and a service, may share a name.
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
  service it names, as a whole word, case-sensitive, either by name (`payment`) or by RPC (`Charge`). Where one
  name contains another (`api` and `api-gateway`), the longer one counts. A line that names no other service is
  about its writer. So `checkout` writing `rpc payment.Charge failed` points at `payment`. Without `calls`, the
  RPC names are the shop's: `Render`, `ListRecommendations`, `PlaceOrder`, `GetQuote`, `ListProducts`,
  `GetCart`, `Charge`, `SendConfirmation`, `Convert`, `Query` and `Get`, in the order of the table above. With
  `calls`, they are the ones in `rpc`, if any.
  The rule matches the simulator on every line of every normal-mode outage (`tests/test_byo.py`). It
  misreads a line that uses a service name as an ordinary word, such as `cache warm-up` written by `db`.

Anything else is refused with 422, and the message points at the problem. Some examples:
`series.cart.cpu_pct: Value error, tick 3 is 140, but cpu_pct must be between 0 and 100`,
`series.email: Field required`, `calls: Value error, orders calls billing, which is not one of the services in
calls; add it, with the services it calls (or [])` and `logs.0.service: 'cart' is not one of the 5 services in
calls`. `detail` names the first three problems in one sentence and counts the rest;
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

- `--calls calls.json` gives your own call graph, the `calls` object above in a file of its own. The script
  then takes your services under their own names and writes `calls` into the document. Without it, map each
  service onto the shop's names with `--map`.
- One combined file works too: give it without `METRIC=`, and set a `metric` label on every series with
  `label_replace`.
- `--service-label app` reads the service from another label. `--end UNIX_TIME` picks the window's last tick.
  The default is the newest sample.
- NaN samples count as missing. A single missing reading is filled with a straight line between its
  neighbours, and at the window's edges the nearest sample is held for at most 5 s. Two or more missing in a row
  are refused, with the series and the times named. The alarm needs 3 abnormal readings in a row, and invented
  ones could fake that.
- Values outside a metric's limits are clamped to them, as the simulator's own monitoring does.
- The script refuses a bad call graph, with the same messages as the server, and unknown services (add them to
  `--calls`, map them, or filter them out of the query), two series for the same service and metric (aggregate
  them), and missing services or metrics. Its output passes the server's checks,
  so a file it writes is always accepted. It does not convert logs. Add those yourself in the shape above.

**Scrape every 5 s.** Prometheus fills a `step=5s` query from slower scrapes by repeating each sample, and then
one abnormal reading looks like 3 in a row. On the sample outage (fault from tick 89, alarm at 91), holding each
reading for 15 s or 30 s fired the alarm at tick 68, during healthy traffic. Holding for 10 s fired it at 92.

## What is checked, and what is not

**Checked:**
- the document's shape, names, lengths and limits, and that `calls`, `series` and the log lines name the same
  services
- that every suspect the agent names is backed by a tool call that really showed it abnormal (the verifier)
- whether the answer explains everything abnormal (the blast-radius check)

**Not checked:**
- that the numbers are real, in the right units, or really 5 s apart
- that `calls` is your real call graph, or that your services are mapped onto the right roles
- that the first 5 minutes were healthy

Nothing scores the answer, because nobody knows the culprit. Uploads get no rollback test: it replays an outage
from the simulator's seed, which real telemetry does not have. The benchmark's numbers come from the simulated
shop, not from uploads.

With its own `calls`, the agent's prompt calls your system "a system built from N services" instead of the
shop, and its tools accept only your services. The prompt for the shop is unchanged.

**Privacy:** the server keeps nothing it is sent. During `/api/investigate`, the agent sends your telemetry and
log lines to the Claude API, so leave secrets and personal data out of the log text.

**Limits:**
- 2 to 40 services, each sending all 4 metrics
- these 4 metrics
- one 10-minute window at 5 s
- no traces
