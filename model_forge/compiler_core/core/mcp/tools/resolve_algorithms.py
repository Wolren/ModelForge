"""
MCP Tool: resolve_algorithms
Maps each semantic step intent to a concrete QGIS algorithm_id + bindings.
"""
import json

SYSTEM_PROMPT = """\
You are a QGIS algorithm resolver.
Map each step to the best-fit algorithm_id and declare parameter bindings.

Return JSON with NO markdown:
{
  "resolved_steps": [
    {
      "step_id":      "<same>",
      "algorithm_id": "<provider:name>",
      "confidence":   0.0-1.0,
      "status":       "resolved|assumed|blocked",
      "parameters": {
        "<param_name>": {
          "source_type":  "model_input|child_output|static|expression|enum_index",
          "model_input":  "<name>",
          "child_id":     "<step_id>",
          "output_name":  "<port>",
          "static_value": null,
          "enum_index":   0
        }
      }
    }
  ]
}
Rules: Use EXACT algorithm_ids. If no match: status=blocked. Return ONLY valid JSON.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "semantic_plan":      {"type": "object"},
        "algorithm_catalog":  {"type": "object"},
    },
    "required": ["semantic_plan", "algorithm_catalog"],
}


def build_user_message(args: dict) -> str:
    semantic_plan = args.get("semantic_plan", {})
    compact_steps = []
    for step in semantic_plan.get("steps", []):
        compact_steps.append({
            "step_id": step.get("step_id"),
            "intent": step.get("intent"),
            "inputs": step.get("inputs", []),
            "outputs": step.get("outputs", []),
            "constraints": step.get("constraints", {}),
        })
    compact_plan = {
        "goal_summary": semantic_plan.get("goal_summary", ""),
        "model_inputs": semantic_plan.get("model_inputs", []),
        "steps": compact_steps,
    }
    plan_txt = json.dumps(compact_plan, ensure_ascii=False)

    lines = []
    catalog_items = list(args.get("algorithm_catalog", {}).items())
    for alg_id, info in catalog_items[:140]:
        pnames = [p["name"] for p in info.get("parameters", [])[:8]]
        lines.append(f"  {alg_id}: {info.get('name','')} params=[{', '.join(pnames)}]")
    if len(catalog_items) > 140:
        lines.append(f"  ... {len(catalog_items) - 140} more algorithms omitted")

    return (
        f"SEMANTIC PLAN:\n{plan_txt}\n\n"
        f"ALGORITHM CATALOG:\n" + ("\n".join(lines) if lines else "  <empty>") +
        "\n\nResolve each step. Prefer exact parameter names."
    )
