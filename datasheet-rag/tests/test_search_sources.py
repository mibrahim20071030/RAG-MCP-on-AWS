"""Which stored documents a search with no document named reads (2026-11-22: AWS guides for the MCP demo only).

No database: these check the settings and the SQL text. The search itself is covered by tests/test_search.py.
"""
import importlib

import pytest

from rag import config, db


def reload_with(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SEARCH_SOURCES", raising=False)
    else:
        monkeypatch.setenv("SEARCH_SOURCES", value)
    return importlib.reload(config)


@pytest.fixture(autouse=True)
def restore_modules(monkeypatch):
    yield
    monkeypatch.delenv("SEARCH_SOURCES", raising=False)
    importlib.reload(config)
    importlib.reload(db)


def test_default_reads_the_curated_datasheets_only(monkeypatch):
    assert reload_with(monkeypatch, None).SEARCH_SOURCES == ("curated",)
    importlib.reload(db)
    assert "d.source IN ('curated')" in db._DOCUMENT_FILTER
    assert "guide" not in db._NEAREST_SQL and "guide" not in db._KEYWORD_SQL


def test_the_mcp_setting_adds_the_guides(monkeypatch):
    assert reload_with(monkeypatch, " curated , guide ").SEARCH_SOURCES == ("curated", "guide")
    importlib.reload(db)
    assert "d.source IN ('curated', 'guide')" in db._NEAREST_SQL
    assert "d.source IN ('curated', 'guide')" in db._KEYWORD_SQL


@pytest.mark.parametrize("value", ["", " , ", "upload", "curated,upload", "curated'; DROP TABLE chunks; --"])
def test_uploads_blank_and_unknown_sources_are_refused(monkeypatch, value):
    with pytest.raises(RuntimeError, match="SEARCH_SOURCES"):
        reload_with(monkeypatch, value)


def test_the_four_datasheets_are_curated_and_every_other_document_is_a_guide():
    assert config.DATASHEET_DOCUMENTS == ("DS_CD00237391", "TCTH022BE_datasheet_en_20230425",
                                          "esp32_datasheet_en", "lm35")
    others = set(config.DOCUMENTS) - set(config.DATASHEET_DOCUMENTS)
    assert len(others) == 8
    assert all(config.DOCUMENTS[name]["source"] == "guide" for name in others)


def test_upsert_refuses_an_unknown_source():
    with pytest.raises(ValueError, match="source"):
        db.upsert_document(None, "x", source="upload")
