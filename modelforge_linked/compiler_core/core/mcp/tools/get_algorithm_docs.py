"""
MCP Tool: get_algorithm_docs
Returns structured documentation for a QGIS algorithm.
"""
import json

SYSTEM_PROMPT = """\
You are a QGIS algorithm documentation assistant.
Given an algorithm_id and its registry metadata, produce structured docs.

Return JSON with NO markdown:
{
  "algorithm_id": "<id>",
  "display_name": "<name>",
  "description": "<two sentence description>",
  "typical_use_cases": ["<use case>"],
  "parameter_notes": { "<param_name>": "<usage note>" },
  "gotchas": ["<common mistake>"]
}
Return ONLY valid JSON.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "algorithm_id":  {"type": "string"},
        "registry_meta": {"type": "object"},
    },
    "required": ["algorithm_id"],
}


def build_user_message(args: dict) -> str:
    meta_txt = json.dumps(args.get("registry_meta", {}), indent=2)
    return (
        f"Algorithm: {args['algorithm_id']}\n\n"
        f"Registry metadata:\n{meta_txt}\n\n"
        f"Produce structured documentation."
    )
