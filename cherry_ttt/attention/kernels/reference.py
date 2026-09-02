"""
Kernel reference interfaces.

No accelerated kernel is shipped in this private phase.  Importers that
explicitly request a custom kernel receive a domain validation error
rather than an unverified silent fallback.
"""

from __future__ import annotations

from ...core.errors import ValidationError


def require_custom_kernel(name: str) -> None:
    """Raise for any custom-kernel request until P6 validates one."""
    raise ValidationError(
        f"custom attention kernel {name!r} is not implemented in this build; "
        "use CandidateAttention reference path or add an equivalence-tested kernel"
    )


__all__ = ["require_custom_kernel"]
