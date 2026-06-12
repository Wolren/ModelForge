"""
MCP Tool: resolve_algorithms
Maps each semantic step intent to a concrete QGIS algorithm_id + bindings.
"""

import json
import re
from typing import Any

SYSTEM_PROMPT = """\
You are a QGIS algorithm resolver.

The upstream planner has already decomposed the user's goal into a
SemanticPlan and, for each step, supplied a ``constraints`` dict with
parameter hints. Your job is to produce the concrete parameter
bindings for every step.

Return a single JSON object with NO markdown, NO code fences, NO prose.

# Output schema
{
  "resolved_steps": [
    {
      "step_id":       "<matches the planner's step_id>",
      "algorithm_id":  "<provider:base; keep the planner's choice if it resolves to a real algorithm>",
      "confidence":    0.0,
      "status":        "resolved|assumed|blocked",
      "reason":        "<one sentence, only if status is blocked>",
      "parameters": {
        "<param_name>": {
          "source_type":   "model_input|child_output|static|expression|enum_index",
          "model_input":   "<name of the upstream model_input>",
          "child_id":      "<step_id of the upstream step that produces this input>",
          "output_name":   "<port name on the upstream step; usually 'OUTPUT'>",
          "static_value":  null,
          "enum_index":    0
        }
      }
    }
  ]
}

# source_type cheat sheet (with concrete examples)
- "model_input"  → bind to a project input declared in model_inputs.
                   Example: bind native:buffer's INPUT to the "roads" layer.
                   Set "model_input": "roads". All other fields null.
- "child_output" → bind to a previous step's output.
                   Example: bind extract's INPUT to buffer_roads' OUTPUT.
                   Set "child_id": "buffer_roads", "output_name": "OUTPUT".
- "static"       → a literal value (number, string, boolean, file path).
                   Set "static_value" to the literal; everything else null.
- "enum"         → bind to a specific enum option by its LABEL.
                   Use "source_type": "static" + "static_value": "<label>"
                   for the resolver pipeline; the link_repair stage will
                   translate labels to indices.
- "expression"   → a QGIS expression string for fields/filter logic.
                   Set "static_value" to the QGIS expression
                   (e.g. '"area" > 1000') and the validator will wrap it.

For each parameter, fill the field relevant to source_type and set
the others to null. Do not return unused fields with non-null values.

# Rules
1. If the planner supplied algorithm_id and it is in the catalog
   subset, keep it. If you cannot validate it (it's not in the
   supplied catalog and the constraint is non-trivial), set
   status="assumed" and confidence <= 0.7.
2. If no algorithm fits at all, set status="blocked" and explain
   why in "reason". Do not invent algorithm IDs.
3. Param names MUST be exact (case-sensitive) matches to the
   algorithm definition in the catalog.
4. When a step needs the OUTPUT of a previous step, use the actual
   step_id of that previous step, not its position or index.
5. Set confidence to:
   - 1.0  when the planner's algorithm_id is confirmed by the catalog
          AND every parameter binding is unambiguous
   - 0.7-0.9  when the algorithm resolves cleanly but some parameter
              value had to be inferred
   - 0.3-0.6  when the algorithm is a guess and may need user review
   - 0.0      for blocked steps

Return ONLY valid JSON matching the schema above.
"""


SCHEMA = {
    "type": "object",
    "properties": {
        "semantic_plan": {"type": "object"},
        "algorithm_catalog": {"type": "object"},
    },
    "required": ["semantic_plan", "algorithm_catalog"],
}


# Stopwords excluded from relevance scoring.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "step",
        "steps",
        "use",
        "using",
    }
)


def _tokenize(text: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z0-9]+", (text or "").lower())
        if tok and tok not in _STOPWORDS
    }


def _relevance_score(alg_id: str, info: dict, intent_tokens: set[str]) -> float:
    """Score how relevant an algorithm is to the planner's intent.

    Uses a cheap token overlap: name + id + group tokens vs intent.
    Returns 0 for empty intent so the caller can fall back to the
    catalog order.
    """
    if not intent_tokens:
        return 0.0
    name = str(info.get("name", ""))
    group = str(info.get("group", ""))
    haystack = f"{alg_id} {name} {group}".lower()
    haystack_tokens = _tokenize(haystack)
    if not haystack_tokens:
        return 0.0
    overlap = len(intent_tokens & haystack_tokens)
    # Normalize to 0..1 by the size of the intent token set, so a
    # short intent still scores a useful 1.0 when fully covered.
    return overlap / max(1, len(intent_tokens))


def _build_user_message(plan: dict, catalog: dict, max_algs: int = 40) -> str:
    compact_steps = []
    for step in plan.get("steps", []):
        compact_steps.append(
            {
                "step_id": step.get("step_id"),
                "label": step.get("label"),
                "intent": step.get("intent"),
                "inputs": step.get("inputs", []),
                "outputs": step.get("outputs", []),
                "constraints": step.get("constraints", {}),
                # The planner may have already chosen an algorithm;
                # surface it explicitly so the resolver trusts it.
                "planner_algorithm_id": step.get("algorithm_id"),
            }
        )
    compact_plan = {
        "goal_summary": plan.get("goal_summary", ""),
        "model_inputs": plan.get("model_inputs", []),
        "steps": compact_steps,
    }
    plan_txt = json.dumps(compact_plan, indent=2, ensure_ascii=False)

    # Build a per-intent ranked catalog. We union the top-N per
    # step so the resolver sees the planner's algorithm choices
    # *and* close-by alternatives. Cap to ``max_algs`` so we stay
    # inside the LLM's context window.
    catalog_items = list(catalog.items())
    selected: dict[str, dict[str, Any]] = {}
    for step in plan.get("steps", []):
        intent_tokens = _tokenize(
            " ".join(
                [
                    str(step.get("intent", "")),
                    str(step.get("label", "")),
                    *[str(c) for c in (step.get("constraints") or {}).keys()],
                ]
            )
        )
        # Always include the planner's chosen algorithm first if it
        # is in the catalog, so the resolver sees it.
        chosen = step.get("algorithm_id")
        if chosen and chosen in catalog:
            selected[chosen] = {
                "info": catalog[chosen],
                "score": 1.0,
            }
        ranked = sorted(
            catalog_items,
            key=lambda kv: _relevance_score(kv[0], kv[1], intent_tokens),
            reverse=True,
        )
        for alg_id, info in ranked:
            if alg_id in selected:
                continue
            selected[alg_id] = {
                "info": info,
                "score": _relevance_score(alg_id, info, intent_tokens),
            }
            if len(selected) >= max_algs:
                break

    lines = []
    for alg_id, entry in selected.items():
        info = entry["info"]
        pnames = [p["name"] for p in info.get("parameters", [])[:10]]
        onames = [o["name"] for o in info.get("outputs", [])[:4]]
        suffix = f" outputs=[{', '.join(onames)}]" if onames else ""
        lines.append(f"  {alg_id}: {info.get('name', '')} params=[{', '.join(pnames)}]{suffix}")

    return (
        "SEMANTIC PLAN:\n"
        + plan_txt
        + "\n\nALGORITHM CATALOG (ranked by relevance to the plan):\n"
        + ("\n".join(lines) if lines else "  <empty>")
        + "\n\nResolve each step. Trust the planner's algorithm_id when "
        "it's in the catalog; only change it if there's a clearly "
        "better alternative. Use EXACT parameter names and output port "
        "names from the catalog."
    )


def build_user_message(args: dict) -> str:
    return _build_user_message(
        args.get("semantic_plan", {}),
        args.get("algorithm_catalog", {}),
    )
