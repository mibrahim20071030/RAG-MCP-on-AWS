"""Project 3 Layer 3 tests for datasheet_mcp.budget.Deadline. The clock is a fake the test moves forward, so nothing
sleeps."""
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from datasheet_mcp.budget import Deadline


class FakeClock:
    """A clock that only moves when the test calls `advance`. It counts how often it was read."""

    def __init__(self, now=1000.0):
        self.now = now
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


# remaining


def test_remaining_starts_at_total(clock):
    assert Deadline(28.0, clock=clock).remaining() == pytest.approx(28.0)


def test_remaining_counts_down_as_the_clock_moves(clock):
    deadline = Deadline(28.0, clock=clock)
    clock.advance(10.0)
    assert deadline.remaining() == pytest.approx(18.0)
    clock.advance(17.5)
    assert deadline.remaining() == pytest.approx(0.5)


def test_remaining_is_zero_at_the_end_and_never_below(clock):
    deadline = Deadline(5.0, clock=clock)
    clock.advance(5.0)
    assert deadline.remaining() == 0.0
    clock.advance(100.0)
    assert deadline.remaining() == 0.0


def test_clock_is_read_at_construction(clock):
    # Time that passed before the deadline was made does not count against it.
    clock.advance(500.0)
    deadline = Deadline(10.0, clock=clock)
    assert deadline.remaining() == pytest.approx(10.0)
    clock.advance(4.0)
    assert deadline.remaining() == pytest.approx(6.0)


def test_default_clock_works_without_a_fake():
    # The real clock (time.monotonic): a fresh deadline has close to all its time left.
    deadline = Deadline(30.0)
    assert 29.0 < deadline.remaining() <= 30.0


# cap


def test_cap_gives_the_step_limit_when_plenty_is_left(clock):
    assert Deadline(28.0, clock=clock).cap(10.0) == pytest.approx(10.0)


def test_cap_gives_remaining_minus_margin_when_less_is_left(clock):
    deadline = Deadline(28.0, clock=clock)
    clock.advance(22.0)  # 6 s left, 5 s after the 1 s margin
    assert deadline.cap(10.0) == pytest.approx(5.0)


def test_cap_at_the_boundary_gives_the_step_limit(clock):
    deadline = Deadline(11.0, clock=clock)  # 11 - 1 margin == the step limit exactly
    assert deadline.cap(10.0) == pytest.approx(10.0)


def test_cap_is_zero_when_less_than_the_margin_is_left(clock):
    deadline = Deadline(28.0, clock=clock)
    clock.advance(27.5)  # 0.5 s left, below the 1 s margin
    assert deadline.cap(10.0) == 0.0


def test_cap_is_zero_when_exactly_the_margin_is_left(clock):
    deadline = Deadline(28.0, clock=clock)
    clock.advance(27.0)
    assert deadline.cap(10.0) == pytest.approx(0.0)


def test_cap_is_zero_when_time_is_spent(clock):
    deadline = Deadline(28.0, clock=clock)
    clock.advance(60.0)
    assert deadline.cap(10.0) == 0.0


def test_cap_with_a_custom_margin(clock):
    deadline = Deadline(28.0, clock=clock)
    clock.advance(20.0)  # 8 s left
    assert deadline.cap(10.0, margin_s=3.0) == pytest.approx(5.0)
    assert deadline.cap(10.0, margin_s=0.0) == pytest.approx(8.0)
    assert deadline.cap(2.0, margin_s=3.0) == pytest.approx(2.0)
    assert deadline.cap(10.0, margin_s=9.0) == 0.0


def test_cap_does_not_use_up_time(clock):
    deadline = Deadline(28.0, clock=clock)
    deadline.cap(10.0)
    deadline.cap(10.0)
    assert deadline.remaining() == pytest.approx(28.0)


# require


def test_require_passes_with_time_left(clock):
    deadline = Deadline(28.0, clock=clock)
    clock.advance(10.0)
    assert deadline.require(5.0, "Not enough time") is None


def test_require_passes_with_exactly_the_minimum_left(clock):
    deadline = Deadline(28.0, clock=clock)
    clock.advance(23.0)  # exactly 5 s left (whole numbers, so no rounding)
    assert deadline.require(5.0, "Not enough time") is None


def test_require_raises_the_given_message_below_the_minimum(clock):
    message = "The search ran out of time. Retry in about 30 seconds."
    deadline = Deadline(28.0, clock=clock)
    clock.advance(23.5)  # 4.5 s left
    with pytest.raises(ToolError) as caught:
        deadline.require(5.0, message)
    assert str(caught.value) == message


def test_require_raises_when_time_is_spent(clock):
    deadline = Deadline(1.0, clock=clock)
    clock.advance(2.0)
    with pytest.raises(ToolError) as caught:
        deadline.require(0.5, "Out of time")
    assert str(caught.value) == "Out of time"


# total_s must be positive


@pytest.mark.parametrize("total", [0, 0.0, -1.0, -28.0, float("nan"), float("inf")])
def test_total_of_zero_or_below_or_not_finite_is_refused(total, clock):
    # NaN would pass a plain `<= 0` check and then refuse every step; infinity would switch the budget off.
    with pytest.raises(ValueError):
        Deadline(total, clock=clock)


@pytest.mark.parametrize("step_limit, margin", [
    (-1.0, 1.0), (float("nan"), 1.0), (float("inf"), 1.0), (10.0, -2.0), (10.0, float("nan")),
])
def test_cap_refuses_a_bad_step_limit_or_margin(step_limit, margin, clock):
    # A negative margin would hand out more time than is left.
    with pytest.raises(ValueError):
        Deadline(28.0, clock=clock).cap(step_limit, margin_s=margin)


def test_a_small_positive_total_is_accepted(clock):
    assert Deadline(0.001, clock=clock).remaining() == pytest.approx(0.001)
