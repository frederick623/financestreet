#!/usr/bin/env python3
"""Incrementally store HKEX Title Search news in SQLite."""

from __future__ import annotations

import argparse
import csv
import html
from concurrent.futures import ThreadPoolExecutor
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
DEFAULT_SECTOR_FILE = Path(__file__).parent / "sector.csv"
HK_TIMEZONE = ZoneInfo("Asia/Hong_Kong")
TAG_PATTERN = re.compile(r"<[^>]+>")
MAX_PDF_BYTES = 50 * 1024 * 1024
LOGGER = logging.getLogger(__name__)

SECTOR_PHRASES = {
    "Financials": (
        "banking",
        "commercial bank",
        "insurance",
        "asset management",
        "investment management",
        "securities brokerage",
        "financial services",
        "money lending",
        "wealth management",
    ),
    "Information Technology": (
        "software",
        "semiconductor",
        "information technology",
        "it services",
        "cloud computing",
        "cybersecurity",
        "computer hardware",
        "data centre",
        "data center",
    ),
    "Health Care": (
        "pharmaceutical",
        "biotechnology",
        "biotech",
        "medical device",
        "medical equipment",
        "hospital",
        "healthcare services",
        "health care services",
        "immuno-oncology",
        "new drugs",
        "innovative drug",
    ),
    "Consumer Discretionary": (
        "non-essential consumer",
        "automobile",
        "motor vehicle",
        "vehicle dealership",
        "department store",
        "fashion retail",
        "apparel",
        "home appliances",
        "education services",
        "restaurant",
        "hotel",
        "tourism",
        "travel services",
        "leisure",
        "jewellery",
        "jewelry",
    ),
    "Consumer Staples": (
        "food and beverage",
        "food products",
        "beverage",
        "dairy",
        "household products",
        "personal care products",
        "supermarket",
        "grocery",
        "agricultural products",
    ),
    "Energy": (
        "oil and gas",
        "crude oil",
        "natural gas",
        "petroleum",
        "coal mining",
        "coal production",
        "renewable energy",
        "solar power",
        "wind power",
    ),
    "Industrials": (
        "manufacturing",
        "aerospace",
        "defence",
        "defense",
        "transportation",
        "logistics",
        "freight forwarding",
        "industrial machinery",
        "construction services",
        "building maintenance",
        "renovation services",
    ),
    "Materials": (
        "chemicals",
        "chemical products",
        "metal mining",
        "mining and processing",
        "iron ore",
        "non-ferrous metal",
        "paper products",
        "packaging products",
        "cement",
        "building materials",
    ),
    "Real Estate": (
        "real estate investment trust",
        "property development",
        "property investment",
        "property management",
        "real estate development",
        "real estate management",
    ),
    "Communication Services": (
        "telecommunications",
        "telecommunication services",
        "media and entertainment",
        "internet platform",
        "online platform",
        "online games",
        "online entertainment",
        "digital media",
        "advertising services",
        "film production",
        "television broadcasting",
        "publishing",
    ),
    "Utilities": (
        "electric utility",
        "electricity generation",
        "power generation",
        "gas utility",
        "water utility",
        "water supply",
        "sewage treatment",
    ),
}
BUSINESS_MARKERS = re.compile(
    r"\b(?:principal(?:ly)? (?:activities|activity|business|businesses|engaged)|"
    r"mainly engaged|primarily engaged)\b",
    re.IGNORECASE,
)


class PDFContentError(HKEXError):
    """Raised when a downloaded PDF cannot be processed and should be ignored."""


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
            document_error TEXT,
            document_ignored_at TEXT,
            sector TEXT
        );
        CREATE INDEX IF NOT EXISTS news_release_time_idx ON news(release_time DESC);
        CREATE INDEX IF NOT EXISTS news_stock_code_idx ON news(stock_code, release_time DESC);
        CREATE TABLE IF NOT EXISTS invalid_document_urls (
            document_url TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            ignored_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_state (
            security_status TEXT PRIMARY KEY CHECK (security_status IN ('current', 'delisted')),
            last_to_date TEXT NOT NULL,
            synced_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS company_sectors (
            stock_name TEXT PRIMARY KEY,
            sector TEXT NOT NULL,
            source_news_id TEXT NOT NULL,
            evidence TEXT NOT NULL,
            classified_at TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(news)")}
    for column, definition in (
        ("document_text", "TEXT"),
        ("document_downloaded_at", "TEXT"),
        ("document_error", "TEXT"),
        ("document_ignored_at", "TEXT"),
        ("sector", "TEXT"),
    ):
        if column not in columns:
            connection.execute(f"ALTER TABLE news ADD COLUMN {column} {definition}")
    return connection


def load_sector_names(sector_file: Path) -> set[str]:
    """Read and validate the sector taxonomy used by the classifier."""
    with sector_file.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != ["Sector", "Key Industries", "Nature"]:
            raise ValueError(
                f"{sector_file} must have Sector, Key Industries, and Nature columns."
            )
        sectors = {row["Sector"].strip() for row in reader if row["Sector"].strip()}
    missing_rules = sectors - SECTOR_PHRASES.keys()
    unknown_rules = SECTOR_PHRASES.keys() - sectors
    if missing_rules or unknown_rules:
        raise ValueError(
            "Sector taxonomy and classification rules differ: "
            f"missing rules={sorted(missing_rules)}, unknown rules={sorted(unknown_rules)}"
        )
    return sectors


def business_passages(document_text: str) -> list[str]:
    """Extract issuer-business passages and exclude incidental document mentions."""
    normalized = " ".join(document_text.split())
    passages: list[str] = []
    for match in BUSINESS_MARKERS.finditer(normalized):
        start = max(0, match.start() - 120)
        end = min(len(normalized), match.end() + 600)
        passages.append(normalized[start:end])
        if len(passages) == 12:
            break
    return passages


def classify_document_sector(document_text: str) -> tuple[str, str] | None:
    """Classify a document when its business passages support one clear sector."""
    passages = business_passages(document_text)
    if not passages:
        return None
    for passage in passages:
        marker = BUSINESS_MARKERS.search(passage)
        if marker is None:
            continue
        sentence_end = passage.find(".", marker.end() + 20)
        scope_end = sentence_end + 1 if sentence_end >= 0 else len(passage)
        scope = passage[marker.start() : min(scope_end, marker.start() + 450)]
        text = scope.casefold()
        scores: dict[str, int] = {}
        matched_phrases: dict[str, list[str]] = {}
        for sector, phrases in SECTOR_PHRASES.items():
            matches = [
                phrase
                for phrase in phrases
                if re.search(rf"\b{re.escape(phrase)}s?\b", text)
            ]
            if matches:
                matched_phrases[sector] = matches
                scores[sector] = sum(3 if " " in phrase else 2 for phrase in matches)
        if not scores:
            continue
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        winner, winner_score = ranked[0]
        runner_up_score = ranked[1][1] if len(ranked) > 1 else 0
        if winner_score >= 2 and winner_score - runner_up_score >= 2:
            return winner, scope
    return None


class IssuerGroups:
    """Connect issuer names that share any stock code, including multi-code rows."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.parent: dict[str, str] = {}
        for stock_code, stock_name in rows:
            identifiers = [f"name:{stock_name}"]
            identifiers.extend(f"code:{code}" for code in stock_code.split())
            for identifier in identifiers:
                self.parent.setdefault(identifier, identifier)
            for identifier in identifiers[1:]:
                self.union(identifiers[0], identifier)

    def find(self, identifier: str) -> str:
        parent = self.parent[identifier]
        if parent != identifier:
            self.parent[identifier] = self.find(parent)
        return self.parent[identifier]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root

    def issuer(self, stock_name: str) -> str:
        return self.find(f"name:{stock_name}")


def classify_company_sectors(
    connection: sqlite3.Connection, sector_file: Path
) -> tuple[int, int]:
    """Reuse known issuer sectors, then classify and propagate previously unseen ones."""
    sector_names = load_sector_names(sector_file)
    issuer_rows = [
        (stock_code, stock_name)
        for stock_code, stock_name in connection.execute(
            """
            SELECT DISTINCT stock_code, stock_name
            FROM news
            WHERE trim(stock_code) <> '' AND trim(stock_name) <> ''
            """
        )
    ]
    groups = IssuerGroups(issuer_rows)
    classified: dict[str, tuple[str, str, str]] = {}
    for stock_name, sector, news_id, evidence in connection.execute(
        """
        SELECT stock_name, sector, source_news_id, evidence
        FROM company_sectors
        """
    ):
        if f"name:{stock_name}" not in groups.parent:
            continue
        if sector not in sector_names:
            raise ValueError(
                f"Stored sector {sector!r} for {stock_name!r} is not in {sector_file}."
            )
        issuer = groups.issuer(stock_name)
        previous = classified.get(issuer)
        if previous is not None and previous[0] != sector:
            raise ValueError(
                f"Conflicting stored sectors for issuer group containing {stock_name!r}: "
                f"{previous[0]!r} and {sector!r}."
            )
        classified[issuer] = (sector, news_id, evidence)

    reports = connection.execute(
        """
        SELECT news_id, stock_name, document_text
        FROM news
        WHERE document_text IS NOT NULL
          AND trim(stock_name) <> ''
          AND (
              lower(title) LIKE '%annual report%'
              OR lower(title) LIKE '%interim report%'
              OR lower(title) LIKE '%annual results%'
              OR lower(title) LIKE '%interim results%'
              OR lower(title) LIKE '%results announcement%'
          )
        ORDER BY release_time DESC
        """
    )
    for news_id, stock_name, document_text in reports:
        issuer = groups.issuer(stock_name)
        if issuer in classified:
            continue
        result = classify_document_sector(document_text)
        if result is not None:
            sector, evidence = result
            classified[issuer] = (sector, news_id, evidence)

    classified_at = datetime.now(HK_TIMEZONE).isoformat()
    assignments: list[tuple[str, str, str, str, str]] = []
    for _, stock_name in issuer_rows:
        match = classified.get(groups.issuer(stock_name))
        if match is not None:
            sector, news_id, evidence = match
            assignments.append((stock_name, sector, news_id, evidence, classified_at))
    connection.executemany(
        """
        INSERT INTO company_sectors (
            stock_name, sector, source_news_id, evidence, classified_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(stock_name) DO NOTHING
        """,
        assignments,
    )
    connection.execute(
        """
        UPDATE news
        SET sector = (
            SELECT company_sectors.sector
            FROM company_sectors
            WHERE company_sectors.stock_name = news.stock_name
        )
        WHERE EXISTS (
            SELECT 1 FROM company_sectors
            WHERE company_sectors.stock_name = news.stock_name
        )
        """
    )
    connection.commit()
    total_issuers = len({groups.issuer(name) for _, name in issuer_rows})
    return len(classified), total_issuers


def release_time(value: str) -> str:
    return datetime.strptime(value, "%d/%m/%Y %H:%M").replace(
        tzinfo=HK_TIMEZONE
    ).isoformat()


def plain_text(value: str) -> str:
    return html.unescape(TAG_PATTERN.sub("", value)).strip()


def normalized_stock_codes(value: str) -> str:
    return " ".join(TAG_PATTERN.sub(" ", html.unescape(value)).split())


MAX_STOCK_CODE = 10000


def has_main_stock_code(row: dict[str, str]) -> bool:
    """True when any listed code is below 10000, excluding warrants, CBBCs and debt-only notices."""
    return any(
        code.isdigit() and int(code) < MAX_STOCK_CODE
        for code in normalized_stock_codes(row.get("STOCK_CODE", "")).split()
    )


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
        raise PDFContentError(
            f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MiB download limit."
        )
    if not content.startswith(b"%PDF-"):
        raise PDFContentError("Downloaded document is not a PDF.")
    try:
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)
        text.encode("utf-8")
        return text
    except (PdfReadError, UnicodeError) as error:
        raise PDFContentError(f"Unable to extract text from PDF: {url}") from error


def is_pdf_document(file_type: str, url: str) -> bool:
    """Identify PDFs from HKEX metadata, falling back to the stored URL suffix."""
    return file_type.strip().upper() == "PDF" or urlsplit(url).path.lower().endswith(".pdf")


def mark_document_url_invalid(
    connection: sqlite3.Connection, url: str, reason: str, ignored_at: str
) -> None:
    """Persist a terminal document failure for every record sharing its URL."""
    connection.execute(
        """
        INSERT INTO invalid_document_urls (document_url, reason, ignored_at)
        VALUES (?, ?, ?)
        ON CONFLICT(document_url) DO UPDATE SET
            reason = excluded.reason,
            ignored_at = excluded.ignored_at
        """,
        (url, reason, ignored_at),
    )
    connection.execute(
        """
        UPDATE news
        SET document_downloaded_at = ?, document_error = ?, document_ignored_at = ?
        WHERE document_url = ? AND document_text IS NULL
        """,
        (ignored_at, reason, ignored_at, url),
    )


def store_document_text(connection: sqlite3.Connection, news_ids: list[str]) -> None:
    """Populate text for active securities in randomized concurrent batches."""
    downloads: list[tuple[str, str]] = []
    for news_id in news_ids:
        row = connection.execute(
            """
            SELECT document_url, file_type, document_text, security_status,
                   document_ignored_at
            FROM news WHERE news_id = ?
            """,
            (news_id,),
        ).fetchone()
        if row is None:
            continue
        url, file_type, existing_text, security_status, ignored_at = row
        if existing_text is not None:
            report(f"PDF {news_id}: skipped (already stored)")
            continue
        if ignored_at is not None:
            report(f"PDF {news_id}: skipped (previously ignored)")
            continue
        invalid_url = connection.execute(
            """
            SELECT reason, ignored_at
            FROM invalid_document_urls
            WHERE document_url = ?
            """,
            (url,),
        ).fetchone()
        if invalid_url is not None:
            reason, ignored_at = invalid_url
            mark_document_url_invalid(connection, url, reason, ignored_at)
            report(f"PDF {news_id}: skipped (invalid document URL)")
            continue
        if security_status != "current":
            report(f"PDF {news_id}: skipped (delisted security)")
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
        downloads.append((news_id, url))

    while downloads:
        batch_size = random.randint(1, 5)
        batch, downloads = downloads[:batch_size], downloads[batch_size:]
        report(f"PDF batch: downloading {len(batch)} document(s) simultaneously")
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {
                news_id: executor.submit(download_pdf_text, url)
                for news_id, url in batch
            }
            for news_id, url in batch:
                downloaded_at = datetime.now(HK_TIMEZONE).isoformat()
                try:
                    text = futures[news_id].result()
                except PDFContentError as error:
                    report(f"PDF {news_id}: ignored: {error}")
                    mark_document_url_invalid(connection, url, str(error), downloaded_at)
                except HKEXError as error:
                    report(f"PDF {news_id}: failed: {error}")
                    connection.execute(
                        "UPDATE news SET document_error = ? WHERE news_id = ?",
                        (str(error), news_id),
                    )
                else:
                    report(f"PDF {news_id}: stored {len(text):,} characters")
                    try:
                        connection.execute(
                            """
                            UPDATE news
                            SET document_text = ?, document_downloaded_at = ?,
                                document_error = NULL
                            WHERE news_id = ?
                            """,
                            (text, downloaded_at, news_id),
                        )
                    except UnicodeError as error:
                        reason = f"Unable to store PDF text as UTF-8: {error}"
                        report(f"PDF {news_id}: ignored: {reason}")
                        mark_document_url_invalid(connection, url, reason, downloaded_at)
        # Keep completed documents when an operator interrupts a long backfill.
        connection.commit()


def backfill_document_text(connection: sqlite3.Connection) -> None:
    """Extract text from every unprocessed PDF stored in the database."""
    missing_ids = [
        news_id
        for news_id, file_type, url in connection.execute(
            """
            SELECT news_id, file_type, document_url
            FROM news
            WHERE document_text IS NULL
              AND document_ignored_at IS NULL
              AND security_status = 'current'
              AND NOT EXISTS (
                  SELECT 1
                  FROM invalid_document_urls
                  WHERE invalid_document_urls.document_url = news.document_url
              )
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
    sector_file: Path = DEFAULT_SECTOR_FILE,
) -> list[SyncResult]:
    connection = connect(database)
    client = HKEXNewsClient()
    try:
        load_sector_names(sector_file)
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
            received = client.search_all(criteria)
            records = [record for record in received if has_main_stock_code(record)]
            report(
                f"Sync {security_status}: received {len(received):,} records, "
                f"kept {len(records):,} with a stock code below {MAX_STOCK_CODE}"
            )
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
        matched, total = classify_company_sectors(connection, sector_file)
        report(f"Sector classification: matched {matched:,} of {total:,} issuers")
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
        choices=("sync", "backfill-pdfs", "classify-sectors"),
        help=(
            "sync downloads new records and upserts the overlap from the prior sync date; "
            "backfill-pdfs processes every stored PDF missing extracted text; "
            "classify-sectors analyzes reports and propagates issuer sectors."
        ),
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--sector-file", type=Path, default=DEFAULT_SECTOR_FILE)
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
        default="current",
        help="Security lists to download; default: current.",
    )
    parser.add_argument(
        "--backfill-pdfs",
        action="store_true",
        help="Download PDFs for current-security records that do not yet have extracted text.",
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
                matched, total = classify_company_sectors(connection, args.sector_file)
            finally:
                connection.close()
            report(
                f"Sector classification: matched {matched:,} of {total:,} issuers",
                stream=sys.stdout,
            )
            return 0
        if args.command == "classify-sectors":
            connection = connect(args.database)
            try:
                matched, total = classify_company_sectors(connection, args.sector_file)
            finally:
                connection.close()
            report(
                f"Sector classification: matched {matched:,} of {total:,} issuers",
                stream=sys.stdout,
            )
            return 0
        results = sync(
            args.database,
            statuses,
            args.from_date,
            to_date,
            args.backfill_pdfs,
            args.sector_file,
        )
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
