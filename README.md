# HKEX News Database

This project downloads HKEX Title Search records into a local SQLite database and
exposes a read-only MCP server for AI assistants.

## Setup and first download

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python hkex_store.py sync --from 2026-08-01
```

The initial date determines how much historical data is downloaded. The default
sync includes both current and delisted securities. Later runs require no dates
and re-download the previous sync date to capture late postings safely:

```bash
.venv/bin/python hkex_store.py sync
```

Use `--security-status current` or `--security-status delisted` to synchronize
only one list. The database is stored at `data/hkex_news.db`.
Sync progress is printed to the terminal and logged to `data/hkex_news.log`;
use `--log-file PATH` to select a different log file.

## MCP server

`.mcp.json` configures the local stdio server for GitHub Copilot-compatible
clients that support workspace MCP configuration. After the virtual environment
and first sync exist, trust and start **hkex-news** in the client's MCP server
manager.

For another local MCP client, add the equivalent server configuration:

```json
{
  "command": "/absolute/path/to/financestreet/.venv/bin/python",
  "args": [
    "/absolute/path/to/financestreet/hkex_mcp.py",
    "--database",
    "/absolute/path/to/financestreet/data/hkex_news.db"
  ]
}
```

During sync, each linked PDF is downloaded and its extracted text is stored in
the `news.document_text` column. HKEX's `file_type` field identifies most PDFs;
the stored `document_url` also normally ends in `.pdf` and is used as a fallback
when that field is absent. Failed downloads retain an error in
`news.document_error` and are retried on the next overlapping sync. The server
provides `database_status`, `search_news`, `get_news`, and `list_categories`;
`search_news` searches document text and `get_news` returns it. It only opens
the SQLite database in read-only mode. PDF downloads are spaced by a random
1–300 second delay, and sync reports whether each PDF was skipped, downloaded,
stored, or failed. To process every stored PDF from every date without making a
new HKEX search, run `backfill-pdfs`:

```bash
.venv/bin/python hkex_store.py backfill-pdfs
```
