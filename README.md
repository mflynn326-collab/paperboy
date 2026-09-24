# Paperboy

Paperboy is a local-first literature pipeline and research assistant for academic research. It builds a private, queryable corpus of journal metadata, abstracts, and legally available open-access full text for the journals *you* care about — then exposes that corpus to Claude as a set of tools (an MCP server), so you can ask real research questions in plain language:

- "How are recent papers studying retail investors and options trading, and what data do they use?"
- "Where would an idea about social media and bank runs fit in the literature?"
- "What methods are rising in corporate finance since 2022?"
- "Fetch the full text of this paper and tell me exactly how they construct their sample."

It was built for finance research (top finance journals, SSRN, AFA/WFA programs), but the pipeline is field-agnostic: point it at your own field's journals and it becomes your corpus.

## The fastest way to set this up: let Claude do it

This repository ships with [CLAUDE_SETUP.md](CLAUDE_SETUP.md) — step-by-step instructions written *for Claude*, not for you.

1. Install [Claude Desktop](https://claude.ai/download) (the Code tab) or Claude Code, plus [Python 3.11+](https://www.python.org/downloads/).
2. Download this repository (green **Code** button → **Download ZIP**, then unzip) or `git clone` it.
3. Open the folder in a Claude session and paste this prompt:

> Read CLAUDE_SETUP.md in this folder and follow it. Set up Paperboy for my research field. My field is [YOUR FIELD], and the journals I care most about are [LIST 5–15 JOURNALS]. My academic email is [YOU@SCHOOL.EDU].

Claude will install the package, look up your journals' ISSNs, build your corpus from Crossref/OpenAlex/Unpaywall, index it for search, and register the MCP server with Claude Desktop.

4. **Important — restart Claude completely.** Closing the window is not enough: fully quit Claude (on Windows, right-click the Claude icon in the system tray and quit, or end the Claude process in Task Manager; on macOS, Cmd-Q). Then reopen it. The Paperboy tools only appear after a full restart.
5. Test it: *"Use Paperboy to give me an overview of my corpus, then search it for [a topic in your field]."*

## Building your own corpus: what's happening under the hood

Your corpus is a SQLite database built from stable scholarly APIs — no scraping, no paywall bypass:

1. **Pick your journals.** [config/sources.toml](config/sources.toml) lists journals by ISSN. Replace the finance journals with your field's. (Claude can look up ISSNs for you; they're also on any journal's website.)
2. **Harvest metadata.** `harvest-crossref` and `harvest-openalex` pull every article those journals published in a date range — titles, abstracts, authors, DOIs.
3. **Find open-access full text.** `enrich-unpaywall` checks every DOI against [Unpaywall](https://unpaywall.org/) for legal open-access PDF locations. `download-pdfs` fetches them (about half of recorded OA locations resolve; bot-filtered publisher sites are skipped rather than circumvented, and SSRN delivery links are skipped by policy).
4. **Index everything.** `index-corpus` builds a full-text search index over abstracts and extracted PDF text in ~2,800-character chunks. When Claude answers a question it retrieves only the few relevant passages — never whole papers — so responses stay fast and grounded.
5. **Optional: warehouse PDFs in Google Drive.** If you use Google Drive for desktop, set `drive.path` in `config/local.toml` and PDFs are stored (and backed up) there. Without Drive, any local folder works.

Everything is resumable: re-running a harvest or enrichment picks up where it left off, and a weekly re-run keeps the corpus current.

## What you can do with it

Once registered, Claude has these tools against your corpus:

| Tool | What it does |
|---|---|
| `search` | Ranked full-text search over abstracts and PDFs; can be scoped to one paper by DOI |
| `fetch` | Full record for a paper: authors, venue, abstract, links, indexed passages |
| `paperboy_overview` | What the corpus contains: counts by journal, year, and source |
| `paperboy_trends` | Papers per year, venues, and method/data signals for any topic |
| `paperboy_fetch_fulltext` | Download and index specific open-access papers on demand |
| `paperboy_analyst_context` | A citation-backed context pack for brainstorming and literature placement |
| `paperboy_memory` / `paperboy_remember` | Durable analyst notes that persist across conversations |
| `paperboy_pdf_candidates` | Open-access PDFs available for a topic but not yet downloaded |

Good prompts to try:

- *"Use Paperboy to map the literature on [topic] since 2021: main questions, methods, datasets, and where the gaps are."*
- *"Which journals publish [topic] work, and how has the mix of methods shifted over time?"*
- *"Fetch full text for the three most relevant open-access papers on [topic] and compare their identification strategies."*
- *"Remember that I prioritize [your standards for a good paper]."* — this note persists for future sessions.

## Tailoring beyond the journal list

- **Method/data trend signals** ([paperboy/knowledge.py](paperboy/knowledge.py)): the `METHOD_PATTERNS` and `DATA_PATTERNS` regex lists drive the trends tool. They ship finance-flavored (diff-in-diff, CRSP, Compustat…) — ask Claude to rewrite them for your field's methods and datasets.
- **Working papers and conferences**: `harvest-ssrn`, `harvest-afa`, and `harvest-wfa` are finance-specific. Other fields can import any conference program as CSV with `import-conference-csv`, or ask Claude to write an adapter for your field's preprint server (OpenAlex covers most of them).
- **Analyst memory** ([memory/paperboy_analyst.md](paperboy/analyst.py), auto-created on first run): standing instructions for how Claude should reason over your corpus. Edit it to match how you evaluate research.

## Requirements and access notes

- Python 3.11+ (the package is nearly all standard library; the one dependency, `pypdf`, is installed automatically).
- Works on Windows, macOS, and Linux. Claude Desktop or Claude Code for the MCP integration; the CLI (`python -m paperboy search "..."` etc.) works standalone.
- The `mailto`/email values are not credentials — they put API requests in polite pools per the providers' guidelines.
- Paperboy deliberately does not scrape around access controls, spoof browser user agents, or bypass paywalls. For subscribed full text, the clean paths are institutional TDM agreements, publisher APIs, or manually adding PDFs you have legitimate access to.

## License and sharing

Share freely with colleagues. Each user builds their own corpus locally from public APIs; no article content is redistributed by this repository.
