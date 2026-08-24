from __future__ import annotations

import math
from collections import Counter


def movement_features(samples: list[dict], static_speed_threshold: float = 1.0) -> dict:
    """Derive movement only from adjacent ordered state samples."""
    if len(samples) < 2:
        return {"sample_count": len(samples), "distance": 0.0, "active_distance": 0.0, "static_seconds": 0.0, "coverage": 0.0}
    distance = active = static_seconds = airtime = vertical = 0.0
    speeds, accelerations, switches = [], [], 0
    previous_heading = None
    for previous, current in zip(samples, samples[1:]):
        dt = max(0.0, min(10.0, float(current["game_ms"] - previous["game_ms"]) / 1000))
        dx, dy = (current.get("x") or 0) - (previous.get("x") or 0), (current.get("y") or 0) - (previous.get("y") or 0)
        step = math.hypot(dx, dy)
        speed = step / dt if dt else 0.0
        distance += step
        speeds.append(speed)
        if speed < static_speed_threshold: static_seconds += dt
        else: active += step
        vertical += abs(dy)
        if previous_heading is not None and dx * previous_heading[0] + dy * previous_heading[1] < 0:
            switches += 1
        if step:
            previous_heading = (dx / step, dy / step)
        state = current.get("state_json") or {}
        if isinstance(state, str):
            import json
            try: state = json.loads(state)
            except json.JSONDecodeError: state = {}
        if state.get("is_airborne") or state.get("airborne"):
            airtime += dt
    accelerations = [abs(speeds[i] - speeds[i - 1]) for i in range(1, len(speeds))]
    return {"sample_count": len(samples), "distance": distance, "active_distance": active, "mean_speed": sum(speeds) / len(speeds), "max_speed": max(speeds), "mean_acceleration": sum(accelerations) / len(accelerations) if accelerations else 0.0, "max_acceleration": max(accelerations, default=0.0), "direction_switches": switches, "vertical_displacement": vertical, "airtime_seconds": airtime, "static_seconds": static_seconds, "coverage": 1.0}


def state_durations(samples: list[dict]) -> dict:
    durations: Counter[str] = Counter()
    for previous, current in zip(samples, samples[1:]):
        dt = max(0.0, min(10.0, (float(current["game_ms"]) - float(previous["game_ms"])) / 1000))
        state = previous.get("state_json") or {}
        if isinstance(state, str):
            import json
            try: state = json.loads(state)
            except json.JSONDecodeError: state = {}
        for key, value in state.items():
            if isinstance(value, bool) and value:
                durations[key] += dt
    return dict(durations)


def action_ngrams(actions: list[str], minimum: int = 2, maximum: int = 6) -> dict[str, int]:
    result: Counter[str] = Counter()
    for size in range(minimum, maximum + 1):
        for index in range(len(actions) - size + 1):
            result[">".join(actions[index:index + size])] += 1
    return dict(result)
