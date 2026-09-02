<div align="center">

<svg width="480" height="200" viewBox="0 0 480 200" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Continuum banner">
  <defs>
    <linearGradient id="ctmGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:#c9a0dc;stop-opacity:1" />
      <stop offset="50%" style="stop-color:#8ec5fc;stop-opacity:1" />
      <stop offset="100%" style="stop-color:#c9a0dc;stop-opacity:1" />
    </linearGradient>
    <filter id="ctmGlow">
      <feGaussianBlur stdDeviation="3" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>
  <rect width="480" height="200" fill="#0d1117" rx="14"/>
  <rect x="1" y="1" width="478" height="198" fill="none" stroke="#1e2430" stroke-width="1" rx="14"/>
  <text x="240" y="94" font-family="'Courier New', monospace" font-size="46" fill="url(#ctmGrad)" text-anchor="middle" filter="url(#ctmGlow)" font-weight="bold" letter-spacing="4">CONTINUUM</text>
  <text x="240" y="122" font-family="'Courier New', monospace" font-size="12" fill="#8b949e" text-anchor="middle" letter-spacing="2">TEST-TIME TOOLING</text>
  <text x="240" y="146" font-family="'Courier New', monospace" font-size="10.5" fill="#484f58" text-anchor="middle">MCTS &#183; A* &#183; BoN  |  Speculative Execution  |  Typed Contract Surface</text>
  <line x1="150" y1="160" x2="330" y2="160" stroke="#21262d" stroke-width="1"/>
  <text x="240" y="178" font-family="'Courier New', monospace" font-size="9" fill="#2d333b" text-anchor="middle" letter-spacing="5">CHERRY_TTT &#183; PROJECT-A119</text>
</svg>

</div>

<div align="center">
  <img src="assets/cherry.png" alt="Continuum project mark, a stylized cherry" width="96" height="96" style="border-radius:50%;object-fit:cover;">
</div>

<p align="center">
  <img src="https://img.shields.io/badge/Status-Experimental-febc2e?style=flat-square" alt="Status: Experimental"/>
  <img src="https://img.shields.io/badge/License-GPLv3-8ec5fc?style=flat-square" alt="License: GPLv3"/>
  <img src="https://img.shields.io/badge/Python-3.11%2B-c9a0dc?style=flat-square" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/Model%20Required-No-28c840?style=flat-square" alt="No trained model required"/>
</p>

---

<style>
.t{background:#141414;border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,.55),0 0 0 1px #2a2a2a;margin:22px 0;font-family:'Menlo','Monaco','Cascadia Code','Courier New',monospace;overflow:hidden}
.t-hdr{background:#252525;padding:11px 16px;display:flex;align-items:center;border-bottom:1px solid #1e1e1e;user-select:none}
.t-btn{width:13px;height:13px;border-radius:50%;margin-right:8px;flex-shrink:0}
.t-btn.r{background:#ff5f57;box-shadow:0 0 4px #ff5f5780}.t-btn.y{background:#febc2e;box-shadow:0 0 4px #febc2e80}.t-btn.g{background:#28c840;box-shadow:0 0 4px #28c84080}
.t-title{color:#888;font-size:12.5px;margin-left:10px;letter-spacing:.4px}
.t-tag{margin-left:auto;background:#1e1e1e;border:1px solid #333;color:#555;font-size:10px;padding:2px 8px;border-radius:3px;letter-spacing:1px;text-transform:uppercase}
.t-body{padding:18px 20px;font-size:13px;line-height:1.65;color:#d4d4d4;overflow-x:auto}
.t-diag{white-space:pre;font-size:12.5px;line-height:1.35;color:#c9d1d9;overflow-x:auto;padding:18px 20px;margin:0;font-family:'Menlo','Monaco','Cascadia Code','Courier New',monospace}
.t-code{white-space:pre;font-size:12.5px;line-height:1.6;overflow-x:auto;padding:18px 20px;margin:0;color:#d4d4d4;font-family:'Menlo','Monaco','Cascadia Code','Courier New',monospace}
.prompt{color:#28c840}.dim{color:#777}.info{color:#8ec5fc}.ok{color:#28c840}.warn{color:#febc2e}.err{color:#ff5f57}.accent{color:#c9a0dc}
.out{margin-bottom:3px}.cmd{margin-bottom:2px}
.t-sep{height:1px;background:#1e1e1e;margin:8px 0}
.mon-row{display:grid;grid-template-columns:190px 1fr 84px;align-items:center;margin-bottom:6px;font-size:12.5px}
.mon-label{color:#888}.mon-bar{background:#1e1e1e;height:9px;border-radius:2px;overflow:hidden;position:relative}
.mon-fill{height:100%;border-radius:2px}.mon-val{color:#c9d1d9;text-align:right;font-size:11px}
.intel{background:#0d1117;border:1px solid #21262d;border-radius:8px;margin:20px 0;font-family:'Menlo','Monaco','Courier New',monospace;overflow:hidden}
.intel-hdr{background:#161b22;padding:10px 18px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #21262d}
.intel-cls{color:#8ec5fc;font-size:10px;font-weight:bold;letter-spacing:4px}.intel-id{color:#484f58;font-size:11px}
.intel-body{padding:12px 18px}
.intel-row{display:flex;padding:7px 0;border-bottom:1px solid #0d1117;font-size:13px;line-height:1.5}
.intel-row:last-child{border-bottom:none}
.intel-lbl{color:#8b949e;min-width:150px;flex-shrink:0}.intel-val{color:#c9d1d9}
</style>

Continuum (package name `cherry_ttt`) extends reasoning at inference time by searching, sampling, and speculatively committing actions against a real, typed contract surface, not a language model's own token stream. Every search in this repository runs with no trained value model in the loop: reward comes from a verifier reading real substrate state through a read-only view. A model can sit behind this later; nothing here requires one to exist first.

---

## Architecture

<div class="t">
  <div class="t-hdr">
    <div class="t-btn r"></div><div class="t-btn y"></div><div class="t-btn g"></div>
    <span class="t-title">cherry_ttt &#183; contract surface</span>
    <span class="t-tag">DIAGRAM</span>
  </div>
<pre class="t-diag">
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
              │   reads through ReadOnlyView only —       │
              │   cannot write world state, even by bug   │
              └────────────────────────────────────────┘
</pre>
</div>

Every candidate action is schema-conformed and effect-gated before search ever sees it. Every transition executes, then snapshots, then digests. Every reward is a verifier reading a `ReadOnlyView`, never the substrate directly. Search strategies and the speculative execution stack operate against that one contract, so swapping strategy never changes what "correct" means.

---

## Quick start

<div class="t">
  <div class="t-hdr">
    <div class="t-btn r"></div><div class="t-btn y"></div><div class="t-btn g"></div>
    <span class="t-title">cherry_ttt &#183; install and smoke</span>
    <span class="t-tag">VERIFIED</span>
  </div>
  <div class="t-body">
    <div class="cmd"><span class="prompt">~/continuum$</span> python3 -m venv .venv && source .venv/bin/activate</div>
    <div class="cmd"><span class="prompt">~/continuum$</span> pip install -e .</div>
    <div class="cmd"><span class="prompt">~/continuum$</span> cherry-ttt smoke</div>
    <div class="t-sep"></div>
    <div class="out dim">{</div>
    <div class="out dim">&nbsp;&nbsp;"core": { "jcs_deterministic": true, ... },</div>
    <div class="out dim">&nbsp;&nbsp;"substrates": { "memory_kv": "...", "sqlite": "...", "filesystem": "..." },</div>
    <div class="out dim">&nbsp;&nbsp;"search": { "algorithms": ["EnvMCTS", "EnvAStar", "BestOfNActionSampler"], ... },</div>
    <div class="out dim">&nbsp;&nbsp;"speculate": { "gamma_initial": 5, "drafter_protocol": true },</div>
    <div class="out dim">&nbsp;&nbsp;...</div>
    <div class="out dim">}</div>
    <div class="out ok" style="margin-top:8px;">exit 0 &#183; every subsystem instantiated, no exception</div>
  </div>
</div>

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

## Fabric (Project-A119)

<div class="intel">
  <div class="intel-hdr">
    <span class="intel-cls">SUBSYSTEM STATUS</span>
    <span class="intel-id">fabric/ &#183; 2026-09-02</span>
  </div>
  <div class="intel-body">
    <div class="intel-row">
      <span class="intel-lbl">Model</span>
      <span class="intel-val">Equalizer gates everything (dyson sphere). Attention fabric is what it gates (the sun). Continuum rides on top, not yet wired.</span>
    </div>
    <div class="intel-row">
      <span class="intel-lbl">fabric/equalizer</span>
      <span class="intel-val">Symbolic fault gate. Single choke point: <code>Equalizer.execute()</code>. Normalize &#8594; Capability &#8594; fault-check &#8594; witness &#8594; commit.</span>
    </div>
    <div class="intel-row">
      <span class="intel-lbl">fabric/attention</span>
      <span class="intel-val">Resident reactive attention over typed streams. 8 kernels, each honestly labeled by real complexity class. Not a stateless scorer.</span>
    </div>
    <div class="intel-row">
      <span class="intel-lbl">fabric/bridge.py</span>
      <span class="intel-val">The one wire built so far: <code>FabricCommitSink</code> feeds every equalized transition into the resident fabric. Verified end to end on real file writes and reads.</span>
    </div>
    <div class="intel-row">
      <span class="intel-lbl">Continuum wire-in</span>
      <span class="intel-val">Not started. See <code>SESSION_NOTES.md</code> and <code>HANDOFF.md</code> for the full gap list.</span>
    </div>
  </div>
</div>

<div class="t">
  <div class="t-hdr">
    <div class="t-btn r"></div><div class="t-btn y"></div><div class="t-btn g"></div>
    <span class="t-title">project-a119 &#183; verification coverage</span>
    <span class="t-tag">HONEST</span>
  </div>
  <div class="t-body">
    <div class="mon-row">
      <span class="mon-label">Substrates (mem/sql/fs)</span>
      <span class="mon-bar"><span class="mon-fill" style="width:100%;background:linear-gradient(90deg,#28c840,#8ec5fc)"></span></span>
      <span class="mon-val ok">3 / 3</span>
    </div>
    <div class="mon-row">
      <span class="mon-label">Search arms exercised</span>
      <span class="mon-bar"><span class="mon-fill" style="width:100%;background:linear-gradient(90deg,#28c840,#8ec5fc)"></span></span>
      <span class="mon-val ok">4 / 4</span>
    </div>
    <div class="mon-row">
      <span class="mon-label">Equalizer sync path</span>
      <span class="mon-bar"><span class="mon-fill" style="width:100%;background:linear-gradient(90deg,#28c840,#8ec5fc)"></span></span>
      <span class="mon-val ok">verified</span>
    </div>
    <div class="mon-row">
      <span class="mon-label">Equalizer async/reactive path</span>
      <span class="mon-bar"><span class="mon-fill" style="width:3%;background:linear-gradient(90deg,#ff5f57,#febc2e)"></span></span>
      <span class="mon-val err">untested</span>
    </div>
    <div class="mon-row">
      <span class="mon-label">Attention kernels exercised</span>
      <span class="mon-bar"><span class="mon-fill" style="width:12.5%;background:linear-gradient(90deg,#ff5f57,#febc2e)"></span></span>
      <span class="mon-val warn">1 / 8</span>
    </div>
    <div class="mon-row">
      <span class="mon-label">RepairKernel fired</span>
      <span class="mon-bar"><span class="mon-fill" style="width:3%;background:linear-gradient(90deg,#ff5f57,#febc2e)"></span></span>
      <span class="mon-val err">never</span>
    </div>
    <div class="mon-row">
      <span class="mon-label">Continuum &#8594; Fabric wire</span>
      <span class="mon-bar"><span class="mon-fill" style="width:3%;background:linear-gradient(90deg,#ff5f57,#febc2e)"></span></span>
      <span class="mon-val err">not started</span>
    </div>
    <div class="t-sep"></div>
    <div class="out dim">Native doctor scan (somnus-debug doctor): 94 serious findings, 9 genuinely open after triage.</div>
    <div class="out dim">Full list: SESSION_NOTES.md &#183; SCOPE.md &#183; HANDOFF.md</div>
  </div>
</div>

---

## Status

Experimental research codebase, licensed under [GPLv3](LICENSE). Search strategies, the speculative execution stack, and the fabric layer are all under active investigation. The [Evidence](#evidence) section and the coverage panel above are the current source of truth on what has actually been measured, not this prose.
