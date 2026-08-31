#!/usr/bin/env python3
"""Read-only MCP tools for querying the local HKEX news database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from hkex_store import DEFAULT_DATABASE


database_path = DEFAULT_DATABASE
mcp = FastMCP("HKEX News")


def readonly_connection() -> sqlite3.Connection:
    if not database_path.is_file():
        raise RuntimeError(
            f"HKEX database does not exist at {database_path}. "
            "Run `python hkex_store.py sync --from YYYY-MM-DD` first."
        )
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def validate_limit(limit: int) -> int:
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200.")
    return limit


def validate_iso_date(value: str, parameter: str) -> None:
    if not value:
        return
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{parameter} must use YYYY-MM-DD.") from error


@mcp.tool()
def database_status() -> dict[str, Any]:
    """Return database size and the last successful sync for each security list."""
    with readonly_connection() as connection:
        count, earliest, latest = connection.execute(
            "SELECT COUNT(*), MIN(release_time), MAX(release_time) FROM news"
        ).fetchone()
        syncs = [
            dict(row)
            for row in connection.execute(
                "SELECT security_status, last_to_date, synced_at FROM sync_state "
                "ORDER BY security_status"
            )
        ]
    return {
        "database": str(database_path),
        "record_count": count,
        "earliest_release_time": earliest,
        "latest_release_time": latest,
        "syncs": syncs,
    }


@mcp.tool()
def search_news(
    query: str = "",
    stock_code: str = "",
    category: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search stored HKEX news by keywords, stock code, category, and date range."""
    validate_iso_date(from_date, "from_date")
    validate_iso_date(to_date, "to_date")
    if from_date and to_date and from_date > to_date:
        raise ValueError("from_date must not be later than to_date.")
    where: list[str] = []
    values: list[Any] = []
    if query:
        term = f"%{escape_like(query)}%"
        where.append("(title LIKE ? ESCAPE '\\' OR stock_name LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\')")
        values.extend((term, term, term))
    if stock_code:
        normalized_code = stock_code.zfill(5) if stock_code.isdigit() else stock_code
        escaped_code = escape_like(normalized_code)
        where.append(
            "(stock_code = ? OR stock_code LIKE ? ESCAPE '\\' "
            "OR stock_code LIKE ? ESCAPE '\\')"
        )
        values.extend((normalized_code, f"{escaped_code} %", f"% {escaped_code} %"))
    if category:
        where.append("category LIKE ? ESCAPE '\\'")
        values.append(f"%{escape_like(category)}%")
    if from_date:
        where.append("release_time >= ?")
        values.append(f"{from_date}T00:00:00+08:00")
    if to_date:
        where.append("release_time <= ?")
        values.append(f"{to_date}T23:59:59+08:00")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    statement = f"""
        SELECT news_id, release_time, stock_code, stock_name, category, title,
               file_info, file_type, document_url, display_url, security_status
        FROM news
        {clause}
        ORDER BY release_time DESC
        LIMIT ?
    """
    with readonly_connection() as connection:
        rows = [dict(row) for row in connection.execute(statement, (*values, validate_limit(limit)))]
    return rows


@mcp.tool()
def get_news(news_id: str) -> dict[str, Any]:
    """Return one stored announcement, including its original HKEX response fields."""
    with readonly_connection() as connection:
        row = connection.execute(
            """
            SELECT news_id, release_time, stock_code, stock_name, category, title,
                   file_info, file_type, document_url, display_url, security_status, raw_json
            FROM news WHERE news_id = ?
            """,
            (news_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"No stored announcement has news ID {news_id!r}.")
    result = dict(row)
    result["original_hkex_record"] = json.loads(result.pop("raw_json"))
    return result


@mcp.tool()
def list_categories(limit: int = 100) -> list[dict[str, Any]]:
    """List stored headline categories with their number of announcements."""
    with readonly_connection() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT category, COUNT(*) AS record_count
                FROM news
                GROUP BY category
                ORDER BY record_count DESC, category ASC
                LIMIT ?
                """,
                (validate_limit(limit),),
            )
        ]
    return rows


def main() -> None:
    global database_path
    parser = argparse.ArgumentParser(description="Run the HKEX News MCP server over stdio.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()
    database_path = args.database.resolve()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
