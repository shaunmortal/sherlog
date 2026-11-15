"""The MCP endpoint at /mcp through FastAPI's TestClient: both protocol eras, every tool's answers and refusals, the
JSON-RPC and HTTP errors, and /llms.txt. No tool calls Claude, so nothing here needs a key."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

import app
import bench
import byo
import mcp_server

client = TestClient(app.app)
TOOLS = {"list_recorded_outages", "get_recorded_verdict", "triage_incident", "sample_incident", "benchmark_summary"}
MODERN = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"}}


def _post(message, headers=None):
    res = client.post("/mcp", content=message if isinstance(message, (bytes, str)) else json.dumps(message),
                      headers={"content-type": "application/json", **(headers or {})})
    return res.status_code, (res.json() if res.content else None)


def _rpc(method, params=None, rid=1, headers=None):
    return _post({"jsonrpc": "2.0", "id": rid, "method": method, **({} if params is None else {"params": params})},
                 headers)


def _modern(method, params=None, rid=1, headers=None):
    """A 2026-07-28 request: _meta in the body, mirrored in the headers, as a modern client sends it."""
    mirrored = {"mcp-protocol-version": MODERN, "mcp-method": method,
                **({"mcp-name": params["name"]} if method == "tools/call" else {})}
    return _rpc(method, {**(params or {}), "_meta": META}, rid, {**mirrored, **(headers or {})})


def _call(name, arguments=None):
    status, answer = _rpc("tools/call", {"name": name, **({} if arguments is None else {"arguments": arguments})})
    assert status == 200 and "error" not in answer, answer
    return answer["result"]


def _ok(name, arguments=None):
    """A tool's structured answer, after checking the text block carries the same JSON."""
    result = _call(name, arguments)
    assert result["isError"] is False and [c["type"] for c in result["content"]] == ["text"]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result["structuredContent"]


def _refused(name, arguments=None):
    result = _call(name, arguments)
    assert result["isError"] is True
    return result["content"][0]["text"]


# --- the handshake era: initialize, then plain requests, with no session -----------------------------------------

@pytest.mark.parametrize("requested, agreed", [("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"),
                                               ("2025-03-26", "2025-03-26"), ("2024-11-05", "2025-11-25"),
                                               (MODERN, "2025-11-25")])
def test_initialize_agrees_the_clients_version_or_offers_the_newest_handshake_one(requested, agreed):
    res = client.post("/mcp", json={"jsonrpc": "2.0", "id": "init", "method": "initialize",
                                    "params": {"protocolVersion": requested, "capabilities": {},
                                               "clientInfo": {"name": "test", "version": "1"}}})
    assert res.status_code == 200 and res.headers["content-type"] == "application/json"
    assert "mcp-session-id" not in res.headers  # stateless: every request stands on its own
    assert res.json() == {"jsonrpc": "2.0", "id": "init", "result": {
        "protocolVersion": agreed, "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "sherlog", "version": app._version()}, "instructions": app.MCP_INSTRUCTIONS}}


def test_initialize_without_a_version_is_invalid_params():
    status, answer = _rpc("initialize", {"capabilities": {}})
    assert status == 200 and answer["error"]["code"] == mcp_server.INVALID_PARAMS


@pytest.mark.parametrize("headers", [{}, {"mcp-protocol-version": "2025-06-18"}, {"mcp-protocol-version": MODERN}])
def test_notifications_get_202_with_no_body(headers):
    res = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    assert res.status_code == 202 and res.content == b""


def test_a_posted_response_is_accepted_in_the_handshake_era_and_refused_in_the_modern_one():
    response = {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert _post(response) == (202, None)
    status, answer = _post(response, {"mcp-protocol-version": MODERN})
    assert status == 400 and answer["error"]["code"] == mcp_server.INVALID_REQUEST


def test_ping_answers_an_empty_result():
    assert _rpc("ping", rid="p") == (200, {"jsonrpc": "2.0", "id": "p", "result": {}})


def test_tools_list_names_five_read_only_tools_each_with_a_closed_object_schema():
    status, answer = _rpc("tools/list")
    tools = answer["result"]["tools"]
    assert status == 200 and {t["name"] for t in tools} == TOOLS and len(tools) == len(TOOLS)
    for tool in tools:
        assert tool["title"] and len(tool["description"]) > 80
        assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["openWorldHint"] is False
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema.get("required", [])) <= set(schema["properties"])
        refs = [v for v in _walk(schema) if isinstance(v, dict) and "$ref" in v]
        assert all(ref["$ref"].removeprefix("#/$defs/") in schema["$defs"] for ref in refs)  # every $ref is local
    required = {t["name"]: t["inputSchema"].get("required", []) for t in tools}
    assert required["get_recorded_verdict"] == ["number"] and required["triage_incident"] == ["incident"]


def _walk(node):
    yield node
    for child in node.values() if isinstance(node, dict) else node if isinstance(node, list) else []:
        yield from _walk(child)


# --- every tool: what it answers, and how it refuses --------------------------------------------------------------

def test_list_recorded_outages_is_the_replay_list():
    assert _ok("list_recorded_outages") == {"outages": client.get("/api/replays").json()}


def test_get_recorded_verdict_gives_the_walk_through_with_every_check():
    answer = _ok("get_recorded_verdict", {"number": 41})
    assert {k: answer[k] for k in ("id", "hard", "caught")} == {"id": 41, "hard": False, "caught": False}
    assert [c["id"] for c in answer["tool_calls"]] == ["c0", "c1", "c2", "c3", "c4"]
    verdict, truth = answer["verdict"], answer["truth"]
    assert verdict["answer"] == "cache" and verdict["causes"][0]["verified"] is True
    assert verdict["blast"]["unexplained"] == [] and verdict["rollback"]["proves"] is True
    assert truth["root"] == "cache" and truth["baseline"][0] == "cart"  # the z-score ranking blamed a victim


def test_get_recorded_verdict_shows_a_caught_wrong_answer():
    caught = next(r["id"] for r in client.get("/api/replays").json() if r["caught"])
    answer = _ok("get_recorded_verdict", {"number": caught})
    assert answer["caught"] and answer["verdict"]["rollback"]["proves"] is False
    assert answer["verdict"]["answer"] == answer["truth"]["ai_answer"] != answer["truth"]["root"]


@pytest.mark.parametrize("arguments, message", [
    ({"number": 0}, "Bad input. number: Input should be greater than or equal to 1"),
    ({"number": "first"}, "Bad input. number: Input should be a valid integer"),
    ({}, "Bad input. number: Field required"),
    ({"number": 41, "verbose": True}, "Bad input. verbose: Extra inputs are not permitted"),
])
def test_get_recorded_verdict_refuses_bad_arguments_as_a_tool_error(arguments, message):
    assert _refused("get_recorded_verdict", arguments).startswith(message)


def test_get_recorded_verdict_names_the_range_for_a_number_too_high():
    last = len(client.get("/api/replays").json())
    assert _refused("get_recorded_verdict", {"number": last + 1}) == (
        f"No recorded outage with that number: they are numbered 1 to {last}.")


@pytest.mark.parametrize("graph", ["shop", "custom"])
def test_sample_incident_is_the_downloadable_sample(graph):
    assert _ok("sample_incident", {"graph": graph}) == client.get(f"/api/sample-incident?graph={graph}").json()


def test_sample_incident_defaults_to_the_shop_and_refuses_another_graph():
    assert _ok("sample_incident") == byo.sample()
    assert _refused("sample_incident", {"graph": "mesh"}).startswith("Bad input. graph: Input should be 'shop' or")


@pytest.mark.parametrize("sample", [byo.sample, byo.graph_sample])
def test_triage_incident_answers_as_the_triage_route_does(sample):
    doc = sample()
    assert _ok("triage_incident", {"incident": doc}) == client.post("/api/triage", json=doc).json()


def test_triage_incident_refuses_a_bad_document_with_the_upload_routes_messages():
    doc = byo.sample()
    doc["series"]["cart"]["cpu_pct"][3] = 140
    route = client.post("/api/triage", json=doc).json()
    result = _call("triage_incident", {"incident": doc})
    assert result["isError"] is True
    assert result["content"][0]["text"] == route["detail"].replace("Bad input. ", "Bad input. incident.", 1)
    tool_errors = result["structuredContent"]["errors"]
    assert [e["msg"] for e in tool_errors] == [e["msg"] for e in route["errors"]]
    assert [e["loc"][2:] for e in tool_errors] == [e["loc"][1:] for e in route["errors"]]


def test_triage_incident_needs_an_incident():
    assert _refused("triage_incident", {}) == "Bad input. incident: Field required"


def test_benchmark_summary_reports_runs_evidence_with_its_caveats():
    answer, evidence = _ok("benchmark_summary"), json.loads(bench.EVIDENCE.read_text())
    normal, hard = answer["normal"], answer["hard"]
    assert normal["outages"] == evidence["agent"]["outages"] and hard["outages"] == evidence["hard-agent"]["outages"]
    assert normal["seeds"].startswith(str(bench.SEED_BASE)) and hard["seeds"].startswith(str(bench.HARD_SEED_BASE))
    assert list(hard["methods"]) == list(app.HEADLINE_METHODS)
    assert hard["methods"]["agent"]["cause_first"] == evidence["hard-agent"]["agent_ac1"]
    assert hard["no_ai"]["call_graph_rule_cause_first"] == evidence["hard-agent"]["graph_ac1"]
    assert hard["no_ai"]["call_graph_rule_wrong"] == evidence["hard-agent"]["graph_wrong"]
    # the wrong hard-mode answers are the ones the rollback caught, as the README's tables say
    assert all(m["wrong_answers"] == m["rollback_caught_wrong"] for m in hard["methods"].values())
    assert any("simulated" in c for c in answer["caveats"]) and any("call-graph rule" in c for c in answer["caveats"])


@pytest.mark.parametrize("name", ["list_recorded_outages", "benchmark_summary"])
def test_tools_without_arguments_refuse_unknown_ones(name):
    assert _refused(name, {"extra": 1}) == "Bad input. extra: Extra inputs are not permitted"


def test_a_tool_that_fails_unexpectedly_is_an_internal_error(monkeypatch):
    def broken():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(app, "_evidence", broken)
    status, answer = _rpc("tools/call", {"name": "benchmark_summary", "arguments": {}})
    assert status == 200 and answer["error"] == {"code": mcp_server.INTERNAL_ERROR,
                                                 "message": "benchmark_summary failed unexpectedly."}


# --- JSON-RPC and HTTP errors -------------------------------------------------------------------------------------

@pytest.mark.parametrize("params", [{"name": "run_live_investigation", "arguments": {}}, {"arguments": {}},
                                    {"name": "benchmark_summary", "arguments": [1]}])
def test_an_unknown_tool_or_arguments_that_are_not_an_object_are_invalid_params(params):
    status, answer = _rpc("tools/call", params)
    assert status == 200 and answer["error"]["code"] == mcp_server.INVALID_PARAMS


def test_bad_json_is_a_parse_error():
    for body in (b"{", b"\xff", b'{"jsonrpc": "2.0", "id": NaN, "method": "ping"}'):
        assert _post(body) == (400, {"jsonrpc": "2.0", "id": None, "error": {
            "code": mcp_server.PARSE_ERROR, "message": "Parse error: the body is not JSON."}})


@pytest.mark.parametrize("message", [
    [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],  # a batch
    {"jsonrpc": "1.0", "id": 1, "method": "ping"},
    {"jsonrpc": "2.0", "id": None, "method": "ping"},
    {"jsonrpc": "2.0", "id": True, "method": "ping"},
    {"jsonrpc": "2.0", "id": 1, "method": 5},
    {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]},
    {"jsonrpc": "2.0", "id": 1},
])
def test_a_message_that_is_not_one_json_rpc_request_is_an_invalid_request(message):
    status, answer = _post(message)
    assert status == 400 and answer["error"]["code"] == mcp_server.INVALID_REQUEST


def test_an_unknown_method_is_method_not_found():
    assert _rpc("resources/list", rid=3) == (200, {"jsonrpc": "2.0", "id": 3, "error": {
        "code": mcp_server.METHOD_NOT_FOUND, "message": "Method not found: resources/list"}})
    status, answer = _modern("resources/list")
    assert status == 404 and answer["error"]["code"] == mcp_server.METHOD_NOT_FOUND  # the modern era's status


def test_a_body_over_the_cap_is_413_declared_or_streamed():
    too_big = b" " * (app.MAX_BODY_MB * 1_000_000 + 1)
    for body in (too_big, iter([too_big[:1_000_000]] * (app.MAX_BODY_MB + 1))):
        res = client.post("/mcp", content=body)
        assert res.status_code == 413
        assert res.json() == {"jsonrpc": "2.0", "id": None, "error": {
            "code": mcp_server.INVALID_REQUEST, "message": f"The request body is over {app.MAX_BODY_MB} MB."}}


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_get_and_delete_are_405_with_an_allow_header(method):
    res = client.request(method, "/mcp")
    assert res.status_code == 405 and res.headers["allow"] == "POST"


@pytest.mark.parametrize("origin, allowed", [("https://evil.example", False), ("null", False),
                                             ("http://testserver", True), ("http://localhost:6274", True),
                                             ("http://127.0.0.1:3000", True)])
def test_a_browser_origin_must_be_this_server_or_this_machine(origin, allowed):
    status, answer = _rpc("ping", headers={"origin": origin})
    assert (status, answer.get("result")) == ((200, {}) if allowed else (403, None))


# --- the modern era: per-request metadata, mirrored in headers, no handshake ---------------------------------------

def test_server_discover_names_every_version_and_the_tools_capability():
    status, answer = _modern("server/discover")
    assert status == 200 and answer["result"] == {
        "supportedVersions": [*mcp_server.MODERN, *mcp_server.LEGACY], "capabilities": {"tools": {}},
        "instructions": app.MCP_INSTRUCTIONS, "ttlMs": mcp_server.LIST_TTL_MS, "cacheScope": "public",
        "resultType": "complete", "_meta": {mcp_server.SERVER_INFO_KEY: {"name": "sherlog", "version": app._version()}}}


def test_modern_results_say_they_are_complete_and_the_tool_list_can_be_cached():
    listed = _modern("tools/list")[1]["result"]
    assert listed["resultType"] == "complete" and listed["cacheScope"] == "public" and listed["ttlMs"] > 0
    assert {t["name"] for t in listed["tools"]} == TOOLS
    called = _modern("tools/call", {"name": "get_recorded_verdict", "arguments": {"number": 41}})[1]["result"]
    assert called["resultType"] == "complete" and called["structuredContent"]["truth"]["root"] == "cache"


def test_modern_tools_call_reads_a_base64_mcp_name_header():
    encoded = "=?base64?" + base64.b64encode(b"benchmark_summary").decode() + "?="
    status, answer = _modern("tools/call", {"name": "benchmark_summary"}, headers={"mcp-name": encoded})
    assert status == 200 and answer["result"]["isError"] is False


@pytest.mark.parametrize("headers, code", [
    ({"mcp-method": "tools/list"}, mcp_server.HEADER_MISMATCH),
    ({"mcp-name": "sample_incident"}, mcp_server.HEADER_MISMATCH),
    ({"mcp-name": "=?base64?not base64?="}, mcp_server.HEADER_MISMATCH),
])
def test_modern_headers_must_match_the_body(headers, code):
    status, answer = _modern("tools/call", {"name": "benchmark_summary"}, headers=headers)
    assert status == 400 and answer["error"]["code"] == code


def test_a_modern_request_needs_its_meta():
    status, answer = _rpc("tools/list", headers={"mcp-protocol-version": MODERN, "mcp-method": "tools/list"})
    assert status == 400 and answer["error"]["code"] == mcp_server.INVALID_PARAMS


def test_a_version_this_server_does_not_speak_is_refused_with_the_ones_it_does():
    meta = {**META, "io.modelcontextprotocol/protocolVersion": "2099-01-01"}
    status, answer = _rpc("tools/list", {"_meta": meta}, headers={"mcp-protocol-version": "2099-01-01",
                                                                  "mcp-method": "tools/list"})
    assert status == 400 and answer["error"] == {"code": mcp_server.UNSUPPORTED_VERSION,
                                                 "message": "Unsupported protocol version",
                                                 "data": {"supported": [*mcp_server.MODERN, *mcp_server.LEGACY],
                                                          "requested": "2099-01-01"}}
    res = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                      headers={"mcp-protocol-version": "2099-01-01"})
    assert res.status_code == 400 and res.json()["error"]["code"] == mcp_server.UNSUPPORTED_VERSION


# --- /llms.txt and the links to it -------------------------------------------------------------------------------

def test_llms_txt_is_plain_text_that_maps_every_route_and_tool():
    res = client.get("/llms.txt")
    assert res.status_code == 200 and res.headers["content-type"] == "text/plain; charset=utf-8"
    assert res.text.startswith("# Sherlog\n\n> ")
    routes = {r.path for r in app.app.routes if getattr(r, "include_in_schema", False)}
    assert routes and all(path in res.text for path in routes - {"/", "/console", "/status"})
    assert all(f"`{name}`" in res.text for name in TOOLS) and "simulated" in res.text
    head = client.head("/llms.txt")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-type"] == res.headers["content-type"]


@pytest.mark.parametrize("path", ["/", "/console"])
def test_the_story_page_and_console_footers_link_the_connector_and_llms_txt(path):
    page = client.get(path).text
    footer = page[page.index("<footer"):page.index("</footer>")]
    assert 'href="/llms.txt"' in footer
    assert 'href="https://github.com/shaunmortal/sherlog/blob/main/docs/mcp.md"' in footer
