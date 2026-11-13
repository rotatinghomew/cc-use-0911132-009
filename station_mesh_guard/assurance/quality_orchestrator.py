from __future__ import annotations
from typing import Any
from station_mesh_guard.registry.runtime import RunState, SQLiteLedger, fingerprint, number, sequence

class QualityOrchestrator:
    artifact_key = 'quality_gate'

    def execute(self, state: RunState, stages: list[Any], ledger: SQLiteLedger) -> None:
        quarantined = []
        checkpoints = []
        for position, stage in enumerate(stages):
            before = len(state.findings)
            stage.apply(state)
            new = state.findings[before:]
            critical = [item.rule for item in new if item.severity == "critical"]
            if critical:
                quarantined.append({"stage": stage.name, "rules": critical})
            checkpoints.append({"stage": stage.name, "revision": state.revision, "finding_delta": len(new)})
            ledger.save(state)
        state.artifacts[self.artifact_key] = {"checkpoints": checkpoints, "quarantined": quarantined, "healthy_stages": len(stages) - len(quarantined), "signature": fingerprint(checkpoints)}
        state.emit(self.artifact_key, "quality_gate_completed", quarantined=len(quarantined))
