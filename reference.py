"""The API reference page (/reference): every route in the OpenAPI schema as plain HTML in the site's style, readable
without JavaScript. Swagger UI stays at /docs for developers who want to try the routes.
"""
from __future__ import annotations

import json
import re
from html import escape

ROUTES = "<!-- routes -->"  # where the template takes the routes
GROUPS = ("Pages", "HTTP API", "MCP connector")


def _group(path: str) -> str:
    return "HTTP API" if path.startswith("/api/") else "MCP connector" if path == "/mcp" else "Pages"


def _type(schema: dict) -> str:
    """A field's type in a few words: its choices if it has them."""
    if "enum" in schema:
        return " | ".join(json.dumps(choice) for choice in schema["enum"])
    return schema.get("type", "any")


def _required(schema: dict, required: bool) -> str:
    if required:
        return "yes"
    return f"no, default {json.dumps(schema['default'])}" if "default" in schema else "no"


def _prose(description: str) -> str:
    """A docstring's paragraphs, with `code` marked up."""
    paragraphs = [" ".join(p.split()) for p in description.split("\n\n") if p.strip()]
    return "".join(f"<p>{re.sub(r'`([^`]+)`', r'<code>\1</code>', escape(p))}</p>" for p in paragraphs)


def _fields(rows: list[tuple[str, str, dict, bool]]) -> str:
    """A table of a route's parameters and JSON body fields."""
    body = "".join(f'<tr><th scope="row"><code>{escape(name)}</code></th><td>{where}</td>'
                   f"<td>{escape(_type(schema))}</td><td>{escape(_required(schema, required))}</td></tr>"
                   for name, where, schema, required in rows)
    head = "".join(f'<th scope="col">{name}</th>' for name in ("Name", "In", "Type", "Required"))
    return f"<table><caption>Parameters</caption><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _operation(path: str, method: str, op: dict, models: dict) -> str:
    rows = [(p["name"], p["in"], p["schema"], p.get("required", False)) for p in op.get("parameters", [])]
    body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    if "$ref" in body:
        model = models[body["$ref"].rsplit("/", 1)[-1]]
        rows += [(name, "JSON body", field, name in model.get("required", []))
                 for name, field in model["properties"].items()]
    anchor = re.sub(r"[^a-z0-9]+", "-", f"{method} {path}".lower()).strip("-")
    return (f'<div class="op"><h3 id="{anchor}"><span class="method">{method.upper()}</span> '
            f"<code>{escape(path)}</code></h3>{_prose(op.get('description', ''))}{_fields(rows) if rows else ''}</div>")


def render(spec: dict, template: str) -> str:
    """The template with every operation in `spec` in place of ROUTES, grouped as GROUPS lists them."""
    models = spec.get("components", {}).get("schemas", {})
    groups: dict[str, list[str]] = {name: [] for name in GROUPS}
    for path, operations in spec["paths"].items():
        groups[_group(path)] += [_operation(path, method, op, models) for method, op in operations.items()]
    html = "".join(f"<h2>{name}</h2>{''.join(ops)}" for name, ops in groups.items() if ops)
    return template.replace(ROUTES, html)
