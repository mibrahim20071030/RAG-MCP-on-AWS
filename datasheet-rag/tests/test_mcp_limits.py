"""Project 3 Layer 6 tests for the limits (datasheet_mcp/limits.py) and their place in the tool (tools.run_search).

No AWS: the counter is an in-memory fake with the functions of rag.usage (rag.usage itself is tested with a fake
DynamoDB in test_usage.py), the clock is a fake, and the retrieval is a fake that calls the rerank client only when
told the scores are not cached, the way rag.rerank does."""
import logging
from datetime import date

import psycopg
import pytest
from botocore.exceptions import ClientError, ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from datasheet_mcp import limits as limits_module
from datasheet_mcp import server, tools
from datasheet_mcp.budget import Budget, Deadline
from datasheet_mcp.limits import LOCAL_USER, Limits, NotPaid, SlotRerankClient, is_throttle
from datasheet_mcp.server import build_server
from datasheet_mcp.tools import Deps
from rag import config, rerank, usage

START = 1_800_000_000.0  # Unix time at the start of a minute window; 2027-01-15 UTC
DOCUMENTS = {"lm35": {"part": "LM35", "revision": "TEST-REV-A"}}
HIT = {"document": "lm35", "page": 4, "page_label": "3", "section": "6.1", "text": "Supply voltage: MAX: 35 V",
       "score": 0.91}
GENERIC_ERROR = "Error executing tool search_datasheets"


def throttle(code="ThrottlingException"):
    return ClientError({"Error": {"Code": code, "Message": "Rate exceeded"}}, "Rerank")


class Clock:
    def __init__(self, now=START + 10):
        self.now = now

    def __call__(self):
        return self.now


class FakeCounter:
    """The functions of rag.usage the limits use, in memory. `fail` makes every take raise it."""

    def __init__(self, fail=None, release_fail=None):
        self.windows, self.days, self.releases = {}, {}, []
        self.fail, self.release_fail = fail, release_fail

    def take_window(self, kind, key, limit, window_s=60, now=None):
        if self.fail:
            raise self.fail
        item = (kind, key, int(now // window_s))
        if self.windows.get(item, 0) >= limit:
            return None
        self.windows[item] = self.windows.get(item, 0) + 1
        return self.windows[item]

    window_left_s = staticmethod(usage.window_left_s)

    def take(self, kind, limit, day=None):
        if self.fail:
            raise self.fail
        if self.days.get((kind, day), 0) >= limit:
            return None
        self.days[(kind, day)] = self.days.get((kind, day), 0) + 1
        return self.days[(kind, day)]

    def release(self, kind, day=None):
        self.releases.append((kind, day))
        if self.release_fail:
            raise self.release_fail
        self.days[(kind, day)] = max(0, self.days.get((kind, day), 0) - 1)

    def used(self, kind="mcp-day", day=date(2027, 1, 15)):
        return self.days.get((kind, day), 0)


class Bedrock:
    """The real rerank client's stand-in: records each request and scores every text."""

    def __init__(self, error=None):
        self.requests, self.error = [], error

    def rerank(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return {"results": [{"index": 0, "relevanceScore": 0.9}]}


class Fakes:
    """Fake connect and retrieve. retrieve calls the rerank client once when `cached` is False (as rag.rerank does
    for texts with no stored score), and raises `error` when set."""

    def __init__(self, cached=False, error=None, connect_error=None):
        self.cached, self.error, self.connect_error = cached, error, connect_error
        self.connects, self.retrieves = 0, 0
        self.bedrock = Bedrock()

    def connect(self):
        self.connects += 1
        if self.connect_error:
            raise self.connect_error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def retrieve(self, conn, query, document=None, k=None, rerank_client=None):
        self.retrieves += 1
        if self.error:
            raise self.error
        if not self.cached:
            (rerank_client or self.bedrock).rerank(queries=[query], sources=["text"])
        return {"passages": [HIT], "reranked": True}

    def deps(self, limits=None):
        return Deps(connect=self.connect, retrieve=self.retrieve, documents=DOCUMENTS, limits=limits,
                    rerank_client=lambda: self.bedrock)


def make_limits(per_user=3, daily=100, rerank=3, counter=None, clock=None):
    return Limits(per_user, daily, rerank, counter=counter or FakeCounter(), clock=clock or Clock())


def search(fakes, limits, user="user-a"):
    return tools.run_search("max supply voltage", "LM35", 5, fakes.deps(limits), user=user)


# Per-user minute


def test_three_calls_pass_and_the_fourth_is_refused_with_a_wait_hint():
    fakes, clock = Fakes(cached=True), Clock(START + 20)
    limits = make_limits(clock=clock)
    for _ in range(3):
        assert search(fakes, limits).count == 1
    with pytest.raises(ToolError, match=r"Too many searches this minute .*3 a minute.*Wait about 40 seconds"):
        search(fakes, limits)
    assert fakes.connects == 3  # the refused call opened no connection


def test_the_next_minute_passes_again():
    fakes, clock = Fakes(cached=True), Clock()
    limits = make_limits(clock=clock)
    for _ in range(3):
        search(fakes, limits)
    clock.now = START + 60
    assert search(fakes, limits).count == 1


def test_two_users_have_separate_counts():
    fakes = Fakes(cached=True)
    limits = make_limits()
    for _ in range(3):
        search(fakes, limits, user="user-a")
    assert search(fakes, limits, user="user-b").count == 1
    with pytest.raises(ToolError, match="Too many searches"):
        search(fakes, limits, user="user-a")


def test_a_per_user_refusal_takes_no_daily_place():
    counter = FakeCounter()
    limits = make_limits(per_user=1, counter=counter)
    fakes = Fakes(cached=True)
    search(fakes, limits)
    with pytest.raises(ToolError, match="Too many searches"):
        search(fakes, limits)
    assert counter.used() == 1


def test_a_blank_query_is_refused_before_any_count():
    counter = FakeCounter()
    fakes = Fakes()
    with pytest.raises(ToolError, match="query is empty"):
        tools.run_search("   ", "LM35", 5, fakes.deps(make_limits(counter=counter)), user="user-a")
    assert counter.windows == {} and counter.days == {} and fakes.connects == 0


# Daily cap


def test_the_daily_cap_refuses_with_the_tomorrow_text():
    counter = FakeCounter()
    limits = make_limits(per_user=0, daily=2, counter=counter)
    fakes = Fakes(cached=True)
    search(fakes, limits)
    search(fakes, limits, user="user-b")  # the cap is for all users together
    with pytest.raises(ToolError, match=r"daily search limit \(2 searches a day.*tomorrow.*00:00 UTC"):
        search(fakes, limits, user="user-c")
    assert fakes.connects == 2


def test_the_daily_count_uses_the_utc_day_of_the_clock():
    counter = FakeCounter()
    limits = make_limits(counter=counter, clock=Clock(START + 86_400))
    search(Fakes(cached=True), limits)
    assert counter.used(day=date(2027, 1, 16)) == 1


# Shared rerank slot


def test_the_rerank_slot_is_shared_by_all_users_one_per_20_seconds():
    fakes, clock = Fakes(cached=False), Clock(START + 5)
    limits = make_limits(per_user=0, clock=clock)
    assert limits.rerank_window_s == 20
    search(fakes, limits, user="user-a")
    with pytest.raises(NotPaid, match=r"reranker is busy .*3 new searches a minute.*Wait about 15 seconds"):
        search(fakes, limits, user="user-b")  # another user, same 20 s window
    clock.now = START + 20
    search(fakes, limits, user="user-b")
    assert len(fakes.bedrock.requests) == 2  # the refused call never reached Bedrock


def test_the_rerank_slot_allows_at_most_2_close_together_across_a_minute_boundary():
    """The review case: at :58 and :01 a 1-minute window would let 3 + 3 calls out; 20 s windows let 1 + 1."""
    fakes, clock = Fakes(cached=False), Clock(START + 58)
    limits = make_limits(per_user=0, clock=clock)
    sent = 0
    for now in (START + 58, START + 58.5, START + 59, START + 61, START + 61.5, START + 62):
        clock.now = now
        try:
            search(fakes, limits, user=f"user-{now}")
            sent += 1
        except NotPaid:
            pass
    assert sent == 2 and len(fakes.bedrock.requests) == 2


@pytest.mark.parametrize(("per_minute", "window_s"), [(1, 60), (2, 30), (3, 20), (7, 9), (60, 1), (100, 1)])
def test_the_rerank_window_is_60_over_the_limit_rounded_up(per_minute, window_s):
    assert make_limits(rerank=per_minute).rerank_window_s == window_s


def test_a_cached_question_takes_no_rerank_slot():
    counter = FakeCounter()
    limits = make_limits(per_user=0, rerank=1, counter=counter)
    fakes = Fakes(cached=True)
    for _ in range(5):
        search(fakes, limits)
    assert not any(kind == "mcp-rerank" for kind, _, _ in counter.windows)
    assert fakes.bedrock.requests == []


def test_a_refused_slot_gives_the_daily_place_back():
    counter = FakeCounter()
    limits = make_limits(per_user=0, rerank=1, counter=counter)
    fakes = Fakes(cached=False)
    search(fakes, limits)
    with pytest.raises(NotPaid):
        search(fakes, limits)
    assert counter.used() == 1  # 2 taken, 1 given back


def test_a_further_page_takes_no_second_slot():
    slots = []
    bedrock = Bedrock()
    client = SlotRerankClient(lambda: slots.append(1), lambda: bedrock)
    client.rerank(queries=["q"])
    client.rerank(queries=["q"], nextToken="page-2")
    assert len(slots) == 1 and len(bedrock.requests) == 2


def test_the_real_client_is_made_only_for_a_paid_call():
    made = []
    client = SlotRerankClient(lambda: None, lambda: made.append(1) or Bedrock())
    assert made == []
    client.rerank(queries=["q"])
    assert made == [1]


def test_no_rerank_limit_passes_no_client():
    seen = {}
    fakes = Fakes(cached=True)

    def retrieve(conn, query, document=None, k=None, rerank_client=None):
        seen["client"] = rerank_client
        return {"passages": [HIT], "reranked": True}

    deps = Deps(connect=fakes.connect, retrieve=retrieve, documents=DOCUMENTS, limits=make_limits(rerank=0))
    tools.run_search("max supply voltage", "LM35", 5, deps, user="user-a")
    assert seen["client"] is None  # rag.rerank then uses its own client


# Bedrock throttle


@pytest.mark.parametrize("code", ["ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException"])
def test_a_throttled_bedrock_call_becomes_the_readable_error(code):
    counter = FakeCounter()
    fakes = Fakes(error=throttle(code))
    with pytest.raises(ToolError, match="AWS is limiting calls") as raised:
        search(fakes, make_limits(counter=counter))
    assert not isinstance(raised.value.__cause__, ToolError)
    assert counter.used() == 0  # the place went back


def test_a_throttle_from_the_wrapped_rerank_call_becomes_the_readable_error():
    fakes = Fakes(cached=False)
    fakes.bedrock.error = throttle()
    with pytest.raises(ToolError, match="AWS is limiting calls"):
        search(fakes, make_limits())


def test_a_throttle_is_converted_without_limits_too():
    with pytest.raises(ToolError, match="AWS is limiting calls"):
        search(Fakes(error=throttle()), None)


def test_another_aws_error_is_not_a_throttle_and_keeps_the_place():
    counter = FakeCounter()
    error = ClientError({"Error": {"Code": "AccessDeniedException"}}, "Rerank")
    with pytest.raises(ClientError):
        search(Fakes(error=error), make_limits(counter=counter))
    assert counter.used() == 1  # Bedrock may have been paid before the failure


def test_is_throttle():
    assert is_throttle(throttle()) and is_throttle(throttle("TooManyRequestsException"))
    assert not is_throttle(ClientError({"Error": {"Code": "ValidationException"}}, "Rerank"))
    assert not is_throttle(ValueError("ThrottlingException"))


# Giving the daily place back


def test_a_database_that_cannot_be_reached_gives_the_place_back():
    counter = FakeCounter()
    with pytest.raises(OSError):
        search(Fakes(connect_error=OSError("no route")), make_limits(counter=counter))
    assert counter.used() == 0 and counter.releases == [("mcp-day", date(2027, 1, 15))]


def test_a_success_and_a_crash_after_connecting_keep_the_place():
    counter = FakeCounter()
    limits = make_limits(per_user=0, counter=counter)
    search(Fakes(cached=True), limits)
    with pytest.raises(KeyError):
        search(Fakes(error=KeyError("x")), limits)
    assert counter.used() == 2 and counter.releases == []


def test_a_failing_release_is_logged_and_the_real_error_still_comes_through(caplog):
    counter = FakeCounter(release_fail=EndpointConnectionError(endpoint_url="https://dynamodb"))
    with caplog.at_level(logging.ERROR, logger="datasheet_mcp.limits"), pytest.raises(ToolError, match="limiting"):
        search(Fakes(error=throttle()), make_limits(counter=counter))
    assert "could not be given back" in caplog.text


# A counter that cannot be reached


@pytest.mark.parametrize("error", [ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "UpdateItem"),
                                   EndpointConnectionError(endpoint_url="https://dynamodb"),
                                   RuntimeError("USAGE_TABLE is not set")],
                         ids=["aws refusal", "network", "no table"])
@pytest.mark.parametrize("which", ["per-user", "daily", "rerank"])
def test_a_counter_failure_refuses_the_call_and_logs_the_detail(caplog, error, which):
    counter = FakeCounter(fail=error)
    settings = {"per-user": {"per_user": 3, "daily": 0, "rerank": 0},
                "daily": {"per_user": 0, "daily": 100, "rerank": 0},
                "rerank": {"per_user": 0, "daily": 0, "rerank": 3}}[which]
    fakes = Fakes(cached=False)
    with (caplog.at_level(logging.ERROR, logger="datasheet_mcp.limits"),
          pytest.raises(ToolError, match="service is busy") as raised):
        search(fakes, make_limits(counter=counter, **settings))
    assert str(error) not in str(raised.value)  # the detail is logged, not sent
    assert "failed; the call is refused" in caplog.text and caplog.records[0].exc_info
    assert fakes.bedrock.requests == []
    if which != "rerank":
        assert fakes.connects == 0


# The Limits object and the settings


@pytest.mark.parametrize("bad", [-1, 1.5, True, "3", None])
def test_limits_refuse_a_bad_number(bad):
    with pytest.raises(ValueError):
        Limits(per_user_per_minute=bad)


def test_zero_switches_each_limit_off():
    counter = FakeCounter()
    limits = make_limits(per_user=0, daily=0, rerank=0, counter=counter)
    fakes = Fakes(cached=False)
    for _ in range(10):
        search(fakes, limits)
    assert counter.windows == {} and counter.days == {}
    assert limits.take_daily() is None


def test_real_limits_follow_the_settings(monkeypatch):
    monkeypatch.setattr(config, "DEPLOYED", False)
    monkeypatch.setattr(config, "USAGE_TABLE", "datasheet-rag-usage")
    for name in ("MCP_USER_PER_MINUTE", "MCP_DAILY_LIMIT", "MCP_RERANK_PER_MINUTE"):
        monkeypatch.setattr(config, name, 0)
    assert server.real_limits() is None
    monkeypatch.setattr(config, "MCP_DAILY_LIMIT", 100)
    limits = server.real_limits()
    assert (limits.per_user_per_minute, limits.daily, limits.rerank_per_minute) == (0, 100, 0)
    assert isinstance(limits.counter, limits_module.QuickCounter)  # rag.usage with a quick client (Layer 7)


# The user id


def test_current_user_without_login_is_local():
    assert limits_module.current_user() == LOCAL_USER


def test_current_user_with_a_token_without_subject_is_refused(monkeypatch):
    from mcp.server.auth.provider import AccessToken

    token = AccessToken(token="t", client_id="c", scopes=[], subject=None)
    monkeypatch.setattr(limits_module, "get_access_token", lambda: token)
    with pytest.raises(ToolError, match="no user id"):
        limits_module.current_user()


# Through the MCP client: the limit texts reach Claude


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_limit_texts_reach_the_client_as_tool_errors():
    fakes = Fakes(cached=False)
    limits = make_limits(per_user=10, rerank=1)
    async with Client(build_server(fakes.deps(limits))) as client:
        first = await client.call_tool("search_datasheets", {"query": "max supply voltage", "datasheet": "LM35"})
        second = await client.call_tool("search_datasheets", {"query": "max supply voltage", "datasheet": "LM35"})
    assert first.is_error is False
    assert second.is_error is True
    assert second.content[0].text.startswith(f"{GENERIC_ERROR}: The reranker is busy")


@pytest.mark.anyio
async def test_the_per_user_limit_uses_the_local_user_without_login():
    counter = FakeCounter()
    async with Client(build_server(Fakes(cached=True).deps(make_limits(counter=counter)))) as client:
        await client.call_tool("search_datasheets", {"query": "max supply voltage"})
    assert [key for _, key, _ in counter.windows] == [LOCAL_USER]


# Review additions (Layer 6)


class FailingFor(FakeCounter):
    """A counter whose window counter fails for one kind only."""

    def __init__(self, kind, error):
        super().__init__()
        self.failing_kind, self.error = kind, error

    def take_window(self, kind, key, limit, window_s=60, now=None):
        if kind == self.failing_kind:
            raise self.error
        return super().take_window(kind, key, limit, window_s=window_s, now=now)


def test_a_broken_rerank_counter_gives_the_daily_place_back(caplog):
    counter = FailingFor("mcp-rerank", EndpointConnectionError(endpoint_url="https://dynamodb"))
    fakes = Fakes(cached=False)
    with caplog.at_level(logging.ERROR, logger="datasheet_mcp.limits"), pytest.raises(NotPaid, match="busy"):
        search(fakes, make_limits(counter=counter))
    assert counter.used() == 0 and fakes.bedrock.requests == []
    assert "mcp-rerank (key all) failed" in caplog.text


def test_a_throttle_with_no_daily_cap_has_nothing_to_give_back():
    counter = FakeCounter()
    with pytest.raises(ToolError, match="AWS is limiting calls"):
        search(Fakes(error=throttle()), make_limits(daily=0, counter=counter))
    assert counter.releases == []


def test_no_limits_pass_no_rerank_client():
    seen = {}

    def retrieve(conn, query, document=None, k=None, rerank_client="unset"):
        seen["client"] = rerank_client
        return {"passages": [HIT], "reranked": True}

    fakes = Fakes()
    tools.run_search("max supply voltage", "LM35", 5, Deps(connect=fakes.connect, retrieve=retrieve,
                                                             documents=DOCUMENTS))
    assert seen["client"] is None


@pytest.mark.anyio
async def test_a_token_without_subject_is_not_refused_when_limits_are_off(monkeypatch):
    from mcp.server.auth.provider import AccessToken

    token = AccessToken(token="t", client_id="c", scopes=[], subject=None)
    monkeypatch.setattr(limits_module, "get_access_token", lambda: token)
    async with Client(build_server(Fakes(cached=True).deps(None))) as client:
        result = await client.call_tool("search_datasheets", {"query": "max supply voltage"})
    assert result.is_error is False


class PagedBedrock:
    """Answers the first request with one score and a nextToken, the second with the other score."""

    def __init__(self):
        self.requests = []

    def rerank(self, **request):
        self.requests.append(dict(request))
        if "nextToken" not in request:
            return {"results": [{"index": 0, "relevanceScore": 0.9}], "nextToken": "page-2"}
        return {"results": [{"index": 1, "relevanceScore": 0.4}]}


def test_the_real_rerank_pages_take_one_slot(monkeypatch):
    """Through rag.rerank._scores itself: a further page of one query does not take a second slot."""
    monkeypatch.setattr(rerank, "_log", lambda: None)  # no row in the real cost log
    monkeypatch.setattr(rerank, "_last_call", None)
    monkeypatch.setattr(config, "RERANK_MIN_INTERVAL_S", 0)
    slots, bedrock = [], PagedBedrock()
    client = SlotRerankClient(lambda: slots.append(1), lambda: bedrock)
    scores = rerank._scores(client, "max supply voltage", ["a", "b"], sleep=lambda s: None, clock=lambda: 0.0)
    assert scores == [0.9, 0.4]
    assert len(slots) == 1 and len(bedrock.requests) == 2


def test_the_real_rerank_skips_the_client_when_every_score_is_cached(monkeypatch):
    """Through rag.rerank.rerank itself: cached scores mean no client call, so no slot."""
    monkeypatch.setattr(config, "RERANK_ENABLED", True)
    monkeypatch.setattr(rerank, "_lookup", lambda conn, q, c: (["h1"], {"h1": "text"}, {"h1": 0.7}))
    slots = []
    client = SlotRerankClient(lambda: slots.append(1), lambda: pytest.fail("Bedrock must not be called"))
    hits = rerank.rerank(None, "max supply voltage", [{"id": 1, "text": "text", "score": 0.02}], k=1,
                         client=client)
    assert hits[0]["score"] == 0.7 and slots == []


def test_limits_with_the_real_usage_module(monkeypatch):
    """rag.usage itself behind Limits (its DynamoDB client is the fake from test_usage.py), so a signature that
    drifts between the two is caught here, not on the deployed Lambda."""
    from test_usage import FakeDynamo

    dynamo = FakeDynamo()
    monkeypatch.setattr(config, "USAGE_TABLE", "datasheet-rag-usage")
    monkeypatch.setattr(usage, "_client", lambda: dynamo)
    limits = Limits(per_user_per_minute=1, daily=5, rerank_per_minute=1, clock=Clock())
    fakes = Fakes(cached=False)
    search(fakes, limits)
    with pytest.raises(ToolError, match="Too many searches"):
        search(fakes, limits)
    with pytest.raises(NotPaid, match="reranker is busy"):
        search(fakes, limits, user="user-b")
    assert usage.count("mcp-day", date(2027, 1, 15), dynamo) == 1  # user-b's place went back


def set_limits(monkeypatch, values, deployed=False, table="datasheet-rag-usage"):
    for name, value in zip(("MCP_USER_PER_MINUTE", "MCP_DAILY_LIMIT", "MCP_RERANK_PER_MINUTE"), values, strict=True):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "DEPLOYED", deployed)
    monkeypatch.setattr(config, "USAGE_TABLE", table)


@pytest.mark.parametrize("values", [(0, 0, 0), (3, 0, 3), (3, 100, 0)])
def test_deployed_with_a_limit_left_out_refuses_to_start(monkeypatch, values):
    set_limits(monkeypatch, values, deployed=True)
    with pytest.raises(ValueError, match="must all be set"):
        server.real_limits()


def test_deployed_with_all_limits_starts(monkeypatch):
    set_limits(monkeypatch, (3, 100, 3), deployed=True)
    assert server.real_limits().daily == 100


def test_a_limit_without_a_usage_table_refuses_to_start(monkeypatch):
    set_limits(monkeypatch, (3, 0, 0), table="")
    with pytest.raises(ValueError, match="USAGE_TABLE"):
        server.real_limits()
    set_limits(monkeypatch, (0, 0, 0), table="")
    assert server.real_limits() is None  # no limits locally needs no table


def test_each_refusal_is_logged(caplog):
    fakes = Fakes(cached=False)
    with caplog.at_level(logging.INFO, logger="datasheet_mcp"):
        limits = make_limits(per_user=1, daily=100, rerank=1)
        search(fakes, limits)
        with pytest.raises(ToolError):
            search(fakes, limits)  # per-user
        with pytest.raises(NotPaid):
            search(fakes, limits, user="user-b")  # rerank slot
    text = caplog.text
    assert "limit mcp-user refused user user-a" in text and "limit mcp-rerank refused" in text


def test_the_daily_refusal_is_logged(caplog):
    limits = make_limits(per_user=0, daily=1, rerank=0)
    search(Fakes(cached=True), limits)
    with caplog.at_level(logging.INFO, logger="datasheet_mcp.limits"), pytest.raises(ToolError):
        search(Fakes(cached=True), limits)
    assert "limit mcp-day refused (1 a day used up on 2027-01-15)" in caplog.text


def test_a_throttle_is_logged_with_its_code(caplog):
    with caplog.at_level(logging.WARNING, logger="datasheet_mcp.tools"), pytest.raises(ToolError):
        search(Fakes(error=throttle("TooManyRequestsException")), None)
    assert "Bedrock throttled the call (TooManyRequestsException" in caplog.text


# The deadline (Layer 7): a fake monotonic clock that the fake steps move forward


BUDGET = Budget(25.0, connect_s=19.0, bedrock_s=3.0)  # the deployed settings (runs/mcp_deploy.md)


class StepClock:
    """The deadline's clock. Steps call `spend(seconds)` to make time pass."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def spend(self, seconds):
        self.now += seconds


class TimedFakes(Fakes):
    """Fakes whose connect takes `connect_s` and whose retrieve embeds (when `vector_cached` is False) and reranks
    (when `cached` is False) the way rag.search and rag.rerank do: through embed_fn and the rerank client."""

    def __init__(self, clock, connect_s=0.0, vector_cached=False, **kwargs):
        super().__init__(**kwargs)
        self.clock, self.connect_s, self.vector_cached = clock, connect_s, vector_cached
        self.embeds, self.options = [], None

    def connect(self):
        self.clock.spend(self.connect_s)
        return super().connect()

    def retrieve(self, conn, query, document=None, k=None, rerank_client=None, **options):
        self.options = options
        if not self.vector_cached:
            (options.get("embed_fn") or self.embed)(query)
        return super().retrieve(conn, query, document=document, k=k, rerank_client=rerank_client)

    def embed(self, text):
        self.embeds.append(text)
        return [0.0]

    def deps(self, limits=None, budget=BUDGET):
        return Deps(connect=self.connect, retrieve=self.retrieve, documents=DOCUMENTS, limits=limits,
                    rerank_client=lambda: self.bedrock, budget=budget, embed=self.embed)


class SlowCounter(FakeCounter):
    """A counter whose daily take spends `take_s` on the deadline's clock (a slow DynamoDB)."""

    def __init__(self, clock, take_s):
        super().__init__()
        self.clock, self.take_s = clock, take_s

    def take(self, kind, limit, day=None):
        self.clock.spend(self.take_s)
        return super().take(kind, limit, day=day)


@pytest.fixture
def step_clock(monkeypatch):
    clock = StepClock()
    monkeypatch.setattr(tools, "Deadline", lambda total_s: Deadline(total_s, clock=clock))
    return clock


def test_a_fast_call_passes_every_time_check(step_clock):
    counter = FakeCounter()
    fakes = TimedFakes(step_clock, connect_s=1.0)
    assert search(fakes, make_limits(counter=counter)).count == 1
    assert fakes.embeds == ["max supply voltage"] and len(fakes.bedrock.requests) == 1
    assert counter.used() == 1


def test_a_typical_cold_wake_still_leaves_time_for_the_embed_and_the_rerank(step_clock):
    # Aurora took 18 s to wake (Project 2 measured 17.9 to 18.7 s): 7 s are left, enough for 3 s + 3 s.
    fakes = TimedFakes(step_clock, connect_s=18.0)
    assert search(fakes, make_limits()).count == 1
    assert fakes.embeds and len(fakes.bedrock.requests) == 1


def test_a_cold_wake_past_the_embed_minimum_is_refused_readably_and_gives_the_place_back(step_clock):
    # Aurora took 23 s to wake: 2 s are left, less than the 3 s an embed can take.
    counter = FakeCounter()
    fakes = TimedFakes(step_clock, connect_s=23.0)
    with pytest.raises(NotPaid, match="ran out of time") as raised:
        search(fakes, make_limits(counter=counter))
    assert str(raised.value) == tools.OUT_OF_TIME
    assert fakes.embeds == [] and fakes.bedrock.requests == []
    assert counter.used() == 0
    assert not any(kind == "mcp-rerank" for kind, _, _ in counter.windows)


def test_too_little_time_for_the_rerank_takes_no_slot_and_gives_the_place_back(step_clock):
    # The vector is cached (no embed), but the rerank scores are not: the time check comes before the slot.
    counter = FakeCounter()
    fakes = TimedFakes(step_clock, connect_s=23.0, vector_cached=True)
    with pytest.raises(NotPaid, match="ran out of time"):
        search(fakes, make_limits(counter=counter))
    assert fakes.bedrock.requests == []
    assert not any(kind == "mcp-rerank" for kind, _, _ in counter.windows)
    assert counter.used() == 0


def test_a_fully_cached_call_needs_no_time_check_after_the_connect(step_clock):
    fakes = TimedFakes(step_clock, connect_s=24.0, vector_cached=True, cached=True)
    assert search(fakes, make_limits()).count == 1


def test_no_time_left_before_the_connect_is_refused_without_connecting(step_clock):
    # A slow daily counter used 7 s: 18 s are left, less than the connect's 19 s worst case.
    counter = SlowCounter(step_clock, take_s=7.0)
    fakes = TimedFakes(step_clock)
    with pytest.raises(ToolError, match="ran out of time"):
        search(fakes, make_limits(counter=counter))
    assert fakes.connects == 0
    assert counter.used() == 0


def test_the_time_checks_work_without_limits(step_clock):
    fakes = TimedFakes(step_clock, connect_s=23.0, vector_cached=True)
    with pytest.raises(NotPaid, match="ran out of time"):
        search(fakes, None)
    assert fakes.bedrock.requests == []


def test_the_rerank_time_check_stays_with_limits_on_and_no_rerank_limit(step_clock):
    # Limits on, MCP_RERANK_PER_MINUTE 0: no slot, but the time check must still guard the paid call.
    fakes = TimedFakes(step_clock, connect_s=23.0, vector_cached=True)
    with pytest.raises(NotPaid, match="ran out of time"):
        search(fakes, make_limits(rerank=0))
    assert fakes.bedrock.requests == []


def test_no_budget_adds_no_checks_and_no_embed_fn():
    fakes = TimedFakes(StepClock(), connect_s=100.0)
    result = tools.run_search("max supply voltage", "LM35", 5, fakes.deps(budget=None))
    assert result.count == 1
    assert "embed_fn" not in fakes.options


def test_the_default_embedder_is_looked_up_at_call_time(step_clock, monkeypatch):
    seen = []
    monkeypatch.setattr(tools.embed, "embed_text", lambda text: seen.append(text) or [0.0])
    fakes = TimedFakes(step_clock)
    deps = Deps(connect=fakes.connect, retrieve=fakes.retrieve, documents=DOCUMENTS,
                rerank_client=lambda: fakes.bedrock, budget=BUDGET)
    tools.run_search("max supply voltage", "LM35", 5, deps)
    assert seen == ["max supply voltage"]


# Budget and the settings it is read from


def test_the_deployed_settings_fit_the_budget(monkeypatch):
    for name, value in {"AURORA_CONNECT_ATTEMPTS": 1, "AURORA_CONNECT_TIMEOUT_S": 19, "BEDROCK_MAX_ATTEMPTS": 0,
                        "BEDROCK_CONNECT_TIMEOUT_S": 1.0, "BEDROCK_READ_TIMEOUT_S": 2.0}.items():
        monkeypatch.setattr(config, name, value)
    assert server.real_budget(tools.DEADLINE_S) == BUDGET


def test_the_local_defaults_do_not_fit_the_budget(monkeypatch):
    # 3 connect attempts of 60 s and 10 Bedrock attempts of 60 + 60 s: a Lambda missing its settings must not start.
    for name, value in {"AURORA_CONNECT_ATTEMPTS": 3, "AURORA_CONNECT_TIMEOUT_S": 60, "AURORA_RETRY_PAUSE_S": 5.0,
                        "BEDROCK_MAX_ATTEMPTS": 10, "BEDROCK_CONNECT_TIMEOUT_S": 60.0,
                        "BEDROCK_READ_TIMEOUT_S": 60.0}.items():
        monkeypatch.setattr(config, name, value)
    with pytest.raises(ValueError, match="cannot fit"):
        server.real_budget(tools.DEADLINE_S)


def test_the_connect_worst_case_counts_attempts_and_pauses(monkeypatch):
    for name, value in {"AURORA_CONNECT_ATTEMPTS": 2, "AURORA_CONNECT_TIMEOUT_S": 5, "AURORA_RETRY_PAUSE_S": 1.5,
                        "BEDROCK_MAX_ATTEMPTS": 0, "BEDROCK_CONNECT_TIMEOUT_S": 1.0,
                        "BEDROCK_READ_TIMEOUT_S": 2.0}.items():
        monkeypatch.setattr(config, name, value)
    assert server.real_budget(25.0).connect_s == 11.5


@pytest.mark.parametrize("values", [(0, 19, 3), (25, -1, 3), (25, 19, float("nan")), (25, 19, 4)])
def test_budget_refuses_bad_or_unfitting_values(values):
    with pytest.raises(ValueError):
        Budget(*values)


def test_a_database_that_does_not_answer_is_a_readable_error_and_gives_the_place_back(caplog):
    counter = FakeCounter()
    fakes = Fakes(connect_error=psycopg.OperationalError("connection timeout expired"))
    with caplog.at_level(logging.WARNING, logger="datasheet_mcp.tools"), pytest.raises(ToolError) as raised:
        search(fakes, make_limits(counter=counter))
    assert str(raised.value) == tools.DB_NOT_READY
    assert "connection timeout expired" not in str(raised.value)
    assert "database connect failed" in caplog.text and "connection timeout expired" in caplog.text
    assert counter.used() == 0


def test_another_connect_error_stays_generic_and_gives_the_place_back():
    counter = FakeCounter()
    with pytest.raises(ValueError):
        search(Fakes(connect_error=ValueError("AURORA_HOST is empty")), make_limits(counter=counter))
    assert counter.used() == 0


@pytest.mark.parametrize("message", [
    'FATAL:  PAM authentication failed for user "rag_app"',
    "SSL error: certificate verify failed",
    'could not translate host name "wrong.example" to address',
    "failed to resolve host 'datasheet-rag': [Errno 16] Device or resource busy",  # psycopg 3, seen on Lambda
])
def test_a_database_setup_fault_is_its_own_message_logged_as_an_error(caplog, message):
    counter = FakeCounter()
    with caplog.at_level(logging.WARNING, logger="datasheet_mcp.tools"), pytest.raises(ToolError) as raised:
        search(Fakes(connect_error=psycopg.OperationalError(message)), make_limits(counter=counter))
    assert str(raised.value) == tools.DB_SETUP
    assert [r.levelno for r in caplog.records if "setup problem" in r.getMessage()] == [logging.ERROR]
    assert counter.used() == 0


def test_a_database_error_during_the_search_stays_generic_and_keeps_the_place():
    # Only the connect is "the database did not answer": a fault mid-query may follow paid work.
    counter = FakeCounter()
    with pytest.raises(psycopg.OperationalError):
        search(Fakes(error=psycopg.OperationalError("server closed the connection")), make_limits(counter=counter))
    assert counter.used() == 1


@pytest.mark.parametrize("error", [ConnectTimeoutError(endpoint_url="https://bedrock"),
                                   EndpointConnectionError(endpoint_url="https://bedrock")])
def test_bedrock_that_cannot_be_reached_is_readable_and_gives_the_place_back(error):
    counter = FakeCounter()
    with pytest.raises(ToolError) as raised:
        search(Fakes(error=error), make_limits(counter=counter))
    assert str(raised.value) == tools.MODELS_SLOW
    assert counter.used() == 0


@pytest.mark.parametrize("error", [ReadTimeoutError(endpoint_url="https://bedrock"),
                                   ConnectionClosedError(endpoint_url="https://bedrock")])
def test_a_bedrock_answer_cut_short_is_readable_and_keeps_the_place(error):
    counter = FakeCounter()
    with pytest.raises(ToolError) as raised:
        search(Fakes(error=error), make_limits(counter=counter))
    assert str(raised.value) == tools.MODELS_SLOW
    assert counter.used() == 1


def test_the_rerank_check_includes_the_slot_counter_time(step_clock):
    # 4 s left after a 21 s connect: enough for the 3 s rerank alone, not with the slot counter's 2 s first.
    counter = FakeCounter()
    fakes = TimedFakes(step_clock, connect_s=21.0, vector_cached=True)
    with pytest.raises(NotPaid, match="ran out of time"):
        search(fakes, make_limits(counter=counter))
    assert not any(kind == "mcp-rerank" for kind, _, _ in counter.windows)


def test_every_rerank_page_is_time_checked_but_only_the_first_takes_the_slot():
    checks, slots, bedrock = [], [], Bedrock()
    client = SlotRerankClient(lambda: slots.append(1), lambda: bedrock, check=lambda: checks.append(1))
    client.rerank(queries=["q"])
    client.rerank(queries=["q"], nextToken="page-2")
    assert checks == [1, 1] and slots == [1]


def test_the_connect_worst_case_uses_libpqs_2_s_minimum(monkeypatch):
    for name, value in {"AURORA_CONNECT_ATTEMPTS": 1, "AURORA_CONNECT_TIMEOUT_S": 1, "BEDROCK_MAX_ATTEMPTS": 0,
                        "BEDROCK_CONNECT_TIMEOUT_S": 1.0, "BEDROCK_READ_TIMEOUT_S": 2.0}.items():
        monkeypatch.setattr(config, name, value)
    assert server.real_budget(25.0).connect_s == 2


def test_budget_refuses_booleans():
    with pytest.raises(ValueError):
        Budget(25.0, True, 3.0)


# QuickCounter: rag.usage with its own DynamoDB client


class RecordingDynamo:
    def __init__(self):
        self.calls = []

    def update_item(self, **request):
        self.calls.append(request)
        return {"Attributes": {"n": {"N": "1"}}}


def test_the_quick_counter_hands_its_client_to_rag_usage(monkeypatch):
    monkeypatch.setattr(config, "USAGE_TABLE", "test-table")
    dynamo = RecordingDynamo()
    counter = limits_module.QuickCounter(client=dynamo)
    assert counter.take_window("mcp-user", "u", 3, window_s=60, now=START) == 1
    assert counter.take("mcp-day", 100, day=date(2027, 1, 15)) == 1
    counter.release("mcp-day", day=date(2027, 1, 15))
    assert len(dynamo.calls) == 3 and all(call["TableName"] == "test-table" for call in dynamo.calls)


def test_the_quick_counter_client_gives_up_quickly():
    client = limits_module.QuickCounter().client()  # made locally; no call is sent
    settings = client.meta.config
    assert settings.connect_timeout == 0.5 and settings.read_timeout == 1.5
    # One try: "add one" is not safe to send twice.
    assert settings.retries == {"total_max_attempts": 1, "mode": "standard"}


def test_real_limits_use_the_quick_counter(monkeypatch):
    for name, value in {"MCP_USER_PER_MINUTE": 3, "MCP_DAILY_LIMIT": 100, "MCP_RERANK_PER_MINUTE": 3,
                        "USAGE_TABLE": "test-table"}.items():
        monkeypatch.setattr(config, name, value)
    assert isinstance(server.real_limits().counter, limits_module.QuickCounter)
