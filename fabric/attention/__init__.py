"""Somnus native attention domain: model-independent reactive attention fabric.

Source: Morpheus flash_attn.py 2023-05-19 (historical artifact mutated 2026-08-24)
Integrated: 2026-08-24
Purpose: Expose the LICHE reactive attention fabric as a lazy-loading native domain
and a bounded describe_path filesystem-stream entry point for harness verification.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabric.attention.reactive_attention_fabric import PathStreamSummary

_MODULE_NAME = "reactive_attention_fabric"

_EXPORTS: dict[str, str] = {
    "ActiveContext": "ActiveContext",
    "AttendedCognitiveView": "AttendedCognitiveView",
    "AttentionCandidate": "AttentionCandidate",
    "AttentionQuery": "AttentionQuery",
    "AttentionReceipt": "AttentionReceipt",
    "ContentRef": "ContentRef",
    "DurableRecord": "DurableRecord",
    "FabricEventKind": "FabricEventKind",
    "NativeIngress": "NativeIngress",
    "PathStreamSummary": "PathStreamSummary",
    "ProvenanceStamp": "ProvenanceStamp",
    "ReactiveAttentionFabric": "ReactiveAttentionFabric",
    "ResourcePressure": "ResourcePressure",
    "SqliteDurableMemory": "SqliteDurableMemory",
    "StreamEvent": "StreamEvent",
    "describe_path": "describe_path",
    "sample_resource_pressure": "sample_resource_pressure",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> object:
    """Lazily resolve exported symbols against the single-file attention fabric.

    Args:
        name: Public attribute requested from the domain package.

    Returns:
        The attribute from reactive_attention_fabric.

    Raises:
        AttributeError: When the name is not part of the domain surface.
    """
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"fabric.attention.{_MODULE_NAME}")
    return getattr(module, name)


def describe_path(project_root: str) -> PathStreamSummary:
    """Ingest a real filesystem path as a typed stream for smoke harnesses.

    Args:
        project_root: Directory or file to address. The path remains the native source.

    Returns:
        A structured PathStreamSummary from the fabric.
    """
    module = import_module(f"fabric.attention.{_MODULE_NAME}")
    return module.describe_path(project_root)
