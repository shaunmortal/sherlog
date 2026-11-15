"""The benchmark adapter (scripts/benchmark/adapter.py) on synthetic rows: the window, resampling, the metric
mapping, the call graph, log levels, and that its documents pass the upload checks unchanged; and the RE3 split."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts" / "benchmark")]

import adapter  # noqa: E402
import byo  # noqa: E402
import investigate as inv  # noqa: E402
import sim  # noqa: E402
import split  # noqa: E402

INJECT = 1_700_000_000
START = adapter.window_start(INJECT)
SECONDS = list(range(START - 120, START + sim.WINDOW_TICKS * sim.TICK_SECONDS + 120))  # 1 s rows, wider window


def _col(f) -> list[float]:
    return [f(t) for t in SECONDS]


def test_the_injection_lands_on_the_first_tick_after_the_baseline():
    assert START + sim.BASELINE_TICKS * sim.TICK_SECONDS == INJECT
    ticks = adapter.resample(SECONDS, _col(lambda t: 1.0 if t >= INJECT else 0.0), START)
    assert ticks[sim.BASELINE_TICKS - 1] == 0.0 and ticks[sim.BASELINE_TICKS] == 1.0


def test_resampling_averages_each_tick_and_holds_over_gaps():
    ticks = adapter.resample(SECONDS, _col(float), START)
    assert len(ticks) == sim.WINDOW_TICKS and ticks[0] == START + 2  # mean of the tick's 5 one-second readings
    gappy = [t for t in SECONDS if not START + 50 <= t < START + 60]  # ticks 10 and 11 have no reading
    ticks = adapter.resample(gappy, [1.0 * t for t in gappy], START)
    assert ticks[10] == ticks[11] == ticks[9]
    assert adapter.resample(SECONDS, [float("nan")] * len(SECONDS), START) is None
    late = [t for t in SECONDS if t >= START + 20]  # data starts at tick 4: earlier ticks take its first reading
    assert adapter.resample(late, [7.0] * len(late), START)[:4] == [7.0] * 4


def test_metric_columns_map_onto_sherlog_names_and_units():
    columns = {
        "time": _col(float), "cart_latency-50": _col(lambda t: 0.01), "cart_latency-90": _col(lambda t: 0.02),
        "cart_cpu": _col(lambda t: 0.5), "cart_workload": _col(lambda t: 10.0), "cart_error": _col(lambda t: 2.0),
        "db_mem": _col(lambda t: 3e8), "db_diskio": _col(lambda t: 0.0), "web_error": _col(lambda t: 1.0),
    }
    series = adapter.series_of(columns, SECONDS, START)
    assert list(series) == ["cart", "db", "web"]
    assert list(series["cart"]) == ["latency_ms", "error_rate", "cpu", "workload"]  # p50 dropped, p90 in ms
    assert series["cart"]["latency_ms"][0] == 20.0 and series["cart"]["error_rate"][0] == 0.2
    assert list(series["db"]) == ["mem", "diskio"] and list(series["web"]) == ["errors"]  # no workload: kept raw
    idle = adapter.series_of({"pay_latency-90": _col(lambda t: 0.0)}, SECONDS, START)
    assert idle["pay"]["latency_ms"][0] == sim.LIMITS["latency_ms"][0]  # no traffic: clamped to the floor
    proxy = adapter.series_of({"PassthroughCluster_workload": _col(lambda t: 1.0)}, SECONDS, START)
    assert list(proxy) == ["passthroughcluster"]  # a valid upload name; the scorer lower-cases names anyway


def test_the_call_graph_comes_from_parent_spans_and_skips_services_without_metrics():
    spans = [("a", "", "frontend"), ("b", "a", "proxy"), ("c", "b", "cart"), ("d", "a", "frontend"),
             ("e", "c", "cart"), ("f", "a", "catalog")]
    calls = adapter.calls_from_spans(spans, ("frontend", "cart", "catalog"))
    assert calls == {"frontend": {"cart", "catalog"}, "cart": set(), "catalog": set()}
    renamed = adapter.calls_from_spans([("a", "", "frontendservice"), ("b", "a", "cart")], ("frontend", "cart"))
    assert renamed == {"frontend": {"cart"}, "cart": set()}


def test_a_datastore_named_after_a_service_is_its_own():
    services = ("carts", "carts-db", "ts-order-service", "ts-order-mongo", "ts-order-other-service",
                "ts-order-other-mongo", "session-db")
    calls = adapter.datastore_calls(services)
    assert calls["carts"] == {"carts-db"} and calls["ts-order-service"] == {"ts-order-mongo"}
    assert calls["ts-order-other-service"] == {"ts-order-other-mongo"} and not calls["session-db"]
    graph = adapter.merge(calls, adapter.hand_calls("re2-ss", ("front-end", "carts", "session-db")))
    assert graph["front-end"] == ["carts", "session-db"]


def test_log_levels_are_read_from_the_line_or_guessed_from_its_words():
    assert adapter.level_of("2024-01-23 05:26:00.898  INFO 1 --- [exec-9] Pay Error. Not allowed") == "INFO"
    assert adapter.level_of("2024-01-23 05:26:00.898 ERROR 1 --- [exec-9] boom") == "ERROR"
    assert adapter.level_of("ts=2024-01-19T11:25:41Z level=warn caller=x.go:1 msg=slow") == "WARN"
    assert adapter.level_of('{"severity":"error","message":"could not charge"}') == "ERROR"
    assert adapter.level_of("rpc error: code = Unavailable desc = connection refused") == "ERROR"
    assert adapter.level_of("request timed out after 3s") == "WARN"
    assert adapter.level_of("serving product page") == "INFO"


def test_log_lines_keep_warnings_and_errors_of_known_services_in_the_window():
    rows = [(INJECT + 1, "cart", "request failed:\n  connection refused"), (INJECT + 2, "cart", "all good"),
            (INJECT + 3, "chaos-agent", "failed"), (START - 1, "cart", "failed early"), (INJECT, "cart", "  ")]
    lines = adapter.log_lines(rows, ("cart",), START)
    assert lines == [{"tick": sim.BASELINE_TICKS, "service": "cart", "level": "WARN",
                      "text": "request failed: connection refused"}]
    many = [(INJECT + i % 60, "cart", f"failed {i}") for i in range(byo.MAX_LOG_LINES + 50)]
    assert len(adapter.log_lines(many, ("cart",), START)) == byo.MAX_LOG_LINES


def _doc() -> dict:
    slow = lambda t: 0.05 + (0.4 if t >= INJECT else 0.0)  # noqa: E731
    wobble = lambda base: (lambda t: base * (1.02 if t % 2 else 0.98))  # noqa: E731
    columns = {"frontend_latency-90": _col(lambda t: slow(t) + 0.02 * (t % 2)), "frontend_cpu": _col(wobble(3.0)),
               "cart_latency-90": _col(lambda t: slow(t) + 0.001 * (t % 2)), "cart_cpu": _col(wobble(1.0)),
               "cart-db_mem": _col(wobble(2e8))}
    series = adapter.series_of(columns, SECONDS, START)
    calls = adapter.merge(adapter.calls_from_spans([("a", "", "frontend"), ("b", "a", "cart")], tuple(series)),
                          adapter.datastore_calls(tuple(series)))
    logs = adapter.log_lines([(INJECT + 10, "frontend", "rpc cart.GetCart failed: deadline exceeded")],
                             tuple(series), START)
    return adapter.to_doc(series, calls, logs)


def test_a_converted_case_is_a_valid_upload_that_sherlog_reads_as_the_benchmark_meant():
    doc = _doc()
    assert doc["calls"] == {"cart": ["cart-db"], "cart-db": [], "frontend": ["cart"]}
    assert doc["metrics"] == ["latency_ms", "cpu", "mem"]
    inc = byo.to_incident(byo.IncidentDoc.model_validate(doc))
    assert inc.alarm == sim.BASELINE_TICKS + sim.ALARM_SUSTAIN_TICKS - 1
    assert inc.logs[0].about == "cart"  # the caller's complaint names the callee
    assert inv.graph_ranking(inc)[0] == "cart"  # cart explains frontend's slowness through the call graph


def test_the_re3_split_is_a_tenth_of_the_cases_and_covers_every_fault_type():
    runs = ROOT / "runs" / "real-benchmark" / "re3"
    answers = [json.loads(line) for line in (runs / "answers.jsonl").read_text().splitlines()]
    out = split.split_re3(answers)
    assert out == json.loads((runs / "split.json").read_text())  # the committed freeze is this rule's output
    by_id = {a["case_id"]: a for a in answers}
    dev = [by_id[c] for c in out["dev"]]
    assert len(dev) <= len(answers) // 10 and {a["fault"] for a in dev} == {a["fault"] for a in answers}
    assert sorted(a["dataset"] for a in dev) == ["re3-ob"] * 3 + ["re3-ss"] * 3 + ["re3-tt"] * 3
    assert all(a["repetition"] == 1 for a in dev) and not set(out["dev"]) & set(out["test"])
    # both suites record the same three systems, so they share its hand-written calls
    assert adapter.hand_calls("re3-ss", ("front-end", "carts")) == adapter.hand_calls("re2-ss", ("front-end", "carts"))
