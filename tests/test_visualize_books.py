from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.visualize_books import (
    APPLE_EPOCH,
    ReadingStat,
    aggregate_daily,
    build_pages_branch_name,
    build_report_payload,
    generate_report,
    GITHUB_REPO_URL,
    infer_profile_name_from_books_dir,
    load_library,
    period_labels,
    render_pages_index,
)


class BooksReportTest(unittest.TestCase):
    def make_library(self) -> tempfile.TemporaryDirectory[str]:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        (root / "shelves.json").write_text(
            json.dumps(
                [
                    {"name": "Series A", "bookIds": ["book-a"]},
                    {"name": "Series C", "bookIds": ["book-c"]},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        book_a = root / "Book A"
        book_a.mkdir()
        apple_seconds = (
            datetime(2026, 1, 2, tzinfo=timezone.utc) - APPLE_EPOCH
        ).total_seconds()
        (book_a / "metadata.json").write_text(
            json.dumps(
                {
                    "id": "book-a",
                    "title": "Book A",
                    "folder": "Book A",
                    "epub": "Book A.epub",
                    "cover": "Books/Book A/cover.jpg",
                    "lastAccess": apple_seconds,
                }
            ),
            encoding="utf-8",
        )
        (book_a / "bookinfo.json").write_text(
            json.dumps(
                {
                    "characterCount": 1000,
                    "chapterInfo": {
                        "a.xhtml": {"chapterCount": 500, "spineIndex": 1},
                        "b.xhtml": {"chapterCount": 500, "spineIndex": 2},
                    },
                }
            ),
            encoding="utf-8",
        )
        (book_a / "bookmark.json").write_text(
            json.dumps(
                {
                    "progress": 0.5,
                    "characterCount": 1000,
                    "lastModified": apple_seconds,
                    "chapterIndex": 1,
                }
            ),
            encoding="utf-8",
        )
        (book_a / "statistics.json").write_text(
            json.dumps(
                [
                    {
                        "dateKey": "2026-01-01",
                        "charactersRead": 100,
                        "readingTime": 3600,
                        "lastReadingSpeed": 100,
                        "minReadingSpeed": 80,
                        "maxReadingSpeed": 120,
                        "altMinReadingSpeed": 90,
                        "lastStatisticModified": 1767225600000,
                        "title": "Book A",
                    },
                    {
                        "dateKey": "2026-01-02",
                        "charactersRead": 300,
                        "readingTime": 3600,
                        "lastReadingSpeed": 300,
                        "minReadingSpeed": 200,
                        "maxReadingSpeed": 330,
                        "altMinReadingSpeed": 250,
                        "lastStatisticModified": 1767312000000,
                        "title": "Book A",
                    },
                    {
                        "dateKey": "2026-01-03",
                        "charactersRead": 0,
                        "readingTime": 120,
                        "lastReadingSpeed": 0,
                        "title": "Book A",
                    },
                    {
                        "dateKey": "2026-01-04",
                        "charactersRead": 5000,
                        "readingTime": 10,
                        "lastReadingSpeed": 1800000,
                        "title": "Book A",
                    },
                ]
            ),
            encoding="utf-8",
        )

        book_b = root / "Book B"
        book_b.mkdir()
        (book_b / "metadata.json").write_text(
            json.dumps({"id": "book-b", "title": "Book B", "folder": "Book B"}),
            encoding="utf-8",
        )
        (book_b / "bookinfo.json").write_text(
            json.dumps({"characterCount": 2000, "chapterInfo": {}}),
            encoding="utf-8",
        )

        book_c = root / "Book C"
        book_c.mkdir()
        (book_c / "metadata.json").write_text(
            json.dumps({"id": "book-c", "title": "Book C", "folder": "Book C"}),
            encoding="utf-8",
        )
        (book_c / "bookinfo.json").write_text(
            json.dumps({"characterCount": 500, "chapterInfo": []}),
            encoding="utf-8",
        )
        (book_c / "bookmark.json").write_text(
            json.dumps({"progress": 1.2, "characterCount": 500}),
            encoding="utf-8",
        )
        (book_c / "statistics.json").write_text(
            json.dumps(
                [
                    {
                        "dateKey": "2026-01-02",
                        "charactersRead": 50,
                        "readingTime": 0,
                        "title": "Book C",
                    }
                ]
            ),
            encoding="utf-8",
        )
        return temp_dir

    def test_period_labels(self) -> None:
        self.assertEqual(period_labels("2026-01-02"), ("2026-01-02", "2026-W01", "2026-01"))

    def test_load_library_handles_missing_files_and_speed(self) -> None:
        with self.make_library() as root:
            library = load_library(Path(root), ZoneInfo("UTC"))
            self.assertEqual(len(library.books), 3)
            books = {book.id: book for book in library.books}
            self.assertEqual(books["book-a"].recorded_characters, 400)
            self.assertEqual(books["book-a"].reading_time_seconds, 7200)
            self.assertEqual(books["book-a"].active_days, 2)
            self.assertEqual(books["book-a"].average_speed, 200)
            self.assertEqual(books["book-a"].progress_characters, 500)
            self.assertFalse(books["book-b"].has_statistics)
            self.assertEqual(books["book-c"].clamped_progress, 1)
            self.assertEqual(books["book-c"].stats, [])

    def test_payload_uses_weighted_speed(self) -> None:
        with self.make_library() as root:
            library = load_library(Path(root), ZoneInfo("UTC"))
            payload = build_report_payload(
                library,
                "UTC",
                generated_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            )
            self.assertEqual(payload["summary"]["bookCount"], 3)
            self.assertEqual(payload["summary"]["activeDays"], 2)
            self.assertEqual(payload["summary"]["totalBookCharacters"], 3500)
            self.assertEqual(payload["summary"]["totalRecordedCharacters"], 400)
            self.assertAlmostEqual(payload["summary"]["weightedAverageSpeed"], 200)
            daily = {row["date"]: row for row in payload["daily"]}
            self.assertEqual(daily["2026-01-01"]["speed"], 100)
            self.assertEqual(daily["2026-01-02"]["speed"], 300)

    def test_outliers_use_local_window_for_improving_speed(self) -> None:
        start = datetime(2026, 1, 1).date()
        stats = []
        for index in range(30):
            current = start + timedelta(days=index)
            speed = 5000 + index * 500
            if index == 10:
                speed = 60000
            stats.append(
                ReadingStat(
                    book_id="book-a",
                    title="Book A",
                    date_key=current.isoformat(),
                    week_key=period_labels(current.isoformat())[1],
                    month_key=period_labels(current.isoformat())[2],
                    characters_read=speed,
                    reading_time_seconds=3600,
                )
            )

        daily = aggregate_daily(stats)
        by_date = {row["date"]: row for row in daily}
        self.assertTrue(by_date["2026-01-11"]["speedOutlier"])
        self.assertFalse(by_date["2026-01-30"]["speedOutlier"])

    def test_generate_report_writes_html(self) -> None:
        with self.make_library() as root:
            output = Path(root) / "out" / "report.html"
            generated, payload = generate_report(
                Path(root),
                output,
                ZoneInfo("UTC"),
            )
            self.assertEqual(generated, output.resolve())
            self.assertEqual(payload["summary"]["booksWithStats"], 2)
            self.assertNotIn("booksDir", payload)
            self.assertNotIn("cover", payload["books"][0])
            html = output.read_text(encoding="utf-8")
            self.assertIn("report-data", html)
            self.assertIn("<title>阅读统计报告</title>", html)
            self.assertIn("Books Reading Report", html)
            self.assertIn("速度摘要", html)
            self.assertIn(GITHUB_REPO_URL, html)
            self.assertIn('aria-label="GitHub repository"', html)
            self.assertIn("document.title = pageTitles[state.lang]", html)
            self.assertIn("--surface-2: #f0ede6", html)
            self.assertIn("font-family: ui-sans-serif", html)
            self.assertIn('id="summaryCards"></section>', html)
            self.assertNotIn("booksDir", html)
            self.assertNotIn('"cover"', html)

    def test_pages_publish_helpers(self) -> None:
        self.assertEqual(
            infer_profile_name_from_books_dir(
                Path("/tmp/Library/Application Support/Books")
            ),
            "Books",
        )
        self.assertEqual(build_pages_branch_name("Books"), "reports/Books")
        self.assertEqual(build_pages_branch_name("public", "pages/"), "pages/public")
        index_html = render_pages_index("Books")
        self.assertIn("Japanese Reading Stats - Books", index_html)
        self.assertIn("books_reading_report.html", index_html)
        self.assertIn("--surface-2: #f0ede6", index_html)
        self.assertIn("font-family: ui-sans-serif", index_html)
        self.assertIn("border-radius: 8px", index_html)


if __name__ == "__main__":
    unittest.main()
