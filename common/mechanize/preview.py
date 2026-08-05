"""Render a Sentinel directory into a single self-contained preview HTML.

Called from the mechanize skill's Stage 8. Reads `sentinel.yaml`,
`rationale.md`, `functions/*.py`, and `inputs.json` from the given directory
and writes `preview.html` next to them. The skill then hands that path to
the Artifact tool (adapter-specific) so the human reviewer sees the compiled
Sentinel rendered inline instead of grepping raw YAML.

Usage:
    python3 common/mechanize/preview.py <sentinel_dir>

Output: <sentinel_dir>/preview.html, plus stdout with the same path.

Mermaid: rendered via `<pre class="mermaid">` blocks. This works natively
inside the Artifact tool (which loads mermaid). When opened as a plain
file in a browser, the diagram degrades to raw source in a code block —
acceptable; the primary consumption path is Artifact.

No dependencies beyond PyYAML (already required by sentinel-lint).
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path
from typing import Any

import yaml

KIND_STYLES = {
    "tool":       ("#0b6bcb", "#e8f0fe"),
    "function":   ("#1e7b3a", "#e6f4ea"),
    "condition":  ("#8a6d0b", "#fef7e0"),
    "emit":       ("#a24a00", "#fce8d6"),
    "llm":        ("#6a1b9a", "#f3e5f5"),
    "ask_human":  ("#b71c1c", "#fdecea"),
}


def _toposort(nodes: dict[str, dict]) -> list[str]:
    """Kahn's algorithm; ties broken by node id for stable output."""
    indeg = {n: 0 for n in nodes}
    for name, spec in nodes.items():
        for dep in spec.get("dependsOn") or []:
            if dep in indeg:
                indeg[name] = indeg.get(name, 0) + 1
    ready = sorted(n for n, d in indeg.items() if d == 0)
    out: list[str] = []
    while ready:
        n = ready.pop(0)
        out.append(n)
        for child, spec in nodes.items():
            if n in (spec.get("dependsOn") or []):
                indeg[child] -= 1
                if indeg[child] == 0:
                    ready.append(child)
                    ready.sort()
    return out or list(nodes)


def _yaml_pretty(value: Any) -> str:
    if value is None or value == {} or value == []:
        return ""
    return yaml.safe_dump(value, sort_keys=False, default_flow_style=False, width=100).rstrip()


def _md_to_html(text: str) -> str:
    """Minimal markdown → html: headings, fenced code, paragraphs, inline code, links.

    Not a full markdown implementation; enough to render rationale.md legibly."""
    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    para: list[str] = []

    def flush_para() -> None:
        if para:
            joined = " ".join(para).strip()
            if joined:
                out.append(f"<p>{_inline(joined)}</p>")
            para.clear()

    for line in lines:
        if line.startswith("```"):
            flush_para()
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append('<pre class="rat-code"><code>')
                in_code = True
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        stripped = line.rstrip()
        if not stripped:
            flush_para()
            continue
        if stripped.startswith("#"):
            flush_para()
            level = 0
            while level < len(stripped) and stripped[level] == "#":
                level += 1
            level = min(level, 6)
            text_ = stripped[level:].strip()
            out.append(f"<h{level+2}>{_inline(text_)}</h{level+2}>")
            continue
        if stripped.startswith(("- ", "* ")):
            flush_para()
            out.append(f"<li>{_inline(stripped[2:])}</li>")
            continue
        para.append(stripped)
    flush_para()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def _inline(text: str) -> str:
    """Escape then re-apply inline `code` spans."""
    esc = html.escape(text)
    parts = esc.split("`")
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(f"<code>{part}</code>")
        else:
            out.append(part)
    return "".join(out)


def _kind_badge(kind: str) -> str:
    fg, bg = KIND_STYLES.get(kind, ("#444", "#eee"))
    return f'<span class="badge" style="color:{fg};background:{bg}">{html.escape(kind)}</span>'


def _mermaid(nodes: dict[str, dict]) -> str:
    lines = ["flowchart TD"]
    for name in nodes:
        lines.append(f'    {name}["{name}"]')
    for name, spec in nodes.items():
        for dep in spec.get("dependsOn") or []:
            lines.append(f"    {dep} --> {name}")
    return "\n".join(lines)


def _render_inputs_table(inputs: dict) -> str:
    if not inputs:
        return "<p><em>No inputs declared.</em></p>"
    rows = []
    for name, spec in inputs.items():
        typ = spec.get("type", "")
        req = "yes" if spec.get("required") else ""
        default = spec.get("default")
        default_s = "" if default is None else html.escape(str(default))
        cons = spec.get("constraints") or {}
        cons_s = ", ".join(f"{k}={v}" for k, v in cons.items()) if cons else ""
        rows.append(
            f"<tr><td><code>{html.escape(name)}</code></td>"
            f"<td>{html.escape(typ)}</td>"
            f"<td>{req}</td>"
            f"<td>{default_s}</td>"
            f"<td>{html.escape(cons_s)}</td></tr>"
        )
    return (
        '<table class="inputs">'
        "<thead><tr><th>name</th><th>type</th><th>required</th>"
        "<th>default</th><th>constraints</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_capabilities(caps: dict) -> str:
    req = (caps or {}).get("required") or []
    if not req:
        return "<p><em>No capabilities declared.</em></p>"
    items = "".join(
        f"<li><code>{html.escape(c.get('id',''))}</code> "
        f"<span class='muted'>({html.escape(c.get('capabilityType',''))})</span></li>"
        for c in req
    )
    return f"<ul class='caps'>{items}</ul>"


def _render_variation_points(vps: list) -> str:
    if not vps:
        return "<p><em>No variation points declared.</em></p>"
    items = "".join(
        f"<li><code>{html.escape(v.get('path',''))}</code> "
        f"<span class='muted'>[{', '.join(v.get('operations') or [])}]</span></li>"
        for v in vps
    )
    return f"<ul class='vps'>{items}</ul>"


def _render_node(name: str, spec: dict, sentinel_dir: Path) -> str:
    kind = spec.get("kind", "")
    deps = spec.get("dependsOn") or []
    dep_chips = "".join(
        f"<a class='chip' href='#node-{html.escape(d)}'>{html.escape(d)}</a>"
        for d in deps
    ) or "<span class='muted'>—</span>"

    config_body = ""
    cfg = spec.get("config") or {}
    if kind == "function":
        source = cfg.get("source", "")
        source_path = sentinel_dir / source if source else None
        body_html = ""
        if source_path and source_path.exists():
            body = source_path.read_text()
            body_html = (
                f"<details open><summary>function body "
                f"(<code>{html.escape(source)}</code>)</summary>"
                f"<pre class='code'><code>{html.escape(body)}</code></pre></details>"
            )
        else:
            body_html = f"<p class='warn'>Missing function file: <code>{html.escape(source)}</code></p>"
        args = cfg.get("arguments") or {}
        args_html = ""
        if args:
            args_html = (
                "<details><summary>arguments</summary>"
                f"<pre class='code'><code>{html.escape(_yaml_pretty(args))}</code></pre>"
                "</details>"
            )
        config_body = args_html + body_html
    elif kind == "tool":
        toolref = cfg.get("toolRef", "")
        args = cfg.get("arguments") or {}
        config_body = (
            f"<p><strong>toolRef:</strong> <code>{html.escape(toolref)}</code></p>"
            + (
                "<details open><summary>arguments</summary>"
                f"<pre class='code'><code>{html.escape(_yaml_pretty(args))}</code></pre></details>"
                if args else ""
            )
        )
    elif kind == "condition":
        expr = cfg.get("expression", "")
        config_body = (
            "<p><strong>expression:</strong></p>"
            f"<pre class='code'><code>{html.escape(expr.strip())}</code></pre>"
        )
    elif kind == "emit":
        finding = cfg.get("finding") or {}
        config_body = (
            "<details open><summary>finding</summary>"
            f"<pre class='code'><code>{html.escape(_yaml_pretty(finding))}</code></pre>"
            "</details>"
        )
        when = spec.get("when")
        if when:
            config_body = (
                f"<p><strong>when:</strong> <code>{html.escape(str(when))}</code></p>"
                + config_body
            )
    elif kind == "llm":
        config_body = (
            "<details open><summary>llm config</summary>"
            f"<pre class='code'><code>{html.escape(_yaml_pretty(cfg))}</code></pre>"
            "</details>"
        )
    elif kind == "ask_human":
        config_body = (
            "<details open><summary>ask_human config</summary>"
            f"<pre class='code'><code>{html.escape(_yaml_pretty(cfg))}</code></pre>"
            "</details>"
        )
    else:
        config_body = (
            "<pre class='code'><code>"
            + html.escape(_yaml_pretty(cfg))
            + "</code></pre>"
        )

    output = spec.get("output") or {}
    output_html = ""
    if output:
        output_html = (
            "<details><summary>output schema</summary>"
            f"<pre class='code'><code>{html.escape(_yaml_pretty(output))}</code></pre></details>"
        )

    return (
        f"<section class='node' id='node-{html.escape(name)}'>"
        f"<h3><code>{html.escape(name)}</code> {_kind_badge(kind)}</h3>"
        f"<p class='muted'>dependsOn: {dep_chips}</p>"
        f"{config_body}"
        f"{output_html}"
        "</section>"
    )


def render(sentinel_dir: Path) -> str:
    sentinel_path = sentinel_dir / "sentinel.yaml"
    if not sentinel_path.exists():
        raise SystemExit(f"sentinel.yaml not found in {sentinel_dir}")
    with sentinel_path.open() as f:
        sentinel = yaml.safe_load(f) or {}

    meta = sentinel.get("metadata") or {}
    spec = sentinel.get("spec") or {}
    purpose = spec.get("purpose") or {}
    inputs = spec.get("inputs") or {}
    caps = spec.get("capabilities") or {}
    vps = spec.get("variationPoints") or []
    nodes = spec.get("nodes") or {}
    outputs = spec.get("outputs") or {}
    execution = spec.get("execution") or {}

    rationale_path = sentinel_dir / "rationale.md"
    rationale_html = ""
    if rationale_path.exists():
        rationale_html = _md_to_html(rationale_path.read_text())
    else:
        rationale_html = "<p><em>No rationale.md present.</em></p>"

    inputs_json_path = sentinel_dir / "inputs.json"
    inputs_json_html = ""
    if inputs_json_path.exists():
        try:
            body = json.dumps(json.loads(inputs_json_path.read_text()), indent=2)
        except json.JSONDecodeError:
            body = inputs_json_path.read_text()
        inputs_json_html = (
            "<details><summary><code>inputs.json</code></summary>"
            f"<pre class='code'><code>{html.escape(body)}</code></pre></details>"
        )

    ordered = _toposort(nodes)
    mermaid_src = _mermaid(nodes)
    node_cards = "\n".join(_render_node(n, nodes[n], sentinel_dir) for n in ordered)
    kind_counts: dict[str, int] = {}
    for spec_ in nodes.values():
        k = spec_.get("kind", "?")
        kind_counts[k] = kind_counts.get(k, 0) + 1
    kind_pills = "".join(
        f"<span class='pill'>{_kind_badge(k)} × {v}</span>"
        for k, v in sorted(kind_counts.items())
    )

    name = meta.get("name", "?")
    version = meta.get("version", "")
    display_name = meta.get("displayName") or name

    return f"""<title>Sentinel preview — {html.escape(name)}</title>
<style>
:root {{
  --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --line: #e6e6e6;
  --code-bg: #f6f7f9; --card-bg: #ffffff; --link: #0b6bcb;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0f1115; --fg: #e6e6e6; --muted: #9a9a9a; --line: #262a33;
    --code-bg: #171a21; --card-bg: #131720; --link: #7ab7ff;
  }}
}}
:root[data-theme="light"] {{
  --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --line: #e6e6e6;
  --code-bg: #f6f7f9; --card-bg: #ffffff; --link: #0b6bcb;
}}
:root[data-theme="dark"] {{
  --bg: #0f1115; --fg: #e6e6e6; --muted: #9a9a9a; --line: #262a33;
  --code-bg: #171a21; --card-bg: #131720; --link: #7ab7ff;
}}
body {{
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  color: var(--fg); background: var(--bg); margin: 0;
  padding: 24px; max-width: 1100px; margin-inline: auto;
}}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
h2 {{ font-size: 16px; margin: 28px 0 10px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }}
h3 {{ font-size: 14px; margin: 0 0 8px; font-weight: 600; }}
p, li {{ margin: 6px 0; }}
a {{ color: var(--link); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
code {{ background: var(--code-bg); padding: 1px 5px; border-radius: 3px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }}
pre.code, pre.rat-code {{
  background: var(--code-bg); padding: 10px 12px; border-radius: 4px;
  overflow-x: auto; font-size: 12.5px; line-height: 1.45; margin: 6px 0;
}}
pre code {{ background: transparent; padding: 0; }}
.muted {{ color: var(--muted); }}
.badge {{ display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 10px; font-weight: 600; }}
.pill {{ display: inline-block; margin-right: 6px; font-size: 12px; }}
.chip {{ display: inline-block; background: var(--code-bg); padding: 1px 7px; border-radius: 3px; margin-right: 4px; font-size: 12px; }}
table.inputs {{ border-collapse: collapse; width: 100%; margin: 6px 0; }}
table.inputs th, table.inputs td {{
  text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top;
}}
table.inputs th {{ font-weight: 600; color: var(--muted); font-size: 12px; }}
ul.caps, ul.vps {{ padding-left: 20px; }}
.node {{
  border: 1px solid var(--line); border-radius: 6px; background: var(--card-bg);
  padding: 12px 14px; margin: 10px 0;
}}
details summary {{ cursor: pointer; color: var(--muted); font-size: 12.5px; margin: 4px 0; }}
details[open] summary {{ color: var(--fg); }}
pre.mermaid {{ background: var(--code-bg); padding: 12px; border-radius: 4px; overflow-x: auto; }}
.warn {{ color: #b71c1c; }}
.rat {{ background: var(--card-bg); border: 1px solid var(--line); border-radius: 6px; padding: 14px 18px; }}
.rat h2, .rat h3, .rat h4 {{ margin-top: 14px; }}
.summary-strip {{ margin: 8px 0 18px; }}
</style>

<h1>{html.escape(display_name)}</h1>
<p class="muted"><code>{html.escape(name)}</code> · v{html.escape(str(version))} ·
conclusionType: <code>{html.escape(purpose.get('conclusionType',''))}</code></p>
<div class="summary-strip">{kind_pills}</div>

<h2>Purpose</h2>
<p><strong>Summary:</strong> {html.escape(purpose.get('summary','').strip())}</p>
<p><strong>Reusable question:</strong> {html.escape(purpose.get('reusableQuestion','').strip())}</p>

<h2>Inputs</h2>
{_render_inputs_table(inputs)}

<h2>Capabilities</h2>
{_render_capabilities(caps)}

<h2>Variation points</h2>
{_render_variation_points(vps)}

<h2>DAG</h2>
<pre class="mermaid">
{mermaid_src}
</pre>

<h2>Nodes ({len(nodes)})</h2>
{node_cards}

<h2>Outputs</h2>
<pre class="code"><code>{html.escape(_yaml_pretty(outputs))}</code></pre>

<h2>Execution</h2>
<pre class="code"><code>{html.escape(_yaml_pretty(execution))}</code></pre>

{inputs_json_html}

<h2>Rationale</h2>
<div class="rat">
{rationale_html}
</div>
"""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 preview.py <sentinel_dir>", file=sys.stderr)
        return 2
    sentinel_dir = Path(argv[1]).resolve()
    if not sentinel_dir.is_dir():
        print(f"not a directory: {sentinel_dir}", file=sys.stderr)
        return 2
    html_body = render(sentinel_dir)
    out = sentinel_dir / "preview.html"
    out.write_text(html_body)
    print(str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
