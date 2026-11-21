"""Retrieval: the passages for a question, found by search and then ordered by the reranker. No answer model.

Both projects use it: `rag.pipeline.answer_question` (Project 2) adds the reviewers' notes and the answer, and the
MCP tool (Project 3) returns the passages as they are, for Claude to answer from.

An argument left as None takes its value from `rag/config.py` when the function is called.
"""
import time

from rag import config, rerank, search


def ms_since(start: float) -> float:
    """Milliseconds since a `time.perf_counter()` reading, to one decimal."""
    return round((time.perf_counter() - start) * 1000, 1)


def retrieve_passages(conn, question: str, document: str | None = None, k: int | None = None, embed_fn=None,
                      rerank_client=None) -> dict:
    """The best k passages for the question, best first, with the frozen search setup.

    `document` limits the search to one datasheet (None: all). `k` is the number of passages kept (None:
    config.TOP_K; below 1 raises ValueError). The result has `passages` (hits as `rerank.rerank` returns them),
    `reranked` and `timings_ms` for `search` and `rerank`; the search time includes embedding the question when it
    is not cached.

    `reranked` is the reranker setting (config.RERANK_ENABLED) at the time of the call: True means passages are in
    rerank order, False means search order. It is not a sign that a rerank call was made: scores can come from the
    cache, and an empty result calls nothing.

    A question with no searchable word gives no passages, and neither the embedder nor the reranker is called.
    """
    if k is not None and k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    reranked = config.RERANK_ENABLED

    start = time.perf_counter()
    hits = search.search(conn, question, config.SEARCH_MODE, document=document, embed_fn=embed_fn)
    search_ms = ms_since(start)

    start = time.perf_counter()
    passages = rerank.rerank(conn, question, hits, k=k, client=rerank_client)
    rerank_ms = ms_since(start)

    return {"passages": passages, "reranked": reranked, "timings_ms": {"search": search_ms, "rerank": rerank_ms}}
