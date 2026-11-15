"""A scripted investigation of recorded outage 41 over MCP, with the official MCP Python client: the steps a model
takes with the investigate_outage prompt, a right answer and a wrong one through check_answer, then the truth. No AI
runs; the script plays the model. See docs/mcp.md.

    uv run --with mcp python scripts/investigate_over_mcp.py [URL] [--mode auto|legacy]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re

from mcp import Client

LIVE = "https://sherlog-jade.vercel.app/mcp"


def _first_lines(text: str, n: int = 3) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines()[:n])


async def investigate(url: str, mode: str) -> dict:
    async with Client(url, mode=mode) as client:
        print(f"connected: protocol {client.protocol_version}")
        prompt = await client.get_prompt("investigate_outage", {"outage": "41"})
        incident = json.loads(re.search(r'"incident": (\{.*?\})', prompt.messages[0].content.text)[1])
        print(f"prompt investigate_outage(41) -> incident {incident}")

        async def call(tool: str, **args) -> dict:
            result = await client.call_tool(tool, {"incident": incident, **args} if tool != "reveal_truth" else args)
            if result.is_error:
                raise SystemExit(f"{tool} failed: {result.content[0].text}")
            return result.structured_content

        ids = {}
        for tool, args in [("top_anomalies", {}), ("get_dependencies", {"service": "cart"}),
                           ("get_metric", {"service": "cache", "metric": "latency_ms"}),
                           ("get_logs", {"service": "cart"})]:
            look = await call(tool, **args)
            ids[tool] = look["call_id"]
            print(f"{tool}({', '.join(args.values())}) -> {look['call_id']}\n{_first_lines(look['text'])}")

        def suspect(service: str, confidence: float, *tools: str) -> dict:
            return {"service": service, "confidence": confidence, "evidence": [ids[t] for t in tools]}

        verdicts = {}
        for label, suspects in [("right", [suspect("cache", 0.9, "top_anomalies", "get_metric", "get_logs")]),
                                ("wrong", [suspect("cart", 0.9, "top_anomalies", "get_dependencies")])]:
            verdict = verdicts[label] = await call("check_answer", suspects=suspects)
            top, rollback = verdict["causes"][0], verdict["rollback"]
            print(f"check_answer({top['service']}, {top['confidence']:.0%}) -> {verdict['decision']}\n"
                  f"    citations: {top['verify_note']}\n"
                  f"    blast radius: explains {verdict['blast']['explained']}, better: {verdict['blast']['better']}\n"
                  f"    rollback: alarm after = {rollback['alarm_after']}, proves = {rollback['proves']}\n"
                  f"    reasons: {verdict['reasons']}")
        truth = await call("reveal_truth", recorded=incident["recorded"])
        print(f"reveal_truth(41) -> cause {truth['root']} ({truth['fault']}); Sherlog's own agent answered "
              f"{truth['sherlog']['answer']}; the z-score ranking's first pick was {truth['zscore_top3'][0]}")
        return {"verdicts": verdicts, "truth": truth}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", nargs="?", default=LIVE)
    parser.add_argument("--mode", default="auto", help="auto (2026-07-28 if served, else the handshake) or legacy")
    args = parser.parse_args()
    outcome = asyncio.run(investigate(args.url, args.mode))
    right, wrong = outcome["verdicts"]["right"], outcome["verdicts"]["wrong"]
    assert right["decision"] == "ship" and right["rollback"]["proves"], right
    assert wrong["decision"] == "hand to a human" and not wrong["rollback"]["proves"], wrong
    assert outcome["truth"]["root"] == right["answer"] == "cache"
    print("ok: the right answer ships, the wrong one goes to a human, and the truth agrees")


if __name__ == "__main__":
    main()
