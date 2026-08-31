#!/usr/bin/env python3
"""Download HKEX listed-company news matching Title Search criteria."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://www1.hkexnews.hk"
SEARCH_URL = f"{BASE_URL}/search/titleSearchServlet.do"
STOCK_LIST_URLS = {
    "current": f"{BASE_URL}/ncms/script/eds/activestock_sehk_e.json",
    "delisted": f"{BASE_URL}/ncms/script/eds/inactivestock_sehk_e.json",
}
METADATA_URLS = {
    "headline_categories": f"{BASE_URL}/ncms/script/eds/tierone_e.json",
    "headline_groups": f"{BASE_URL}/ncms/script/eds/tiertwogrp_e.json",
    "headline_subcategories": f"{BASE_URL}/ncms/script/eds/tiertwo_e.json",
    "document_types": f"{BASE_URL}/ncms/script/eds/doc_e.json",
}
MAX_ROWS_PER_REQUEST = 10_000


class HKEXError(RuntimeError):
    """Raised when HKEX cannot satisfy a search request."""


def get_json(url: str, params: dict[str, str] | None = None) -> Any:
    """Fetch JSON with a small retry budget for transient HKEX failures."""
    if params:
        url = f"{url}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "hkex-news-downloader/1.0"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2**attempt)
    raise HKEXError(f"Request failed after 3 attempts: {url}") from last_error


def parse_date(value: str) -> date:
    for pattern in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(
        f"Invalid date {value!r}; use YYYY-MM-DD, YYYY/MM/DD, or YYYYMMDD."
    )


def format_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def absolute_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return f"{BASE_URL}{path}"


@dataclass(frozen=True)
class Criteria:
    from_date: date
    to_date: date
    security_status: str
    stock_id: str
    title: str
    search_mode: str
    document_type: str
    t1_code: str
    t2_group_code: str
    t2_code: str
    sort_by: str
    sort_order: str

    def parameters(self, from_date: date, to_date: date) -> dict[str, str]:
        return {
            "sortDir": "0" if self.sort_order == "desc" else "1",
            "sortByOptions": self.sort_by,
            "category": "0" if self.security_status == "current" else "1",
            "market": "SEHK",
            "stockId": self.stock_id,
            "documentType": self.document_type,
            "fromDate": format_date(from_date),
            "toDate": format_date(to_date),
            "title": self.title,
            "searchType": {"all": "0", "headline": "1", "document": "2"}[
                self.search_mode
            ],
            "t1code": self.t1_code,
            "t2Gcode": self.t2_group_code,
            "t2code": self.t2_code,
            "rowRange": str(MAX_ROWS_PER_REQUEST),
            "lang": "EN",
        }


class HKEXNewsClient:
    def fetch_page(self, criteria: Criteria, from_date: date, to_date: date) -> tuple[list[dict[str, Any]], int, bool]:
        payload = get_json(SEARCH_URL, criteria.parameters(from_date, to_date))
        if not isinstance(payload, dict) or "result" not in payload:
            raise HKEXError(f"Unexpected search response: {payload!r}")
        try:
            rows = json.loads(payload["result"])
            return rows, int(payload["recordCnt"]), bool(payload["hasNextRow"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise HKEXError(f"Invalid search response: {payload!r}") from error

    def search_all(self, criteria: Criteria) -> list[dict[str, Any]]:
        def collect(from_date: date, to_date: date) -> list[dict[str, Any]]:
            rows, count, has_more = self.fetch_page(criteria, from_date, to_date)
            if not has_more and count <= MAX_ROWS_PER_REQUEST:
                return rows
            if from_date == to_date:
                raise HKEXError(
                    f"HKEX returned more than {MAX_ROWS_PER_REQUEST:,} records for "
                    f"{from_date.isoformat()}, which cannot be paginated further."
                )
            midpoint = from_date + timedelta(days=(to_date - from_date).days // 2)
            return collect(from_date, midpoint) + collect(midpoint + timedelta(days=1), to_date)

        records = collect(criteria.from_date, criteria.to_date)
        unique_records = {str(row["NEWS_ID"]): row for row in records}
        return sorted(
            unique_records.values(),
            key=self._sort_key(criteria.sort_by),
            reverse=criteria.sort_order == "desc",
        )

    @staticmethod
    def _sort_key(sort_by: str) -> Any:
        if sort_by == "DateTime":
            return lambda row: datetime.strptime(row["DATE_TIME"], "%d/%m/%Y %H:%M")
        if sort_by == "StockCode":
            return lambda row: row["STOCK_CODE"]
        return lambda row: row["STOCK_NAME"].casefold()

    def resolve_stock(self, value: str, security_status: str) -> str:
        stocks = get_json(STOCK_LIST_URLS[security_status])
        if not isinstance(stocks, list):
            raise HKEXError("Unexpected stock-list response from HKEX.")
        code = value.zfill(5) if value.isdigit() else value.upper()
        exact = [stock for stock in stocks if stock["c"].upper() == code or stock["n"].casefold() == value.casefold()]
        if len(exact) == 1:
            return str(exact[0]["i"])
        if len(exact) > 1:
            options = ", ".join(f'{stock["c"]} {stock["n"]} (stock ID {stock["i"]})' for stock in exact)
            raise HKEXError(f"Stock {value!r} is ambiguous: {options}. Use --stock-id.")
        raise HKEXError(
            f"Stock {value!r} was not found in the {security_status} securities list. "
            "Use --stock-id for a historical or otherwise unavailable security."
        )


def export_records(records: list[dict[str, Any]], output_format: str, output: str | None) -> None:
    for record in records:
        record["DOCUMENT_URL"] = absolute_url(record.get("FILE_LINK", ""))
        record["DISPLAY_URL"] = absolute_url(record.get("DOD_WEB_PATH", ""))

    destination = sys.stdout if output is None else open(output, "w", encoding="utf-8", newline="")
    try:
        if output_format == "json":
            json.dump(records, destination, ensure_ascii=False, indent=2)
            destination.write("\n")
        elif output_format == "jsonl":
            for record in records:
                json.dump(record, destination, ensure_ascii=False)
                destination.write("\n")
        else:
            fields = sorted({field for record in records for field in record})
            writer = csv.DictWriter(destination, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
    finally:
        if output is not None:
            destination.close()


def print_metadata() -> None:
    metadata = {name: get_json(url) for name, url in METADATA_URLS.items()}
    json.dump(metadata, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download every HKEX Title Search record matching the supplied criteria. "
            "Use --list-categories to obtain headline and document-type codes."
        )
    )
    parser.add_argument("--from", dest="from_date", type=parse_date, help="Start date, inclusive.")
    parser.add_argument("--to", dest="to_date", type=parse_date, help="End date, inclusive.")
    parser.add_argument("--security-status", choices=("current", "delisted"), default="current")
    stock_group = parser.add_mutually_exclusive_group()
    stock_group.add_argument("--stock", help="Exact stock code or exact stock short name.")
    stock_group.add_argument("--stock-id", help="HKEX internal stock ID; bypasses stock lookup.")
    parser.add_argument("--title", default="", help="News-title keyword(s).")
    parser.add_argument("--search-mode", choices=("all", "headline", "document"), default="all")
    parser.add_argument("--document-type", default="-2", help="Document type code for --search-mode document.")
    parser.add_argument("--t1-code", default="-2", help="Headline category code for --search-mode headline.")
    parser.add_argument("--t2-group-code", default="-2", help="Headline group code for --search-mode headline.")
    parser.add_argument("--t2-code", default="-2", help="Headline subcategory code for --search-mode headline.")
    parser.add_argument("--sort-by", choices=("DateTime", "StockCode", "StockName"), default="DateTime")
    parser.add_argument("--sort-order", choices=("desc", "asc"), default="desc")
    parser.add_argument("--format", choices=("json", "jsonl", "csv"), default="json")
    parser.add_argument("--output", help="Write to this file instead of standard output.")
    parser.add_argument("--list-categories", action="store_true", help="Print supported category metadata and exit.")
    return parser


def validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.list_categories:
        return
    if args.from_date is None or args.to_date is None:
        parser.error("--from and --to are required unless --list-categories is used.")
    if args.from_date > args.to_date:
        parser.error("--from must not be later than --to.")
    if args.search_mode != "headline" and any(
        value != "-2" for value in (args.t1_code, args.t2_group_code, args.t2_code)
    ):
        parser.error("--t1-code, --t2-group-code, and --t2-code require --search-mode headline.")
    if args.search_mode != "document" and args.document_type != "-2":
        parser.error("--document-type requires --search-mode document.")


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()
    validate_arguments(parser, args)
    try:
        if args.list_categories:
            print_metadata()
            return 0
        client = HKEXNewsClient()
        stock_id = args.stock_id or (client.resolve_stock(args.stock, args.security_status) if args.stock else "")
        criteria = Criteria(
            from_date=args.from_date,
            to_date=args.to_date,
            security_status=args.security_status,
            stock_id=stock_id,
            title=args.title,
            search_mode=args.search_mode,
            document_type=args.document_type,
            t1_code=args.t1_code,
            t2_group_code=args.t2_group_code,
            t2_code=args.t2_code,
            sort_by=args.sort_by,
            sort_order=args.sort_order,
        )
        export_records(client.search_all(criteria), args.format, args.output)
        return 0
    except HKEXError as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
