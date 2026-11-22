"""The MCP server with the one tool, search_datasheets. Run over stdio from datasheet-rag/:

    .venv\\Scripts\\python.exe -m datasheet_mcp.server

stdio: the client starts this program and sends JSON-RPC messages on its standard input; replies go to standard
output. So nothing else may print to stdout (warnings and logs go to stderr).
"""
from typing import Annotated

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from datasheet_mcp import limits, tools
from datasheet_mcp.budget import Budget
from datasheet_mcp.limits import Limits
from datasheet_mcp.tools import DatasheetName, Deps, SearchResult
from rag import config, db, retrieve

INSTRUCTIONS = ("Search the datasheets of four hardware parts (STM32F205/207, TCTH0xxxE, ESP32, LM35) and eight AWS "
                "Prescriptive Guidance guides on agentic AI and RAG.")


def build_server(deps: Deps, **server_kwargs) -> MCPServer:
    """A server with search_datasheets registered, calling the given deps.

    subscriptions=False: `subscriptions/listen` (2026-07-28) is not served, and `server/discover` says the lists
    never change (listChanged false). The tool list is fixed, and on Lambda each listen stream stayed open until
    the 60 s timeout (Layer 8: Claude opened one about every 30 s while a chat was open)."""
    mcp = MCPServer("datasheet-search", instructions=INSTRUCTIONS, subscriptions=False, **server_kwargs)

    @mcp.tool(name="search_datasheets", title="Search datasheets", description=tools.SEARCH_DESCRIPTION,
              annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True))
    def search_datasheets(
        query: Annotated[str, Field(min_length=1, max_length=500,
                                    description="The question, or the key words to look for")],
        datasheet: Annotated[DatasheetName, Field(description="Search one document, or 'all'")] = "all",
        num_results: Annotated[int, Field(ge=1, le=10, description="How many passages to return")] = 10,
    ) -> SearchResult:
        # The user id is needed only for the per-user limit; with limits off, no token check is added here.
        user = limits.current_user() if deps.limits is not None else limits.LOCAL_USER
        return tools.run_search(query, datasheet, num_results, deps, user=user)

    return mcp


def real_limits() -> Limits | None:
    """The limits set in rag/config.py (MCP_USER_PER_MINUTE, MCP_DAILY_LIMIT, MCP_RERANK_PER_MINUTE), or None
    when all three are 0 (the local default).

    Refuses to start (ValueError) when deployed with any of them 0, so a setting left out of the Lambda cannot open
    the public endpoint to unlimited paid calls, and when a limit is set without USAGE_TABLE, so a setup mistake
    is not reported to Claude as a brief "busy" on every call."""
    values = (config.MCP_USER_PER_MINUTE, config.MCP_DAILY_LIMIT, config.MCP_RERANK_PER_MINUTE)
    if config.DEPLOYED and not all(values):
        raise ValueError("deployed, MCP_USER_PER_MINUTE, MCP_DAILY_LIMIT and MCP_RERANK_PER_MINUTE must all be set "
                         f"above 0 (got {values})")
    if any(values) and not config.USAGE_TABLE:
        raise ValueError("an MCP limit is set but USAGE_TABLE is empty: the limits are counted in that table")
    return Limits(*values, counter=limits.QuickCounter()) if any(values) else None


def real_budget(total_s: float) -> Budget:
    """The budget of one call with each step's worst case read from the settings (Layer 7): the Aurora connect
    (attempts x timeout + the pauses between attempts) and one Bedrock call (tries x (connect + read timeout)).
    Refuses (ValueError) settings that cannot fit in `total_s`.

    botocore reads BEDROCK_MAX_ATTEMPTS (its retries `max_attempts`) as the number of retries: it makes that many
    plus one tries (checked on botocore 1.43.114, 2026-11-21: 0 gives total_max_attempts 1). So the Lambda sets 0
    for one try; with more, the retry pauses are not counted here."""
    attempts = config.AURORA_CONNECT_ATTEMPTS
    # libpq uses at least 2 s for a connect_timeout above 0 (PostgreSQL docs, libpq connect_timeout).
    timeout_s = max(2, config.AURORA_CONNECT_TIMEOUT_S)
    connect_s = attempts * timeout_s + (attempts - 1) * config.AURORA_RETRY_PAUSE_S
    tries = config.BEDROCK_MAX_ATTEMPTS + 1
    bedrock_s = tries * (config.BEDROCK_CONNECT_TIMEOUT_S + config.BEDROCK_READ_TIMEOUT_S)
    return Budget(total_s, connect_s, bedrock_s)


def real_deps(budget_s: float | None = None) -> Deps:
    """The real database and retrieval (config.DB_TARGET picks the local database or Aurora), and the limits.
    `budget_s` is the time budget of one call (the Lambda passes tools.DEADLINE_S); None, the local default, is
    no deadline."""
    budget = real_budget(budget_s) if budget_s is not None else None
    return Deps(connect=db.connect, retrieve=retrieve.retrieve_passages, documents=config.DOCUMENTS,
                limits=real_limits(), budget=budget)


def main() -> None:
    build_server(real_deps()).run()  # stdio is the default transport


if __name__ == "__main__":
    main()
