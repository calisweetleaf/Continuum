"""
Transactional substrate utilities.

This module contains adapter-independent enforcement helpers for the
Tier-T contract.  It is intentionally small: concrete substrates own the
state, while this module owns repeatable soundness checks and branch
receipts that higher layers can use without learning substrate internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..core.errors import SoundnessError
from ..core.types import ActionCandidate, Cost, EnvDigest, SnapshotHandle
from .base import TransactionalSubstrateBase


@dataclass(frozen=True, slots=True)
class RestoreReceipt:
    """Evidence for one snapshot/restore soundness check."""

    snapshot: SnapshotHandle
    before: EnvDigest
    after: EnvDigest
    actions_executed: int
    execution_cost: Cost


def verify_restore_soundness(
    substrate: TransactionalSubstrateBase,
    actions: Iterable[ActionCandidate],
) -> RestoreReceipt:
    """Snapshot, execute actions, restore, and require digest equality.

    Args:
        substrate: Real Tier-T substrate under test.
        actions: Actions to execute after the snapshot.  The substrate's
            public effect gate is used; forbidden effects raise before
            any side effect.

    Returns:
        RestoreReceipt with digest evidence and accumulated action cost.

    Raises:
        SoundnessError: If restore(snapshot) does not return the touched
            state digest exactly to the pre-snapshot value.
    """
    snap = substrate.snapshot()
    before = substrate.digest()
    total = Cost()
    count = 0
    for action in actions:
        _obs, cost = substrate.execute(action)
        total = total + cost
        count += 1
    substrate.restore(snap)
    after = substrate.digest()
    if str(before) != str(after):
        raise SoundnessError(
            f"restore({snap.token}) failed digest equality: {after} != {before}"
        )
    return RestoreReceipt(
        snapshot=snap,
        before=before,
        after=after,
        actions_executed=count,
        execution_cost=total,
    )


__all__ = ["RestoreReceipt", "verify_restore_soundness"]
