from dataclasses import dataclass
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
        "status",
        "turns",
        "unknown_event_count",
    )

    metadata: SessionMetadata
    file_path: Path
    status: SessionStatus
    turns: list[Turn]
    last_event_at: float
    last_progress_at: float
    activity_since: float
    pending_tool_name: ToolName | None
    pending_tool_started_at: float | None
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
        self.status = SessionStatus.UNKNOWN
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
