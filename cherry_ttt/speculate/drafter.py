"""
Drafters — the speculation proposal side (proposal §3.2, D7).

Source: written for cherry_ttt P4; the Drafter protocol shape is frozen
    in build plan Part II (speculate/executor.py block). Template
    drafter per D7: "parameterized action macros with slot-fill from the
    current observation — deterministic, zero-parameter, directly
    measures the mechanism without confounding drafter quality."
Integrated: 2026-07-06
Purpose: A uniform drafter interface so drafter quality is an ablation
    axis, not an architecture change (§3.2). TemplateDrafter is the D7
    phase-1 default (predicate mode: probs are None). TabularDrafter
    exposes true per-state distributions over a discrete action space —
    the lossless-mode counterpart needed for the §8.4 distributional
    test, and the shape an n-gram-over-actions drafter will take later.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from ..core.mdp import State
from ..core.types import ActionCandidate


@runtime_checkable
class Drafter(Protocol):
    """Frozen Part II shape: draft(s, gamma) -> [(action, prob|None)].

    float prob => lossless mode eligible; None => predicate mode only.
    """

    def draft(self, s: State, gamma: int) -> list[tuple[ActionCandidate, float | None]]: ...


@dataclass(frozen=True)
class ActionTemplate:
    """One macro step: tool_id + args with {slot} placeholders in string
    values, filled from slot_fn(state) at draft time."""

    tool_id: str
    args: Mapping[str, Any]

    def bind(self, slots: Mapping[str, Any]) -> ActionCandidate:
        """Fill placeholders; non-string args pass through untouched."""
        bound: dict[str, Any] = {}
        for key, value in self.args.items():
            bound[key] = value.format(**slots) if isinstance(value, str) else value
        return ActionCandidate(self.tool_id, bound)


class TemplateDrafter:
    """D7: deterministic macro drafter, position-indexed by state depth.

    Args:
        macro: Ordered template steps; draft(s, gamma) returns the gamma
            steps starting at position s.depth - base_depth (a state
            deeper than the macro drafts nothing — the chain is spent).
        slot_fn: Extracts slot values from the current state/observation
            trail (the ctx carries observation kinds by construction of
            ContractMDP transitions).
        base_depth: Depth at which the macro begins (default 0 = root).
    """

    def __init__(
        self,
        macro: list[ActionTemplate],
        slot_fn: Callable[[State], Mapping[str, Any]] | None = None,
        base_depth: int = 0,
    ) -> None:
        self.macro = list(macro)
        self.slot_fn = slot_fn or (lambda _s: {})
        self.base_depth = base_depth

    def draft(self, s: State, gamma: int) -> list[tuple[ActionCandidate, float | None]]:
        """Next gamma macro steps from the state's position; probs None
        (predicate mode) by D7 design."""
        position = max(0, s.depth - self.base_depth)
        slots = self.slot_fn(s)
        window = self.macro[position: position + gamma]
        return [(template.bind(slots), None) for template in window]


class TabularDrafter:
    """Lossless-mode drafter: an explicit distribution per state key.

    Args:
        table: state-key -> {action: prob}; probs must sum to ~1.
        key_fn: State -> table key (default: ctx).
        rng: Seeded generator — sampling is part of the §8.4 contract,
            so the source of randomness is explicit and reproducible.
    """

    def __init__(
        self,
        table: Mapping[str, Mapping[ActionCandidate, float]],
        rng: random.Random,
        key_fn: Callable[[State], str] | None = None,
    ) -> None:
        self.table = {k: dict(v) for k, v in table.items()}
        self.rng = rng
        self.key_fn = key_fn or (lambda s: s.ctx)

    def dist(self, s: State) -> dict[ActionCandidate, float]:
        """The drafter's true distribution at s — the p_D of §3.2."""
        return dict(self.table[self.key_fn(s)])

    def draft(self, s: State, gamma: int) -> list[tuple[ActionCandidate, float | None]]:
        """Sample gamma actions sequentially; each carries its true draft
        prob at the state it was drafted from. Chain conditioning uses
        the SAME key (single-state tables draft i.i.d.; keyed tables
        condition on ctx growth — the test suite uses both)."""
        out: list[tuple[ActionCandidate, float | None]] = []
        dist = self.dist(s)
        actions = list(dist.keys())
        weights = [dist[a] for a in actions]
        for _ in range(gamma):
            choice = self.rng.choices(actions, weights=weights, k=1)[0]
            out.append((choice, dist[choice]))
        return out


__all__ = ["ActionTemplate", "Drafter", "TabularDrafter", "TemplateDrafter"]
