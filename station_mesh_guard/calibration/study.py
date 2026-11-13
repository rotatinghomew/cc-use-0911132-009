from __future__ import annotations

import json
import math
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from station_mesh_guard.registry.runtime import DomainError, RunState, SQLiteLedger

from station_mesh_guard.calibration import metrics
from station_mesh_guard.calibration.study_data import (
    digest,
    load_study_input,
    pairing_segments,
    sort_rows,
    usable_observations,
)

ALGORITHM_VERSION = "calibration-study-v1"
BOOTSTRAP_RESAMPLES = 2000
CONFIDENCE = 0.95
MIN_EVENTS = 2
MIN_EFFECT = metrics.MIN_EFFECT
SIGNIFICANCE = 0.05

@dataclass(frozen=True)
class Method:
    name: str
    label: str
    explanation: str


METHODS = (
    Method("none", "未校准", "直接使用原始观测，作为对照基线。"),
    Method("mean_offset", "均值偏移校准", "从维修前配对残差估计固定加性偏移，读数减去该偏移。"),
    Method("median_offset", "中位偏移校准", "用维修前配对残差的中位数估计加性偏移，对极端残差更稳健。"),
    Method("gain_ratio", "增益比例校准", "按维修前读数与参考值的比例估计乘性增益，读数除以该增益。"),
)


def _seed(study_id: str, *parts: str) -> int:
    return int(digest([study_id, *parts])[:16], 16)


def _maintenance_map(study: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in study["maintenance"]:
        if item["available_at"] <= study["as_of"]:
            result.setdefault(item["device_id"], []).append(item)
    for items in result.values():
        items.sort(key=lambda item: (item["completed_at"], item["index"]))
    return result


def _period(observation, maintenance: Sequence[Mapping[str, Any]]) -> str:
    """Split observations around the last recorded maintenance.

    A reading stamped exactly at the completion instant still reflects conditions
    before the fix, so the boundary is inclusive on the pre side. This avoids
    crediting maintenance with a change that was already present in the last
    pre-fix reading.
    """
    if not maintenance:
        return "unsplit"
    return "pre" if observation.observed_at <= maintenance[-1]["completed_at"] else "post"


def _residual(row: Mapping[str, Any]) -> float:
    return row["observation"].value - row["observation"].reference_value


def _fit_parameter(method: Method, rows: Sequence[Mapping[str, Any]]) -> dict[str, float] | None:
    residuals = [_residual(row) for row in rows]
    if not residuals:
        return None
    if method.name == "none":
        return {}
    if method.name == "mean_offset":
        return {"offset": sum(residuals) / len(residuals)}
    if method.name == "median_offset":
        return {"offset": metrics.percentile(residuals, 0.5)}
    if method.name == "gain_ratio":
        denominator = sum(row["observation"].reference_value ** 2 for row in rows)
        numerator = sum(row["observation"].value * row["observation"].reference_value for row in rows)
        return {"gain": numerator / denominator} if denominator > 0 else None
    raise ValueError(f"unknown calibration method: {method.name}")


def _bootstrap_parameter(
    method: Method,
    pre_rows: Sequence[Mapping[str, Any]],
    study_id: str,
    device_id: str,
) -> dict[str, Any] | None:
    """Resample pre-period events to show how stable the fitted parameter is."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in pre_rows:
        grouped.setdefault(row["observation"].event_id, []).append(row)
    events = sorted(grouped)
    if len(events) < MIN_EVENTS or method.name == "none":
        return None
    generator = random.Random(_seed(study_id, device_id, method.name, "parameter"))
    samples: dict[str, list[float]] = {}
    for _ in range(BOOTSTRAP_RESAMPLES):
        chosen = [events[generator.randrange(len(events))] for _ in events]
        fitted = _fit_parameter(method, [row for event in chosen for row in grouped[event]])
        if not fitted:
            continue
        for key, value in fitted.items():
            samples.setdefault(key, []).append(float(value))
    if not samples:
        return None
    alpha = (1.0 - CONFIDENCE) / 2.0
    key = next(iter(samples))
    return {
        "parameter": key,
        "estimate": metrics.rounded(metrics.mean(samples[key])),
        "interval": [metrics.rounded(metrics.percentile(samples[key], alpha)),
                     metrics.rounded(metrics.percentile(samples[key], 1.0 - alpha))],
        "resamples": len(samples[key]),
        "confidence": CONFIDENCE,
    }


def fit_candidate(
    method: Method,
    pre_rows: Sequence[Mapping[str, Any]],
    study_id: str,
    device_id: str,
) -> dict[str, Any]:
    """Estimate the calibration parameter using pre-maintenance events only."""
    parameter = _fit_parameter(method, pre_rows)
    residuals = [_residual(row) for row in pre_rows]
    training_mae = None
    if parameter is not None and residuals:
        corrected = [metrics.correct(method.name, row["observation"].value, parameter) - row["observation"].reference_value
                     for row in pre_rows]
        training_mae = metrics.rounded(metrics.mean([abs(item) for item in corrected]))
    return {
        "candidate_id": f"{device_id}:{method.name}",
        "device_id": device_id,
        "method": method.name,
        "label": method.label,
        "explanation": method.explanation,
        "parameter": {key: metrics.rounded(value) for key, value in parameter.items()} if parameter is not None else None,
        "parameter_interval": _bootstrap_parameter(method, pre_rows, study_id, device_id),
        "training_observations": len(pre_rows),
        "training_events": len({row["observation"].event_id for row in pre_rows}),
        "training_residual": metrics.summarise(residuals),
        "training_mae": training_mae,
        "fittable": parameter is not None,
        "training_period": "pre_maintenance only",
    }


def _event_sign_test(per_event_improvement: Mapping[str, float]) -> dict[str, Any]:
    wins = sum(1 for value in per_event_improvement.values() if value > 0)
    losses = sum(1 for value in per_event_improvement.values() if value < 0)
    ties = len(per_event_improvement) - wins - losses
    result = metrics.sign_test(wins, losses, ties)
    result["unit"] = "event"
    return result


def validate_candidate(
    candidate: Mapping[str, Any],
    post_rows: Sequence[Mapping[str, Any]],
    pre_rows: Sequence[Mapping[str, Any]],
    reference_error_bound: float,
    study_id: str,
    device_id: str,
) -> dict[str, Any]:
    method = next(item for item in METHODS if item.name == candidate["method"])
    rows = sort_rows(post_rows)
    events = sorted({row["observation"].event_id for row in rows})
    base = {
        "candidate_id": candidate["candidate_id"],
        "device_id": device_id,
        "method": method.name,
        "label": method.label,
        "post_observations": len(rows),
        "post_events": len(events),
        "post_references": sorted({row["observation"].reference_id for row in rows}),
        "reference_error_bound": reference_error_bound,
    }
    if candidate["parameter"] is None or len(rows) < MIN_EVENTS or len(events) < MIN_EVENTS:
        return {**base, "verdict": "insufficient_evidence",
                "reasons": ["no fitted parameter or fewer than two eligible post-maintenance events"],
                "improvement": None, "corrected_bias": None, "sign_test": None,
                "raw": metrics.summarise([_residual(row) for row in rows]) if rows else None,
                "corrected": None, "per_event": [],
                "high_intensity": None, "naive_before_after": _naive_before_after(pre_rows, rows)}

    raw_residuals = [_residual(row) for row in rows]
    corrected_residuals = [metrics.correct(method.name, row["observation"].value, candidate["parameter"])
                           - row["observation"].reference_value for row in rows]
    improvements = [abs(raw) - abs(corrected) for raw, corrected in zip(raw_residuals, corrected_residuals)]

    per_event: list[dict[str, Any]] = []
    by_event: dict[str, list[float]] = {}
    for row, raw, corrected, gain in zip(rows, raw_residuals, corrected_residuals, improvements):
        by_event.setdefault(row["observation"].event_id, []).append(gain)
    for event_id in events:
        event_rows = [row for row in rows if row["observation"].event_id == event_id]
        reference_mean = metrics.mean([row["observation"].reference_value for row in event_rows])
        per_event.append({
            "event_id": event_id,
            "observed_at": event_rows[0]["observation"].observed_at.isoformat(),
            "reference_id": event_rows[0]["observation"].reference_id,
            "reference_mean": metrics.rounded(reference_mean),
            "raw_mean_abs_residual": metrics.rounded(metrics.mean([abs(_residual(row)) for row in event_rows])),
            "corrected_mean_abs_residual": metrics.rounded(metrics.mean([
                abs(metrics.correct(method.name, row["observation"].value, candidate["parameter"])
                    - row["observation"].reference_value) for row in event_rows])),
            "mean_improvement": metrics.rounded(metrics.mean(by_event[event_id])),
        })

    improvement_pairs = [(row["observation"].event_id, gain) for row, gain in zip(rows, improvements)]
    improvement = metrics.bootstrap_event_means(
        improvement_pairs, seed=_seed(study_id, device_id, method.name, "improvement"),
        resamples=BOOTSTRAP_RESAMPLES, confidence=CONFIDENCE, minimum_effect=MIN_EFFECT,
    )
    corrected_bias = metrics.bootstrap_event_means(
        [(row["observation"].event_id, value) for row, value in zip(rows, corrected_residuals)],
        seed=_seed(study_id, device_id, method.name, "corrected_bias"),
        resamples=BOOTSTRAP_RESAMPLES, confidence=CONFIDENCE,
    )
    per_event_improvement = {item["event_id"]: item["mean_improvement"] for item in per_event}
    sign = _event_sign_test(per_event_improvement)
    training_events = {row["observation"].event_id for row in pre_rows}
    shared_events = training_events & set(events)

    reference_values = [row["observation"].reference_value for row in rows]
    median_reference = metrics.percentile(reference_values, 0.5)
    high_events = [item for item in per_event if item["reference_mean"] >= median_reference]
    high_improvement = metrics.mean([item["mean_improvement"] for item in high_events]) if high_events else None

    reasons: list[str] = []
    verdict = "not_material"
    parameter = candidate["parameter"]
    degenerate = False
    if method.name in ("mean_offset", "median_offset"):
        degenerate = abs(float(parameter.get("offset", 0.0))) < MIN_EFFECT / 2.0
    elif method.name == "gain_ratio":
        degenerate = abs(float(parameter.get("gain", 1.0)) - 1.0) < MIN_EFFECT / 2.0
    role = "baseline" if method.name == "none" else ("degenerate" if degenerate else "candidate")
    bias_interval = corrected_bias.get("interval")
    remaining_bias = corrected_bias.get("estimate")
    systematic_bias_left = bool(bias_interval is not None and remaining_bias is not None
                                and (bias_interval[0] > 0 or bias_interval[1] < 0)
                                and abs(remaining_bias) >= MIN_EFFECT / 2.0)
    if systematic_bias_left:
        reasons.append(f"systematic bias remains after correction: {remaining_bias} "
                       f"with interval {bias_interval}")
    training_mismatch = bool(candidate["training_mae"] is not None
                             and candidate.get("best_training_mae") is not None
                             and candidate["training_mae"] > candidate["best_training_mae"] + MIN_EFFECT)
    if training_mismatch:
        reasons.append("the training fit is not the best explainable model for the pre-period residual")
    intensity_gap = bool(high_improvement is not None and high_improvement < MIN_EFFECT)
    if intensity_gap and method.name != "none" and not degenerate:
        reasons.append("improvement does not persist in the higher-intensity half of post events")
    candidate_improves = bool(improvement["meets_minimum_effect"] and improvement["estimate"] >= MIN_EFFECT)
    if method.name == "none":
        reasons = [f"baseline comparator; residual bias {metrics.rounded(metrics.mean(raw_residuals))} "
                   f"is left uncorrected"]
    elif degenerate:
        reasons = ["the fitted correction is effectively zero; applying it cannot change readings"]
    elif not candidate_improves:
        if improvement["interval"] is not None and improvement["interval"][0] <= 0 < improvement["interval"][1]:
            reasons.append("improvement interval includes zero; no material calibration benefit")
        else:
            reasons.append("improvement is below the practical threshold or too uncertain")
    elif systematic_bias_left or training_mismatch or intensity_gap:
        verdict = "rejected"
    elif sign["p_value"] <= SIGNIFICANCE and sign["wins"] > sign["losses"]:
        verdict = "supported"
        reasons.append(f"event-level absolute error improves by {improvement['estimate']} "
                       f"(interval {improvement['interval']}), exceeding the {MIN_EFFECT} unit threshold; "
                       f"one-sided exact sign test p={sign['p_value']} "
                       f"(two-sided {sign['p_value_two_sided']})")
    else:
        reasons.append("improvement is positive but not consistent at the event level")

    return {
        **base,
        "verdict": verdict,
        "role": role,
        "reasons": reasons,
        "raw": metrics.summarise(raw_residuals),
        "corrected": metrics.summarise(corrected_residuals),
        "improvement": improvement,
        "corrected_bias": corrected_bias,
        "sign_test": sign,
        "high_intensity": {"events": [item["event_id"] for item in high_events],
                           "mean_improvement": metrics.rounded(high_improvement) if high_improvement is not None else None},
        "per_event": per_event,
        "event_disjoint": {
            "training_events": sorted(training_events),
            "validation_events": events,
            "disjoint": not shared_events,
            "shared_events": sorted(shared_events),
        },
        "naive_before_after": _naive_before_after(pre_rows, rows),
        "event_intensity": _event_intensity(pre_rows, rows),
    }


def _naive_before_after(pre_rows: Sequence[Mapping[str, Any]], post_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The comparison a naive report would make, with its confounds made explicit."""
    if not pre_rows or not post_rows:
        return {"available": False}
    pre_observed = metrics.mean([row["observation"].value for row in pre_rows])
    post_observed = metrics.mean([row["observation"].value for row in post_rows])
    pre_reference = metrics.mean([row["observation"].reference_value for row in pre_rows])
    post_reference = metrics.mean([row["observation"].reference_value for row in post_rows])
    pre_residual = metrics.mean([_residual(row) for row in pre_rows])
    post_residual = metrics.mean([_residual(row) for row in post_rows])
    observed_delta = post_observed - pre_observed
    reference_delta = post_reference - pre_reference
    residual_delta = post_residual - pre_residual
    if abs(residual_delta) <= MIN_EFFECT / 2.0 and abs(reference_delta) >= MIN_EFFECT:
        attribution = "event_intensity"
        note = "observed mean change tracks the reference mean; the paired residual does not change"
    elif abs(residual_delta) > MIN_EFFECT / 2.0:
        attribution = "residual_change"
        note = "paired residual changes beyond the practical threshold"
    else:
        attribution = "no_change"
        note = "no material change in observed or reference means"
    return {
        "available": True,
        "observed_mean_pre": metrics.rounded(pre_observed),
        "observed_mean_post": metrics.rounded(post_observed),
        "reference_mean_pre": metrics.rounded(pre_reference),
        "reference_mean_post": metrics.rounded(post_reference),
        "observed_delta": metrics.rounded(observed_delta),
        "reference_delta": metrics.rounded(reference_delta),
        "residual_delta": metrics.rounded(residual_delta),
        "attribution": attribution,
        "note": note,
        "caution": "pre and post use different reference instruments and different events; "
                   "this comparison cannot be read as a maintenance benefit",
    }


def _event_intensity(pre_rows: Sequence[Mapping[str, Any]],
                     post_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Show whether the pre/post boundary also changes rain intensity.

    If the reference mean drops across the boundary and the paired residual does not
    change, an observed mean that looks better is an intensity effect, not a
    maintenance benefit.
    """
    if not pre_rows or not post_rows:
        return {"available": False}
    reference_delta = metrics.mean([row["observation"].reference_value for row in post_rows]) \
        - metrics.mean([row["observation"].reference_value for row in pre_rows])
    observed_delta = metrics.mean([row["observation"].value for row in post_rows]) \
        - metrics.mean([row["observation"].value for row in pre_rows])
    residual_delta = metrics.mean([_residual(row) for row in post_rows]) \
        - metrics.mean([_residual(row) for row in pre_rows])
    changed = abs(reference_delta) >= MIN_EFFECT
    exploited = abs(residual_delta) <= MIN_EFFECT / 2.0
    if changed and exploited:
        label = "intensity_confound"
        direction = "falls" if reference_delta < 0 else "rises"
        statement = (f"rain intensity {direction} across the boundary while the paired residual does not "
                     f"change, so a changed observed mean is not a maintenance benefit")
    elif changed:
        label = "intensity_change_with_residual_change"
        statement = "rain intensity changes across the boundary and the paired residual also changes"
    else:
        label = "stable_intensity"
        statement = "rain intensity is stable across the boundary"
    return {
        "reference_mean_delta": metrics.rounded(reference_delta),
        "observed_mean_delta": metrics.rounded(observed_delta),
        "residual_mean_delta": metrics.rounded(residual_delta),
        "label": label,
        "statement": statement,
    }


def _effect_size(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Report the improvement relative to raw error so readers can judge practical size."""
    if entry is None:
        return None
    raw_mae = entry["raw"]["mae"]
    estimate = entry["improvement"]["estimate"] if entry.get("improvement") else None
    if raw_mae is None or raw_mae == 0 or estimate is None:
        return {"absolute_improvement": estimate, "raw_mae": raw_mae,
                "relative_reduction": 0.0 if raw_mae == 0 else None}
    return {
        "absolute_improvement": estimate,
        "raw_mae": raw_mae,
        "relative_reduction": metrics.rounded(estimate / raw_mae),
        "corrected_mae": entry["corrected"]["mae"],
        "corrected_max_abs": entry["corrected"]["max_abs"],
        "raw_max_abs": entry["raw"]["max_abs"],
    }


def _batch_improvement(validations: Sequence[Mapping[str, Any]], batch_id: str) -> dict[str, Any] | None:
    """Cluster bootstrap by event, averaging primary-method device improvements within each event."""
    primary = [entry for entry in validations
               if entry["batch_id"] == batch_id and entry["method"] == "mean_offset"
               and entry.get("per_event")]
    if not primary:
        return None
    by_event: dict[str, list[float]] = {}
    for entry in primary:
        for item in entry["per_event"]:
            by_event.setdefault(item["event_id"], []).append(item["mean_improvement"])
    events = sorted(by_event)
    if len(events) < MIN_EVENTS:
        return {"estimate": None, "interval": None, "events": len(events)}
    values = [metrics.mean(by_event[event]) for event in events]
    generator = random.Random(_seed("batch", batch_id, "improvement"))
    samples: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        chosen = [events[generator.randrange(len(events))] for _ in events]
        samples.append(metrics.mean([metrics.mean(by_event[item]) for item in chosen]))
    alpha = (1.0 - CONFIDENCE) / 2.0
    return {
        "estimate": metrics.rounded(metrics.mean(values)),
        "interval": [metrics.rounded(metrics.percentile(samples, alpha)),
                     metrics.rounded(metrics.percentile(samples, 1.0 - alpha))],
        "events": len(events),
        "devices": len(primary),
        "resamples": BOOTSTRAP_RESAMPLES,
        "confidence": CONFIDENCE,
        "cluster_unit": "event (devices averaged within an event)",
    }


class CalibrationStudy:
    def __init__(self, payload: Mapping[str, Any], database: str | Path | None = None):
        self.study = load_study_input(payload)
        self.database = str(database) if database is not None else None
        self.study_id = digest({
            "algorithm": ALGORITHM_VERSION,
            "as_of": self.study["as_of"].isoformat(),
            "source_versions": self.study["source_versions"],
            "observations": [item.raw for item in self.study["observations"]],
            "pairings": [item["raw"] for item in self.study["pairings"]],
            "maintenance": [item["raw"] for item in self.study["maintenance"]],
            "outages": [item["raw"] for item in self.study["outages"]],
            "quality_revisions": self.study["raw"].get("quality_revisions", []),
            "thresholds": {"min_effect": MIN_EFFECT, "confidence": CONFIDENCE,
                           "resamples": BOOTSTRAP_RESAMPLES, "min_events": MIN_EVENTS},
        })

    def execute(self) -> dict[str, Any]:
        if self.database is not None:
            recalled = SQLiteLedger(self.database).recalled("calibration-study:" + self.study_id)
            if recalled is not None:
                return recalled
        output = self._build()
        if self.database is not None:
            self._persist(output)
        return output

    def _persist(self, output: dict[str, Any]) -> None:
        ledger = SQLiteLedger(self.database)
        output["evidence"]["database"] = self._database_evidence(ledger)
        payload = {"case_id": "calibration-study:" + self.study_id,
                   "as_of": self.study["as_of"].isoformat(),
                   "source_versions": self.study["source_versions"],
                   "study_id": self.study_id,
                   "input_digest": output["input"]["digest"]}
        run = RunState(self.study_id, "calibration-study:" + self.study_id, payload)
        run.state = "completed"
        run.artifacts["calibration_study"] = output
        run.set_metric("supported_candidates",
                       sum(1 for entry in output["validation"] if entry["verdict"] == "supported"))
        run.emit("calibration_study", "study_completed", study_id=self.study_id)
        ledger.save(run)
        ledger.remember("calibration-study:" + self.study_id, self.study_id, output)
        output["evidence"]["database"] = self._database_evidence(ledger)

    def _database_evidence(self, ledger: SQLiteLedger) -> dict[str, Any]:
        baseline = self.study["baseline_case_id"]
        run_id = None
        try:
            with sqlite3.connect(ledger.database) as connection:
                row = connection.execute(
                    "SELECT run_id FROM runs WHERE case_id=? ORDER BY updated_at LIMIT 1", (baseline,)
                ).fetchone()
            if row is not None:
                run_id = str(row[0])
        except sqlite3.Error:
            run_id = None
        return {
            "baseline_case_id": baseline,
            "baseline_case_present": run_id is not None,
            "baseline_run_id": run_id,
            "study_run_id": self.study_id,
        }

    def _build(self) -> dict[str, Any]:
        usable, excluded = usable_observations(self.study)
        maintenance = _maintenance_map(self.study)
        device_ids = sorted({row["observation"].device_id for row in usable}
                            | {item["device_id"] for item in self.study["maintenance"]})
        batches = {item["device_id"]: item["batch_id"] for item in self.study["maintenance"]}
        candidates: list[dict[str, Any]] = []
        validations: list[dict[str, Any]] = []
        uncertainty: list[dict[str, Any]] = []
        device_views: list[dict[str, Any]] = []

        for device_id in device_ids:
            device_rows = [row for row in usable if row["observation"].device_id == device_id]
            device_maintenance = maintenance.get(device_id, [])
            pre_rows = sort_rows([row for row in device_rows
                                  if _period(row["observation"], device_maintenance) == "pre"])
            post_rows = sort_rows([row for row in device_rows
                                   if _period(row["observation"], device_maintenance) == "post"])
            device_excluded = [entry for entry in excluded if entry["device_id"] == device_id]
            batch_id = batches.get(device_id, "unassigned")
            bounds = [row["reference_error"] for row in device_rows if row["reference_error"] is not None]
            bound = max(bounds) if bounds else None
            fitted = {method.name: fit_candidate(method, pre_rows, self.study_id, device_id) for method in METHODS}
            best_training = min((item["training_mae"] for item in fitted.values()
                                 if item["training_mae"] is not None), default=None)
            for method in METHODS:
                candidate = dict(fitted[method.name], batch_id=batch_id, best_training_mae=best_training)
                candidates.append(candidate)
                if bound is None:
                    validations.append({
                        "candidate_id": candidate["candidate_id"], "device_id": device_id,
                        "batch_id": batch_id, "method": method.name, "label": method.label,
                        "post_observations": len(post_rows), "post_events": 0,
                        "verdict": "insufficient_evidence",
                        "reasons": ["reference error bound unknown"],
                    })
                    continue
                entry = validate_candidate(candidate, post_rows, pre_rows, bound, self.study_id, device_id)
                validations.append({**entry, "batch_id": batch_id})
                if method.name != "none":
                    improvement = entry["improvement"]
                    reference_sd = metrics.uniform_sd(bound)
                    events = max(1, entry["post_events"])
                    # Residuals share one reference reading per row, so the reference
                    # error largely cancels in raw-vs-corrected comparisons. The event
                    # aggregate is what matters; 2x is a conservative allowance for the
                    # non-linearity of absolute residuals.
                    aggregate_reference_se = 2.0 * reference_sd / math.sqrt(events)
                    floor = max(MIN_EFFECT, 2.0 * aggregate_reference_se)
                    limited = bool(improvement and improvement["estimate"] is not None
                                   and improvement["estimate"] < floor)
                    uncertainty.append({
                        "candidate_id": candidate["candidate_id"], "device_id": device_id,
                        "batch_id": batch_id, "method": method.name,
                        "method_family": "event_cluster_bootstrap",
                        "cluster_unit": "rain event",
                        "training_events": candidate["training_events"],
                        "training_observations": candidate["training_observations"],
                        "post_events": entry["post_events"],
                        "effective_sample_units": entry["post_events"],
                        "training_parameter": candidate["parameter"],
                        "parameter_interval": candidate["parameter_interval"],
                        "improvement": improvement,
                        "systematic_bias_after": entry["corrected_bias"],
                        "reference_error_bound": bound,
                        "reference_error_sd_assumption": "symmetric uniform ±bound",
                        "reference_error_sd": metrics.rounded(reference_sd),
                        "aggregate_reference_se": metrics.rounded(aggregate_reference_se),
                        "resolution_floor": metrics.rounded(floor),
                        "reference_limited": limited,
                        "note": "the same reference instrument is used for raw and corrected residuals, so a "
                                "common reference offset cancels in the paired improvement; reference noise "
                                "still widens the interval and limits effects below the resolution floor",
                    })
            supported = [entry for entry in validations
                         if entry["device_id"] == device_id and entry.get("verdict") == "supported"]
            recommended = None
            for entry in supported:
                if recommended is None or entry["improvement"]["estimate"] > recommended["improvement"]["estimate"]:
                    recommended = entry
            device_views.append({
                "device_id": device_id,
                "batch_id": batch_id,
                "pre_observations": len(pre_rows),
                "post_observations": len(post_rows),
                "pre_events": len({row["observation"].event_id for row in pre_rows}),
                "post_events": len({row["observation"].event_id for row in post_rows}),
                "reference_ids": sorted({row["observation"].reference_id for row in device_rows}),
                "reference_error_bound": bound,
                "excluded_observations": len(device_excluded),
                "exclusion_reasons": sorted({reason for entry in device_excluded for reason in entry["reasons"]}),
                "supported_methods": sorted({entry["method"] for entry in supported}),
                "recommended_method": recommended["method"] if recommended else None,
                "recommended_improvement": recommended["improvement"] if recommended else None,
                "effect_size": _effect_size(recommended),
                "pairing_ids": sorted({row["pairing"]["pairing_id"] for row in device_rows}),
                "effective_sample": {
                    "post_observations": len(post_rows),
                    "independent_post_events": len({row["observation"].event_id for row in post_rows}),
                    "pre_observations": len(pre_rows),
                    "independent_pre_events": len({row["observation"].event_id for row in pre_rows}),
                    "cluster_unit": "rain event",
                    "excluded_observations": len(device_excluded),
                },
                "training_mae_by_method": {name: item["training_mae"] for name, item in fitted.items()},
                "naive_before_after": _naive_before_after(pre_rows, post_rows),
                "event_intensity": _event_intensity(pre_rows, post_rows),
            })

        batch_views = []
        for batch_id in sorted(set(batches.values()) or {"unassigned"}):
            members = [view for view in device_views if view["batch_id"] == batch_id]
            supported_members = [view for view in members if view["supported_methods"]]
            batch_views.append({
                "batch_id": batch_id,
                "devices": [view["device_id"] for view in members],
                "supported_devices": [view["device_id"] for view in supported_members],
                "supported_methods": sorted({method for view in supported_members
                                             for method in view["supported_methods"]}),
                "effective_post_observations": sum(view["post_observations"] for view in supported_members),
                "effective_post_events": sum(view["post_events"] for view in supported_members),
                "batch_improvement": _batch_improvement(validations, batch_id),
                "naive_attribution": {view["device_id"]: view["naive_before_after"].get("attribution")
                                      for view in members},
                "decision": "continue_calibration" if supported_members else "do_not_apply_calibration",
            })

        rejected = [{"candidate_id": entry["candidate_id"], "device_id": entry["device_id"],
                     "batch_id": entry["batch_id"], "method": entry["method"], "reasons": entry["reasons"]}
                    for entry in validations if entry.get("verdict") == "rejected"]
        no_evidence = [{"candidate_id": entry["candidate_id"], "device_id": entry["device_id"],
                        "batch_id": entry["batch_id"], "method": entry["method"], "reasons": entry["reasons"]}
                       for entry in validations if entry.get("verdict") == "insufficient_evidence"]

        applicability = {
            "supported_devices": [view["device_id"] for view in device_views if view["supported_methods"]],
            "supported_methods_by_device": {view["device_id"]: view["supported_methods"] for view in device_views},
            "rejected_candidates": rejected,
            "no_evidence_cases": no_evidence,
            "reference_outage_gaps": self._outage_segments(usable, excluded),
            "batches": batch_views,
            "maintenance_concentration": self._maintenance_concentration(),
            "limits": self._limits(uncertainty, device_views),
            "can_support": [
                "For the supported devices, a fixed additive offset learned from pre-maintenance events "
                "reduces paired absolute error in the post-maintenance period.",
                "The interval is event-clustered: repeated observations from one rain event are not "
                "treated as independent evidence.",
                "A device whose observed mean moved only because event intensity changed is reported as "
                "not material, not as a maintenance benefit.",
            ],
            "cannot_support": [
                "It cannot attribute the pre/post difference to maintenance: maintenance, the reference "
                "switch, and event intensity all change near the same boundary.",
                "It cannot certify multiplicative or intensity-dependent calibration: the apparent gain "
                "improvement is not supported by pre-period fit or post-period systematic bias.",
                "It provides no evidence inside reference outage windows, or for devices with fewer than "
                "two eligible post-maintenance events.",
                "It is an offline statistical study; it does not validate or alter live alerts.",
            ],
        }
        return {
            "study_id": self.study_id,
            "algorithm_version": ALGORITHM_VERSION,
            "input": {
                "as_of": self.study["as_of"].isoformat(),
                "schema_version": self.study["schema_version"],
                "source_versions": self.study["source_versions"],
                "baseline_case_id": self.study["baseline_case_id"],
                "digest": digest(self.study["raw"]),
            },
            "pairings": pairing_segments(self.study),
            "candidates": candidates,
            "validation": validations,
            "uncertainty": uncertainty,
            "applicability": applicability,
            "evidence": self._evidence(),
            "exclusions": {
                "total": len(excluded),
                "by_reason": _count_reasons(excluded),
                "rows": excluded,
            },
        }

    def _outage_segments(self, usable: Sequence[Mapping[str, Any]],
                         excluded: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        gaps: list[dict[str, Any]] = []
        for outage in self.study["outages"]:
            inside = [row for row in usable
                      if row["observation"].reference_id == outage["reference_id"]
                      and outage["start"] <= row["observation"].observed_at < outage["end"]]
            dropped = [entry for entry in excluded
                       if entry["reference_id"] == outage["reference_id"]
                       and outage["start"].isoformat() <= entry["observed_at"] < outage["end"].isoformat()]
            gaps.append({
                "reference_id": outage["reference_id"],
                "start": outage["start"].isoformat(),
                "end": outage["end"].isoformat(),
                "usable_observations": len(inside),
                "excluded_observations": sorted(entry["observation_id"] for entry in dropped),
                "evidence": "none" if not inside else "available",
                "statement": "no calibration evidence is available in this reference outage window; "
                             "pairings are not fabricated across the gap"
                             if not inside else "outage window contains no excluded observations",
            })
        return gaps

    def _maintenance_concentration(self) -> dict[str, Any]:
        records = self.study["maintenance"]
        if not records:
            return {"status": "no_maintenance_records", "devices": 0, "concentrated": False,
                    "control_devices": [], "control_available": True}
        counts: dict[str, int] = {}
        for item in records:
            counts[item["batch_id"]] = counts.get(item["batch_id"], 0) + 1
        maintained = {item["device_id"] for item in records}
        all_devices = {item.raw.get("device_id") for item in self.study["observations"]}
        control = sorted(item for item in all_devices if item not in maintained)
        concentrated = max(counts.values()) < len(records)
        if not concentrated:
            opinion = ("maintenance is recorded for every device at the same boundary, so a calibration effect "
                       "cannot be separated from a fleet-wide maintenance effect")
        elif control:
            opinion = ("maintenance targets part of the fleet, but the analysis still separates batches per "
                       "device; cross-device differences remain observational")
        else:
            opinion = ("maintenance targets only the devices under study and no unmaintained device is left as "
                       "a control; maintenance benefit cannot be separated from device selection")
        return {
            "total_devices": len(records),
            "batch_device_counts": counts,
            "concentrated": concentrated,
            "control_devices": control,
            "control_available": bool(control),
            "opinion": opinion,
        }

    def _limits(self, uncertainty: Sequence[Mapping[str, Any]],
                device_views: Sequence[Mapping[str, Any]]) -> list[str]:
        limits: list[str] = []
        for view in device_views:
            view_uncertainty = [item for item in uncertainty
                                if item["device_id"] == view["device_id"]
                                and item.get("reference_error_bound") is not None]
            if not view_uncertainty:
                continue
            floor = max(float(item["resolution_floor"]) for item in view_uncertainty)
            bound = max(float(item["reference_error_bound"]) for item in view_uncertainty)
            if not view["supported_methods"]:
                continue
            estimate = (view["recommended_improvement"] or {}).get("estimate")
            statement = (f"{view['device_id']}: reference error bound ±{metrics.rounded(bound)} implies a "
                         f"resolution floor near {metrics.rounded(floor)}; effects below that floor cannot "
                         f"be claimed")
            if estimate is not None and float(estimate) < floor:
                statement += " and the supported effect is itself close to the floor"
            elif estimate is not None:
                statement += (f"; the supported improvement of {metrics.rounded(float(estimate))} is above "
                              f"that floor, but the bound still makes this a coarse estimate")
            limits.append(statement)
        concentration = self._maintenance_concentration()
        if concentration.get("concentrated") and not concentration.get("control_available"):
            limits.append("no unmaintained control device remains, so the supported improvement is a "
                          "within-device before/after statement, not a maintained-vs-control comparison")
        if concentration.get("total_devices") and not concentration.get("concentrated"):
            limits.append("every device is maintained at the same boundary; the study cannot isolate the "
                          "calibration parameter from a fleet-wide change")
        for view in device_views:
            if not view["supported_methods"]:
                limits.append(f"{view['device_id']} has no supported calibration method; its readings should "
                              f"stay on the existing handling path")
        return limits

    def _evidence(self) -> dict[str, Any]:
        calibration_digest = digest({
            "observations": [item.raw for item in self.study["observations"]],
            "pairings": [item["raw"] for item in self.study["pairings"]],
            "quality_revisions": self.study["raw"].get("quality_revisions", []),
            "reference_outages": [item["raw"] for item in self.study["outages"]],
        })
        maintenance_digest = digest([item["raw"] for item in self.study["maintenance"]])
        reference_version = str(self.study["source_versions"].get("reference", "unknown"))
        operations_version = str(self.study["source_versions"].get("operations", "unknown"))
        return {
            "calibration": {
                "version": f"{reference_version}:{calibration_digest[:12]}",
                "digest": calibration_digest,
                "source_versions": {"reference": reference_version},
                "observations": len(self.study["observations"]),
                "pairings": len(self.study["pairings"]),
                "quality_revisions": len(self.study["raw"].get("quality_revisions", []) or []),
                "cutoff": self.study["as_of"].isoformat(),
            },
            "maintenance": {
                "version": f"{operations_version}:{maintenance_digest[:12]}",
                "digest": maintenance_digest,
                "source_versions": {"operations": operations_version},
                "records": len(self.study["maintenance"]),
                "batches": sorted({item["batch_id"] for item in self.study["maintenance"]}),
                "cutoff": self.study["as_of"].isoformat(),
            },
            "baseline": {"case_id": self.study["baseline_case_id"]},
        }


def _count_reasons(excluded: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in excluded:
        for reason in entry["reasons"]:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def load_payload(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainError(f"cannot read study input: {exc}") from exc
    if not isinstance(payload, dict):
        raise DomainError("study input must be a JSON object")
    return payload


def run_study(input_path: str | Path, database: str | Path | None, output_path: str | Path) -> dict[str, Any]:
    payload = load_payload(input_path)
    study = CalibrationStudy(payload, database)
    output = study.execute()
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return output
