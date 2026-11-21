"""Model choices from saved usage readings, with no provider requests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cockpit import sessions, usage


def unknown() -> dict[str, Any]:
    return {"state": "unknown", "resets_at": None, "remaining_percent": None, "reason": "Usage is not known",
            "observed_at": None, "model_observed_at": None}


def _observation(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def account(windows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """One route's most constrained live window; reset readings await the provider's next observation."""
    live = [each for each in windows if isinstance(each, dict) and isinstance(each.get("used_percentage"), (float, int))
            and isinstance(each.get("resets_at"), (float, int)) and each["resets_at"] > now.timestamp()]
    if not live:
        return unknown()
    observations = [_observation(each.get("captured_at")) for each in live]
    observed_at = min(each for each in observations if each is not None).isoformat() if all(observations) else None
    model_observed_at = observed_at if any(each.get("model_specific") for each in live) else None
    blocked = [each for each in live if each["used_percentage"] >= 100]
    if blocked:
        reset = max(each["resets_at"] for each in blocked)
        return {"state": "limited", "resets_at": datetime.fromtimestamp(reset, UTC).isoformat(),
                "remaining_percent": 0, "reason": "Usage limit reached", "observed_at": observed_at,
                "model_observed_at": model_observed_at}
    tightest = max(live, key=lambda each: each["used_percentage"])
    remaining = max(0, 100 - tightest["used_percentage"])
    return {"state": "low" if tightest["used_percentage"] >= usage.FIVE_HOUR_NEAR else "available",
            "resets_at": datetime.fromtimestamp(tightest["resets_at"], UTC).isoformat(),
            "remaining_percent": remaining, "reason": f"{remaining:g}% usage remaining", "observed_at": observed_at,
            "model_observed_at": model_observed_at}


def combine(routes: list[dict[str, Any]]) -> dict[str, Any]:
    """A usable route keeps the model available; unknown routes prevent an all-routes limit claim."""
    available = [each for each in routes if each["state"] == "available"]
    if available:
        return max(available, key=lambda each: each["remaining_percent"]) | {
            "observed_at": max((each["observed_at"] for each in available if each.get("observed_at")), default=None),
            "model_observed_at": max((each["model_observed_at"] for each in available if each.get("model_observed_at")), default=None),
        }
    if not routes or any(each["state"] == "unknown" for each in routes):
        return unknown()
    low = [each for each in routes if each["state"] == "low"]
    if low:
        return max(low, key=lambda each: each["remaining_percent"]) | {
            "observed_at": max((each["observed_at"] for each in low if each.get("observed_at")), default=None),
            "model_observed_at": max((each["model_observed_at"] for each in low if each.get("model_observed_at")), default=None),
        }
    reset = min((each["resets_at"] for each in routes if each["resets_at"]), default=None)
    return {"state": "limited", "resets_at": reset, "remaining_percent": 0,
            "reason": "Usage limit reached on every available account"}


def model_windows(model: str, windows: list[dict[str, Any]], named: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Only Fable's own model uses the separately reported Fable week."""
    matching = [reading | {"model_specific": True} for name, reading in named.items()
                if name == model or (name == "Fable" and model == "claude-fable-5-2")]
    return [*windows, *matching]


def for_session(observed: sessions.Usage | None, model: str | None, provider: str, capacities: dict[str, Any],
                now: datetime) -> dict[str, Any]:
    """The exact session's latest quota stop can explain its next turn even before saved route readings update."""
    found = capacities.get(model, unknown())
    if provider != "claude" or model is None or observed is None:
        return found
    stop = observed.stop
    if stop is not None and observed.model == model:
        at = datetime.fromisoformat(stop.at)
        available_at = _observation(found.get("observed_at"))
        scoped_at = _observation(found.get("model_observed_at"))
        scoped_current = model != "claude-fable-5-2" or scoped_at is not None and scoped_at > at
        if found["state"] in ("available", "low") and available_at is not None and available_at > at and scoped_current:
            return found
        # Match limits.sync's retry horizon: a cooldown names no quota reset and is retried after 10–80 minutes.
        if stop.cooldown_stops and at + timedelta(minutes=10 * 2 ** (stop.cooldown_stops - 1)) <= now:
            return found
        if at >= now - timedelta(days=7) and (stop.resets is None or stop.resets > now):
            return {"state": "limited", "resets_at": stop.resets.isoformat() if stop.resets else None,
                    "remaining_percent": 0, "reason": "This session stopped on the model's usage limit"}
    return found
