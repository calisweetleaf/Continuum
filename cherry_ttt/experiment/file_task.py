"""
File-tool task — real-substrate analogue of NormalizeLoadInstance (runner.py).

Purpose: runner.py's four-arm engine only ever ran against SQLiteSubstrate
    under a synthetic LatencyModel. This module wires the same MDP contract
    (ContractMDP + schema + predicates) to FileSystemSubstrate — a real
    directory on real disk — so search-strategy comparison happens over
    real fs.write latency (measured via time.perf_counter() in
    substrate/adapters/fs.py, not modeled), the first "real tool" test
    for cherry_ttt as test-time tooling sitting between a reasoner and an
    actual tool surface.
Integrated: 2026-09-01
Purpose (design note): the action space is deliberately a flat list of
    fs.write candidates, one per target file — mirroring CsvProposer's
    discipline that search strategies are compared on SEARCH quality, not
    proposal quality. The oracle (oracle_actions = file count) is
    computable because each file needs exactly one write to satisfy its
    file_predicate.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

from ..core.contract_mdp import ContractMDP, ContractMDPConfig
from ..core.mdp import State
from ..core.schema import default_registry
from ..core.types import ActionCandidate, GoalSpec, PredicateRef
from ..substrate.adapters.fs import FileSystemSubstrate
from ..verify.predicates import default_predicate_registry

_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


@dataclass(frozen=True)
class FileTaskInstance:
    """One mini task: a set of target files, each needing distinct content."""

    name: str
    files: dict[str, str]      # relative path -> required content
    oracle_actions: int        # = len(files); one fs.write each is minimal


def make_file_instances(count: int, seed: int) -> list[FileTaskInstance]:
    """Seeded instances; each target file's content is a unique token so
    file_predicate(contains=...) cannot be satisfied by any other file."""
    rng = random.Random(seed)
    instances: list[FileTaskInstance] = []
    for index in range(count):
        n_files = rng.randint(1, 6)
        files: dict[str, str] = {}
        for f in range(n_files):
            token = "".join(rng.choice(_ALPHABET) for _ in range(12))
            files[f"item_{f}.txt"] = f"payload-{token}"
        instances.append(FileTaskInstance(
            name=f"file-{seed}-{index}", files=files, oracle_actions=n_files))
    return instances


class FileProposer:
    """Proposer over an instance: one fs.write candidate per target file,
    novelty-ordered (least-attempted first) — matches CsvProposer's
    discipline so arms differ by SEARCH, not by proposal quality."""

    def __init__(self, instance: FileTaskInstance) -> None:
        self.actions: list[ActionCandidate] = []
        for path, content in sorted(instance.files.items()):
            self.actions.append(ActionCandidate("fs.write", {"path": path, "content": content}))

    def propose(self, s: State, n: int) -> list[tuple[ActionCandidate, float]]:
        """Return up to n candidates, least-attempted action first.

        Args:
            s: Current MDP state; s.ctx encodes prior action labels.
            n: Maximum candidates to return.

        Returns:
            (action, uniform_prior) pairs, novelty-ordered.
        """
        prior = 1.0 / max(1, len(self.actions))
        ordered = sorted(
            enumerate(self.actions),
            key=lambda pair: (s.ctx.count(
                f"{pair[1].tool_id}:{pair[1].canonical()[:8]}"), pair[0]),
        )
        return [(a, prior) for _i, a in ordered[:n]]


def file_goal(instance: FileTaskInstance) -> GoalSpec:
    """Build one file_predicate per target file, each requiring exists+contains.

    Args:
        instance: The seeded file-task instance whose files must all be written.

    Returns:
        A GoalSpec whose predicates are satisfied only when every target
        file exists with its required content.
    """
    predicates = tuple(
        PredicateRef("file_predicate", {"path": path, "exists": True, "contains": content})
        for path, content in sorted(instance.files.items())
    )
    return GoalSpec(predicates=predicates, max_per_action=1)


def file_mdp(instance: FileTaskInstance, root: str | Path) -> ContractMDP:
    """Bind a real FileSystemSubstrate at root to this instance's proposer/schema.

    Args:
        instance: The seeded file-task instance.
        root: Real directory the substrate operates on; created if missing.

    Returns:
        A ContractMDP ready for initial_state()/legal_actions()/transition().
    """
    schema = default_registry()
    substrate = FileSystemSubstrate(root, substrate_id=f"fs-{instance.name}")
    return ContractMDP(
        substrate, FileProposer(instance), schema,
        default_predicate_registry(schema), ContractMDPConfig(max_depth=24),
    )


__all__ = [
    "FileTaskInstance", "FileProposer",
    "make_file_instances", "file_goal", "file_mdp",
]
