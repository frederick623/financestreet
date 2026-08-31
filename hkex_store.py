#!/usr/bin/env python3
"""Incrementally store HKEX Title Search news in SQLite."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from hkex_news import Criteria, HKEXError, HKEXNewsClient, absolute_url, parse_date


DEFAULT_DATABASE = Path(__file__).parent / "data" / "hkex_news.db"
HK_TIMEZONE = ZoneInfo("Asia/Hong_Kong")
TAG_PATTERN = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class SyncResult:
    security_status: str
    from_date: date
    to_date: date
    records_saved: int


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
            synced_at TEXT NOT NULL
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
) -> list[SyncResult]:
    connection = connect(database)
    client = HKEXNewsClient()
    try:
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
                continue
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
            connection.executemany(
                UPSERT_NEWS,
                (row_for_storage(record, security_status) for record in records),
            )
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
            results.append(SyncResult(security_status, from_date, to_date, len(records)))
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
        choices=("sync",),
        help="sync downloads new records and upserts the overlap from the prior sync date.",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--from", dest="from_date", type=parse_date)
    parser.add_argument("--to", dest="to_date", type=parse_date)
    parser.add_argument(
        "--security-status",
        choices=("all", "current", "delisted"),
        default="all",
        help="Security lists to download; default: all.",
    )
    return parser


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()
    to_date = args.to_date or datetime.now(HK_TIMEZONE).date()
    if args.from_date and args.from_date > to_date:
        parser.error("--from must not be later than --to.")
    statuses = ("current", "delisted") if args.security_status == "all" else (args.security_status,)
    try:
        results = sync(args.database, statuses, args.from_date, to_date)
    except (HKEXError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"error: {error}\n")
    for result in results:
        print(
            f"{result.security_status}: saved {result.records_saved:,} records "
            f"from {result.from_date.isoformat()} through {result.to_date.isoformat()}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
