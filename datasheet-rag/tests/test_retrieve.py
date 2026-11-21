"""Project 3 Layer 1 tests for rag.retrieve. No paid call and no database: search and rerank are replaced by fakes,
or the real ones run on inputs that stop them before they reach the database or a model."""
import pytest

from rag import config, pipeline, rerank, retrieve, search

QUESTION = "What is the maximum supply voltage of the LM35?"
HITS = [
    {"id": 1, "document": "lm35", "page": 4, "page_label": "3", "section": "6.1", "kind": "table",
     "text": "Supply voltage: MAX: 35 V", "score": 0.03},
    {"id": 2, "document": "lm35", "page": 5, "page_label": "4", "section": None, "kind": "text",
     "text": "Operating range", "score": 0.02},
    {"id": 3, "document": "esp32_datasheet_en", "page": 51, "page_label": "51", "section": None, "kind": "text",
     "text": "VDD 3.3 V", "score": 0.01},
]


class Recorder:
    """A fake embedder and rerank client: it records every call, and the tests check there was none."""

    def __init__(self):
        self.calls = []

    def __call__(self, text):
        self.calls.append(("embed", text))
        return [0.0]

    def rerank(self, **request):
        self.calls.append(("rerank", request))
        return {"results": []}


@pytest.fixture
def steps(monkeypatch):
    """Fake search and rerank that record their arguments. The fake rerank returns the hits in reverse order."""
    seen = {"order": []}

    def fake_search(conn, question, mode="hybrid", n=None, strategy=None, ts_config=None, document=None,
                    embed_fn=None):
        seen["order"].append("search")
        seen["search"] = {"conn": conn, "question": question, "mode": mode, "document": document,
                          "embed_fn": embed_fn}
        return list(HITS)

    def fake_rerank(conn, question, candidates, k=None, enabled=None, client=None, **kwargs):
        seen["order"].append("rerank")
        seen["rerank"] = {"conn": conn, "question": question, "candidates": candidates, "k": k,
                          "enabled": enabled, "client": client}
        return [{**hit, "search_score": hit["score"]} for hit in reversed(candidates)]

    monkeypatch.setattr(search, "search", fake_search)
    monkeypatch.setattr(rerank, "rerank", fake_rerank)
    return seen


def test_arguments_pass_through(steps):
    embedder, client = object(), object()
    result = retrieve.retrieve_passages("conn", QUESTION, document="lm35", k=3, embed_fn=embedder,
                                        rerank_client=client)
    assert steps["order"] == ["search", "rerank"]
    assert steps["search"] == {"conn": "conn", "question": QUESTION, "mode": config.SEARCH_MODE, "document": "lm35",
                               "embed_fn": embedder}
    assert steps["rerank"]["conn"] == "conn" and steps["rerank"]["question"] == QUESTION
    assert steps["rerank"]["candidates"] == HITS
    assert steps["rerank"]["k"] == 3 and steps["rerank"]["client"] is client
    # Not passed: rerank reads its switch from config when called.
    assert steps["rerank"]["enabled"] is None
    # The passages are what rerank returned, in its order.
    assert [hit["id"] for hit in result["passages"]] == [3, 2, 1]


def test_defaults_leave_k_and_document_to_the_steps(steps):
    retrieve.retrieve_passages(None, QUESTION)
    assert steps["search"]["document"] is None
    # None: rerank takes k from config.TOP_K when called.
    assert steps["rerank"]["k"] is None


def test_timings_for_search_and_rerank(steps):
    result = retrieve.retrieve_passages(None, QUESTION)
    assert set(result) == {"passages", "reranked", "timings_ms"}
    assert set(result["timings_ms"]) == {"search", "rerank"}
    assert all(value >= 0 for value in result["timings_ms"].values())


@pytest.mark.parametrize("question", ["?!", "   ", "", "(...)"])
@pytest.mark.parametrize("enabled", [True, False])
def test_question_with_no_word_gives_no_passages_and_no_call(monkeypatch, question, enabled):
    # The real search and rerank, with no database: they must stop before using it.
    monkeypatch.setattr(config, "RERANK_ENABLED", enabled)
    recorder = Recorder()
    result = retrieve.retrieve_passages(None, question, embed_fn=recorder, rerank_client=recorder)
    assert result["passages"] == []
    assert recorder.calls == []


@pytest.mark.parametrize("enabled", [True, False])
def test_reranked_follows_config(steps, monkeypatch, enabled):
    monkeypatch.setattr(config, "RERANK_ENABLED", enabled)
    assert retrieve.retrieve_passages(None, QUESTION)["reranked"] is enabled


@pytest.mark.parametrize("k", [0, -1])
def test_k_below_one_is_refused(steps, k):
    # A slice with k <= 0 would quietly return too few or almost all hits, so it is refused before any step runs.
    with pytest.raises(ValueError, match="k must be at least 1"):
        retrieve.retrieve_passages(None, QUESTION, k=k)
    assert steps["order"] == []


def test_answer_question_passes_its_clients_to_retrieve(monkeypatch):
    # The Project 2 pipeline must hand its embedder and rerank client on, or a caller's client would be ignored.
    seen = {}

    def fake_retrieve(conn, question, document=None, k=None, embed_fn=None, rerank_client=None):
        seen.update(conn=conn, question=question, document=document, k=k, embed_fn=embed_fn, client=rerank_client)
        return {"passages": [], "reranked": True, "timings_ms": {"search": 1.0, "rerank": 2.0}}

    monkeypatch.setattr(retrieve, "retrieve_passages", fake_retrieve)
    monkeypatch.setattr(config, "CORRECTIONS_ENABLED", False)
    embedder, client = object(), object()
    result = pipeline.answer_question("conn", QUESTION, document="lm35", embed_fn=embedder, rerank_client=client)
    assert seen == {"conn": "conn", "question": QUESTION, "document": "lm35", "k": None, "embed_fn": embedder,
                    "client": client}
    assert result["timings_ms"]["search"] == 1.0 and result["timings_ms"]["rerank"] == 2.0


def test_reranker_off_keeps_search_order_without_a_call(monkeypatch):
    # The real rerank with its switch off: the first k hits in search order, and the client is never called.
    monkeypatch.setattr(config, "RERANK_ENABLED", False)
    monkeypatch.setattr(search, "search", lambda conn, question, mode, document=None, embed_fn=None: list(HITS))
    recorder = Recorder()
    result = retrieve.retrieve_passages(None, QUESTION, k=2, rerank_client=recorder)
    assert result["reranked"] is False
    assert [hit["id"] for hit in result["passages"]] == [1, 2]
    assert [hit["search_score"] for hit in result["passages"]] == [0.03, 0.02]
    assert recorder.calls == []
