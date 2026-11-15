"""A real Claude model investigates recorded outage 41 through Sherlog's live MCP server: a plain tool-use loop over
the server's tools/list and tools/call. Prints every tool call and the model's final answer, then the token cost.

    uv run --env-file .env --with-requirements requirements.txt --with mcp python scripts/claude_over_mcp.py
"""
import asyncio
import json

from anthropic import Anthropic
from mcp import Client

URL = "https://sherlog-jade.vercel.app/mcp"
MODEL = "claude-haiku-5-5"
MAX_TURNS = 16


def tool_defs(tools) -> list[dict]:
    out = []
    for t in tools:
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
        out.append({"name": t.name, "description": t.description or "", "input_schema": schema})
    return out


async def main() -> None:
    api = Anthropic()
    async with Client(URL) as mcp:
        prompt = await mcp.get_prompt("investigate_outage", {"outage": "41"})
        text = "\n".join(m.content.text for m in prompt.messages if getattr(m.content, "text", None))
        tools = tool_defs((await mcp.list_tools()).tools)
        messages = [{"role": "user", "content": text}]
        tokens = [0, 0]
        for _ in range(MAX_TURNS):
            resp = api.messages.create(model=MODEL, max_tokens=4000, tools=tools, messages=messages)
            tokens[0] += resp.usage.input_tokens
            tokens[1] += resp.usage.output_tokens
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    print("TEXT", block.text.strip()[:900])
                if block.type != "tool_use":
                    continue
                print("TOOL", block.name, json.dumps(block.input)[:170])
                res = await mcp.call_tool(block.name, block.input)
                body = "\n".join(c.text for c in res.content if getattr(c, "text", None))
                if block.name in ("check_answer", "reveal_truth"):
                    print("   ->", body[:600].replace("\n", " | "))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": body,
                                "is_error": bool(getattr(res, "is_error", False))})
            if resp.stop_reason != "tool_use":
                break
            messages.append({"role": "user", "content": results})
        print("tokens", tokens, "approx $", round(tokens[0] * 1e-7 + tokens[1] * 5e-7, 4))


asyncio.run(main())
