# CHERRY_TTT A/B LAB — SEARCH-STRATEGY ARMS — PASS

**Run ID:** `CHERRY-TTT-A-B-LAB-SEARCH-STRATEGY-ARMS-20260901-222424-1716c2`  
**Started:** 2026-09-01T22:24:24.841962+00:00  
**Finished:** 2026-09-01T22:24:28.162075+00:00  
**Elapsed:** 3.319335s  
**Verdict:** **PASS**  (structural soundness only — see Findings below)

## Summary

cherry_ttt four-arm search-strategy A/B lab: 9 conditions (3 MCTS sim budgets x 3 seeds), 54 instance-runs per arm. Aggregate solve rates — greedy_react=1.0, bon_8=1.0, mcts=0.3519, mcts_l3_speculative=0.3519. L3 speculative wall-clock vs serial MCTS: 70.66% (positive = faster) at matched solve rate.

## §10.1 Empirical Finding — L3 Speculative Overlap

```json
{
  "mcts_serial_mean_wall_ms": 141.333,
  "mcts_l3_speculative_mean_wall_ms": 41.472,
  "l3_wall_reduction_pct": 70.66,
  "mcts_solve_rate": 0.3519,
  "mcts_l3_speculative_solve_rate": 0.3519,
  "solve_rate_matched": true
}
```

## Aggregate Results By Arm

| Arm | Solve Rate | Mean wall_ms/instance | Mean env_calls/instance | Total regret |
|---|---:|---:|---:|---:|
| greedy_react | 1.0 | 237.333 | 9.889 | 78 |
| bon_8 | 1.0 | 514.222 | 67.667 | 78 |
| mcts | 0.3519 | 141.333 | 80.556 | 19 |
| mcts_l3_speculative | 0.3519 | 41.472 | 80.556 | 19 |

## Structural Acceptance Checks (91 pass / 0 fail of 91)

All structural checks passed. (Full per-condition check list is in manifest.json.)

## Run Parameters

```json
{
  "n_conditions": 9,
  "total_instance_runs_per_arm": 54,
  "mcts_sim_budgets": [
    16,
    32,
    64
  ],
  "seeds": [
    1,
    2,
    3
  ]
}
```

## Notes

- Acceptance checks in this lab validate STRUCTURAL soundness of the experiment run (non-negative costs, all four arms reporting, bounded solve counts) — they do not gate on which arm 'wins'. The empirical comparison lives in evidence.aggregate_by_arm and evidence.l3_speculative_overlap_finding.
- Instances are seeded CSV->SQLite normalize-and-load tasks with a computable oracle (oracle_actions); regret_actions = max(0, committed - oracle) per instance, summed across the condition.
- wall_ms for all arms is composed under a shared synthetic LatencyModel (draft=0.5ms/action, verify=40ms, env=8ms/action, jitter=0) so the arm-4-vs-arm-3 wall comparison is reproducible without a network in the loop, per runner.py's own design note.
- If run_arms() or make_instances() raised, this test recorded that as an execution failure (see manifest 'error'), not a swallowed exception — there is no try/except around the experiment loop.

## Artifact Roles

- `manifest.json` — machine-readable run truth, full per-condition raw arm data.
- `report.md` — human-readable interpretation surface.
- `test.log` / `terminal_output.txt` — raw execution/logging surface.

This is an experiment, not a gate. PASS means the four-arm engine ran to completion with structurally sound outputs across all conditions — it does not mean any particular arm 'won'. Read the Empirical Finding and Aggregate Results sections for the actual science.
