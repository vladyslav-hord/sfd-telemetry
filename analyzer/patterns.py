from __future__ import annotations

import statistics
import hashlib
import json
from collections import defaultdict

from .features import movement_features


def robust_z(value: float, values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    median = statistics.median(values)
    mad = statistics.median(abs(item - median) for item in values)
    return 0.0 if mad == 0 else 0.6745 * (value - median) / mad


def candidate_state(occurrences: int, players: int, rounds: int, agreement: float, confidence: float, artifact_rate: float, confirmation_occurrences: int = 12) -> str:
    if occurrences < 3 or players < 2 or rounds < 2:
        return "candidate"
    if occurrences >= confirmation_occurrences and players >= 3 and rounds >= 3 and agreement >= .8 and confidence >= .75 and artifact_rate < .1:
        return "active"
    return "candidate"


def should_drift(audit_agreement: float | None, population_stability_index: float, version_changed: bool) -> bool:
    return version_changed or population_stability_index > .25 or (audit_agreement is not None and audit_agreement < .75)


def window_signature(features: dict) -> str:
    stable = {key: round(float(value), 2) for key, value in features.items() if isinstance(value, (int, float))}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:24]


def detect_windows(rows: list[dict], z_threshold: float, min_coverage: float) -> list[dict]:
    """Returns candidates only; it never makes moderation or gameplay decisions."""
    values = [row["features"].get("max_speed", 0.0) for row in rows]
    candidates = []
    isolation = [False] * len(rows)
    if len(rows) >= 20:
        try:
            from sklearn.ensemble import IsolationForest
            model = IsolationForest(contamination=.01, random_state=0)
            matrix = [[row["features"].get(key, 0.0) for key in ("distance", "mean_speed", "max_speed", "max_acceleration", "direction_switches")] for row in rows]
            # numpy.bool_ is not JSON serializable and reports must remain pure JSON.
            isolation = [bool(prediction == -1) for prediction in model.fit_predict(matrix)]
        except (ImportError, ValueError):
            pass
    for index, row in enumerate(rows):
        coverage = row["features"].get("coverage", 0.0)
        score = robust_z(values[index], values)
        if coverage < min_coverage:
            continue
        if (score is not None and abs(score) >= z_threshold) or isolation[index]:
            candidates.append({**row, "robust_z": score, "isolation_forest": isolation[index], "signature": window_signature(row["features"])})
    return candidates


def group_candidate_signatures(windows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for window in windows: grouped[window["signature"]].append(window)
    return grouped
