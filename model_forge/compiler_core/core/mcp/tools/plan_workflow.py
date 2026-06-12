"""
MCP Tool: plan_workflow
Decomposes a natural-language geoprocessing description into a SemanticPlan.
"""

SYSTEM_PROMPT = """\
You are a QGIS Processing workflow planner.

Given a user goal and the project's current QGIS state, output exactly
one JSON object matching the SemanticPlan schema below. Do not
include any prose, code fences, or commentary.

# SemanticPlan schema
{
  "goal_summary": "<one sentence restating the user's goal>",
  "steps": [
    {
      "step_id":        "<snake_case, unique across all steps>",
      "label":          "<short human-readable label, same language as the user goal>",
      "intent":         "<verb phrase: e.g. 'Buffer the input by 50m', 'Extract by attribute', 'Reproject to EPSG:4326'>",
      "algorithm_id":   "<provider:algorithm or null if uncertain>",
      "inputs":         ["<step_id of an earlier step, or a model_input name>"],
      "outputs":        ["<output port name; use 'OUTPUT' if unsure>"],
      "constraints":    { "<param_name>": <value> },
      "needs_review":   false
    }
  ],
  "model_inputs": [
    {
      "name":         "<snake_case, must match a layer in the project>",
      "kind":         "vectorlayer|rasterlayer|number|string|boolean|field|expression|crs",
      "label":        "<human label>",
      "description":  "<optional>",
      "optional":     false
    }
  ],
  "open_questions": ["<questions to ask the user if anything is ambiguous>"]
}

# Rules
1. **Use the project's actual layer names** in model_inputs.name. If a
   referenced layer isn't in the supplied Layers list, set
   open_questions to flag it; do not invent layer names.
2. **step_id** must be unique snake_case, lower-case letters/digits/_
   only, no leading digit.
3. **algorithm_id** is allowed and encouraged. Use the format
   "provider:base" (e.g. "native:buffer", "gdal:cliprasterbyextent").
   Prefer "native:" for built-in QGIS algorithms. Set to null if no
   suitable algorithm exists — the resolver will then mark the step
   "blocked" and a warning will be surfaced to the user.
4. **inputs** lists step_ids of upstream steps or model_input names;
   the order doesn't matter. The pipeline derives dependencies from
   this field; you do not need a separate "depends_on".
5. **constraints** values must be one of:
   - number (e.g. 50, 1.5)
   - string (e.g. "EPSG:4326", "memory:")
   - boolean (true / false)
   - object: {"field": "name", "op": ">", "value": 100} for filters
   - object: {"type": "enum", "option": "ROUND"} for enum picks
   Never use placeholders like "<value>" or "?".
6. **needs_review** should be true when the LLM had to guess at
   something it cannot verify from the supplied context (e.g. a
   specific CRS, a field name not in the layer schema).
7. **Intent granularity**: one algorithm per step. Don't combine
   "buffer + dissolve + clip" into a single step.
8. **Output ports**: name them after the algorithm's actual output
   (e.g. "OUTPUT", "RESULT", "OUTPUT_LAYER") if known; otherwise
   "OUTPUT" is the safe default.
9. If the goal is underspecified (e.g. "clean my data"), populate
   open_questions with the specific clarifications you need; do not
   fabricate steps to fill the plan.
10. Keep all strings in the same language as the user goal.

# Worked example
Goal: "Buffer the roads layer by 50 meters, then keep only segments longer than 1 km."

Response:
{
  "goal_summary": "Buffer the roads layer by 50m and keep only segments longer than 1km.",
  "steps": [
    {
      "step_id": "buffer_roads",
      "label": "Buffer roads 50m",
      "intent": "Buffer the roads layer by 50 meters",
      "algorithm_id": "native:buffer",
      "inputs": ["roads"],
      "outputs": ["OUTPUT"],
      "constraints": {"DISTANCE": 50, "SEGMENTS": 8, "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2},
      "needs_review": false
    },
    {
      "step_id": "extract_long",
      "label": "Keep segments > 1 km",
      "intent": "Filter to features whose length exceeds 1000 metres",
      "algorithm_id": "native:extractbyexpression",
      "inputs": ["buffer_roads"],
      "outputs": ["OUTPUT"],
      "constraints": {"EXPRESSION": {"field": "$length", "op": ">", "value": 1000}},
      "needs_review": false
    }
  ],
  "model_inputs": [
    {"name": "roads", "kind": "vectorlayer", "label": "Roads", "description": "", "optional": false}
  ],
  "open_questions": []
}

Return ONLY valid JSON matching the schema above. No markdown.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "qgis_context": {"type": "object"},
        "model_name": {"type": "string"},
        "model_group": {"type": "string"},
    },
    "required": ["description", "qgis_context"],
}


def build_user_message(args: dict) -> str:
    ctx = args.get("qgis_context", {})
    layers = ctx.get("layers", [])

    # Full layer list (no truncation) — the LLM needs to know exactly
    # which layer names exist. Field names and CRS are included
    # because they are needed for the worked-example's expression
    # field-name references. Truncation here caused the model to
    # invent layer names in the past.
    layer_lines = []
    for layer in layers:
        fields = layer.get("fields", []) or []
        if isinstance(fields, list) and fields and isinstance(fields[0], dict):
            field_names = [str(f.get("name", "")) for f in fields]
        else:
            field_names = [str(f) for f in fields]
        fields_txt = ", ".join([f for f in field_names if f]) or "-"
        layer_lines.append(
            f"- {layer.get('name', 'layer')!r} "
            f"[{layer.get('type', 'unknown')}] "
            f"crs={layer.get('crs') or '?'} "
            f"feature_count={layer.get('feature_count', '?')} "
            f"fields=[{fields_txt}]"
        )

    return (
        f"Model name: {args.get('model_name', 'workflow')}\n"
        f"Model group: {args.get('model_group', 'ModelForge')}\n\n"
        f"Goal:\n{args['description']}\n\n"
        f"Project CRS: {ctx.get('project_crs') or 'unknown'}\n\n"
        f"Layers in the project (use these names verbatim in model_inputs):\n"
        f"{chr(10).join(layer_lines) if layer_lines else '- none'}\n\n"
        f"Produce the SemanticPlan JSON."
    )
