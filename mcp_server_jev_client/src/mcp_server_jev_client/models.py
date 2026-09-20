"""Public observation, scored-decision, and voice-command contracts."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ObserveRequest(StrictModel):
    goal: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class VisibleObject(StrictModel):
    label: str
    screen_region: Literal["left", "center", "right"]


class Observation(StrictModel):
    summary: str
    objects: list[VisibleObject]
    possible_hazards: list[str]
    uncertainties: list[str]


class Degrees(StrictModel):
    x: Literal[0] = 0
    y: Annotated[int, Field(ge=-180, le=180, multiple_of=2)]
    z: Literal[0] = 0


class RotationArguments(StrictModel):
    degrees: Degrees


class EmptyArguments(StrictModel):
    pass


class RotationDecision(StrictModel):
    action: Literal["rotate"]
    arguments: RotationArguments


class SimpleDecision(StrictModel):
    action: Literal["walk_forward", "stop", "grab_item", "drop_item", "interact", "wait"]
    arguments: EmptyArguments = Field(default_factory=EmptyArguments)


Decision = Annotated[RotationDecision | SimpleDecision, Field(discriminator="action")]


class ObserveResponse(StrictModel):
    observation_sequence: int
    game_state: dict[str, Any]
    observation: Observation
    decision: Decision
    timings_ms: dict[str, float]


GAME_ACTIONS = (
    "walk_forward",
    "rotate",
    "stop",
    "grab_item",
    "drop_item",
    "wait",
)
SCORED_ACTIONS = (*GAME_ACTIONS, "abstain")
JobStatus = Literal[
    "queued",
    "scoring",
    "ready_to_execute",
    "dispatching",
    "dispatched",
    "completed",
    "needs_clarification",
    "blocked",
    "expired",
    "cancelled",
    "failed",
    "execution_unknown",
]


class ScoredDecision(StrictModel):
    """Model scores for the supplied decision question, not physics success odds."""

    action: Literal[
        "walk_forward", "rotate", "stop", "grab_item", "drop_item", "wait", "abstain"
    ]
    arguments: RotationArguments | EmptyArguments = Field(default_factory=EmptyArguments)
    action_probabilities: dict[str, float]
    selected_action_probability: float
    distribution_confidence: float | None = None
    resolved_model: str | None = None
    yaw_probabilities: dict[str, float] | None = None
    selected_yaw_probability: float | None = None
    yaw_distribution_confidence: float | None = None

    @model_validator(mode="after")
    def _arguments_match_action(self):
        if self.action == "rotate":
            if not isinstance(self.arguments, RotationArguments):
                raise ValueError("rotate requires relative yaw arguments")
        elif not isinstance(self.arguments, EmptyArguments):
            raise ValueError("this action takes no arguments")
        return self


class ExecutionResult(StrictModel):
    game_status: str | None = None
    message: str | None = None
    request_id: str | None = None
    interpretation: str | None = None
    paused: bool = False


class CommandContext(StrictModel):
    service_instance_id: UUID
    generation: int = Field(ge=0)


class VoiceCommandReceipt(StrictModel):
    accepted: bool
    service_instance_id: UUID
    command_id: UUID
    status: JobStatus | None = None
    existing: bool = False
    error: str | None = None


class VoiceCommandStatus(StrictModel):
    found: bool
    service_instance_id: UUID
    command_id: UUID | None = None
    transcript: str | None = None
    status: JobStatus | None = None
    source: Literal["jev", "explicit_control"] | None = None
    decision: ScoredDecision | None = None
    observation_sequence: int | None = None
    execution: ExecutionResult | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)
    error: str | None = None


class StopGameResult(StrictModel):
    service_instance_id: UUID
    cancelled_jobs: int
    generation: int
    game_cleared: bool
    game_stopped: bool
    execution_paused: bool
    error: str | None = None
