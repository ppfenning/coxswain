"""The pure pacing planner: given a spend window and a policy, what a run may
spend next. No clock, network or filesystem here — the edge module in a
later phase gathers the real window (from ccusage, a run log) and hands it
in as plain data; `now` is always supplied by the caller.

The provider profile is a ceiling, never a setting: `assess` only ever
tightens `tier_ceiling`/`effort_ceiling` down a ladder read from `policy`,
and never loosens them back up within one call. Raw spend outranks pace:
`spent_fraction >= policy.hard_stop_fraction` returns `stop` before the pace
ratio is even computed, so a window that is merely on pace still stops once
it has actually spent the ceiling. Short of that, `stop` outranks `hold`: a
pace ratio that has climbed past the last rung either ladder can offer
returns `stop` before headroom is even considered, so a temporary spike in
`burn_usd_per_hour` can never mask the harsher pace verdict as the softer
one. The last combined rung (cheapest tier, lowest effort) is still
`go_degraded`; `stop` only fires one threshold beyond that. The pace ratio
is noise at the start of a window, so it is judged only once
`elapsed_fraction >= policy.min_elapsed_fraction`; before that only the hard
stop applies. The burn projection may hold only past `PROJECTION_MIN_ELAPSED`,
and that hold carries no `hold_until`: it is re-assessed on every call.

A calendar-month budget is a further limit, given as `month`. It stops at
`policy.weekly_hard_stop_fraction`; short of that its own verdict maps on_track
to go, over_pace to go_degraded at rung 1 and over_budget to stop. With no
usable ceiling on either window the month alone decides. Otherwise every limit
is judged and the strictest verdict wins: stop, hold, go_degraded, go.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from agent_tools.budget import MonthBudget

__all__ = ["Assessment", "Policy", "WeeklyPace", "Window", "assess", "weekly_pace"]

WEEKLY_MIN_ELAPSED = 0.05
# A burn-rate projection is held on only once this share of the window has elapsed.
PROJECTION_MIN_ELAPSED = 0.25
# `usage_meter.as_window` builds percent-scale windows with exactly this ceiling; dollar windows come from `usage_window`.
PERCENT_SCALE_CEILING = 100.0


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    spent_usd: float
    ceiling_usd: float | None
    burn_usd_per_hour: float
    runs_in_flight: int  # carried for a future concurrency-aware ladder; assess does not read it yet


@dataclass(frozen=True)
class Policy:
    # `pace_thresholds` wants one entry per rung of the combined ladder --
    # (len(tier_ladder) - 1) + (len(effort_ladder) - 1) entries -- to reach
    # the fully degraded rung (cheapest tier, lowest effort) as
    # `go_degraded`, plus one more threshold beyond that for `stop` to be
    # reachable by pace alone. Fewer thresholds than the ladder's own step
    # count just means the ladder never fully degrades by pace; the module
    # never assumes the two counts agree.
    pace_thresholds: tuple[float, ...]
    tier_ladder: tuple[str, ...]
    effort_ladder: tuple[str, ...]
    min_headroom_usd: float
    hard_stop_fraction: float = 0.99
    weekly_hard_stop_fraction: float = 0.93
    min_elapsed_fraction: float = 0.10
    weekly_target_fraction: float = 0.90
    # (ratio bound, lane fraction) rungs, read in order by `weekly_pace`.
    weekly_pace_lanes: tuple[tuple[float, float], ...] = ((0.8, 1.0), (1.0, 1.0), (1.15, 0.5), (1.3, 0.25))


@dataclass(frozen=True)
class WeeklyPace:
    target_fraction: float | None
    ratio: float | None
    lane_fraction: float
    verdict: str
    reason: str


@dataclass(frozen=True)
class Assessment:
    spent_fraction: float | None
    elapsed_fraction: float
    projected_total: float
    headroom_usd: float | None
    verdict: str
    tier_ceiling: str
    effort_ceiling: str
    hold_until: datetime | None
    reason: str
    month_verdict: str | None = None
    month_spent_usd: float | None = None
    month_projected_usd: float | None = None


def _elapsed_fraction(window: Window, now: datetime) -> float:
    span = (window.end - window.start).total_seconds()
    if span <= 0:
        return 1.0 if now >= window.end else 0.0
    return min(1.0, max(0.0, (now - window.start).total_seconds() / span))


def _projected_total(window: Window, now: datetime) -> float:
    hours_remaining = max(0.0, (window.end - now).total_seconds() / 3600)
    return window.spent_usd + window.burn_usd_per_hour * hours_remaining


def _projection_holds(headroom: float, elapsed_fraction: float, policy: Policy) -> bool:
    """Choice: the burn projection may hold only after PROJECTION_MIN_ELAPSED (never earlier than min_elapsed_fraction), when an early burst no longer dominates it."""
    judged = elapsed_fraction >= max(policy.min_elapsed_fraction, PROJECTION_MIN_ELAPSED)
    return judged and headroom < policy.min_headroom_usd


def _amount(value: float, window: Window) -> str:
    """Unit follows the window: a ceiling of PERCENT_SCALE_CEILING is a meter percentage, anything else is dollars. A dollar ceiling of exactly 100 is misread as percent."""
    if window.ceiling_usd == PERCENT_SCALE_CEILING:
        return f"{value:.2f}%"
    return f"${value:.2f}"


def _ratio(spent_fraction: float, elapsed_fraction: float) -> float:
    """Pace: how far ahead of the clock the spend is running."""
    if elapsed_fraction > 0:
        return spent_fraction / elapsed_fraction
    if spent_fraction > 0:
        return float("inf")
    return 0.0


def _rung(ratio: float, thresholds: tuple[float, ...]) -> int:
    return sum(1 for t in thresholds if ratio > t)


def _ladder_ceilings(rung: int, policy: Policy) -> tuple[str, str]:
    """Walk `rung` steps down tier first, then effort, never past either
    ladder's last entry — the cheapest rung both ladders can reach."""
    tier_steps = len(policy.tier_ladder) - 1
    tier_idx = min(rung, tier_steps)
    effort_idx = min(max(rung - tier_steps, 0), len(policy.effort_ladder) - 1)
    return policy.tier_ladder[tier_idx], policy.effort_ladder[effort_idx]


def _unmeasured(window: Window, policy: Policy, elapsed_fraction: float, now: datetime) -> Assessment:
    return Assessment(
        spent_fraction=None,
        elapsed_fraction=elapsed_fraction,
        projected_total=_projected_total(window, now),
        headroom_usd=None,
        verdict="go",
        tier_ceiling=policy.tier_ladder[0],
        effort_ceiling=policy.effort_ladder[0],
        hold_until=None,
        reason="window is unmeasured: no usable ceiling_usd; reporting pace only",
    )


def _weekly_fraction(weekly: Window | None) -> float | None:
    if weekly is None or weekly.ceiling_usd is None or weekly.ceiling_usd <= 0:
        return None
    return weekly.spent_usd / weekly.ceiling_usd


def _weekly_stop(window: Window, policy: Policy, now: datetime, elapsed_fraction: float,
                  weekly_fraction: float) -> Assessment:
    projected_total = _projected_total(window, now)
    spent_fraction = window.spent_usd / window.ceiling_usd if window.ceiling_usd else None
    headroom_usd = window.ceiling_usd - projected_total if window.ceiling_usd else None
    return Assessment(
        spent_fraction=spent_fraction, elapsed_fraction=elapsed_fraction,
        projected_total=projected_total, headroom_usd=headroom_usd,
        verdict="stop", tier_ceiling=policy.tier_ladder[-1], effort_ceiling=policy.effort_ladder[-1],
        hold_until=None,
        reason=f"weekly spend {weekly_fraction:.0%} of weekly ceiling (hard stop at {policy.weekly_hard_stop_fraction:.0%})",
    )


def _lane_fraction(ratio: float, lanes: tuple[tuple[float, float], ...]) -> float:
    """Interior bounds are inclusive so a ratio of exactly 1.0 (on pace) keeps
    its lane; at or above the last bound the lane is 0."""
    if not lanes:
        return 1.0
    for bound, fraction in lanes[:-1]:
        if ratio <= bound:
            return fraction
    last_bound, last_fraction = lanes[-1]
    return last_fraction if ratio < last_bound else 0.0


def _weekly_verdict(lane_fraction: float) -> str:
    if lane_fraction >= 1.0:
        return "full"
    return "hold" if lane_fraction <= 0 else "reduced"


def _not_judged(reason: str, target_fraction: float | None = None) -> WeeklyPace:
    return WeeklyPace(target_fraction=target_fraction, ratio=None, lane_fraction=1.0,
                      verdict="not_judged", reason=reason)


def weekly_pace(weekly: Window | None, policy: Policy, now: datetime) -> WeeklyPace:
    """Pure: the lane fraction the weekly spend pace allows at `now`. The weekly
    hard stop is not part of this verdict; `assess` keeps it."""
    spent_fraction = _weekly_fraction(weekly)
    if weekly is None or spent_fraction is None:
        return _not_judged("no usable weekly window or ceiling; pace not judged")
    elapsed = _elapsed_fraction(weekly, now)
    target = policy.weekly_target_fraction * elapsed
    if elapsed < WEEKLY_MIN_ELAPSED:
        return _not_judged(f"week {elapsed:.0%} elapsed, under {WEEKLY_MIN_ELAPSED:.0%}; pace not judged", target)
    if target <= 0:
        return _not_judged("weekly target is zero; pace not judged", target)
    ratio = spent_fraction / target
    lane = _lane_fraction(ratio, policy.weekly_pace_lanes)
    return WeeklyPace(
        target_fraction=target, ratio=ratio, lane_fraction=lane, verdict=_weekly_verdict(lane),
        reason=f"weekly spend {spent_fraction:.0%} is {ratio:.2f}x the {target:.0%} target; lanes at {lane:.0%}",
    )


# verdict -> (combined ladder rung, or None for stop). Strictness order is in _STRICTNESS.
_MONTH_VERDICTS: dict[str, int | None] = {"on_track": 0, "over_pace": 1, "over_budget": None}
_STRICTNESS = {"stop": 3, "hold": 2, "go_degraded": 1, "go": 0}


def _month_usable(month: MonthBudget | None) -> bool:
    """A month with no budget, a zero budget (fraction None) or a verdict outside the mapped three is not judged."""
    return month is not None and month.fraction is not None and month.verdict in _MONTH_VERDICTS


def _month_assessment(month: MonthBudget, policy: Policy) -> Assessment:
    fraction = month.fraction or 0.0
    budget = month.budget_usd or 0.0
    rung = _MONTH_VERDICTS[month.verdict]
    narration = (f"month spent {fraction:.0%} of budget at {month.elapsed_fraction:.0%} elapsed, "
                 f"projected ${month.projected_usd:.2f} of ${budget:.2f}")
    if fraction >= policy.weekly_hard_stop_fraction:
        verdict, reason = "stop", f"month spend {fraction:.0%} of monthly budget (hard stop at {policy.weekly_hard_stop_fraction:.0%})"
    elif rung is None:
        verdict, reason = "stop", f"{narration}; budget spent"
    elif rung == 0:
        verdict, reason = "go", f"{narration}; on track"
    else:
        verdict, reason = "go_degraded", f"{narration}; projected over budget"
    if verdict == "stop":
        tier_ceiling, effort_ceiling = policy.tier_ladder[-1], policy.effort_ladder[-1]
    else:
        tier_ceiling, effort_ceiling = _ladder_ceilings(rung or 0, policy)
    return Assessment(
        spent_fraction=fraction, elapsed_fraction=month.elapsed_fraction,
        projected_total=month.projected_usd, headroom_usd=budget - month.projected_usd,
        verdict=verdict, tier_ceiling=tier_ceiling, effort_ceiling=effort_ceiling,
        hold_until=None, reason=reason,
        month_verdict=month.verdict, month_spent_usd=month.spent_usd, month_projected_usd=month.projected_usd,
    )


def assess(window: Window, policy: Policy, now: datetime, weekly: Window | None = None,
           month: MonthBudget | None = None) -> Assessment:
    """Pure: the one verdict the limits and a policy make at `now`. Reports an
    unmeasured window rather than guessing a ceiling for it. `weekly`, when
    given and measured, can force `stop` on its own hard-stop fraction ahead
    of the window's own verdict; short of that, its fraction still rides
    along in `reason` so every caller narrates both windows at once. `month`,
    when it has a budget, is judged too: it alone decides when neither window
    has a usable ceiling, otherwise the strictest verdict wins and a tie keeps
    the window's result, with the month appended to its reason."""
    if not _month_usable(month):
        return _assess_windows(window, policy, now, weekly)
    month_result = _month_assessment(month, policy)
    window_unusable = window.ceiling_usd is None or window.ceiling_usd <= 0
    if window_unusable and _weekly_fraction(weekly) is None:
        return month_result
    windows_result = _assess_windows(window, policy, now, weekly)
    winner = max((windows_result, month_result), key=lambda a: _STRICTNESS[a.verdict])
    if winner is month_result:
        return winner
    return replace(
        winner, reason=f"{winner.reason}; month {month_result.spent_fraction:.0%} of monthly budget, {month.verdict}",
        month_verdict=month.verdict, month_spent_usd=month.spent_usd, month_projected_usd=month.projected_usd,
    )


def _assess_windows(window: Window, policy: Policy, now: datetime, weekly: Window | None) -> Assessment:
    elapsed_fraction = _elapsed_fraction(window, now)
    weekly_fraction = _weekly_fraction(weekly)
    if weekly_fraction is not None and weekly_fraction >= policy.weekly_hard_stop_fraction:
        return _weekly_stop(window, policy, now, elapsed_fraction, weekly_fraction)
    result = _assess_window(window, policy, now, elapsed_fraction)
    if weekly_fraction is None:
        return result
    return replace(result, reason=f"{result.reason}; weekly {weekly_fraction:.0%} of weekly ceiling")


def _assess_window(window: Window, policy: Policy, now: datetime, elapsed_fraction: float) -> Assessment:
    if window.ceiling_usd is None or window.ceiling_usd <= 0:
        return _unmeasured(window, policy, elapsed_fraction, now)

    spent_fraction = window.spent_usd / window.ceiling_usd
    projected_total = _projected_total(window, now)
    headroom_usd = window.ceiling_usd - projected_total

    if spent_fraction >= policy.hard_stop_fraction:
        return Assessment(
            spent_fraction=spent_fraction, elapsed_fraction=elapsed_fraction,
            projected_total=projected_total, headroom_usd=headroom_usd,
            verdict="stop", tier_ceiling=policy.tier_ladder[-1], effort_ceiling=policy.effort_ladder[-1],
            hold_until=None,
            reason=f"spent {spent_fraction:.0%} of ceiling (hard stop at {policy.hard_stop_fraction:.0%})",
        )

    pace_judged = elapsed_fraction >= policy.min_elapsed_fraction
    ratio = _ratio(spent_fraction, elapsed_fraction) if pace_judged else 0.0
    # The last combined ladder index (cheapest tier, lowest effort) is still
    # a degraded rung, not a stop; stop needs one threshold past it.
    max_rung = (len(policy.tier_ladder) - 1) + (len(policy.effort_ladder) - 1)
    rung = _rung(ratio, policy.pace_thresholds)
    pace = f"spent {spent_fraction:.0%} of ceiling at {elapsed_fraction:.0%} elapsed"

    if rung > max_rung:
        return Assessment(
            spent_fraction=spent_fraction, elapsed_fraction=elapsed_fraction,
            projected_total=projected_total, headroom_usd=headroom_usd,
            verdict="stop", tier_ceiling=policy.tier_ladder[-1], effort_ceiling=policy.effort_ladder[-1],
            hold_until=None, reason=f"{pace}; both ladders exhausted",
        )

    tier_ceiling, effort_ceiling = _ladder_ceilings(rung, policy)

    if _projection_holds(headroom_usd, elapsed_fraction, policy):
        return Assessment(
            spent_fraction=spent_fraction, elapsed_fraction=elapsed_fraction,
            projected_total=projected_total, headroom_usd=headroom_usd,
            verdict="hold", tier_ceiling=policy.tier_ladder[-1], effort_ceiling=policy.effort_ladder[-1],
            hold_until=None,
            reason=(f"headroom {_amount(headroom_usd, window)} below minimum "
                    f"{_amount(policy.min_headroom_usd, window)}; hold is re-assessed each tick"),
        )

    if rung == 0:
        return Assessment(
            spent_fraction=spent_fraction, elapsed_fraction=elapsed_fraction,
            projected_total=projected_total, headroom_usd=headroom_usd,
            verdict="go", tier_ceiling=tier_ceiling, effort_ceiling=effort_ceiling,
            hold_until=None,
            reason=f"{pace}; on pace" if pace_judged
            else f"{pace}; pace not judged before {policy.min_elapsed_fraction:.0%} elapsed",
        )

    return Assessment(
        spent_fraction=spent_fraction, elapsed_fraction=elapsed_fraction,
        projected_total=projected_total, headroom_usd=headroom_usd,
        verdict="go_degraded", tier_ceiling=tier_ceiling, effort_ceiling=effort_ceiling,
        hold_until=None, reason=f"{pace}; ceilings tier={tier_ceiling} effort={effort_ceiling}",
    )
