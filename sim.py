"""A simulated microservice shop: call graph, telemetry, fault injection and the alarm.

An incident is fully determined by (root, fault, seed), so every run can be replayed.
The telemetry is synthetic -- see README, "Limitations".
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from functools import cached_property

TICK_SECONDS = 5
WINDOW_TICKS = 120            # 10 minutes of history
BASELINE_TICKS = 60           # the first 5 minutes are always healthy
ONSET_RANGE = (70, 90)        # tick at which the fault starts
LOOKAHEAD_TICKS = 4           # telemetry that exists after the alarm when the investigation starts
ALARM_Z = 4.0
ALARM_SUSTAIN_TICKS = 3
MAD_TO_STD = 1.4826        # median absolute deviation -> standard deviation, for normal noise
BASE_ERROR_RATE = 0.002
PROPAGATED_ERROR_SHARE = 0.9  # retries absorb some of a callee's failures
FAILING_ERROR_RATE = 0.05     # callers log failed RPCs above this
SLOW_EXTRA_MS = 100           # callers log slow RPCs above this much extra latency
ROUTINE_LOG_P = 0.05
RED_HERRING_LOG_P = 0.01
LOW_TRAFFIC = 80              # requests per tick at or below which a service's telemetry is sparse
SPARSE_KINDS = ("sparse", "wander", "steady")
BUSY_KINDS = ("busy", "busy", "steady", "wander")
SAWTOOTH_SHARE = 0.6          # services whose heap visibly saws up and down with garbage collection
LATENCY_WOBBLE = (math.log(0.02), math.log(0.6))  # latency noise as a share of healthy latency, log-uniform
HARD_COMPLAIN_SHARE = 0.4     # hard mode: callers log this share of the complaints they normally would
DECOY_CPU_SHARE = 0.6
DECOY_LOG_P = 0.25            # the decoy's own, honest log line: what an engineer would find if they looked
DECOY_LINES = {
    "cpu_pct": "nightly batch job running: reindexing, worker pool busy",
    "memory_mb": "cache warm-up: loading entries into memory",
}

# caller -> {callee: share of the caller's requests that make this call}
CALLS: dict[str, dict[str, float]] = {
    "frontend": {"catalog": 0.8, "recommend": 0.5, "cart": 0.3, "checkout": 0.1},
    "recommend": {"catalog": 1.0},
    "checkout": {"cart": 1.0, "payment": 1.0, "shipping": 1.0, "currency": 1.0, "email": 1.0},
    "shipping": {"currency": 1.0},
    "catalog": {"db": 1.0},
    "cart": {"cache": 1.0},
    "payment": {}, "email": {}, "currency": {}, "db": {}, "cache": {},
}
SERVICES = tuple(CALLS)
# requests per 5 s tick; a quiet service reports noisier, sparser telemetry
TRAFFIC = {
    "frontend": 400, "recommend": 200, "checkout": 40, "shipping": 40, "catalog": 320, "cart": 120,
    "payment": 40, "email": 40, "currency": 80, "db": 500, "cache": 900,
}
CALLEES = {s: tuple(callees) for s, callees in CALLS.items()}


def _callers(calls: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    return {s: tuple(c for c in calls if s in calls[c]) for s in calls}


CALLERS = _callers(CALLEES)
RPC = {
    "frontend": "Render", "recommend": "ListRecommendations", "checkout": "PlaceOrder",
    "shipping": "GetQuote", "catalog": "ListProducts", "cart": "GetCart", "payment": "Charge",
    "email": "SendConfirmation", "currency": "Convert", "db": "Query", "cache": "Get",
}
# a service's own work per request: latency ms, cpu %, memory MB
BASE = {
    "frontend": (25, 40, 420), "recommend": (35, 30, 380), "checkout": (30, 25, 260),
    "shipping": (15, 15, 180), "catalog": (20, 30, 300), "cart": (10, 20, 200),
    "payment": (45, 20, 240), "email": (25, 10, 150), "currency": (8, 15, 120),
    "db": (12, 45, 900), "cache": (2, 20, 500),
}
METRICS = ("latency_ms", "error_rate", "cpu_pct", "memory_mb")
# a quiet service can go many ticks without a single error; one failed request out of 40 is not an alarm
STD_FLOOR = {"latency_ms": 2.0, "error_rate": 0.01, "cpu_pct": 1.5, "memory_mb": 6.0}
# an upload's own metric (bytes, cores, sockets...) has no floor of its own: a wobble under 1% of its normal level
# is not an alarm, whatever its unit; one that is flat at zero gets a tiny floor so a z-score is still defined
GENERIC_FLOOR_SHARE = 0.01
GENERIC_FLOOR_MIN = 1e-6
LIMITS = {"latency_ms": (0.1, None), "error_rate": (0.0, 1.0), "cpu_pct": (0.0, 100.0), "memory_mb": (1.0, None)}
DECIMALS = {"latency_ms": 1, "error_rate": 4, "cpu_pct": 1, "memory_mb": 0}

FAULTS = {
    "latency": "adds 400 ms to every request (slow network)",
    "errors": "fails 30% of requests with exceptions (bad deploy)",
    "cpu": "starves the CPU so requests queue up",
    "memory": "leaks memory steadily",
}
EXCEPTIONS = (
    "NullPointerException", "KeyError: 'currency_code'",
    "IndexError: list index out of range", "TimeoutError: connection pool exhausted",
)
RED_HERRINGS = (
    "retrying connection to config server (attempt 2)", "GC pause 140 ms",
    "slow query log: 310 ms", "TLS certificate expires in 21 days",
)


def std_floor(metric: str, mean: float) -> float:
    """The smallest spread a metric's baseline is given, so a quiet baseline cannot turn noise into an alarm."""
    if metric in STD_FLOOR:
        return STD_FLOOR[metric]
    return max(GENERIC_FLOOR_SHARE * abs(mean), GENERIC_FLOOR_MIN)


@dataclass(frozen=True)
class LogLine:
    tick: int
    service: str  # who wrote the line
    level: str
    text: str
    about: str    # which service the line implicates


@dataclass(frozen=True)
class Incident:
    root: str | None
    fault: str | None
    seed: int
    onset: int
    series: dict[str, dict[str, tuple[float, ...]]]
    logs: tuple[LogLine, ...]
    decoy: str | None = None  # hard mode: a service with a harmless anomaly at the same time
    # an upload's own call graph, service -> the services it calls; None is the shop's (CALLS)
    calls: dict[str, tuple[str, ...]] | None = None
    # an upload's own metric names, of which each service reports one or more; the shop's are the four
    metrics: tuple[str, ...] = METRICS

    @cached_property
    def services(self) -> tuple[str, ...]:
        return SERVICES if self.calls is None else tuple(self.calls)

    @cached_property
    def callees(self) -> dict[str, tuple[str, ...]]:
        """service -> the services it calls."""
        return CALLEES if self.calls is None else self.calls

    @cached_property
    def callers(self) -> dict[str, tuple[str, ...]]:
        """service -> the services that call it."""
        return CALLERS if self.calls is None else _callers(self.calls)

    @cached_property
    def measured(self) -> dict[str, tuple[str, ...]]:
        """service -> the metrics it reports, in the order of `metrics`."""
        return {s: tuple(m for m in self.metrics if m in self.series[s]) for s in self.services}

    @cached_property
    def stats(self) -> dict[tuple[str, str], tuple[float, float]]:
        """(service, metric) -> (mean, std) over the healthy baseline."""
        out = {}
        for s in self.services:
            for m in self.measured[s]:
                base = self.series[s][m][:BASELINE_TICKS]
                mean = statistics.fmean(base)
                out[(s, m)] = (mean, max(statistics.pstdev(base), std_floor(m, mean)))
        return out

    def z(self, service: str, metric: str, tick: int) -> float:
        mean, std = self.stats[(service, metric)]
        return (self.series[service][metric][tick] - mean) / std

    @cached_property
    def alarm(self) -> int | None:
        """First tick where any metric stayed ALARM_Z above normal for ALARM_SUSTAIN_TICKS ticks."""
        return self.alarm_on(self.metrics)

    def alarm_on(self, metrics: tuple[str, ...]) -> int | None:
        """The alarm, watching only these metrics (on the services that report them)."""
        watched = [(s, m) for s in self.services for m in metrics if m in self.series[s]]
        for t in range(BASELINE_TICKS, WINDOW_TICKS):
            recent = range(t - ALARM_SUSTAIN_TICKS + 1, t + 1)
            if any(all(self.z(s, m, k) >= ALARM_Z for k in recent) for s, m in watched):
                return t
        return None

    @property
    def now(self) -> int:
        """Last tick of telemetry the investigator may see."""
        if self.alarm is None:
            return WINDOW_TICKS - 1
        return min(self.alarm + LOOKAHEAD_TICKS, WINDOW_TICKS - 1)

    def window_z(self, service: str, metric: str) -> float:
        """Mean z-score from the alarm to now: how abnormal this metric is right now."""
        start = self.alarm if self.alarm is not None else self.now - LOOKAHEAD_TICKS
        return statistics.fmean(self.z(service, metric, t) for t in range(start, self.now + 1))

    def worst(self, service: str) -> tuple[str, float]:
        """The service's most abnormal metric and its window z-score."""
        return max(((m, self.window_z(service, m)) for m in self.measured[service]), key=lambda mz: mz[1])


def zscore_ranking(inc: Incident) -> list[str]:
    """The no-AI control: rank services by their strongest deviation."""
    return sorted(inc.services, key=lambda s: inc.worst(s)[1], reverse=True)


def robust_ranking(inc: Incident) -> list[str]:
    """A second no-AI control: the same ranking, but measured against the baseline's median and MAD, which a
    few spikes cannot inflate (the robust scaling that methods like BARO use)."""
    start = inc.alarm if inc.alarm is not None else inc.now - LOOKAHEAD_TICKS

    def score(service: str) -> float:
        best = float("-inf")
        for m in inc.measured[service]:
            base = inc.series[service][m][:BASELINE_TICKS]
            centre = statistics.median(base)
            spread = max(MAD_TO_STD * statistics.median(abs(v - centre) for v in base), std_floor(m, centre))
            best = max(best, (statistics.median(inc.series[service][m][start:inc.now + 1]) - centre) / spread)
        return best

    return sorted(inc.services, key=score, reverse=True)


def simulate(root: str | None, fault: str | None, seed: int, hard: bool = False,
             rolled_back: str | None = None) -> Incident:
    """Simulate 10 minutes of the shop; with a root and fault, break that service at a random onset.

    Hard mode adds what makes real outages confusing: a decoy (an unrelated service with a harmless anomaly
    that starts at the same time, like a batch job) and callers that log far fewer complaints.

    `rolled_back` replays the same outage, with the same noise, as if that service had been rolled back: the
    root loses its fault, the decoy its anomaly, and any other service had nothing of its own to remove."""
    if (root is None) != (fault is None):
        raise ValueError("give both root and fault, or neither")
    if root is not None and (root not in CALLS or fault not in FAULTS):
        raise ValueError(f"unknown service or fault: {root!r}, {fault!r}")
    if rolled_back is not None and rolled_back not in CALLS:
        raise ValueError(f"unknown service to roll back: {rolled_back!r}")
    rng = random.Random(seed)
    onset = rng.randint(*ONSET_RANGE)
    noise = {s: _noise(rng, s) for s in SERVICES}
    decoy = _decoy(rng, root, onset) if hard and root is not None else None
    truth = _truth(root, fault, onset, decoy)
    if rolled_back is None:
        seen = truth
    else:
        root, fault = (None, None) if rolled_back == root else (root, fault)
        decoy = None if decoy and decoy.service == rolled_back else decoy
        seen = _truth(root, fault, onset, decoy)
    series = {
        s: {m: _observe_as(rng, [x[s][m] for x in truth], [x[s][m] for x in seen], m, noise[s][m], TRAFFIC[s])
            for m in METRICS}
        for s in SERVICES
    }
    complain = HARD_COMPLAIN_SHARE if hard else 1.0
    # ponytail: a rollback re-measures the metrics only (logs come last, so skipping them moves no draw);
    # re-simulate the logs too if a rolled-back outage is ever shown to the agent
    logs = () if rolled_back else _logs(rng, root, fault, onset, truth, complain, decoy)
    return Incident(root, fault, seed, onset, series, logs, decoy.service if decoy else None)


def _truth(root: str | None, fault: str | None, onset: int, decoy: Decoy | None) -> tuple[dict, ...]:
    """Noise-free metrics of every service at every tick."""
    truth = tuple(_true_state(root, fault, t - onset) for t in range(WINDOW_TICKS))
    if decoy:
        truth = tuple(_with_decoy(state, decoy, t) for t, state in enumerate(truth))
    return truth


@dataclass(frozen=True)
class Decoy:
    service: str
    metric: str    # cpu_pct (a batch job) or memory_mb (a cache warm-up): neither slows anyone down
    level: float
    onset: int


def callers_of(service: str) -> set[str]:
    """Everything that calls the service, directly or through others."""
    reach, todo = set(), [service]
    while todo:
        for caller in CALLERS[todo.pop()]:
            if caller not in reach:
                reach.add(caller)
                todo.append(caller)
    return reach


def _decoy(rng: random.Random, root: str, onset: int) -> Decoy:
    """A harmless anomaly on a service the real fault cannot touch, starting with it (never before it)."""
    service = rng.choice([s for s in SERVICES if s != root and s not in callers_of(root)])
    if rng.random() < DECOY_CPU_SHARE:
        return Decoy(service, "cpu_pct", rng.uniform(80, 95), onset + rng.randint(0, 2))
    return Decoy(service, "memory_mb", BASE[service][2] * rng.uniform(1.2, 1.35), onset + rng.randint(0, 2))


def _with_decoy(state: dict, decoy: Decoy, t: int) -> dict:
    if t < decoy.onset:
        return state
    return {**state, decoy.service: {**state[decoy.service], decoy.metric: decoy.level}}


@dataclass(frozen=True)
class Texture:
    """How one metric of one service is noisy in one incident. Real telemetry is not one kind of noise."""
    jitter: float          # white noise on every reading
    spike_p: float = 0.0   # chance of a one-tick spike (a slow request, a GC pause)
    spike: float = 0.0     # mean height of a spike
    phi: float = 0.0       # memory of the slow drift
    drift: float = 0.0     # size of the slow drift's steps
    gap_p: float = 0.0     # chance a tick has no fresh sample; the line runs straight between samples
    saw: float = 0.0       # sawtooth height: heap growing until garbage collection
    period: int = 1        # sawtooth length in ticks


def _texture(kind: str, scale: float) -> Texture:
    if kind == "steady":   # flat, with the odd tall needle
        return Texture(jitter=0.3 * scale, spike_p=0.03, spike=4 * scale)
    if kind == "busy":     # dense grass of small spikes
        return Texture(jitter=0.9 * scale, spike_p=0.07, spike=2 * scale)
    if kind == "wander":   # slow rolling drift
        return Texture(jitter=0.25 * scale, phi=0.6, drift=0.6 * scale)
    return Texture(jitter=0.7 * scale, gap_p=0.6)  # sparse: few requests, few fresh samples


def _noise(rng: random.Random, service: str) -> dict[str, Texture]:
    """Per-incident noise of each metric. Services differ: a quiet victim can out-shout a noisy cause."""
    kinds = SPARSE_KINDS if TRAFFIC[service] <= LOW_TRAFFIC else BUSY_KINDS
    memory = BASE[service][2]
    heap = (Texture(jitter=memory * 0.004, saw=memory * rng.uniform(0.02, 0.06), period=rng.randint(10, 30))
            if rng.random() < SAWTOOTH_SHARE else Texture(jitter=memory * 0.01))
    return {
        # a request's latency wobbles in proportion to how long it takes; how much varies by service
        "latency_ms": _texture(rng.choice(kinds), HEALTHY_LATENCY[service] * math.exp(rng.uniform(*LATENCY_WOBBLE))),
        "error_rate": Texture(jitter=0.0),  # errors are counted from requests, see _observe
        "cpu_pct": _texture(rng.choice(kinds), rng.uniform(1, 5)),
        "memory_mb": heap,
    }


def _fault_effect(fault: str, k: int, own: dict[str, float]) -> dict[str, float]:
    """What the fault does to the broken service itself, k ticks after onset."""
    if fault == "latency":
        return {"latency_ms": own["latency_ms"] + 400 * min(1.0, (k + 1) / 2)}
    if fault == "errors":
        return {"error_rate": 0.30}
    if fault == "cpu":
        return {"cpu_pct": 97.0, "latency_ms": own["latency_ms"] + 180}
    return {"memory_mb": own["memory_mb"] + 35 * (k + 1), "latency_ms": own["latency_ms"] + 4 * (k + 1)}


def _true_state(root: str | None, fault: str | None, k: int) -> dict[str, dict[str, float]]:
    """Noise-free metrics of every service; latency and errors flow up the call graph to callers."""
    own = {
        s: {"latency_ms": BASE[s][0], "error_rate": BASE_ERROR_RATE, "cpu_pct": BASE[s][1], "memory_mb": BASE[s][2]}
        for s in SERVICES
    }
    if root is not None and k >= 0:
        own = {**own, root: {**own[root], **_fault_effect(fault, k, own[root])}}

    total: dict[str, dict[str, float]] = {}

    def visit(s: str) -> dict[str, float]:
        if s not in total:
            callees = CALLS[s].items()
            latency = own[s]["latency_ms"] + sum(share * visit(c)["latency_ms"] for c, share in callees)
            inherited = sum(share * visit(c)["error_rate"] for c, share in callees) * PROPAGATED_ERROR_SHARE
            total[s] = {**own[s], "latency_ms": latency, "error_rate": min(1.0, own[s]["error_rate"] + inherited)}
        return total[s]

    return {s: visit(s) for s in SERVICES}


HEALTHY_LATENCY = {s: v["latency_ms"] for s, v in _true_state(None, None, 0).items()}


def _observe(rng: random.Random, truth: list[float], metric: str, tex: Texture, traffic: int) -> tuple[float, ...]:
    """What the monitoring system records for one metric of one service, tick by tick."""
    lo, hi = LIMITS[metric]
    if metric == "error_rate":  # failed requests out of this tick's requests: mostly 0 on a quiet service
        raw = [rng.binomialvariate(traffic, min(1.0, v)) / traffic for v in truth]
    else:
        raw = [v + n for v, n in zip(truth, _noise_track(rng, len(truth), tex))]
    return tuple(round(max(lo, v) if hi is None else min(hi, max(lo, v)), DECIMALS[metric]) for v in raw)


def _observe_as(rng: random.Random, truth: list[float], seen: list[float], metric: str, tex: Texture,
                traffic: int) -> tuple[float, ...]:
    """Record `seen` with the noise `truth` would have had. The stream then moves on exactly as it does for
    `truth` (an error count draws more or fewer numbers depending on its rate), so no other metric changes."""
    if seen == truth:
        return _observe(rng, truth, metric, tex, traffic)
    replay = random.Random()
    replay.setstate(rng.getstate())
    _observe(rng, truth, metric, tex, traffic)
    return _observe(replay, seen, metric, tex, traffic)


def _noise_track(rng: random.Random, n: int, tex: Texture) -> list[float]:
    """The noise on one metric over the whole window."""
    phase = rng.randrange(tex.period)
    out: list[float] = []
    drift, spiked = 0.0, False
    for t in range(n):
        drift = tex.phi * drift + rng.gauss(0, tex.drift)
        spiked = bool(tex.spike) and not spiked and rng.random() < tex.spike_p  # never two spikes in a row
        spike = rng.expovariate(1 / tex.spike) if spiked else 0.0
        saw = tex.saw * (((t + phase) % tex.period) / tex.period - 0.5)
        out.append(drift + saw + spike + rng.gauss(0, tex.jitter))
    if not tex.gap_p:
        return out
    fresh = [t for t in range(n) if t in (0, n - 1) or rng.random() > tex.gap_p]
    return _join_samples(out, fresh)


def _join_samples(values: list[float], fresh: list[int]) -> list[float]:
    """Keep the values at the fresh ticks and draw straight lines between them."""
    out = list(values)
    for a, b in zip(fresh, fresh[1:]):
        for t in range(a + 1, b):
            out[t] = values[a] + (values[b] - values[a]) * (t - a) / (b - a)
    return out


def _logs(rng: random.Random, root, fault, onset: int, truth, complain: float = 1.0,
          decoy: Decoy | None = None) -> tuple[LogLine, ...]:
    lines = []
    for t in range(WINDOW_TICKS):
        for s in SERVICES:
            if rng.random() < ROUTINE_LOG_P:
                lines.append(LogLine(t, s, "INFO", f"{RPC[s]} ok, {rng.randint(80, 400)} requests in last 5s", s))
            if rng.random() < RED_HERRING_LOG_P:
                lines.append(LogLine(t, s, "WARN", rng.choice(RED_HERRINGS), s))
            lines.extend(_caller_complaints(rng, t, s, truth, complain))
        if decoy and t >= decoy.onset and rng.random() < DECOY_LOG_P:
            lines.append(LogLine(t, decoy.service, "INFO", DECOY_LINES[decoy.metric], decoy.service))
        if root is not None and t >= onset:
            own = _root_line(rng, root, fault, t - onset)
            if own:
                lines.append(LogLine(t, root, own[0], own[1], root))
    return tuple(lines)


def _caller_complaints(rng: random.Random, t: int, caller: str, truth, complain: float = 1.0) -> list[LogLine]:
    """A caller logs about callees that fail or are slow -- naming the callee, not itself."""
    out = []
    for callee in CALLS[caller]:
        state = truth[t][callee]
        extra_ms = state["latency_ms"] - truth[0][callee]["latency_ms"]
        rpc = f"rpc {callee}.{RPC[callee]}"
        if state["error_rate"] > FAILING_ERROR_RATE and rng.random() < 0.6 * complain:
            out.append(LogLine(t, caller, "WARN", f"{rpc} failed: UNAVAILABLE", callee))
        elif extra_ms > SLOW_EXTRA_MS and rng.random() < 0.5 * complain:
            out.append(LogLine(t, caller, "WARN", f"{rpc} slow: {state['latency_ms']:.0f} ms (deadline 500 ms)", callee))
    return out


def _root_line(rng: random.Random, root: str, fault: str, k: int) -> tuple[str, str] | None:
    """What the broken service itself logs. A network delay leaves no trace in its own logs."""
    if fault == "errors" and rng.random() < 0.8:
        return "ERROR", f"unhandled {rng.choice(EXCEPTIONS)} in {RPC[root]}"
    if fault == "cpu" and rng.random() < 0.5:
        return "WARN", f"request queue depth {rng.randint(120, 400)}, all workers busy"
    if fault == "memory" and rng.random() < 0.5:
        return "WARN", f"heap at {min(99, 70 + 3 * k)}% after full GC"
    return None
