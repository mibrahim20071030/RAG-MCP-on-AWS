"""The search_datasheets tool: its inputs, its result shape, and the work it does. No MCP server code here.

The tool returns passages only; Claude writes the answer from them. Reviewer notes (corrections) are not
included: they are Project 2's way of fixing its own answers, and they never enter `retrieve_passages`.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, get_args

import psycopg
from botocore.exceptions import ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BedrockUnreachable  # botocore's: connect timeout, no endpoint
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from datasheet_mcp.budget import Budget, Deadline
from datasheet_mcp.limits import LOCAL_USER, THROTTLED, Limits, NotPaid, QuickCounter, SlotRerankClient, is_throttle
from rag import config, embed, rerank

log = logging.getLogger(__name__)

# The time budget of one deployed tool call (Layer 7). The gateway stops waiting 30 s after the request arrives; a
# cold Lambda spends some of that before the tool starts (imports, app build, token check), so 25 s, not 30. Each
# step's worst case comes from the Lambda settings (server.real_budget), and a step starts only when it fits.
DEADLINE_S = 25.0

DB_NOT_READY = ("The database did not answer in time (it pauses when idle and can take up to 30 s to wake). "
                "Try again in a minute.")
DB_SETUP = ("The search service could not log in to its database. Try again in a minute; if it keeps failing, the "
            "server has a setup problem (its log has the details).")
OUT_OF_TIME = ("This search ran out of time on the server (a database waking up or a slow step). Search again now: "
               "the next try is usually quick.")
MODELS_SLOW = "The search models did not answer in time. Wait a minute and search again."
# Parts of libpq's message for a connect fault that is most likely the setup (the login, the certificate or the
# host name), logged as an error. DNS and IAM logins can also fail briefly, so the message still suggests a retry.
# "failed to resolve host" is psycopg 3's own DNS lookup (seen on Lambda 2026-11-21 with AURORA_HOST set to the
# cluster name); "could not translate host name" is libpq's.
SETUP_FAULTS = ("authentication failed", "certificate", "could not translate host name", "failed to resolve host",
                "no pg_hba.conf entry")

# Tool-facing document name (the part, or the AWS guide's short title) -> document name in the database (a key of rag.config.DOCUMENTS).
DATASHEETS = {doc["part"]: name for name, doc in config.DOCUMENTS.items()}
# Written out because the input schema needs the names when the module loads; checked against DATASHEETS below.
DatasheetName = Literal[
    "all", "STM32F205/207", "TCTH0xxxE", "ESP32", "LM35",
    "AWS Agentic AI Foundations", "AWS Agentic AI Frameworks", "AWS Agentic AI Multi-tenant", "AWS Agentic AI Patterns",
    "AWS Agentic AI Serverless", "AWS Vector Databases for RAG", "AWS RAG for Healthcare",
    "AWS RAG Writing Best Practices",
]
if set(get_args(DatasheetName)) != {"all", *DATASHEETS}:
    # Out of step, the SDK would accept a name run_search cannot map (a KeyError, so only the generic error).
    raise RuntimeError(f"DatasheetName {get_args(DatasheetName)} does not match the parts in config.DOCUMENTS")

SEARCH_DESCRIPTION = (
    "Search a document library and return the passages that best match a question. It holds the datasheets of "
    "four electronic parts: STM32F205/207 (ST microcontroller), TCTH0xxxE (Toshiba Thermoflagger over-temperature "
    "detection IC), ESP32 (Espressif Wi-Fi and Bluetooth chip) and LM35 (TI temperature sensor). Use it for "
    "questions about these parts' ratings, pinouts, electrical characteristics, timing, features or packages. "
    "It also holds eight AWS Prescriptive Guidance guides: agentic AI foundations, frameworks (and protocols such "
    "as MCP), multi-tenant and serverless architectures, and patterns and workflows; choosing a vector database "
    "for RAG; RAG for healthcare; and writing best practices for RAG. Use it for questions on those topics.\n\n"
    "Answer only from the returned passages. Cite each fact with the document name (`datasheet`) and the PDF page "
    "(`page`; `printed_page` is the number printed on the page, when it differs). If no passage answers the "
    "question, say that the documents searched do not contain it; do not fill the gap from memory. "
    "Passages are best first. When `reranked` is true, `relevance` is the reranker's score from 0 to 1; "
    "otherwise it is the search score, which is small (about 0.01 to 0.03) and only orders the passages."
)


class Passage(BaseModel):
    rank: int = Field(description="Position in the result, 1 is the best match")
    datasheet: str = Field(description="The document: the part a datasheet covers (for example LM35) or an AWS guide")
    revision: str = Field(description="The document revision or date searched; empty when it has none")
    page: int | None = Field(description="Page number in the PDF file, counted from 1")
    printed_page: str | None = Field(description="Page number printed on the page, when it differs from page")
    section: str | None = Field(description="Heading of the section the passage is in, when known")
    text: str = Field(description="The passage text from the document")
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

    `budget` (Layer 7) is the time budget of one call, or None for no deadline (local runs). With a budget, each
    slow step is refused with a readable error when its worst case does not fit in the time left, and `embed` is
    wrapped in that check. `embed` is used only with a budget (without one, retrieval embeds the question itself);
    None means rag.embed.embed_text, looked up at call time.
    """
    connect: Callable
    retrieve: Callable
    documents: dict
    limits: Limits | None = None
    rerank_client: Callable = rerank._bedrock
    budget: Budget | None = None
    embed: Callable[[str], list[float]] | None = None


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
    refused, too little time was left for a paid step, Bedrock throttled the call, or Bedrock could not be reached.
    These become readable ToolErrors, and so does a Bedrock answer that took too long (the place is kept: the call
    may have been billed); other errors pass on.

    With `deps.budget` set, a Deadline starts here and is checked before the connect, the question embed (only
    made when the question's vector is not cached) and the paid rerank (before its slot is taken): each step starts
    only when its worst case fits in the time left.
    """
    query = query.strip()
    if not query:
        raise ToolError("The query is empty. Send the question or the key words to look for.")
    document = None if datasheet == "all" else DATASHEETS[datasheet]
    budget = deps.budget
    deadline = Deadline(budget.total_s) if budget is not None else None
    limits = deps.limits
    day = None
    if limits is not None:
        limits.check_user(user)
        day = limits.take_daily()
    give_back = True  # until the database answers
    try:
        options = _retrieve_options(deps, deadline)
        if deadline is not None:
            deadline.require(budget.connect_s, OUT_OF_TIME)
        connection = _connect(deps)
        with connection as conn:
            give_back = False
            result = deps.retrieve(conn, query, document=document, k=num_results, **options)
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
    except BedrockUnreachable as error:  # ConnectTimeoutError, EndpointConnectionError: nothing reached Bedrock
        give_back = True
        log.warning("Bedrock could not be reached; the call is refused: %s", error)
        raise ToolError(MODELS_SLOW) from error
    except HTTPClientError as error:
        # ReadTimeoutError, ConnectionClosedError: the request was sent, so it may have been billed (place kept).
        log.warning("Bedrock did not answer in full (%s): %s", type(error).__name__, error)
        raise ToolError(MODELS_SLOW) from error
    finally:
        if give_back and limits is not None:
            limits.release_daily(day)
    return shape(result, query, deps.documents)


def _connect(deps: Deps):
    """deps.connect(), with a database that does not answer turned into a readable ToolError (detail in the log)."""
    try:
        return deps.connect()
    except psycopg.OperationalError as error:
        if any(fault in str(error).lower() for fault in SETUP_FAULTS):
            # Most likely a refused login, a bad certificate or a wrong host: logged as an error.
            log.exception("database connect failed (a setup problem)")  # the traceback carries libpq's message
            raise ToolError(DB_SETUP) from error
        # A paused Aurora that did not wake within the timeout, or a network fault.
        log.warning("database connect failed; the call is refused: %s", error, exc_info=True)
        raise ToolError(DB_NOT_READY) from error


def _retrieve_options(deps: Deps, deadline: Deadline | None) -> dict:
    """The rerank_client (and, with a deadline, embed_fn) arguments for deps.retrieve.

    Both checks raise NotPaid, because they come before a paid call: the daily place is given back. The rerank time
    check runs before the slot is taken, so a call that would not fit uses no slot."""
    limits = deps.limits
    if deadline is None:
        return {"rerank_client": limits.rerank_client(deps.rerank_client) if limits is not None else None}
    bedrock_s = deps.budget.bedrock_s
    embedder = deps.embed or embed.embed_text
    # The first rerank request also takes the shared slot first: one counter call before the paid one.
    takes_slot = limits is not None and bool(limits.rerank_per_minute)
    rerank_s = bedrock_s + (QuickCounter.WORST_S if takes_slot else 0.0)

    def check_rerank_time() -> None:
        deadline.require(rerank_s, OUT_OF_TIME, error=NotPaid)

    def timed_embed(text: str) -> list[float]:
        deadline.require(bedrock_s, OUT_OF_TIME, error=NotPaid)
        return embedder(text)

    if limits is not None:
        rerank_client = limits.rerank_client(deps.rerank_client, before=check_rerank_time)
    else:
        rerank_client = SlotRerankClient(None, deps.rerank_client, check=check_rerank_time)
    return {"rerank_client": rerank_client, "embed_fn": timed_embed}
