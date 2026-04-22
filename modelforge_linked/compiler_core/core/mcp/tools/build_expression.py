"""
MCP Tool: build_expression
Translates a natural-language constraint to an ExpressionNode + rendered QGIS string.
"""
import json

SYSTEM_PROMPT = """\
You are a QGIS expression builder.
Convert a natural language constraint to an ExpressionNode and a rendered
QGIS expression string. Return JSON with NO markdown:
{
  "node_type": "comparison|logical|function|literal|field_ref",
  "operator":  "<op>",
  "left":  { <node> },
  "right": { <node> },
  "value": null,
  "field_name": null,
  "function_name": null,
  "arguments": [],
  "rendered": "<QGIS expression>"
}
Use double quotes for field names, single quotes for string literals.
Return ONLY valid JSON.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "constraint_text": {"type": "string"},
        "layer_fields":    {"type": "array"},
    },
    "required": ["constraint_text"],
}


def build_user_message(args: dict) -> str:
    fields_txt = json.dumps(args.get("layer_fields", []))
    return (
        f"Constraint: {args['constraint_text']}\n"
        f"Available fields: {fields_txt}\n\n"
        f"Build an ExpressionNode with rendered QGIS expression string."
    )
