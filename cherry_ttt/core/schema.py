"""
Schema registry Σ — the typed argument contracts of the action space.

Source: written for cherry_ttt P3; generalizes the format_checker role
    from inference_optimizations.py BestOfNSampler (proposal §6.2:
    "format_checker -> schema validity (hard filter)").
Integrated: 2026-07-06
Purpose: The Σ in the contract surface C = <S_env, A, O, E, Σ, c>
    (proposal §1.1). Declares per-tool argument schemas, validates
    candidates (the BoN hard filter and the schema_validity predicate
    both consume this), and implements the D3 boundary rule: floats are
    rounded to declared schema precision HERE, before construction —
    canonical() itself stays exact (core/types.py docstring contract).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ValidationError
from .types import ActionCandidate

_TYPE_CHECKS: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "int": int,
    "float": (int, float),  # ints are acceptable floats; bool excluded below
    "bool": bool,
    "list": list,
    "dict": dict,
    "any": object,
}


@dataclass(frozen=True, slots=True)
class ArgSpec:
    """One argument's declared contract."""

    type_name: str
    required: bool = True
    float_precision: int | None = None  # D3: decimal places for rounding


@dataclass
class ToolSchema:
    """Declared schema for one tool_id."""

    tool_id: str
    args: dict[str, ArgSpec] = field(default_factory=dict)
    allow_extra: bool = False


class SchemaRegistry:
    """Σ: tool_id -> ToolSchema, with validate and D3 conform.

    Source: cherry_ttt P3 (module docstring carries provenance).
    """

    def __init__(self) -> None:
        self._schemas: dict[str, ToolSchema] = {}

    def declare(self, schema: ToolSchema) -> None:
        """Register a tool schema; redeclaration is a validation error
        (schemas are contracts, not preferences)."""
        if schema.tool_id in self._schemas:
            raise ValidationError(f"schema for {schema.tool_id!r} already declared")
        self._schemas[schema.tool_id] = schema

    def known(self, tool_id: str) -> bool:
        """True when tool_id has a declared schema."""
        return tool_id in self._schemas

    def violations(self, a: ActionCandidate) -> list[str]:
        """Return all schema violations for a candidate (empty = valid).

        Returns messages rather than a bool so the BoN filter and the
        schema_validity predicate can report *why* a candidate failed.
        """
        schema = self._schemas.get(a.tool_id)
        if schema is None:
            return [f"unknown tool {a.tool_id!r}"]
        problems: list[str] = []
        for name, spec in schema.args.items():
            if name not in a.args:
                if spec.required:
                    problems.append(f"missing required arg {name!r}")
                continue
            value = a.args[name]
            expected = _TYPE_CHECKS.get(spec.type_name, object)
            if spec.type_name in ("int", "float") and isinstance(value, bool):
                problems.append(f"arg {name!r}: bool is not {spec.type_name}")
            elif not isinstance(value, expected):
                problems.append(
                    f"arg {name!r}: {type(value).__name__} is not {spec.type_name}"
                )
        if not schema.allow_extra:
            extra = set(a.args) - set(schema.args)
            if extra:
                problems.append(f"undeclared args {sorted(extra)}")
        return problems

    def is_valid(self, a: ActionCandidate) -> bool:
        """The BoN hard-filter boolean."""
        return not self.violations(a)

    def conform(self, a: ActionCandidate) -> ActionCandidate:
        """Validate and apply D3 float rounding at the schema boundary.

        Args:
            a: A candidate whose args may carry unrounded floats.

        Returns:
            A new ActionCandidate with floats rounded to each arg's
            declared precision — the canonical identity of the returned
            candidate is the identity the rest of the system sees.

        Raises:
            ValidationError: If the candidate violates its schema.
        """
        problems = self.violations(a)
        if problems:
            raise ValidationError(
                f"candidate {a.tool_id!r} violates schema: {'; '.join(problems)}"
            )
        schema = self._schemas[a.tool_id]
        rounded: dict[str, Any] = dict(a.args)
        for name, spec in schema.args.items():
            if (
                spec.float_precision is not None
                and name in rounded
                and isinstance(rounded[name], float)
            ):
                rounded[name] = round(rounded[name], spec.float_precision)
        return ActionCandidate(a.tool_id, rounded)


def default_registry() -> SchemaRegistry:
    """Σ for built-in adapters (memory_kv, sqlite, filesystem, lexical)."""
    registry = SchemaRegistry()
    registry.declare(ToolSchema("kv.get", {"k": ArgSpec("str")}))
    registry.declare(ToolSchema("kv.put", {"k": ArgSpec("str"), "v": ArgSpec("any")}))
    registry.declare(ToolSchema("kv.delete", {"k": ArgSpec("str")}))
    registry.declare(
        ToolSchema(
            "kv.increment",
            {"k": ArgSpec("str"), "by": ArgSpec("float", required=False, float_precision=6)},
        )
    )
    registry.declare(ToolSchema("sql.exec", {"statement": ArgSpec("str")}))
    registry.declare(ToolSchema("fs.read", {"path": ArgSpec("str")}))
    registry.declare(ToolSchema("fs.exists", {"path": ArgSpec("str")}))
    registry.declare(ToolSchema("fs.list", {"path": ArgSpec("str")}))
    registry.declare(ToolSchema("fs.write", {"path": ArgSpec("str"), "content": ArgSpec("str")}))
    registry.declare(ToolSchema("fs.append", {"path": ArgSpec("str"), "content": ArgSpec("str")}))
    registry.declare(ToolSchema("fs.delete", {"path": ArgSpec("str")}))
    registry.declare(ToolSchema("fs.mkdir", {"path": ArgSpec("str")}))
    registry.declare(ToolSchema("lexical.append", {"text": ArgSpec("str")}))
    return registry


__all__ = ["ArgSpec", "SchemaRegistry", "ToolSchema", "default_registry"]
