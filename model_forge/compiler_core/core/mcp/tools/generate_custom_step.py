"""
MCP Tool: generate_custom_step
Generates a CustomStepSpec from a natural-language description.
"""
import json

SYSTEM_PROMPT = """\
You are a QGIS custom algorithm author.
Given a description, produce a CustomStepSpec JSON with NO markdown:

IMPORTANT OUTPUT TYPE GUIDANCE:
- If the description mentions adding features, calculating values that will flow to subsequent steps, 
  or saving results to the project, output a "vector" layer (kind: "vector"), NOT just a number.
- Use "number" output ONLY for final scalar results that don't need to connect to other algorithms.
- When in doubt about chaining, prefer "vector" output so it can be linked in the model builder.

Output schema:
{
  "step_id":      "<snake_case>",
  "display_name": "<human label>",
  "group":        "<group name>",
  "group_id":     "<snake_case>",
  "help_text":    "<one sentence>",
  "parameters": [
    {
      "name":        "<snake_case>",
      "kind":        "vectorlayer|rasterlayer|number|string|boolean|field|sink",
      "description": "<label>",
      "optional":    false
    }
  ],
  "outputs": [
    {
      "name":        "<snake_case>",
      "kind":        "vector|raster|number|string",
      "description": "<label>"
    }
  ],
  "code_body": "<Python body using uppercase param variable names>"
}
code_body rules: raise QgProcessingException for errors, return dict of outputs,
NO iface/QMessageBox/os.system/subprocess.
Return ONLY valid JSON.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "context":     {"type": "object"},
    },
    "required": ["description"],
}


def build_user_message(args: dict) -> str:
    ctx_txt = json.dumps(args.get("context", {}), indent=2)
    return (
        f"Description: {args['description']}\n\n"
        f"Context:\n{ctx_txt}\n\n"
        f"Generate a CustomStepSpec."
    )
