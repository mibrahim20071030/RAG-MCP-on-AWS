"""The MCP server with the one tool, search_datasheets. Run over stdio from datasheet-rag/:

    .venv\\Scripts\\python.exe -m datasheet_mcp.server

stdio: the client starts this program and sends JSON-RPC messages on its standard input; replies go to standard
output. So nothing else may print to stdout (warnings and logs go to stderr).
"""
from typing import Annotated

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from datasheet_mcp import tools
from datasheet_mcp.tools import DatasheetName, Deps, SearchResult
from rag import config, db, retrieve

INSTRUCTIONS = "Search the datasheets of four hardware parts (STM32F205/207, TCTH0xxxE, ESP32, LM35)."


def build_server(deps: Deps, **server_kwargs) -> MCPServer:
    """A server with search_datasheets registered, calling the given deps."""
    mcp = MCPServer("datasheet-search", instructions=INSTRUCTIONS, **server_kwargs)

    @mcp.tool(name="search_datasheets", title="Search datasheets", description=tools.SEARCH_DESCRIPTION,
              annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True))
    def search_datasheets(
        query: Annotated[str, Field(min_length=1, max_length=500,
                                    description="The question, or the key words to look for")],
        datasheet: Annotated[DatasheetName, Field(description="Search one datasheet, or 'all'")] = "all",
        num_results: Annotated[int, Field(ge=1, le=10, description="How many passages to return")] = 10,
    ) -> SearchResult:
        return tools.run_search(query, datasheet, num_results, deps)

    return mcp


def real_deps() -> Deps:
    """The real database and retrieval (config.DB_TARGET picks the local database or Aurora)."""
    return Deps(connect=db.connect, retrieve=retrieve.retrieve_passages, documents=config.DOCUMENTS)


def main() -> None:
    build_server(real_deps()).run()  # stdio is the default transport


if __name__ == "__main__":
    main()
