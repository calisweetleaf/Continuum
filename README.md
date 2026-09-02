![Continuum project mark, a stylized cherry](assets/cherry.png)

# Continuum

Test-time tooling: environment-trajectory search on a typed contract surface.

Continuum (package name `cherry_ttt`) extends reasoning at inference time by
searching, sampling, and speculatively committing actions against a real,
typed contract surface, not a language model's own token stream. Every
search in this repository runs with no trained value model in the loop:
reward comes from a verifier reading real substrate state through a
read-only view. A model can sit behind this later; nothing here requires
one to exist first.

## What it actually does

A goal is a set of predicates (`GoalSpec`). An environment is a
`TransactionalSubstrateBase` that can snapshot and restore itself
(`MemoryKVSubstrate`, `SQLiteSubstrate`, `FileSystemSubstrate`). A
`ContractMDP` binds a substrate, an action schema, and a predicate registry
into one MDP: every candidate action is schema-conformed and effect-gated
before search ever sees it, every transition executes, then snapshots, then
digests, and every reward is a verifier reading a `ReadOnlyView`, never the
substrate directly. Search strategies
(`EnvMCTS`, `EnvAStar`, `BestOfNActionSampler`) and a speculative execution
stack (`Drafter`, `SpeculativeExecutor`, `AdaptiveGammaController`) operate
against that one contract, so a strategy swap never changes what "correct"
means.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cherry-ttt smoke
```

`cherry-ttt smoke` exercises every subsystem in one pass, core types,
schema, all three substrates, attention, search, speculative execution,
encoders, and value heads, and prints a structured JSON receipt. A clean
run ends with every field populated and no exception. This is an
integration smoke check, not proof that search behaves well on your task;
see [Evidence](#evidence) for what has actually been measured.

## Component map

| Package | Owns |
| --- | --- |
| `core` | `ActionCandidate`, `Cost` (an explicit non-collapsing vector: env calls, wall time, tokens, risk), `GoalSpec`, the schema registry, and `ContractMDP` itself |
| `substrate` | `TransactionalSubstrateBase` and the three adapters (memory KV, SQLite, filesystem), each snapshot-reversible |
| `verify` | `PredicateRegistry` and the built-in predicates (`db_predicate`, `kv_predicate`, `file_predicate`, `state_digest_equals`, `schema_validity`), each reading through a `ReadOnlyView` so a predicate cannot write world state even by bug |
| `search` | `EnvMCTS` (PUCT, progressive widening, parity-gated against captured goldens), `EnvAStar`, `BestOfNActionSampler` |
| `speculate` | `Drafter`/`TemplateDrafter`, `SpeculativeExecutor`, `AdaptiveGammaController` for draft-and-overlap commit instead of serial draft-then-verify-then-execute |
| `attention` | `CandidateAttention`, a stateless top-k scorer over action candidates (see [Fabric status](#fabric-status-experimental-not-wired) for the newer, resident alternative) |
| `experiment` | `runner.py`'s four-arm engine (`greedy_react`, `bon_8`, `mcts`, `mcts_l3_speculative`) and `file_task.py`, the same contract bound to real disk I/O |

## Evidence

These are real measurements taken against this codebase, not projected
numbers. Full raw data and methodology are in `test-runs/` after running the
scripts in `tests/`.

> **MCTS underperforms greedy on the synthetic SQLite benchmark, and that is
> a budget artifact, not a defect.** Across 9 conditions (3 simulation
> budgets times 3 seeds, 54 instance-runs per arm), MCTS solved 35.2% of
> instances against greedy's 100%. Solve rate scaled monotonically with
> simulation budget (38.9% at 64 sims to 72.2% at 4096 sims on the same 18
> instances), and `run_arms()`'s fixed `n_actions=6` window caps the visible
> candidate set below what some instances need independent of budget. See
> `tests/ab_lab.py` and `tests/mcts_budget_probe.py`.

> **Search overhead can dwarf real tool cost.** Against `FileSystemSubstrate`
> (real disk I/O, real `Cost.wall_ms` from `time.perf_counter()`, no
> synthetic latency model), MCTS reached 100% solve rate at 512 simulations,
> and the real committed-plan wall clock was nearly identical to greedy's
> (about 1ms either way, writing a few small files is fast regardless of
> strategy). MCTS's own tree-search overhead averaged about 1.5 seconds per
> instance. For a cheap, fast real tool, that overhead is the actual cost,
> not the plan it produces. See `tests/file_lab_probe.py`.

## Fabric status (experimental, not wired)

`fabric/` is a separate, newer layer: a resident, model-independent
reactive attention system (`fabric/attention/`) and a symbolic fault
equalizer that gates every action before it reaches anything else
(`fabric/equalizer/`). `fabric/bridge.py` wires one seam between them
(`Equalizer.execute()` feeds a resident attention fabric through a
`CommitSink`), verified end to end on real file writes and reads. Neither
piece is yet connected to `cherry_ttt`'s own search or substrate layer.
Read `SESSION_NOTES.md` and `HANDOFF.md` for the current state and the
open list before relying on this layer for anything.

## Status

Experimental research codebase, licensed under [GPLv3](LICENSE). Search
strategies, the speculative execution stack, and the fabric layer are all
under active investigation; the [Evidence](#evidence) section above is the
most current source of truth on what has actually been measured, not this
prose.
