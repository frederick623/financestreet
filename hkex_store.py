#!/usr/bin/env python3
"""Incrementally store HKEX Title Search news in SQLite."""

from __future__ import annotations

import argparse
import html
from io import BytesIO
import json
import logging
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from hkex_news import Criteria, HKEXError, HKEXNewsClient, absolute_url, parse_date
from pypdf import PdfReader
from pypdf.errors import PdfReadError


DEFAULT_DATABASE = Path(__file__).parent / "data" / "hkex_news.db"
HK_TIMEZONE = ZoneInfo("Asia/Hong_Kong")
TAG_PATTERN = re.compile(r"<[^>]+>")
MAX_PDF_BYTES = 50 * 1024 * 1024
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncResult:
    security_status: str
    from_date: date
    to_date: date
    records_saved: int


def configure_logging(log_file: Path) -> None:
    """Write sync activity to a file while progress remains visible in the terminal."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.handlers.clear()
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)


def report(message: str, *, stream: TextIO = sys.stderr) -> None:
    print(message, file=stream, flush=True)
    LOGGER.info(message)


def connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS news (
            news_id TEXT PRIMARY KEY,
            release_time TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            file_info TEXT NOT NULL,
            file_type TEXT NOT NULL,
            document_url TEXT NOT NULL,
            display_url TEXT NOT NULL,
            security_status TEXT NOT NULL CHECK (security_status IN ('current', 'delisted')),
            raw_json TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            document_text TEXT,
            document_downloaded_at TEXT,
            document_error TEXT
        );
        CREATE INDEX IF NOT EXISTS news_release_time_idx ON news(release_time DESC);
        CREATE INDEX IF NOT EXISTS news_stock_code_idx ON news(stock_code, release_time DESC);
        CREATE TABLE IF NOT EXISTS sync_state (
            security_status TEXT PRIMARY KEY CHECK (security_status IN ('current', 'delisted')),
            last_to_date TEXT NOT NULL,
            synced_at TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(news)")}
    for column, definition in (
        ("document_text", "TEXT"),
        ("document_downloaded_at", "TEXT"),
        ("document_error", "TEXT"),
    ):
        if column not in columns:
            connection.execute(f"ALTER TABLE news ADD COLUMN {column} {definition}")
    return connection


def release_time(value: str) -> str:
    return datetime.strptime(value, "%d/%m/%Y %H:%M").replace(
        tzinfo=HK_TIMEZONE
    ).isoformat()


def plain_text(value: str) -> str:
    return html.unescape(TAG_PATTERN.sub("", value)).strip()


def normalized_stock_codes(value: str) -> str:
    return " ".join(TAG_PATTERN.sub(" ", html.unescape(value)).split())


def row_for_storage(row: dict[str, str], security_status: str) -> tuple[str, ...]:
    return (
        row["NEWS_ID"],
        release_time(row["DATE_TIME"]),
        normalized_stock_codes(row.get("STOCK_CODE", "")),
        row.get("STOCK_NAME", ""),
        plain_text(row.get("LONG_TEXT", row.get("SHORT_TEXT", ""))),
        row.get("TITLE", "").replace("\n", " ").strip(),
        row.get("FILE_INFO", ""),
        row.get("FILE_TYPE", ""),
        absolute_url(row.get("FILE_LINK", "")),
        absolute_url(row.get("DOD_WEB_PATH", "")),
        security_status,
        json.dumps(row, ensure_ascii=False, sort_keys=True),
        datetime.now(HK_TIMEZONE).isoformat(),
    )


UPSERT_NEWS = """
INSERT INTO news (
    news_id, release_time, stock_code, stock_name, category, title, file_info,
    file_type, document_url, display_url, security_status, raw_json, synced_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(news_id) DO UPDATE SET
    release_time = excluded.release_time,
    stock_code = excluded.stock_code,
    stock_name = excluded.stock_name,
    category = excluded.category,
    title = excluded.title,
    file_info = excluded.file_info,
    file_type = excluded.file_type,
    document_url = excluded.document_url,
    display_url = excluded.display_url,
    security_status = excluded.security_status,
    raw_json = excluded.raw_json,
    synced_at = excluded.synced_at
"""


def download_pdf_text(url: str) -> str:
    """Download a PDF and extract its text, retrying transient HKEX failures."""
    request = Request(url, headers={"User-Agent": "hkex-news-downloader/1.0"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=60) as response:
                content = response.read(MAX_PDF_BYTES + 1)
        except (HTTPError, URLError, TimeoutError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise HKEXError(f"PDF download failed after 3 attempts: {url}") from error
        break
    else:
        raise HKEXError(f"PDF download failed after 3 attempts: {url}") from last_error
    if len(content) > MAX_PDF_BYTES:
        raise HKEXError(f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MiB download limit.")
    if not content.startswith(b"%PDF-"):
        raise HKEXError("Downloaded document is not a PDF.")
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)
    except PdfReadError as error:
        raise HKEXError(f"Unable to extract text from PDF: {url}") from error


def is_pdf_document(file_type: str, url: str) -> bool:
    """Identify PDFs from HKEX metadata, falling back to the stored URL suffix."""
    return file_type.strip().upper() == "PDF" or urlsplit(url).path.lower().endswith(".pdf")


def store_document_text(connection: sqlite3.Connection, news_ids: list[str]) -> None:
    """Populate text for newly discovered or previously failed document downloads."""
    downloaded_count = 0
    for news_id in news_ids:
        row = connection.execute(
            "SELECT document_url, file_type, document_text FROM news WHERE news_id = ?",
            (news_id,),
        ).fetchone()
        if row is None:
            continue
        url, file_type, existing_text = row
        if existing_text is not None:
            report(f"PDF {news_id}: skipped (already stored)")
            continue
        downloaded_at = datetime.now(HK_TIMEZONE).isoformat()
        if not url:
            report(f"PDF {news_id}: skipped (no document URL)")
            connection.execute(
                """
                UPDATE news
                SET document_text = '', document_downloaded_at = ?, document_error = NULL
                WHERE news_id = ?
                """,
                (downloaded_at, news_id),
            )
            connection.commit()
            continue
        if not is_pdf_document(file_type, url):
            report(f"PDF {news_id}: skipped (unsupported {file_type or 'unknown'} document)")
            connection.execute(
                """
                UPDATE news
                SET document_text = '', document_downloaded_at = ?, document_error = NULL
                WHERE news_id = ?
                """,
                (downloaded_at, news_id),
            )
            connection.commit()
            continue
        if downloaded_count:
            delay = random.randint(1, 10)
            report(f"PDF {news_id}: waiting {delay}s before download")
            time.sleep(delay)
        report(f"PDF {news_id}: downloading")
        try:
            text = download_pdf_text(url)
        except HKEXError as error:
            report(f"PDF {news_id}: failed: {error}")
            connection.execute(
                "UPDATE news SET document_error = ? WHERE news_id = ?",
                (str(error), news_id),
            )
        else:
            report(f"PDF {news_id}: stored {len(text):,} characters")
            connection.execute(
                """
                UPDATE news
                SET document_text = ?, document_downloaded_at = ?, document_error = NULL
                WHERE news_id = ?
                """,
                (text, downloaded_at, news_id),
            )
        # Keep completed documents when an operator interrupts a long backfill.
        connection.commit()
        downloaded_count += 1


def backfill_document_text(connection: sqlite3.Connection) -> None:
    """Extract text from every unprocessed PDF stored in the database."""
    missing_ids = [
        news_id
        for news_id, file_type, url in connection.execute(
            """
            SELECT news_id, file_type, document_url
            FROM news
            WHERE document_text IS NULL
            """
        )
        if is_pdf_document(file_type, url)
    ]
    report(f"PDF backfill: processing {len(missing_ids):,} records")
    store_document_text(connection, missing_ids)
    connection.commit()
    report("PDF backfill: complete")


def last_synced_date(connection: sqlite3.Connection, security_status: str) -> date | None:
    row = connection.execute(
        "SELECT last_to_date FROM sync_state WHERE security_status = ?",
        (security_status,),
    ).fetchone()
    return date.fromisoformat(row[0]) if row else None


def sync(
    database: Path,
    security_statuses: tuple[str, ...],
    initial_from_date: date | None,
    to_date: date,
    backfill_pdfs: bool = False,
) -> list[SyncResult]:
    connection = connect(database)
    client = HKEXNewsClient()
    try:
        report(
            f"Sync started: {', '.join(security_statuses)} records through "
            f"{to_date.isoformat()} into {database}"
        )
        starts: dict[str, date] = {}
        for security_status in security_statuses:
            previous_date = last_synced_date(connection, security_status)
            if previous_date is None and initial_from_date is None:
                raise HKEXError(
                    f"No previous {security_status} sync exists. "
                    "Use --from to choose the first date to download."
                )
            starts[security_status] = initial_from_date or previous_date

        results: list[SyncResult] = []
        for security_status, from_date in starts.items():
            if from_date > to_date:
                report(
                    f"Sync {security_status}: skipped because "
                    f"{from_date.isoformat()} is after {to_date.isoformat()}"
                )
                continue
            report(
                f"Sync {security_status}: downloading records from "
                f"{from_date.isoformat()} through {to_date.isoformat()}"
            )
            criteria = Criteria(
                from_date=from_date,
                to_date=to_date,
                security_status=security_status,
                stock_id="",
                title="",
                search_mode="all",
                document_type="-2",
                t1_code="-2",
                t2_group_code="-2",
                t2_code="-2",
                sort_by="DateTime",
                sort_order="desc",
            )
            records = client.search_all(criteria)
            report(f"Sync {security_status}: received {len(records):,} records")
            connection.executemany(
                UPSERT_NEWS,
                (row_for_storage(record, security_status) for record in records),
            )
            record_ids = [str(record["NEWS_ID"]) for record in records]
            if record_ids:
                store_document_text(connection, record_ids)
            connection.execute(
                """
                INSERT INTO sync_state (security_status, last_to_date, synced_at)
                VALUES (?, ?, ?)
                ON CONFLICT(security_status) DO UPDATE SET
                    last_to_date = excluded.last_to_date,
                    synced_at = excluded.synced_at
                """,
                (security_status, to_date.isoformat(), datetime.now(HK_TIMEZONE).isoformat()),
            )
            connection.commit()
            report(f"Sync {security_status}: saved {len(records):,} records")
            results.append(SyncResult(security_status, from_date, to_date, len(records)))
        if backfill_pdfs:
            backfill_document_text(connection)
        report("Sync complete")
        return results
    finally:
        connection.close()


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize all HKEX current and/or delisted news since the prior "
            "successful download. The first sync requires --from."
        )
    )
    parser.add_argument(
        "command",
        choices=("sync", "backfill-pdfs"),
        help=(
            "sync downloads new records and upserts the overlap from the prior sync date; "
            "backfill-pdfs processes every stored PDF missing extracted text."
        ),
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Write sync activity to this file; default: beside the database with a .log suffix.",
    )
    parser.add_argument("--from", dest="from_date", type=parse_date)
    parser.add_argument("--to", dest="to_date", type=parse_date)
    parser.add_argument(
        "--security-status",
        choices=("all", "current", "delisted"),
        default="all",
        help="Security lists to download; default: all.",
    )
    parser.add_argument(
        "--backfill-pdfs",
        action="store_true",
        help="Download PDFs for all existing records that do not yet have extracted text.",
    )
    return parser


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()
    log_file = args.log_file or args.database.with_suffix(".log")
    configure_logging(log_file)
    to_date = args.to_date or datetime.now(HK_TIMEZONE).date()
    if args.from_date and args.from_date > to_date:
        parser.error("--from must not be later than --to.")
    statuses = ("current", "delisted") if args.security_status == "all" else (args.security_status,)
    try:
        if args.command == "backfill-pdfs":
            connection = connect(args.database)
            try:
                backfill_document_text(connection)
            finally:
                connection.close()
            return 0
        results = sync(args.database, statuses, args.from_date, to_date, args.backfill_pdfs)
    except KeyboardInterrupt:
        report("Interrupted: completed document updates have been saved.")
        return 130
    except (HKEXError, sqlite3.Error, ValueError) as error:
        LOGGER.error("Sync failed: %s", error)
        parser.exit(1, f"error: {error}\n")
    for result in results:
        report(
            f"{result.security_status}: saved {result.records_saved:,} records "
            f"from {result.from_date.isoformat()} through {result.to_date.isoformat()}",
            stream=sys.stdout,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
