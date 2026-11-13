from __future__ import annotations

import math
import random
from typing import Iterable, Sequence

from station_mesh_guard.registry.runtime import percentile

MIN_EFFECT = 0.5
"""Smallest absolute-error reduction treated as practically meaningful, in observation units."""


def is_finite_number(value: object) -> bool:
    """True only for a real, finite number; booleans and strings are not measurements."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def rounded(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarise(residuals: Iterable[float]) -> dict[str, object]:
    """Describe systematic bias, random spread, and tail behaviour separately."""
    values = [float(item) for item in residuals]
    if not values:
        return {"n": 0, "bias": None, "median_bias": None, "std": None, "mae": None,
                "rmse": None, "max_abs": None, "p90_abs": None, "min": None, "max": None}
    absolute = [abs(item) for item in values]
    centre = mean(values)
    spread = 0.0
    if len(values) > 1:
        spread = math.sqrt(sum((item - centre) ** 2 for item in values) / len(values))
    return {
        "n": len(values),
        "bias": rounded(centre),
        "median_bias": rounded(percentile(values, 0.5)),
        "std": rounded(spread),
        "mae": rounded(mean(absolute)),
        "rmse": rounded(math.sqrt(mean([item * item for item in values]))),
        "max_abs": rounded(max(absolute)),
        "p90_abs": rounded(percentile(absolute, 0.9)),
        "min": rounded(min(values)),
        "max": rounded(max(values)),
    }


def group_by_event(pairs: Iterable[tuple[str, float]]) -> dict[str, list[float]]:
    grouped: dict[str, list[float]] = {}
    for event_id, value in pairs:
        grouped.setdefault(str(event_id), []).append(float(value))
    return grouped


def event_means(pairs: Sequence[tuple[str, float]]) -> dict[str, float]:
    grouped = group_by_event(pairs)
    return {key: mean(values) for key, values in grouped.items()}


def bootstrap_event_means(
    pairs: Sequence[tuple[str, float]],
    seed: int,
    resamples: int = 2000,
    confidence: float = 0.95,
    minimum_effect: float = 0.0,
) -> dict[str, object]:
    """Percentile bootstrap over independent events, never over correlated rows."""
    per_event = event_means(pairs)
    events = sorted(per_event)
    if not events:
        return {"estimate": None, "interval": None, "events": 0, "resamples": 0,
                "confidence": confidence, "meets_minimum_effect": False}
    estimate = mean([per_event[key] for key in events])
    generator = random.Random(seed)
    sampled: list[float] = []
    for _ in range(resamples):
        chosen = [events[generator.randrange(len(events))] for _ in events]
        sampled.append(mean([per_event[item] for item in chosen]))
    alpha = (1.0 - confidence) / 2.0
    interval = [rounded(percentile(sampled, alpha)), rounded(percentile(sampled, 1.0 - alpha))]
    return {
        "estimate": rounded(estimate),
        "interval": interval,
        "events": len(events),
        "resamples": resamples,
        "confidence": confidence,
        "meets_minimum_effect": bool(interval[0] >= minimum_effect and estimate >= minimum_effect),
    }


def sign_test(wins: int, losses: int, ties: int = 0) -> dict[str, object]:
    """Exact binomial sign test on event-level wins; ties are reported but excluded.

    The study question is directional (does calibration reduce error?), so the
    one-sided p-value is the decision input; the two-sided value is reported for
    readers who prefer a non-directional reading.
    """
    trials = int(wins) + int(losses)
    if trials <= 0:
        return {"wins": int(wins), "losses": int(losses), "ties": int(ties),
                "p_value": 1.0, "p_value_two_sided": 1.0, "direction": "none"}
    one_sided = sum(math.comb(trials, index) for index in range(int(losses) + 1)) / (2 ** trials)
    two_sided = min(1.0, 2.0 * one_sided)
    return {"wins": int(wins), "losses": int(losses), "ties": int(ties),
            "p_value": rounded(one_sided), "p_value_two_sided": rounded(two_sided),
            "direction": "improves" if wins > losses else ("worsens" if losses > wins else "unchanged")}


def correct(method: str, value: float, parameter: dict[str, object] | None) -> float:
    """Apply the candidate correction in observation units; never extrapolate context."""
    if method == "none":
        return float(value)
    if parameter is None:
        raise ValueError(f"candidate {method} has no fitted parameter")
    if method in ("mean_offset", "median_offset"):
        return float(value) - float(parameter["offset"])
    if method == "gain_ratio":
        gain = float(parameter["gain"])
        if not math.isfinite(gain) or gain <= 0:
            raise ValueError("gain must be a positive finite number")
        return float(value) / gain
    raise ValueError(f"unknown calibration method: {method}")


def uniform_sd(bound: float) -> float:
    """Standard deviation of a symmetric ±bound uniform reference-error assumption."""
    return abs(float(bound)) / math.sqrt(3.0) if math.isfinite(float(bound)) else 0.0
