"""
Schema structural encoding.

Tool schemas are encoded by argument structure and declared effect
features rather than learned tool-id embeddings.  This preserves the
cross-harness transfer discipline in the proposal.
"""

from __future__ import annotations

import numpy as np

from ..core.schema import SchemaRegistry, ToolSchema
from ..core.types import EffectClass
from .hashing import HashingEncoder


def encode_tool_schema(
    schema: ToolSchema,
    effect: EffectClass | None = None,
    dim: int = 128,
) -> np.ndarray:
    """Encode one ToolSchema into a normalized numpy vector."""
    tokens = [
        f"arity:{len(schema.args)}",
        f"allow_extra:{schema.allow_extra}",
    ]
    if effect is not None:
        tokens.append(f"effect:{effect.name}")
    for name, spec in sorted(schema.args.items()):
        tokens.append(f"arg:{name}:type:{spec.type_name}:required:{spec.required}")
        if spec.float_precision is not None:
            tokens.append(f"arg:{name}:precision:{spec.float_precision}")
    return HashingEncoder(dim=dim, salt="schema").encode_tokens(tokens)


def encode_registry(registry: SchemaRegistry, dim: int = 128) -> dict[str, np.ndarray]:
    """Encode all declared schemas.

    This uses the registry's internal contracts through known public tool
    ids when available.  If a future registry exposes an iterator, this
    function can switch to that without changing the output shape.
    """
    schemas = getattr(registry, "_schemas")
    return {tool_id: encode_tool_schema(schema, dim=dim) for tool_id, schema in schemas.items()}


__all__ = ["encode_registry", "encode_tool_schema"]
