"""
MCP Tool: plan_workflow
Decomposes a natural-language geoprocessing description into a SemanticPlan.
"""
import json

SYSTEM_PROMPT = """\
You are a QGIS geoprocessing workflow planner.
Given a natural-language description and a QGIS context, produce a SemanticPlan
as a single JSON object with NO markdown fencing.

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
Rules: step_ids must be unique snake_case. Do NOT use algorithm IDs.
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
    layers_txt = json.dumps(ctx.get("layers", []), indent=2)
    algos_txt  = json.dumps(list(ctx.get("algorithms", {}).keys()), indent=2)
    return (
        f"Model name: {args.get('model_name', 'workflow')}\n"
        f"Model group: {args.get('model_group', 'ModelForge')}\n\n"
        f"DESCRIPTION:\n{args['description']}\n\n"
        f"AVAILABLE LAYERS:\n{layers_txt}\n\n"
        f"AVAILABLE ALGORITHM IDs (reference only):\n{algos_txt}\n\n"
        f"Produce a SemanticPlan JSON."
    )
