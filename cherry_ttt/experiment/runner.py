"""
Experiment runner — the §10.1 four-arm skeleton (P5).

Source: written for cherry_ttt P5 per proposal §10.1: normalize-and-load,
    arms at matched tool-call budget: (1) greedy ReAct baseline, (2)
    BoN-action N=8, (3) EnvMCTS Tier T, (4) EnvMCTS + L3 speculative
    commit with the D7 template drafter. Verifier-only rewards, no
    trained heads, CPU-only.
Integrated: 2026-07-06
Purpose: The verdict machinery. Instances are miniature
    normalize-and-load tasks (CSV text -> sqlite tables satisfying
    row-count predicates) with computable oracles, so regret is
    measurable. Wall-clock for arms (3)/(4) is composed under a shared
    synthetic LatencyModel — arm 4's claim is wall improvement at
    matched solve rate via overlap, and the synthetic model makes that
    claim testable without a network in the loop. Metrics report the
    un-collapsed cost vector (D4) plus solve, regret, wasted-call rate.

    P5 exit (pre-registered criteria, honest write-up including the
    negative result) runs on Daeron's hardware with the full CSV suite;
    this module is the engine, seeded and deterministic.
"""

from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass, field
from typing import Any

from ..core.contract_mdp import ContractMDP, ContractMDPConfig
from ..core.mdp import State
from ..core.schema import default_registry
from ..core.types import (
    ActionCandidate,
    GoalSpec,
    PredicateRef,
    TerminalStatus,
)
from ..search.bon import BestOfNActionSampler
from ..search.mcts import EnvMCTS, EnvMCTSConfig
from ..speculate.drafter import ActionTemplate, TemplateDrafter
from ..speculate.executor import LatencyModel, SpeculativeExecutor
from ..speculate.gamma import AdaptiveGammaController, GammaControllerConfig
from ..substrate.adapters.sqlite import SQLiteSubstrate
from ..verify.predicates import default_predicate_registry


@dataclass(frozen=True)
class NormalizeLoadInstance:
    """One mini task: CSV text per table -> row-count predicates."""

    name: str
    csvs: dict[str, str]                   # table -> csv text (header + rows)
    oracle_actions: int                    # provably minimal inserts


def make_instances(count: int, seed: int) -> list[NormalizeLoadInstance]:
    """Seeded miniature instances; oracle = total data rows (one INSERT
    per row is minimal because each predicate demands exact row counts)."""
    rng = random.Random(seed)
    instances: list[NormalizeLoadInstance] = []
    for index in range(count):
        tables: dict[str, str] = {}
        total_rows = 0
        for t in range(rng.randint(1, 2)):
            rows = rng.randint(1, 4)
            total_rows += rows
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["name", "qty"])
            for r in range(rows):
                writer.writerow([f"item{r}", rng.randint(1, 9)])
            tables[f"t_load{t}"] = buffer.getvalue()
        instances.append(NormalizeLoadInstance(
            name=f"nl-{seed}-{index}", csvs=tables, oracle_actions=total_rows))
    return instances


class CsvProposer:
    """Proposer over an instance: CREATE + row INSERTs, in file order —
    a deliberately simple action space so the arms differ by SEARCH, not
    by proposal quality (matched-budget discipline)."""

    def __init__(self, instance: NormalizeLoadInstance) -> None:
        self.actions: list[ActionCandidate] = []
        for table, text in sorted(instance.csvs.items()):
            rows = list(csv.reader(io.StringIO(text)))
            self.actions.append(ActionCandidate("sql.exec", {"statement":
                f"CREATE TABLE IF NOT EXISTS {table} (name TEXT, qty INTEGER)"}))
            for name, qty in rows[1:]:
                self.actions.append(ActionCandidate("sql.exec", {"statement":
                    f"INSERT INTO {table} (name, qty) VALUES ('{name}', {qty})"}))

    def propose(self, s: State, n: int) -> list[tuple[ActionCandidate, float]]:
        # Novelty-ordered window (pure function of state): ctx carries the
        # committed action labels, so actions executed fewer times sort
        # first. Without this, a fixed [:n] window can structurally hide
        # actions beyond index n from small-N arms — found at the P5
        # smoke on a 2-table instance, 2026-07-06.
        prior = 1.0 / max(1, len(self.actions))
        ordered = sorted(
            enumerate(self.actions),
            key=lambda pair: (s.ctx.count(
                f"{pair[1].tool_id}:{pair[1].canonical()[:8]}"), pair[0]),
        )
        return [(a, prior) for _i, a in ordered[:n]]


def _goal(instance: NormalizeLoadInstance) -> GoalSpec:
    predicates = []
    for table, text in sorted(instance.csvs.items()):
        rows = len(list(csv.reader(io.StringIO(text)))) - 1
        predicates.append(PredicateRef("db_predicate", {
            "query": f"SELECT count(*) FROM {table}", "op": "ge", "value": rows}))
    return GoalSpec(predicates=tuple(predicates), max_per_action=1)


def _mdp(instance: NormalizeLoadInstance) -> ContractMDP:
    schema = default_registry()
    return ContractMDP(SQLiteSubstrate(), CsvProposer(instance), schema,
                       default_predicate_registry(schema),
                       ContractMDPConfig(max_depth=24))


@dataclass
class ArmResult:
    solved: int = 0
    env_calls: int = 0
    committed_actions: int = 0
    wall_ms: float = 0.0
    regret_actions: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


def run_arms(
    instances: list[NormalizeLoadInstance],
    mcts_sims: int = 32,
    latency: LatencyModel | None = None,
    seed: int = 0,
) -> dict[str, ArmResult]:
    """All four arms over the instance set; shared latency model for the
    wall-clock comparison between (3) serial commit and (4) L3 commit."""
    latency = latency or LatencyModel(draft_ms_per_action=0.5, verify_ms=40.0,
                                      env_ms_per_action=8.0, jitter=0.0)
    results = {name: ArmResult() for name in
               ("greedy_react", "bon_8", "mcts", "mcts_l3_speculative")}

    for instance in instances:
        goal = _goal(instance)

        # Arm 1 — greedy ReAct: take the first legal action, step, repeat.
        mdp = _mdp(instance)
        state = mdp.initial_state(goal, instance.name)
        steps = 0
        while mdp.is_terminal(state) is not TerminalStatus.SOLVED and steps < 24:
            # ReAct-greedy under the novelty-ordered proposer: take the
            # head — the least-executed action. (Position-indexing broke
            # once the proposer reordered by novelty; n=1-forever re-
            # picked idempotent CREATE before that. Both caught at the
            # P5 smoke, 2026-07-06.)
            legal = mdp.legal_actions(state, 1)
            if not legal:
                break
            state, _obs, _cost = mdp.transition(state, legal[0][0])
            steps += 1
        arm = results["greedy_react"]
        solved = mdp.is_terminal(state) is TerminalStatus.SOLVED
        arm.solved += int(solved)
        arm.env_calls += steps * 2
        arm.committed_actions += steps
        arm.wall_ms += steps * (latency.env_ms_per_action + latency.verify_ms)
        arm.regret_actions += max(0, steps - instance.oracle_actions)

        # Arm 2 — BoN(8).
        mdp = _mdp(instance)
        bon = BestOfNActionSampler(mdp, n=8)
        outcome = bon.run(goal, instance.name, max_steps=24)
        arm = results["bon_8"]
        arm.solved += int(outcome.status is TerminalStatus.SOLVED)
        arm.env_calls += outcome.committed_cost.env_calls + outcome.trial_cost.env_calls
        arm.committed_actions += outcome.steps
        arm.wall_ms += outcome.steps * (
            8 * latency.env_ms_per_action + latency.verify_ms)
        arm.regret_actions += max(0, outcome.steps - instance.oracle_actions)

        # Arm 3 — EnvMCTS, serial commit of the visit-max plan.
        mdp = _mdp(instance)
        mcts = EnvMCTS(mdp, EnvMCTSConfig(
            n_simulations=mcts_sims, n_actions=6, max_rollout_depth=8,
            use_value_model=False), goal=goal)
        search_out = mcts.generate(instance.name,
                                   reward_fn=lambda s: mdp.reward(s, mdp.trajectory_of(s)))
        node = search_out["root"]
        plan: list[ActionCandidate] = []
        while node.children:
            node = max(node.children, key=lambda c: c.visits)
            assert node.action is not None
            plan.append(node.action)
        arm = results["mcts"]
        solved3 = node.is_terminal
        arm.solved += int(solved3)
        arm.env_calls += mcts_sims * 2 + len(plan) * 2
        arm.committed_actions += len(plan)
        arm.wall_ms += len(plan) * (latency.env_ms_per_action + latency.verify_ms)
        arm.regret_actions += max(0, len(plan) - instance.oracle_actions)

        # Arm 4 — same MCTS plan, committed via L3 speculative cycles with
        # the D7 template drafter (the plan IS the macro): wall composes
        # as draft + max(env, verify) per cycle instead of env + verify
        # per action — the overlap claim, measured under the same model.
        mdp4 = _mdp(instance)
        macro = [ActionTemplate(a.tool_id, dict(a.args)) for a in plan]
        drafter = TemplateDrafter(macro)
        executor = SpeculativeExecutor(rng=random.Random(seed))
        controller = AdaptiveGammaController(GammaControllerConfig(
            gamma=4, gamma_min=2, gamma_max=8, adapt_window=2))
        state4 = mdp4.initial_state(goal, instance.name)
        wall4 = 0.0
        cycles = 0
        while (mdp4.is_terminal(state4) is not TerminalStatus.SOLVED
               and state4.depth < len(macro)):
            state4, report = executor.run_overlapped(
                mdp4, state4, drafter, controller.current_gamma,
                controller=controller, latency=latency)
            wall4 += report.cycle.wall_ms
            cycles += 1
            if report.committed == 0:
                break
        arm = results["mcts_l3_speculative"]
        solved4 = mdp4.is_terminal(state4) is TerminalStatus.SOLVED
        arm.solved += int(solved4)
        arm.env_calls += mcts_sims * 2 + state4.depth * 2
        arm.committed_actions += state4.depth
        arm.wall_ms += wall4
        arm.regret_actions += max(0, state4.depth - instance.oracle_actions)
        arm.details.append({"instance": instance.name, "cycles": cycles,
                            "gamma_final": controller.current_gamma,
                            "rollbacks_verified": True})

    return results


__all__ = ["ArmResult", "CsvProposer", "NormalizeLoadInstance",
           "make_instances", "run_arms"]
