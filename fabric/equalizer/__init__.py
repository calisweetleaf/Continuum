"""Somnus native equalizer domain: symbolic fault-equalization primitive.

Source: Morpheus GPT-4o real_time_interrupt_handler.py (historical artifact mutated 2026-08-25)
Integrated: 2026-08-25
Purpose: Expose the symbolic fault equalizer as a lazy-loading native domain
and a bounded describe_path filesystem-execution entry point for harness verification.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabric.equalizer.symbolic_fault_equalizer import PathEqualizationSummary

_MODULE_NAME = "symbolic_fault_equalizer"

_EXPORTS: dict[str, str] = {
    "CanonicalObservation": "CanonicalObservation",
    "CausalContext": "CausalContext",
    "CausalEvent": "CausalEvent",
    "EqualizedTransition": "EqualizedTransition",
    "Equalizer": "Equalizer",
    "EqualizerError": "EqualizerError",
    "EventFamily": "EventFamily",
    "EventPriority": "EventPriority",
    "FabricWitness": "FabricWitness",
    "FaultSymbol": "FaultSymbol",
    "FileCapability": "FileCapability",
    "PathEqualizationSummary": "PathEqualizationSummary",
    "RepairOperator": "RepairOperator",
    "SqliteCapability": "SqliteCapability",
    "SymbolicAction": "SymbolicAction",
    "WitnessStore": "WitnessStore",
    "describe_path": "describe_path",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> object:
    """Lazily resolve exported symbols against the single-file equalizer.

    Args:
        name: Public attribute requested from the domain package.

    Returns:
        The attribute from symbolic_fault_equalizer.

    Raises:
        AttributeError: When the name is not part of the domain surface.
    """
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"fabric.equalizer.{_MODULE_NAME}")
    return getattr(module, name)


def describe_path(project_root: str) -> PathEqualizationSummary:
    """Execute a real filesystem path through the equalizer for smoke harnesses.

    Args:
        project_root: Directory or file to address. The path remains the native source.

    Returns:
        A structured PathEqualizationSummary from the equalizer.
    """
    module = import_module(f"fabric.equalizer.{_MODULE_NAME}")
    return module.describe_path(project_root)
