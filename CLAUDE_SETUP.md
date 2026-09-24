# Paperboy setup instructions — for Claude

You (Claude) are reading this because a researcher asked you to set up Paperboy on their machine. Follow these steps in order. The goal: a working local literature corpus for **their** field, exposed to Claude as an MCP server.

## What this project is

- `paperboy/` — a Python package (stdlib + `pypdf`) that harvests journal metadata from Crossref/OpenAlex, enriches DOIs with Unpaywall open-access locations, downloads legal OA PDFs, chunk-indexes everything into SQLite FTS, and serves it over a hand-rolled MCP stdio server (`paperboy/mcp_server.py`).
- `config/sources.toml` — the journal list (ISSN-pinned). This is the main thing to tailor per user.
- `data/paperboy.sqlite` — created locally on first run. Never committed.
- `memory/paperboy_analyst.md` — analyst standing instructions, auto-created with sensible defaults on first use.

## Step 1 — Interview the user (only what's missing)

Ask for anything not already in their prompt:
1. **Research field** and the **journals** they care about (5–15 is a good start).
2. **Academic email** — used only as the polite-pool `mailto` for Crossref/OpenAlex/Unpaywall.
3. **Date range** for the corpus (default: last 5–6 years).
4. **Google Drive for desktop?** If yes, PDFs and DB snapshots go to a Drive folder (ask which, default `G:/My Drive/Paperboy` on Windows). If no, use a local folder like `<repo>/data/pdfs` — set it as `drive.path` in `config/local.toml` anyway; that path is where PDFs are stored.

## Step 2 — Install

1. Verify Python 3.11+ (`python --version`). If missing, have the user install it.
2. From the repo root: `python -m pip install -e .`
3. Create `config/local.toml` from `config/local.example.toml` with their email and PDF/Drive path.

## Step 3 — Tailor the journal list

Rewrite `config/sources.toml` for their field, keeping the exact TOML shape (`key`, `name`, `publisher`, `issns = [..]`, `tier`). Look up ISSNs via the Crossref API (`https://api.crossref.org/journals?query=<name>`) or web search. Prefer the print ISSN first in the list; only the first ISSN is queried. Confirm the final list with the user before harvesting.

## Step 4 — Build the corpus

Run in order (adjust dates; use their email):

```
python -m paperboy init-db
python -m paperboy harvest-crossref --from-date 2020-01-01 --until-date <today> --mailto <email> --rows 50
python -m paperboy harvest-openalex --from-date 2020-01-01 --until-date <today> --mailto <email> --per-page 50
python -m paperboy enrich-unpaywall --email <email> --limit 500 --all
python -m paperboy index-corpus --rebuild
python -m paperboy download-pdfs --limit 2000 --index --max-pdf-pages 120
```

Notes:
- Harvests and enrichment are **resumable**; if interrupted, re-run the same command.
- Enrichment of a large corpus takes a while (~0.1 s/DOI). Run long steps in the background and report progress.
- Expect roughly **half** of recorded OA PDF locations to download. Wiley/Elsevier/OUP bot-filter plain downloads; the code falls back to repository copies (arXiv, PMC, university archives) and **must not** be modified to spoof browser user agents or touch SSRN delivery links — that is project policy.
- `harvest-ssrn`, `harvest-afa`, `harvest-wfa` are finance-specific; skip them for other fields unless asked.
- Optionally rewrite `METHOD_PATTERNS` and `DATA_PATTERNS` in `paperboy/knowledge.py` to the field's methods and datasets — these drive the `paperboy_trends` tool signals.

## Step 5 — Register the MCP server

1. `python -m paperboy mcp-config` prints the correct JSON block for this machine (it uses the running Python's absolute path).
2. Merge it into Claude Desktop's config, **preserving any existing `mcpServers` entries**:
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
3. For Claude Code, also write the same server entry to `.mcp.json` in the repo root.

## Step 6 — Verify before declaring success

Simulate the client handshake over stdio and confirm every response is a result, not an error:

```
printf '%s\n' \
'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
'{"jsonrpc":"2.0","method":"notifications/initialized"}' \
'{"jsonrpc":"2.0","id":1,"method":"ping"}' \
'{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"paperboy_overview","arguments":{}}}' \
| python -m paperboy mcp-server
```

Expect nine tools listed and a corpus overview with nonzero counts.

## Step 7 — Tell the user to restart Claude fully

Claude only discovers MCP servers at startup, and closing the window does **not** stop the app. Instruct the user, explicitly:

> Fully quit Claude: on Windows, right-click the Claude tray icon and quit — or open Task Manager, find the Claude process, and End Task. On macOS, Cmd-Q. Then reopen Claude.

Then have them test with: *"Use Paperboy to give me an overview of my corpus, then search it for [a topic in their field]."*

## Engineering gotchas (learned the hard way — do not reintroduce)

- The MCP server **must** answer `ping` with an empty result; an error reply makes Claude Desktop mark the server dead.
- Nothing may print to stdout in server mode — stdout is the protocol wire. `run_mcp_server` already redirects `sys.stdout` to stderr; keep it that way.
- Never hold a SQLite write transaction across PDF text extraction (`download_candidate_pdfs` commits before extracting). WAL mode plus `busy_timeout` is already configured in `db.connect`.
- On Windows, the server reconfigures stdout to UTF-8 with `\n` newlines; keep that too.
