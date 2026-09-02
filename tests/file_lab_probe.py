#!/usr/bin/env python3
"""
File-tool lab probe — first real-substrate run of cherry_ttt search over
FileSystemSubstrate (real disk I/O, real measured Cost.wall_ms, no
synthetic LatencyModel). Exploratory: greedy vs EnvMCTS on the same
seeded file-task instances used to sanity-check cherry_ttt/experiment/
file_task.py end-to-end before formalizing a full arm harness.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from cherry_ttt.core.types import TerminalStatus
from cherry_ttt.experiment.file_task import file_goal, file_mdp, make_file_instances
from cherry_ttt.search.mcts import EnvMCTS, EnvMCTSConfig

SEEDS = (1, 2, 3, 4, 5)
INSTANCES_PER_SEED = 6
MCTS_SIMS = 512  # sized off the earlier probe: solves depth<=6 reliably


def run_greedy(instance, tmp) -> dict:
    mdp = file_mdp(instance, tmp)
    goal = file_goal(instance)
    state = mdp.initial_state(goal, instance.name)
    steps = 0
    wall_ms = 0.0
    while mdp.is_terminal(state) is not TerminalStatus.SOLVED and steps < 24:
        legal = mdp.legal_actions(state, 1)
        if not legal:
            break
        state, _obs, cost = mdp.transition(state, legal[0][0])
        wall_ms += cost.wall_ms
        steps += 1
    solved = mdp.is_terminal(state) is TerminalStatus.SOLVED
    return {"solved": solved, "steps": steps, "wall_ms_real": round(wall_ms, 4)}


def run_mcts(instance, tmp, sims) -> dict:
    mdp = file_mdp(instance, tmp)
    goal = file_goal(instance)
    cfg = EnvMCTSConfig(n_simulations=sims, n_actions=6, max_rollout_depth=8,
                         use_value_model=False)
    mcts = EnvMCTS(mdp, cfg, goal=goal)
    out = mcts.generate(instance.name,
                         reward_fn=lambda s: mdp.reward(s, mdp.trajectory_of(s)))
    node = out["root"]
    plan = []
    while node.children:
        node = max(node.children, key=lambda c: c.visits)
        plan.append(node.action)
    # Serially commit the visit-max plan on a fresh substrate instance to
    # measure real wall_ms for the actually-committed path (search-time
    # exploration cost is separate and reported too).
    mdp2 = file_mdp(instance, tmp)
    state2 = mdp2.initial_state(goal, instance.name)
    wall_ms = 0.0
    for action in plan:
        state2, _obs, cost = mdp2.transition(state2, action)
        wall_ms += cost.wall_ms
    solved = mdp2.is_terminal(state2) is TerminalStatus.SOLVED
    return {
        "solved": solved, "plan_len": len(plan),
        "wall_ms_real_committed": round(wall_ms, 4),
        "root_children": len(out["root"].children),
    }


def main() -> None:
    records = []
    with tempfile.TemporaryDirectory(prefix="cherry_ttt_file_lab_") as tmp_root:
        for seed in SEEDS:
            instances = make_file_instances(count=INSTANCES_PER_SEED, seed=seed)
            for instance in instances:
                # Each instance gets its own subdirectory so files/predicates
                # from different instances never collide on real disk.
                tmp = Path(tmp_root) / instance.name
                tmp.mkdir(parents=True, exist_ok=True)

                t0 = time.perf_counter()
                g = run_greedy(instance, tmp)
                g["wall_ms_wallclock"] = round((time.perf_counter() - t0) * 1000, 4)

                # Fresh subdir for MCTS so greedy's committed files don't
                # pre-satisfy MCTS's predicates.
                tmp_m = Path(tmp_root) / (instance.name + "-mcts")
                tmp_m.mkdir(parents=True, exist_ok=True)
                t0 = time.perf_counter()
                m = run_mcts(instance, tmp_m, MCTS_SIMS)
                m["wall_ms_wallclock"] = round((time.perf_counter() - t0) * 1000, 4)

                rec = {
                    "instance": instance.name,
                    "oracle_actions": instance.oracle_actions,
                    "greedy": g,
                    "mcts": m,
                }
                records.append(rec)
                print(f"{instance.name} oracle={instance.oracle_actions}  "
                      f"greedy: solved={g['solved']} steps={g['steps']} "
                      f"real_wall_ms={g['wall_ms_real']} wallclock_ms={g['wall_ms_wallclock']}  |  "
                      f"mcts(sims={MCTS_SIMS}): solved={m['solved']} plan_len={m['plan_len']} "
                      f"real_wall_ms={m['wall_ms_real_committed']} wallclock_ms={m['wall_ms_wallclock']}")

    n = len(records)
    greedy_solved = sum(r["greedy"]["solved"] for r in records)
    mcts_solved = sum(r["mcts"]["solved"] for r in records)
    mean_greedy_real_wall = sum(r["greedy"]["wall_ms_real"] for r in records) / n
    mean_mcts_real_wall = sum(r["mcts"]["wall_ms_real_committed"] for r in records) / n
    mean_mcts_search_wallclock = sum(r["mcts"]["wall_ms_wallclock"] for r in records) / n

    summary = {
        "n_instances": n,
        "greedy_solve_rate": round(greedy_solved / n, 3),
        "mcts_solve_rate": round(mcts_solved / n, 3),
        "mean_greedy_real_committed_wall_ms": round(mean_greedy_real_wall, 4),
        "mean_mcts_real_committed_wall_ms": round(mean_mcts_real_wall, 4),
        "mean_mcts_full_search_wallclock_ms": round(mean_mcts_search_wallclock, 4),
        "note": "real_committed_wall_ms is the real Cost.wall_ms ContractMDP "
                "accumulated from actual fs.write calls on the committed plan "
                "(FileSystemSubstrate._ok/_err time.perf_counter()) -- no "
                "synthetic LatencyModel anywhere in this run. wallclock_ms is "
                "the outer time.perf_counter() around the whole call including "
                "MCTS's own tree-search overhead (Python object churn), which "
                "the SQLite-arm ab_lab could not distinguish because it used a "
                "modeled cost, not a measured one.",
    }
    print()
    print("=" * 72)
    print("SUMMARY")
    print(json.dumps(summary, indent=2))

    out_path = Path(__file__).resolve().parent.parent / "test-runs" / "file_lab_probe.json"
    out_path.write_text(json.dumps({"records": records, "summary": summary}, indent=2), encoding="utf-8")
    print(f"\nfull evidence: {out_path}")


if __name__ == "__main__":
    main()
