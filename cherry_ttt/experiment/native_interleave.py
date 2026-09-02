"""Native reasoning/tool interleave pilot for the v0.4 power-scale slice.

The pilot is intentionally small and falsifiable.  A contextual proposer must
read a raw ``kv.get`` observation from its branch trajectory and synthesize the
next typed action.  EnvMCTS must widen the root, preserve branch-local
observations, reach the verified target, and emit a complete training trace.

This does not claim SRA integration.  It proves the exact contract an SRA
adapter can inhabit without assistant-style tool narration.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..collect.trajectories import TrajectoryCollector
from ..core.contract_mdp import ContractMDP
from ..core.schema import default_registry
from ..core.types import ActionCandidate, GoalSpec, PredicateRef
from ..interleave import BranchEventLedger, InterleavedEvent, ReasoningContext
from ..search.mcts import EnvMCTS, EnvMCTSConfig
from ..substrate.adapters import MemoryKVSubstrate
from ..verify.predicates import default_predicate_registry


class ObservationDrivenPilotProposer:
    """Deterministic contextual policy used only by this mechanism pilot."""

    def __init__(self, events: BranchEventLedger) -> None:
        self.events = events
        self._recorded_branches: set[str] = set()

    def propose_with_context(
        self,
        context: ReasoningContext,
        n: int,
    ) -> list[tuple[ActionCandidate, float]]:
        last = context.last_step
        if context.branch_id not in self._recorded_branches:
            self.events.append(
                context.branch_id,
                InterleavedEvent(
                    kind="reasoning.propose",
                    source="pilot_contextual_policy",
                    payload={
                        "depth": context.state.depth,
                        "last_tool": last.action.tool_id if last is not None else None,
                        "last_observation_kind": (
                            last.observation.kind if last is not None else None
                        ),
                        "last_observation_payload": (
                            last.observation.payload if last is not None else None
                        ),
                    },
                ),
            )
            self._recorded_branches.add(context.branch_id)
        if last is None:
            return [
                (ActionCandidate("kv.get", {"k": "seed"}), 0.8),
                (ActionCandidate("kv.get", {"k": "missing"}), 0.2),
            ][:n]
        if last.action.tool_id == "kv.get":
            payload = last.observation.payload
            value = payload + 1 if isinstance(payload, (int, float)) else -1
            return [(ActionCandidate("kv.put", {"k": "result", "v": value}), 1.0)]
        return []


def run_native_interleave_pilot(output_dir: str | Path) -> dict[str, Any]:
    """Execute the pilot and write JSON, Markdown, log, and JSONL artifacts."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)

    events = BranchEventLedger()
    substrate = MemoryKVSubstrate()
    substrate.execute(ActionCandidate("kv.put", {"k": "seed", "v": 7}))
    schema = default_registry()
    mdp = ContractMDP(
        substrate=substrate,
        proposer=ObservationDrivenPilotProposer(events),
        schema=schema,
        predicates=default_predicate_registry(schema),
    )
    goal = GoalSpec((PredicateRef("kv_predicate", {"k": "result", "v": 8}),))
    search = EnvMCTS(
        mdp,
        EnvMCTSConfig(
            n_simulations=24,
            n_actions=4,
            max_rollout_depth=2,
            progressive_widening_alpha=0.5,
            use_value_model=False,
        ),
        goal=goal,
    )
    result = search.generate(
        "derive result from the observed seed",
        reward_fn=lambda state: mdp.reward(state, mdp.trajectory_of(state)),
    )
    root = result["root"]
    samples = TrajectoryCollector().collect_from_mcts(
        root,
        "derive result from the observed seed",
        event_ledger=events,
    )
    terminal = [sample for sample in samples if sample.status == "terminal"]
    if not terminal:
        raise RuntimeError("native interleave pilot failed: no terminal trajectory emitted")
    best = max(terminal, key=lambda sample: (sample.reward, sample.visit_count))

    report: dict[str, Any] = {
        "pilot": "native-reasoning-tool-interleave-v0.4",
        "solved": best.reward >= 0.999,
        "root_children": len(root.children),
        "root_actions": [dict(child.action.args) for child in root.children if child.action],
        "terminal_depth": best.depth,
        "terminal_actions": best.actions,
        "terminal_observations": best.observations,
        "terminal_cost": best.cost,
        "terminal_events": best.events,
        "samples_emitted": len(samples),
        "claims": {
            "raw_observation_reached_contextual_proposer": any(
                event["kind"] == "reasoning.propose"
                and event["payload"].get("last_observation_payload") == 7
                for event in best.events
            ),
            "root_progressive_widening": len(root.children) > 1,
            "complete_two_action_training_trace": len(best.actions) == 2,
            "sra_integration": False,
        },
    }
    if not all(
        (
            report["solved"],
            report["claims"]["raw_observation_reached_contextual_proposer"],
            report["claims"]["root_progressive_widening"],
            report["claims"]["complete_two_action_training_trace"],
        )
    ):
        raise RuntimeError(f"native interleave pilot failed: {report}")

    (target / "result.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    TrajectoryCollector.to_jsonl(samples, target / "trajectories.jsonl")
    markdown = "\n".join(
        [
            "# Native Reasoning/Tool Interleave Pilot v0.4",
            "",
            f"- solved: **{report['solved']}**",
            f"- root children: **{report['root_children']}**",
            f"- terminal depth: **{report['terminal_depth']}**",
            f"- emitted samples: **{report['samples_emitted']}**",
            "- raw observation reached contextual proposer: "
            f"**{report['claims']['raw_observation_reached_contextual_proposer']}**",
            "- complete two-action training trace: "
            f"**{report['claims']['complete_two_action_training_trace']}**",
            "- SRA integration claimed: **False**",
            "",
            "The pilot proves the native observation and trajectory contract. "
            "It does not simulate or claim the SRA runtime.",
        ]
    )
    (target / "result.md").write_text(markdown + "\n", encoding="utf-8")
    log_lines = [
        f"root child {index}: action={child.action.args if child.action else None} "
        f"visits={child.visits} value={child.value():.6f} children={len(child.children)}"
        for index, child in enumerate(root.children)
    ]
    log_lines.append("terminal sample=" + json.dumps(asdict(best), sort_keys=True))
    (target / "result.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    report = run_native_interleave_pilot(args.output_dir)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
