"""Cycle 2 step 7b tests for rag.usage: the daily counter in DynamoDB. No AWS call: a fake client that follows the
conditional updates the counter sends."""
from datetime import UTC, date, datetime

import pytest
from botocore.exceptions import ClientError

from rag import config, usage

DAY = date(2026, 10, 23)


class FakeDynamo:
    """Just enough of a DynamoDB client for rag.usage: items by `pk`, and the two conditions it uses."""

    def __init__(self):
        self.items, self.calls = {}, []

    @staticmethod
    def _failed():
        return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, "UpdateItem")

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        pk = kwargs["Key"]["pk"]["S"]
        values = kwargs["ExpressionAttributeValues"]
        item = self.items.get(pk)
        n = int(item["n"]["N"]) if item and "n" in item else None
        if kwargs["ConditionExpression"] == "attribute_not_exists(n) OR n < :limit":
            if n is not None and n >= int(values[":limit"]["N"]):
                raise self._failed()
            self.items[pk] = {"pk": {"S": pk}, "n": {"N": str((n or 0) + 1)}, "expires_at": values[":expires"]}
            return {"Attributes": {"n": self.items[pk]["n"], "expires_at": values[":expires"]}}
        if kwargs["ConditionExpression"] == "n > :zero":
            if n is None or n <= 0:
                raise self._failed()
            item["n"] = {"N": str(n - 1)}
            return {}
        raise AssertionError(f"unexpected condition {kwargs['ConditionExpression']}")

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        item = self.items.get(kwargs["Key"]["pk"]["S"])
        return {"Item": item} if item else {}


@pytest.fixture
def dynamo(monkeypatch):
    monkeypatch.setattr(config, "USAGE_TABLE", "datasheet-rag-usage")
    return FakeDynamo()


def test_take_counts_up_to_the_limit(dynamo):
    assert [usage.take("asks", 3, DAY, dynamo) for _ in range(3)] == [1, 2, 3]
    assert usage.take("asks", 3, DAY, dynamo) is None
    assert usage.take("asks", 3, DAY, dynamo) is None
    assert usage.count("asks", DAY, dynamo) == 3  # a refused take is not counted


def test_a_higher_limit_allows_more(dynamo):
    assert usage.take("asks", 1, DAY, dynamo) == 1
    assert usage.take("asks", 1, DAY, dynamo) is None
    assert usage.take("asks", 2, DAY, dynamo) == 2


def test_days_and_kinds_are_separate(dynamo):
    assert usage.take("asks", 1, DAY, dynamo) == 1
    assert usage.take("asks", 1, date(2026, 10, 24), dynamo) == 1
    assert usage.take("uploads", 1, DAY, dynamo) == 1
    assert set(dynamo.items) == {"asks#2026-10-23", "asks#2026-10-24", "uploads#2026-10-23"}


def test_release_gives_a_place_back_and_never_goes_below_zero(dynamo):
    assert usage.take("asks", 1, DAY, dynamo) == 1
    usage.release("asks", DAY, dynamo)
    assert usage.count("asks", DAY, dynamo) == 0
    usage.release("asks", DAY, dynamo)  # already 0
    usage.release("asks", date(2026, 1, 1), dynamo)  # no item at all
    assert usage.count("asks", DAY, dynamo) == 0
    assert usage.take("asks", 1, DAY, dynamo) == 1


def test_count_of_a_day_with_no_item_is_zero(dynamo):
    assert usage.count("asks", DAY, dynamo) == 0


def test_the_update_sent_to_dynamodb(dynamo):
    usage.take("asks", 50, DAY, dynamo)
    call = dynamo.calls[0]
    assert call["TableName"] == "datasheet-rag-usage"
    assert call["Key"] == {"pk": {"S": "asks#2026-10-23"}}
    assert call["UpdateExpression"] == "ADD n :one SET expires_at = :expires"
    assert call["ExpressionAttributeValues"][":limit"] == {"N": "50"}
    expires = datetime.fromtimestamp(int(call["ExpressionAttributeValues"][":expires"]["N"]), UTC)
    assert expires == datetime(2026, 10, 23 + config.USAGE_KEEP_DAYS, tzinfo=UTC)
    usage.count("asks", DAY, dynamo)
    assert dynamo.calls[1]["ConsistentRead"] is True


def test_today_is_the_utc_day(dynamo):
    usage.take("asks", 5, client=dynamo)
    assert dynamo.calls[0]["Key"]["pk"]["S"] == f"asks#{datetime.now(UTC).date().isoformat()}"


def test_other_dynamodb_errors_are_raised(dynamo):
    class Broken(FakeDynamo):
        def update_item(self, **kwargs):
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}}, "UpdateItem")

    with pytest.raises(ClientError):
        usage.take("asks", 5, DAY, Broken())
    with pytest.raises(ClientError):
        usage.release("asks", DAY, Broken())


def test_a_limit_without_a_table_is_refused(monkeypatch):
    monkeypatch.setattr(config, "USAGE_TABLE", "")
    with pytest.raises(RuntimeError, match="USAGE_TABLE"):
        usage.take("asks", 5, DAY, FakeDynamo())


# Window counters (Project 3 Layer 6)

NOW = 1_800_000_030.0  # 30 s into window 30_000_000 of 60 s


def test_take_window_counts_up_to_the_limit_then_refuses(dynamo):
    assert [usage.take_window("mcp-user", "u1", 3, now=NOW, client=dynamo) for _ in range(3)] == [1, 2, 3]
    assert usage.take_window("mcp-user", "u1", 3, now=NOW + 29, client=dynamo) is None
    assert usage.take_window("mcp-user", "u1", 3, now=NOW + 29, client=dynamo) is None
    assert dynamo.items["mcp-user#u1#30000000"]["n"] == {"N": "3"}  # a refused take is not counted


def test_the_next_window_starts_again(dynamo):
    for _ in range(3):
        usage.take_window("mcp-user", "u1", 3, now=NOW, client=dynamo)
    assert usage.take_window("mcp-user", "u1", 3, now=NOW + 30, client=dynamo) == 1  # 1_800_000_060: next window
    assert set(dynamo.items) == {"mcp-user#u1#30000000", "mcp-user#u1#30000001"}


def test_window_keys_and_kinds_are_separate(dynamo):
    assert usage.take_window("mcp-user", "u1", 1, now=NOW, client=dynamo) == 1
    assert usage.take_window("mcp-user", "u2", 1, now=NOW, client=dynamo) == 1
    assert usage.take_window("mcp-rerank", "u1", 1, now=NOW, client=dynamo) == 1
    assert usage.take_window("mcp-user", "u1", 1, now=NOW, client=dynamo) is None


def test_the_window_update_sent_to_dynamodb(dynamo):
    usage.take_window("mcp-rerank", "all", 3, window_s=60, now=NOW, client=dynamo)
    call = dynamo.calls[-1]
    assert call["TableName"] == "datasheet-rag-usage"
    assert call["Key"] == {"pk": {"S": "mcp-rerank#all#30000000"}}
    assert call["ConditionExpression"] == "attribute_not_exists(n) OR n < :limit"
    assert call["ExpressionAttributeValues"][":limit"] == {"N": "3"}
    # The item expires USAGE_WINDOW_KEEP_S after its window ends, for DynamoDB's TTL.
    assert call["ExpressionAttributeValues"][":expires"] == {"N": str(1_800_000_060 + config.USAGE_WINDOW_KEEP_S)}


def test_take_window_raises_other_dynamodb_errors(monkeypatch):
    monkeypatch.setattr(config, "USAGE_TABLE", "datasheet-rag-usage")

    class Refusing:
        def update_item(self, **kwargs):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")

    with pytest.raises(ClientError):
        usage.take_window("mcp-user", "u1", 3, now=NOW, client=Refusing())


def test_take_window_refuses_a_window_of_zero(dynamo):
    with pytest.raises(ValueError):
        usage.take_window("mcp-user", "u1", 3, window_s=0, now=NOW, client=dynamo)


def test_take_window_without_a_table_is_refused(monkeypatch):
    monkeypatch.setattr(config, "USAGE_TABLE", "")
    with pytest.raises(RuntimeError, match="USAGE_TABLE"):
        usage.take_window("mcp-user", "u1", 3, now=NOW, client=FakeDynamo())


@pytest.mark.parametrize(("now", "left"), [(NOW, 30), (1_800_000_000.0, 60), (1_800_000_059.5, 1),
                                           (1_800_000_059.9, 1), (1_800_000_000.2, 60)])
def test_window_left_is_whole_seconds_and_at_least_one(now, left):
    assert usage.window_left_s(60, now) == left
