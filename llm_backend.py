import json
import urllib.request
import urllib.error


SYSTEM_PROMPT = """You are a QGIS Processing Model architect. Given a natural language description of a geoprocessing workflow, you produce a structured JSON definition that can be assembled into a QGIS .model3 file.

RULES:
1. Output ONLY valid JSON. No markdown, no commentary, no code fences.
2. Use real QGIS Processing algorithm IDs (e.g. "native:clip", "native:dissolve", "native:buffer", "native:joinattributesbylocation", "native:fieldcalculator", "qgis:zonalstatisticsfb", "native:reprojectlayer", "native:mergevectorlayers", "native:difference", "native:intersection", "native:centroids", "native:convexhull", "native:minimumboundinggeometry", "gdal:slope", "gdal:hillshade", "gdal:viewshed", "native:rasterlayerstatistics", "native:reclassifybytable", "gdal:rasterize", "native:savefeatures", "native:extractbyattribute", "native:selectbyexpression", "qgis:basicstatisticsforfields").
3. Every algorithm step MUST have: "id" (unique string), "algorithm_id" (Processing ID), "description" (human label), "parameters" (dict mapping param names to sources), and optionally "outputs" (dict of named outputs to expose as model outputs).
4. Parameter sources are objects with a "type" field:
   - {"type": "model_input", "name": "<input_param_name>"} — refers to a model-level input
   - {"type": "child_output", "child_id": "<step_id>", "output_name": "<output_key>"} — refers to a previous step's output
   - {"type": "static", "value": <any>} — a hardcoded value
   - {"type": "expression", "expression": "<qgis_expression>"} — a QGIS expression
5. Define all model-level inputs in "inputs". Each input has: "name" (internal key), "label" (display name), "type" (one of: "vector", "raster", "number", "string", "field", "enum", "crs", "extent", "boolean", "multilayer", "band"), and optionally "default", "parent" (for field type, the parent layer input name), "geometry" (0=point, 1=line, 2=polygon, -1=any).
6. Define all processing steps in "algorithms" in execution order.
7. Mark final outputs by adding "outputs" dict to the last relevant algorithm steps. Each output key maps to {"label": "Human Name"}.
8. Keep models between 2 and 15 steps. Prefer simplicity.

RESPOND WITH A SINGLE JSON OBJECT matching this schema:
{
  "inputs": [...],
  "algorithms": [...]
}"""


class LLMBackend:
    """Handles communication with local (Ollama) or remote (OpenAI-compatible) LLMs"""

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
                req = urllib.request.Request(f"{self.url}/models")
                req.add_header("Authorization", f"Bearer {self.api_key}")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status == 200
        except:
            return False

    def generate_workflow(self, description, model_name, model_group, layer_context=""):
        user_message = f"Model name: {model_name}\nModel group: {model_group}\n\n"
        user_message += f"Workflow description:\n{description}"

        if layer_context:
            user_message += f"\n\nAvailable layers context:\n{layer_context}"

        if self.backend == "ollama":
            return self._call_ollama(user_message)
        else:
            return self._call_openai(user_message)

    def _call_ollama(self, user_message):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": self.temperature,
                "num_predict": 4096,
            },
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        content = body.get("message", {}).get("content", "")
        return self._parse_json_response(content)

    def _call_openai(self, user_message):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "temperature": self.temperature,
            "max_tokens": 4096,
        }

        if "json" in self.model or True:
            payload["response_format"] = {"type": "json_object"}

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        content = body["choices"][0]["message"]["content"]
        return self._parse_json_response(content)

    def _parse_json_response(self, text):
        text = text.strip()

        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(text[start:end])
            else:
                raise ValueError(f"LLM did not return valid JSON:\n{text[:500]}")

        if "inputs" not in result or "algorithms" not in result:
            raise ValueError(
                "LLM response missing required keys. "
                f"Got keys: {list(result.keys())}"
            )

        return result
