from __future__ import annotations
import json
import uuid
from pathlib import Path
from typing import Any, Mapping
from station_mesh_guard.registry.station_catalog import StationCatalog
from station_mesh_guard.ingest.observation_contract import ObservationContract
from station_mesh_guard.quality.sensor_drift import SensorDrift
from station_mesh_guard.quality.gap_recovery import GapRecovery
from station_mesh_guard.calibration.reference_pairing import ReferencePairing
from station_mesh_guard.topology.village_coverage import VillageCoverage
from station_mesh_guard.outages.maintenance_queue import MaintenanceQueue
from station_mesh_guard.registry.runtime import DomainError, RunState, SQLiteLedger, fingerprint, number, stable_json
from station_mesh_guard.assurance.quality_orchestrator import QualityOrchestrator

class ApplicationService:
    def __init__(self, database: str | Path):
        self.ledger = SQLiteLedger(database)
        self.stages = [StationCatalog(), ObservationContract(), SensorDrift(), GapRecovery(), ReferencePairing(), VillageCoverage(), MaintenanceQueue()]
        self.workflow = QualityOrchestrator()

    def execute(self, payload: Mapping[str, Any], request_key: str | None = None) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise DomainError("payload must be an object")
        case_id = str(payload.get("case_id", "")).strip()
        if not case_id:
            self.ledger.fail("", "intake", "case_id is required", dict(payload))
            raise DomainError("case_id is required")
        key = request_key or fingerprint(payload)
        recalled = self.ledger.recalled(key)
        if recalled is not None:
            return recalled
        state = RunState(uuid.uuid4().hex, case_id, dict(payload))
        state.emit("received", "run_started", project='station_mesh_guard', architecture='站点主数据、观测契约与可恢复质控流水线')
        try:
            self.workflow.execute(state, self.stages, self.ledger)
            state.advance("completed")
            state.artifacts["operational_summary"] = self._summary(state)
            self.ledger.save(state)
        except Exception as exc:
            self.ledger.fail(case_id, state.state, str(exc), dict(payload))
            raise
        response = {
            "project": 'station_mesh_guard', "architecture": '站点主数据、观测契约与可恢复质控流水线', "run_id": state.run_id,
            "case_id": state.case_id, "state": state.state, "revision": state.revision,
            "finding_count": len(state.findings), "findings": [item.__dict__ if hasattr(item, "__dict__") else {"rule": item.rule, "stage": item.stage, "severity": item.severity, "message": item.message, "observed": item.observed, "expected": item.expected} for item in state.findings],
            "metrics": state.metrics, "artifacts": state.artifacts, "event_count": len(state.events),
            "storage": self.ledger.stats(),
        }
        self.ledger.remember(key, state.run_id, response)
        return response

    def _summary(self, state: RunState) -> dict[str, Any]:
        severities: dict[str, int] = {}
        for finding in state.findings:
            severities[finding.severity] = severities.get(finding.severity, 0) + 1
        return {
            "case_id": state.case_id,
            "completed_stages": [item.stage for item in state.findings] + list(state.artifacts),
            "severity_counts": severities,
            "metric_checksum": fingerprint(state.metrics)[:16],
            "artifact_checksum": fingerprint(state.artifacts)[:16],
            "priority": round(number(state.payload.get("attributes", {}).get("risk_score")), 3),
        }

    def execute_batch(self, records: list[Mapping[str, Any]]) -> dict[str, Any]:
        completed, failed = [], []
        for position, record in enumerate(records):
            try:
                completed.append(self.execute(record, f"batch:{position}:{fingerprint(record)}"))
            except Exception as exc:
                failed.append({"position": position, "case_id": str(record.get("case_id", "")), "error": str(exc)})
        return {"completed": completed, "failed": failed, "storage": self.ledger.stats()}

def run_fixture(path: str | Path, database: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    service = ApplicationService(database)
    if isinstance(payload, list):
        return service.execute_batch(payload)
    return service.execute(payload, "acceptance:" + fingerprint(payload))
