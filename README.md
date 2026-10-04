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
sync includes current securities only, excluding delisted stocks. Later runs
require no dates and re-download the previous sync date to capture late
postings safely:

```bash
.venv/bin/python hkex_store.py sync
```

Only announcements listing at least one stock code below 10000 are stored;
notices solely for derivative warrants, CBBCs, debt securities and other
codes of 10000 or above are skipped.

Use `--security-status all` or `--security-status delisted` when historical
delisted announcements are required. The database is stored at
`data/hkex_news.db`.
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

### Remote access over HTTP

To let clients on other machines connect, serve the same read-only tools over
streamable HTTP. A bearer token of at least 16 characters is required:

```bash
export HKEX_MCP_TOKEN="$(openssl rand -hex 32)"
.venv/bin/python hkex_mcp.py --transport http --host 0.0.0.0 --port 8000
```

Clients connect to `http://<server>:8000/mcp` with the header
`Authorization: Bearer <token>`:

```json
{
  "type": "http",
  "url": "https://mcp.example.com/mcp",
  "headers": { "Authorization": "Bearer <token>" }
}
```

Use `--allowed-host mcp.example.com` (repeatable) to restrict accepted `Host`
headers. The token travels in plain text over HTTP, so put the server behind a
TLS reverse proxy (Caddy, nginx) or a tunnel (Tailscale, Cloudflare Tunnel)
rather than exposing the port directly to the internet.

During sync, each linked PDF for a current security is downloaded and its
extracted text is stored in the `news.document_text` column. HKEX's `file_type`
field identifies most PDFs; the stored `document_url` also normally ends in
`.pdf` and is used as a fallback when that field is absent. Failed downloads retain an error in `news.document_error` and are retried on the next
overlapping sync. PDFs that exceed the 50 MiB limit, cannot be read, or contain
text that cannot be stored as UTF-8 are registered in `invalid_document_urls`
and marked ignored with a timestamp in `news.document_ignored_at`; the URL is
never retried by later syncs or backfills. Downloads run in randomized batches
of one to five simultaneous requests.
The server provides `database_status`, `search_news`, `get_news`, and
`list_categories`; `search_news` searches document text and supports an exact
sector filter, while `get_news` returns document text and the assigned sector.
`list_sectors` summarizes classified companies and announcements. The server
only opens the SQLite database in read-only mode. To process every stored
current-security PDF from every date without making a new HKEX search, run
`backfill-pdfs`:

```bash
.venv/bin/python hkex_store.py backfill-pdfs
```

Sync and PDF backfill automatically reuse an existing company sector, including
for renamed companies sharing a stock code. For a previously unseen company,
they analyze principal-business passages in annual/interim reports against the
industries in `sector.csv` and propagate a confident match to every announcement
for that issuer. To run this classification independently:

```bash
.venv/bin/python hkex_store.py classify-sectors
```

Only unambiguous matches are assigned. Provenance is retained in
`company_sectors` (`source_news_id`, evidence passage, and classification time);
unmatched companies keep a null `news.sector`.
