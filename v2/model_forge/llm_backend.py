import json
import time
import urllib.request
import urllib.error


SYSTEM_PROMPT_PLAN = """You are a QGIS Processing workflow planner. Given a natural language description of a geoprocessing task AND a catalog of available QGIS algorithms with their exact parameters, produce a HIGH-LEVEL PLAN.

RULES:
1. Output ONLY valid JSON. No markdown, no commentary, no code fences.
2. The plan lists steps in order. Each step has:
   - "step": a short human label
   - "algorithm_id": the exact Processing algorithm ID from the catalog
   - "reasoning": why this step is needed
   - "inputs_from": list of step indices (0-based) or "model_input" that feed into this step
3. Also list all required model-level inputs with name, type, and which steps use them.
4. Keep plans between 2 and 15 steps.

RESPOND WITH:
{
  "plan_steps": [...],
  "model_inputs": [...]
}"""


SYSTEM_PROMPT_BUILD = """You are a QGIS Processing Model code generator. Given a HIGH-LEVEL PLAN and the EXACT parameter signatures of each algorithm from the user's QGIS installation, produce a structured JSON model definition.

RULES:
1. Output ONLY valid JSON. No markdown, no commentary, no code fences.
2. Use ONLY algorithm IDs from the plan. Do NOT invent new ones.
3. Use ONLY parameter names that appear in the provided algorithm signatures.
4. Every algorithm step MUST have: "id" (unique string), "algorithm_id" (Processing ID), "description" (human label), "parameters" (dict mapping param names to sources).
5. Parameter sources are objects with a "type" field:
   - {"type": "model_input", "name": "<input_param_name>"} — refers to a model-level input
   - {"type": "child_output", "child_id": "<step_id>", "output_name": "<output_key>"} — output from previous step
   - {"type": "static", "value": <any>} — a hardcoded value
   - {"type": "expression", "expression": "<qgis_expression>"} — a QGIS expression
6. Define model-level inputs in "inputs". Each: "name", "label", "type" (vector/raster/number/string/field/crs/boolean/multilayer), optionally "default", "parent", "geometry" (0=point, 1=line, 2=polygon, -1=any).
7. For destination/output parameters: use {"type": "static", "value": "TEMPORARY_OUTPUT"} or omit them.
8. Define steps in "algorithms" in execution order.

RESPOND WITH:
{
  "inputs": [...],
  "algorithms": [...]
}"""


SYSTEM_PROMPT_REPAIR = """You are a QGIS Processing Model debugger. Given a model JSON definition and a list of validation errors, fix the JSON so it passes validation.

RULES:
1. Output ONLY the corrected JSON. No markdown, no commentary.
2. Fix ONLY what the errors describe. Do not restructure or add unnecessary steps.
3. Use only algorithm IDs and parameter names from the provided catalog.
4. Common fixes: add missing required parameters, fix parameter type mismatches, correct child_id references.

RESPOND WITH the corrected full JSON:
{
  "inputs": [...],
  "algorithms": [...]
}"""


class LLMBackend:
    """Handles communication with Ollama or OpenAI-compatible LLMs.
    Supports two-phase generation (plan then build) and repair prompts."""

    def __init__(self):
        self.backend = "ollama"
        self.url = "http://localhost:11434"
        self.api_key = ""
        self.model = "qwen2.5-coder:7b"
        self.temperature = 0.2

    def configure(self, backend="ollama", url="", api_key="", model="", temperature=0.2):
        self.backend = backend
        self.url = url or ("http://localhost:11434" if backend == "ollama" else "https://api.openai.com/v1")
        self.api_key = api_key
        self.model = model or ("qwen2.5-coder:7b" if backend == "ollama" else "gpt-4o-mini")
        self.temperature = temperature

    def test_connection(self):
        try:
            if self.backend == "ollama":
                req = urllib.request.Request(f"{self.url}/api/tags")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status == 200
            else:
                base = self.url.rstrip('/')
                req = urllib.request.Request(f"{base}/models")
                req.add_header("Authorization", f"Bearer {self.api_key}")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status == 200
        except:
            return False

    # ---- Two-phase generation ----

    def generate_plan(self, description, context_text):
        user_msg = f"Workflow description:\n{description}\n\n{context_text}"
        return self._call_llm(SYSTEM_PROMPT_PLAN, user_msg)

    def generate_model_from_plan(self, plan_json, context_text):
        user_msg = (
            f"HIGH-LEVEL PLAN:\n{json.dumps(plan_json, indent=2)}"
            f"\n\nALGORITHM SIGNATURES AND CONTEXT:\n{context_text}"
        )
        return self._call_llm(SYSTEM_PROMPT_BUILD, user_msg)

    def generate_single_pass(self, description, model_name, model_group, context_text):
        combined_prompt = SYSTEM_PROMPT_BUILD
        user_msg = (
            f"Model name: {model_name}\nModel group: {model_group}\n\n"
            f"Workflow description:\n{description}\n\n{context_text}"
        )
        return self._call_llm(combined_prompt, user_msg)

    def repair_model(self, workflow_json, errors, context_text):
        user_msg = (
            f"MODEL JSON:\n{json.dumps(workflow_json, indent=2)}"
            f"\n\nVALIDATION ERRORS:\n"
            + "\n".join(f"- {e}" for e in errors)
            + f"\n\nALGORITHM CATALOG:\n{context_text}"
        )
        return self._call_llm(SYSTEM_PROMPT_REPAIR, user_msg)

    # ---- Core LLM call with retry ----

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
            f"{self.url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body.get("message", {}).get("content", "")
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

        base = self.url.rstrip('/')
        url = f"{base}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
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
                except:
                    pass
                raise ValueError(f"HTTP Error {e.code}: {error_body}")

            except Exception as e:
                last_error = e
                if i < attempts - 1:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise ValueError(f"Error calling LLM API: {str(e)}")

        raise ValueError(f"LLM API failed after {attempts} retries: {last_error}")

    def _parse_json_response(self, text):
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(text[start:end])
            raise ValueError(f"LLM did not return valid JSON:\n{text[:500]}")
