"""
MCP Tool: suggest_layout
Suggests semantic groups and annotations for graph layout.
"""
import json

SYSTEM_PROMPT = """\
You are a QGIS model graph layout advisor.
Suggest semantic groups and annotation labels to improve readability.

Return JSON with NO markdown:
{
  "groups": [
    {
      "group_id": "<snake_case>",
      "label":    "<human label>",
      "step_ids": ["<step_id>"]
    }
  ],
  "annotations": [
    {
      "text":           "<annotation text>",
      "attach_to_step": "<step_id or null>"
    }
  ]
}
Return ONLY valid JSON.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array"},
    },
    "required": ["steps"],
}


def build_user_message(args: dict) -> str:
    steps_txt = json.dumps(args.get("steps", []), indent=2)
    return (
        f"Steps (with dependencies):\n{steps_txt}\n\n"
        f"Suggest semantic groups and annotations."
    )
