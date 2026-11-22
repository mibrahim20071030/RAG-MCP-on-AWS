"""The search_datasheets tool: its inputs, its result shape, and the work it does. No MCP server code here.

The tool returns passages only; Claude writes the answer from them. Reviewer notes (corrections) are not
included: they are Project 2's way of fixing its own answers, and they never enter `retrieve_passages`.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, get_args

from botocore.exceptions import ClientError
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from datasheet_mcp.limits import LOCAL_USER, THROTTLED, Limits, NotPaid, is_throttle
from rag import config, rerank

log = logging.getLogger(__name__)

# Tool-facing datasheet name (the part) -> document name in the database (a key of rag.config.DOCUMENTS).
DATASHEETS = {doc["part"]: name for name, doc in config.DOCUMENTS.items()}
# Written out because the input schema needs the names when the module loads; checked against DATASHEETS below.
DatasheetName = Literal["all", "STM32F205/207", "TCTH0xxxE", "ESP32", "LM35"]
if set(get_args(DatasheetName)) != {"all", *DATASHEETS}:
    # Out of step, the SDK would accept a name run_search cannot map (a KeyError, so only the generic error).
    raise RuntimeError(f"DatasheetName {get_args(DatasheetName)} does not match the parts in config.DOCUMENTS")

SEARCH_DESCRIPTION = (
    "Search the datasheets of four electronic parts and return the passages that best match a question: "
    "STM32F205/207 (ST microcontroller), TCTH0xxxE (Toshiba Thermoflagger over-temperature detection IC), ESP32 (Espressif "
    "Wi-Fi and Bluetooth chip) and LM35 (TI temperature sensor). Use it for questions about these parts' "
    "ratings, pinouts, electrical characteristics, timing, features or packages.\n\n"
    "Answer only from the returned passages. Cite each fact with the datasheet name and the PDF page "
    "(`page`; `printed_page` is the number printed on the sheet, when it differs). If no passage answers the "
    "question, say that the datasheets searched do not contain it; do not fill the gap from memory. "
    "Passages are best first. When `reranked` is true, `relevance` is the reranker's score from 0 to 1; "
    "otherwise it is the search score, which is small (about 0.01 to 0.03) and only orders the passages."
)


class Passage(BaseModel):
    rank: int = Field(description="Position in the result, 1 is the best match")
    datasheet: str = Field(description="The part the datasheet covers, for example LM35")
    revision: str = Field(description="The datasheet revision searched")
    page: int | None = Field(description="Page number in the PDF file, counted from 1")
    printed_page: str | None = Field(description="Page number printed on the sheet, when it differs from page")
    section: str | None = Field(description="Heading of the section the passage is in, when known")
    text: str = Field(description="The passage text from the datasheet")
    relevance: float = Field(description="Relevance score, higher is better (reranker score 0 to 1 when reranked)")


class SearchResult(BaseModel):
    query: str = Field(description="The query as searched")
    count: int = Field(description="Number of passages returned; 0 means nothing matched")
    reranked: bool = Field(description="True when the passages are in reranker order, False in search order")
    passages: list[Passage]


@dataclass(frozen=True)
class Deps:
    """What the tool calls, passed in so tests can give fakes.

    `connect()` opens a database connection usable in a `with` block (closed at the end). `retrieve` has the
    signature of `rag.retrieve.retrieve_passages`. `documents` is shaped like `rag.config.DOCUMENTS`. `limits`
    (Layer 6) are the usage limits, None for none. `rerank_client` makes the real Bedrock client a rerank slot
    wraps (rag.rerank's own, by default).
    """
    connect: Callable
    retrieve: Callable
    documents: dict
    limits: Limits | None = None
    rerank_client: Callable = rerank._bedrock


def shape(result: dict, query: str, documents: dict) -> SearchResult:
    """The tool result for a `retrieve_passages` result: ranked passages with the part name and revision."""
    passages = []
    for rank, hit in enumerate(result["passages"], start=1):
        doc = documents.get(hit["document"], {})
        page, label = hit.get("page"), hit.get("page_label")
        passages.append(Passage(
            rank=rank,
            datasheet=doc.get("part", hit["document"]),
            revision=doc.get("revision", ""),
            page=page,
            printed_page=str(label) if label is not None and str(label) != str(page) else None,
            section=hit.get("section"),
            text=hit["text"],
            relevance=round(float(hit["score"]), 4),
        ))
    return SearchResult(query=query, count=len(passages), reranked=result["reranked"], passages=passages)


def run_search(query: str, datasheet: str, num_results: int, deps: Deps, user: str = LOCAL_USER) -> SearchResult:
    """Search one datasheet (or all) for the query and return up to num_results passages.

    The SDK has already checked the token and the input limits. Then, cheapest first: the blank-query check, the
    user's per-minute count, the daily cap, the database. One database connection is opened for the call and
    closed when it ends, so an idle Aurora can pause.

    The daily place goes back when nothing was paid for: the database could not be reached, the rerank slot was
    refused, or Bedrock throttled the call. A throttle becomes a readable ToolError; other errors pass on.
    """
    query = query.strip()
    if not query:
        raise ToolError("The query is empty. Send the question or the key words to look for.")
    document = None if datasheet == "all" else DATASHEETS[datasheet]
    limits = deps.limits
    day = None
    if limits is not None:
        limits.check_user(user)
        day = limits.take_daily()
    rerank_client = limits.rerank_client(deps.rerank_client) if limits is not None else None
    give_back = True  # until the database answers
    try:
        with deps.connect() as conn:
            give_back = False
            result = deps.retrieve(conn, query, document=document, k=num_results, rerank_client=rerank_client)
    except NotPaid:
        give_back = True
        raise
    except ClientError as error:
        if not is_throttle(error):
            raise
        give_back = True
        log.warning("Bedrock throttled the call (%s, request %s)", error.response.get("Error", {}).get("Code"),
                    error.response.get("ResponseMetadata", {}).get("RequestId"))
        raise ToolError(THROTTLED) from error
    finally:
        if give_back and limits is not None:
            limits.release_daily(day)
    return shape(result, query, deps.documents)
