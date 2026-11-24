"""Replay identical in-process confirmation requests without a second write."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from threading import Lock
from typing import TYPE_CHECKING

from backend.protocol import ConfirmProposal
from backend.state import SessionState

if TYPE_CHECKING:
    from backend.actions import ActionOutcome


class ConfirmationConflict(ValueError):
    """The same proposal was already reserved with different inputs."""


@dataclass(frozen=True)
class _SavedConfirmation:
    outcome: ActionOutcome
    state_json: str


@dataclass(frozen=True)
class _Claim:
    fingerprint: bytes
    expires_at: int
    result: Future[_SavedConfirmation] = field(default_factory=Future)


class ConfirmationLedger:
    """Coordinate confirmation retries for one service process."""

    def __init__(self) -> None:
        self._claims: dict[tuple[str, str], _Claim] = {}
        self._lock = Lock()

    def execute(
        self,
        state: SessionState,
        action: ConfirmProposal,
        now: datetime,
        confirm: Callable[[], ActionOutcome],
    ) -> ActionOutcome:
        key = state.session_id, action.proposal_id
        payload = state.model_dump_json(exclude_none=True) + "\n" + action.model_dump_json(exclude_none=True)
        fingerprint = sha256(payload.encode("utf-8")).digest()
        timestamp = now.timestamp()
        with self._lock:
            expired = [
                name for name, item in self._claims.items()
                if item.expires_at <= timestamp and item.result.done()
            ]
            for name in expired:
                del self._claims[name]
            claim = self._claims.get(key)
            owner = claim is None
            if claim is None:
                claim = _Claim(fingerprint, state.expires_at)
                self._claims[key] = claim
            elif claim.fingerprint != fingerprint:
                raise ConfirmationConflict()

        if not owner:
            saved = claim.result.result()
            snapshot = SessionState.model_validate_json(saved.state_json)
            for name in SessionState.model_fields:
                setattr(state, name, getattr(snapshot, name))
            return deepcopy(saved.outcome)

        try:
            outcome = confirm()
            saved = _SavedConfirmation(deepcopy(outcome), state.model_dump_json(exclude_none=True))
        except BaseException as error:
            with self._lock:
                del self._claims[key]
                claim.result.set_exception(error)
            raise
        with self._lock:
            if outcome.error_code == "write_not_applied":
                del self._claims[key]
            claim.result.set_result(saved)
        return outcome
