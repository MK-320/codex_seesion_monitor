import json
from datetime import datetime
from json import JSONDecodeError
from typing import Annotated, ClassVar, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError


class BoundaryModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)


class SessionMetaPayload(BoundaryModel):
    session_id: str | None = None
    id: str | None = None
    cwd: str
    originator: str = "unknown"
    cli_version: str = "unknown"
    source: str = "unknown"
    model_provider: str = "unknown"


class SessionMetaEvent(BoundaryModel):
    timestamp: datetime
    type: Literal["session_meta"]
    payload: SessionMetaPayload


class TaskStartedPayload(BoundaryModel):
    type: Literal["task_started"]
    turn_id: str
    started_at: float | None = None


class UserMessagePayload(BoundaryModel):
    type: Literal["user_message"]
    message: str


class AgentMessagePayload(BoundaryModel):
    type: Literal["agent_message"]
    message: str


class TaskCompletePayload(BoundaryModel):
    type: Literal["task_complete"]
    turn_id: str


class TurnAbortedPayload(BoundaryModel):
    type: Literal["turn_aborted"]
    turn_id: str
    reason: str = "aborted"


class MessageContent(BoundaryModel):
    type: Literal["input_text", "output_text"]
    text: str


class ResponseMessagePayload(BoundaryModel):
    type: Literal["message"]
    role: Literal["user", "assistant"]
    content: tuple[MessageContent, ...] = ()


class FunctionCallPayload(BoundaryModel):
    type: Literal["function_call", "custom_tool_call"]
    name: str
    call_id: str
    arguments: str = ""
    input: str = ""


class FunctionCallOutputPayload(BoundaryModel):
    type: Literal["function_call_output", "custom_tool_call_output"]
    call_id: str
    output: object = ""


type EventPayload = Annotated[
    TaskStartedPayload
    | UserMessagePayload
    | AgentMessagePayload
    | TaskCompletePayload
    | TurnAbortedPayload,
    Field(discriminator="type"),
]


class EventMessageEvent(BoundaryModel):
    timestamp: datetime
    type: Literal["event_msg"]
    payload: EventPayload


type ResponsePayload = Annotated[
    ResponseMessagePayload | FunctionCallPayload | FunctionCallOutputPayload,
    Field(discriminator="type"),
]


class ResponseItemEvent(BoundaryModel):
    timestamp: datetime
    type: Literal["response_item"]
    payload: ResponsePayload


type KnownEvent = Annotated[
    SessionMetaEvent | EventMessageEvent | ResponseItemEvent,
    Field(discriminator="type"),
]

EVENT_ADAPTER: Final[TypeAdapter[KnownEvent]] = TypeAdapter(KnownEvent)

type ParseIssue = Literal["unknown", "malformed"]


def parse_event_with_issue(raw_line: bytes) -> tuple[KnownEvent | None, ParseIssue | None]:
    try:
        value = cast("object", json.loads(raw_line))
    except (JSONDecodeError, UnicodeDecodeError):
        return None, "malformed"
    try:
        return EVENT_ADAPTER.validate_python(value), None
    except ValidationError:
        if isinstance(value, dict) and _is_unknown_event(cast("dict[str, object]", value)):
            return None, "unknown"
        return None, "malformed"


def parse_event(raw_line: bytes) -> KnownEvent | None:
    event, _ = parse_event_with_issue(raw_line)
    return event


def _is_unknown_event(value: dict[str, object]) -> bool:
    event_type = value.get("type")
    if event_type not in {"session_meta", "event_msg", "response_item"}:
        return True
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return False
    payload_type = cast("dict[str, object]", payload).get("type")
    if event_type == "event_msg":
        return payload_type not in {
            "task_started",
            "user_message",
            "agent_message",
            "task_complete",
            "turn_aborted",
        }
    if event_type == "response_item":
        return payload_type not in {
            "message",
            "function_call",
            "custom_tool_call",
            "function_call_output",
            "custom_tool_call_output",
        }
    return False
