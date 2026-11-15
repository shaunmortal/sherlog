"""The open-weights adapter: the unchanged agent loop over a chat completions API, with a fake HTTP transport
(no network, no key)."""
import json

import pytest

import bench
import investigate as inv
import openmodel
import sim

CLAUDE_ONLY = ("thinking", "output_config", "betas", "fallbacks")


def _reply(*calls, reasoning="", content=None, finish="tool_calls", usage=(100, 20)):
    """One chat completion: tool calls as (name, arguments), arguments a dict or the raw string the model sent."""
    tool_calls = [{"id": f"t{i}", "type": "function",
                   "function": {"name": n, "arguments": a if isinstance(a, str) else json.dumps(a)}}
                  for i, (n, a) in enumerate(calls)]
    message = {"role": "assistant", "content": content, "reasoning_content": reasoning, "tool_calls": tool_calls}
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


def _submit(service="payment", confidence=0.9, evidence=("c0",)):
    cause = {"service": service, "fault_type": "errors", "reason": "r", "confidence": confidence,
             "evidence": list(evidence)}
    return ("submit_ranking", {"causes": [cause], "ticket_note": "n", "customer_update": "u"})


class Provider:
    """A fake chat completions endpoint: answers each request with the next scripted (status, body), or raises it."""

    def __init__(self, *replies):
        self.replies, self.requests, self.sleeps = list(replies), [], []

    def post(self, url: str, data: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
        self.requests.append({"url": url, "body": json.loads(data), "headers": headers})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        return status, json.dumps(body).encode()

    def client(self) -> openmodel.Client:
        return openmodel.Client(api_key="test-key", transport=self.post, sleep=self.sleeps.append)

    def bodies(self) -> list[dict]:
        return [r["body"] for r in self.requests]


def _run(provider: Provider, inc=None) -> list[dict]:
    inc = inc or sim.simulate("payment", "errors", seed=3)
    return list(inv.investigate(inc, client=provider.client(), model=openmodel.GLM))


def test_the_agent_loop_runs_unchanged_and_its_answer_goes_through_the_same_verifier():
    provider = Provider((200, _reply(("get_logs", {"service": "checkout"}), reasoning="checkout calls payment")),
                        (200, _reply(_submit(), usage=(300, 40))))
    events = _run(provider)
    verdict = events[-1]

    assert verdict["answer"] == "payment" and verdict["causes"][0]["verified"]
    assert verdict["usage"] == {"input_tokens": 400, "output_tokens": 60, "model_calls": 2}
    assert verdict["model"] == openmodel.GLM
    assert {"type": "thinking", "text": "checkout calls payment"} in events
    assert not any(e["type"] == "error" for e in events)


def test_the_request_is_the_claude_request_translated_with_claude_only_fields_dropped():
    provider = Provider((200, _reply(_submit())))
    _run(provider)
    body, request = provider.bodies()[0], provider.requests[0]

    assert request["url"] == f"{openmodel.BASE_URL}/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert body["model"] == openmodel.GLM and body["max_tokens"] == inv.MAX_TOKENS
    assert body["messages"][0] == {"role": "system", "content": inv.SYSTEM}
    assert body["messages"][1]["role"] == "user" and body["messages"][1]["content"].startswith("ALERT:")
    # the same tools, the same schemas: only the wrapper and the strict flag differ
    assert body["tools"] == [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                               "parameters": t["input_schema"]}} for t in inv.TOOLS]
    assert '"strict"' not in json.dumps(body["tools"])
    assert not set(CLAUDE_ONLY) & set(body) and "reasoning_effort" not in body and "temperature" not in body


def test_tool_calls_and_results_round_trip_in_the_chat_format():
    provider = Provider((200, _reply(("get_logs", {"service": "checkout"}), ("get_metric", '{"service": "payment"'),
                                     reasoning="look at checkout", content="Checking.")),
                        (200, _reply(_submit())))
    events = _run(provider)
    _, assistant, first, second = provider.bodies()[1]["messages"][1:5]

    assert assistant == {
        "role": "assistant", "content": "Checking.", "reasoning_content": "look at checkout",
        "tool_calls": [
            {"id": "t0", "type": "function", "function": {"name": "get_logs", "arguments": '{"service": "checkout"}'}},
            # what the model sent, echoed exactly, even when it is not valid JSON
            {"id": "t1", "type": "function", "function": {"name": "get_metric", "arguments": '{"service": "payment"'}},
        ]}
    tools = [e for e in events if e["type"] == "tool"]
    assert first == {"role": "tool", "tool_call_id": "t0", "content": tools[1]["text"]}
    assert first["content"].startswith("[c1] get_logs(checkout)")
    assert second == {"role": "tool", "tool_call_id": "t1", "content": "[c2] error: arguments must be an object"}


def test_malformed_tool_call_json_is_an_error_result_never_a_crash():
    # a bad lookup: the loop's usual error result, and the investigation goes on
    provider = Provider((200, _reply(("get_metric", "{not json"))), (200, _reply(_submit())))
    events = _run(provider)
    assert next(e for e in events if e["type"] == "tool" and e["id"] == "c1")["error"]
    assert events[-1]["answer"] == "payment"
    # a bad submission: no suspects, so it goes to a human, exactly as a non-object submission from Claude would
    events = _run(Provider((200, _reply(("submit_ranking", '{"causes": [')))))
    assert events[-1]["answer"] is None and events[-1]["escalation"] == "no suspects submitted"
    assert not any(e["type"] == "error" for e in events)


def test_a_reply_without_tool_calls_gets_the_loops_nudge_as_a_user_message():
    provider = Provider((200, _reply(content="I think it is payment.", finish="stop")), (200, _reply(_submit())))
    assert _run(provider)[-1]["answer"] == "payment"
    nudge = {"role": "user", "content": "Call submit_ranking now with your best ranking."}
    assert provider.bodies()[1]["messages"][-1] == nudge


def test_tool_results_come_before_a_trailing_note_in_the_same_turn():
    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t0", "content": "[c1] ...", "is_error": False},
        {"type": "text", "text": "Tool budget used up: call submit_ranking now."}]}]
    assert openmodel.to_chat(model="m", max_tokens=10, system="s", tools=[], messages=messages)["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "tool", "tool_call_id": "t0", "content": "[c1] ..."},
        {"role": "user", "content": "Tool budget used up: call submit_ranking now."},
    ]


@pytest.mark.parametrize("finish, stop", [("stop", "end_turn"), ("length", "max_tokens"),
                                          ("content_filter", "refusal"), ("tool_calls", "end_turn")])
def test_finish_reasons_map_to_the_stop_reasons_the_loop_reads(finish, stop):
    # a reply that names tool calls but sends none is not a tool use
    assert openmodel.from_chat(_reply(content="x", finish=finish)).stop_reason == stop
    assert openmodel.from_chat(_reply(_submit(), finish=finish)).stop_reason == "tool_use"


def test_a_content_filter_stop_ends_the_investigation_like_a_refusal():
    events = _run(Provider((200, _reply(finish="content_filter"))))
    assert {"type": "error", "message": "The model declined to continue this investigation."} in events
    assert events[-1]["answer"] is None


def test_transient_errors_are_retried_with_backoff():
    provider = Provider((429, {}), TimeoutError("read timed out"), (503, {}), (200, _reply(_submit())))
    assert _run(provider)[-1]["answer"] == "payment"
    assert len(provider.requests) == 4
    assert provider.sleeps == [openmodel.BACKOFF_S, 2 * openmodel.BACKOFF_S, 4 * openmodel.BACKOFF_S]


def test_a_bad_request_is_not_retried_and_the_outage_goes_to_a_human():
    provider = Provider((400, {"error": "bad"}))
    events = _run(provider)
    assert len(provider.requests) == 1 and not provider.sleeps
    assert events[-2]["type"] == "error" and events[-1]["escalation"].startswith("investigation failed")


def test_retries_give_up_after_the_last_attempt():
    provider = Provider(*[(503, {})] * openmodel.ATTEMPTS)
    events = _run(provider)
    assert len(provider.requests) == openmodel.ATTEMPTS and len(provider.sleeps) == openmodel.ATTEMPTS - 1
    assert events[-1]["answer"] is None and events[-1]["escalation"].startswith("investigation failed")


def test_a_missing_key_fails_before_any_request(monkeypatch):
    monkeypatch.delenv(openmodel.KEY_ENV, raising=False)
    with pytest.raises(RuntimeError, match=openmodel.KEY_ENV):
        openmodel.Client()


def test_the_benchmark_runs_the_open_variants_on_the_same_tools_and_prices_them():
    assert bench.VARIANTS["open"] == (inv.TOOLS, openmodel.GLM, False)
    assert bench.VARIANTS["hard-open"] == (inv.TOOLS, openmodel.GLM, True)
    assert bench.VARIANTS["hard-open-small"] == (inv.TOOLS, openmodel.GPT_OSS, True)
    assert bench.cost_usd({"input_tokens": 1_000_000, "output_tokens": 1_000_000}, openmodel.GLM) == pytest.approx(5.80)
    provider = Provider((200, _reply(_submit())))
    record = bench.run_case("payment", "errors", 3, *bench.VARIANTS["open"], client=provider.client())
    assert record["model"] == openmodel.GLM and record["truth"]["ai_answer"] == "payment" and not record["errors"]
