"""
Intermediate Representation (IR) for the ModelForge compiler pipeline.
All compiler stages operate on these dataclasses.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ParamKind(Enum):
    VECTOR_LAYER  = "vectorlayer"
    RASTER_LAYER  = "rasterlayer"
    FIELD         = "field"
    EXPRESSION    = "expression"
    NUMBER        = "number"
    BOOLEAN       = "boolean"
    STRING        = "string"
    ENUM          = "enum"
    CRS           = "crs"
    EXTENT        = "extent"
    SINK          = "sink"
    FEATURE_SINK  = "featuresink"
    RASTER_DEST   = "rasterdestination"
    UNKNOWN       = "unknown"


class StepStatus(Enum):
    ASSUMED   = "assumed"
    RESOLVED  = "resolved"
    BLOCKED   = "blocked"
    CUSTOM    = "custom"


class IssueLevel(Enum):
    INFO    = "info"
    WARNING = "warning"
    ERROR   = "error"


@dataclass
class ModelInput:
    name:          str
    kind:          ParamKind
    label:         str   = ""
    description:   str   = ""
    optional:      bool  = False
    default_value: Any   = None
    pos_x:         float = 20.0
    pos_y:         float = 20.0


@dataclass
class ResolvedAlgorithm:
    algorithm_id:  str
    display_name:  str = ""
    provider_id:   str = ""


@dataclass
class ExpressionNode:
    node_type:     str            # comparison|logical|function|literal|field_ref
    rendered:      Optional[str] = None
    value:         Any           = None
    field_name:    Optional[str] = None
    function_name: Optional[str] = None
    arguments:     List          = field(default_factory=list)
    operator:      Optional[str] = None
    left:          Optional["ExpressionNode"] = None
    right:         Optional["ExpressionNode"] = None


@dataclass
class ParameterBinding:
    source_type:   str                       # model_input|child_output|static|expression|enum_index
    model_input:   Optional[str]  = None
    child_id:      Optional[str]  = None
    output_name:   Optional[str]  = None
    static_value:  Any            = None
    enum_index:    Optional[int]  = None
    expression:    Optional[ExpressionNode] = None


@dataclass
class ExecutableStep:
    step_id:    str
    label:      str
    status:     StepStatus        = StepStatus.ASSUMED
    confidence: float             = 0.0
    algorithm:  Optional[ResolvedAlgorithm] = None
    parameters: Dict[str, ParameterBinding] = field(default_factory=dict)
    pos_x:      float             = 0.0
    pos_y:      float             = 0.0
    rank:       int               = 0


@dataclass
class PlanIssue:
    level:      IssueLevel
    code:       str
    message:    str
    step_id:    Optional[str] = None
    param_name: Optional[str] = None


@dataclass
class ExecutablePlan:
    inputs:   List[ModelInput]   = field(default_factory=list)
    steps:    List[ExecutableStep] = field(default_factory=list)
    issues:   List[PlanIssue]    = field(default_factory=list)
    metadata: Dict[str, Any]     = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not any(i.level == IssueLevel.ERROR for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.level == IssueLevel.WARNING for i in self.issues)
