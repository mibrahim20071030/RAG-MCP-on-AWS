"""The time budget for one tool call.

The API gateway stops waiting after 30 s and answers with a 504 the client cannot read. A `Deadline` counts down
from when the tool starts, so each step can be given a timeout that fits in what is left, and a step that cannot
fit is refused with a readable ToolError instead.

How a step uses it: first `require(minimum_s, message)`, then pass `cap(step_limit_s)` as the step's timeout.
`require` first, because `cap` returns 0.0 when no time is left, and several libraries read a timeout of 0 as
"wait forever" (libpq's connect_timeout) or "do not wait at all". So never pass a `cap` value on without `require`.
"""
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from mcp.server.mcpserver.exceptions import ToolError


@dataclass(frozen=True)
class Budget:
    """The time budget of one tool call and the longest each slow step can take (Layer 7).

    `connect_s` is the database connect's worst case (attempts x timeout, plus the pauses between attempts);
    `bedrock_s` is one Bedrock call's (attempts x (connect timeout + read timeout)). The tool starts a step only when
    its worst case fits in the time left, so a call ends close to `total_s` whatever the steps do. Not counted:
    the usage counters (normally milliseconds; the rerank check adds the slot counter's worst case), the search
    query after the connect (about 1 s; it has no timeout of its own) and the daily place given back at the end.

    Refuses (ValueError) settings where a connect, a question embed and a rerank cannot all fit in `total_s`: a
    missing or wrong Lambda setting (for example the local default of 3 connect attempts of 60 s) then stops the
    server at its first call, instead of refusing every cold call or ending in the gateway's bare 504."""
    total_s: float
    connect_s: float
    bedrock_s: float

    def __post_init__(self):
        for name in ("total_s", "connect_s", "bedrock_s"):
            value = getattr(self, name)
            if (not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value)
                    or value <= 0):
                raise ValueError(f"{name} must be a finite number above 0, got {value!r}")
        if self.connect_s + 2 * self.bedrock_s > self.total_s:
            raise ValueError(f"the steps cannot fit in the {self.total_s} s budget: connect {self.connect_s} s + "
                             f"embed {self.bedrock_s} s + rerank {self.bedrock_s} s; lower the timeouts or attempts")


class Deadline:
    def __init__(self, total_s: float, clock: Callable[[], float] = time.monotonic):
        """Start the countdown now. The default clock is monotonic: it does not jump when the system time changes.
        The clock can be passed in so tests use a fake one."""
        if not math.isfinite(total_s) or total_s <= 0:
            raise ValueError(f"total_s must be a finite number above 0, got {total_s}")
        self._clock = clock
        self._end = clock() + total_s

    def remaining(self) -> float:
        """Seconds left, never below 0."""
        return max(0.0, self._end - self._clock())

    def cap(self, step_limit_s: float, margin_s: float = 1.0) -> float:
        """The timeout one step may use: its own limit, or what is left minus a margin for the reply, if that is less.

        0.0 means no time is left; it is not a usable timeout (see the module docstring: call `require` first).
        """
        if not math.isfinite(step_limit_s) or step_limit_s < 0 or not math.isfinite(margin_s) or margin_s < 0:
            raise ValueError(f"step_limit_s and margin_s must be finite and not negative, got {step_limit_s}, {margin_s}")
        return float(max(0.0, min(step_limit_s, self.remaining() - margin_s)))

    def require(self, minimum_s: float, message: str, error: type[ToolError] = ToolError) -> None:
        """Refuse to start a step that needs at least `minimum_s` seconds when less than that is left.

        `minimum_s` should include the margin `cap` takes off, so a step that passes here gets at least what it
        needs from `cap`. `message` reaches Claude word for word: make it readable, with a retry hint, and never put
        exception text or other internal detail in it. `error` is the ToolError class raised (the tool passes
        limits.NotPaid for steps before a paid call, so the daily place is given back).
        """
        if self.remaining() < minimum_s:
            raise error(message)
