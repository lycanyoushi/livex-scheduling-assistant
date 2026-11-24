"""Provider-neutral events and errors for a streamed model interaction."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelText:
    text: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ModelCompletion:
    interaction_id: str
    status: str
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    thought_tokens: int = 0
    can_continue: bool = True
    reason: str | None = None


@dataclass
class ModelError(Exception):
    code: str
    http_status: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        super().__init__(self.code)


class ModelClient(Protocol):
    def stream(
        self,
        *,
        input_steps: list[dict],
        previous_interaction_id: str | None,
        system_instruction: str,
        tools: list[dict],
    ) -> AsyncIterator[ModelText | ModelCompletion]: ...
