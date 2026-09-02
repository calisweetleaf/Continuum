"""
Adaptive-γ controller — ported and closed over measured latency (§9.6).

Source: inference_optimizations.py SpeculativeDecoder._adapt_gamma
    (acceptance-rate window thresholds: >0.8 -> γ+1, <0.5 -> γ-1,
    clamped to [gamma_min, gamma_max]) — carried as the boundary rule.
    The latency-closed loop is new per proposal §3.3/§9.6: the
    controller now balances ENV latency against MODEL latency, and its
    fixed point γ* is the convergence point.
Integrated: 2026-07-06
Purpose: Ingest measured per-cycle telemetry (acceptance fraction,
    draft/verify/env latencies), maintain EMA estimates, and steer γ
    one step per adaptation toward the throughput argmax:

        E[committed | γ, α] = Σ_{i=1..γ} α^i + α^γ      (bonus on full accept)
        T(γ) = γ·draft_ms + max(γ·env_ms, verify_ms)     (L3 overlap)
        γ* = argmax_{γ ∈ [γmin, γmax]}  E[committed] / T(γ)

    One-step movement (not jumps) keeps the controller stable under
    noisy telemetry; with no latency data yet, the original threshold
    rule governs, so behavior degrades to the verified legacy port.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GammaControllerConfig:
    """Port of the SpeculativeDecoderConfig gamma surface + EMA decay."""

    gamma: int = 5
    gamma_min: int = 3
    gamma_max: int = 12
    adapt_window: int = 50
    ema_decay: float = 0.9


class AdaptiveGammaController:
    """Measured-latency γ controller (see module docstring)."""

    def __init__(self, config: GammaControllerConfig | None = None) -> None:
        self.config = config or GammaControllerConfig()
        self.current_gamma = self.config.gamma
        self._accepted_history: list[float] = []
        self._step_count = 0
        self._alpha_ema: float | None = None
        self._draft_ms_ema: float | None = None
        self._verify_ms_ema: float | None = None
        self._env_ms_ema: float | None = None

    # -- telemetry ingestion (§9.6) -------------------------------------------

    def record(
        self,
        accepted: int,
        drafted: int,
        draft_ms_per_action: float | None = None,
        verify_ms: float | None = None,
        env_ms_per_action: float | None = None,
    ) -> None:
        """Ingest one cycle's telemetry; adapts every adapt_window steps."""
        fraction = accepted / max(drafted, 1)
        self._accepted_history.append(fraction)
        self._step_count += 1
        decay = self.config.ema_decay

        def _ema(current: float | None, sample: float) -> float:
            return sample if current is None else decay * current + (1 - decay) * sample

        # Per-action acceptance α, Bernoulli MLE from boundary semantics:
        # a cycle observes `accepted` successes and, iff the boundary fell
        # short of γ, exactly one failure — so the per-cycle sample is
        # k/(k+1), or 1.0 on full acceptance. (The naive committed
        # fraction E[k]/γ FALLS as γ grows and made the controller
        # converge to the fixed point of its own bias — found and fixed
        # at the P4 gate, 2026-07-06.)
        if accepted >= drafted:
            alpha_sample = 1.0
        else:
            alpha_sample = accepted / (accepted + 1)
        self._alpha_ema = _ema(self._alpha_ema, alpha_sample)
        if draft_ms_per_action is not None:
            self._draft_ms_ema = _ema(self._draft_ms_ema, draft_ms_per_action)
        if verify_ms is not None:
            self._verify_ms_ema = _ema(self._verify_ms_ema, verify_ms)
        if env_ms_per_action is not None:
            self._env_ms_ema = _ema(self._env_ms_ema, env_ms_per_action)

        if self._step_count % self.config.adapt_window == 0:
            self._adapt()

    # -- the throughput model ----------------------------------------------------

    def expected_committed(self, gamma: int, alpha: float) -> float:
        """E[committed | γ, α] = Σ α^i + α^γ (bonus on full acceptance)."""
        if alpha <= 0.0:
            return 0.0
        if alpha >= 1.0:
            return float(gamma + 1)
        prefix = alpha * (1.0 - alpha**gamma) / (1.0 - alpha)
        return prefix + alpha**gamma

    def cycle_time(self, gamma: int) -> float:
        """T(γ) = γ·draft + max(γ·env, verify) under L3 overlap."""
        draft = (self._draft_ms_ema or 0.0) * gamma
        env = (self._env_ms_ema or 0.0) * gamma
        verify = self._verify_ms_ema or 0.0
        return draft + max(env, verify)

    def gamma_star(self) -> int | None:
        """Throughput argmax over the legal γ range; None without latency
        telemetry (the legacy rule then governs)."""
        if self._alpha_ema is None or (
            self._verify_ms_ema is None and self._env_ms_ema is None
        ):
            return None
        best_gamma, best_rate = self.config.gamma_min, -1.0
        for gamma in range(self.config.gamma_min, self.config.gamma_max + 1):
            time_ms = self.cycle_time(gamma)
            if time_ms <= 0.0:
                continue
            rate = self.expected_committed(gamma, self._alpha_ema) / time_ms
            if rate > best_rate:
                best_rate, best_gamma = rate, gamma
        return best_gamma

    # -- adaptation ------------------------------------------------------------------

    def _adapt(self) -> None:
        """One step toward γ*; legacy threshold rule when latency-blind."""
        cfg = self.config
        target = self.gamma_star()
        if target is not None:
            if target > self.current_gamma:
                self.current_gamma = min(self.current_gamma + 1, cfg.gamma_max)
            elif target < self.current_gamma:
                self.current_gamma = max(self.current_gamma - 1, cfg.gamma_min)
            return
        # Verbatim legacy rule (original _adapt_gamma):
        if not self._accepted_history:
            return
        window = self._accepted_history[-cfg.adapt_window:]
        rate = sum(window) / len(window)
        if rate > 0.8 and self.current_gamma < cfg.gamma_max:
            self.current_gamma = min(self.current_gamma + 1, cfg.gamma_max)
        elif rate < 0.5 and self.current_gamma > cfg.gamma_min:
            self.current_gamma = max(self.current_gamma - 1, cfg.gamma_min)

    def stats(self) -> dict[str, float | int | None]:
        """Telemetry snapshot (port of SpeculativeDecoder.stats surface)."""
        return {
            "current_gamma": self.current_gamma,
            "gamma_star": self.gamma_star(),
            "alpha_ema": self._alpha_ema,
            "draft_ms_ema": self._draft_ms_ema,
            "verify_ms_ema": self._verify_ms_ema,
            "env_ms_ema": self._env_ms_ema,
            "steps": self._step_count,
        }


__all__ = ["AdaptiveGammaController", "GammaControllerConfig"]
