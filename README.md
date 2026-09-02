<p align="center">
  <img src="assets/continuum-banner.svg" alt="Continuum — Test-Time Tooling" width="480">
</p>

<div align="center">
  <img src="assets/cherry.png" alt="Continuum project mark, a stylized cherry" width="96" height="96">
</div>

<p align="center">
  <img src="https://img.shields.io/badge/Status-Experimental-febc2e?style=flat-square" alt="Status: Experimental"/>
  <img src="https://img.shields.io/badge/License-GPLv3-8ec5fc?style=flat-square" alt="License: GPLv3"/>
  <img src="https://img.shields.io/badge/Python-3.11%2B-c9a0dc?style=flat-square" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/Model%20Required-No-28c840?style=flat-square" alt="No trained model required"/>
</p>

---


Cherry Continuum is introduced here as the test-time member of the Cherry method family: a model-agnostic search and control regime in which cognition, typed environment action, raw observation, verification, reversible state transition, and later training evidence remain attached to one governed branch graph. The method was previously carried under the working label **Cherry Test-Time Tooling**. That name accurately described the initial engineering move—lifting Cherry's inference operators from token sequences into tool and environment trajectories—but it no longer names the property that makes the resulting system distinct. The load-bearing property is continuity. Search does not terminate when a model emits a tool call, restart when a shell returns output, or flatten a branch into an assistant-authored transcript before reasoning resumes. One branch remains one branch as the type of transition changes.

---


## What Continuum is actually testing

Continuum is not a model wrapper and it does not depend on a trained value model to function. The repository is an inference-time control and search substrate: candidate actions are proposed against a typed contract, filtered before search, executed against reversible state, and judged from observed substrate state rather than model self-report.

The current runtime surface is deliberately split into a few hard responsibilities:

- **Contract before search.** `ContractMDP` schema-conforms candidates and applies the effect gate before MCTS, A*, Best-of-N, or speculative execution can act on them.
- **Real reversible substrates.** `MemoryKV`, SQLite, and filesystem adapters implement snapshot and restore so search can branch against state instead of pretending a token trace is the environment.
- **Verifier isolation.** `PredicateRegistry` reads through `ReadOnlyView`. Reward is computed from world state while the verifier is denied the ability to mutate that state.
- **Strategy independence.** `EnvMCTS`, `EnvAStar`, and `BestOfNActionSampler` share the same action and verification contract. Changing the search strategy does not redefine correctness.
- **Speculative execution as a first-class path.** `Drafter`, `SpeculativeExecutor`, and `AdaptiveGammaController` explore draft-and-overlap execution instead of forcing every candidate through a purely serial commit path.
- **No model required.** A model can be placed behind the proposal surface later, but the contract, substrate, verification, search, and speculation machinery are independently executable now.

### Current experimental surface

| Surface | Current implementation |
|---|---|
| Search | `EnvMCTS` with PUCT and progressive widening, `EnvAStar`, `BestOfNActionSampler` |
| Substrate | Memory KV, SQLite, filesystem |
| Verification | `PredicateRegistry` over `ReadOnlyView` |
| Speculation | `Drafter`, `TemplateDrafter`, `SpeculativeExecutor`, `AdaptiveGammaController` |
| Experiment harness | Four-arm runner plus real filesystem task binding |
| Resident attention | Project-A119 fabric, 8 attention kernels declared, 1 exercised so far |
| Equalizer bridge | `FabricCommitSink` carries equalized transitions into the resident fabric |
| Continuum to Fabric integration | Not started yet |

The repository is therefore testing a narrower and more falsifiable question than "does more inference compute help?": **when an inference-time system is forced to act through a typed, reversible, externally verified environment, which search and speculative strategies actually improve outcomes enough to justify their own cost?**

## Architecture

<p align="center">
  <img src="assets/architecture.svg" alt="Cherry TTT contract surface architecture: GoalSpec through ContractMDP into substrate, search, speculation, and PredicateRegistry" width="1000">
</p>

<details>
<summary><strong>Contract surface · text view</strong></summary>

```text
              ┌───────────────────────────────────────┐
              │              GoalSpec                  │
              │   predicates over real substrate state │
              └────────────────────┬────────────────────┘
                                   │
              ┌────────────────────▼────────────────────┐
              │               ContractMDP                │
              │  schema-conform + effect-gate every       │
              │  candidate BEFORE search ever sees it     │
              └───┬──────────────┬──────────────┬────────┘
                  │              │              │
      ┌───────────▼──┐  ┌────────▼───────┐  ┌───▼─────────────┐
      │  SUBSTRATE    │  │    SEARCH       │  │   SPECULATE      │
      │  MemoryKV     │  │  EnvMCTS (PUCT) │  │  Drafter          │
      │  SQLite       │  │  EnvAStar       │  │  SpeculativeExec  │
      │  FileSystem   │  │  BestOfN        │  │  AdaptiveGamma    │
      │  snapshot /   │  │                 │  │  draft + overlap  │
      │  restore      │  │                 │  │  instead of       │
      │               │  │                 │  │  serial commit    │
      └───────┬───────┘  └────────┬────────┘  └────────┬─────────┘
              │                   │                    │
              └───────────────────┼────────────────────┘
                                   │ execute → snapshot → digest
              ┌────────────────────▼────────────────────┐
              │             PredicateRegistry             │
              │   reads through ReadOnlyView only         │
              │   cannot write world state, even by bug   │
              └────────────────────────────────────────┘
```

</details>

Every candidate action is schema-conformed and effect-gated before search ever sees it. Every transition executes, then snapshots, then digests. Every reward is a verifier reading a `ReadOnlyView`, never the substrate directly. Search strategies and the speculative execution stack operate against that one contract, so swapping strategy never changes what "correct" means.

---

## Quick start

<p align="center">
  <img src="assets/quick-start.svg" alt="Cherry TTT install and smoke terminal" width="1000">
</p>

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cherry-ttt smoke
```

Expected smoke surface:

```json
{
  "core": { "jcs_deterministic": true, "...": "..." },
  "substrates": { "memory_kv": "...", "sqlite": "...", "filesystem": "..." },
  "search": { "algorithms": ["EnvMCTS", "EnvAStar", "BestOfNActionSampler"], "...": "..." },
  "speculate": { "gamma_initial": 5, "drafter_protocol": true },
  "...": "..."
}
```

`exit 0` · every subsystem instantiated, no exception

`cherry-ttt smoke` is an integration smoke check: every subsystem runs through its real code path once. It is not proof that search behaves well on your task. See [Evidence](#evidence) for what has actually been measured.

---

## Component map

| Package | Owns |
|---|---|
| `core` | `ActionCandidate`, `Cost` (a non-collapsing vector: env calls, wall time, tokens, risk), `GoalSpec`, the schema registry, `ContractMDP` |
| `substrate` | `TransactionalSubstrateBase` and three adapters (memory KV, SQLite, filesystem), each snapshot-reversible |
| `verify` | `PredicateRegistry` and the built-in predicates, each reading through a `ReadOnlyView` |
| `search` | `EnvMCTS` (PUCT, progressive widening, parity-gated against captured goldens), `EnvAStar`, `BestOfNActionSampler` |
| `speculate` | `Drafter`/`TemplateDrafter`, `SpeculativeExecutor`, `AdaptiveGammaController` |
| `attention` | `CandidateAttention`, a stateless top-k scorer (see [Fabric](#fabric-project-a119) for the newer, resident alternative) |
| `experiment` | `runner.py`'s four-arm engine and `file_task.py`, the same contract bound to real disk I/O |

---

## Evidence

Real measurements against this codebase. Full raw data in `test-runs/` after running the scripts in `tests/`.

> **CLAIM &#183; MCTS underperforms greedy on the synthetic SQLite benchmark**
>
> **Status:** `CONFIRMED, BUDGET ARTIFACT` &#8203;&#183;&#8203; **Method:** 9 conditions, 3 simulation budgets &#215; 3 seeds, 54 instance-runs per arm
>
> MCTS solved 35.2% of instances against greedy's 100%. Solve rate scaled monotonically with simulation budget (38.9% at 64 sims to 72.2% at 4096 sims, same 18 instances) and `run_arms()`'s fixed `n_actions=6` window caps the visible candidate set below what some instances need, independent of budget. Not a defect in the MCTS port. See `tests/ab_lab.py`, `tests/mcts_budget_probe.py`.

> **CLAIM &#183; Search overhead can dwarf real tool cost**
>
> **Status:** `CONFIRMED` &#8203;&#183;&#8203; **Method:** `FileSystemSubstrate`, real disk I/O, real `Cost.wall_ms` from `time.perf_counter()`, no synthetic latency model
>
> MCTS reached 100% solve rate at 512 simulations. Real committed-plan wall clock was nearly identical to greedy's (about 1ms either way). MCTS's own tree-search overhead averaged about 1.5 seconds per instance. For a cheap, fast real tool, that overhead is the actual cost, not the plan it produces. See `tests/file_lab_probe.py`.

---


## What the measurements already say

The current evidence is intentionally not flattering to every search strategy. That is useful. On the synthetic SQLite benchmark, greedy solved every instance while the fixed-window MCTS configuration solved 35.2% overall. Increasing the simulation budget improved MCTS monotonically, which points at budget and candidate-visibility constraints rather than a broken port.

The filesystem probe exposes a different failure mode: MCTS can reach the same solved outcome while spending orders of magnitude more time deciding what to do than the committed tool plan itself takes to execute. In the measured probe, committed-plan wall time stayed around the millisecond scale while MCTS tree-search overhead averaged around 1.5 seconds per instance.

Those two results define the practical research pressure on Continuum: search quality, candidate visibility, verifier quality, and search overhead have to be measured together. A search algorithm winning in abstract tree quality is not enough if its own inference-time control cost overwhelms the tool call it is trying to improve.

## Fabric (Project-A119)

<p align="center">
  <img src="assets/subsystem-status.svg" alt="Project A119 subsystem status for Equalizer, resident attention fabric, bridge, and Continuum wire-in" width="1120">
</p>

| Surface | State |
|---|---|
| **Model** | Equalizer gates everything (dyson sphere). Attention fabric is what it gates (the sun). Continuum rides on top, not yet wired. |
| `fabric/equalizer` | Symbolic fault gate. Single choke point: `Equalizer.execute()`. Normalize → Capability → fault-check → witness → commit. |
| `fabric/attention` | Resident reactive attention over typed streams. 8 kernels, each honestly labeled by real complexity class. Not a stateless scorer. |
| `fabric/bridge.py` | `FabricCommitSink` feeds every equalized transition into the resident fabric. Verified end to end on real file writes and reads. |
| **Continuum wire-in** | Not started. See `SESSION_NOTES.md` and `HANDOFF.md` for the full gap list. |

<p align="center">
  <img src="assets/verification-coverage.svg" alt="Project A119 verification coverage" width="1000">
</p>

| Verification surface | Current state |
|---|---:|
| Substrates (mem/sql/fs) | **3 / 3** |
| Search arms exercised | **4 / 4** |
| Equalizer sync path | **verified** |
| Equalizer async/reactive path | `untested` |
| Attention kernels exercised | **1 / 8** |
| RepairKernel fired | `never` |
| Continuum → Fabric wire | `not started` |

> Native doctor scan (`somnus-debug doctor`): **94 serious findings, 9 genuinely open after triage.**  
> Full list: `SESSION_NOTES.md` · `SCOPE.md` · `HANDOFF.md`

---

## Status

Experimental research codebase, licensed under [GPLv3](LICENSE). Search strategies, the speculative execution stack, and the fabric layer are all under active investigation. The [Evidence](#evidence) section and the coverage panel above are the current source of truth on what has actually been measured, not this prose.
