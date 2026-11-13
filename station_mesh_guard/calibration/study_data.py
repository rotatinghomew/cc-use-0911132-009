from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from station_mesh_guard.registry.runtime import DomainError, stable_json

from station_mesh_guard.calibration.metrics import is_finite_number

VALID_QUALITY = "valid"


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def digest(value: object) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


@dataclass
class Revision:
    observation_id: str
    quality: str
    available_at: datetime
    raw: Mapping[str, Any]


@dataclass
class Observation:
    index: int
    device_id: str
    reference_id: str
    event_id: str
    observed_at: datetime
    available_at: datetime
    value: float
    reference_value: float
    quality: str
    reference_error: float | None
    raw: Mapping[str, Any]
    revisions: list[Revision] = field(default_factory=list)

    def effective(self, as_of: datetime) -> tuple[str, datetime]:
        """Quality in force at *as_of*, ignoring revisions that were not yet known."""
        quality = self.quality
        effective_at = self.available_at
        for revision in self.revisions:
            if revision.available_at <= as_of and revision.available_at >= effective_at:
                quality = revision.quality
                effective_at = revision.available_at
        return quality, effective_at

    @property
    def key(self) -> str:
        return str(self.raw.get("observation_id", f"row-{self.index}"))


def load_study_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalise the study payload without inventing missing values."""
    if not isinstance(payload, Mapping):
        raise DomainError("study input must be an object")
    as_of = parse_time(payload.get("as_of"))
    if as_of is None:
        raise DomainError("as_of must be a timezone-aware ISO 8601 timestamp")
    schema = payload.get("schema_version")
    observations_raw = payload.get("observations")
    if not isinstance(observations_raw, list) or not observations_raw:
        raise DomainError("observations must be a non-empty list")

    observations: list[Observation] = []
    violations: list[dict[str, Any]] = []
    seen_observation_ids: dict[str, int] = {}
    revisions_by_observation: dict[str, list[Revision]] = {}
    for position, item in enumerate(payload.get("quality_revisions", []) or []):
        if not isinstance(item, Mapping):
            continue
        observation_id = str(item.get("observation_id", "")).strip()
        available_at = parse_time(item.get("available_at"))
        if not observation_id or available_at is None:
            violations.append({"kind": "quality_revision", "index": position,
                               "reason": "observation_id and available_at are required"})
            continue
        if str(item.get("quality", "")) == "":
            violations.append({"kind": "quality_revision", "index": position,
                               "reason": "quality is required"})
            continue
        revisions_by_observation.setdefault(observation_id, []).append(
            Revision(observation_id, str(item["quality"]), available_at, dict(item))
        )

    for index, item in enumerate(observations_raw):
        if not isinstance(item, Mapping):
            violations.append({"kind": "observation", "index": index, "reason": "not an object"})
            continue
        missing = [name for name in ("device_id", "reference_id", "event_id")
                   if not str(item.get(name, "")).strip()]
        observed_at = parse_time(item.get("observed_at"))
        available_at = parse_time(item.get("available_at"))
        if missing or observed_at is None or available_at is None:
            violations.append({"kind": "observation", "index": index,
                               "observation_id": item.get("observation_id"),
                               "reason": "missing identifiers or timestamps"})
            continue
        if not is_finite_number(item.get("value")) or not is_finite_number(item.get("reference_value")):
            violations.append({"kind": "observation", "index": index,
                               "observation_id": item.get("observation_id"),
                               "reason": "value and reference_value must be finite numbers"})
            continue
        error_bound = item.get("reference_error")
        if error_bound is not None and not is_finite_number(error_bound):
            violations.append({"kind": "observation", "index": index,
                               "observation_id": item.get("observation_id"),
                               "reason": "reference_error must be a finite number or null"})
            continue
        observation = Observation(
            index=index,
            device_id=str(item["device_id"]),
            reference_id=str(item["reference_id"]),
            event_id=str(item["event_id"]),
            observed_at=observed_at,
            available_at=available_at,
            value=float(item["value"]),
            reference_value=float(item["reference_value"]),
            quality=str(item.get("quality", "")).strip(),
            reference_error=float(error_bound) if error_bound is not None else None,
            raw=dict(item),
        )
        if observation.key in seen_observation_ids:
            violations.append({"kind": "observation", "index": index,
                               "observation_id": observation.key,
                               "reason": "observation_id must be unique"})
            continue
        seen_observation_ids[observation.key] = index
        observation.revisions = revisions_by_observation.get(observation.key, [])
        observations.append(observation)

    pairings_raw = payload.get("pairings")
    if not isinstance(pairings_raw, list) or not pairings_raw:
        raise DomainError("pairings must be a non-empty list")
    pairings: list[dict[str, Any]] = []
    for index, item in enumerate(pairings_raw):
        if not isinstance(item, Mapping):
            violations.append({"kind": "pairing", "index": index, "reason": "not an object"})
            continue
        device_id = str(item.get("device_id", "")).strip()
        reference_id = str(item.get("reference_id", "")).strip()
        start = parse_time(item.get("start"))
        end = parse_time(item.get("end"))
        if not device_id or not reference_id or start is None or end is None:
            violations.append({"kind": "pairing", "index": index,
                               "reason": "device_id, reference_id, start, end are required"})
            continue
        if start >= end:
            violations.append({"kind": "pairing", "index": index,
                               "reason": "pairing interval must be left-closed, right-open and non-empty"})
            continue
        pairings.append({
            "index": index,
            "pairing_id": f"pairing-{index}",
            "device_id": device_id,
            "reference_id": reference_id,
            "start": start,
            "end": end,
            "available_at": parse_time(item.get("available_at")) or start,
            "raw": dict(item),
        })

    maintenance: list[dict[str, Any]] = []
    for index, item in enumerate(payload.get("maintenance", []) or []):
        if not isinstance(item, Mapping):
            continue
        completed_at = parse_time(item.get("completed_at"))
        if not str(item.get("device_id", "")).strip() or completed_at is None:
            violations.append({"kind": "maintenance", "index": index,
                               "reason": "device_id and completed_at are required"})
            continue
        maintenance.append({
            "index": index,
            "device_id": str(item["device_id"]),
            "batch_id": str(item.get("batch_id", "")).strip(),
            "completed_at": completed_at,
            "available_at": parse_time(item.get("available_at")) or completed_at,
            "raw": dict(item),
        })

    outages: list[dict[str, Any]] = []
    for index, item in enumerate(payload.get("reference_outages", []) or []):
        if not isinstance(item, Mapping):
            continue
        reference_id = str(item.get("reference_id", "")).strip()
        start = parse_time(item.get("start"))
        end = parse_time(item.get("end"))
        if not reference_id or start is None or end is None or start >= end:
            violations.append({"kind": "reference_outage", "index": index,
                               "reason": "reference_id and a non-empty interval are required"})
            continue
        outages.append({"reference_id": reference_id, "start": start, "end": end, "raw": dict(item)})

    if violations:
        raise DomainError("invalid study input: " + stable_json(violations[:4]))
    return {
        "schema_version": schema if isinstance(schema, int) and not isinstance(schema, bool) else None,
        "as_of": as_of,
        "source_versions": dict(payload.get("source_versions", {}) or {}),
        "baseline_case_id": str(payload.get("baseline_case_id", "") or ""),
        "observations": observations,
        "pairings": pairings,
        "maintenance": maintenance,
        "outages": outages,
        "raw": dict(payload),
    }


def resolve_pairing(observation: Observation, pairings: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    matches = [item for item in pairings if item["device_id"] == observation.device_id
               and item["reference_id"] == observation.reference_id
               and item["start"] <= observation.observed_at < item["end"]]
    if len(matches) > 1:
        raise DomainError(f"overlapping pairings for {observation.key}")
    return matches[0] if matches else None


def outage_reason(observation: Observation, outages: Sequence[Mapping[str, Any]]) -> str | None:
    for item in outages:
        if item["reference_id"] == observation.reference_id and item["start"] <= observation.observed_at < item["end"]:
            return "reference_outage"
    return None


def has_reference_error(observation: Observation) -> bool:
    return observation.reference_error is not None


def usable_observations(study: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return rows usable as of the study cutoff plus excluded rows with reasons.

    An observation is usable only with: a resolved pairing, quality ``valid`` that was
    already known at ``as_of``, no reference outage, no future availability, and a
    reference-error bound. Anything else is excluded rather than paired by force.
    """
    used: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    as_of = study["as_of"]
    for observation in study["observations"]:
        quality, effective_at = observation.effective(as_of)
        reasons: list[str] = []
        pairing = resolve_pairing(observation, study["pairings"])
        outage = outage_reason(observation, study["outages"])
        if pairing is None:
            reasons.append("no_active_pairing")
        if observation.available_at > as_of:
            reasons.append("not_yet_available")
        if quality != VALID_QUALITY:
            reasons.append(f"quality_{quality or 'unknown'}")
        if outage:
            reasons.append(outage)
        if not has_reference_error(observation):
            reasons.append("reference_error_unknown")
        row = {
            "observation": observation,
            "pairing": pairing,
            "quality": quality,
            "quality_effective_at": effective_at,
            "reference_error": observation.reference_error,
        }
        if reasons:
            excluded.append({
                "observation_id": observation.key,
                "device_id": observation.device_id,
                "reference_id": observation.reference_id,
                "event_id": observation.event_id,
                "observed_at": observation.observed_at.isoformat(),
                "available_at": observation.available_at.isoformat(),
                "quality": quality,
                "quality_effective_at": effective_at.isoformat(),
                "reference_error": observation.reference_error,
                "pairing_active": pairing is not None,
                "reasons": reasons,
            })
        else:
            used.append(row)
    return used, excluded


def sort_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (row["observation"].observed_at,
                                         row["observation"].device_id, row["observation"].index))


def pairing_segments(study: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Summarise every declared pairing, including the ones the data cannot support."""
    rows, excluded = usable_observations(study)
    segments: list[dict[str, Any]] = []
    for pairing in study["pairings"]:
        matched = [row for row in rows if row["pairing"] is pairing]
        dropped = [entry for entry in excluded
                   if entry["device_id"] == pairing["device_id"]
                   and entry["reference_id"] == pairing["reference_id"]
                   and pairing["start"].isoformat() <= entry["observed_at"] < pairing["end"].isoformat()]
        segments.append({
            "pairing_id": pairing["pairing_id"],
            "device_id": pairing["device_id"],
            "reference_id": pairing["reference_id"],
            "start": pairing["start"].isoformat(),
            "end": pairing["end"].isoformat(),
            "active_at_cutoff": bool(pairing["available_at"] <= study["as_of"]),
            "usable_observations": len(matched),
            "events": len({row["observation"].event_id for row in matched}),
            "excluded_observations": len(dropped),
            "exclusion_reasons": sorted({reason for entry in dropped for reason in entry["reasons"]}),
            "reference_error_bound": max((row["reference_error"] for row in matched), default=None),
        })
    return segments
