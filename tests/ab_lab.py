#!/usr/bin/env python3
"""
A/B Experiment Lab — cherry_ttt test-time tooling
==================================================

This is NOT a pass/fail unit test. `cherry_ttt` is test-time tooling: the
inference-time mechanism that searches / samples / speculatively commits
actions against a real environment (ContractMDP over a real SQLite
substrate). The scientific question is not "does the code import" — it is
"how do the four search strategies actually behave against real problem
instances, and how do they compare to each other."

The four arms (cherry_ttt/experiment/runner.py, proposal §10.1):
    1. greedy_react            — take the first legal action, no search
    2. bon_8                   — Best-of-N action sampling, N=8
    3. mcts                    — EnvMCTS (PUCT), serial commit of visit-max plan
    4. mcts_l3_speculative     — same MCTS plan, committed via L3 speculative
                                  execution (draft + overlap instead of
                                  draft-then-verify-then-env serially)

Each instance is a seeded CSV-to-SQLite normalize-and-load task with a
computable oracle (oracle_actions = minimal INSERTs required), so regret
is measurable, not guessed.

This script runs run_arms() — the actual four-arm engine — across multiple
CONDITIONS (varying MCTS simulation budget, varying seeds) and records
what happened. There is no "expected" outcome baked in beyond structural
soundness (non-negative costs, solved <= n_instances, all four arms report
for every condition). Whether MCTS beats greedy, whether the speculative
arm is actually faster under the shared LatencyModel, whether solve rate
holds as instance difficulty scales — that is the empirical finding, and
it is reported as-is, pass or fail be damned.

Artifacts: manifest.json (raw + aggregated data), report.md (human read),
test.log + terminal_output.txt (raw execution).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


# ============================================================================
# Stable harness types (copied per tests/run_test.py's own instruction:
# "Copy this directory into a repository, edit the PROJECT-SPECIFIC TEST
# SURFACE, and keep the artifact machinery boring.")
# ============================================================================

@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""
    expected: Any = None
    observed: Any = None


@dataclass
class TestOutcome:
    summary: str
    checks: list[Check]
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


@dataclass(frozen=True)
class TestContext:
    run_id: str
    project_root: Path
    artifact_dir: Path
    logger: logging.Logger


# ============================================================================
# EXPERIMENT SURFACE
# ============================================================================

TEST_NAME = "CHERRY_TTT A/B LAB — SEARCH-STRATEGY ARMS"

# Three MCTS simulation budgets (a resource A/B axis) crossed with three
# seeds (an instance-variety axis) = 9 conditions, 6 instances each =
# 54 instance-runs per arm, 216 total instance-runs across all four arms.
MCTS_SIM_BUDGETS = (16, 32, 64)
SEEDS = (1, 2, 3)
INSTANCES_PER_CONDITION = 6
ARM_NAMES = ("greedy_react", "bon_8", "mcts", "mcts_l3_speculative")


def execute_test(ctx: TestContext) -> TestOutcome:
    from cherry_ttt.experiment.runner import make_instances, run_arms
    from cherry_ttt.experiment import archive_dependency_available
    from cherry_ttt.speculate.executor import LatencyModel

    latency = LatencyModel(
        draft_ms_per_action=0.5, verify_ms=40.0, env_ms_per_action=8.0, jitter=0.0,
    )

    conditions_raw: list[dict[str, Any]] = []
    checks: list[Check] = []

    ctx.logger.info(
        "Running %d conditions (%d mcts_sim budgets x %d seeds), %d instances each",
        len(MCTS_SIM_BUDGETS) * len(SEEDS), len(MCTS_SIM_BUDGETS), len(SEEDS),
        INSTANCES_PER_CONDITION,
    )

    for sims in MCTS_SIM_BUDGETS:
        for seed in SEEDS:
            instances = make_instances(count=INSTANCES_PER_CONDITION, seed=seed)
            ctx.logger.info(
                "Condition mcts_sims=%d seed=%d: instances=%s oracle_actions=%s",
                sims, seed, [i.name for i in instances],
                [i.oracle_actions for i in instances],
            )

            t_start = time.perf_counter()
            arms = run_arms(instances, mcts_sims=sims, latency=latency, seed=seed)
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0

            ctx.logger.info(
                "Condition mcts_sims=%d seed=%d finished in %.2fms", sims, seed, elapsed_ms,
            )
            for name in ARM_NAMES:
                arm = arms[name]
                ctx.logger.info(
                    "  [%s] solved=%d/%d env_calls=%d committed=%d wall_ms=%.2f regret=%d",
                    name, arm.solved, len(instances), arm.env_calls,
                    arm.committed_actions, arm.wall_ms, arm.regret_actions,
                )

            # ── Structural checks (fail-loud on the harness contract, not
            #    on the empirical result). If run_arms() raised, that
            #    exception propagates out of execute_test() uncaught —
            #    no try/except here.
            n = len(instances)
            for name in ARM_NAMES:
                arm = arms[name]
                cond_label = f"mcts_sims={sims} seed={seed} arm={name}"
                checks.append(Check(
                    f"solved-bounded::{cond_label}",
                    0 <= arm.solved <= n,
                    expected=f"0 <= solved <= {n}",
                    observed=arm.solved,
                ))
                checks.append(Check(
                    f"costs-non-negative::{cond_label}",
                    arm.env_calls >= 0 and arm.wall_ms >= 0.0
                    and arm.committed_actions >= 0 and arm.regret_actions >= 0,
                    expected="env_calls, wall_ms, committed_actions, regret_actions >= 0",
                    observed=asdict(arm) | {"details": len(arm.details)},
                ))

            checks.append(Check(
                f"all-four-arms-present::mcts_sims={sims} seed={seed}",
                set(arms.keys()) == set(ARM_NAMES),
                expected=sorted(ARM_NAMES),
                observed=sorted(arms.keys()),
            ))

            spec_arm = arms["mcts_l3_speculative"]
            checks.append(Check(
                f"speculative-rollbacks-verified::mcts_sims={sims} seed={seed}",
                all(d.get("rollbacks_verified") is True for d in spec_arm.details),
                expected="every mcts_l3_speculative cycle detail reports rollbacks_verified=True",
                observed=[d.get("rollbacks_verified") for d in spec_arm.details],
            ))

            conditions_raw.append({
                "mcts_sims": sims,
                "seed": seed,
                "n_instances": n,
                "instance_oracle_actions": [i.oracle_actions for i in instances],
                "wall_elapsed_ms_lab_measured": round(elapsed_ms, 3),
                "arms": {name: asdict(arms[name]) for name in ARM_NAMES},
            })

    # ── Aggregate across all conditions, per arm — the actual empirical
    #    comparison. This is reported evidence, not an acceptance gate.
    aggregate: dict[str, dict[str, Any]] = {}
    total_instances_run = sum(c["n_instances"] for c in conditions_raw)
    for name in ARM_NAMES:
        total_solved = sum(c["arms"][name]["solved"] for c in conditions_raw)
        total_wall = sum(c["arms"][name]["wall_ms"] for c in conditions_raw)
        total_env_calls = sum(c["arms"][name]["env_calls"] for c in conditions_raw)
        total_committed = sum(c["arms"][name]["committed_actions"] for c in conditions_raw)
        total_regret = sum(c["arms"][name]["regret_actions"] for c in conditions_raw)
        aggregate[name] = {
            "solve_rate": round(total_solved / total_instances_run, 4),
            "solved": total_solved,
            "n": total_instances_run,
            "mean_wall_ms_per_instance": round(total_wall / total_instances_run, 3),
            "mean_env_calls_per_instance": round(total_env_calls / total_instances_run, 3),
            "mean_committed_actions_per_instance": round(
                total_committed / total_instances_run, 3
            ),
            "total_regret_actions": total_regret,
        }

    ctx.logger.info("Aggregate across %d conditions:\n%s",
                     len(conditions_raw), json.dumps(aggregate, indent=2))

    # Empirical comparison: does the L3 speculative arm actually reduce
    # wall-clock relative to serial MCTS commit, at matched solve rate?
    # This is the §10.1 pre-registered claim under test — report the number,
    # do not assert it must hold.
    mcts_wall = aggregate["mcts"]["mean_wall_ms_per_instance"]
    spec_wall = aggregate["mcts_l3_speculative"]["mean_wall_ms_per_instance"]
    wall_delta_pct = round(100.0 * (mcts_wall - spec_wall) / mcts_wall, 2) if mcts_wall else None

    finding = {
        "mcts_serial_mean_wall_ms": mcts_wall,
        "mcts_l3_speculative_mean_wall_ms": spec_wall,
        "l3_wall_reduction_pct": wall_delta_pct,
        "mcts_solve_rate": aggregate["mcts"]["solve_rate"],
        "mcts_l3_speculative_solve_rate": aggregate["mcts_l3_speculative"]["solve_rate"],
        "solve_rate_matched": (
            aggregate["mcts"]["solve_rate"] == aggregate["mcts_l3_speculative"]["solve_rate"]
        ),
    }
    ctx.logger.info("§10.1 speculative-overlap finding: %s", json.dumps(finding, indent=2))

    archive_available = archive_dependency_available()
    checks.append(Check(
        "archive-dependency-reported",
        isinstance(archive_available, bool),
        expected="archive_dependency_available() returns a bool (informational, not gated)",
        observed=archive_available,
    ))

    return TestOutcome(
        summary=(
            f"cherry_ttt four-arm search-strategy A/B lab: {len(conditions_raw)} conditions "
            f"({len(MCTS_SIM_BUDGETS)} MCTS sim budgets x {len(SEEDS)} seeds), "
            f"{total_instances_run} instance-runs per arm. Aggregate solve rates — "
            f"greedy_react={aggregate['greedy_react']['solve_rate']}, "
            f"bon_8={aggregate['bon_8']['solve_rate']}, "
            f"mcts={aggregate['mcts']['solve_rate']}, "
            f"mcts_l3_speculative={aggregate['mcts_l3_speculative']['solve_rate']}. "
            f"L3 speculative wall-clock vs serial MCTS: "
            f"{wall_delta_pct}% (positive = faster) at "
            f"{'matched' if finding['solve_rate_matched'] else 'DIVERGENT'} solve rate."
        ),
        checks=checks,
        metrics={
            "n_conditions": len(conditions_raw),
            "total_instance_runs_per_arm": total_instances_run,
            "mcts_sim_budgets": list(MCTS_SIM_BUDGETS),
            "seeds": list(SEEDS),
        },
        evidence={
            "conditions": conditions_raw,
            "aggregate_by_arm": aggregate,
            "l3_speculative_overlap_finding": finding,
            "archive_dependency_available": archive_available,
        },
        notes=[
            "Acceptance checks in this lab validate STRUCTURAL soundness of the "
            "experiment run (non-negative costs, all four arms reporting, bounded "
            "solve counts) — they do not gate on which arm 'wins'. The empirical "
            "comparison lives in evidence.aggregate_by_arm and "
            "evidence.l3_speculative_overlap_finding.",
            "Instances are seeded CSV->SQLite normalize-and-load tasks with a "
            "computable oracle (oracle_actions); regret_actions = "
            "max(0, committed - oracle) per instance, summed across the condition.",
            "wall_ms for all arms is composed under a shared synthetic LatencyModel "
            "(draft=0.5ms/action, verify=40ms, env=8ms/action, jitter=0) so the "
            "arm-4-vs-arm-3 wall comparison is reproducible without a network in "
            "the loop, per runner.py's own design note.",
            "If run_arms() or make_instances() raised, this test recorded that as "
            "an execution failure (see manifest 'error'), not a swallowed exception "
            "— there is no try/except around the experiment loop.",
        ],
    )


# ============================================================================
# Harness internals (unmodified from tests/run_test.py's contract)
# ============================================================================

SCHEMA_VERSION = "1.0"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(value: str) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in value).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or "test"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(v) for v in value]
    return str(value)


def _setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("cherry_ttt.ab_lab")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
    )

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def _default_output_root(project_root: Path) -> Path:
    override = os.environ.get("AB_LAB_OUTPUT_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return project_root / "test-runs"


def _build_manifest(
    *,
    ctx: TestContext,
    started_at: datetime,
    finished_at: datetime,
    elapsed_s: float,
    outcome: TestOutcome | None,
    error: dict[str, Any] | None,
) -> dict[str, Any]:
    verdict = "PASS" if outcome is not None and outcome.passed and error is None else "FAIL"

    checks = []
    if outcome is not None:
        checks = [_json_safe(asdict(check)) for check in outcome.checks]

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": ctx.run_id,
        "test_name": TEST_NAME,
        "verdict": verdict,
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": finished_at.isoformat(),
        "elapsed_s": round(elapsed_s, 6),
        "environment": {
            "python": sys.version,
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "cwd": str(Path.cwd()),
            "project_root": str(ctx.project_root),
            "runner": str(Path(__file__).resolve()),
        },
        "acceptance": {
            "total_checks": len(checks),
            "passed_checks": sum(1 for c in checks if c.get("passed") is True),
            "failed_checks": sum(1 for c in checks if c.get("passed") is False),
            "checks": checks,
        },
        "result": {
            "summary": outcome.summary if outcome else None,
            "metrics": _json_safe(outcome.metrics) if outcome else {},
            "evidence": _json_safe(outcome.evidence) if outcome else {},
            "notes": _json_safe(outcome.notes) if outcome else [],
        },
        "error": _json_safe(error),
        "artifacts": {
            "manifest": "manifest.json",
            "report": "report.md",
            "log": "test.log",
        },
    }


def _render_report(manifest: Mapping[str, Any]) -> str:
    verdict = manifest["verdict"]
    acceptance = manifest["acceptance"]
    result = manifest["result"]
    error = manifest.get("error")

    lines = [
        f"# {manifest['test_name']} — {verdict}",
        "",
        f"**Run ID:** `{manifest['run_id']}`  ",
        f"**Started:** {manifest['started_at_utc']}  ",
        f"**Finished:** {manifest['finished_at_utc']}  ",
        f"**Elapsed:** {manifest['elapsed_s']:.6f}s  ",
        f"**Verdict:** **{verdict}**  (structural soundness only — see Findings below)",
        "",
        "## Summary",
        "",
        result.get("summary") or "No summary was returned.",
        "",
    ]

    evidence = result.get("evidence") or {}
    finding = evidence.get("l3_speculative_overlap_finding")
    if finding:
        lines += ["## §10.1 Empirical Finding — L3 Speculative Overlap", "", "```json",
                   json.dumps(finding, indent=2, default=str), "```", ""]

    aggregate = evidence.get("aggregate_by_arm")
    if aggregate:
        lines += ["## Aggregate Results By Arm", "", "| Arm | Solve Rate | Mean wall_ms/instance | Mean env_calls/instance | Total regret |",
                   "|---|---:|---:|---:|---:|"]
        for name, row in aggregate.items():
            lines.append(
                f"| {name} | {row['solve_rate']} | {row['mean_wall_ms_per_instance']} | "
                f"{row['mean_env_calls_per_instance']} | {row['total_regret_actions']} |"
            )
        lines.append("")

    checks = acceptance.get("checks", [])
    n_pass = acceptance["passed_checks"]
    n_fail = acceptance["failed_checks"]
    lines += [f"## Structural Acceptance Checks ({n_pass} pass / {n_fail} fail of {len(checks)})",
               ""]
    failed = [c for c in checks if not c.get("passed")]
    if failed:
        lines += ["**Failed checks:**", "",
                   "| Check | Expected | Observed |", "|---|---|---|"]
        for check in failed:
            expected = json.dumps(check.get("expected"), default=str)
            observed = json.dumps(check.get("observed"), default=str)
            lines.append(f"| {check.get('name')} | `{expected}` | `{observed}` |")
        lines.append("")
    else:
        lines.append("All structural checks passed. (Full per-condition check list is in manifest.json.)")
        lines.append("")

    metrics = result.get("metrics") or {}
    lines += ["## Run Parameters", ""]
    if metrics:
        lines.append("```json")
        lines.append(json.dumps(metrics, indent=2, default=str))
        lines.append("```")

    notes = result.get("notes") or []
    if notes:
        lines += ["", "## Notes", ""]
        for note in notes:
            lines.append(f"- {note}")

    if error:
        lines += [
            "", "## Execution Failure", "",
            f"**Type:** `{error.get('type', 'Unknown')}`", "",
            f"**Message:** {error.get('message', '')}", "",
            "```text", error.get("traceback", ""), "```",
        ]

    lines += [
        "", "## Artifact Roles", "",
        "- `manifest.json` — machine-readable run truth, full per-condition raw arm data.",
        "- `report.md` — human-readable interpretation surface.",
        "- `test.log` / `terminal_output.txt` — raw execution/logging surface.",
        "",
        "This is an experiment, not a gate. PASS means the four-arm engine ran to "
        "completion with structurally sound outputs across all conditions — it does "
        "not mean any particular arm 'won'. Read the Empirical Finding and Aggregate "
        "Results sections for the actual science.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    project_root = Path(
        os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parent.parent)
    ).expanduser().resolve()

    started_at = _utc_now()
    run_id = (
        f"{_slug(TEST_NAME).upper()}-"
        f"{started_at.strftime('%Y%m%d-%H%M%S')}-"
        f"{uuid.uuid4().hex[:6]}"
    )

    artifact_dir = _default_output_root(project_root) / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)

    log_file = artifact_dir / "test.log"
    manifest_file = artifact_dir / "manifest.json"
    report_file = artifact_dir / "report.md"

    logger = _setup_logger(log_file)
    ctx = TestContext(
        run_id=run_id, project_root=project_root, artifact_dir=artifact_dir, logger=logger,
    )

    logger.info("=" * 72)
    logger.info("%s", TEST_NAME)
    logger.info("Run ID: %s", run_id)
    logger.info("Artifacts: %s", artifact_dir)
    logger.info("=" * 72)

    t0 = time.perf_counter()
    outcome: TestOutcome | None = None
    error: dict[str, Any] | None = None

    try:
        outcome = execute_test(ctx)
        if not isinstance(outcome, TestOutcome):
            raise TypeError(
                f"execute_test() must return TestOutcome, got {type(outcome).__name__}"
            )
        if not outcome.checks:
            raise ValueError(
                "execute_test() returned no acceptance checks; an empty test cannot PASS."
            )
        for check in outcome.checks:
            if not check.passed:
                logger.info("[FAIL] %s%s", check.name, f" — {check.detail}" if check.detail else "")
    except Exception as exc:
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        logger.exception("Experiment execution failed")

    elapsed_s = time.perf_counter() - t0
    finished_at = _utc_now()

    manifest = _build_manifest(
        ctx=ctx, started_at=started_at, finished_at=finished_at,
        elapsed_s=elapsed_s, outcome=outcome, error=error,
    )

    manifest_file.write_text(
        json.dumps(manifest, indent=2, sort_keys=False, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    report_file.write_text(_render_report(manifest), encoding="utf-8")

    verdict = manifest["verdict"]
    logger.info("=" * 72)
    logger.info("FINAL VERDICT: %s", verdict)
    if outcome is not None:
        logger.info("SUMMARY: %s", outcome.summary)
    logger.info("manifest.json: %s", manifest_file)
    logger.info("report.md:     %s", report_file)
    logger.info("test.log:      %s", log_file)
    logger.info("=" * 72)

    print()
    print(json.dumps({
        "run_id": run_id, "test_name": TEST_NAME, "verdict": verdict,
        "elapsed_s": manifest["elapsed_s"],
        "artifacts": {
            "manifest": str(manifest_file), "report": str(report_file), "log": str(log_file),
        },
    }, indent=2))

    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
