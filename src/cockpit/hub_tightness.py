"""How tight each provider's hub allowance is (SPEC SESSION-28): whether the week its usable accounts have left lasts,
at the use expected for the coming hours, until their resets free more. The sweeper reads it to hold back wake-ups
that can wait while it is tight, and to bring work forward while there is room.

- Room left: each usable account's (signed in and enabled: USABLE) share of its week left now and when the week resets,
  from the limits Cockpit kept (hub_left.rows). At its reset an account has its whole week again.
- Expected use per hour: the level times the shape.
  - The level is the accounts' pace, in percent points of their weeks per hour: each account's rate over its kept
    readings (hub_left.account_lines), added up. It comes from the hub because the hub serves other machines too.
  - The shape is how busy each hour of the day is against the average hour (shape), weekdays and weekend days apart,
    in the user's timezone: from this machine's own record of use per hour over the last SHAPE_SECONDS, Claude's from
    spend.SpendIndex.use_by_hour, Codex's from codex.CodexIndex.hours. It is flat while that record spans less than
    MIN_HISTORY_SECONDS.
- The reading (read): step from now to the latest upcoming weekly reset, AHEAD_SECONDS at most, taking each hour's
  expected use from the accounts as hub_left.pace_lines does (each its own part, an empty account's part shared out
  equally among those with some left). It runs out when an hour's use finds no account with any left.
  - "tight": it runs out; `hours_left` says when.
  - "room": it lasts even at SPARE times the expected use.
  - "normal": in between.
  - Tight too, until the first reset that frees more, while no usable account has week left, or every one with week
    left has used FIVE_HOUR_NEAR or more of its 5-hour window.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from cockpit import hub, hub_left, usage

if TYPE_CHECKING:
    from cockpit.codex import CodexIndex, HubReading
    from cockpit.spend import SpendIndex

HOUR = 3600
# The shape's bins: the 24 hours of a weekday, then the 24 of a weekend day, in the user's timezone. Four weeks give
# each bin about 20 samples on weekdays and 8 on weekends; a bin per hour of the week would have 4.
BINS = 48
# How many days of a week fall in each bin: the shape's average hour is the average over a week.
BIN_DAYS = [5] * 24 + [2] * 24
# The span of use the shape is learned from: the last four weeks.
SHAPE_SECONDS = 28 * 86400
# Below this much recorded use, some hours of the week have no sample, and the shape is flat.
MIN_HISTORY_SECONDS = 7 * 86400
# The farthest the reading looks ahead: a week, after which every account has reset once.
AHEAD_SECONDS = hub_left.WEEK
# "room" means the week left lasts at this many times the expected use: a quarter to spare.
SPARE = 1.25
# The account states the hub sends work to, now or after a rest (hub.state_of).
USABLE = ("working", "cooling")
# The limits a reading is made of, as hub_limit_readings keys them.
WEEK_KEY = "seven_day"
FIVE_HOUR_KEY = "five_hour"
PROVIDERS = ("claude", "codex")
# Use left over from taking, in percent points, below which it counts as all taken: float rounding, not use.
SPILL_ROUNDING = 1e-9


@dataclass(frozen=True)
class Tightness:
    """One provider's reading: "tight", "normal" or "room"; the hours until its week left runs out (None when it
    lasts); the reset that frees more next (after the run-out when tight; None when no reset is known); and why, in a
    sentence."""

    reading: str
    hours_left: float | None
    next_reset: float | None
    why: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "reading": self.reading,
            "hours_left": None if self.hours_left is None else round(self.hours_left, 1),
            "next_reset": None if self.next_reset is None else iso(self.next_reset),
            "why": self.why,
        }


@dataclass
class Account:
    """One usable account in the stepping: its share of the week left, its next reset (None once passed and not read
    again) and its expected use per hour at the average hour, in percent points."""

    left: float
    resets_at: float | None
    per_hour: float


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_hour(at: float, zone: ZoneInfo) -> int:
    """The bin `at` lies in (BINS): its hour of the day in `zone`, plus 24 on a Saturday or Sunday."""
    moment = datetime.fromtimestamp(at, zone)
    return (24 if moment.weekday() >= 5 else 0) + moment.hour


def shape(use: dict[str, float], now: float, zone: ZoneInfo) -> list[float]:
    """Each bin's use (day_hour) against the average hour's over a week (1.0 for an average hour), from `use`: an
    amount per hour, keyed by the hour's start as "YYYY-MM-DDTHH:00:00Z", in any unit. Hours from the first with use,
    or from SHAPE_SECONDS ago if later, to the start of this hour count; each bin's use is its mean over the hours that
    fell in it. Flat while those hours span less than MIN_HISTORY_SECONDS or hold no use."""
    flat = [1.0] * BINS
    end = math.floor(now / HOUR) * HOUR
    by_hour: dict[float, float] = {}
    for key, amount in use.items():
        if amount < 0 or not math.isfinite(amount):
            raise ValueError(f"use of {amount!r} in the hour {key}")
        at = datetime.strptime(key, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
        if end - SHAPE_SECONDS <= at < end and amount > 0:
            by_hour[at] = amount
    if not by_hour or end - min(by_hour) < MIN_HISTORY_SECONDS:
        return flat
    start = min(by_hour)
    totals = [0.0] * BINS
    counts = [0] * BINS
    for at in range(int(start), int(end), HOUR):
        totals[day_hour(at, zone)] += by_hour.get(at, 0.0)
        counts[day_hour(at, zone)] += 1
    means = [total / count for total, count in zip(totals, counts, strict=True)]
    average = sum(mean * days for mean, days in zip(means, BIN_DAYS, strict=True)) / sum(BIN_DAYS)
    return [each / average for each in means]


def run_out(
    accounts: list[Account], hour_shape: list[float], zone: ZoneInfo, factor: float, now: float, end: float
) -> float | None:
    """When the `accounts` have no week left if use goes on at `factor` times the expected use, stepping from `now` to
    `end`; None if it lasts. Each account takes its own part of an hour's use and an equal part of the parts of the
    accounts with nothing left, and none below 0; at its reset an account has its whole week again."""
    lefts = [each.left for each in accounts]
    resets = [each.resets_at for each in accounts]
    marks = {end, *(t for t in resets if t is not None and now < t < end)}
    # The starts of the hours in `zone`, where the shape may change: a zone's offset can hold a half or quarter hour.
    shift = -round(datetime.fromtimestamp(now, zone).utcoffset().total_seconds()) % HOUR
    first = math.floor((now - shift) / HOUR) * HOUR + shift + HOUR
    marks |= {float(t) for t in range(first, math.ceil(end), HOUR) if t < end}
    t = now
    for then in sorted(marks):
        span = (then - t) / HOUR
        wanted = [each.per_hour * hour_shape[day_hour(t, zone)] * factor * span for each in accounts]
        unmet = take(lefts, wanted)
        if unmet > 0:
            total = sum(wanted)
            return then - (then - t) * unmet / total
        for i, reset in enumerate(resets):
            if reset == then:
                lefts[i], resets[i] = 100.0, None
        t = then
    return None


def take(lefts: list[float], wanted: list[float]) -> float:
    """Take the `wanted` use from `lefts` in place: each account its own, and what an account with nothing left cannot
    take shared out equally among those with some left, again until all is taken or none has any left. Returns the
    use no account could take."""
    own = list(wanted)
    while True:
        spill = 0.0
        for i, value in enumerate(lefts):
            taken = min(value, own[i])
            lefts[i] = value - taken
            spill += own[i] - taken
        with_some = [i for i, value in enumerate(lefts) if value > 0]
        if spill <= SPILL_ROUNDING:
            return 0.0
        if not with_some:
            return spill
        own = [spill / len(with_some) if i in with_some else 0.0 for i in range(len(lefts))]


def read(
    accounts: list[str], readings: list[hub_left.Reading], use: dict[str, float], zone: ZoneInfo, now: float
) -> Tightness | None:
    """The reading at `now` for one provider's usable `accounts` (by hub.limits_key), from their kept `readings`
    (hub_left.rows) and this machine's `use` per hour of that provider (shape, in `zone`). None when no account's week is known."""
    by_limit: dict[tuple[str, str], list[hub_left.Reading]] = {}
    for reading in sorted(readings, key=lambda each: each.at):
        if reading.at <= now:
            by_limit.setdefault((reading.account, reading.limit_key), []).append(reading)
    if not accounts:
        return Tightness("tight", 0.0, None, "No account is signed in and enabled.")
    weeks = []
    fives = []
    for key in accounts:
        if (key, WEEK_KEY) not in by_limit:
            continue
        lines = hub_left.account_lines(by_limit[key, WEEK_KEY], now)
        if lines.rate < 0 or not math.isfinite(lines.rate):
            raise ValueError(f"{key} has a pace of {lines.rate!r}")
        weeks.append(Account(lines.left_now, lines.resets_at, lines.rate * HOUR))
        five = by_limit.get((key, FIVE_HOUR_KEY))
        if lines.left_now > 0:
            fives.append(five[-1] if five and five[-1].resets_at > now else None)
    if not weeks:
        return None
    resets = [each.resets_at for each in weeks if each.resets_at is not None]
    # The weekly resets that give an account with nothing left its week again.
    refills = [each.resets_at for each in weeks if each.left <= 0 and each.resets_at is not None]
    if not fives:
        return Tightness("tight", 0.0, min(refills, default=None), "Every account has used its whole week.")
    if all(each is not None and each.used >= usage.FIVE_HOUR_NEAR for each in fives):
        reset = min([*(each.resets_at for each in fives if each is not None), *refills])
        return Tightness(
            "tight", 0.0, reset,
            f"Every account with week left has used {usage.FIVE_HOUR_NEAR} % or more of its 5-hour window.",
        )
    end = min(max(resets, default=now + AHEAD_SECONDS), now + AHEAD_SECONDS)
    hour_shape = shape(use, now, zone)
    out_at = run_out(weeks, hour_shape, zone, 1.0, now, end)
    if out_at is not None:
        after = min((t for t in resets if t > out_at), default=None)
        return Tightness(
            "tight", (out_at - now) / HOUR, after,
            "At the expected use the week left runs out before a reset frees more.",
        )
    first = min(resets, default=None)
    if run_out(weeks, hour_shape, zone, SPARE, now, end) is None:
        return Tightness(
            "room", None, first, "The week left lasts until the resets even at a quarter more than the expected use."
        )
    return Tightness(
        "normal", None, first, "The week left lasts until the resets at the expected use, but not at a quarter more."
    )


def provider_uses(
    spend_index: SpendIndex, codex_index: CodexIndex, hub_codex: list[HubReading], now: datetime
) -> dict[str, dict[str, float]]:
    """This machine's use per hour over the last SHAPE_SECONDS, per provider, for the shape: Claude's in dollars at list
    prices, each call in the hour it ran; Codex's in its share of a weekly limit (codex.AccountIndex._week_shares), from
    the hub's readings of its Codex accounts `hub_codex` (hub_left.codex_weeks)."""
    since = datetime.fromtimestamp(now.timestamp() - SHAPE_SECONDS, UTC)
    codex = {hour["hour"]: float(hour["week_percent"]) for hour in codex_index.hours(since, hub_codex)}
    return {"claude": spend_index.use_by_hour(since), "codex": codex}


def readings(
    accounts: list[dict[str, Any]],
    rows: list[hub_left.Reading],
    uses: dict[str, dict[str, float]],
    zone: ZoneInfo,
    now: float,
) -> dict[str, dict[str, Any]]:
    """Each provider's reading, as the page and `cockpit get` show it, from the hub read's `accounts` (hub.Hub.read),
    the kept limits' `rows`, the use per hour by provider (provider_uses) and the user's `zone`. A provider with no
    account and one whose usable accounts have no known week are left out."""
    found = {}
    for provider in PROVIDERS:
        mine = [each for each in accounts if each["provider"] == provider]
        if not mine:
            continue
        usable = [hub.limits_key(each) for each in mine if each["state"] in USABLE]
        reading = read(usable, rows, uses[provider], zone, now)
        if reading is not None:
            found[provider] = reading.as_dict()
    return found
