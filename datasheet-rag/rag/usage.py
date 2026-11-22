"""Daily counters in DynamoDB (Cycle 2 step 7b): how many questions the demo has answered today.

The cap used to live in Aurora, so a request over it still woke Aurora to find that out. Here it is one DynamoDB item
per kind and UTC day: {"pk": "asks#2026-10-23", "n": 12, "expires_at": <epoch>}. `expires_at` is a few days later, so
DynamoDB's TTL can delete old days. `kind` is "asks" (our pipeline) or "kb_asks" (the Knowledge Base, step 8);
later steps can count other things the same way.

Window counters (Project 3 Layer 6) count per fixed window of a few seconds instead of per day: one item per kind,
key and window, {"pk": "mcp-user#<sub>#29712345", "n": 2, "expires_at": <epoch>}, where 29712345 is the Unix time
divided by the window length. A new window is a new item, so the count starts again at 0.
"""
import functools
import math
import time
from datetime import UTC, date, datetime, timedelta

from botocore.exceptions import ClientError

from rag import config


@functools.cache
def _client():
    import boto3

    return boto3.client("dynamodb", region_name=config.AWS_REGION)


def today() -> date:
    """Today in UTC, the day a counter counts against."""
    return datetime.now(UTC).date()


def _table() -> str:
    if not config.USAGE_TABLE:
        raise RuntimeError("USAGE_TABLE is not set: put the DynamoDB table's name (datasheet-rag-usage) in .env, "
                           "or in the Lambda's environment.")
    return config.USAGE_TABLE


def _key(kind: str, day: date) -> dict:
    return {"pk": {"S": f"{kind}#{day.isoformat()}"}}


def _expires(day: date) -> str:
    end = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=config.USAGE_KEEP_DAYS)
    return str(int(end.timestamp()))


def _condition_failed(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def take(kind: str, limit: int, day: date | None = None, client=None) -> int | None:
    """Count one against the cap of `day` (default today, UTC): the new count, or None when `limit` is already
    reached. One conditional update, so two requests at the same moment cannot both take the last place."""
    day, client = day or today(), client or _client()
    return _count_one(client, _key(kind, day), limit, _expires(day))


def _count_one(client, key: dict, limit: int, expires: str) -> int | None:
    """Add one to the item at `key` if its count is below `limit`: the new count, or None when it is not."""
    try:
        response = client.update_item(
            TableName=_table(), Key=key,
            UpdateExpression="ADD n :one SET expires_at = :expires",
            ConditionExpression="attribute_not_exists(n) OR n < :limit",
            ExpressionAttributeValues={":one": {"N": "1"}, ":limit": {"N": str(limit)},
                                       ":expires": {"N": expires}},
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as error:
        if _condition_failed(error):
            return None
        raise
    return int(response["Attributes"]["n"]["N"])


def take_window(kind: str, key: str, limit: int, window_s: int = 60, now: float | None = None,
                client=None) -> int | None:
    """Count one against `key`'s limit in the current fixed window of `window_s` seconds: the new count, or None
    when `limit` is already reached. `now` is Unix time (default: the clock). Same conditional update as `take`.

    The window is fixed, not sliding: the count starts again at every multiple of `window_s`, so up to twice
    `limit` can pass within a few seconds around the end of a window."""
    if window_s <= 0:
        raise ValueError(f"window_s must be above 0, got {window_s}")
    now, client = time.time() if now is None else now, client or _client()
    window = int(now // window_s)
    expires = str((window + 1) * window_s + config.USAGE_WINDOW_KEEP_S)
    return _count_one(client, {"pk": {"S": f"{kind}#{key}#{window}"}}, limit, expires)


def window_left_s(window_s: int = 60, now: float | None = None) -> int:
    """Whole seconds until the current window ends (at least 1), for a "wait about N seconds" message."""
    now = time.time() if now is None else now
    return max(1, math.ceil(window_s - now % window_s))


def release(kind: str, day: date | None = None, client=None) -> None:
    """Give back one place of the cap of `day` (default today, UTC), for a request refused before an answer was
    paid for. Never goes below 0."""
    day, client = day or today(), client or _client()
    try:
        client.update_item(
            TableName=_table(), Key=_key(kind, day),
            UpdateExpression="ADD n :minus_one",
            ConditionExpression="n > :zero",
            ExpressionAttributeValues={":minus_one": {"N": "-1"}, ":zero": {"N": "0"}},
        )
    except ClientError as error:
        if not _condition_failed(error):
            raise


def count(kind: str, day: date | None = None, client=None) -> int:
    """How many the cap of `day` (default today, UTC) has counted."""
    day, client = day or today(), client or _client()
    item = client.get_item(TableName=_table(), Key=_key(kind, day), ConsistentRead=True).get("Item")
    return int(item["n"]["N"]) if item and "n" in item else 0
