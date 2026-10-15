"""Steps, pace and distance on the glasses while you walk.

The one sensor nothing else here uses is the accelerometer, and this is
what it is for. The wearer averages 7,700 steps a day (Google Health, last
week), so a glance at pace without pulling a phone out gets used.

HOW STEPS ARE COUNTED. A walking step is a bump in the magnitude of
acceleration: the body rises and falls once per footfall. Magnitude rather
than any single axis, so it does not matter how the glasses sit on the
head. Then:

  * subtract a slow moving average, which removes gravity and whatever
    tilt the head is at -- a HEAD-mounted sensor sees every glance down at
    the pavement as a change in "vertical"
  * a step is a peak above a threshold, at least MIN_STEP_GAP after the
    previous one. The gap is what keeps one footfall from counting twice
    when the signal is ragged, and keeps a nod from counting at all

Timestamps are taken on the HOST when each sample arrives, so the device's
exact send rate (about 10 Hz, varying with what else the loop is doing)
never enters the arithmetic.

What this cannot do is match a phone in a pocket: a head sees a softer
bump than a hip does, so the threshold is lower and the occasional
vigorous nod will score a step. It is a walk gauge, not a pedometer of
record.
"""

from __future__ import annotations

import math
import time
from collections import deque

from shell.app import App
from shell.gestures import Gesture

# Peaks closer together than this are the same footfall. 0.3 s allows up
# to 200 steps/min, which is a fast run; walking is 90-130.
MIN_STEP_GAP = 0.30
# How far above the moving average counts as a footfall, in g. Measured
# gently on the head, not on the hip, hence the low bar.
STEP_THRESHOLD_G = 0.08
# Samples in the moving average that stands in for gravity + tilt.
BASELINE_SAMPLES = 15
# A typical stride; distance is an estimate and is labelled as one.
STRIDE_M = 0.74
# Cadence is read over this many recent steps.
CADENCE_STEPS = 10


class StepCounter:
    """Pure logic: feed it (t, ax, ay, az), read steps."""

    def __init__(self, threshold: float = STEP_THRESHOLD_G,
                 min_gap: float = MIN_STEP_GAP) -> None:
        self.threshold = threshold
        self.min_gap = min_gap
        self.steps = 0
        self._baseline: deque = deque(maxlen=BASELINE_SAMPLES)
        self._last_step_t = -1.0
        self._prev = 0.0          # previous sample above baseline
        self._rising = False
        self._step_times: deque = deque(maxlen=CADENCE_STEPS)

    def feed(self, t: float, ax: float, ay: float, az: float) -> bool:
        """Returns True if this sample completed a step."""
        magnitude = math.sqrt(ax * ax + ay * ay + az * az)
        self._baseline.append(magnitude)
        if len(self._baseline) < 3:
            return False
        above = magnitude - sum(self._baseline) / len(self._baseline)

        # A peak is the sample where the signal stops rising. Counting at
        # the turn rather than at the threshold crossing means a ragged
        # climb cannot fire several times on the way up.
        stepped = False
        if above > self._prev:
            self._rising = True
        elif self._rising:
            self._rising = False
            if (self._prev >= self.threshold
                    and t - self._last_step_t >= self.min_gap):
                self.steps += 1
                self._last_step_t = t
                self._step_times.append(t)
                stepped = True
        self._prev = above
        return stepped

    @property
    def cadence_spm(self) -> float:
        """Steps per minute over the last few steps; 0 when standing."""
        if len(self._step_times) < 2:
            return 0.0
        span = self._step_times[-1] - self._step_times[0]
        if span <= 0:
            return 0.0
        recent = (len(self._step_times) - 1) / span * 60.0
        # Standing still for a while means the last steps are stale.
        if time.monotonic() - self._step_times[-1] > 3.0:
            return 0.0
        return recent

    @property
    def distance_m(self) -> float:
        return self.steps * STRIDE_M


def pace_text(spm: float) -> str:
    if spm <= 0:
        return "standing"
    if spm < 80:
        return "strolling"
    if spm < 115:
        return "walking"
    if spm < 140:
        return "brisk"
    return "running"


class Walk(App):
    def __init__(self) -> None:
        super().__init__(name="walk", label="Walk", tick_seconds=1.0)
        self.counter = StepCounter()
        self.started_at = 0.0
        self.streaming = False

    def on_imu(self, sample) -> None:
        """Called by the shell with each IMU sample (host-timestamped)."""
        acc = getattr(sample, "accel", None) or getattr(sample, "acc", None)
        if acc is None:
            return
        try:
            ax, ay, az = float(acc[0]), float(acc[1]), float(acc[2])
        except (TypeError, IndexError, ValueError):
            return
        self.counter.feed(time.monotonic(), ax, ay, az)

    def view(self) -> list[str]:
        elapsed = time.monotonic() - self.started_at if self.started_at else 0.0
        mins, secs = divmod(int(elapsed), 60)
        spm = self.counter.cadence_spm
        km = self.counter.distance_m / 1000.0
        dist = (f"{self.counter.distance_m:.0f} m" if km < 1.0
                else f"{km:.2f} km")
        rows = [
            f"{self.counter.steps} steps",
            f"{dist}  ~",                       # the ~ says "estimate"
            f"{spm:.0f}/min  {pace_text(spm)}",
            "",
            f"{mins}:{secs:02d}",
            "",
            "2 taps resets" if self.streaming else "no motion sensor here",
        ]
        return rows

    async def draw(self) -> None:
        await self.screen.page(self.view(), heading=True)

    async def start(self) -> None:
        self.counter = StepCounter()
        self.started_at = time.monotonic()
        shell = self.context.get("shell")
        self.streaming = bool(shell) and await shell.start_imu(self.on_imu)
        await self.draw()

    async def tick(self) -> None:
        await self.draw()

    async def stop(self) -> None:
        shell = self.context.get("shell")
        if shell is not None:
            await shell.stop_imu()
        self.streaming = False

    async def gesture(self, gesture: Gesture) -> bool:
        if gesture is Gesture.SELECT:
            self.counter = StepCounter()
            self.started_at = time.monotonic()
            await self.draw()
        return True
