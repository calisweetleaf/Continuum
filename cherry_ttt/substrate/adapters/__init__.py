"""Substrate adapters — the only non-agnostic code in cherry_ttt (proposal section 7)."""

from __future__ import annotations

from .archive import (
    ArchiveChannel,
    ArchiveEpisodeSubstrate,
    ArchiveEvidence,
    ArchiveEvidenceResult,
    ArchiveReadClient,
    EpisodeEvidenceLedger,
)
from .fs import FileSystemSubstrate
from .memory_kv import MemoryKVSubstrate
from .sqlite import SQLiteSubstrate

__all__ = [
    "ArchiveChannel",
    "ArchiveEpisodeSubstrate",
    "ArchiveEvidence",
    "ArchiveEvidenceResult",
    "ArchiveReadClient",
    "EpisodeEvidenceLedger",
    "FileSystemSubstrate",
    "MemoryKVSubstrate",
    "SQLiteSubstrate",
]
