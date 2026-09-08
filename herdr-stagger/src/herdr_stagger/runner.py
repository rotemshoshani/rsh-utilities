"""The chain executor.

Each step waits its own delay measured from the moment the *previous* step
resolved, not from a wall clock computed at launch. That single choice is what
makes a hold shift everything after it: the operator picked a gap between
implementations, and a delayed send must not eat into the next one.
"""

import math
import time
from dataclasses import dataclass, field

PENDING = "PENDING"
SENT = "SENT"
FAILED = "FAILED"
GAVE_UP = "GAVE_UP"
CANCELLED = "CANCELLED"

# `done` is the same idle state as `idle` for an agent whose tab has not been
# seen in the focused UI. Every pane in a stagger run is unwatched by
# definition, so excluding it would hold on almost every step.
PROMPTABLE = ("idle", "done")

DEFAULT_HOLD_CAP = 30 * 60
POLL_SECONDS = 5
TICK_SECONDS = 1


@dataclass
class Result:
    outcome: str = PENDING
    at: float = None
    held_seconds: int = 0
    error: str = None


@dataclass
class Runner:
    steps: list
    hd: object
    clock: object = time.time
    sleeper: object = time.sleep
    observer: object = None
    hold_cap_seconds: int = DEFAULT_HOLD_CAP
    poll_seconds: int = POLL_SECONDS
    tick_seconds: int = TICK_SECONDS

    results: list = field(default_factory=list)
    index: int = 0
    phase: str = "waiting"
    hold_status: str = None
    next_at: float = 0.0
    started_at: float = 0.0

    def __post_init__(self):
        self.results = [Result() for _ in self.steps]

    # --- state the display reads -------------------------------------------

    def seconds_until_next(self):
        return max(0, int(math.ceil(self.next_at - self.clock())))

    def _notify(self):
        if self.observer:
            self.observer(self)

    # --- the loop -----------------------------------------------------------

    def run(self):
        self.started_at = self.clock()
        try:
            for index, step in enumerate(self.steps):
                self.index = index
                self.hold_status = None
                self.phase = "waiting"
                self.next_at = self.clock() + step.after_seconds
                self._wait_until(self.next_at)
                self.results[index] = self._fire(step)
        except KeyboardInterrupt:
            for result in self.results:
                if result.outcome == PENDING:
                    result.outcome = CANCELLED
        self.phase = "done"
        self._notify()
        return self.results

    def _wait_until(self, deadline):
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self._notify()
            self.sleeper(min(self.tick_seconds, remaining))

    def _fire(self, step):
        """Hold until the agent can actually receive a prompt, then send."""
        hold_started = self.clock()
        while True:
            try:
                status = self.hd.agent_status(step.pane_id)
            except self.hd.HerdrError as exc:
                return Result(FAILED, self.clock(), 0, str(exc))

            held = int(self.clock() - hold_started)

            if status is None:
                return Result(
                    FAILED, self.clock(), held, "no agent in pane %s" % step.pane_id
                )

            if status in PROMPTABLE:
                return self._send(step, held)

            if held >= self.hold_cap_seconds:
                return Result(
                    GAVE_UP, self.clock(), held,
                    "still %s after %ds" % (status, held),
                )

            self.phase = "holding"
            self.hold_status = status
            self._notify()
            self.sleeper(self.poll_seconds)

    def _send(self, step, held):
        self.phase = "sending"
        self._notify()
        try:
            self.hd.agent_prompt(step.pane_id, step.prompt)
        except self.hd.HerdrError as exc:
            return Result(FAILED, self.clock(), held, str(exc))
        return Result(SENT, self.clock(), held)
