"""Public observation and decision contracts."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


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
    action: Literal["walk_forward", "stop", "grab_item", "drop_item", "wait"]
    arguments: EmptyArguments = Field(default_factory=EmptyArguments)


Decision = Annotated[RotationDecision | SimpleDecision, Field(discriminator="action")]


class ObserveResponse(StrictModel):
    observation_sequence: int
    game_state: dict[str, Any]
    observation: Observation
    decision: Decision
    timings_ms: dict[str, float]
