"""Execution deadline contract — one absolute deadline propagated through the stack.

WTS ac4bcb05: the 2026-07-09 incidents showed that every layer owned a
different clock (or none), no layer knew its remaining budget, and prompt
wording like "finish in four minutes" was advisory.  This module is the
single owner of the wall-clock contract:

* The OUTER execution supervisor (delegate_tool for in-process children,
  dd-lane-run for detached ``hermes -z`` specialists) creates ONE absolute
  deadline plus a closeout deadline and hands both down — as env vars for
  process children, as an ``AIAgent.execution_deadline`` attribute for
  in-process children.
* Every consumer asks the same object three questions: how much budget is
  left (``remaining``), has the closeout fuse tripped (``in_closeout``),
  and what is the cap for the call I am about to start (``cap``).
* No deadline configured ⇒ ``from_env`` returns ``None`` and every caller
  behaves exactly as before (AC9: short tasks and gateway turns unaffected).

Env contract (names deliberately avoid the dd-lane-run routing-scrub
pattern ``ROUTE|SESSION|WAKE|RESUME|REPLY|CHAT_ID|THREAD_ID|CHANNEL_ID``
so they survive into specialist children):

    HERMES_DEADLINE_TS       absolute unix epoch (float) — hard ceiling
    HERMES_CLOSEOUT_TS       absolute unix epoch (float) — closeout fuse
    HERMES_EXEC_BUDGET_SECS  relative fallback: budget from process start
    HERMES_CLOSEOUT_FRACTION fraction of budget before closeout (default 0.8)
"""

from __future__ import annotations

import os
import time
from typing import Optional

DEFAULT_CLOSEOUT_FRACTION = 0.8
# Never cap an individual call below this: a 0-second cap turns every call
# into an instant failure and burns the closeout window on retries.
MIN_CALL_CAP_SECS = 5.0

ENV_DEADLINE_TS = "HERMES_DEADLINE_TS"
ENV_CLOSEOUT_TS = "HERMES_CLOSEOUT_TS"
ENV_BUDGET_SECS = "HERMES_EXEC_BUDGET_SECS"
ENV_CLOSEOUT_FRACTION = "HERMES_CLOSEOUT_FRACTION"


def _clamp_fraction(raw: object) -> float:
    try:
        frac = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CLOSEOUT_FRACTION
    return min(0.95, max(0.5, frac))


class ExecutionDeadline:
    """Absolute wall-clock budget with a reserved closeout window."""

    __slots__ = ("created_ts", "deadline_ts", "closeout_ts")

    def __init__(
        self,
        deadline_ts: float,
        closeout_ts: Optional[float] = None,
        created_ts: Optional[float] = None,
    ) -> None:
        self.created_ts = float(created_ts if created_ts is not None else time.time())
        self.deadline_ts = float(deadline_ts)
        if closeout_ts is None:
            budget = max(0.0, self.deadline_ts - self.created_ts)
            closeout_ts = self.created_ts + budget * DEFAULT_CLOSEOUT_FRACTION
        # Closeout can never sit past the hard deadline.
        self.closeout_ts = min(float(closeout_ts), self.deadline_ts)

    # ── constructors ────────────────────────────────────────────────

    @classmethod
    def from_budget(
        cls,
        budget_secs: float,
        closeout_fraction: float = DEFAULT_CLOSEOUT_FRACTION,
        now: Optional[float] = None,
    ) -> "ExecutionDeadline":
        start = float(now if now is not None else time.time())
        budget = max(1.0, float(budget_secs))
        frac = _clamp_fraction(closeout_fraction)
        return cls(
            deadline_ts=start + budget,
            closeout_ts=start + budget * frac,
            created_ts=start,
        )

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> Optional["ExecutionDeadline"]:
        """Build from the env contract; None when no deadline is configured.

        Malformed values are treated as absent rather than raising — a
        broken env var must never take down an agent run.
        """
        e = os.environ if env is None else env
        raw_deadline = e.get(ENV_DEADLINE_TS)
        raw_closeout = e.get(ENV_CLOSEOUT_TS)
        raw_budget = e.get(ENV_BUDGET_SECS)

        deadline_ts: Optional[float] = None
        if raw_deadline:
            try:
                deadline_ts = float(raw_deadline)
            except (TypeError, ValueError):
                deadline_ts = None

        if deadline_ts is None and raw_budget:
            try:
                budget = float(raw_budget)
            except (TypeError, ValueError):
                budget = None
            if budget is not None and budget > 0:
                return cls.from_budget(
                    budget,
                    closeout_fraction=_clamp_fraction(
                        e.get(ENV_CLOSEOUT_FRACTION, DEFAULT_CLOSEOUT_FRACTION)
                    ),
                )

        if deadline_ts is None:
            return None

        closeout_ts: Optional[float] = None
        if raw_closeout:
            try:
                closeout_ts = float(raw_closeout)
            except (TypeError, ValueError):
                closeout_ts = None
        return cls(deadline_ts=deadline_ts, closeout_ts=closeout_ts)

    # ── queries ─────────────────────────────────────────────────────

    def remaining(self, now: Optional[float] = None) -> float:
        t = time.time() if now is None else now
        return self.deadline_ts - t

    def expired(self, now: Optional[float] = None) -> bool:
        return self.remaining(now) <= 0

    def in_closeout(self, now: Optional[float] = None) -> bool:
        t = time.time() if now is None else now
        return t >= self.closeout_ts

    def closeout_remaining(self, now: Optional[float] = None) -> float:
        t = time.time() if now is None else now
        return self.closeout_ts - t

    def cap(self, requested_secs: float, now: Optional[float] = None) -> float:
        """Cap an individual call to the remaining hard budget.

        Never returns less than MIN_CALL_CAP_SECS so an about-to-expire
        run fails through the deadline fuse (a classified terminal
        reason) rather than through a storm of instant call timeouts.
        """
        rem = self.remaining(now)
        return min(float(requested_secs), max(MIN_CALL_CAP_SECS, rem))

    def to_env(self) -> dict:
        """Render as the env contract for a process child."""
        return {
            ENV_DEADLINE_TS: repr(self.deadline_ts),
            ENV_CLOSEOUT_TS: repr(self.closeout_ts),
        }

    def to_dict(self) -> dict:
        return {
            "created_ts": self.created_ts,
            "deadline_ts": self.deadline_ts,
            "closeout_ts": self.closeout_ts,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"ExecutionDeadline(remaining={self.remaining():.1f}s, "
            f"closeout_in={self.closeout_remaining():.1f}s)"
        )


CLOSEOUT_INSTRUCTION = (
    "[EXECUTION CLOSEOUT — MANDATORY] Your execution budget has reached its "
    "closeout window. New investigation and tool calls are now disabled by the "
    "runtime. Immediately produce your final result from what you already have: "
    "state what you established, what remains unknown, and cite concrete "
    "evidence (paths, run ids, log lines) you already collected. If the work is "
    "incomplete, label the result PARTIAL and name the single next step. This "
    "is the last opportunity to return anything at all."
)
