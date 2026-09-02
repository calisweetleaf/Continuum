#!/usr/bin/env python3
"""
MCTS budget probe — diagnosing the ab_lab finding that EnvMCTS solves only
35.2% of instances that greedy_react solves 100% of.

Question: is EnvMCTS *wrong*, or is it *starved* — i.e. does run_arms()'s
fixed config (n_actions=6, max_rollout_depth=8, mcts_sims in {16,32,64})
simply not carry enough simulations to grow the search tree to the depth
these instances need (oracle_actions up to 7, meaning a solving plan is
7-8 sequential commits deep) before progressive widening has even
constructed that branch?

Method: for each of the 18 instances from the ab_lab run (seeds 1-3,
6 instances each), run EnvMCTS directly at increasing simulation budgets
(64, 256, 1024, 4096) and record: did it solve, plan length, root child
count, max tree depth actually reached, total nodes. Also record greedy's
plan length on the same instance for comparison. This is exploratory
diagnosis — no PASS/FAIL gate, just raw evidence written to JSON + printed.

No mocks. Real EnvMCTS, real SQLite substrate, real ContractMDP, same
CsvProposer/goal construction as runner.py so results are apples-to-apples
with the ab_lab run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from cherry_ttt.experiment.runner import (
    CsvProposer, NormalizeLoadInstance, _goal, _mdp, make_instances,
)
from cherry_ttt.core.types import TerminalStatus
from cherry_ttt.search.mcts import EnvMCTS, EnvMCTSConfig

SIM_BUDGETS = (64, 256, 1024, 4096)
SEEDS = (1, 2, 3)
INSTANCES_PER_SEED = 6


def greedy_plan_length(instance: NormalizeLoadInstance) -> tuple[int, bool]:
    mdp = _mdp(instance)
    goal = _goal(instance)
    state = mdp.initial_state(goal, instance.name)
    steps = 0
    while mdp.is_terminal(state) is not TerminalStatus.SOLVED and steps < 24:
        legal = mdp.legal_actions(state, 1)
        if not legal:
            break
        state, _obs, _cost = mdp.transition(state, legal[0][0])
        steps += 1
    return steps, mdp.is_terminal(state) is TerminalStatus.SOLVED


def mcts_probe(instance: NormalizeLoadInstance, sims: int) -> dict:
    mdp = _mdp(instance)
    goal = _goal(instance)
    cfg = EnvMCTSConfig(n_simulations=sims, n_actions=6, max_rollout_depth=8,
                         use_value_model=False)
    mcts = EnvMCTS(mdp, cfg, goal=goal)
    t0 = time.perf_counter()
    out = mcts.generate(instance.name,
                         reward_fn=lambda s: mdp.reward(s, mdp.trajectory_of(s)))
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    root = out["root"]

    node = root
    plan_len = 0
    while node.children:
        node = max(node.children, key=lambda c: c.visits)
        plan_len += 1

    # Walk the whole tree once to get total node count and max depth reached.
    total_nodes = 0
    max_depth = 0
    stack = [(root, 0)]
    while stack:
        n, depth = stack.pop()
        total_nodes += 1
        max_depth = max(max_depth, depth)
        for c in n.children:
            stack.append((c, depth + 1))

    return {
        "sims": sims,
        "solved": bool(node.is_terminal),
        "plan_len": plan_len,
        "root_children": len(root.children),
        "root_visits": root.visits,
        "tree_total_nodes": total_nodes,
        "tree_max_depth_reached": max_depth,
        "elapsed_ms": round(elapsed_ms, 3),
    }


def main() -> None:
    records = []
    for seed in SEEDS:
        instances = make_instances(count=INSTANCES_PER_SEED, seed=seed)
        for instance in instances:
            g_len, g_solved = greedy_plan_length(instance)
            entry = {
                "instance": instance.name,
                "oracle_actions": instance.oracle_actions,
                "n_action_candidates": len(CsvProposer(instance).actions),
                "greedy_plan_len": g_len,
                "greedy_solved": g_solved,
                "mcts_by_budget": {},
            }
            print(f"{instance.name}  oracle={instance.oracle_actions}  "
                  f"n_actions={entry['n_action_candidates']}  "
                  f"greedy: solved={g_solved} plan_len={g_len}")
            for sims in SIM_BUDGETS:
                r = mcts_probe(instance, sims)
                entry["mcts_by_budget"][str(sims)] = r
                print(f"    sims={sims:5d}  solved={r['solved']!s:5}  "
                      f"plan_len={r['plan_len']:2d}  "
                      f"root_children={r['root_children']}  "
                      f"tree_nodes={r['tree_total_nodes']:4d}  "
                      f"tree_max_depth={r['tree_max_depth_reached']:2d}  "
                      f"({r['elapsed_ms']:.1f}ms)")
            records.append(entry)

    # Aggregate: solve rate by sim budget, and solve rate by oracle_actions
    # bucket (does difficulty predict failure the way budget starvation would?).
    by_budget = {str(s): {"solved": 0, "n": 0} for s in SIM_BUDGETS}
    by_oracle = {}
    for entry in records:
        for sims in SIM_BUDGETS:
            r = entry["mcts_by_budget"][str(sims)]
            by_budget[str(sims)]["n"] += 1
            by_budget[str(sims)]["solved"] += int(r["solved"])
        bucket = entry["oracle_actions"]
        by_oracle.setdefault(bucket, {"solved_at_1024": 0, "n": 0})
        by_oracle[bucket]["n"] += 1
        by_oracle[bucket]["solved_at_1024"] += int(
            entry["mcts_by_budget"]["1024"]["solved"]
        )

    summary = {
        "solve_rate_by_sim_budget": {
            k: round(v["solved"] / v["n"], 3) for k, v in by_budget.items()
        },
        "solve_rate_by_oracle_actions_at_1024_sims": {
            str(k): round(v["solved_at_1024"] / v["n"], 3)
            for k, v in sorted(by_oracle.items())
        },
        "greedy_solve_rate": round(
            sum(e["greedy_solved"] for e in records) / len(records), 3
        ),
    }

    print()
    print("=" * 72)
    print("SUMMARY")
    print(json.dumps(summary, indent=2))

    out_path = Path(__file__).resolve().parent.parent / "test-runs" / "mcts_budget_probe.json"
    out_path.write_text(json.dumps({"records": records, "summary": summary}, indent=2), encoding="utf-8")
    print(f"\nfull evidence: {out_path}")


if __name__ == "__main__":
    main()
