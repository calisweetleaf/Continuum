# Session notes — 2026-09-01/02, Project-A119

First-person handoff notes. I'm writing these for whoever picks this up next
(ChatGPT, Codex, or daeron directly) so the next pass doesn't have to
re-derive what I already found. Read `SCOPE.md` alongside this for the
formal engagement-mode declarations.

## What this project actually is (as I now understand it)

`cherry_ttt` (package name; conceptually "Continuum") is test-time tooling —
search (MCTS/A*/BoN) and speculative execution over a real, typed contract
surface (`ContractMDP` + `TransactionalSubstrateBase` + `PredicateRegistry`).
It extends reasoning at inference time, independent of whether a trained
model sits behind it — I confirmed this directly: every search run this
session used `use_value_model=False` and a pure environment-driven reward
(`mdp.reward()`), no model in the loop anywhere.

`fabric/` is daeron's newer layer, described to me as two "lego bags," not
yet fully assembled:

- `fabric/attention/reactive_attention_fabric.py` (2857 lines) — a
  model-independent reactive attention fabric. Its primitive is
  `observe(stream_event)`, not `attention(tokens)`. It is a **resident**
  object — `self._streams`, `self._mailbox`, `self._context` persist across
  calls — not a stateless function. It honestly renames what the historical
  Morpheus artifact called "FlashAttention" (which was actually O(N²) dense
  matmul): 8 real kernels (`exact_dense`, `block_streaming_exact`,
  `linear_kernel`, `sparse_top_k`, `retrieval_first`,
  `hierarchical_multiscale`, `cross_stream`, `temporal_event`), each tagged
  with its true complexity class, one selected per `attend()` call by
  `TopologySelector` based on live `ResourcePressure` (real `/proc` reads).

- `fabric/equalizer/symbolic_fault_equalizer.py` (2089 lines) — a symbolic
  fault-equalization gate, descended from a GPT-4o-era interrupt handler
  (`fabric/equalizer/real_time_interrupt_handler.py`, kept as reference/
  lineage, not itself wired to anything). `Equalizer.execute(ingress) ->
  EqualizedTransition` is the single choke point: normalize -> route to a
  `Capability` (`FileCapability`, `SqliteCapability`) -> symbolic fault
  check -> witness (durable, causal, sqlite-backed proof log) -> commit.
  Every `execute()` call — committed or lawfully absent — unconditionally
  calls `self._commit_sink.accept(transition)` if a `CommitSink` was
  registered at construction.

daeron's framing, which I now hold as the working model: **the equalizer is
the gate around everything (the dyson sphere); the attention fabric is what
it gates (the sun); Continuum/cherry_ttt is the reasoning layer that will
eventually ride on top of the fabric, itself subject to the same gate.**
Nothing — including a trained model deployed in this stack — sees a
malformed input/output, because the equalizer already caught it before
either the fabric or Continuum would see it.

## What I actually built and verified this session

1. **Fixed a real bug.** `fabric/attention/__init__.py` and
   `fabric/equalizer/__init__.py` both pointed their lazy `__getattr__` at
   `tools.native.<domain>.<module>` — neither package could import at all
   (`ModuleNotFoundError: No module named 'tools'`). Corrected to
   `fabric.<domain>.<module>`, which is where the files actually live in
   this repo. Verified both import cleanly after the fix.

2. **Built the first Equalizer -> Fabric wire** (`fabric/bridge.py`,
   `FabricCommitSink`). It's a `CommitSink` (duck-typed against the
   Protocol; no import of it into either production file) that converts
   every `EqualizedTransition` into a real `StreamEvent` and calls
   `fabric.observe()`. I ran it end-to-end for real
   (`tests/bridge_probe.py`): 2 real file writes, 1 real read-back byte-
   verified against what was written, 1 real read of a path that doesn't
   exist (correctly classified `lawful_absent`, not an error — the fabric
   receives truth, not a swallowed exception). All 4 landed in the resident
   fabric; `fabric.drain()` correctly ran `TopologySelector` and picked
   `exact_dense` (small candidate set, legal). Evidence:
   `test-runs/bridge_probe.json`.

3. **Wired `cherry_ttt` to a real substrate for the first time.**
   `runner.py`'s four-arm engine (greedy / BoN(8) / MCTS / MCTS+L3-
   speculative) had only ever run against a synthetic SQLite benchmark with
   a hand-set `LatencyModel` (`env_ms_per_action=8.0` — a fiction, not a
   measurement). I built `cherry_ttt/experiment/file_task.py` to bind the
   same MDP contract to `FileSystemSubstrate` (real disk I/O), using
   `FilePredicate`, which already existed and was fully wired but unused by
   any test. `ContractMDP.transition()` already sums real
   `time.perf_counter()`-measured `Cost.wall_ms` from the substrate — no
   synthetic model needed for this substrate at all.

4. **Ran the actual A/B science, not `test=pass` theater.** My first pass
   (`smoke_report()`/`run_test.py`) only proved objects instantiate — it
   never called `run_arms()`, the actual four-arm engine. Once I did:
   - MCTS solved only 35.2% of instances greedy solved 100% of, on the
     synthetic SQLite benchmark. I diagnosed this properly rather than
     concluding MCTS was broken: solve rate scaled monotonically with
     simulation budget (38.9% -> 44.4% -> 61.1% -> 72.2% at 64/256/1024/4096
     sims — a starved search converging correctly, not a broken one), and
     `run_arms()`'s hardcoded `n_actions=6` caps the candidate window below
     the actual action count some instances need (9 candidates,
     `n_actions=6`) — a structural cap independent of budget. Neither is a
     defect in the MCTS port itself (parity-gated against goldens per its
     own docstring).
   - On the real file substrate, MCTS hit 100% solve rate at 512 sims (this
     benchmark's max depth is lower). More importantly: real committed-plan
     wall-clock was nearly identical between greedy and MCTS (~0.8-1.0ms —
     writing a few small files is fast regardless of strategy), but MCTS's
     own tree-search overhead averaged ~1.5 SECONDS. For a cheap, fast real
     tool, search overhead can dwarf the tool cost it's supposedly
     optimizing — the synthetic benchmark's hand-set `LatencyModel` could
     never have shown this, because it made search cost and tool cost
     artificially proportional by construction.

## What is NOT verified — the honest gap list

I was asked directly whether this is "fully fleshed, production grade, to
the standard" before handoff. My answer was no, and I want that on record
here, not softened:

1. `ArchiveReadClient` (`cherry_ttt/substrate/adapters/archive.py`, 6
   methods flagged by the native doctor as stubs) — I have not confirmed
   whether these are `Protocol`-shaped (correct) or actually incomplete.
2. `KSAProjectReadClient.__init__` (`cherry_ttt/experiment/archive_client.py:147,153`)
   catches bare `BaseException` — broader than `Exception`, catches
   `KeyboardInterrupt`/`SystemExit` too. I have not read this file closely
   enough to know if that's justified.
3. `cherry_ttt/search/bon.py` — `BestOfNActionSampler` ran successfully
   inside `run_arms()` (arm 2, "bon_8") but I never inspected its exception
   handler at line 103 or ran it standalone against the file substrate or
   the fabric.
4. `cherry_ttt/substrate/adapters/sqlite.py` — never read this file at all,
   despite it being the substrate every SQLite-arm result this session
   depended on.
5. Only 1 of the attention fabric's 8 kernels (`exact_dense`) has actually
   executed. The other 7's arithmetic is structurally present, honestly
   labeled, but unexercised by me.
6. `RepairKernel` (equalizer) never fired — every transition in my probe
   was clean. I never forced a genuinely repairable fault through it.
7. The equalizer's async/reactive half — `submit()`, `process_events()`,
   `start()`/`stop()`, the heartbeat thread, preemption — is completely
   untested. I only exercised the synchronous `execute()` path. The
   "reactive" half of "reactive fault equalizer" is unverified.
8. `real_time_interrupt_handler.py` — read once for context (it's the
   lineage source, not itself wired to anything active), not audited
   against the 5 broad-exception sites the doctor flagged in it.
9. Continuum/cherry_ttt is not wired into the Equalizer/Fabric stack at
   all yet. Everything in items 1-8 sits below where that integration would
   need to happen.

Native doctor scan (`somnus-debug doctor scan .`, report at
`production_doctor_report.md`/`.json`): 94 "serious" findings. I triaged
these — most are false positives from an AST heuristic that can't
distinguish a `Protocol` method's `...` body (correct) from actual neglect,
or can't see that an `except OSError: return 0.5` for a missing
`/proc/meminfo` read is a deliberate, documented portability fallback, not
a silent failure. The 9 items above are the ones I could NOT wave off that
way — they're either unread by me or genuinely unexercised.

## What I'd want from the next pass

- Items 1-4 above are read-and-triage work — probably fast for a fresh
  pass with the doctor report in hand.
- Items 5-8 are exercise-work — need real forcing conditions (a genuinely
  malformed action to trip `RepairKernel`, a second concurrent context to
  trip preemption, deliberately large candidate sets to force the other 7
  attention kernels to run).
- Item 9 is the actual next architectural step once 1-8 are closed enough
  to trust the substrate they'd sit on.

I did not restructure this repo into the `tools/native/<domain>/` +
`snapshots/v<x>.<y>/` layout `somnus-code-forge` expects for full
promotion — that would have been a large unrequested refactor of an
already-functioning, differently-laid-out package. `SCOPE.md` documents
this as an open gap rather than silently working around it.
