import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import NewType, final

SessionId = NewType("SessionId", str)
TurnId = NewType("TurnId", str)
ToolName = NewType("ToolName", str)


class SessionStatus(StrEnum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    IDLE = "idle"
    STUCK = "stuck"


class ActivityState(StrEnum):
    ACTIVE = "active"
    QUIET = "quiet"
    TOOL_RUNNING = "tool_running"
    LONG_RUNNING_TOOL = "long_running_tool"
    NO_PROGRESS = "no_progress"
    DATA_STALE = "data_stale"


class AttentionReason(StrEnum):
    STUCK = "stuck"
    LONG_RUNNING_TOOL = "long_running_tool"
    NO_PROGRESS = "no_progress"
    TOOL_ERROR = "tool_error"
    TURN_ABORTED = "turn_aborted"


class ToolStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    ERROR = "error"


class TraceStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class TraceParseState(StrEnum):
    RECOGNIZED = "recognized"
    PARTIALLY_RECOGNIZED = "partially_recognized"
    UNKNOWN = "unknown"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class TraceEvent:
    trace_id: str
    provider: str
    session_id: SessionId
    turn_id: TurnId | None
    sequence: int
    event_kind: str
    call_id: str | None
    tool_name: str | None
    namespace: str | None
    status: TraceStatus
    started_at: float | None
    ended_at: float | None
    input_value: object | None
    result_value: object | None
    raw_payload: object
    source_line: int | None
    source_offset: int | None
    source_type: str | None
    parse_state: TraceParseState
    related_trace_id: str | None = None
    _content_metadata_cache: dict[str, object] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.ended_at is None:
            return None
        return max(0, round((self.ended_at - self.started_at) * 1000))

    @property
    def content_metadata(self) -> dict[str, object]:
        if self._content_metadata_cache is not None:
            return self._content_metadata_cache
        values = [self.input_value, self.result_value, self.raw_payload]
        encoded = [json_bytes(value) for value in values if value is not None]
        metadata: dict[str, object] = {
            "bytes": sum(len(item) for item in encoded),
            "has_input": self.input_value is not None,
            "has_result": self.result_value is not None,
            "has_raw": self.raw_payload is not None,
            "sha256": hashlib.sha256(
                b"".join(json_bytes(value) for value in values if value is not None)
            ).hexdigest(),
        }
        object.__setattr__(self, "_content_metadata_cache", metadata)
        return metadata


def json_size(value: object) -> int:
    return len(json_bytes(value))


def json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError):
        return str(value).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    session_id: SessionId
    cwd: str
    originator: str
    source: str
    cli_version: str
    model_provider: str


@final
class ToolCall:
    __slots__ = (
        "call_id",
        "ended_at",
        "input_summary",
        "name",
        "result_summary",
        "started_at",
        "status",
    )

    name: ToolName
    input_summary: str
    status: ToolStatus
    result_summary: str
    started_at: float
    ended_at: float | None
    call_id: str | None

    def __init__(
        self,
        name: ToolName,
        input_summary: str,
        started_at: float,
        call_id: str | None = None,
    ) -> None:
        self.name = name
        self.input_summary = input_summary
        self.call_id = call_id
        self.status = ToolStatus.PENDING
        self.result_summary = ""
        self.started_at = started_at
        self.ended_at = None


@final
class Turn:
    __slots__ = (
        "aborted",
        "agent_text_snippets",
        "end_reason",
        "ended_at",
        "started_at",
        "tool_calls",
        "turn_id",
        "user_message",
    )

    turn_id: TurnId
    started_at: float
    ended_at: float | None
    user_message: str
    tool_calls: list[ToolCall]
    agent_text_snippets: list[str]
    aborted: bool
    end_reason: str

    def __init__(self, turn_id: TurnId, started_at: float) -> None:
        self.turn_id = turn_id
        self.started_at = started_at
        self.ended_at = None
        self.user_message = ""
        self.tool_calls = []
        self.agent_text_snippets = []
        self.aborted = False
        self.end_reason = ""


@final
class Session:
    __slots__ = (
        "activity_since",
        "approx_context_chars",
        "byte_offset",
        "file_path",
        "last_event_at",
        "last_progress_at",
        "malformed_line_count",
        "metadata",
        "oversized_line_count",
        "pending_tool_name",
        "pending_tool_started_at",
        "project_key",
        "project_root",
        "source_identity",
        "status",
        "trace_events",
        "turns",
        "unknown_event_count",
    )

    metadata: SessionMetadata
    file_path: Path
    status: SessionStatus
    trace_events: list[TraceEvent]
    turns: list[Turn]
    last_event_at: float
    last_progress_at: float
    activity_since: float
    pending_tool_name: ToolName | None
    pending_tool_started_at: float | None
    source_identity: str
    approx_context_chars: int
    byte_offset: int
    unknown_event_count: int
    malformed_line_count: int
    oversized_line_count: int

    def __init__(
        self,
        metadata: SessionMetadata,
        file_path: Path,
        last_event_at: float,
        project_key: str = "default",
        project_root: Path | None = None,
    ) -> None:
        self.metadata = metadata
        self.file_path = file_path
        self.project_key = project_key
        self.project_root = project_root or Path(metadata.cwd)
        self.source_identity = ""
        self.status = SessionStatus.UNKNOWN
        self.trace_events = []
        self.turns = []
        self.last_event_at = last_event_at
        self.last_progress_at = last_event_at
        self.activity_since = last_event_at
        self.pending_tool_name = None
        self.pending_tool_started_at = None
        self.approx_context_chars = 0
        self.byte_offset = 0
        self.unknown_event_count = 0
        self.malformed_line_count = 0
        self.oversized_line_count = 0

    @property
    def session_id(self) -> SessionId:
        return self.metadata.session_id

    @property
    def session_key(self) -> str:
        return f"{self.project_key}:{self.session_id}"

    @property
    def cwd(self) -> str:
        return self.metadata.cwd

    @property
    def current_turn(self) -> Turn | None:
        return self.turns[-1] if self.turns else None
