import json
from datetime import datetime

from qgis.PyQt.QtCore import QThread, pyqtSignal

from .compiler_core.core.context_collector import ContextCollector as CompilerContextCollector
from .compiler_core.core.llm.factory import create_backend as create_compiler_backend
from .compiler_core.core.llm.base import LLMBackendError, LLMTimeoutError
from .compiler_core.core.mcp.client import DirectMCPClient
from .compiler_core.core.mcp.tool_registry import build_server
from .compiler_core.core.compiler.algorithm_resolver import AlgorithmResolver
from .compiler_core.core.compiler.expression_validator import ExpressionValidator
from .compiler_core.core.compiler.intent_parser import IntentParser
from .compiler_core.core.compiler.ir_validator import IRValidator
from .compiler_core.core.compiler.model_emitter import ModelEmitter
from .compiler_core.core.compiler.pipeline import CompilerPipeline
from .compiler_core.core.compiler.semantic_planner import SemanticPlanner
from .compiler_core.core.ir import IssueLevel
from .compiler_core.core.services.layout.graph_layout import GraphLayoutService


class ForgeGenerateWorker(QThread):
    finished = pyqtSignal(dict, list)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(
        self,
        description,
        model_name,
        model_group,
        llm_config,
        layout_profile,
        layout_orientation,
        layout_algorithm,
        selected_layer_ids,
        algo_config,
        optimize_generation,
    ):
        super().__init__()
        self.description = description
        self.model_name = model_name
        self.model_group = model_group
        self.llm_config = llm_config
        self.layout_profile = layout_profile
        self.layout_orientation = layout_orientation
        self.layout_algorithm = layout_algorithm
        self.selected_layer_ids = set(selected_layer_ids or [])
        self.algo_config = algo_config or {}
        self.optimize_generation = bool(optimize_generation)
        self._is_cancelled = False

    def request_cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            self.progress.emit("Connecting to LLM backend...")
            if self._is_cancelled:
                self.cancelled.emit()
                return

            llm = create_compiler_backend(self.llm_config)

            self.progress.emit("Collecting QGIS context...")
            if self._is_cancelled:
                self.cancelled.emit()
                return

            max_algorithms = int(self.algo_config.get("max_algorithms", 60))
            ctx = CompilerContextCollector().collect(max_algorithms=max_algorithms)
            ctx["layers"] = self._filter_layers(ctx.get("layers", []))
            ctx["algorithms"] = self._filter_algorithms(ctx.get("algorithms", {}))

            if self._is_cancelled:
                self.cancelled.emit()
                return

            server = build_server(llm)
            client = DirectMCPClient(server)
            pipeline = CompilerPipeline(
                intent_parser=IntentParser(),
                semantic_planner=SemanticPlanner(),
                algorithm_resolver=AlgorithmResolver(),
                expression_validator=ExpressionValidator(),
                ir_validator=IRValidator(),
                model_emitter=ModelEmitter(),
            )

            if self._is_cancelled:
                self.cancelled.emit()
                return

            plan, model_json = self._run_optimized_pipeline(
                pipeline=pipeline,
                client=client,
                full_context=ctx,
            )

            if self._is_cancelled:
                self.cancelled.emit()
                return

            model_json = GraphLayoutService().layout_model_json(
                model_json,
                mode=self.layout_profile,
                orientation=self.layout_orientation,
                strategy=self.layout_algorithm,
            )
            self.finished.emit(model_json, plan.issues)
        except Exception as e:
            if self._is_cancelled:
                self.cancelled.emit()
                return
            self.error.emit(self._friendly_error_text(e))

    def _filter_layers(self, layers):
        if not self.selected_layer_ids:
            return layers
        return [layer for layer in layers if layer.get("id") in self.selected_layer_ids]

    def _filter_algorithms(self, algorithms):
        if self.algo_config.get("include_all"):
            return algorithms

        enabled = set()
        if self.algo_config.get("include_native"):
            enabled.add("native")
        if self.algo_config.get("include_gdal"):
            enabled.add("gdal")
        if self.algo_config.get("include_grass"):
            enabled.add("grass")
        if self.algo_config.get("include_saga"):
            enabled.add("saga")

        if not enabled:
            return {}

        return {
            alg_id: alg
            for alg_id, alg in algorithms.items()
            if alg_id.split(":", 1)[0] in enabled
        }

    def _run_optimized_pipeline(self, pipeline, client, full_context):
        max_attempts = 3 if self.optimize_generation else 1
        last_error = None
        base_text = " ".join((self.description or "").split())

        for attempt in range(max_attempts):
            if self._is_cancelled:
                self.cancelled.emit()
                return None, {}

            retry_index = attempt + 1
            attempt_context = self._build_attempt_context(full_context, attempt)
            attempt_text = self._build_attempt_prompt(base_text, last_error, attempt)

            if attempt > 0:
                self.progress.emit(f"Optimizing prompt and retrying ({retry_index}/{max_attempts})...")

            if self._is_cancelled:
                self.cancelled.emit()
                return None, {}

            try:
                plan, model_json = pipeline.run(
                    raw_text=attempt_text,
                    model_name=self.model_name,
                    model_group=self.model_group,
                    qgis_context=attempt_context,
                    mcp_client=client,
                    progress_callback=lambda msg: self.progress.emit(msg),
                )
            except Exception as e:
                if self._is_cancelled:
                    self.cancelled.emit()
                    return None, {}
                last_error = e
                if retry_index < max_attempts:
                    continue
                raise

            if self._is_cancelled:
                self.cancelled.emit()
                return None, {}

            error_issues = [i for i in plan.issues if i.level == IssueLevel.ERROR]
            if error_issues and retry_index < max_attempts and self.optimize_generation:
                last_error = RuntimeError(
                    "; ".join(issue.message for issue in error_issues[:2])
                )
                continue

            return plan, model_json

        if last_error:
            raise last_error
        raise RuntimeError("Generation failed before a model could be produced.")

    def _build_attempt_context(self, full_context, attempt):
        if attempt <= 0:
            return full_context

        layers = list(full_context.get("layers", []))
        slim_layers = []
        for layer in layers[:8]:
            item = dict(layer)
            fields = list(item.get("fields", []))
            if fields:
                item["fields"] = fields[:8]
            slim_layers.append(item)

        algorithms = full_context.get("algorithms", {})
        algo_items = list(algorithms.items())
        if attempt == 1:
            limit = min(len(algo_items), 40)
        else:
            limit = min(len(algo_items), 20)
        slim_algorithms = dict(algo_items[:limit])

        return {
            "layers": slim_layers,
            "algorithms": slim_algorithms,
            "project_crs": full_context.get("project_crs"),
            "canvas_extent": full_context.get("canvas_extent"),
        }

    def _build_attempt_prompt(self, base_text, last_error, attempt):
        if attempt <= 0 or not last_error:
            return base_text
        return (
            f"{base_text}\n\n"
            f"Retry notes: {last_error}. "
            "Use only algorithms present in the catalog. "
            "Return a compact, valid plan."
        )

    def _friendly_error_text(self, error):
        if isinstance(error, LLMTimeoutError):
            return (
                "The LLM request timed out after optimized retries. "
                "Try a smaller model context or increase backend timeout."
            )
        if isinstance(error, LLMBackendError):
            return str(error)
        message = str(error).strip()
        if not message:
            return "Generation failed due to an unexpected error."
        return message
