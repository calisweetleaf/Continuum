"""Equalizer -> Attention Fabric bridge — Project-A119, first wire.

Purpose: the single joint between the two independently complete fabric
    halves, laid along the seam they already define themselves:
    Equalizer.execute() calls self._commit_sink.accept(transition)
    unconditionally after every pipeline run (committed or lawful-absent —
    see symbolic_fault_equalizer.py Equalizer.execute()/_commit()), and
    CommitSink is documented there as "Optional fabric peg. Attention or
    continuum may accept committed transitions." This module is that peg.
    Equalizer is the gate: everything that reaches ReactiveAttentionFabric
    through this sink has already been normalized, routed to a Capability,
    symbolically fault-checked, and witnessed. Nothing here bypasses that —
    FabricCommitSink has no path to the fabric except through a transition
    the equalizer already committed.
Integrated: 2026-09-02
Purpose (design note): neither production module (reactive_attention_fabric.py,
    symbolic_fault_equalizer.py) is modified. This is new glue at the fabric/
    package root, structurally satisfying CommitSink (accept(transition)) by
    duck typing — Equalizer holds no import of this class, only the Protocol
    shape.
"""

from __future__ import annotations

import time

from fabric.attention.reactive_attention_fabric import (
    ContentRef,
    FabricEventKind,
    ProvenanceStamp,
    ReactiveAttentionFabric,
    StreamEvent,
)
from fabric.equalizer.symbolic_fault_equalizer import EqualizedTransition


class FabricCommitSink:
    """CommitSink that feeds every equalized transition into a resident fabric.

    The fabric instance is resident (long-lived, event-observing), not
    reconstructed per call — matches ReactiveAttentionFabric's own design
    (self._streams/self._mailbox persist across observe() calls).
    """

    def __init__(self, fabric: ReactiveAttentionFabric) -> None:
        """Bind to a resident fabric instance.

        Args:
            fabric: The attention fabric that receives observed transitions.
        """
        self._fabric = fabric
        self.events_observed = 0

    def accept(self, transition: EqualizedTransition) -> None:
        """Convert one equalized transition into a StreamEvent and observe it.

        Args:
            transition: Committed or lawful-absent transition from
                Equalizer.execute(). Called for every execute(), not just
                successful writes — lawful absence is still real evidence
                (a path that legitimately does not exist).
        """
        observation = transition.observation
        event = StreamEvent(
            stream_id=transition.context_id,
            event_id=transition.witness.witness_id,
            source="equalizer",
            domain=f"equalized:{observation.verb.lower()}",
            content_ref=ContentRef(
                scheme="equalized",
                locator=f"{transition.context_id}:{transition.action_id}:{observation.target}",
                checksum=transition.state_digest,
            ),
            timestamp=transition.witness.recorded_at,
            kind=FabricEventKind.STREAM_APPEND,
            importance=observation.stability,
            uncertainty=1.0 - observation.stability,
            payload=transition,
            provenance=ProvenanceStamp(
                authority="equalizer",
                agent="FabricCommitSink",
                recorded_at=time.time(),
                lineage=(transition.witness.witness_id,),
            ),
        )
        self._fabric.observe(event)
        self.events_observed += 1


__all__ = ["FabricCommitSink"]
