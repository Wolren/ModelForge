"""Outer MCP prompt templates.

These are ``@mcp.prompt`` registrations. Clients (Claude Desktop,
Cursor, Cline, Continue) expose them as named, fillable templates so
the user (or the agent) can start a chat primed for a specific
workflow. Each prompt points at the right tool, not at the raw
``generate_model`` plumbing, so the model has clear guidance about
which tools to use and in what order.

The prompts are designed to be **short and self-contained**; we do
not dump the full inner-MCP prompt bodies here. The inner tools
already encode the QGIS-specific rules.
"""

from __future__ import annotations

from typing import Any

# --- generate_model_from_intent -----------------------------------------

_GENERATE_MODEL_FROM_INTENT = """\
You are a QGIS Processing model designer. Translate the user's
request into a working QGIS Processing model.

## Workflow

1. Inspect the project with `list_layers` and `get_project_info`
   to see what layers, CRS, and field names are actually available.
2. Use `list_algorithms` (filter by query) and `get_algorithm_info`
   to confirm the algorithm IDs, parameter names, and output port
   names you'll need.
3. Read the user's intent carefully and plan a minimal, well-
   connected sequence. Prefer fewer steps that each do a clear
   thing over a long pipeline of small steps.
4. If the LLM backend is not yet configured, call `set_llm_config`
   first with the user's preferred provider and model.
5. Call `generate_model` with:
   - ``description``: the user's intent, possibly enriched with
     concrete numbers / fields the project context revealed.
   - ``model_name``, ``model_group``: human-readable.
   - ``progress_token``: a stable string so the client can show
     streaming progress.
6. If `generate_model` returns issues, treat them as
   ``PLANNER_QUESTION`` or ``PLANNER_NEEDS_REVIEW`` rows in
   ``_mf_plan_issues`` and address them. Use `export_model` to
   get the final artifact (model3, mermaid, script, or json).
7. If the user asks for clarification, prefer to ask a single
   focused question rather than guessing.

## Don'ts

- Do not invent algorithm IDs. Use `list_algorithms` first.
- Do not assume a field name; the project's actual field list is in
  `list_layers`. Confirm by name.
- Do not over-engineer. If the user's request is "buffer the
  roads by 50m", a single `native:buffer` step is the right answer.
"""

_EXPLAIN_MODEL = """\
You are explaining a QGIS Processing model to a user. The model JSON
schema is documented in `summarize_model`; use that tool first to
get the high-level summary, then read individual steps from the
returned ``model`` object as needed.

## Workflow

1. Call `summarize_model` with the model's JSON to get a one-screen
   overview of inputs and steps.
2. For each algorithm step, look up the algorithm's expected
   parameters and outputs with `get_algorithm_info` so you can
   explain what each binding does in plain English.
3. Build a human-readable explanation in this order:
   - What the model does in one sentence.
   - The inputs (with their type, e.g. "a vector layer", "a number").
   - The output: which step produces the final result, and what
     format / type the output is.
   - Step-by-step: for each step, what algorithm it runs, what each
     parameter is bound to, and why.
   - Known issues: any ``PLANNER_QUESTION`` or
     ``PLANNER_NEEDS_REVIEW`` rows from ``_mf_plan_issues``.
4. If the model is broken (BLOCKED steps, missing bindings, or
   references to unknown algorithms), call those out explicitly
   with the exact `step_id` and what is wrong.

## Style

- Plain English, no jargon unless defined.
- A user with QGIS experience should be able to look at your
  explanation and immediately know whether the model does what
  they want.
- When quoting a parameter, use the exact name from
  `get_algorithm_info` so the user can search for it.
"""

_CONVERT_SCRIPT_TO_MODEL = """\
You are converting a standalone QGIS Processing Python script
(``processing.run(...)`` style) into a QGIS Processing model
(``.model3``) JSON.

## Workflow

1. Read the script. Identify:
   - Each call to ``processing.run(...)`` - that's a step.
   - The arguments to each call - those become parameter bindings.
   - Outputs that are passed as inputs to subsequent calls - those
     are ``child_output`` links in the model.
   - Inputs the user provides (vector/raster/number/string/boolean)
     - those are ``model_input`` entries.
2. Call `list_algorithms` to confirm the algorithm IDs and exact
   parameter / output names. Scripts often use friendly names or
   omit prefixes; the catalog is the source of truth.
3. Call `get_algorithm_info` for each algorithm used in the script
   so you have the canonical parameter list and the canonical
   output port names (``OUTPUT``, ``RESULT``, ``OUTPUT_LAYER``,
   etc.).
4. Build a `model_json` dict with the structure:
   - ``model_name``, ``model_group``
   - ``inputs`` - one entry per script parameter that came from the
     user (a layer, a number, etc.).
   - ``algorithms`` - one entry per script algorithm call:
     - ``id``: snake_case step id
     - ``algorithm_id``: from `get_algorithm_info`
     - ``parameters``: each script argument, with ``type`` set to
       ``model_input`` / ``child_output`` / ``static`` / ``expression``
       as appropriate.
5. Validate the result with `validate_model`. If issues appear,
   address them before returning.

## Style

- Preserve the script's *behaviour* exactly. If the script set a
  parameter to a literal, set the model's static binding to that
  literal. Don't try to "improve" the script.
- Surface any *implicit* assumptions the script made (e.g. CRS
  choices, hard-coded paths) as ``open_questions`` in the
  generated plan so the user can confirm them.
"""


# --- Public registration -----------------------------------------------


def register_prompts(mcp: Any) -> None:
    """Register all outer-MCP prompt templates on the FastMCP app."""

    @mcp.prompt(
        name="generate_model_from_intent",
        description=(
            "Prime the model to design a QGIS Processing model from a "
            "user's natural-language request. Use the tools in the order "
            "described: list_layers → list_algorithms → get_algorithm_info → "
            "generate_model → export_model."
        ),
    )
    def _generate_model_from_intent(intent: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": _GENERATE_MODEL_FROM_INTENT,
                },
            },
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"User intent:\n\n{intent}\n\n"
                        "Begin by calling list_layers and get_project_info "
                        "to see the project's actual layers, CRS, and fields."
                    ),
                },
            },
        ]

    @mcp.prompt(
        name="explain_model",
        description=(
            "Prime the model to explain a QGIS Processing model JSON in "
            "plain English. Walks through inputs, steps, parameter "
            "bindings, and any planner issues."
        ),
    )
    def _explain_model(model_json: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": _EXPLAIN_MODEL,
                },
            },
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        "Model JSON (stringified; pass to summarize_model):\n\n"
                        f"{model_json}\n\n"
                        "Begin by calling summarize_model on this JSON."
                    ),
                },
            },
        ]

    @mcp.prompt(
        name="convert_script_to_model",
        description=(
            "Prime the model to convert a standalone QGIS Processing "
            "Python script into a QGIS Processing model JSON. Walks "
            "through identifying processing.run() calls, parameters, "
            "and output linkages."
        ),
    )
    def _convert_script_to_model(script: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": _CONVERT_SCRIPT_TO_MODEL,
                },
            },
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        "Python script (standalone QGIS Processing runnable):\n\n"
                        f"```python\n{script}\n```\n\n"
                        "Begin by parsing the script for processing.run() calls "
                        "and listing the relevant algorithms."
                    ),
                },
            },
        ]

    @mcp.prompt(
        name="generate_complete_map_from_intent",
        description=(
            "Prime the model to take a user's plain-language request and "
            "produce a complete map deliverable: a QGIS Processing model, "
            "a print layout template, and per-layer symbology. The workflow "
            "is chained and re-uses MCP tools: generate_model → "
            "generate_symbology → generate_print_layout → verify_layout "
            "→ (re-emit on violations) → export_layout."
        ),
    )
    def _generate_complete_map_from_intent(intent: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        "You are a GIS cartographer. The user wants a complete map "
                        "deliverable from this intent:\n\n"
                        f"{intent}\n\n"
                        "Chain the tools in this order:\n"
                        "1. ``generate_model`` (model JSON)\n"
                        "2. ``generate_symbology`` (per-layer .qml files)\n"
                        "3. ``generate_print_layout`` (a .qpt template)\n"
                        "4. ``verify_layout`` (run the ruleset; if violations, "
                        "revise the parameters and call ``generate_print_layout`` "
                        "again - max 2 retries)\n"
                        "5. ``export_layout`` (render to PDF)\n\n"
                        "Pass the project CRS to the layout tool so the "
                        "scientific template can include it in the metadata "
                        "block. Pick a template based on the intent: "
                        "'scientific' for academic, 'presentation' for "
                        "client-facing, 'default' for internal, 'minimal' "
                        "when the user wants a one-image map."
                    ),
                },
            },
        ]

    @mcp.prompt(
        name="generate_print_layout_from_model",
        description=(
            "Prime the model to design a QGIS print layout template "
            "(.qpt) for an existing model JSON. The LLM writes "
            "title/subtitle text; the layout builder does geometry. "
            "The verifier checks structural correctness; the LLM "
            "adjusts parameters and re-emits if the ruleset reports "
            "violations."
        ),
    )
    def _generate_print_layout_from_model(model_json: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        "You are designing a print layout. Read the model JSON, "
                        "infer the project's intent from the layer names, and "
                        "call ``generate_print_layout`` with appropriate "
                        "title, subtitle, and template. Then call "
                        "``verify_layout`` and, if it reports violations, "
                        "revise the parameters and call ``generate_print_layout`` "
                        "again. Max 2 retries. The user can re-run this prompt "
                        "later to adjust.\n\n"
                        f"Model JSON:\n\n```json\n{model_json}```\n"
                    ),
                },
            },
        ]

    @mcp.prompt(
        name="generate_symbology_from_model",
        description=(
            "Prime the model to design per-layer QML symbology for a "
            "model JSON. The LLM picks renderers and field choices "
            "based on layer geometry and intent; the QML builder "
            "emits the .qml files."
        ),
    )
    def _generate_symbology_from_model(model_json: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        "You are designing per-layer symbology. Read the model "
                        "JSON and call ``generate_symbology`` with a sensible "
                        "default renderer (single_symbol for point/line, "
                        "graduated for numeric fields, categorized for "
                        "categorical fields). For outputs that are clearly "
                        "intermediate (e.g. a temporary buffer), keep the "
                        "symbology simple.\n\n"
                        f"Model JSON:\n\n```json\n{model_json}\n```\n"
                    ),
                },
            },
        ]


PROMPT_REGISTRY: tuple[str, ...] = (
    "generate_model_from_intent",
    "explain_model",
    "convert_script_to_model",
    "generate_complete_map_from_intent",
    "generate_print_layout_from_model",
    "generate_symbology_from_model",
)

__all__ = ["PROMPT_REGISTRY", "register_prompts"]
