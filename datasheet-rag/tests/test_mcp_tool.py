"""Project 3 Layer 3 tests for the search_datasheets tool, called through the SDK's in-memory Client. No database, no
AWS and no paid call: the connection and the retrieval are fakes that record what they were given."""
import json

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from datasheet_mcp import tools
from datasheet_mcp.server import build_server
from datasheet_mcp.tools import Deps

pytestmark = pytest.mark.anyio

TOOL = "search_datasheets"
GENERIC_ERROR = "Error executing tool search_datasheets"
PARTS = {"all", "STM32F205/207", "TCTH0xxxE", "ESP32", "LM35"}
# Shaped like rag.config.DOCUMENTS; the revisions are made up so a test can tell they came from here.
DOCUMENTS = {
    "lm35": {"part": "LM35", "revision": "TEST-REV-A"},
    "esp32_datasheet_en": {"part": "ESP32", "revision": "TEST-REV-B"},
}
HITS = [
    {"document": "lm35", "page": 4, "page_label": "3", "section": "6.1", "text": "Supply voltage: MAX: 35 V",
     "score": 0.91},
    {"document": "lm35", "page": 5, "page_label": "5", "section": None, "text": "Operating range", "score": 0.5},
    {"document": "esp32_datasheet_en", "page": 51, "page_label": None, "section": None, "text": "VDD 3.3 V",
     "score": 0.1},
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeConnection:
    """A connection usable in a `with` block; it records when the block is entered and left."""

    def __init__(self, log):
        self.log = log

    def __enter__(self):
        self.log.append("enter")
        return self

    def __exit__(self, *exc_info):
        self.log.append("exit")
        return False


class Fakes:
    """A fake connect and a fake retrieve. `retrieve` returns `passages` or raises `error`, and records its calls."""

    def __init__(self, passages=None, error=None, connect_error=None, reranked=True):
        self.passages = HITS if passages is None else passages
        self.error = error
        self.connect_error = connect_error
        self.reranked = reranked
        self.connects = 0
        self.with_log = []
        self.calls = []

    def connect(self):
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        return FakeConnection(self.with_log)

    def retrieve(self, conn, query, document=None, k=None, rerank_client=None):
        self.calls.append({"conn": conn, "query": query, "document": document, "k": k})
        if self.error is not None:
            raise self.error
        return {"passages": list(self.passages[:k] if k else self.passages), "reranked": self.reranked}

    def deps(self):
        return Deps(connect=self.connect, retrieve=self.retrieve, documents=DOCUMENTS)


async def call(fakes, arguments):
    async with Client(build_server(fakes.deps())) as client:
        return await client.call_tool(TOOL, arguments)


def text_of(result):
    return result.content[0].text


def find_enum(schema, root):
    """The allowed values of a schema property, wherever the SDK put them: inline, behind $ref, or in anyOf/allOf."""
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return find_enum(root.get("$defs", root.get("definitions", {}))[name], root)
    if "enum" in schema:
        return set(schema["enum"])
    if "const" in schema:
        return {schema["const"]}
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            values = set()
            for option in schema[key]:
                values |= find_enum(option, root)
            return values
    return set()


# tools/list


async def test_lists_one_tool_with_its_annotations_and_datasheet_names():
    async with Client(build_server(Fakes().deps())) as client:
        listed = await client.list_tools()
    assert [tool.name for tool in listed.tools] == [TOOL]
    tool = listed.tools[0]
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.open_world_hint is False
    assert tool.annotations.idempotent_hint is True
    schema = tool.input_schema
    assert find_enum(schema["properties"]["datasheet"], schema) == PARTS


# Valid calls


async def test_valid_call_passes_its_arguments_and_shapes_the_passages():
    fakes = Fakes()
    result = await call(fakes, {"query": "  max voltage  ", "datasheet": "LM35", "num_results": 3})
    assert not result.is_error
    data = result.structured_content
    assert json.loads(text_of(result)) == data
    # The fake was asked once, with the part mapped to its document, k as sent, and the query stripped.
    assert len(fakes.calls) == 1
    seen = fakes.calls[0]
    assert seen["document"] == tools.DATASHEETS["LM35"] == "lm35"
    assert seen["k"] == 3
    assert seen["query"] == "max voltage"
    assert isinstance(seen["conn"], FakeConnection)
    assert data["query"] == "max voltage"
    assert data["reranked"] is True
    assert data["count"] == len(data["passages"]) == 3
    assert [p["rank"] for p in data["passages"]] == [1, 2, 3]
    first, second, third = data["passages"]
    assert (first["datasheet"], first["revision"], first["page"]) == ("LM35", "TEST-REV-A", 4)
    assert first["printed_page"] == "3" and first["section"] == "6.1"
    assert first["text"] == "Supply voltage: MAX: 35 V" and first["relevance"] == pytest.approx(0.91)
    # Printed page the same as the PDF page, or unknown: not repeated.
    assert second["printed_page"] is None and third["printed_page"] is None
    assert (third["datasheet"], third["revision"]) == ("ESP32", "TEST-REV-B")


async def test_all_searches_every_datasheet_and_default_is_ten_results():
    fakes = Fakes()
    result = await call(fakes, {"query": "supply voltage", "datasheet": "all"})
    assert not result.is_error
    assert fakes.calls[0]["document"] is None
    assert fakes.calls[0]["k"] == 10


async def test_omitted_datasheet_means_all():
    fakes = Fakes()
    result = await call(fakes, {"query": "supply voltage"})
    assert not result.is_error
    assert fakes.calls[0]["document"] is None and fakes.calls[0]["k"] == 10


async def test_reranked_false_is_passed_on():
    result = await call(Fakes(reranked=False), {"query": "supply voltage"})
    assert result.structured_content["reranked"] is False


async def test_empty_result_is_a_success_with_count_zero():
    fakes = Fakes(passages=[])
    result = await call(fakes, {"query": "flux capacitor", "datasheet": "LM35"})
    assert not result.is_error
    assert result.structured_content["count"] == 0
    assert result.structured_content["passages"] == []
    assert json.loads(text_of(result)) == result.structured_content


async def test_single_passage():
    fakes = Fakes(passages=HITS[:1])
    result = await call(fakes, {"query": "supply voltage", "num_results": 1})
    assert not result.is_error
    assert fakes.calls[0]["k"] == 1
    assert result.structured_content["count"] == 1
    assert result.structured_content["passages"][0]["rank"] == 1


# Inputs at the limits that pass


@pytest.mark.parametrize("arguments, k", [
    ({"query": "v" * 500}, 10),
    ({"query": "supply voltage", "num_results": 1}, 1),
    ({"query": "supply voltage", "num_results": 10}, 10),
    ({"query": "x"}, 10),
])
async def test_values_at_the_limits_pass(arguments, k):
    fakes = Fakes()
    result = await call(fakes, arguments)
    assert not result.is_error, text_of(result)
    assert fakes.calls[0]["k"] == k
    assert fakes.calls[0]["query"] == arguments["query"]


@pytest.mark.parametrize("part", sorted(PARTS - {"all"}))
async def test_each_part_maps_to_its_document(part):
    fakes = Fakes(passages=[])
    result = await call(fakes, {"query": "pinout", "datasheet": part})
    assert not result.is_error
    assert fakes.calls[0]["document"] == tools.DATASHEETS[part]


# Bad inputs: an error result, and the search never runs


@pytest.mark.parametrize("arguments", [
    {"query": "supply voltage", "datasheet": "LM358"},
    {"query": ""},
    {"query": "v" * 501},
    {"query": "supply voltage", "num_results": 11},
    {"query": "supply voltage", "num_results": 0},
    {"query": "supply voltage", "num_results": -1},
    {"datasheet": "LM35"},
], ids=["unknown-part", "empty-query", "query-501", "num-11", "num-0", "num-negative", "no-query"])
async def test_sdk_rejects_bad_input_before_the_tool_runs(arguments):
    fakes = Fakes()
    result = await call(fakes, arguments)
    assert result.is_error
    # The validation path keeps the reason after the prefix; a crash would give the bare generic text.
    assert text_of(result).startswith(f"{GENERIC_ERROR}: ")
    assert fakes.calls == []
    assert fakes.connects == 0


async def test_unknown_part_error_lists_the_allowed_names():
    result = await call(Fakes(), {"query": "supply voltage", "datasheet": "LM358"})
    assert result.is_error
    text = text_of(result)
    for part in PARTS:
        assert f"'{part}'" in text


@pytest.mark.parametrize("query", ["   ", "\t\n "])
async def test_blank_query_gives_the_readable_error(query):
    fakes = Fakes()
    result = await call(fakes, {"query": query})
    assert result.is_error
    assert "The query is empty" in text_of(result)
    assert fakes.calls == [] and fakes.connects == 0


# Errors raised while the tool runs


async def test_tool_error_text_reaches_the_client():
    message = "Search is busy. Retry in about 20 seconds."
    fakes = Fakes(error=ToolError(message))
    result = await call(fakes, {"query": "supply voltage"})
    assert result.is_error
    assert message in text_of(result)
    assert text_of(result) == f"{GENERIC_ERROR}: {message}"


async def test_unexpected_error_in_retrieve_gives_only_the_generic_text():
    fakes = Fakes(error=KeyError("secret detail"))
    result = await call(fakes, {"query": "supply voltage"})
    assert result.is_error
    assert text_of(result) == GENERIC_ERROR
    assert "secret" not in text_of(result)


async def test_unexpected_error_in_connect_gives_only_the_generic_text():
    fakes = Fakes(connect_error=OSError("db password xyz"))
    result = await call(fakes, {"query": "supply voltage"})
    assert result.is_error
    assert text_of(result) == GENERIC_ERROR
    assert "password" not in text_of(result) and "xyz" not in text_of(result)
    assert fakes.calls == []


async def test_unexpected_error_is_logged_with_its_detail(caplog):
    # The detail the client does not see must still reach the server log (CloudWatch on Lambda).
    caplog.set_level("INFO", logger="mcp")
    await call(Fakes(error=KeyError("secret detail")), {"query": "supply voltage"})
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None
    assert "secret detail" in repr(errors[0].exc_info[1].__cause__)


async def test_tool_error_is_not_logged_as_an_error(caplog):
    # An expected failure (busy, limits) is not a crash, so it does not fill the log with ERROR lines.
    caplog.set_level("INFO", logger="mcp")
    await call(Fakes(error=ToolError("Search is busy. Retry in about 20 seconds.")), {"query": "supply voltage"})
    assert [record for record in caplog.records if record.levelname == "ERROR"] == []


# The connection is closed


async def test_connection_closed_after_a_success():
    fakes = Fakes()
    result = await call(fakes, {"query": "supply voltage"})
    assert not result.is_error
    assert fakes.connects == 1
    assert fakes.with_log == ["enter", "exit"]


@pytest.mark.parametrize("error", [ToolError("Search is busy. Retry in about 20 seconds."), KeyError("secret")])
async def test_connection_closed_after_a_retrieve_error(error):
    fakes = Fakes(error=error)
    result = await call(fakes, {"query": "supply voltage"})
    assert result.is_error
    assert fakes.connects == 1
    assert fakes.with_log == ["enter", "exit"]
