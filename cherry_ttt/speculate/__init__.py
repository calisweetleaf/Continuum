"""Speculative execution: drafters, L2/L3 executor, adaptive-gamma (§3)."""

from __future__ import annotations

from .drafter import ActionTemplate, Drafter, TabularDrafter, TemplateDrafter
from .executor import CommitReport, LatencyModel, SpeculativeExecutor
from .gamma import AdaptiveGammaController, GammaControllerConfig

__all__ = ["ActionTemplate", "AdaptiveGammaController", "CommitReport", "Drafter",
           "GammaControllerConfig", "LatencyModel", "SpeculativeExecutor",
           "TabularDrafter", "TemplateDrafter"]
