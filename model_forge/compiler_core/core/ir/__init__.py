"""
Intermediate Representation (IR) for the ModelForge compiler pipeline.
All compiler stages operate on these dataclasses.

Single source of truth — do not duplicate in this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ParamKind(str, Enum):
    VECTOR_LAYER = "vectorlayer"
    RASTER_LAYER = "rasterlayer"
    FIELD = "field"
    EXPRESSION = "expression"
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING = "string"
    ENUM = "enum"
    CRS = "crs"
    EXTENT = "extent"
    FILE = "file"
    FOLDER = "folder"
    SINK = "sink"
    FEATURE_SINK = "featuresink"
    RASTER_DEST = "rasterdestination"
    UNKNOWN = "unknown"


class OutputKind(str, Enum):
    VECTOR = "vector"
    RASTER = "raster"
    FILE = "file"
    NUMBER = "number"
    STRING = "string"
    BOOLEAN = "boolean"
    LAYER = "layer"
    UNKNOWN = "unknown"


class StepStatus(str, Enum):
    ASSUMED = "assumed"
    RESOLVED = "resolved"
    BLOCKED = "blocked"
    CUSTOM = "custom"


class IssueLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class ParameterSpec:
    name: str
    kind: ParamKind
    description: str = ""
    optional: bool = False
    enum_options: list[str] = field(default_factory=list)
    default_value: Any = None


@dataclass
class OutputSpec:
    name: str
    kind: OutputKind
    description: str = ""


@dataclass
class ModelInput:
    name: str
    kind: ParamKind
    label: str = ""
    description: str = ""
    optional: bool = False
    default_value: Any = None
    pos_x: float = 20.0
    pos_y: float = 20.0


@dataclass
class ResolvedAlgorithm:
    algorithm_id: str
    display_name: str = ""
    provider_id: str = ""
    parameters: list[ParameterSpec] = field(default_factory=list)
    outputs: list[OutputSpec] = field(default_factory=list)
    doc_url: str | None = None


@dataclass
class ExpressionNode:
    node_type: str  # comparison|logical|function|literal|field_ref
    rendered: str | None = None
    value: Any = None
    field_name: str | None = None
    function_name: str | None = None
    arguments: list = field(default_factory=list)
    operator: str | None = None
    left: ExpressionNode | None = None
    right: ExpressionNode | None = None


@dataclass
class ParameterBinding:
    source_type: str  # model_input|child_output|static|expression|enum_index
    model_input: str | None = None
    child_id: str | None = None
    output_name: str | None = None
    static_value: Any = None
    enum_index: int | None = None
    expression: ExpressionNode | None = None


@dataclass
class ExecutableStep:
    step_id: str
    label: str
    status: StepStatus = StepStatus.ASSUMED
    confidence: float = 0.0
    algorithm: ResolvedAlgorithm | None = None
    parameters: dict[str, ParameterBinding] = field(default_factory=dict)
    output_names: list[str] = field(default_factory=list)
    pos_x: float = 0.0
    pos_y: float = 0.0
    rank: int = 0
    # Free-form hints from the planner: ``planner_algorithm_id``,
    # ``constraints`` (the raw constraints dict from plan_workflow),
    # ``needs_review``. The resolver uses these to short-circuit
    # re-derivation of algorithm IDs and parameter values.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanIssue:
    level: IssueLevel
    code: str
    message: str
    step_id: str | None = None
    param_name: str | None = None


@dataclass
class ExecutablePlan:
    inputs: list[ModelInput] = field(default_factory=list)
    steps: list[ExecutableStep] = field(default_factory=list)
    issues: list[PlanIssue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not any(i.level == IssueLevel.ERROR for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.level == IssueLevel.WARNING for i in self.issues)

    def assumed_steps(self) -> list[ExecutableStep]:
        return [s for s in self.steps if s.status == StepStatus.ASSUMED]

    def blocked_steps(self) -> list[ExecutableStep]:
        return [s for s in self.steps if s.status == StepStatus.BLOCKED]

    def step_by_id(self, step_id: str) -> ExecutableStep | None:
        return next((s for s in self.steps if s.step_id == step_id), None)
