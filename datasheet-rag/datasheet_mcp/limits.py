"""The limits on search_datasheets (Project 3 Layer 6), counted in DynamoDB (rag.usage) so every Lambda copy sees
the same counts. Each refusal is a ToolError that tells Claude what happened and when to try again.

Order, cheapest first (after the SDK's token and input checks): the per-user count for this minute, then the daily
count for all users, then the database. Inside the search, a shared rerank slot is taken only just before a paid
rerank call, so a question whose scores are all in the cache never uses one.

All windows are fixed (the count starts again at each multiple of the window length). The per-user count uses a
minute. The rerank slot uses windows of 60 / limit seconds with one call each (20 s for 3 a minute): a fixed minute
would let 3 calls through at the end of one minute and 3 more at the start of the next, 6 within seconds, more than
Bedrock allows; with 20 s windows at most 2 go out close together.

A counter that cannot be reached (DynamoDB error, USAGE_TABLE unset) refuses the call: a broken counter must not
lift the limits. The detail goes to the log, never to Claude.
"""
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError

from rag import usage

log = logging.getLogger(__name__)

WINDOW_S = 60
LOCAL_USER = "local"  # the one user when login is off (local runs only)
THROTTLE_CODES = ("ThrottlingException", "TooManyRequestsException")  # as in rag.api._bedrock_error

BUSY = "The search service is busy right now. Try again in a minute."
THROTTLED = "AWS is limiting calls to the search models right now. Wait a minute and search again."
NO_USER = "Your login token has no user id, so the search limits cannot be applied. Log in again."
# What a counter can raise: an AWS refusal, a network failure, or USAGE_TABLE unset (rag.usage._table).
COUNTER_ERRORS = (ClientError, BotoCoreError, RuntimeError)


class NotPaid(ToolError):
    """A refusal or failure after the daily place was taken but before a paid call was made, so the place goes
    back: the rerank slot was refused, or Bedrock throttled the call."""


def is_throttle(error: BaseException) -> bool:
    """True for an AWS refusal that means "too many calls", which a retry a minute later can fix."""
    return isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") in THROTTLE_CODES


def current_user() -> str:
    """The id of the logged-in user (the token's `sub`), or LOCAL_USER when login is off.

    With login on, the SDK refuses a request without a valid token (401) before the tool runs, so no token here
    means login is off."""
    token = get_access_token()
    if token is None:
        return LOCAL_USER
    if not token.subject:
        raise ToolError(NO_USER)
    return token.subject


@dataclass(frozen=True)
class Limits:
    """The three limits; 0 switches one off. `counter` has the functions of rag.usage (tests pass a fake),
    `clock` gives Unix time."""
    per_user_per_minute: int = 0
    daily: int = 0
    rerank_per_minute: int = 0
    counter: Any = usage
    clock: Callable[[], float] = time.time

    def __post_init__(self):
        for name in ("per_user_per_minute", "daily", "rerank_per_minute"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a whole number, 0 or more, got {value!r}")

    def _take_window(self, kind: str, key: str, limit: int, window_s: int = WINDOW_S,
                     busy: type[ToolError] = ToolError) -> tuple[int | None, int]:
        """(new count or None when full, seconds until the window ends). A counter failure raises `busy`."""
        now = self.clock()
        try:
            taken = self.counter.take_window(kind, key, limit, window_s=window_s, now=now)
        except COUNTER_ERRORS:
            log.exception("usage counter %s (key %s) failed; the call is refused", kind, key[:64])
            raise busy(BUSY) from None
        return taken, self.counter.window_left_s(window_s, now)

    def check_user(self, user: str) -> None:
        """Count this call against the user's limit for this minute, or refuse it."""
        if not self.per_user_per_minute:
            return
        taken, wait_s = self._take_window("mcp-user", user, self.per_user_per_minute)
        if taken is None:
            # The SDK does not log a ToolError, so each refusal is logged here (Layer 8 counts them).
            log.info("limit mcp-user refused user %s (%d a minute; %d s left)", user[:64],
                     self.per_user_per_minute, wait_s)
            raise ToolError(f"Too many searches this minute (the limit is {self.per_user_per_minute} a minute for "
                            f"each user). Wait about {wait_s} seconds and try again.")

    def take_daily(self) -> date | None:
        """Take a place under today's cap (UTC): the day taken, to give it back with `release_daily`, or None when
        there is no daily cap. Refuses the call when the cap is reached."""
        if not self.daily:
            return None
        day = datetime.fromtimestamp(self.clock(), UTC).date()
        try:
            taken = self.counter.take("mcp-day", self.daily, day=day)
        except COUNTER_ERRORS:
            log.exception("usage counter mcp-day failed; the call is refused")
            raise ToolError(BUSY) from None
        if taken is None:
            log.info("limit mcp-day refused (%d a day used up on %s)", self.daily, day)
            raise ToolError(f"This server's daily search limit ({self.daily} searches a day for all users) is used "
                            "up. Try again tomorrow (the day starts at 00:00 UTC).")
        return day

    def release_daily(self, day: date | None) -> None:
        """Give back the place taken on `day` (nothing when None). A failure is logged, not raised: the caller is
        already answering with the real error."""
        if day is None:
            return
        try:
            self.counter.release("mcp-day", day=day)
        except COUNTER_ERRORS:
            log.exception("usage counter mcp-day %s: a place could not be given back; the count is 1 too high", day)

    @property
    def rerank_window_s(self) -> int:
        """The length of one rerank slot's window: 60 / rerank_per_minute, rounded up (20 s for 3 a minute)."""
        return math.ceil(WINDOW_S / self.rerank_per_minute)

    def take_rerank_slot(self) -> None:
        """Take one of the paid rerank calls allowed this minute for all users, or refuse the call."""
        # NotPaid either way: the slot comes before the paid call, so a refusal or a broken counter gives the daily
        # place back. Holds while a search makes at most one paid rerank call (30 candidates, RERANK_MAX_DOCS 100).
        taken, wait_s = self._take_window("mcp-rerank", "all", 1, window_s=self.rerank_window_s, busy=NotPaid)
        if taken is None:
            log.info("limit mcp-rerank refused (%d a minute for all users; %d s left)", self.rerank_per_minute, wait_s)
            raise NotPaid(f"The reranker is busy (it allows {self.rerank_per_minute} new searches a minute for all "
                          f"users together). Wait about {wait_s} seconds and try again.")

    def rerank_client(self, inner_factory: Callable[[], Any]) -> "SlotRerankClient | None":
        """The rerank client to pass to retrieve_passages: one that takes a slot before each paid call, or None
        (retrieve uses its own client) when there is no rerank limit."""
        if not self.rerank_per_minute:
            return None
        return SlotRerankClient(self.take_rerank_slot, inner_factory)


class SlotRerankClient:
    """A stand-in for the Bedrock client in rag.rerank. rag.rerank calls `rerank` only for texts whose scores are
    not in the cache, so a fully cached question never takes a slot. The real client is made only when needed."""

    def __init__(self, take_slot: Callable[[], None], inner_factory: Callable[[], Any]):
        self._take_slot = take_slot
        self._inner_factory = inner_factory

    def rerank(self, **request):
        if "nextToken" not in request:  # a further page of the same query takes no second slot
            self._take_slot()
        return self._inner_factory().rerank(**request)
