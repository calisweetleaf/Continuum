"""
PagedCandidateStore — PagedKVCache reinterpreted for candidate embeddings.

The implementation is CPU/numpy and exact.  It focuses on the two
highest-value phase-1 properties: persistent candidate embeddings and
page-level namespace grouping for cheap block skipping.  Custom kernels
are intentionally absent until the proposal's P6 kill-switch justifies
them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from ..core.errors import ValidationError
from .bias import CandidateMeta


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    """One stored candidate embedding."""

    key: str
    embedding: np.ndarray
    value: np.ndarray
    meta: CandidateMeta
    payload: Any = None


@dataclass(frozen=True, slots=True)
class StoreStats:
    records: int
    pages: int
    dim: int
    evictions: int


class PagedCandidateStore:
    """Append-mostly embedding store grouped into fixed-size pages."""

    def __init__(self, dim: int, page_size: int = 256, max_pages: int | None = None) -> None:
        if dim <= 0:
            raise ValidationError("PagedCandidateStore.dim must be positive")
        if page_size <= 0:
            raise ValidationError("PagedCandidateStore.page_size must be positive")
        self.dim = dim
        self.page_size = page_size
        self.max_pages = max_pages
        self._records: list[CandidateRecord] = []
        self._by_key: dict[str, int] = {}
        self._evictions = 0

    def add(
        self,
        key: str,
        embedding: np.ndarray,
        *,
        value: np.ndarray | None = None,
        meta: CandidateMeta,
        payload: Any = None,
    ) -> None:
        """Add or replace one candidate."""
        emb = self._vector(embedding, "embedding")
        val = self._vector(value if value is not None else embedding, "value")
        if key in self._by_key:
            self._records[self._by_key[key]] = CandidateRecord(key, emb, val, meta, payload)
            return
        if self.max_pages is not None and self.pages >= self.max_pages:
            self._evict_one_page()
        self._by_key[key] = len(self._records)
        self._records.append(CandidateRecord(key, emb, val, meta, payload))

    def extend(self, records: Iterable[CandidateRecord]) -> None:
        for record in records:
            self.add(
                record.key,
                record.embedding,
                value=record.value,
                meta=record.meta,
                payload=record.payload,
            )

    @property
    def pages(self) -> int:
        return (len(self._records) + self.page_size - 1) // self.page_size

    def records(self) -> tuple[CandidateRecord, ...]:
        return tuple(self._records)

    def matrices(self) -> tuple[np.ndarray, np.ndarray, list[CandidateMeta], list[str]]:
        """Return K, V, metadata and keys in store order."""
        if not self._records:
            return (
                np.zeros((0, self.dim), dtype=np.float32),
                np.zeros((0, self.dim), dtype=np.float32),
                [],
                [],
            )
        keys = [r.key for r in self._records]
        metas = [r.meta for r in self._records]
        k = np.stack([r.embedding for r in self._records]).astype(np.float32, copy=False)
        v = np.stack([r.value for r in self._records]).astype(np.float32, copy=False)
        return k, v, metas, keys

    def invalidate_namespace(self, namespace: str) -> int:
        """Remove all candidates in a namespace; returns removed count."""
        before = len(self._records)
        self._records = [r for r in self._records if r.meta.namespace != namespace]
        self._reindex()
        removed = before - len(self._records)
        self._evictions += removed
        return removed

    def stats(self) -> StoreStats:
        return StoreStats(
            records=len(self._records),
            pages=self.pages,
            dim=self.dim,
            evictions=self._evictions,
        )

    def _vector(self, arr: np.ndarray, name: str) -> np.ndarray:
        vec = np.asarray(arr, dtype=np.float32)
        if vec.shape != (self.dim,):
            raise ValidationError(f"{name} shape {vec.shape} != ({self.dim},)")
        return vec.copy()

    def _evict_one_page(self) -> None:
        if not self._records:
            return
        del self._records[: self.page_size]
        self._evictions += self.page_size
        self._reindex()

    def _reindex(self) -> None:
        self._by_key = {record.key: index for index, record in enumerate(self._records)}


__all__ = ["CandidateRecord", "PagedCandidateStore", "StoreStats"]
