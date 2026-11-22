"""The pure parts of scripts/mcp_measure.py (Project 3 Layer 8): the fixed list, the page check, the outcome names
and the summary. No network, no AWS."""
import importlib.util
import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from datasheet_mcp import limits, tools
from datasheet_mcp.limits import Limits

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mcp_measure.py"
spec = importlib.util.spec_from_file_location("mcp_measure", SCRIPT)
mcp_measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp_measure)


def test_the_fixed_list_has_20_questions_covering_every_datasheet_and_type():
    questions = mcp_measure.load_questions()
    assert [q["id"] for q in questions] == mcp_measure.FIXED_IDS
    assert len(questions) == 20 and len(set(mcp_measure.FIXED_IDS)) == 20
    documents = {doc for q in questions for doc, _ in q["sources"]}
    assert documents == set(mcp_measure.config.DOCUMENTS)
    assert {q["type"] for q in questions} == {"single_value", "table_lookup", "rephrased", "comparison", "no_answer"}


def test_a_missing_id_is_an_error(tmp_path):
    path = tmp_path / "q.jsonl"
    path.write_text(json.dumps({"id": "q01", "question": "x", "sources": [], "type": "no_answer"}) + "\n\n")
    assert mcp_measure.load_questions(path, ["q01"])[0]["id"] == "q01"
    with pytest.raises(ValueError, match="q02"):
        mcp_measure.load_questions(path, ["q01", "q02"])


def test_right_page_maps_the_document_to_its_part():
    question = {"sources": [["lm35", 4], ["lm35", 19]]}
    assert mcp_measure.right_page(question, [{"datasheet": "ESP32", "page": 4}, {"datasheet": "LM35", "page": 19}])
    assert mcp_measure.right_page(question, [{"datasheet": "LM35", "page": 5}]) is False
    assert mcp_measure.right_page(question, []) is False


def test_a_question_with_no_answer_is_not_judged():
    assert mcp_measure.right_page({"sources": []}, [{"datasheet": "LM35", "page": 4}]) is None


PREFIX = "Error executing tool search_datasheets: "


class FullCounter:
    """A usage counter whose every window is full, so Limits raises its real refusal text."""
    def take_window(self, kind, key, limit, window_s=60, now=None):
        return None

    def window_left_s(self, window_s=60, now=None):
        return 12

    def take(self, kind, limit, day=None):
        return None


def refusal(call) -> str:
    with pytest.raises(ToolError) as caught:
        call()
    return PREFIX + str(caught.value)


@pytest.mark.parametrize("status, error, expected", [
    (200, None, "ok"),
    (200, PREFIX + tools.DB_NOT_READY, "not_ready"),
    (200, PREFIX + tools.OUT_OF_TIME, "out_of_time"),
    (200, PREFIX + tools.DB_SETUP, "tool_error"),
    (200, PREFIX + tools.MODELS_SLOW, "models_slow"),
    (200, PREFIX + limits.THROTTLED, "throttled"),
    (200, PREFIX + limits.BUSY, "busy"),
    (200, "Error executing tool search_datasheets", "tool_error"),
    (401, None, "http_401"),
    (504, "anything", "http_504"),
])
def test_outcome_names_follow_the_server_texts(status, error, expected):
    assert mcp_measure.outcome_of(status, error) == expected


def test_the_real_refusal_texts_get_their_names():
    full = Limits(per_user_per_minute=3, daily=100, rerank_per_minute=3, counter=FullCounter(),
                  clock=lambda: 1_000_000.0)
    assert mcp_measure.outcome_of(200, refusal(lambda: full.check_user("u1"))) == "limit"
    assert mcp_measure.outcome_of(200, refusal(full.take_daily)) == "daily_limit"
    assert mcp_measure.outcome_of(200, refusal(full.take_rerank_slot)) == "rerank_busy"


def test_settings_build_the_deployed_scope_from_the_url(monkeypatch):
    monkeypatch.setenv("MCP_API_URL", "https://abc.execute-api.us-east-1.amazonaws.com/")
    monkeypatch.setenv("MCP_COGNITO_DOMAIN", "https://example.auth.us-east-1.amazoncognito.com")
    monkeypatch.setenv("MCP_COGNITO_CLIENT_IDS", "client1, client2")
    monkeypatch.setenv("MCP_SCOPE", "datasheets/search")  # the local server's scope is not used
    server, _, client_id, scope = mcp_measure._settings()
    assert server == "https://abc.execute-api.us-east-1.amazonaws.com" and client_id == "client1"
    assert scope == "https://abc.execute-api.us-east-1.amazonaws.com/mcp/search"


@pytest.mark.parametrize("url", ["http://abc.example.com", "https://abc.example.com/mcp", ""])
def test_settings_refuse_a_url_that_is_not_the_api_base(monkeypatch, url):
    monkeypatch.setenv("MCP_API_URL", url)
    monkeypatch.setenv("MCP_COGNITO_DOMAIN", "https://example.auth.us-east-1.amazoncognito.com")
    monkeypatch.setenv("MCP_COGNITO_CLIENT_IDS", "client1")
    with pytest.raises(SystemExit):
        mcp_measure._settings()


def row(outcome="ok", seconds=1.0, right_page=True):
    return {"outcome": outcome, "seconds": seconds, "right_page": right_page}


def test_summary_counts_successes_and_times_only_successful_calls():
    rows = [row(seconds=1.0), row(seconds=3.0), row(seconds=2.0, right_page=False), row(right_page=None),
            row("limit", 0.3, None), row("not_ready", 23.6, None)]
    summary = mcp_measure.summarize(rows)
    assert summary["calls"] == 6 and summary["ok"] == 4
    assert summary["success_rate"] == round(4 / 6, 3)
    assert summary["median_s"] == 1.5 and summary["max_s"] == 3.0
    assert summary["right_page"] == 2 and summary["answerable"] == 3
    assert summary["outcomes"] == {"limit": 1, "not_ready": 1, "ok": 4}


def test_summary_reads_rows_back_from_the_csv_as_text():
    rows = [{"outcome": "ok", "seconds": "0.40", "right_page": "True"},
            {"outcome": "ok", "seconds": "0.60", "right_page": "False"},
            {"outcome": "ok", "seconds": "0.50", "right_page": ""}]
    summary = mcp_measure.summarize(rows)
    assert summary["right_page"] == 1 and summary["answerable"] == 2 and summary["median_s"] == 0.5


def test_an_empty_summary_has_no_rate():
    assert mcp_measure.summarize([]) == {"calls": 0, "ok": 0, "success_rate": None, "median_s": None,
                                         "max_s": None, "right_page": 0, "answerable": 0, "outcomes": {}}


@pytest.mark.parametrize("now, expected", [(120.0, 60.0), (179.5, 0.5), (61.0, 59.0)])
def test_seconds_to_the_next_window(now, expected):
    assert mcp_measure.seconds_to_next_minute(now) == expected


def csv_row(**values):
    return {field: values.get(field, "") for field in mcp_measure.CSV_FIELDS}


def test_rows_are_appended_under_one_header(tmp_path):
    path = tmp_path / "runs" / "m.csv"
    mcp_measure._append(csv_row(id="q01"), path)
    mcp_measure._append(csv_row(id="q02"), path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(mcp_measure.CSV_FIELDS) and len(lines) == 3
    mcp_measure.check_header(path)  # the same columns: no stop


def test_a_csv_with_other_columns_stops_the_run(tmp_path):
    path = tmp_path / "m.csv"
    path.write_text("time_utc,id,seconds\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="Move it aside"):
        mcp_measure.check_header(path)
    mcp_measure.check_header(tmp_path / "missing.csv")  # a new file: no stop


def test_no_answer_means_nothing_is_sent(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    with pytest.raises(SystemExit, match="Nothing was sent"):
        mcp_measure.confirm(23)
    assert "$0.046" in capsys.readouterr().out
