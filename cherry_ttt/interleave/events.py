"""Branch-local event ledger for interleaved reasoning/tool trajectories.

Cherry TTT cannot fabricate SRA activations.  It can, however, provide a stable
append-only lane where an attached reasoning runtime records phase, scratchpad,
memory, stability, or strategic events under the same branch identity used by
search and trajectory collection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class InterleavedEvent:
    """One provenance-visible event attached to a search branch."""

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source: str = "reasoning"


class BranchEventLedger:
    """Append-only in-memory event ledger keyed by stable branch id."""

    def __init__(self) -> None:
        self._events: dict[str, list[InterleavedEvent]] = {}

    def append(self, branch_id: str, event: InterleavedEvent) -> None:
        if not branch_id:
            raise ValueError("branch_id must be non-empty")
        self._events.setdefault(branch_id, []).append(event)

    def events_for(self, branch_id: str) -> tuple[InterleavedEvent, ...]:
        return tuple(self._events.get(branch_id, ()))


__all__ = ["BranchEventLedger", "InterleavedEvent"]
