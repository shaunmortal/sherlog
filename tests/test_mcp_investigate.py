"""Sherlog as an MCP investigator: the investigation tools, check_answer, reveal_truth and the investigate_outage
prompt, through /mcp. The model is a script here, and no tool calls Claude, so nothing needs a key."""
import hashlib
import json
import re
from functools import cache

import pytest

import app
import byo
import investigate
import mcp_investigation
import mcp_server
import sim
from tests.test_mcp import _modern, _ok, _refused, _rpc, _walk, client

RECORDED_41 = {"recorded": 41}
CALL_ID = re.compile(r"c[0-9a-f]{10}")
REVEALING = {"reveal_truth", "get_recorded_verdict"}  # they show a recorded outage's answer, by design
HIDDEN_KEYS = {"root", "fault", "seed", "decoy", "onset", "truth", "ai_answer"}
HIDDEN_WORDS = re.compile(r"\b(decoy|seed|culprit|onset|truth|root cause)\b", re.I)
LISTED = app.replays()
LEAK_CASES = [41, next(r["id"] for r in LISTED if r["hard"] and not r["caught"]),
              next(r["id"] for r in LISTED if r["caught"])]


def _record(number):
    return app._replays()[number - 1]["record"]


def _simulated(number):
    r = _record(number)
    return sim.simulate(r["root"], r["fault"], r["seed"], r.get("hard", False))


def _look(tool, incident=RECORDED_41, **args):
    """One investigation call: its citation id and its text."""
    answer = _ok(tool, {"incident": incident, **args})
    assert set(answer) == {"call_id", "text"} and CALL_ID.fullmatch(answer["call_id"]), answer
    return answer["call_id"], answer["text"]


def _check(suspects, incident=RECORDED_41):
    return _ok("check_answer", {"incident": incident, "suspects": suspects})


def _suspect(service, confidence, *evidence):
    return {"service": service, "fault_type": "latency", "confidence": confidence, "evidence": list(evidence),
            "reason": f"{service} looks broken."}


def _calls(inc):
    """Every call a model can make about this incident, with valid arguments."""
    yield "top_anomalies", {}
    for s in inc.services:
        for m in inc.measured[s]:
            yield "get_metric", {"service": s, "metric": m}
        yield "get_logs", {"service": s}
        yield "get_dependencies", {"service": s}


# --- the investigation tools: what Sherlog's own agent sees, under a stateless citation id -------------------------

@pytest.mark.parametrize("tool, args", [("top_anomalies", {}), ("get_metric", {"service": "cache", "metric": "latency_ms"}),
                                        ("get_logs", {"service": "cart"}), ("get_dependencies", {"service": "cart"})])
def test_each_investigation_tool_answers_with_the_text_sherlogs_own_agent_sees(tool, args):
    call_id, text = _look(tool, **args)
    assert text == investigate.run_tool(_simulated(41), tool, args, call_id).text
    assert text.startswith(f"[{call_id}] {tool}(")


def test_the_recorded_agents_own_calls_come_back_word_for_word_under_a_new_id():
    made = [e for e in _record(41)["events"] if e["type"] == "tool"]
    assert len(made) == 5
    for event in made:
        call_id, text = _look(event["name"], **event["args"])
        assert text == event["text"].replace(f"[{event['id']}]", f"[{call_id}]", 1)


def test_a_call_id_is_a_hash_of_the_incident_the_tool_and_its_arguments():
    args = {"service": "cache", "metric": "latency_ms"}
    first = _look("get_metric", **args)[0]
    assert _look("get_metric", **args)[0] == first  # stateless: the same call always gets the same id
    key = json.dumps(["recorded:41", "get_metric", args], sort_keys=True).encode()
    assert first == "c" + hashlib.sha256(key).hexdigest()[:10]
    others = {_look("get_metric", service="cache", metric="cpu_pct")[0], _look("get_logs", service="cache")[0],
              _look("get_metric", {"recorded": 40}, **args)[0], _look("get_metric", {"sample": "shop"}, **args)[0]}
    assert len(others) == 4 and first not in others


def test_a_document_gets_the_same_ids_each_time_and_another_document_other_ids():
    doc = byo.sample()
    first = _look("top_anomalies", {"document": doc})[0]
    assert _look("top_anomalies", {"document": json.loads(json.dumps(doc))})[0] == first
    doc["series"]["cart"]["cpu_pct"][0] += 1
    assert _look("top_anomalies", {"document": doc})[0] != first


def test_a_custom_graph_sample_is_investigated_under_its_own_service_names():
    _, text = _look("get_dependencies", {"sample": "custom"}, service="inventory")
    assert "calls (downstream): postgres" in text


IN_41 = {"incident": RECORDED_41}


@pytest.mark.parametrize("tool, arguments, message", [
    ("top_anomalies", {}, "Bad input. incident: Field required"),
    ("top_anomalies", {"incident": {}}, "Bad input. incident: Value error, give exactly one of recorded, sample or "
                                        "document"),
    ("top_anomalies", {"incident": {"recorded": 41, "sample": "shop"}}, "Bad input. incident: Value error, give "
                                                                        "exactly one"),
    ("top_anomalies", {"incident": {"recorded": 0}}, "Bad input. incident.recorded: Input should be greater than or "
                                                     "equal to 1"),
    ("top_anomalies", {"incident": {"sample": "mesh"}}, "Bad input. incident.sample: Input should be 'shop' or "
                                                        "'custom'"),
    ("top_anomalies", {"incident": {"replay": 41}}, "Bad input. incident.replay: Extra inputs are not permitted"),
    ("top_anomalies", {**IN_41, "service": "cache"}, "Bad input. service: Extra inputs are not permitted"),
    ("get_metric", {**IN_41, "service": "cache"}, "Bad input. metric: Field required"),
    ("get_metric", {**IN_41, "service": "nope", "metric": "cpu_pct"}, "Bad input. unknown service 'nope'; known: "
                                                                     "frontend, recommend, checkout"),
    ("get_metric", {**IN_41, "service": "cache", "metric": "disk"}, "Bad input. unknown metric 'disk'; known: "
                                                                   "latency_ms, error_rate, cpu_pct, memory_mb"),
    ("get_logs", {"incident": {"sample": "custom"}, "service": "cart"}, "Bad input. unknown service 'cart'; known: "
                                                                        "gateway"),
    ("get_dependencies", {**IN_41, "service": "x" * 41}, "Bad input. service: String should have at most 40 "
                                                         "characters"),
])
def test_investigation_tools_refuse_bad_input_as_a_tool_error(tool, arguments, message):
    assert _refused(tool, arguments).startswith(message)


def test_a_recorded_number_too_high_names_the_range():
    assert _refused("get_logs", {"incident": {"recorded": len(LISTED) + 1}, "service": "cart"}) == (
        f"No recorded outage with that number: they are numbered 1 to {len(LISTED)}.")


def test_a_document_is_checked_as_an_upload_is_with_each_path_under_incident_document():
    doc = byo.sample()
    doc["series"]["cart"]["cpu_pct"][3] = 140
    route = client.post("/api/triage", json=doc).json()
    assert _refused("top_anomalies", {"incident": {"document": doc}}) == route["detail"].replace(
        "Bad input. ", "Bad input. incident.document.", 1)


def test_the_tool_list_keeps_the_upload_schema_out_of_every_investigation_tool():
    tools = {t["name"]: t for t in _rpc("tools/list")[1]["result"]["tools"]}
    for name in ("top_anomalies", "get_metric", "get_logs", "get_dependencies", "check_answer"):
        schema = tools[name]["inputSchema"]
        assert "incident" in schema["required"] and len(json.dumps(schema)) < len(json.dumps(
            tools["triage_incident"]["inputSchema"])) + 1500
    assert tools["check_answer"]["inputSchema"]["required"] == ["incident", "suspects"]
    assert tools["reveal_truth"]["inputSchema"]["required"] == ["recorded"]


# --- check_answer: the same gate as the product, around any model ------------------------------------------------

def _ids_41():
    return (_look("top_anomalies")[0], _look("get_metric", service="cache", metric="latency_ms")[0],
            _look("get_metric", service="cart", metric="latency_ms")[0])


def test_check_answer_verifies_the_cause_and_its_rollback_stops_the_alarm():
    anomalies, cache, _ = _ids_41()
    verdict = _check([_suspect("cache", 0.9, anomalies, cache)])
    top = verdict["causes"][0]
    assert top["verified"] is True and top["verify_note"] == f"{anomalies}, {cache} show cache abnormal"
    assert verdict["answer"] == "cache" and verdict["escalation"] is None
    assert verdict["blast"]["unexplained"] == [] and verdict["blast"]["better"] is None
    assert verdict["rollback"] == {"service": "cache", "alarm_after": False, "still_abnormal": [],
                                   "cleared": ["cart", "cache", "frontend", "checkout"], "proves": True}
    assert verdict["decision"] == "ship" and verdict["reasons"] == []


def test_check_answer_lets_a_victim_pass_its_citations_and_catches_it_with_the_blast_radius_and_rollback():
    anomalies, _, cart = _ids_41()
    verdict = _check([_suspect("cart", 0.9, anomalies, cart)])
    assert verdict["causes"][0]["verified"] is True and verdict["answer"] == "cart"  # a victim is abnormal too
    assert verdict["blast"]["better"] == "cache" and verdict["rollback"]["proves"] is False
    assert verdict["decision"] == "hand to a human"
    assert verdict["reasons"] == ["cache would explain more of the outage than cart",
                                  "rolling back cart does not stop the alarm"]


def test_check_answer_reports_every_suspect_but_gates_on_the_first():
    anomalies, cache, _ = _ids_41()
    logs = _look("get_logs", service="cart")[0]  # cart's lines complain about cache, so they show cache, not cart
    verdict = _check([_suspect("cache", 0.9, anomalies, cache), _suspect("cart", 0.3, logs), _suspect("db", 0.1)])
    assert [(c["service"], c["verified"]) for c in verdict["causes"]] == [("cache", True), ("cart", False),
                                                                         ("db", False)]
    assert verdict["causes"][1]["verify_note"] == f"none of {logs} shows cart abnormal"
    assert verdict["causes"][2]["verify_note"] == "no evidence cited"
    assert verdict["decision"] == "ship"


def _escalated(verdict, escalation):
    assert verdict["answer"] is None and verdict["escalation"] == escalation
    assert verdict["blast"] is None and verdict["rollback"] is None
    assert verdict["decision"] == "hand to a human" and verdict["reasons"] == [escalation]


def test_check_answer_hands_a_confident_answer_under_80_percent_to_a_human():
    anomalies, cache, _ = _ids_41()
    _escalated(_check([_suspect("cache", 0.6, anomalies, cache)]), "confidence 60% is below 80%")


def test_check_answer_rejects_a_citation_that_is_not_a_call_on_this_incident():
    elsewhere = _look("top_anomalies", {"recorded": 40})[0]
    _escalated(_check([_suspect("cache", 0.95, "c0", elsewhere)]),
               f"top suspect failed verification (cited call(s) c0, {elsewhere} were never made)")


def test_check_answer_rejects_a_citation_that_does_not_show_the_suspect_abnormal():
    payment = _look("get_metric", service="payment", metric="cpu_pct")[0]
    _escalated(_check([_suspect("cache", 0.95, payment)]),
               f"top suspect failed verification (none of {payment} shows cache abnormal)")


def test_check_answer_with_no_suspects_hands_the_outage_to_a_human():
    _escalated(_check([]), "no suspects submitted")


@pytest.mark.parametrize("suspects, message", [
    ([_suspect("cache", 0.9)] * 6, "Bad input. suspects: List should have at most 5 items after validation, not 6"),
    ([_suspect("cache", 1.5)], "Bad input. suspects.0.confidence: Input should be less than or equal to 1"),
    ([_suspect("cache", 0.9, *["c0"] * 21)], "Bad input. suspects.0.evidence: List should have at most 20 items"),
    ([_suspect("cache", 0.9, "c" * 65)], "Bad input. suspects.0.evidence.0: String should have at most 64"),
    ([{**_suspect("cache", 0.9), "fault_type": "disk"}], "Bad input. suspects.0.fault_type: Input should be"),
    ([{"service": "cache", "confidence": 0.9}], "Bad input. suspects.0.evidence: Field required"),
    ([_suspect("cache", 0.9), _suspect("nope", 0.5)], "Bad input. suspects.1.service: unknown service 'nope'; "
                                                     "known: frontend"),
])
def test_check_answer_refuses_bad_suspects_and_limits_their_number_and_citations(suspects, message):
    assert _refused("check_answer", {"incident": RECORDED_41, "suspects": suspects}).startswith(message)


def test_check_answer_on_an_upload_says_plainly_that_it_has_no_rollback_replay():
    incident = {"document": byo.sample()}
    anomalies = _look("top_anomalies", incident)[0]
    verdict = _check([_suspect("cache", 0.9, anomalies)], incident)
    assert verdict["answer"] == "cache" and verdict["decision"] == "ship"
    assert verdict["blast"]["unexplained"] == [] and verdict["rollback"] is None
    assert verdict["note"] == mcp_investigation.NO_ROLLBACK and "no rollback replay" in verdict["note"]


@pytest.mark.parametrize("graph, cause, victim", [("shop", "cache", "cart"), ("custom", "postgres", "inventory")])
def test_check_answer_replays_a_sample_with_the_answer_rolled_back(graph, cause, victim):
    incident = {"sample": graph}
    anomalies = _look("top_anomalies", incident)[0]
    right = _check([_suspect(cause, 0.9, anomalies)], incident)
    assert right["decision"] == "ship" and right["rollback"]["proves"] is True
    assert right["rollback"]["alarm_after"] is False and "note" not in right
    wrong = _check([_suspect(victim, 0.9, anomalies)], incident)
    assert wrong["answer"] == victim and wrong["decision"] == "hand to a human"
    assert wrong["rollback"]["proves"] is False and wrong["blast"]["better"] == cause


# --- reveal_truth: only for recorded outages, to score a model -----------------------------------------------------

def test_reveal_truth_gives_the_cause_and_what_sherlogs_recorded_agent_answered():
    truth = _ok("reveal_truth", {"recorded": 41})
    assert {k: truth[k] for k in ("recorded", "root", "fault", "hard", "decoy")} == {
        "recorded": 41, "root": "cache", "fault": "latency", "hard": False, "decoy": None}
    sherlog = truth["sherlog"]
    assert sherlog["model"] == "claude-opus-5-5" and sherlog["answer"] == "cache" and sherlog["escalation"] is None
    assert sherlog["right"] is True and sherlog["caught"] is False and sherlog["suspects"][0]["service"] == "cache"
    assert truth["zscore_top3"] == ["cart", "cache", "frontend"]  # the no-AI ranking blamed a victim


def test_reveal_truth_names_the_decoy_and_a_caught_wrong_answer():
    caught = LEAK_CASES[2]
    truth, record = _ok("reveal_truth", {"recorded": caught}), _record(caught)
    assert truth["root"] == record["root"] and truth["decoy"] == record["truth"]["decoy"] and truth["hard"] is True
    assert truth["sherlog"]["answer"] != truth["root"] and truth["sherlog"]["right"] is False
    assert truth["sherlog"]["caught"] is True


@pytest.mark.parametrize("arguments, message", [
    ({}, "Bad input. recorded: Field required"),
    ({"recorded": 0}, "Bad input. recorded: Input should be greater than or equal to 1"),
    ({"recorded": len(LISTED) + 1}, f"No recorded outage with that number: they are numbered 1 to {len(LISTED)}."),
    ({"sample": "shop"}, "Bad input. recorded: Field required"),
])
def test_reveal_truth_refuses_anything_but_a_recorded_outage(arguments, message):
    assert _refused("reveal_truth", arguments).startswith(message)


# --- no tool but the two reveals gives the answer away -------------------------------------------------------------

def _verified_suspect(incident, inc, service):
    """service as the top suspect, citing every call that could show it abnormal."""
    cited = [_look("top_anomalies", incident)[0], _look("get_dependencies", incident, service=service)[0],
             *(_look("get_metric", incident, service=service, metric=m)[0] for m in inc.measured[service])]
    return _check([_suspect(service, 0.9, *cited)], incident)


@cache
def _answers(number, as_document):
    """Every answer the incident tools give about one recorded outage, by call, with ids blanked so the outage can
    be compared with its own telemetry uploaded as a document."""
    inc = _simulated(number)
    incident = {"document": byo.export(inc)} if as_document else {"recorded": number}
    out = {}
    for name, args in _calls(inc):
        call_id, text = _look(name, incident, **args)
        out[(name, json.dumps(args))] = text.replace(call_id, "")
    for service in inc.services:
        verdict = _verified_suspect(incident, inc, service)
        out[("check_answer", service)] = json.loads(re.sub(r"c[0-9a-f]{10}", "", json.dumps(verdict)))
    return out


@pytest.mark.parametrize("number", LEAK_CASES)
def test_no_tool_but_the_reveals_names_the_culprit_fault_seed_or_decoy(number):
    record = _record(number)
    others = [_ok("list_recorded_outages"), _ok("sample_incident"), _ok("benchmark_summary"),
              _ok("triage_incident", {"incident": byo.export(_simulated(number))})]
    incident_answers = list(_answers(number, False).values())
    for answer in [*others, *incident_answers]:
        assert not {k for node in _walk(answer) if isinstance(node, dict) for k in node} & HIDDEN_KEYS
    for answer in incident_answers:
        values = [v for v in _walk(answer) if not isinstance(v, (dict, list))]
        assert record["seed"] not in values and str(record["seed"]) not in values
        assert not HIDDEN_WORDS.search(json.dumps(answer)), answer
    exercised = {name for name, _ in _answers(number, False)} | {
        "list_recorded_outages", "sample_incident", "benchmark_summary", "triage_incident"}
    assert exercised == {t["name"] for t in _rpc("tools/list")[1]["result"]["tools"]} - REVEALING


@pytest.mark.parametrize("number", LEAK_CASES)
def test_the_investigation_reads_only_what_the_telemetry_itself_shows(number):
    """Every tool answers the same about a recorded outage as about its telemetry uploaded as a document, which has
    no culprit, fault, seed or decoy in it: only the ids differ, and check_answer only in the rollback replay."""
    recorded, document = _answers(number, False), _answers(number, True)
    assert recorded.keys() == document.keys()
    for key, answer in recorded.items():
        if key[0] != "check_answer":
            assert answer == document[key], key
            continue
        assert document[key]["rollback"] is None and document[key]["note"] == mcp_investigation.NO_ROLLBACK
        assert {k: v for k, v in answer.items() if k not in ("rollback", "reasons", "decision")} == {
            k: v for k, v in document[key].items() if k not in ("rollback", "reasons", "decision", "note")}, key


# --- the investigate_outage prompt ---------------------------------------------------------------------------------

def test_prompts_list_offers_investigate_outage_with_one_optional_argument():
    status, answer = _rpc("prompts/list")
    (prompt,) = answer["result"]["prompts"]
    assert status == 200 and prompt["name"] == "investigate_outage" and prompt["title"]
    assert "check_answer" in prompt["description"]
    assert [(a["name"], a["required"]) for a in prompt["arguments"]] == [("outage", False)]


def _prompt_text(arguments=None):
    status, answer = _rpc("prompts/get", {"name": "investigate_outage",
                                          **({} if arguments is None else {"arguments": arguments})})
    assert status == 200, answer
    (message,) = answer["result"]["messages"]
    assert message["role"] == "user" and message["content"]["type"] == "text" and answer["result"]["description"]
    return message["content"]["text"]


@pytest.mark.parametrize("arguments, incident", [({"outage": "41"}, {"recorded": 41}), (None, {"recorded": 41}),
                                                 ({"outage": " shop "}, {"sample": "shop"}),
                                                 ({"outage": "custom"}, {"sample": "custom"})])
def test_investigate_outage_hands_the_model_the_protocol_for_one_incident(arguments, incident):
    text = _prompt_text(arguments)
    assert f'"incident": {json.dumps(incident)}' in text
    steps = ("top_anomalies", "get_metric", "get_logs", "get_dependencies", "call id", "check_answer", "80%",
             "hand to a human")
    assert [s for s in steps if s not in text] == []
    assert text.index("top_anomalies") < text.index("check_answer")
    assert ("reveal_truth" in text) == ("recorded" in incident)


@pytest.mark.parametrize("params, message", [
    ({"name": "triage"}, "Unknown prompt: 'triage'. prompts/list names the prompts."),
    ({}, "Unknown prompt: 'None'."),
    ({"name": "investigate_outage", "arguments": {"outage": "999"}}, "outage must be a recorded outage's number"),
    ({"name": "investigate_outage", "arguments": {"outage": "abc"}}, "outage must be a recorded outage's number"),
    ({"name": "investigate_outage", "arguments": {"outage": 41}}, "Prompt arguments must be strings."),
    ({"name": "investigate_outage", "arguments": ["41"]}, "params.arguments must be an object."),
    ({"name": "investigate_outage", "arguments": {"number": "41"}}, "Unknown argument for investigate_outage: "
                                                                    "'number'."),
])
def test_prompts_get_refuses_bad_params_as_invalid_params(params, message):
    status, answer = _rpc("prompts/get", params)
    assert status == 200 and answer["error"]["code"] == mcp_server.INVALID_PARAMS
    assert answer["error"]["message"].startswith(message)


def test_modern_prompts_list_can_be_cached_and_prompts_get_checks_its_mcp_name_header():
    listed = _modern("prompts/list")[1]["result"]
    assert listed["resultType"] == "complete" and listed["ttlMs"] > 0 and listed["cacheScope"] == "public"
    status, answer = _modern("prompts/get", {"name": "investigate_outage", "arguments": {"outage": "41"}})
    assert status == 200 and answer["result"]["resultType"] == "complete"
    status, answer = _modern("prompts/get", {"name": "investigate_outage"}, headers={"mcp-name": "other"})
    assert status == 400 and answer["error"]["code"] == mcp_server.HEADER_MISMATCH


# --- one whole investigation, as a model runs it --------------------------------------------------------------------

def test_a_scripted_model_investigates_recorded_outage_41_from_the_prompt_to_its_score():
    incident = json.loads(re.search(r'"incident": (\{.*?\})', _prompt_text({"outage": "41"}))[1])
    anomalies, shown = _look("top_anomalies", incident)
    assert shown.splitlines()[1].split()[0] == "cart"  # the loudest service is a victim
    _, deps = _look("get_dependencies", incident, service="cart")
    assert "calls (downstream): cache (latency_ms z=" in deps  # cart calls cache, which is abnormal too
    cache, _ = _look("get_metric", incident, service="cache", metric="latency_ms")
    logs, _ = _look("get_logs", incident, service="cart")
    right = _check([_suspect("cache", 0.9, anomalies, cache, logs), _suspect("cart", 0.2, anomalies)], incident)
    assert right["decision"] == "ship" and right["answer"] == "cache" and right["rollback"]["proves"] is True
    wrong = _check([_suspect("cart", 0.9, anomalies)], incident)
    assert wrong["decision"] == "hand to a human" and wrong["rollback"]["proves"] is False
    truth = _ok("reveal_truth", incident)
    assert truth["root"] == right["answer"] == truth["sherlog"]["answer"]
