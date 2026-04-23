"""
MCP Tool: plan_workflow
Decomposes a natural-language geoprocessing description into a SemanticPlan.
"""
SYSTEM_PROMPT = """\
You are a QGIS workflow planner.
Output exactly one SemanticPlan JSON object.

JSON schema:
{
  "goal_summary": "<one sentence>",
  "steps": [
    {
      "step_id":     "<snake_case>",
      "label":       "<human label>",
      "intent":      "<verb phrase>",
      "inputs":      ["<step_id or model_input_name>"],
      "outputs":     ["<output_name>"],
      "constraints": { "<param>": "<value or constraint>" }
    }
  ],
  "model_inputs": [
    {
      "name":        "<snake_case>",
      "kind":        "vectorlayer|rasterlayer|number|string|boolean|field|expression|crs",
      "label":       "<human label>",
      "description": "<optional>",
      "optional":    false
    }
  ]
}
Rules:
- step_ids: unique snake_case
- intents: concise action phrases
- keep step labels and intents in the same language as the user goal text
- do not use algorithm IDs in steps
Return ONLY valid JSON.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "description":  {"type": "string"},
        "qgis_context": {"type": "object"},
        "model_name":   {"type": "string"},
        "model_group":  {"type": "string"},
    },
    "required": ["description", "qgis_context"],
}


def build_user_message(args: dict) -> str:
    ctx = args.get("qgis_context", {})
    layers = ctx.get("layers", [])
    alg_ids = list(ctx.get("algorithms", {}).keys())

    layer_lines = []
    for layer in layers[:8]:
        fields = [f.get("name", "") for f in layer.get("fields", [])[:8]]
        fields_txt = ", ".join([f for f in fields if f]) or "-"
        layer_lines.append(
            f"- {layer.get('name', 'layer')} [{layer.get('type', 'unknown')}] "
            f"crs={layer.get('crs', '?')} fields={fields_txt}"
        )
    if len(layers) > 8:
        layer_lines.append(f"- ... {len(layers) - 8} more layer(s)")

    algo_preview = alg_ids[:80]
    algo_lines = [f"- {alg_id}" for alg_id in algo_preview]
    if len(alg_ids) > len(algo_preview):
        algo_lines.append(f"- ... {len(alg_ids) - len(algo_preview)} more algorithm id(s)")

    return (
        f"Model name: {args.get('model_name', 'workflow')}\n"
        f"Model group: {args.get('model_group', 'ModelForge')}\n\n"
        f"Goal:\n{args['description']}\n\n"
        f"Project CRS: {ctx.get('project_crs') or 'unknown'}\n\n"
        f"Layers:\n{chr(10).join(layer_lines) if layer_lines else '- none'}\n\n"
        f"Available algorithm IDs (reference only):\n{chr(10).join(algo_lines) if algo_lines else '- none'}\n\n"
        "Produce the SemanticPlan JSON."
    )
