#!/usr/bin/env python3
"""
Bridge probe — first real end-to-end run of Project-A119's Equalizer -> Fabric
wire (fabric/bridge.py). Real file writes, real reads, one real fault
(read of a path that doesn't exist -> lawful_absent), all routed through
Equalizer.execute() and observed by a resident ReactiveAttentionFabric via
FabricCommitSink. No mocks: the equalizer's own FileCapability touches real
disk; the fabric is a real long-lived object, not reconstructed per call.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fabric.attention.reactive_attention_fabric import ReactiveAttentionFabric
from fabric.bridge import FabricCommitSink
from fabric.equalizer.symbolic_fault_equalizer import Equalizer


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cherry_ttt_bridge_probe_") as tmp:
        fabric = ReactiveAttentionFabric(memory_path=str(Path(tmp) / "fabric_memory.sqlite"))
        sink = FabricCommitSink(fabric)
        equalizer = Equalizer(
            witness_path=str(Path(tmp) / "equalizer_witness.sqlite"),
            commit_sink=sink,
        )

        target_a = str(Path(tmp) / "alpha.txt")
        target_b = str(Path(tmp) / "beta.txt")
        missing = str(Path(tmp) / "does_not_exist.txt")

        # 1. Real WRITE through the gate.
        t1 = equalizer.execute({
            "verb": "WRITE", "target": target_a, "contents": "hello-from-A119",
        })
        # 2. Real WRITE, second file.
        t2 = equalizer.execute({
            "verb": "WRITE", "target": target_b, "contents": "second-file-content",
        })
        # 3. Real READ back of what was just written.
        t3 = equalizer.execute({"verb": "READ", "target": target_a})
        # 4. Real READ of a path that legitimately does not exist -> lawful_absent,
        #    still committed, still observed by the fabric (this is the point:
        #    the fabric sees truth, including lawful absence, not just happy path).
        t4 = equalizer.execute({"verb": "READ", "target": missing})

        transitions = [t1, t2, t3, t4]
        for label, t in zip(["write_a", "write_b", "read_a", "read_missing"], transitions):
            print(f"{label}: outcome={t.witness.outcome} kind={t.observation.kind} "
                  f"digest={t.observation.digest[:12]} witness_id={t.witness.witness_id}")

        # Verify the fabric actually received all four -- not just that
        # execute() ran, but that observe() landed real StreamEvents in the
        # resident fabric's stream state.
        assert sink.events_observed == 4, f"expected 4 observed events, got {sink.events_observed}"

        # Real attend() over what the fabric now holds: a genuine attention
        # query against the streams the equalizer fed it, not a synthetic
        # candidate list.
        view = fabric.drain(budget=16)
        result = {
            "equalizer_metrics": {
                "received": equalizer.metrics().received,
                "committed": equalizer.metrics().committed,
                "equalized": equalizer.metrics().equalized,
                "lawful_absent": equalizer.metrics().lawful_absent,
            },
            "sink_events_observed": sink.events_observed,
            "fabric_attend": {
                "status": view.status,
                "focus_stream_ids": list(view.focus_stream_ids),
                "selected_event_ids": list(view.selected_event_ids),
                "receipt_mechanism": view.receipt.mechanism,
                "receipt_complexity_class": view.receipt.complexity_class,
                "receipt_candidates_before": view.receipt.candidates_before,
                "receipt_candidates_after": view.receipt.candidates_after,
                "receipt_topology_reason": view.receipt.topology_reason,
            },
            "verified_read_matches_write": t3.observation.body == "hello-from-A119",
            "verified_missing_is_lawful_absent": t4.witness.outcome == "lawful_absent",
        }
        print()
        print("=" * 72)
        print("RESULT")
        print(json.dumps(result, indent=2))

        out_path = Path(__file__).resolve().parent.parent / "test-runs" / "bridge_probe.json"
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nfull evidence: {out_path}")

        assert result["verified_read_matches_write"], "equalized READ did not match equalized WRITE"
        assert result["verified_missing_is_lawful_absent"], "missing-path READ was not lawful_absent"
        print("\nBRIDGE PROBE: all assertions passed -- Equalizer -> FabricCommitSink -> ReactiveAttentionFabric verified end-to-end on real disk.")


if __name__ == "__main__":
    main()
