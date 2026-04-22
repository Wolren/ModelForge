"""
ModelForge Intermediate Representation (IR)
============================================
Single source of truth for the typed data structures that flow through
the six-stage compiler pipeline.

Stages:
  1. IntentParser       -> RawIntent
  2. SemanticPlanner    -> SemanticPlan
  3. AlgorithmResolver  -> ExecutablePlan  (each step gains a ResolvedAlgorithm)
  4. ExpressionValidator-> mutates ExecutablePlan (renders expressions)
  5. IRValidator        -> annotates issues list
  6. ModelEmitter       -> emits model_json dict
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ─── Enumerations ────────────────────────────────────────────────────────────

class ParamKind(str, Enum):
    VECTOR_LAYER   = "vectorlayer"
    RASTER_LAYER   = "rasterlayer"
    FIELD          = "field"
    EXPRESSION     = "expression"
    NUMBER         = "number"
    BOOLEAN        = "boolean"
    STRING         = "string"
    ENUM           = "enum"
    CRS            = "crs"
    EXTENT         = "extent"
    FILE           = "file"
    FOLDER         = "folder"
    SINK           = "sink"
    RASTER_DEST    = "rasterdestination"
    FEATURE_SINK   = "featuresink"
    UNKNOWN        = "unknown"


class OutputKind(str, Enum):
    VECTOR    = "vector"
    RASTER    = "raster"
    FILE      = "file"
    NUMBER    = "number"
    STRING    = "string"
    BOOLEAN   = "boolean"
    LAYER     = "layer"
    UNKNOWN   = "unknown"


class StepStatus(str, Enum):
    RESOLVED = "resolved"   # algorithm_id confirmed in registry
    ASSUMED  = "assumed"    # algorithm_id guessed; needs human confirmation
    BLOCKED  = "blocked"    # cannot resolve; errors recorded


class IssueLevel(str, Enum):
    ERROR   = "error"
    WARNING = "warning"
    INFO    = "info"


# ─── Leaf structures ──────────────────────────────────────────────────────────

@dataclass
class ParameterSpec:
    name: str
    kind: ParamKind
    description: str = ""
    optional: bool = False
    enum_options: List[str] = field(default_factory=list)
    default_value: Any = None


@dataclass
class OutputSpec:
    name: str
    kind: OutputKind
    description: str = ""


@dataclass
class PlanIssue:
    level: IssueLevel
    code: str               # e.g. "UNRESOLVED_ALGORITHM"
    message: str
    step_id: Optional[str] = None
    param_name: Optional[str] = None


@dataclass
class ExpressionNode:
    """A predicate node tree produced by build_expression MCP tool."""
    node_type: str          # "comparison", "logical", "function", "literal", "field_ref"
    operator: Optional[str] = None
    left: Optional["ExpressionNode"] = None
    right: Optional["ExpressionNode"] = None
    value: Any = None
    field_name: Optional[str] = None
    function_name: Optional[str] = None
    arguments: List["ExpressionNode"] = field(default_factory=list)
    rendered: Optional[str] = None    # QGIS expression string, filled by ExpressionValidator


@dataclass
class ResolvedAlgorithm:
    algorithm_id: str               # e.g. "native:buffer"
    display_name: str
    provider_id: str                # e.g. "native"
    parameters: List[ParameterSpec] = field(default_factory=list)
    outputs: List[OutputSpec] = field(default_factory=list)
    doc_url: Optional[str] = None


@dataclass
class ParameterBinding:
    """How a step parameter gets its value at model-build time."""
    source_type: str   # "model_input" | "child_output" | "static" | "expression" | "enum_index"
    model_input: Optional[str] = None      # name of ModelInput
    child_id: Optional[str] = None         # step id of producing child
    output_name: Optional[str] = None      # output port name on child
    static_value: Any = None
    expression: Optional[ExpressionNode] = None
    enum_index: Optional[int] = None


# ─── Step and Plan ────────────────────────────────────────────────────────────

@dataclass
class ExecutableStep:
    step_id: str
    label: str
    algorithm: Optional[ResolvedAlgorithm] = None
    status: StepStatus = StepStatus.ASSUMED
    confidence: float = 0.0
    parameters: Dict[str, ParameterBinding] = field(default_factory=dict)
    output_names: List[str] = field(default_factory=list)
    # Layout hint (assigned by GraphLayoutService)
    pos_x: float = 0.0
    pos_y: float = 0.0
    rank: int = 0   # topological layer


@dataclass
class ModelInput:
    name: str
    kind: ParamKind
    label: str
    description: str = ""
    optional: bool = False
    default_value: Any = None
    # Layout hint
    pos_x: float = 0.0
    pos_y: float = 0.0


@dataclass
class ExecutablePlan:
    inputs: List[ModelInput] = field(default_factory=list)
    steps: List[ExecutableStep] = field(default_factory=list)
    issues: List[PlanIssue] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not any(i.level == IssueLevel.ERROR for i in self.issues)

    def assumed_steps(self) -> List[ExecutableStep]:
        return [s for s in self.steps if s.status == StepStatus.ASSUMED]

    def blocked_steps(self) -> List[ExecutableStep]:
        return [s for s in self.steps if s.status == StepStatus.BLOCKED]

    def step_by_id(self, step_id: str) -> Optional[ExecutableStep]:
        return next((s for s in self.steps if s.step_id == step_id), None)
