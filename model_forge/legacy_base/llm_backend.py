"""
LLM Backend for Model Forge.
Two-phase generation (plan -> build), repair, robust JSON parsing, retry with backoff.
Supports Ollama, OpenAI-compatible (DeepSeek, Qwen Cloud, etc.).
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

SYSTEM_PROMPT_PLAN = """You are a QGIS Processing workflow planner.

Your job: convert a natural language task + an algorithm catalog into a FORMAL PLAN
that the next system will turn into a QGIS model JSON.

### KEY IDEAS

- You are NOT generating model JSON here.
- You are defining a small, explicit DAG of processing steps and model inputs.
- The next system will map each plan step directly to one QGIS model algorithm node.

### OUTPUT FORMAT (MUST FOLLOW EXACTLY)

You MUST respond with ONE JSON object of the form:

{
  "plan_steps": [
    {
      "index": 0,
      "label": "short human label (max 8 words)",
      "algorithm_id": "exact Processing algorithm ID from catalog",
      "inputs_from": ["model_input", 0, 2],
      "uses_inputs": ["input_vector", "threshold"],
      "notes": "1-2 short sentences, high-level reasoning only"
    }
  ],
  "model_inputs": [
    {
      "name": "input_vector",
      "label": "Input layer",
      "type": "vector",
      "geometry": -1,
      "used_in_steps": [0, 1]
    }
  ]
}

### CRITICAL RULES

1. Output ONLY VALID JSON. No markdown, no code fences, no comments or text outside JSON.
2. Use ONLY algorithm IDs that appear in the provided catalog. Never invent IDs.
3. Each plan step MUST have:
   - "index": integer, 0-based, unique per step
   - "label": short human-friendly label (max 8 words)
   - "algorithm_id": exact ID from catalog
   - "inputs_from": list containing either "model_input" or step indices (integers) whose outputs feed this step
   - "uses_inputs": list of model input names whose values are needed here
   - "notes": short reasoning (1-2 sentences max)
4. "model_inputs" MUST:
   - Use simple, lowercase_snake_case "name" values.
   - Use "type" from: vector, raster, number, string, field, crs, boolean.
   - For type "vector", include "geometry" (-1 any, 0 point, 1 line, 2 polygon).
   - List in "used_in_steps" all step indices that depend on this input.
5. Keep between 2 and 12 plan_steps. Prefer the most concise chain that achieves the task.
6. If the task can be done with native:extractbyexpression instead of multiple
   native:extractbyattribute calls, prefer the expression approach.

Remember: you are defining a clean, minimal DAG structure for the next stage.
RESPOND WITH ONLY THE JSON OBJECT DESCRIBED ABOVE."""


SYSTEM_PROMPT_BUILD = """You are a QGIS Processing Model code generator. Given a HIGH-LEVEL PLAN and the EXACT parameter signatures from the user's QGIS installation, produce a model JSON definition.

CRITICAL RULES - VIOLATING ANY WILL BREAK THE MODEL:

1. Output ONLY VALID JSON. No markdown, no code fences, no text outside JSON.
2. Use ONLY algorithm IDs from the plan and catalog. Never invent IDs.
3. Use ONLY parameter names that appear in the catalog signatures. Spell them EXACTLY.

4. ENUM PARAMETERS: When a parameter has type "enum", you MUST use the INTEGER INDEX, never the string label.
   Examples from the catalog:
   - OPERATOR: 0=equals, 1=not equal, 2=greater than, 3=>=, 4=<, 5=<=, 6=begins with, 7=contains, 8=is null, 9=is not null, 10=does not contain
   - FIELD_TYPE: 0=Float, 1=Integer, 2=String, 3=Date
   WRONG: {"type": "static", "value": "="}
   CORRECT: {"type": "static", "value": 0}

5. BOOLEAN PARAMETERS: Use true/false (JSON booleans), not strings.
   WRONG: {"type": "static", "value": "True"}
   CORRECT: {"type": "static", "value": true}

6. Parameter sources MUST be objects with a "type" field:
   - {"type": "model_input", "name": "<input_name>"} for model inputs
   - {"type": "child_output", "child_id": "<step_id>", "output_name": "OUTPUT"} for previous step outputs
   - {"type": "static", "value": <value>} for hardcoded values (use integer for enums!)
   - {"type": "expression", "expression": "<qgis_expression>"} for expressions

7. Model inputs: each needs "name" (lowercase_snake), "label" (human), "type" (vector/raster/number/string/field/crs/boolean).
   For vector inputs, add "geometry" (-1=any, 0=point, 1=line, 2=polygon).

8. For OUTPUT/destination parameters: use {"type": "static", "value": "TEMPORARY_OUTPUT"} or omit them entirely.
   For GeoPackage outputs, use ONLY the .gpkg file path. Do NOT append |layername=... to OUTPUT values.

9. Algorithm steps: each needs "id" (unique string like "step0"), "algorithm_id", "description" (max 8 words), "parameters" dict.

10. Keep the model concise. Prefer fewer steps. If native:extractbyexpression can replace multiple native:extractbyattribute calls, use it.

You MUST always include BOTH top-level keys:
- "inputs": [...]
- "algorithms": [...]

Do NOT omit "algorithms" even if it is an empty list.

RESPOND WITH ONLY:
{
  "inputs": [...],
  "algorithms": [...]
}"""


SYSTEM_PROMPT_REPAIR = """You are a QGIS Processing Model debugger and improver. Given a model JSON and validation errors or user feedback, fix and improve the JSON.

CRITICAL RULES:
1. Output ONLY the corrected full JSON. No markdown, no code fences, no commentary.
2. Fix what the errors describe. If user feedback requests improvements or additions, address those too.
3. Common fixes:
   - ENUM values must be INTEGER INDICES (e.g., OPERATOR: 0 for =, 2 for >, etc.), never strings like "=" or ">"
   - FIELD_TYPE must be integer (0=Float, 1=Integer, 2=String, 3=Date)
   - Boolean values must be true/false, not "True"/"False"
   - Missing parameters: add them with sensible defaults from the catalog
   - Wrong child_id references: fix to match actual step IDs
   - For GeoPackage outputs, use ONLY the .gpkg file path. Do NOT append |layername=...
4. You MUST always include BOTH top-level keys: "inputs" and "algorithms".

RESPOND WITH ONLY the corrected JSON:
{
  "inputs": [...],
  "algorithms": [...]
}"""


class LLMBackend:
    # Predefined backend profiles
    BACKENDS = {
        "ollama": {
            "label": "Ollama (Local)",
            "default_url": "http://localhost:11434",
            "default_model": "gpt-oss:20b-cloud",
        },
        "openai": {
            "label": "OpenAI",
            "default_url": "https://api.openai.com/v1",
            "default_model": "gpt-4o-mini",
        },
        "deepseek": {
            "label": "DeepSeek",
            "default_url": "https://api.deepseek.com/v1",
            "default_model": "deepseek-chat",
        },
        "qwen": {
            "label": "Qwen Cloud",
            "default_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "default_model": "qwen-plus",
        },
        "openrouter": {
            "label": "OpenRouter",
            "default_url": "https://openrouter.ai/api/v1",
            "default_model": "qwen/qwen3-coder:free",
        },
        "custom": {
            "label": "Custom (OpenAI-compatible)",
            "default_url": "",
            "default_model": "",
        },
    }

    def __init__(self):
        self.backend = "ollama"
        self.url = "http://localhost:11434"
        self.api_key = ""
        self.model = ""
        self.temperature = 0.2

    def configure(self, backend="ollama", url="", api_key="", model="", temperature=0.2):
        self.backend = backend
        profile = self.BACKENDS.get(backend, self.BACKENDS["custom"])
        self.url = url or profile["default_url"]
        self.model = model or profile["default_model"]
        self.api_key = api_key
        self.temperature = temperature

    def test_connection(self):
        try:
            if self.backend == "ollama":
                req = urllib.request.Request(self.url.rstrip("/") + "/api/tags")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status == 200
            else:
                base = self.url.rstrip("/")
                req = urllib.request.Request(base + "/models")
                req.add_header("Authorization", "Bearer " + self.api_key)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status == 200
        except Exception:
            log.warning("_test_connection failed for %s", self.url)
            return False

    def generate_plan(self, description, context_text):
        user_msg = "Workflow description:\n" + description + "\n\n" + context_text
        return self._call_llm(SYSTEM_PROMPT_PLAN, user_msg)

    def generate_model_from_plan(self, plan_json, context_text):
        user_msg = (
            "HIGH-LEVEL PLAN:\n"
            + json.dumps(plan_json, indent=2)
            + "\n\nALGORITHM SIGNATURES AND CONTEXT:\n"
            + context_text
        )
        result = self._call_llm(SYSTEM_PROMPT_BUILD, user_msg)
        return self._validate_model_structure(result)

    def generate_single_pass(self, description, model_name, model_group, context_text):
        user_msg = (
            "Model name: "
            + model_name
            + "\nModel group: "
            + model_group
            + "\n\nWorkflow description:\n"
            + description
            + "\n\n"
            + context_text
        )
        result = self._call_llm(SYSTEM_PROMPT_BUILD, user_msg)
        return self._validate_model_structure(result)

    def repair_model(self, workflow_json, errors, context_text):
        user_msg = (
            "MODEL JSON:\n"
            + json.dumps(workflow_json, indent=2)
            + "\n\nVALIDATION ERRORS / USER FEEDBACK:\n"
            + "\n".join("- " + str(e) for e in errors)
            + "\n\nALGORITHM CATALOG:\n"
            + str(context_text)
        )
        result = self._call_llm(SYSTEM_PROMPT_REPAIR, user_msg)
        return self._validate_model_structure(result)

    def _validate_model_structure(self, result):
        if not isinstance(result, dict):
            raise ValueError(
                "Model response is not a JSON object.\n\n"
                "Try simplifying the description or switching to another model."
            )
        if "inputs" not in result or "algorithms" not in result:
            existing_keys = list(result.keys())
            raise ValueError(
                "LLM returned incomplete model definition.\n"
                f"Expected keys: 'inputs' and 'algorithms'. Got: {existing_keys}.\n\n"
                "Try: simplifying the description, using two-phase generation, "
                "or switching to a more capable model."
            )
        return result

    # ── LLM dispatch ──────────────────────────────────────────

    def _call_llm(self, system_prompt, user_message):
        if self.backend == "ollama":
            return self._call_ollama(system_prompt, user_message)
        else:
            return self._call_openai(system_prompt, user_message)

    def _call_ollama(self, system_prompt, user_message):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": self.temperature, "num_predict": 4096},
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url.rstrip("/") + "/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        content = body.get("message", {}).get("content", "")

        if not content:
            raise ValueError(
                "LLM returned an empty response (no content).\n\n"
                "Try a shorter description, lower thinking level, or another model."
            )

        return self._parse_json_response(content)

    def _call_openai(self, system_prompt, user_message):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": self.temperature,
            "max_tokens": 4096,
        }

        base = self.url.rstrip("/")
        url = base + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.api_key,
        }

        attempts = 3
        backoff = 5.0
        last_error = None

        for i in range(attempts):
            try:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=180) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"]

                if not content:
                    raise ValueError(
                        "LLM returned an empty response (no content).\n\n"
                        "Try a shorter description, lower thinking level, or another model."
                    )

                return self._parse_json_response(content)

            except urllib.error.HTTPError as e:
                last_error = e
                if e.code == 429 or 500 <= e.code <= 599:
                    retry_after = e.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else backoff
                    if i < attempts - 1:
                        time.sleep(wait)
                        backoff *= 2
                        continue
                error_body = ""
                try:
                    error_body = e.read().decode("utf-8")[:500]
                except Exception:
                    log.warning("Failed to decode error body from HTTP %s", e.code)
                    error_body = ""
                raise ValueError("HTTP Error " + str(e.code) + ": " + error_body)

            except urllib.error.URLError as e:
                last_error = e
                if i < attempts - 1:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise ValueError("Connection error: " + str(e.reason))

            except ValueError:
                raise

            except Exception as e:
                last_error = e
                if i < attempts - 1:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise ValueError("Error calling LLM API: " + str(e))

        raise ValueError("LLM API failed after " + str(attempts) + " retries: " + str(last_error))

    def _parse_json_response(self, text):
        """
        Try hard to recover a JSON object from an LLM response.
        On total failure, raise ValueError with the raw text so
        the user can still inspect and debug it.
        """
        original_text = text
        text = text.strip()

        # 0) Strip markdown/code fences
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        # 1) Fast path
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 2) Extract first {...} block with brace counting
        start = text.find("{")
        if start >= 0:
            depth = 0
            end = None
            in_string = False
            escape_next = False
            for i in range(start, len(text)):
                c = text[i]
                if escape_next:
                    escape_next = False
                    continue
                if c == "\\":
                    escape_next = True
                    continue
                if c == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break

            candidate = text[start:end] if end else text[start:]
        else:
            candidate = text

        # 3) Try candidate as-is
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        # 4) Safe repair: trailing commas
        fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass

        # 5) Progressive trimming + auto-closing
        for cut in range(len(candidate) - 1, max(0, len(candidate) - 200), -1):
            snippet = candidate[:cut]
            open_braces = snippet.count("{") - snippet.count("}")
            open_brackets = snippet.count("[") - snippet.count("]")
            closer = "]" * max(0, open_brackets) + "}" * max(0, open_braces)
            try:
                return json.loads(snippet + closer)
            except json.JSONDecodeError:
                continue

        # 6) Give up but include raw text for debugging
        snippet = original_text.strip()
        if not snippet:
            snippet = "[empty response from model]"
        elif len(snippet) > 800:
            snippet = snippet[:800] + "\n...[truncated]..."

        raise ValueError("LLM did not return valid JSON. Raw response snippet:\n\n" + snippet)
