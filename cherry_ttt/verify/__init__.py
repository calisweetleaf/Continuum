"""Verifier layer: predicates, registry, read-only substrate views (§9.7)."""

from __future__ import annotations

from .predicates import (
    SATISFIED,
    Predicate,
    PredicateRegistry,
    ReadOnlyView,
    default_predicate_registry,
)

__all__ = ["SATISFIED", "Predicate", "PredicateRegistry", "ReadOnlyView",
           "default_predicate_registry"]
