"""
CandidateAttention — exact numpy cross-attention over candidate stores.

This is the phase-1/P6 reference implementation: Q attends over
persistent candidate K/V with structured biases, returning both soft
weighted outputs and exact hard top-k routes.  It is intentionally
kernel-free and CPU-friendly; GPU kernels must implement the same
semantics and pass equivalence tests before being used.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..core.errors import ValidationError
from .bias import BiasQuery, build_structured_bias
from .paged_store import PagedCandidateStore


@dataclass(frozen=True, slots=True)
class AttentionResult:
    """Soft and hard routing outputs."""

    soft: np.ndarray
    probabilities: np.ndarray
    topk_indices: np.ndarray
    topk_scores: np.ndarray
    topk_keys: tuple[tuple[str, ...], ...]


class CandidateAttention:
    """Exact cross-attention and top-k over a PagedCandidateStore."""

    def __init__(self, dim: int) -> None:
        if dim <= 0:
            raise ValidationError("CandidateAttention.dim must be positive")
        self.dim = dim

    def attend(
        self,
        queries: np.ndarray,
        store: PagedCandidateStore,
        bias_queries: list[BiasQuery] | None = None,
        top_k: int = 8,
    ) -> AttentionResult:
        """Compute softmax(QK^T/sqrt(d)+B)V and exact top-k indices.

        Args:
            queries: Array of shape (M, d) or (d,).
            store: Candidate store containing K/V vectors.
            bias_queries: Optional query constraints.  If omitted, no
                structural masks or soft penalties are applied.
            top_k: Number of hard routes per query.

        Raises:
            ValidationError: On dimension mismatch, empty store, or fully
                masked queries.
        """
        q = np.asarray(queries, dtype=np.float32)
        if q.ndim == 1:
            q = q.reshape(1, -1)
        if q.ndim != 2 or q.shape[1] != self.dim:
            raise ValidationError(f"queries shape {q.shape} != (M, {self.dim})")
        k, v, metas, keys = store.matrices()
        if k.shape[0] == 0:
            raise ValidationError("CandidateAttention requires at least one candidate")
        if k.shape[1] != self.dim or v.shape[1] != self.dim:
            raise ValidationError("store dimension mismatch")
        scores = (q @ k.T) / math.sqrt(self.dim)
        if bias_queries is not None:
            if len(bias_queries) != q.shape[0]:
                raise ValidationError(
                    f"bias query count {len(bias_queries)} != query count {q.shape[0]}"
                )
            scores = scores + build_structured_bias(bias_queries, metas)
        probs = _row_softmax(scores)
        soft = probs @ v
        if top_k <= 0:
            raise ValidationError("top_k must be positive")
        n = min(top_k, k.shape[0])
        idx = np.argpartition(-scores, kth=n - 1, axis=1)[:, :n]
        row_order = np.arange(scores.shape[0])[:, None]
        order = np.argsort(-scores[row_order, idx], axis=1)
        idx = idx[row_order, order]
        top_scores = scores[row_order, idx]
        top_keys = tuple(tuple(keys[int(j)] for j in row) for row in idx)
        return AttentionResult(
            soft=soft.astype(np.float32, copy=False),
            probabilities=probs.astype(np.float32, copy=False),
            topk_indices=idx.astype(np.int64, copy=False),
            topk_scores=top_scores.astype(np.float32, copy=False),
            topk_keys=top_keys,
        )


def _row_softmax(scores: np.ndarray) -> np.ndarray:
    """Stable row softmax with hard error on fully masked rows."""
    maxes = np.max(scores, axis=1, keepdims=True)
    if not np.all(np.isfinite(maxes)):
        bad = np.where(~np.isfinite(maxes[:, 0]))[0].tolist()
        raise ValidationError(f"fully masked attention rows: {bad}")
    exp = np.exp(scores - maxes)
    denom = np.sum(exp, axis=1, keepdims=True)
    return exp / denom


def streaming_topk(scores: np.ndarray, k: int) -> np.ndarray:
    """Exact reference streaming-top-k interface.

    The implementation materializes scores because it is the reference
    CPU path.  Kernel implementations must return identical indices.
    """
    arr = np.asarray(scores, dtype=np.float32)
    if arr.ndim != 2:
        raise ValidationError("streaming_topk expects a 2D score matrix")
    if k <= 0:
        raise ValidationError("k must be positive")
    n = min(k, arr.shape[1])
    idx = np.argpartition(-arr, kth=n - 1, axis=1)[:, :n]
    row = np.arange(arr.shape[0])[:, None]
    order = np.argsort(-arr[row, idx], axis=1)
    return idx[row, order].astype(np.int64, copy=False)


__all__ = ["AttentionResult", "CandidateAttention", "streaming_topk"]
