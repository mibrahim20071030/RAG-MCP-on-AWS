# CLAUDE.md: Projects 2 and 3, datasheet RAG and its MCP server

This repo holds two projects of Ibrahim's 9-project AI engineering plan, built one on top of the other.

- **Project 2 (done): the datasheet RAG pipeline.** Question answering over component datasheets (STM32, ESP32, LM35, Toshiba TCTH) with page-level citations. Cycle 1 built it locally: parsing, chunking, Titan embeddings in Postgres with pgvector, hybrid search, Cohere rerank, generation with citations, and an evaluation. Cycle 2 deployed it to AWS (Aurora, Lambda, HTTP API, CloudFront and S3), added a Knowledge Base pipeline, PDF upload and a feedback loop, and wrote the report.
- **Project 3 (in progress): a remote MCP server around Project 2.** It exposes Project 2's search and rerank as one tool Claude can call over the internet, with login, rate limiting and an AWS deployment. Claude writes the answers; this server returns passages only. It reuses Project 2's code in `datasheet-rag/rag/` and adds new code beside it.

Ibrahim must be able to explain every part. Build one layer at a time. Do not build ahead.

## Project 2: the RAG pipeline (done)

- Full build plan and history: `docs/CLAUDE_project2.md` (it was the root `CLAUDE.md` until 2026-11-21). It has the schema, the Cycle 1 layers, the Cycle 2 steps, the teardown checklist, the gotchas and the test commands. Read the part you need before changing a Project 2 module.
- Still open from Project 2 (see `progress.md`): step 10c (caps and throttle back to normal after the LinkedIn traffic) and step 11 (teardown). Project 3 shares this AWS account and adds a new Lambda behind the existing HTTP API, so check Project 3 needs before tearing anything down.
- Project 2 modules in `rag/` change only where a Project 3 layer says so, and Project 2's tests must keep passing.

## Project 3: the remote MCP server (in progress)

Design documents: `docs/project3/` (blueprint, code tour, tooling; local only).

### Start here (Layer 0)

1. Confirm the AWS billing alert exists. If not, stop and ask Ibrahim to set it first.
2. Project 2's root `CLAUDE.md` should live at `docs/CLAUDE_project2.md` (`git mv`) so this file is the root one. Ask Ibrahim before moving it if it has not been moved.
3. Work on a branch. Keep Project 2's Stop hooks (`tools/review_gate.py`, `tools/progress_gate.py`) and `progress.md` as they are; update `progress.md` after each layer.
4. New code goes in `datasheet-rag/datasheet_mcp/` and `datasheet-rag/mcp_handler.py`. Project 2 modules in `rag/` change only where a layer says so.

### Project 3 rules

- One concept at a time. Plain language, no analogies. Define a term the first time it appears.
- When explaining code, mark only code that is new (not yet written) with `NEW`. Do not label existing functions as "ours".
- Do not rely on memory for the MCP spec, SDK names or AWS limits. Check current docs, and state the source and date.
- Keep what comes from Ibrahim's documents separate from what the agent proposes. Mark proposals as proposals.
- Check cheapest first, database last (see check order).
- AWS: stay cheap, no new always-on resources, tear everything down after publishing (Lambda, gateway routes, Cognito pool, test data). Keep the billing alert.
- Every layer ends with its test passing, ruff clean, then an update to this file (see "Differences log").
- **After finishing each layer, record in this file anything that differs from the reference design below, and why.**
- Use parallel agents only for independent file sets (for example `budget.py` and `auth.py`). A layer that edits `rag/usage.py` is not run in parallel with another that edits it.
- CI runs ruff and pytest with no AWS. Deployed tests are run by hand.

### Reference design (as of 2026-11-20; verify against this before building)

- Transport: Streamable HTTP, `POST /mcp`, stateless, JSON replies. One endpoint serves the 2026-07-28 spec style (`params._meta` with protocol version and client capabilities on every request) and the older `initialize` style.
- SDK: Python `mcp` 2.x, pinned. 2.3.0 resolved on 2026-11-20 and was tested on Python 3.13. Lambda uses Python 3.14: check wheels.
- App: `mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=TransportSecuritySettings(allowed_hosts=[API host]))`.
- Lambda: `mcp_handler.lambda_handler` builds the app **on every call** and runs `Mangum(app, lifespan="auto")`. Reason: Mangum runs the app startup on every call, and the SDK's session manager can run once per instance (observed 2026-11-20).
- Gateway: HTTP API; routes only `POST /mcp` and `GET /.well-known/oauth-protected-resource/mcp`. A `GET /mcp` without a version header opened a stream that stayed open locally. Loose throttle on `POST /mcp`. Integration timeout max 30 s.
- Lambda: new function `datasheet-mcp`, 512 MB, 60 s timeout, separate from `datasheet-rag`.
- Tool: `search_datasheets(query: str 1..500 chars, datasheet: all | STM32F205/207 | TCTH0xxxE | ESP32 | LM35 = "all", num_results: int 1..10 = 10)`. Returns `SearchResult` (query, count, reranked, passages[rank, datasheet, revision, page, printed_page, section, text, relevance]). Annotations: read-only, closed-world, idempotent. Reviewer notes (corrections) are not included.
- Retrieval: new `rag/retrieve.py::retrieve_passages(conn, question, document=None, k=None, embed_fn=None, rerank_client=None) -> {"passages", "reranked"}`; `answer_question` calls it.
- Login: Cognito user pool, public app client, authorization code + PKCE (S256), callback `https://claude.ai/api/mcp/auth_callback`. Token checked in Lambda by `CognitoVerifier.verify_token` (PyJWT, RS256, issuer, `token_use == "access"`, `client_id` in allowed set, required scope, `aud` equals the MCP URL when present). The SDK returns 401 with `WWW-Authenticate ... resource_metadata` and serves the metadata document. Decide whether to set `validate_token_resource=True`.
- Per-user id: `get_access_token().subject` (verified in a prototype).
- Check order (cheapest first): gateway throttle; token check (SDK); input validation (SDK, before the function runs); per-user per-minute counter (DynamoDB); daily cap (DynamoDB); Aurora connect; search; rerank cache check; shared rerank slot (DynamoDB, 3 a minute for everyone); Cohere rerank. Note: this puts input validation before the per-user counter, unlike the earlier concept 6 order, because the SDK validates before calling the function.
- Deadline: a `Deadline(28.0)` starts when the tool function starts; each step uses `cap`/`require`; too little time gives a readable `ToolError`, not a gateway 504.
- Errors: limits, busy reranker, timeouts, Bedrock throttles are `ToolError` with a readable message and a retry hint. Unexpected exceptions become a generic "Error executing tool search_datasheets": log the detail. HTTP-level errors (401, gateway 429) happen before the tool. An empty result is a success with `count: 0`.
- Rerank pacing: Lambda sets `RERANK_MIN_INTERVAL_S=0`; the DynamoDB slot counter replaces the in-memory pause.
- DB: one connection per tool call, closed in `finally` (so Aurora can pause). Aurora resume is about 15 s (30 s or more after a day), so cold calls need the deadline.
- Claude limits (docs, 2026-11-20): tool timeout 240 s on claude.ai/Desktop; result about 150,000 characters; 10 s endpoint check; egress 160.79.104.0/21.

### Layer plan and tests

| Layer | Build | Test (must pass before moving on) |
|---|---|---|
| 0 | Billing alert, branch, this file in place | Alert exists; CI green |
| 1 | `rag/retrieve.py`; `answer_question` uses it | Existing Project 2 tests unchanged and passing; new tests with fakes: `k` and `document` pass through; empty-word question returns no passages and does not call the reranker; `reranked` follows config |
| 2 | `datasheet_mcp/tools.py`, `server.py`: stdio server, one tool | Inspector UI lists the tool and a call to LM35 returns passages |
| 3 | pytest for the tool with the in-memory `Client`; `budget.py` `Deadline` | Valid call; empty result is success; bad enum, blank query, 501 chars, `num_results` 11 give `isError`; raised `ToolError` text reaches the client; a `KeyError` gives only the generic text; `Deadline` with a fake clock |
| 4 | `datasheet_mcp/app.py`: HTTP, stateless, both styles | curl: new-style call 200; missing `_meta` 400 (-32602); name mismatch 400 (-32020); old-style `initialize` then call; `GET`/`DELETE` 405; wrong Host 421; Inspector CLI list and call; `claude mcp add` against localhost |
| 5 | `datasheet_mcp/auth.py`: `CognitoVerifier`; Cognito pool and client | Token table with generated keys: valid 200; expired, wrong issuer, ID token, wrong client, no scope, wrong `aud`, garbage all 401; no token gives 401 with `resource_metadata`; metadata URL returns 200; then a real Cognito login |
| 6 | `rag/usage.py` window counter; per-user, daily, rerank slot; Bedrock throttle conversion | pytest with a fake DynamoDB and fake clock: 3 pass and the 4th is refused; next window passes; two users separate; slot shared; throttled Bedrock becomes the readable error |
| 7 | `mcp_handler.py`; `build/mcp.zip`; Lambda; gateway routes; Cognito config | Deployed curl: no token 401 with metadata; valid token result; 4th quick call gets the readable limit error; `GET /mcp` does not reach Lambda; cold call after Aurora pause ends in time or with the readable timeout |
| 8 | Connect Claude; measure; README, REPORT.md, demo; tear down | Measurements table (success rate, warm and cold latency, login tests, limit tests); demo recorded; resources removed |

### Unverified items (do not assert; test in the layer shown)

- Which protocol version Claude sends at runtime: its docs describe the older `initialize` handshake (Layer 8: log the first requests).
- Mangum plus per-call app build on real Lambda; `mcp` wheels for Python 3.14; zip size with the new `opentelemetry-api` dependency (Layer 7).
- Cognito discovery accepted by Claude; Cognito resource indicators with the exact MCP URL (docs conflict) (Layers 5 and 8). Fallback: static header option.
- How Claude shows a bare gateway 429 (Layer 7).
- Gateway returns 404 for a method with no route (Layer 7).
- The Request headers (static header) option on Ibrahim's account (Layer 8).
- Inspector CLI flag names: run `--help` first and record what works here.

### Open decisions for Ibrahim

- Fixed one-minute window versus spacing for the per-user counter.
- Structured result versus text-only (default structured; Layer 8 decides).
- The numbers: per-user per minute, daily cap, gateway rate and burst.

### Differences log (fill after each layer)

Format: `Layer N, date: what differs from the reference design, and why.`

- Layer 1, 2026-11-21: `retrieve_passages` returns `timings_ms` (`search`, `rerank`) beside `passages` and `reranked`. Why: the API response and the deployed evaluation read separate search and rerank times from `answer_question`; without it they could only be timed together. The MCP tool can use it for the Layer 8 latency table. (Proposal by Claude, approved in the Layer 1 plan.)
- Layer 1, 2026-11-21: one Project 2 test line changed, against "existing tests unchanged": `tests/test_corrections.py:264` now replaces `rerank.rerank` on its own module with a fake that accepts `k`, instead of reaching it through `pipeline.rerank`. Why: `pipeline.py` no longer imports `rerank`, and `retrieve_passages` always passes `k`; keeping the line would have needed an unused import and a special case in real code. What the test checks is unchanged (Ibrahim chose this, 2026-11-21).
- Layer 2, 2026-11-21: `Deps` has `connect`, `retrieve`, `documents` only; the code tour's `budget_s` waits for the Layer 3 `Deadline` (not building ahead).
- Layer 2, 2026-11-21: the local database needed `db.create_schema` (via `scripts/show_db.py`) before the first search: it was made before Cycle 2 step 9a added `documents.source`, which was applied to Aurora only. No code change.

### Status log

- Layer 0, 2026-11-21 (done; PR #25 merged, `97530e8`):
  - Done: branch `project-3` created from `main`; Project 2's `CLAUDE.md` moved to `docs/CLAUDE_project2.md` with `git mv` (Ibrahim chose to build Project 3 inside this repo); this file is the root one; checks pass locally.
  - Done: the AWS billing alert. Ibrahim confirmed the zero spend budget in the console (status Healthy); the dev user `Ibrahim-Dev` cannot read budgets.
  - Done: the folder was renamed to `P2 & P3 - RAG and MCP`; the venv launchers were rebuilt for the new path; PDFs and `runs/demo_script.md` are in `.gitignore`.
  - Done: CI green on the PR; merged into `main`.
- Layer 1, 2026-11-21 (built; waiting for Ibrahim's commit, PR and merge):
  - NEW `rag/retrieve.py`: `retrieve_passages` (search, then rerank; `ms_since` moved here from `pipeline.py`). `answer_question` in `rag/pipeline.py` calls it; notes and generation unchanged.
  - NEW `tests/test_retrieve.py` (17 tests, fakes only). Checks: 1,149 passed, 212 skipped; ruff clean.
  - Review (built-in `/code-review` and `pr-test-analyzer`): `retrieve` added to `RAG_MODULES` in `scripts/build_lambda.py` (the Project 2 Lambda build would have stopped); `k` below 1 raises `ValueError`; the docstring says `reranked` is the setting, not proof a rerank call was made; a test that `answer_question` hands its clients on. Not changed: `rag/kb.py` keeps its own `_ms_since` (no layer asks to change `kb.py`).
  - The optional free check on the local database was skipped (Docker Desktop not running).
- Layer 2, 2026-11-21 (done; PR #27 merged, `d60a6d9`, CI green):
  - Review fixes: `DATASHEETS` built from `config.DOCUMENTS` with an import-time check against `DatasheetName`; description says `relevance` is 0..1 only when reranked; `pydantic==2.14.4` pinned. Open: `rag/rerank.py` pacing has no lock (overlapping calls; Layer 6 slot covers Lambda).
  - `mcp==2.3.0` (still newest on PyPI 2026-11-21; pure-Python wheel) in `requirements.txt` and the CI install line.
  - NEW `datasheet_mcp/tools.py` (`DATASHEETS`, `Passage`, `SearchResult`, `Deps`, `shape`, `run_search`, `SEARCH_DESCRIPTION`) and `datasheet_mcp/server.py` (`build_server`, `real_deps`, `main`; stdio). Run from `datasheet-rag/`: `.venv\Scripts\python.exe -m datasheet_mcp.server`.
  - Test passed: Inspector UI listed the tool; LM35 call ($0, all cached) returned 10 passages, page 4 (absolute maximum ratings, 35 V) among them. Also over stdio: blank query gives the `ToolError` text; `num_results=11` is rejected by the SDK.
  - Inspector 2.13.0 flags that worked (stdio): `npx @modelcontextprotocol/inspector --cli --cwd <datasheet-rag> <venv python> -m datasheet_mcp.server --method tools/list` (or `--method tools/call --tool-name search_datasheets --tool-arg query=... --tool-arg datasheet=LM35`). UI: same with `--web` (the default). The mode flag must come first.

## Environment notes (both projects)

- The human works on Windows with PowerShell. Use PowerShell-compatible commands. Activate the virtual environment with `.venv\Scripts\Activate.ps1`.
- The project path contains spaces. Quote paths in commands.
- The code and its virtual environment live in `datasheet-rag/` (`datasheet-rag\.venv`, Python 3.14.6). Run tests from `datasheet-rag/` and the checks from the repo root: `datasheet-rag\.venv\Scripts\python.exe tools\checks.py`.
- Project 3 packages: `mcp==2.3.0` installed (Layer 2; it brings PyJWT); Mangum not yet. Each is installed in the layer that first needs it, then added to `datasheet-rag/requirements.txt` and the CI install line. Lambda runs Python 3.14: check that `mcp` has wheels for it (Layer 7).
- The Project 3 design documents (`Project3_blueprint.pdf`, `Project3_code_tour.pdf`, `Project3_tooling_new.pdf`) are in `docs/project3/`. They are gitignored (personal study material; `tools/checks.py` also blocks committing PDFs), so they exist only on this machine.
- Never write the database password or AWS keys into any file in the repo, into `CLAUDE.md`, or into a chat message. Read the local database password from an environment variable or a `.env` file listed in `.gitignore`.
- Never create IAM users, access keys, or other credentials on the human's behalf. Tell the human what to create in the console, and what permissions it needs.

## Rules for working in this repo (both projects)

- Do not start building a layer until the human says to build it. Each layer starts in plan mode; build only after the human approves the plan. Finishing one layer is not permission to start the next.
- When a build runs into an issue, explain it in simple language: what the issue is, why it is happening, and how to fix it.
- Teach what was built (the human, 2026-10-23): after each feature, layer or step is built, walk the human through its code step by step, in detail and in simple language: what each file and function does, how the flow runs, why each decision was made, and how each AWS service involved works. The goal is that the human can explain and defend the work to employers. From 2026-11-21 (the human): the walkthrough is written as a PDF in `walkthroughs/` (Project 3: `walkthroughs/Project3/LayerN_Walkthrough.pdf`), not given in the chat; the chat only says it is done and where it is. Each walkthrough starts with a "Terms" section that defines, in simple language, every new term or tool the layer uses (for example MCP Inspector). Console guides in `.md` files stay short (only the steps to do). After the console work is checked and works, Claude explains in the chat what was done and why: what each resource and setting is, how it works, and how it fits the rest of the project (the human, 2026-10-23).
- Two catch-up walkthrough sessions are planned, at the human's request, because the built parts were never walked through: (1) Cycle 1 in depth, run locally: the flow from start to finish, each layer, its files and functions and where they are, and the decisions; (2) Cycle 2 steps 1 to 7: each step's code, console setup and AWS services. Step 7b was walked through when it finished (2026-10-23). From then on, walkthroughs happen as each step is built, not as full sessions.
- Git, as set by the human on 2026-10-12. Layer builds: the human does the branch, commits, PR and merge; Claude says when the work is ready and lists the changed files. Small changes (edits to `.md` files, small fixes, tooling tweaks): Claude commits and pushes them straight to `main` with no PR, but only after telling the human what it wants to commit and getting a yes (added by the human on 2026-10-13; every commit needs its own yes). Before the commit, check `git status` for anything that must not be committed. If it is unclear whether a change is small, ask. The GitHub workflow also runs on a push to `main` unless the push only touches Markdown files. A commit that touches only `CLAUDE.md` and/or `progress.md` skips the tests (the human, 2026-10-23: they waste time): run the secret scan by hand, then `git commit --no-verify`, and push straight to `main` (the yes is still asked).
- Hooks (added 2026-10-12):
  - `.githooks/pre-commit` runs `tools/checks.py --staged` before every commit: secret scan, `ruff`, `pytest`. A failure stops the commit. Enabled per clone with `git config core.hooksPath .githooks`. Needs `ruff` in the venv (`pip install ruff`).
  - `.claude/settings.json` has a Stop hook (`tools/review_gate.py`). When code has changed it runs the same checks and sends Claude back to fix failures. When they pass and the layer has not been reviewed, it asks for the code review.
  - Code review rule (set by the human on 2026-10-12): one review per layer, with the built-in code-review skill, when the layer is finished and before the PR. Claude runs it without being asked; the human never has to call it. Small changes outside a layer build are not reviewed: when the hook asks about one, say it is not a layer and carry on. Record it with `python tools/review_gate.py --reviewed`. After fixing review findings, re-run the checks only. Run a second review only if a fix rewrote logic substantially, and never more than two per layer. The hook counts a layer as everything since the branch left `main`; `python tools/review_gate.py --status` shows the count.
  - Extra review agents (the human, 2026-11-21; from the `pr-review-toolkit` plugin), run in the same step as the code review, before the PR: `pr-review-toolkit:silent-failure-hunter` on Layers 3, 5, 6 and 7 (error handling: `ToolError` versus the generic error, deadlines, throttles, limits), and `pr-review-toolkit:pr-test-analyzer` on every layer that adds tests (do the tests cover the layer's test list). Fix or report their findings the same way. Do not run the full `review-pr` command.
  - Plugins (the human, 2026-11-21): `superpowers` is turned off for this repo in `.claude/settings.json` (its brainstorm, worktree and test-first process duplicates this repo's layer process, and worktrees lack the PDFs and the venv). `security-guidance` stays on (Layers 5 to 7 add login and a public endpoint). The plugin `code-review` and `code-simplifier` are not used here: the built-in `/code-review` and `/simplify` cover them.
  - `.github/workflows/tests.yml` runs ruff and pytest on every pull request into `main` and on pushes to `main` that touch more than Markdown. It is pinned to `ubuntu-24.04`, `actions/checkout@v6` and `actions/setup-python@v6` (v7 of both exists; not adopted because its changes were not checked). GitHub has no AWS credentials, database or datasheet PDFs, so a test that needs one must skip itself there. `pytest.ini` limits collection to `tests/`; never put a paid call in a file pytest can collect.
  - A second Stop hook (`tools/progress_gate.py`, added 2026-10-12) keeps `progress.md` current. When files have changed and the log has no entry dated today, or a file changed after the log was last saved, it sends Claude back to write or update today's entry. It cannot see a session where no file changed, so log decisions made in discussion by hand.
- Build one layer at a time, in order. Do not start a layer until the previous layer's test passes.
- Keep the pipeline steps as separate small functions in separate modules. The entry point (local server in Cycle 1, Lambda handler in Cycle 2) only calls them in order.
- Keep all SQL in `rag/db.py`. Nothing else writes SQL strings.
- Never put secrets in code, in the repo, or in the frontend. No AWS keys in Git. Add `.env` and `.aws/` to `.gitignore` first.
- Every claim about an AWS price, limit, API field, or model ID that is marked VERIFY must be checked against the current AWS documentation or console before it is relied on.
- Keep `progress.md` (repo root) up to date. At the end of every session, add a dated entry at the top of the log: what was done, decisions, problems, what is still open. Update the human's checklist in the same file.
- Keep a cost log. Every Bedrock call records its token or query count to `runs/cost_log.csv`.
- Do not run any evaluation loop that calls a paid model until the number of calls and the estimated cost are printed and confirmed.

## Parallel layer builds (set by the human on 2026-10-13)

A layer that splits well is built by several agents at the same time, with Claude as the lead. This is a standing request from the human to use subagents for those layers. It was tried on Layer 2 and kept for Layers 4 and 7. When planning a parallel layer, try the contract's rules on real data before the agents start.

Project 2 layers used the split in `docs/CLAUDE_project2.md`. Project 3 layers (proposed by Claude on 2026-11-21, not from Ibrahim's documents; kept by Ibrahim on 2026-11-21):

| Layer | Build | Split |
|---|---|---|
| 0 setup | Single | Setup and checks only |
| 1 `rag/retrieve.py` | Single | `retrieve.py` and the change to `answer_question` depend on each other |
| 2 stdio server | Single | Small; `tools.py` and `server.py` are one piece |
| 3 tool tests, `Deadline` | Parallel | `datasheet_mcp/budget.py`; test writer (in-memory `Client` tool tests, `Deadline` with a fake clock) |
| 4 HTTP app | Single | One file (`app.py`); tests are curl and Inspector runs by hand |
| 5 login | Parallel | `datasheet_mcp/auth.py` (`CognitoVerifier`); test writer (token table with generated keys). The Cognito pool and the real login are done by the lead |
| 6 limits | Single | Edits `rag/usage.py` (see "Project 3 rules") |
| 7 deploy | Single | Console and deployment work; cloud calls are lead-only |
| 8 connect, measure, tear down | Single | Live measurements; paid and cloud calls are lead-only |

The table is the default. The layer's plan may change it with a reason (for example, a layer turns out smaller than expected).

How a parallel build runs:

1. Plan, in plan mode. The plan has a "Work split" section with: the contract (shared data shapes, function names and signatures); each agent's job and done test; file ownership, with no file given to two agents. A single-agent layer says so in one line instead.
2. Build. After the human approves the plan, the lead starts the agents together: `layer-builder` for each code piece and one `test-writer` (definitions in `.claude/agents/`). Each prompt carries the contract, the job, the owned files and the done test in full, because an agent starts with no memory of the conversation.
3. Integrate. The lead reads every agent's report and the code, joins the pieces, makes the changes agents may not make (`rag/config.py`, shared files), runs the full checks (`tools/checks.py`) and fixes what does not fit. A test that fails is fixed in the code unless the test contradicts the contract.
4. Review and hand over. One code review of the whole layer with the built-in code-review skill, as in the code review rule above. Then the lead updates `CLAUDE.md` and `progress.md` and tells the human the layer is ready, with the list of changed files.

Rules for the agents (also written into each definition):

- An agent changes only the files it owns. It reports problems in other files and does not fix them.
- Agents work in the normal project folder on separate files, not in git worktrees: the parsed datasheets, the PDFs and the venv are not in git, so a worktree would not have them.
- Agents never run a git command that changes anything. Commits, branches and pushes stay as in the Git rule.
- Agents never make a paid or cloud call. Only the lead does, after the cost is printed and confirmed.
- Agents never edit `CLAUDE.md`, `progress.md`, `rag/config.py`, or anything under `.claude/`, `.githooks/`, `.github/` or `tools/`.
- The test writer works from the contract and does not read the new code. Builders do not edit tests.
- An agent that finds the contract wrong stops and reports it. Only the lead changes the contract, and tells the human when it does.

