"""
MDP protocol and the degenerate lexical binding — the pivotal split.

Source: inference_optimizations.py LexicalMDP / MDPConfig (Cherry RL
    pipeline), generalized per proposal §6.2: extract the MDP Protocol
    into core/, keep LexicalMDP as the reference implementation and the
    D6 equivalence-test oracle binding.
Integrated: 2026-07-05
Purpose: The contract every search consumes (build plan Part II). The
    lexical binding is proposal §1.2's degenerate contract surface:
    S_env = strings (carried in State.ctx, env handle None), actions are
    appended text, transitions are free (zero-cost) string concatenation.
    ContractMDP over a TransactionalSubstrate arrives in P3; nothing in
    search/ may know which binding it is running (standing invariant 1).

Ledger amendment A1 (recorded in snapshots/v0.1/manifest.json): the MDP
    Protocol gains a sixth method, action_label(a) -> str, defaulting to
    a.canonical(). Required so trace records and serialized trees are
    binding-appropriate (lexical: raw text, matching the original files'
    output for the D6 gate; contract: canonical id) without search code
    special-casing bindings.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

from .types import (
    ActionCandidate,
    Cost,
    EnvDigest,
    GoalSpec,
    Observation,
    SnapshotHandle,
    TerminalStatus,
    Trajectory,
)

LEXICAL_TOOL_ID = "lexical.append"


@dataclass(frozen=True, slots=True)
class State:
    """Search state (frozen, Part II): context text, optional env handle,
    transposition digest, depth. env is None in the degenerate binding."""

    ctx: str
    env: SnapshotHandle | None
    digest: EnvDigest
    depth: int


@runtime_checkable
class MDP(Protocol):
    """The contract both EnvMCTS and EnvAStar consume (frozen, Part II
    plus amendment A1)."""

    def initial_state(self, goal: GoalSpec, ctx: str) -> State: ...

    def legal_actions(self, s: State, n: int) -> list[tuple[ActionCandidate, float]]: ...

    def transition(self, s: State, a: ActionCandidate) -> tuple[State, Observation, Cost]: ...

    def is_terminal(self, s: State) -> TerminalStatus: ...

    def reward(self, s: State, trajectory: Trajectory) -> float: ...

    def action_label(self, a: ActionCandidate) -> str: ...  # amendment A1


@runtime_checkable
class LexicalPolicy(Protocol):
    """Action proposer for the lexical binding: continuations with priors.

    Any object with this shape is a policy — a torch model adapter, an
    n-gram sampler, or a replay table; the binding cannot tell and must
    not care."""

    def propose(self, ctx: str, n: int, temperature: float) -> list[tuple[str, float]]: ...


@dataclass
class LexicalMDPConfig:
    """Port of MDPConfig, field-for-field (terminal semantics preserved)."""

    max_depth: int = 100
    terminal_strings: list[str] = field(default_factory=lambda: ["</s>"])
    terminal_max_len: int = 2000
    step_delimiter: str = "\n\n"
    eos_token: str | None = "</s>"


def _lexical_digest(ctx: str) -> EnvDigest:
    """Digest for the degenerate binding: content hash of the string state.

    Injective over ctx, so transposition keys reproduce the original
    files' string-keyed dedup semantics exactly."""
    return EnvDigest(hashlib.sha256(ctx.encode("utf-8")).hexdigest())


class LexicalMDP:
    """The degenerate contract surface (proposal §1.2), reference binding.

    Source: inference_optimizations.py LexicalMDP; transition, terminal
    logic, and legal-action shape carried faithfully. The torch-coupled
    legal_actions body is replaced by the injected LexicalPolicy — the
    binding no longer knows what proposes its actions.
    """

    def __init__(
        self,
        policy: LexicalPolicy,
        config: LexicalMDPConfig | None = None,
        temperature: float = 1.0,
        reward_fn: Callable[[str], float] | None = None,
    ) -> None:
        self.policy = policy
        self.config = config or LexicalMDPConfig()
        self.temperature = temperature
        self.reward_fn = reward_fn

    def initial_state(self, goal: GoalSpec, ctx: str) -> State:
        """Root state from prompt text; goal is carried by the caller in
        the degenerate binding (rewards arrive via reward_fn)."""
        return State(ctx=ctx, env=None, digest=_lexical_digest(ctx), depth=0)

    def legal_actions(self, s: State, n: int) -> list[tuple[ActionCandidate, float]]:
        """Top-n continuations as structured actions with priors."""
        proposals = self.policy.propose(s.ctx, n, self.temperature)
        return [
            (ActionCandidate(LEXICAL_TOOL_ID, {"text": text}), float(prob))
            for text, prob in proposals
        ]

    def transition(self, s: State, a: ActionCandidate) -> tuple[State, Observation, Cost]:
        """Free string concatenation: E(s, a) = (s + a, empty, 0) — the
        defining property of the degenerate surface (proposal §1.2)."""
        new_ctx = s.ctx + str(a.args["text"])
        new_state = State(
            ctx=new_ctx, env=None, digest=_lexical_digest(new_ctx), depth=s.depth + 1
        )
        return new_state, Observation(kind="empty", payload=None), Cost()

    def is_terminal(self, s: State) -> TerminalStatus:
        """Terminal semantics of the original, check order preserved:
        length cap first (BUDGET), then terminal strings / eos (SOLVED)."""
        cfg = self.config
        if len(s.ctx) >= cfg.terminal_max_len:
            return TerminalStatus.BUDGET
        for terminal in cfg.terminal_strings:
            if s.ctx.endswith(terminal):
                return TerminalStatus.SOLVED
        if cfg.eos_token and s.ctx.endswith(cfg.eos_token):
            return TerminalStatus.SOLVED
        return TerminalStatus.OPEN

    def reward(self, s: State, trajectory: Trajectory) -> float:
        """Delegate to reward_fn over the string state, else 0.0 —
        verbatim semantics of the original."""
        if self.reward_fn is not None:
            return float(self.reward_fn(s.ctx))
        return 0.0

    def action_label(self, a: ActionCandidate) -> str:
        """Lexical label is the raw appended text (D6: trace records and
        tree dumps must match the original files byte-for-byte)."""
        return str(a.args["text"])
