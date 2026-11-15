"""Draw the README's results chart from the recorded benchmark, so every bar is a number in runs/evidence.json.

    python scripts/make_figures.py      # writes media/results.svg
"""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "runs" / "evidence.json"
OUT = ROOT / "media" / "results.svg"

# two identities, validated for colour-blind separation and 3:1 contrast on the card
NO_AI, CLAUDE = "#c8641e", "#1a2ffb"
CARD, INK, MUTED, GRID = "#f0f1fa", "#0b0c10", "#5c5f6e", "#d9dbe7"
FONT = "Geist, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"

WIDTH, PAD, LABEL_W, VALUE_W = 900, 32, 250, 64
ROW_H, BAR_H = 30, 14
BAR_X = PAD + LABEL_W
BAR_W = WIDTH - BAR_X - VALUE_W - PAD


def rows(ev: dict, prefix: str) -> list[tuple[str, float, list | None, str]]:
    """(label, cause-first share, 95% interval, colour) for one benchmark: normal ('') or hard ('hard-')."""
    agent = ev[f"{prefix}agent"]
    out = [("No AI · z-score ranking", agent["baseline_ac1"], agent.get("baseline_ac1_ci"), NO_AI)]
    if agent.get("robust_ac1") is not None:
        out.append(("No AI · robust z-score", agent["robust_ac1"], agent.get("robust_ac1_ci"), NO_AI))
    if agent.get("graph_ac1") is not None:
        out.append(("No AI · call-graph rule", agent["graph_ac1"], agent.get("graph_ac1_ci"), NO_AI))
    for key, label in [("one-shot", "Opus · one prompt, no tools"), ("haiku", "Haiku agent"),
                       ("sonnet", "Sonnet agent"), ("agent", "Opus agent"), ("auto", "Auto · Haiku, then Opus")]:
        s = ev.get(f"{prefix}{key}")
        if s and s.get("outages"):
            out.append((label, s["agent_ac1"], s.get("agent_ac1_ci"), CLAUDE))
    return out


def panel(y: float, title: str, data: list) -> tuple[list[str], float]:
    parts = [f'<text x="{PAD}" y="{y}" font-size="15" font-weight="600" fill="{INK}">{escape(title)}</text>']
    y += 14
    top = y
    for pct in (0, 0.5, 1):  # recessive grid at 0, 50 and 100%
        gx = BAR_X + pct * BAR_W
        parts.append(f'<line x1="{gx:.1f}" x2="{gx:.1f}" y1="{top}" y2="{top + len(data) * ROW_H}" stroke="{GRID}" stroke-width="1"/>')
    for label, share, ci, colour in data:
        cy = y + ROW_H / 2
        w = max(share * BAR_W, 8)
        parts.append(f'<text x="{BAR_X - 12}" y="{cy + 4.5}" font-size="13" text-anchor="end" fill="{INK}">{escape(label)}</text>')
        # bar anchored at the baseline, 4px rounded data end
        parts.append(f'<path d="M{BAR_X},{cy - BAR_H / 2} h{w - 4:.1f} a4,4 0 0 1 4,4 v{BAR_H - 8} a4,4 0 0 1 -4,4 h{-(w - 4):.1f} z" fill="{colour}"/>')
        if ci:
            x0, x1 = BAR_X + ci[0] * BAR_W, BAR_X + ci[1] * BAR_W
            parts.append(f'<path d="M{x0:.1f},{cy} H{x1:.1f} M{x0:.1f},{cy - 5} v10 M{x1:.1f},{cy - 5} v10" stroke="{INK}" stroke-width="1.5" fill="none" opacity="0.55"/>')
        parts.append(f'<text x="{WIDTH - PAD}" y="{cy + 4.5}" font-size="13" font-weight="600" text-anchor="end" fill="{INK}">{round(share * 100)}%</text>')
        y += ROW_H
    for pct, text in ((0, "0%"), (0.5, "50%"), (1, "100%")):
        parts.append(f'<text x="{BAR_X + pct * BAR_W:.1f}" y="{y + 16}" font-size="11" text-anchor="middle" fill="{MUTED}">{text}</text>')
    return parts, y + 40


def summary(normal: list, hard: list) -> str:
    fmt = lambda data: "; ".join(f"{label} {round(share * 100)}%" for label, share, _, _ in data)
    return f"Normal outages: {fmt(normal)}. Hard mode: {fmt(hard)}."


def main() -> None:
    ev = json.loads(EVIDENCE.read_text())
    normal, hard = rows(ev, ""), rows(ev, "hard-")
    head = [
        f'<text x="{PAD}" y="44" font-size="20" font-weight="600" fill="{INK}">Named the true cause first</text>',
        f'<text x="{PAD}" y="68" font-size="13" fill="{MUTED}">Ranked first and accepted by the verifier · whiskers: 95% Wilson intervals</text>',
        # legend: two identities, also named on every row
        f'<rect x="{WIDTH - PAD - 150}" y="34" width="12" height="12" rx="3" fill="{NO_AI}"/>',
        f'<text x="{WIDTH - PAD - 132}" y="44.5" font-size="13" fill="{INK}">No AI</text>',
        f'<rect x="{WIDTH - PAD - 76}" y="34" width="12" height="12" rx="3" fill="{CLAUDE}"/>',
        f'<text x="{WIDTH - PAD - 58}" y="44.5" font-size="13" fill="{INK}">Claude</text>',
    ]
    p1, y = panel(108, f"Normal · {ev['agent']['outages']} outages", normal)
    p2, y = panel(y, f"Hard mode, with a decoy · {ev['hard-agent']['outages']} outages", hard)
    height = y - 8
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height:.0f}" viewBox="0 0 {WIDTH} {height:.0f}" '
           f'font-family="{FONT}" role="img" aria-labelledby="t d">'
           f'<title id="t">Named the true cause first, normal and hard-mode outages</title>'
           f'<desc id="d">{escape(summary(normal, hard))}</desc>'
           f'<rect width="{WIDTH}" height="{height:.0f}" rx="18" fill="{CARD}"/>'
           + "".join(head + p1 + p2) + "</svg>\n")
    OUT.write_text(svg)
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
