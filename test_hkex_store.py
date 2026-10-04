import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from hkex_store import (
    HK_TIMEZONE,
    PDFContentError,
    backfill_document_text,
    classify_company_sectors,
    classify_document_sector,
    connect,
    download_pdf_text,
    has_main_stock_code,
    store_document_text,
)
from pypdf.errors import PdfReadError


class StockCodeFilterTest(unittest.TestCase):
    def test_keeps_rows_with_any_code_below_10000(self) -> None:
        self.assertTrue(has_main_stock_code({"STOCK_CODE": "00700"}))
        self.assertTrue(has_main_stock_code({"STOCK_CODE": "02800<br/>82800"}))
        self.assertTrue(has_main_stock_code({"STOCK_CODE": "00853 40168"}))

    def test_skips_rows_with_only_codes_of_10000_or_above(self) -> None:
        self.assertFalse(has_main_stock_code({"STOCK_CODE": "65614"}))
        self.assertFalse(has_main_stock_code({"STOCK_CODE": "10000<br/>69435"}))
        self.assertFalse(has_main_stock_code({"STOCK_CODE": ""}))
        self.assertFalse(has_main_stock_code({}))


class IgnoredPdfTest(unittest.TestCase):
    def test_oversized_pdf_is_a_terminal_content_error(self) -> None:
        with (
            patch("hkex_store.MAX_PDF_BYTES", 4),
            patch("hkex_store.urlopen") as urlopen,
        ):
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = b"%PDF-x"
            with self.assertRaisesRegex(PDFContentError, "exceeds"):
                download_pdf_text("https://example.test/document.pdf")

    def test_unreadable_pdf_is_a_terminal_content_error(self) -> None:
        with patch("hkex_store.urlopen") as urlopen, patch(
            "hkex_store.PdfReader", side_effect=PdfReadError("invalid PDF")
        ):
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = b"%PDF-1.7"
            with self.assertRaisesRegex(PDFContentError, "Unable to extract"):
                download_pdf_text("https://example.test/document.pdf")

    def test_pdf_text_with_surrogates_is_a_terminal_content_error(self) -> None:
        with patch("hkex_store.urlopen") as urlopen, patch(
            "hkex_store.PdfReader"
        ) as pdf_reader:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = b"%PDF-1.7"
            pdf_reader.return_value.pages = [
                type("Page", (), {"extract_text": lambda self: "\ud800"})()
            ]
            with self.assertRaisesRegex(PDFContentError, "Unable to extract"):
                download_pdf_text("https://example.test/document.pdf")

    def test_terminal_pdf_error_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "news.db")
            try:
                now = datetime.now(HK_TIMEZONE).isoformat()
                connection.execute(
                    """
                    INSERT INTO news (
                        news_id, release_time, stock_code, stock_name, category,
                        title, file_info, file_type, document_url, display_url,
                        security_status, raw_json, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "ignored-pdf",
                        now,
                        "00001",
                        "Example",
                        "",
                        "Example",
                        "",
                        "PDF",
                        "https://example.test/document.pdf",
                        "",
                        "current",
                        "{}",
                        now,
                    ),
                )
                with patch(
                    "hkex_store.download_pdf_text",
                    side_effect=PDFContentError("PDF exceeds the 50 MiB download limit."),
                ):
                    store_document_text(connection, ["ignored-pdf"])

                text, error, ignored_at = connection.execute(
                    """
                    SELECT document_text, document_error, document_ignored_at
                    FROM news WHERE news_id = 'ignored-pdf'
                    """
                ).fetchone()
                self.assertIsNone(text)
                self.assertEqual(error, "PDF exceeds the 50 MiB download limit.")
                self.assertIsNotNone(ignored_at)
                invalid_url = connection.execute(
                    """
                    SELECT reason FROM invalid_document_urls
                    WHERE document_url = 'https://example.test/document.pdf'
                    """
                ).fetchone()
                self.assertEqual(invalid_url[0], error)

                with patch("hkex_store.download_pdf_text") as download:
                    backfill_document_text(connection)
                download.assert_not_called()
            finally:
                connection.close()


class SectorClassificationTest(unittest.TestCase):
    def test_classifies_a_clear_principal_business_passage(self) -> None:
        result = classify_document_sector(
            "The Group is principally engaged in property development and "
            "property investment in Hong Kong. Other information follows."
        )
        self.assertEqual(result[0], "Real Estate")

    def test_ignores_keywords_outside_business_passages(self) -> None:
        self.assertIsNone(
            classify_document_sector(
                "The Group purchased software from a bank and insured its offices."
            )
        )

    def test_ignores_industries_in_later_sentences(self) -> None:
        result = classify_document_sector(
            "The Group is principally engaged in construction services in China. "
            "Its customers include water supply and power generation companies."
        )
        self.assertEqual(result[0], "Industrials")

    def test_propagates_a_match_to_names_sharing_a_stock_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "news.db"
            sector_file = Path(directory) / "sector.csv"
            sector_file.write_text(
                Path(__file__).with_name("sector.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            connection = connect(database)
            try:
                now = datetime.now(HK_TIMEZONE).isoformat()
                rows = [
                    (
                        "report",
                        "00001",
                        "OLD NAME",
                        "Annual Report",
                        "The Group is principally engaged in banking and wealth management.",
                    ),
                    ("other", "00001", "NEW NAME", "Other Announcement", None),
                ]
                for news_id, code, name, title, document_text in rows:
                    connection.execute(
                        """
                        INSERT INTO news (
                            news_id, release_time, stock_code, stock_name, category,
                            title, file_info, file_type, document_url, display_url,
                            security_status, raw_json, synced_at, document_text
                        ) VALUES (?, ?, ?, ?, '', ?, '', 'PDF', '', '', 'current',
                                  '{}', ?, ?)
                        """,
                        (news_id, now, code, name, title, now, document_text),
                    )
                matched, total = classify_company_sectors(connection, sector_file)
                self.assertEqual((matched, total), (1, 1))
                sectors = {
                    name: sector
                    for name, sector in connection.execute(
                        "SELECT stock_name, sector FROM news"
                    )
                }
                self.assertEqual(
                    sectors, {"OLD NAME": "Financials", "NEW NAME": "Financials"}
                )
            finally:
                connection.close()

    def test_reuses_existing_sector_before_analyzing_newer_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "news.db"
            sector_file = Path(__file__).with_name("sector.csv")
            connection = connect(database)
            try:
                now = datetime.now(HK_TIMEZONE).isoformat()
                rows = [
                    (
                        "old-report",
                        "00001",
                        "OLD NAME",
                        "Annual Report",
                        "The Group is principally engaged in banking services.",
                    ),
                    (
                        "new-report",
                        "00001",
                        "NEW NAME",
                        "Annual Report",
                        "The Group is principally engaged in hotel operations.",
                    ),
                ]
                for news_id, code, name, title, document_text in rows:
                    connection.execute(
                        """
                        INSERT INTO news (
                            news_id, release_time, stock_code, stock_name, category,
                            title, file_info, file_type, document_url, display_url,
                            security_status, raw_json, synced_at, document_text
                        ) VALUES (?, ?, ?, ?, '', ?, '', 'PDF', '', '', 'current',
                                  '{}', ?, ?)
                        """,
                        (news_id, now, code, name, title, now, document_text),
                    )
                connection.execute(
                    """
                    INSERT INTO company_sectors (
                        stock_name, sector, source_news_id, evidence, classified_at
                    ) VALUES ('OLD NAME', 'Financials', 'old-report', 'known', ?)
                    """,
                    (now,),
                )

                matched, total = classify_company_sectors(connection, sector_file)

                self.assertEqual((matched, total), (1, 1))
                assignments = connection.execute(
                    """
                    SELECT stock_name, sector, source_news_id
                    FROM company_sectors
                    ORDER BY stock_name
                    """
                ).fetchall()
                self.assertEqual(
                    assignments,
                    [
                        ("NEW NAME", "Financials", "old-report"),
                        ("OLD NAME", "Financials", "old-report"),
                    ],
                )
                self.assertEqual(
                    {
                        sector
                        for sector, in connection.execute(
                            "SELECT DISTINCT sector FROM news"
                        )
                    },
                    {"Financials"},
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
