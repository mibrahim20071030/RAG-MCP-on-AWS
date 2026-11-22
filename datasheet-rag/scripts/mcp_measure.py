"""Project 3 Layer 8: measure the deployed MCP server on a fixed question list.

Run from datasheet-rag/, with MCP_API_URL and the MCP_COGNITO_* values in .env:
    .venv\\Scripts\\python.exe scripts\\mcp_measure.py estimate     count the paid calls first (wakes Aurora; no Bedrock)
    .venv\\Scripts\\python.exe scripts\\mcp_measure.py warm         wake Aurora, then 20 searches 21 s apart (8 min)
    .venv\\Scripts\\python.exe scripts\\mcp_measure.py limits       4 quick searches: the 4th must be refused
    .venv\\Scripts\\python.exe scripts\\mcp_measure.py cold         1 search after Aurora paused, then a retry 30 s later

`warm`, `limits` and `cold` print the most they can cost and wait for a yes. Then they log in once with a real
Cognito login (PKCE, the browser; see scripts/mcp_login.py), bound to the deployed URL (`resource`), and add one row
per call to runs/mcp_measure.csv as it comes. Each prints a summary: success rate, the median and highest time, and
how many answerable questions had a right page among the passages.

The searches use datasheet "all", as the Project 2 evaluation did, so the candidates match the ones already scored in
Aurora's rerank cache. `estimate` connects to Aurora and counts what is not cached.
"""
import argparse
import csv
import json
import math
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from datasheet_mcp import limits, tools  # the server's own error texts, so the outcome names follow them
from rag import config  # reads .env into the environment

QUESTIONS_FILE = Path(__file__).resolve().parent.parent / "data" / "questions.jsonl"
CSV_FILE = config.RUNS_DIR / "mcp_measure.csv"
# Five from each datasheet (every question type), the two cross-datasheet comparisons, and two with no answer in
# the datasheets. Fixed, so every run sends the same list.
FIXED_IDS = ["q01", "q02", "q04", "q08", "q10", "q11", "q14", "q15", "q17", "q18", "q22", "q25",
             "q29", "q30", "q33", "q35", "q41", "q42", "q44", "q45"]
LIMIT_TEXT = "Too many searches this minute"  # start of the per-user refusal (datasheet_mcp/limits.py)
PACE_S = 21  # under 3 per user a minute and the 1-per-20 s rerank slot
RETRY_AFTER_S = 30
WINDOW_MARGIN_S = 4
NUM_RESULTS = 10
CSV_FIELDS = ["time_utc", "run", "id", "type", "status", "outcome", "error", "count", "right_page", "seconds"]


def load_questions(path: Path = QUESTIONS_FILE, ids: list[str] = FIXED_IDS) -> list[dict]:
    """The questions with the given ids, in that order. A missing id is an error, so the list cannot shrink silently."""
    by_id = {}
    with open(path, encoding="utf-8") as lines:
        for line in lines:
            if line.strip():
                question = json.loads(line)
                by_id[question["id"]] = question
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise ValueError(f"questions not in {path.name}: {missing}")
    return [by_id[i] for i in ids]


def right_page(question: dict, passages: list[dict]) -> bool | None:
    """True when one passage is from a (datasheet, page) the question's answer is on; None for a question with no
    answer in the datasheets. Passages name the part (LM35), the question file the document (lm35)."""
    sources = {(config.DOCUMENTS[doc]["part"], page) for doc, page in question.get("sources", [])}
    if not sources:
        return None
    return any((p.get("datasheet"), p.get("page")) in sources for p in passages)


# (outcome name, a piece of the server's readable error text), checked in order.
OUTCOMES = [
    ("limit", LIMIT_TEXT),
    ("daily_limit", "daily search limit"),
    ("rerank_busy", "The reranker is busy"),
    ("busy", limits.BUSY),
    ("not_ready", tools.DB_NOT_READY),
    ("out_of_time", tools.OUT_OF_TIME),
    ("throttled", limits.THROTTLED),
    ("models_slow", tools.MODELS_SLOW),
]


def outcome_of(status: int, error: str | None) -> str:
    """ok; one name of OUTCOMES for a readable refusal or error; tool_error for any other (the generic error among
    them); or http_<status> for an HTTP-level reply (401, a gateway 429 or 504)."""
    if status != 200:
        return f"http_{status}"
    if error is None:
        return "ok"
    return next((name for name, text in OUTCOMES if text in error), "tool_error")


def summarize(rows: list[dict]) -> dict:
    """Success rate and times of a list of CSV rows. Times are of successful calls only."""
    ok = [r for r in rows if r["outcome"] == "ok"]
    seconds = [float(r["seconds"]) for r in ok]
    judged = [r for r in ok if r["right_page"] in (True, False, "True", "False")]
    return {
        "calls": len(rows),
        "ok": len(ok),
        "success_rate": round(len(ok) / len(rows), 3) if rows else None,
        "median_s": round(statistics.median(seconds), 2) if seconds else None,
        "max_s": round(max(seconds), 2) if seconds else None,
        "right_page": sum(1 for r in judged if r["right_page"] in (True, "True")),
        "answerable": len(judged),
        "outcomes": {o: sum(1 for r in rows if r["outcome"] == o) for o in sorted({r["outcome"] for r in rows})},
    }


def seconds_to_next_minute(now: float) -> float:
    """Seconds until the next fixed one-minute window starts (rag.usage.take_window counts from multiples of 60)."""
    return 60 - (now % 60)


# ---- network part (by hand only; never collected by pytest) ----

def _settings() -> tuple[str, str, str, str]:
    server = os.environ.get("MCP_API_URL", "").rstrip("/")
    domain = os.environ.get("MCP_COGNITO_DOMAIN", "").rstrip("/")
    client_ids = [c.strip() for c in os.environ.get("MCP_COGNITO_CLIENT_IDS", "").split(",") if c.strip()]
    if not (server.startswith("https://") and not server.endswith("/mcp") and domain and client_ids):
        sys.exit("Set MCP_API_URL (https://<api-id>.execute-api.us-east-1.amazonaws.com, without /mcp), "
                 "MCP_COGNITO_DOMAIN and MCP_COGNITO_CLIENT_IDS in .env.")
    # The deployed scope, built from the URL: MCP_SCOPE in .env is the local server's (it reads the same name).
    return server, domain, client_ids[0], f"{server}/mcp/search"


def _call(server: str, token: str, run: str, question: dict) -> dict:
    """One search; its row is written to the CSV at once, so a run stopped halfway keeps the calls already made."""
    import mcp_login  # the script beside this one

    start = time.monotonic()
    status, _, text = mcp_login.post_mcp(
        server, {"query": question["question"], "datasheet": "all", "num_results": NUM_RESULTS}, token)
    seconds = time.monotonic() - start
    try:
        result, error = mcp_login.tool_reply(status, text)
    except ValueError:  # a 200 whose body is not JSON
        result, error = {}, f"reply is not JSON: {text[:80]}"
    passages = (result.get("structuredContent") or {}).get("passages", [])
    row = {"time_utc": datetime.now(UTC).isoformat(timespec="seconds"), "run": run, "id": question["id"],
           "type": question["type"], "status": status, "outcome": outcome_of(status, error),
           "error": (error or "")[:120], "count": len(passages),
           "right_page": right_page(question, passages) if error is None and status == 200 else None,
           "seconds": round(seconds, 2)}
    print(f"  {row['time_utc']} {row['id']} {row['outcome']:<11} {row['seconds']:>6.2f} s  passages "
          f"{row['count']:>2}  right page {row['right_page']}" + (f"  {row['error']}" if row["error"] else ""))
    _append(row)
    return row


def check_header(path: Path) -> None:
    """Stops when an existing CSV has other columns than CSV_FIELDS: new rows would land under the wrong names."""
    if path.exists() and path.stat().st_size:
        with open(path, newline="", encoding="utf-8") as existing:
            header = next(csv.reader(existing), [])
        if header != CSV_FIELDS:
            sys.exit(f"{path} has the columns {header}, not {CSV_FIELDS}. Move it aside and run again.")


def _append(row: dict, path: Path | None = None) -> None:
    path = path or CSV_FILE
    new = not path.exists() or not path.stat().st_size
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def _login(server: str, domain: str, client_id: str, scope: str) -> str:
    import mcp_login

    return mcp_login.login(domain, client_id, scope, f"{server}/mcp")


def confirm(calls: int) -> None:
    """The paid-call rule: print the most these calls can cost and wait for a yes."""
    most = calls * config.RERANK_USD_PER_QUERY
    print(f"Up to {calls} searches; at most {calls} paid rerank calls, about ${most:.3f} if none is cached (run "
          "`estimate` for the exact count), plus Aurora wake time.")
    if input("Send them? [y/N] ").strip().lower() != "y":
        sys.exit("Stopped. Nothing was sent.")


def run_warm(server: str, token: str) -> list[dict]:
    """Wakes Aurora first with q01 (its own `wakeup` rows, up to 3 tries), so the timed list measures warm calls."""
    wake = load_questions(ids=["q01"])[0]
    for _ in range(3):
        if _call(server, token, "wakeup", wake)["outcome"] == "ok":
            break
        time.sleep(RETRY_AFTER_S)
    rows = []
    for question in load_questions():
        time.sleep(PACE_S)
        rows.append(_call(server, token, "warm", question))
    return rows


def run_limits(server: str, token: str) -> list[dict]:
    """4 quick searches at the start of a fresh minute. WINDOW_MARGIN_S covers a PC clock a few seconds off."""
    wait = seconds_to_next_minute(time.time()) + WINDOW_MARGIN_S
    print(f"Waiting {wait:.0f} s for a fresh one-minute window, then 4 quick searches (q01, cached).")
    time.sleep(wait)
    question = load_questions(ids=["q01"])[0]
    return [_call(server, token, "limits", question) for _ in range(4)]


def run_cold(server: str, token: str) -> list[dict]:
    question = load_questions(ids=["q01"])[0]
    first = _call(server, token, "cold", question)
    print(f"Retrying in {RETRY_AFTER_S} s.")
    time.sleep(RETRY_AFTER_S)
    return [first, _call(server, token, "cold_retry", question)]


RUNS = {"warm": (run_warm, 23), "limits": (run_limits, 4), "cold": (run_cold, 2)}  # function, most searches


def estimate() -> None:
    """Count the paid calls of the warm list in Aurora. Wakes Aurora (about 5 minutes of capacity); calls no Bedrock
    model (a question with no stored vector is counted, not embedded)."""
    from rag import db, rerank, search

    questions = [q["question"] for q in load_questions()]
    with db.connect(target="aurora") as conn:
        to_embed = search.uncached_questions(conn, questions)
        reranks = len(to_embed) * math.ceil(config.N_CANDIDATES / config.RERANK_MAX_DOCS)
        for question in questions:
            if question not in to_embed:
                reranks += rerank.calls_needed(conn, question, search.search(conn, question, "hybrid"))
    cost = reranks * config.RERANK_USD_PER_QUERY
    print(f"Warm list: {len(questions)} questions; {len(to_embed)} not embedded yet, {reranks} rerank calls not "
          f"cached (at most). Bedrock estimate about ${cost:.4f} (embeddings a fraction of a cent), plus Aurora "
          "wake time. `limits` and `cold` use q01.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run", choices=["estimate", *RUNS])
    args = parser.parse_args(argv)
    if args.run == "estimate":
        estimate()
        return
    check_header(CSV_FILE)
    server, domain, client_id, scope = _settings()
    run, most = RUNS[args.run]
    confirm(most)
    token = _login(server, domain, client_id, scope)
    print(f"\nLogged in. Run `{args.run}` against {server}/mcp (each row goes to {CSV_FILE} as it comes):")
    rows = run(server, token)
    print(json.dumps(summarize(rows), indent=2))


if __name__ == "__main__":
    main()
