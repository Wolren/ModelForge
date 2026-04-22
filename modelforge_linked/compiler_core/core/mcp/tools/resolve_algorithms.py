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
    plan_txt = json.dumps(args.get("semantic_plan", {}), indent=2)
    lines = []
    for alg_id, info in args.get("algorithm_catalog", {}).items():
        pnames = [p["name"] for p in info.get("parameters", [])]
        lines.append(f"  {alg_id}: {info.get('name','')} params=[{', '.join(pnames)}]")
    return (
        f"SEMANTIC PLAN:\n{plan_txt}\n\n"
        f"ALGORITHM CATALOG:\n" + "\n".join(lines) +
        "\n\nResolve each step."
    )
