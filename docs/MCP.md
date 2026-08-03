# Model Forge MCP Server Reference

Model Forge ships an MCP (Model Context Protocol) server built on the MCP SDK 2.x. It exposes the model-generation pipeline to any MCP client. This reference covers the full surface: CLI, transports, tools, prompts, resources, error codes, headless mode, and security.

## Quick start

```bash
pip install model-forge      # mcp SDK 2.x is a base dependency
pip install uvicorn          # only needed for SSE transport
```

### Claude Desktop

Edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "model-forge": {
      "command": "python",
      "args": ["-m", "model_forge.mcp_server", "--transport", "stdio"]
    }
  }
}
```

### SSE transport

```bash
python -m model_forge.mcp_server --transport sse --host 127.0.0.1 --port 9090
# MCP endpoint: http://127.0.0.1:9090/sse
```

## CLI reference

```bash
python -m model_forge.mcp_server [options]
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--transport` | `stdio` | `stdio` or `sse` |
| `--host` | `127.0.0.1` | SSE bind host |
| `--port` | `9090` | SSE bind port |
| `--llm-provider` | auto-detect | `ollama`, `openai`, `openai_compat`, `azure_openai`, `anthropic`, `gemini` |
| `--llm-model` | auto-detect | Model name (Azure: deployment name) |
| `--llm-base-url` | provider default | Base URL (Azure: resource endpoint) |
| `--llm-api-key` | `MODELFORGE_API_KEY` | API key |
| `--llm-temperature` | `0.2` | Sampling temperature |
| `--llm-timeout` | provider default | Request timeout in seconds |
| `--llm-default-headers` | - | JSON object of extra HTTP headers |
| `--llm-extra-body` | - | JSON object merged into the request body |
| `--auth-token` | `MODELFORGE_MCP_TOKEN` | Bearer token for SSE auth |
| `--tls-cert` | `MODELFORGE_MCP_TLS_CERT` | PEM certificate for HTTPS |
| `--tls-key` | `MODELFORGE_MCP_TLS_KEY` | PEM private key for HTTPS |
| `--shutdown-timeout` | `15.0` | Seconds to drain in-flight SSE on stop |
| `-v` / `--verbose` | - | Debug logging |

## Configuring the LLM

Before calling `generate_model`, the server needs an LLM backend. Configure it via the `set_llm_config` tool, or pass CLI flags / env vars at startup:

```bash
# Ollama (default)
python -m model_forge.mcp_server --llm-provider ollama --llm-model qwen2.5-coder:7b

# OpenAI
python -m model_forge.mcp_server --llm-provider openai --llm-api-key sk-... --llm-model gpt-4o-mini

# Any OpenAI-compatible endpoint (LM Studio, vLLM, OpenRouter, ...)
python -m model_forge.mcp_server --llm-provider openai_compat \
  --llm-base-url http://localhost:1234 \
  --llm-model local-model \
  --llm-api-key not-needed

# Anthropic
python -m model_forge.mcp_server --llm-provider anthropic --llm-api-key sk-ant-... --llm-model claude-3-5-sonnet-latest

# Azure OpenAI (model = deployment name; base_url = resource endpoint)
python -m model_forge.mcp_server --llm-provider azure_openai \
  --llm-model my-gpt4-deployment \
  --llm-base-url https://<resource>.openai.azure.com \
  --llm-api-key <azure-key>

# Google Gemini
python -m model_forge.mcp_server --llm-provider gemini --llm-api-key AIza... --llm-model gemini-1.5-pro

# Persist so subsequent boots reuse it
python -m model_forge.mcp_server --llm-provider ollama --llm-model qwen2.5-coder:7b
# the server writes to $MODELFORGE_MCP_CONFIG (default ~/.config/model-forge/mcp.json)
```

### Env vars (lowest priority)

`MODELFORGE_PROVIDER`, `MODELFORGE_MODEL`, `MODELFORGE_BASE_URL`, `MODELFORGE_API_KEY`, `MODELFORGE_TEMPERATURE`, `MODELFORGE_TIMEOUT`, `MODELFORGE_DEFAULT_HEADERS` (JSON object), `MODELFORGE_EXTRA_BODY` (JSON object), `MODELFORGE_REQUIRE_KEY` (`0` skips the API key check for self-hosted deployments), `MODELFORGE_MCP_CONFIG` (config path), `MODELFORGE_MCP_TOKEN`, `MODELFORGE_MCP_TLS_CERT`, `MODELFORGE_MCP_TLS_KEY`, `MODELFORGE_MCP_CATALOG_LIMIT`.

## Tool catalog (23 tools)

| Category | Tool | Purpose |
| --- | --- | --- |
| Context | `list_layers`, `get_layer_info`, `get_project_info`, `refresh_qgis_context`, `configure_headless_context` | QGIS project state, plus headless context override |
| Algorithms | `list_algorithms`, `get_algorithm_info`, `list_providers`, `list_algorithm_groups`, `load_catalog_from_file`, `export_catalog` | Algorithm discovery + headless catalog I/O |
| Generation | `generate_model`, `validate_model`, `export_model`, `summarize_model` | The main pipeline + validation + export |
| Management | `set_llm_config`, `get_server_info`, `ping`, `cancel_generation`, `get_generation_status`, `subscription_status`, `subscribe_resource`, `unsubscribe_resource` | Server control, job lifecycle, resource subscription |

### Export formats (8)

`json` (pretty-printed), `mermaid` (flowchart diagram), `script` (standalone Python Processing script), `model3` (QGIS model XML, requires QGIS), `geojson` (contract preview), `gpkg` (GeoPackage), `runnable_script` (argparse-wrapped script), `processing_runnable_json` (QGIS Processing runnable recipe).

## Prompts (3)

`generate_model_from_intent`, `explain_model`, `convert_script_to_model`. Clients expose these as named, fillable templates that prime a chat for a specific workflow.

## Resources (3)

| URI | Content |
| --- | --- |
| `model-forge://server-info` | Version, schema version, supported providers, tool list, prompt list, subscription capabilities |
| `model-forge://context/layers` | Current project layers (or headless snapshot) |
| `model-forge://algorithms` | Algorithm index: `[{id, name, group}]` |

Resources can be subscribed to; the server pushes dirty-set updates that the client polls via `subscription_status(consume=True)`.

## Streaming progress + cancellation

`generate_model` accepts `progress_token: str` and `timeout_seconds: float`. When a token is supplied, the server forwards structured `(current, total, message)` notifications via the MCP `notifications/progress` channel. Long-running jobs get a `job_id` back in the response; pair it with `cancel_generation(job_id)` for mid-flight cancellation, or `get_generation_status(job_id)` to inspect progress.

## Error model

Every tool returns either a JSON payload (the model) or a structured error:

```json
{
  "code": "E_LLM_NOT_CONFIGURED",
  "message": "LLM not configured. Set provider/model via set_llm_config tool.",
  "details": { "provider": "" }
}
```

Stable codes include `E_LLM_NOT_CONFIGURED`, `E_INVALID_JSON`, `E_ALG_NOT_FOUND`, `E_LAYER_NOT_FOUND`, `E_VALIDATION_FAILED`, `E_PIPELINE_FAILED`, `E_QGIS_NOT_AVAILABLE`, `E_LLM_PROVIDER`, `E_CANCELLED`, `E_TIMEOUT`, `E_UNKNOWN_FORMAT`, `E_CONFIG`, `E_INTERNAL`. The schema version is reported in `model-forge://server-info`.

## Pure-Python / headless mode

The server boots cleanly without QGIS installed. In that mode, `qgis_available` is `false` and the context tools return empty results. To work headlessly:

1. Call `export_catalog(path)` from a QGIS-equipped dev machine to dump the live catalog.
2. On the headless box, call `load_catalog_from_file(path)`.
3. Call `configure_headless_context(layers_json=...)` with a layer snapshot to give `list_layers` something to report.

Algorithm discovery (`list_algorithms`, `get_algorithm_info`, `list_providers`, `list_algorithm_groups`) works against the loaded catalog. `generate_model` runs end-to-end - the compiler pipeline tolerates the missing QGIS registry via try/except fallbacks.

## Security

The SSE transport binds to `127.0.0.1` by default (loopback only). For non-localhost deployments:

```bash
python -m model_forge.mcp_server --transport sse --host 0.0.0.0 --port 9090 \
  --auth-token "$MODELFORGE_TOKEN" \
  --tls-cert /etc/ssl/model-forge.crt \
  --tls-key /etc/ssl/model-forge.key
```

The `--auth-token` (or `MODELFORGE_MCP_TOKEN` env var) requires `Authorization: Bearer <token>` (or `X-Model-Forge-Token: <token>`) on every request; `/healthz` is exempt. TLS via `--tls-cert` / `--tls-key` serves HTTPS on the same SSE endpoint. The shutdown timeout (`--shutdown-timeout`, default 15s) controls how long `stop_server` waits for in-flight SSE messages to drain.

## Embedding in QGIS

The QGIS plugin (loaded via Plugin Manager) starts and stops the same MCP server in a background thread. Tool users can attach an external MCP client to the same in-process server via `--transport sse` from a separate `model_forge.mcp_server` invocation, or rely on the in-process stdio connection the plugin uses internally.

## Model JSON schema

```json
{
  "inputs": [{ "id": "input_points", "type": "vector", "geometry": "point" }],
  "algorithms": [
    {
      "id": "buffer_step",
      "algorithm_id": "native:buffer",
      "parameters": {
        "INPUT": { "type": "child_output", "child_id": "input_points" },
        "DISTANCE": 500
      }
    }
  ]
}
```

Child outputs use `{ "type": "child_output", "child_id": "step_id" }`.
