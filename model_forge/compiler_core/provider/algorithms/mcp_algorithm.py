"""
ModelForgeMCPAlgorithm
======================
Processing algorithm that runs the full compiler pipeline (NLP → model JSON).
Exposes the workflow to QGIS Batch Runner, scripts, and console.
"""
from __future__ import annotations

try:
    from qgis.core import (
        QgsProcessingAlgorithm,
        QgsProcessingParameterString,
        QgsProcessingParameterBoolean,
        QgsProcessingParameterEnum,
        QgsProcessingOutputString,
        QgsProcessingException,
    )
    from qgis.PyQt.QtCore import QCoreApplication
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False

if _HAS_QGIS:
    import json

    class ModelForgeMCPAlgorithm(QgsProcessingAlgorithm):

        def tr(self, message: str) -> str:
            return QCoreApplication.translate("ModelForgeMCPAlgorithm", message)

        INPUT_DESCRIPTION = "INPUT_DESCRIPTION"
        MODEL_NAME        = "MODEL_NAME"
        MODEL_GROUP       = "MODEL_GROUP"
        LLM_PROVIDER      = "LLM_PROVIDER"
        LLM_MODEL         = "LLM_MODEL"
        OUTPUT_JSON       = "OUTPUT_JSON"
        AUTO_LAYOUT       = "AUTO_LAYOUT"
        LAYOUT_MODE       = "LAYOUT_MODE"

        def name(self)        -> str: return "mcp_build_workflow"
        def displayName(self) -> str: return self.tr("Build Workflow from Description (MCP)")
        def group(self)       -> str: return self.tr("ModelForge")
        def groupId(self)     -> str: return "model_forge"

        def shortHelpString(self) -> str:
            return self.tr(
                "Converts a natural-language geoprocessing description to a "
                "QGIS model JSON using the ModelForge MCP compiler pipeline. "
                "Requires a running LLM backend (Ollama or OpenAI)."
            )

        def createInstance(self): return ModelForgeMCPAlgorithm()

        def initAlgorithm(self, config=None):
            self.addParameter(QgsProcessingParameterString(
                self.INPUT_DESCRIPTION, self.tr("Workflow description"), multiLine=True))
            self.addParameter(QgsProcessingParameterString(
                self.MODEL_NAME,  self.tr("Model name"),  defaultValue="my_workflow"))
            self.addParameter(QgsProcessingParameterString(
                self.MODEL_GROUP, self.tr("Model group"), defaultValue="ModelForge"))
            self.addParameter(QgsProcessingParameterEnum(
                self.LLM_PROVIDER, self.tr("LLM provider"),
                options=["Ollama (local)", "OpenAI"],
                defaultValue=0,
            ))
            self.addParameter(QgsProcessingParameterString(
                self.LLM_MODEL, self.tr("LLM model name"), defaultValue="llama3"))
            self.addParameter(QgsProcessingParameterBoolean(
                self.AUTO_LAYOUT, self.tr("Apply auto-layout"), defaultValue=True))
            self.addParameter(QgsProcessingParameterEnum(
                self.LAYOUT_MODE, self.tr("Layout mode"),
                options=["compact", "balanced", "dense", "spacious", "debug"],
                defaultValue=1,
            ))
            self.addOutput(QgsProcessingOutputString(
                self.OUTPUT_JSON, self.tr("Generated model JSON")))

        def processAlgorithm(self, parameters, context, feedback):
            description  = self.parameterAsString(parameters, self.INPUT_DESCRIPTION, context)
            model_name   = self.parameterAsString(parameters, self.MODEL_NAME,        context)
            model_group  = self.parameterAsString(parameters, self.MODEL_GROUP,       context)
            provider_idx = self.parameterAsEnum  (parameters, self.LLM_PROVIDER,      context)
            llm_model    = self.parameterAsString(parameters, self.LLM_MODEL,         context)
            auto_layout  = self.parameterAsBool  (parameters, self.AUTO_LAYOUT,       context)
            layout_idx   = self.parameterAsEnum  (parameters, self.LAYOUT_MODE,       context)

            layout_modes = ["compact", "balanced", "dense", "spacious", "debug"]
            layout_mode  = layout_modes[layout_idx]
            providers    = ["ollama", "openai"]
            provider     = providers[provider_idx]

            feedback.pushInfo(f"Starting ModelForge MCP pipeline (provider={provider}, model={llm_model})")

            # ── Build LLM backend ──────────────────────────────────────────
            from ...core.llm.factory import create_backend
            try:
                llm = create_backend({
                    "provider": provider,
                    "model":    llm_model,
                })
            except Exception as e:
                raise QgsProcessingException(f"Failed to create LLM backend: {e}") from e

            # ── Collect QGIS context ───────────────────────────────────────
            from ...core.context_collector import ContextCollector
            qgis_context = ContextCollector().collect()

            # ── Build MCP infrastructure ───────────────────────────────────
            from ...core.mcp.tool_registry import build_server
            from ...core.mcp.client import DirectMCPClient
            server = build_server(llm)
            client = DirectMCPClient(server)

            # ── Run compiler pipeline ──────────────────────────────────────
            from ...core.compiler.pipeline import CompilerPipeline
            from ...core.compiler.intent_parser import IntentParser
            from ...core.compiler.semantic_planner import SemanticPlanner
            from ...core.compiler.algorithm_resolver import AlgorithmResolver
            from ...core.compiler.expression_validator import ExpressionValidator
            from ...core.compiler.ir_validator import IRValidator
            from ...core.compiler.model_emitter import ModelEmitter

            from ...core.services.registry.registry_catalog import RegistryCatalogService
            catalog_svc = RegistryCatalogService()

            pipeline = CompilerPipeline(
                intent_parser=IntentParser(),
                semantic_planner=SemanticPlanner(),
                algorithm_resolver=AlgorithmResolver(registry_catalog=catalog_svc),
                expression_validator=ExpressionValidator(),
                ir_validator=IRValidator(),
                model_emitter=ModelEmitter(),
                registry_catalog=catalog_svc,
            )

            try:
                plan, model_json = pipeline.run(
                    raw_text=description,
                    model_name=model_name,
                    model_group=model_group,
                    qgis_context=qgis_context,
                    mcp_client=client,
                    progress_callback=lambda msg: feedback.pushInfo(msg),
                )
            except Exception as e:
                raise QgsProcessingException(f"Compiler pipeline failed: {e}") from e

            # ── Report issues ──────────────────────────────────────────────
            from ...core.ir import IssueLevel
            for issue in plan.issues:
                if issue.level == IssueLevel.ERROR:
                    feedback.reportError(f"[{issue.code}] {issue.message}", fatalError=False)
                elif issue.level == IssueLevel.WARNING:
                    feedback.pushWarning(f"[{issue.code}] {issue.message}")
                else:
                    feedback.pushInfo(f"[{issue.code}] {issue.message}")

            # ── Optional auto-layout ───────────────────────────────────────
            if auto_layout:
                from ...core.services.graph_layout import GraphLayoutService
                layout_svc = GraphLayoutService()
                model_json = layout_svc.layout_model_json(model_json, mode=layout_mode)
                feedback.pushInfo(f"Applied {layout_mode} layout.")

            result_str = json.dumps(model_json, indent=2, ensure_ascii=False)
            feedback.pushInfo(f"Compiler pipeline complete. Steps: {len(plan.steps)}, "
                              f"Issues: {len(plan.issues)}")

            return {self.OUTPUT_JSON: result_str}

else:
    class ModelForgeMCPAlgorithm:
        pass
