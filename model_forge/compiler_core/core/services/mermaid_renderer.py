"""Convert model JSON dict to mermaid.js flowchart markdown."""

from __future__ import annotations

from typing import Any


def to_mermaid(model_json: dict[str, Any]) -> str:
    if not model_json:
        return "flowchart TD\n  empty[No model data]"

    lines = ["flowchart TD"]
    model_name = model_json.get("model_name", "Workflow")
    lines.append(f"  title[{model_name}]")

    inputs = model_json.get("inputs", [])
    algs = model_json.get("algorithms", [])

    input_nodes: list[str] = []
    for inp in inputs:
        name = inp.get("name", "?")
        label = inp.get("label", name) or name
        safe_id = _safe(input_name_id(name))
        lines.append(f'  {safe_id}["{_esc(label)}"]:::modelInput')
        input_nodes.append((name, safe_id))

    step_nodes: dict[str, str] = {}
    for alg in algs:
        sid = alg.get("id", "?")
        desc = alg.get("description", sid) or sid
        short = _label_short(desc)
        safe = _safe(sid)
        lines.append(f'  {safe}["{_esc(short)}"]:::algStep')
        step_nodes[sid] = safe

    for alg in algs:
        sid = alg.get("id", "?")
        safe = step_nodes.get(sid)
        if not safe:
            continue

        params = alg.get("parameters", {})
        for pname, pval in params.items():
            ptype = pval.get("type", "")
            if ptype == "model_input":
                inp_name = pval.get("input_name", "")
                src_id = _safe(input_name_id(inp_name))
                if src_id in [v for v in step_nodes.values()] or any(
                    src_id == _safe(input_name_id(n)) for n, _ in input_nodes
                ):
                    lines.append(f"  {src_id} -->|{_esc(pname)}| {safe}")
            elif ptype == "child_output":
                child = pval.get("child_id", "")
                child_safe = step_nodes.get(child)
                if child_safe:
                    oname = pval.get("output_name", "OUTPUT")
                    lines.append(f"  {child_safe} -->|{_esc(oname)}| {safe}")

    implied_deps = model_json.get("step_dependencies", {})
    for child_id, parent_ids in implied_deps.items():
        child_safe = step_nodes.get(child_id)
        if not child_safe:
            continue
        for pid in parent_ids or []:
            parent_safe = step_nodes.get(pid)
            if parent_safe:
                edge = f"  {parent_safe} -.-> {child_safe}"
                if edge not in lines:
                    lines.append(edge)

    lines.append("")
    lines.append("  classDef modelInput fill:#4a9eff,stroke:#2a7edf,color:#fff")
    lines.append("  classDef algStep fill:#2d2d2d,stroke:#555,color:#eee")

    return "\n".join(lines)


def _safe(s: str) -> str:
    out = "".join(c if c.isalnum() or c == "_" else "_" for c in s)
    if out and out[0].isdigit():
        out = "n" + out
    return out or "node"


def _esc(s: str) -> str:
    return s.replace('"', "'").replace("[", "(").replace("]", ")")


def _label_short(s: str, max_len: int = 24) -> str:
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def input_name_id(name: str) -> str:
    return f"input_{name}"
