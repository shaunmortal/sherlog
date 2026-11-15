"""The Model Context Protocol (MCP) over Streamable HTTP, by hand: JSON-RPC 2.0, stateless, one JSON answer per POST
and no server-sent events. app.py serves it at /mcp with Sherlog's tools; docs/mcp.md says how to connect.

Dual-era, as protocol revision 2026-07-28 allows. A POST whose MCP-Protocol-Version header names a revision outside the
handshake era is served on its own, from the version and capabilities in its params._meta, with server/discover in
place of a handshake. Any other POST is the handshake era (2025-03-26 to 2025-11-25): initialize agrees a version,
and no session is kept, so every request stands on its own there too.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping
from urllib.parse import urlsplit

MODERN = ("2026-07-28",)
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")  # newest first: initialize offers the first in place of others
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
HEADER_MISMATCH, UNSUPPORTED_VERSION = -32020, -32022
# the HTTP status of each error: the modern era's comes from its spec; the handshake era answers 200 for an error a
# well-formed request earned, as its servers do. Codes not listed answer 200.
MODERN_STATUS = {PARSE_ERROR: 400, INVALID_REQUEST: 400, INVALID_PARAMS: 400, HEADER_MISMATCH: 400,
                 UNSUPPORTED_VERSION: 400, METHOD_NOT_FOUND: 404}
LEGACY_STATUS = {PARSE_ERROR: 400, INVALID_REQUEST: 400}
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
LIST_TTL_MS = 300_000  # how long a client may cache the tool and prompt lists; they change only with a deploy
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
MAX_SHOWN_NAME = 128  # the longest tool name the spec recommends; an unknown one is echoed back no longer
NAMED = ("tools/call", "prompts/get")  # the methods whose params.name the Mcp-Name header mirrors
BASE64_HEADER = re.compile(r"=\?base64\?(.*)\?=")

log = logging.getLogger("sherlog")


@dataclass(frozen=True)
class Tool:
    title: str
    description: str
    input_schema: dict
    run: Callable[[dict], dict]  # the call's arguments -> its result; raises ToolError when the call cannot be done


@dataclass(frozen=True)
class Prompt:
    """A prompt template the user picks, such as a slash command: its arguments are optional strings."""
    title: str
    description: str
    arguments: Mapping[str, str]  # each argument's name -> its description
    render: Callable[[dict[str, str]], str]  # the arguments -> the user message; raises ValueError for a bad one


@dataclass(frozen=True)
class Server:
    name: str
    version: str
    instructions: str
    tools: Mapping[str, Tool]
    prompts: Mapping[str, Prompt] = field(default_factory=dict)


class ToolError(Exception):
    """A tool's own failure, such as a bad argument: answered as a result with isError, so the model can retry."""

    def __init__(self, message: str, structured: dict | None = None):
        super().__init__(message)
        self.message, self.structured = message, structured


class _Fail(Exception):
    """A JSON-RPC error answer."""

    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def error(rid: str | int | None, code: int, message: str, data: dict | None = None) -> dict:
    """A JSON-RPC error; `rid` is None when the request's id could not be read."""
    body = {"code": code, "message": message, **({"data": data} if data else {})}
    return {"jsonrpc": "2.0", "id": rid, "error": body}


def handle(body: bytes, headers: Mapping[str, str], server: Server) -> tuple[int, dict | None]:
    """One POST to the MCP endpoint: its HTTP status and JSON-RPC answer, or None for 202 Accepted with no body."""
    if not _origin_allowed(headers):
        return 403, error(None, INVALID_REQUEST, "This Origin may not call the MCP endpoint (a DNS rebinding guard).")
    header = headers.get("mcp-protocol-version")
    modern = header is not None and header not in LEGACY  # no header: the handshake era, read as 2025-03-26
    rid = None
    try:
        message = _message(body)
        if "id" not in message:  # a notification: nothing to act on, and no answer is owed
            if modern and header not in MODERN:
                raise _Fail(UNSUPPORTED_VERSION, "Unsupported protocol version", _versions(header))
            return 202, None
        if "method" not in message:  # a response to a server request: this server sends none
            if modern:
                raise _Fail(INVALID_REQUEST, "Clients send requests and notifications only.")
            return 202, None
        rid = message["id"]
        params = message.get("params", {})
        if modern:
            _check_envelope(message["method"], params, headers)
        result = _dispatch(message["method"], params, server, modern)
    except _Fail as fail:
        status = (MODERN_STATUS if modern else LEGACY_STATUS).get(fail.code, 200)
        return status, error(rid, fail.code, fail.message, fail.data)
    if modern:
        result = {**result, "resultType": "complete", "_meta": {SERVER_INFO_KEY: _info(server)}}
    return 200, {"jsonrpc": "2.0", "id": rid, "result": result}


def _origin_allowed(headers: Mapping[str, str]) -> bool:
    """No Origin (not a browser), the server's own, or a page on this machine. The spec asks every server to check."""
    origin = headers.get("origin")
    if origin is None:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return parts.netloc == headers.get("host") or parts.hostname in LOCAL_HOSTS


def _no_constants(name: str):
    raise ValueError(f"{name} is not JSON")


def _message(body: bytes) -> dict:
    """One JSON-RPC 2.0 request, notification or response, its envelope checked."""
    try:
        message = json.loads(body, parse_constant=_no_constants)
    except (ValueError, RecursionError):  # not UTF-8 or not JSON, NaN, a huge integer literal, nesting too deep
        raise _Fail(PARSE_ERROR, "Parse error: the body is not JSON.") from None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise _Fail(INVALID_REQUEST, "Send one JSON-RPC 2.0 message per POST; batches are not supported.")
    rid = message.get("id")
    if "id" in message and (not isinstance(rid, (str, int)) or isinstance(rid, bool)):
        raise _Fail(INVALID_REQUEST, "A request's id must be a string or an integer.")
    if "method" in message and not isinstance(message["method"], str):
        raise _Fail(INVALID_REQUEST, "method must be a string.")
    if "method" not in message and ("id" not in message or not {"result", "error"} & set(message)):
        raise _Fail(INVALID_REQUEST, "A message needs a method, or an id with a result or an error.")
    if not isinstance(message.get("params", {}), dict):
        raise _Fail(INVALID_REQUEST, "params must be an object.")
    return message


def _versions(requested) -> dict:
    return {"supported": [*MODERN, *LEGACY], "requested": requested}


def _decoded(value: str | None) -> str | None:
    """A header's value, with its =?base64?...?= form decoded; None when that form is malformed."""
    match = BASE64_HEADER.fullmatch(value or "")
    if not match:
        return value
    try:
        return base64.b64decode(match[1], validate=True).decode()
    except ValueError:  # bad base64 or bad UTF-8
        return None


def _check_envelope(method: str, params: dict, headers: Mapping[str, str]) -> None:
    """A modern request's _meta, the headers that mirror its body, then its version, in the order the reference
    SDK checks them, so a client that disagrees with itself is told so first."""
    meta = params.get("_meta")
    if not isinstance(meta, dict) or VERSION_KEY not in meta or CAPABILITIES_KEY not in meta:
        raise _Fail(INVALID_PARAMS, f"params._meta must carry {VERSION_KEY} and {CAPABILITIES_KEY}.")
    if headers.get("mcp-protocol-version") != meta[VERSION_KEY]:
        raise _Fail(HEADER_MISMATCH, "The MCP-Protocol-Version header does not match the protocol version in _meta.")
    if headers.get("mcp-method") != method:
        raise _Fail(HEADER_MISMATCH, "The Mcp-Method header does not match the method.")
    name = params.get("name")
    if method in NAMED and name is not None and _decoded(headers.get("mcp-name")) != name:
        raise _Fail(HEADER_MISMATCH, "The Mcp-Name header does not match params.name.")
    if meta[VERSION_KEY] not in MODERN:
        raise _Fail(UNSUPPORTED_VERSION, "Unsupported protocol version", _versions(meta[VERSION_KEY]))


def _info(server: Server) -> dict:
    return {"name": server.name, "version": server.version}


def _dispatch(method: str, params: dict, server: Server, modern: bool) -> dict:
    if method == "ping":
        return {}
    if method == "tools/list":
        tools = {"tools": [_definition(name, tool) for name, tool in server.tools.items()]}
        return {**tools, "ttlMs": LIST_TTL_MS, "cacheScope": "public"} if modern else tools
    if method == "tools/call":
        return _call(params, server)
    if method == "prompts/list":
        prompts = {"prompts": [_prompt_definition(name, prompt) for name, prompt in server.prompts.items()]}
        return {**prompts, "ttlMs": LIST_TTL_MS, "cacheScope": "public"} if modern else prompts
    if method == "prompts/get":
        return _get_prompt(params, server)
    if modern and method == "server/discover":
        return {"supportedVersions": [*MODERN, *LEGACY], "capabilities": {"tools": {}, "prompts": {}},
                "instructions": server.instructions, "ttlMs": LIST_TTL_MS, "cacheScope": "public"}
    if not modern and method == "initialize":
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise _Fail(INVALID_PARAMS, "initialize needs params.protocolVersion.", _versions(requested))
        return {"protocolVersion": requested if requested in LEGACY else LEGACY[0],
                "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                "serverInfo": _info(server),
                "instructions": server.instructions}
    raise _Fail(METHOD_NOT_FOUND, f"Method not found: {method[:MAX_SHOWN_NAME]}")


def _definition(name: str, tool: Tool) -> dict:
    # every tool reads recorded or uploaded data and changes nothing, so a client may run it without asking
    return {"name": name, "title": tool.title, "description": tool.description, "inputSchema": tool.input_schema,
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                            "openWorldHint": False}}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _call(params: dict, server: Server) -> dict:
    name, arguments = params.get("name"), params.get("arguments")
    tool = server.tools.get(name) if isinstance(name, str) else None
    if tool is None:
        raise _Fail(INVALID_PARAMS, f"Unknown tool: {str(name)[:MAX_SHOWN_NAME]!r}. tools/list names the tools.")
    if not isinstance(arguments, (dict, type(None))):
        raise _Fail(INVALID_PARAMS, "params.arguments must be an object.")
    try:
        out = tool.run(arguments or {})
    except ToolError as exc:
        structured = {"structuredContent": exc.structured} if exc.structured else {}
        return {"content": [_text(exc.message)], **structured, "isError": True}
    except Exception:
        log.exception("MCP tool %s failed", name)
        raise _Fail(INTERNAL_ERROR, f"{name} failed unexpectedly.") from None
    # the same JSON twice, as the spec asks: as text for clients that show text, as structuredContent for code
    return {"content": [_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))],
            "structuredContent": out, "isError": False}


def _prompt_definition(name: str, prompt: Prompt) -> dict:
    return {"name": name, "title": prompt.title, "description": prompt.description,
            "arguments": [{"name": arg, "description": text, "required": False}
                          for arg, text in prompt.arguments.items()]}


def _get_prompt(params: dict, server: Server) -> dict:
    """A prompt's message for these arguments; a bad name or argument is invalid params, as the spec asks."""
    name, arguments = params.get("name"), params.get("arguments") or {}
    prompt = server.prompts.get(name) if isinstance(name, str) else None
    if prompt is None:
        raise _Fail(INVALID_PARAMS, f"Unknown prompt: {str(name)[:MAX_SHOWN_NAME]!r}. prompts/list names the prompts.")
    if not isinstance(arguments, dict):
        raise _Fail(INVALID_PARAMS, "params.arguments must be an object.")
    unknown = next((arg for arg in arguments if arg not in prompt.arguments), None)
    if unknown is not None:
        raise _Fail(INVALID_PARAMS, f"Unknown argument for {name}: {unknown[:MAX_SHOWN_NAME]!r}.")
    if not all(isinstance(value, str) for value in arguments.values()):
        raise _Fail(INVALID_PARAMS, "Prompt arguments must be strings.")
    try:
        text = prompt.render(arguments)
    except ValueError as exc:
        raise _Fail(INVALID_PARAMS, str(exc)) from None
    return {"description": prompt.description, "messages": [{"role": "user", "content": _text(text)}]}
