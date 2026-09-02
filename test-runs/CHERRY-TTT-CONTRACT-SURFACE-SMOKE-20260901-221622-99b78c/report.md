# CHERRY_TTT CONTRACT SURFACE SMOKE — PASS

**Run ID:** `CHERRY-TTT-CONTRACT-SURFACE-SMOKE-20260901-221622-99b78c`  
**Started:** 2026-09-01T22:16:22.314646+00:00  
**Finished:** 2026-09-01T22:16:22.665637+00:00  
**Elapsed:** 0.350334s  
**Verdict:** **PASS**

## Test Result

cherry_ttt v0.2.0 contract surface validated: CLI smoke_report exercised all three transactional substrates (MemoryKV, SQLite, FileSystem), candidate attention, and density metrics. Independent checks verified canonical identity (JCS/sha256), cost vector non-collapsibility, schema registry (4 namespaces), predicate registry, search algorithms (MCTS/A*/BoN), speculative execution stack, encoder determinism, trajectory collection, interleave context, and value heads. 15 acceptance checks across the full contract surface.

## Acceptance Checks

| Check | Result | Expected | Observed | Detail |
|---|---:|---|---|---|
| package-import-and-version | **PASS** | `"semantic version string (X.Y.Z)"` | `"0.2.0"` |  |
| smoke-report-complete | **PASS** | `["core", "schema", "predicates", "substrates", "attention", "metrics", "search", "speculate", "encode", "collect", "interleave", "value", "experiment", "mdp"]` | `["attention", "collect", "core", "encode", "experiment", "interleave", "mdp", "metrics", "predicates", "schema", "search", "speculate", "substrates", "value"]` |  |
| canonical-identity-key-order-invariant | **PASS** | `"identical canonical() for reordered args"` | `{"id_a": "7201c1adf2bdce8b", "id_b": "7201c1adf2bdce8b", "match": true}` |  |
| jcs-deterministic | **PASS** | `"identical JCS output regardless of key insertion order"` | `"{\"a\":1,\"b\":2}"` |  |
| cost-vector-non-collapsible | **PASS** | `"Cost carries wall_ms, model_tokens, env_calls, risk with no scalar collapse"` | `{"wall_ms": 100.0, "model_tokens": 50, "env_calls": 3, "risk": 0.1}` |  |
| schema-registry-four-namespaces | **PASS** | `["fs", "kv", "lexical", "sql"]` | `["fs", "kv", "lexical", "sql"]` |  |
| predicate-registry-functional | **PASS** | `"PredicateRegistry.resolve is callable"` | `true` |  |
| search-algorithms-instantiable | **PASS** | `"MCTS, A*, BoN all instantiate without substrate"` | `{"mcts": true, "astar": true, "bon": true}` |  |
| speculative-gamma-controller | **PASS** | `"AdaptiveGammaController instantiates from default config"` | `true` |  |
| encoder-deterministic-fixed-dim | **PASS** | `"identical vectors for identical input, shape=(16,)"` | `{"deterministic": true, "shape": [16]}` |  |
| collector-importable | **PASS** | `"TrajectoryCollector is callable"` | `true` |  |
| interleave-importable | **PASS** | `"ReasoningContext and BranchEventLedger are callable"` | `true` |  |
| value-head-callable | **PASS** | `"LinearStateValue.score_vector returns float for zero features"` | `0.0` |  |
| density-metrics-bounded | **PASS** | `"action_density in [0,1], gamma_throughput > 0"` | `{"action_density": 0.75, "wasted_call_rate": 0.5, "gamma_throughput": 0.04668888888888889}` |  |
| cli-entry-point-smoke | **PASS** | `"cherry-ttt smoke produces valid JSON with all 14 subsystem keys"` | `["attention", "collect", "core", "encode", "experiment", "interleave", "mdp", "metrics", "predicates", "schema", "search", "speculate", "substrates", "value"]` |  |

## Measurements

```json
{
  "smoke_elapsed_ms": 8.148,
  "version": "0.2.0",
  "total_checks": 15
}
```

## Structured Evidence

```json
{
  "smoke_report": {
    "core": {
      "canonical_id": "7201c1adf2bdce8b",
      "jcs_deterministic": true,
      "cost_vector_fields": [
        "env_calls",
        "model_tokens",
        "risk",
        "wall_ms"
      ]
    },
    "schema": {
      "tool_count": 13,
      "namespaces": [
        "fs",
        "kv",
        "lexical",
        "sql"
      ]
    },
    "predicates": {
      "registry_type": "PredicateRegistry"
    },
    "substrates": {
      "memory_kv": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "sqlite": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "filesystem": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    },
    "attention": {
      "topk_keys": [
        "a",
        "b"
      ],
      "store_stats": {
        "records": 2,
        "pages": 1,
        "dim": 4
      }
    },
    "metrics": {
      "action_density": 0.75,
      "gamma_throughput": 0.04668888888888889
    },
    "search": {
      "algorithms": [
        "EnvMCTS",
        "EnvAStar",
        "BestOfNActionSampler"
      ],
      "path_to_id_deterministic": true,
      "action_distance_same_tool": 0.6667,
      "action_distance_cross_tool": 1.0
    },
    "speculate": {
      "gamma_initial": 5,
      "gamma_after_55_perfect": 6,
      "template_bind": {
        "k": "test_key",
        "v": 1
      },
      "drafter_protocol": true
    },
    "encode": {
      "dim": 64,
      "json_deterministic": true,
      "token_shape": [
        64
      ]
    },
    "collect": {
      "sample_status": "SOLVED",
      "group_id": "3554d2b8a1e34099"
    },
    "interleave": {
      "branch_id": "4f53cda18c2baa0c",
      "reasoning_context_valid": true
    },
    "value": {
      "linear_score": 36.0,
      "conformal_score": 35.54,
      "calibration_applied": true
    },
    "experiment": {
      "instances_generated": 2,
      "oracle_actions": [
        1,
        2
      ],
      "archive_available": false
    },
    "mdp": {
      "lexical_initial_depth": 0,
      "lexical_ctx": "start"
    }
  },
  "smoke_elapsed_ms": 8.148,
  "canonical_id_a": "7201c1adf2bdce8b",
  "canonical_id_b": "7201c1adf2bdce8b",
  "jcs_output": "{\"a\":1,\"b\":2}",
  "cost_fields": {
    "wall_ms": 100.0,
    "model_tokens": 50,
    "env_calls": 3,
    "risk": 0.1
  },
  "schema_namespaces": [
    "fs",
    "kv",
    "lexical",
    "sql"
  ],
  "predicate_resolve": true,
  "search_configs": {
    "mcts": "EnvMCTSConfig(n_simulations=100, c_puct=1.25, puct_c2=19652.0, temperature=1.0, max_depth=100, max_rollout_depth=50, n_actions=10, use_value_model=True, progressive_widening_alpha=0.5, depth_discount=0.95, reward_value_blend=0.5, serialize_tree=False)",
    "astar": "EnvAStarConfig(max_nodes=200, max_depth=50, n_actions=8, heuristic_weight=1.0, temperature=0.8, use_value_heuristic=True)"
  },
  "gamma_config": "GammaControllerConfig(gamma=5, gamma_min=3, gamma_max=12, adapt_window=50, ema_decay=0.9)",
  "encoder_shape": [
    16
  ],
  "linear_value_output": 0.0,
  "density_metrics": {
    "action_density": 0.75,
    "wasted_call_rate": 0.5,
    "gamma_throughput": 0.04668888888888889
  },
  "cli_output": {
    "attention": {
      "store_stats": {
        "dim": 4,
        "pages": 1,
        "records": 2
      },
      "topk_keys": [
        "a",
        "b"
      ]
    },
    "collect": {
      "group_id": "3554d2b8a1e34099",
      "sample_status": "SOLVED"
    },
    "core": {
      "canonical_id": "7201c1adf2bdce8b",
      "cost_vector_fields": [
        "env_calls",
        "model_tokens",
        "risk",
        "wall_ms"
      ],
      "jcs_deterministic": true
    },
    "encode": {
      "dim": 64,
      "json_deterministic": true,
      "token_shape": [
        64
      ]
    },
    "experiment": {
      "archive_available": false,
      "instances_generated": 2,
      "oracle_actions": [
        1,
        2
      ]
    },
    "interleave": {
      "branch_id": "4f53cda18c2baa0c",
      "reasoning_context_valid": true
    },
    "mdp": {
      "lexical_ctx": "start",
      "lexical_initial_depth": 0
    },
    "metrics": {
      "action_density": 0.75,
      "gamma_throughput": 0.04668888888888889
    },
    "predicates": {
      "registry_type": "PredicateRegistry"
    },
    "schema": {
      "namespaces": [
        "fs",
        "kv",
        "lexical",
        "sql"
      ],
      "tool_count": 13
    },
    "search": {
      "action_distance_cross_tool": 1.0,
      "action_distance_same_tool": 0.6667,
      "algorithms": [
        "EnvMCTS",
        "EnvAStar",
        "BestOfNActionSampler"
      ],
      "path_to_id_deterministic": true
    },
    "speculate": {
      "drafter_protocol": true,
      "gamma_after_55_perfect": 6,
      "gamma_initial": 5,
      "template_bind": {
        "k": "test_key",
        "v": 1
      }
    },
    "substrates": {
      "filesystem": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "memory_kv": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "sqlite": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    },
    "value": {
      "calibration_applied": true,
      "conformal_score": 35.54,
      "linear_score": 36.0
    }
  }
}
```

## Notes

- smoke_report() is the CLI integration surface — it exercises substrates, attention, and metrics through their real code paths.
- Canonical identity is verified by constructing ActionCandidates with reordered args dicts.
- Cost non-collapsibility is checked structurally: Cost must not expose scalar() or collapse() methods.
- The archive substrate (ArchiveEpisodeSubstrate) is not exercised because it requires the optional httpx dependency.

## Artifact Roles

- `manifest.json` — machine-readable run truth and final verdict.
- `report.md` — human/model-readable interpretation surface.
- `test.log` — raw execution/logging surface for diagnosis.

The report may explain the manifest; it must not silently override it.
