from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

from scripts.visualize_books import (
    APPLE_EPOCH,
    DEFAULT_BOOKS_DIR,
    ReadingStat,
    aggregate_daily,
    build_pages_branch_name,
    build_report_payload,
    generate_report,
    GITHUB_REPO_URL,
    infer_profile_name_from_books_dir,
    load_library,
    period_labels,
    parse_args,
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

    def test_default_source_is_hoshi_reader_desktop(self) -> None:
        with patch("sys.argv", ["visualize_books.py"]):
            self.assertEqual(parse_args().books_dir, DEFAULT_BOOKS_DIR)
            self.assertEqual(parse_args().day_start_hour, 5)
        self.assertEqual(
            DEFAULT_BOOKS_DIR,
            Path("~/Library/Application Support/de.manhhao.hoshi/Books"),
        )

    def test_desktop_sessions_and_metadata(self) -> None:
        def millis(hour: int, minute: int = 0) -> int:
            return int(datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            book_dir = root / "Desktop Book"
            book_dir.mkdir()
            (root / "shelves.json").write_text(json.dumps({
                "Desktop Shelf": {"modified": 1, "value": 0},
                "Deleted Shelf": {"modified": 2, "value": None},
            }), encoding="utf-8")
            (book_dir / "metadata.json").write_text(json.dumps({
                "id": "desktop-book", "title": "Desktop Book", "characterCount": 7000,
                "lastAccess": -63114076800.0,
                "shelves": {
                    "Desktop Shelf": {"modified": 1, "value": True},
                    "Removed Membership": {"modified": 2, "value": False},
                },
            }), encoding="utf-8")
            (book_dir / "bookmark.json").write_text(json.dumps({
                "progress": 0.5, "characterCount": 100,
            }), encoding="utf-8")
            sessions = {
                "evening": {"modified": millis(16, 31), "value": {
                    "startedAt": millis(15, 30), "endedAt": millis(16, 30),
                    "charactersRead": 300, "readingTime": 3600,
                }},
                "morning": {"modified": millis(1, 10), "value": {
                    "startedAt": millis(1), "endedAt": millis(1, 10),
                    "charactersRead": 100, "readingTime": 600,
                }},
                "deleted": {"modified": millis(17), "value": None},
                "short": {"modified": millis(2), "value": {
                    "startedAt": millis(2), "endedAt": millis(2) + 10000,
                    "charactersRead": 5000, "readingTime": 10,
                }},
                "zero-characters": {"modified": millis(3), "value": {
                    "startedAt": millis(3), "endedAt": millis(3, 10),
                    "charactersRead": 0, "readingTime": 600,
                }},
                "invalid-interval": {"modified": millis(4), "value": {
                    "startedAt": millis(4), "endedAt": millis(3),
                    "charactersRead": 100, "readingTime": 600,
                }},
                "missing-start": {"modified": millis(4), "value": {
                    "endedAt": millis(4), "charactersRead": 100, "readingTime": 600,
                }},
                "invalid-timestamp": {"modified": millis(4), "value": {
                    "startedAt": 1e30, "endedAt": 1e30,
                    "charactersRead": 100, "readingTime": 600,
                }},
            }
            (book_dir / "statistics.json").write_text(json.dumps(sessions), encoding="utf-8")
            library = load_library(root, ZoneInfo("Asia/Shanghai"))
            book = library.books[0]
            self.assertEqual(book.total_characters, 7000)
            self.assertIsNone(book.last_access)
            self.assertEqual(book.progress_characters, 3500)
            self.assertEqual(book.shelves, ["Desktop Shelf"])
            self.assertEqual(library.shelves, ["Desktop Shelf"])
            self.assertEqual(book.reading_time_seconds, 4200)
            self.assertEqual(book.recorded_characters, 400)
            self.assertEqual(book.active_days, 1)
            self.assertEqual([s.session_id for s in book.stats], ["morning", "evening"])
            evening = book.stats[-1]
            self.assertEqual(evening.date_key, "2026-01-01")
            self.assertEqual(evening.ended_at.date().isoformat(), "2026-01-02")
            self.assertEqual(evening.last_modified.minute, 31)
            payload = build_report_payload(library, "Asia/Shanghai")
            self.assertEqual(payload["summary"]["totalRecordedCharacters"], 400)
            self.assertEqual(payload["stats"][-1]["sessionId"], "evening")
            self.assertIn("+08:00", payload["stats"][-1]["startedAt"])
            west = load_library(root, ZoneInfo("America/Los_Angeles"))
            self.assertEqual(west.books[0].stats[0].date_key, "2025-12-31")

    def test_session_day_boundary_at_five(self) -> None:
        cases = [
            ("before", "2026-03-01T04:59:59.999+08:00", "2026-02-28"),
            ("at", "2026-03-01T05:00:00+08:00", "2026-03-01"),
            ("week-before", "2026-03-02T04:30:00+08:00", "2026-03-01"),
            ("week-at", "2026-03-02T05:00:00+08:00", "2026-03-02"),
            ("year-before", "2026-01-01T00:01:00+08:00", "2025-12-31"),
        ]
        with self.make_library() as temp:
            root = Path(temp)
            sessions = {}
            for session_id, timestamp, _ in cases:
                millis = int(datetime.fromisoformat(timestamp).timestamp() * 1000)
                sessions[session_id] = {"modified": millis + 900000, "value": {
                    "startedAt": millis, "endedAt": millis + 900000,
                    "charactersRead": 100, "readingTime": 900,
                }}
            (root / "Book A" / "statistics.json").write_text(
                json.dumps(sessions), encoding="utf-8",
            )
            library = load_library(root, ZoneInfo("Asia/Shanghai"))
            by_id = {s.session_id: s for s in library.stats}
            for session_id, _, expected in cases:
                stat = by_id[session_id]
                self.assertEqual((stat.date_key, stat.week_key, stat.month_key), period_labels(expected))
            self.assertEqual(by_id["before"].ended_at.date().isoformat(), "2026-03-01")
            midnight = load_library(root, ZoneInfo("Asia/Shanghai"), day_start_hour=0)
            midnight_by_id = {s.session_id: s for s in midnight.stats}
            self.assertEqual(midnight_by_id["before"].date_key, "2026-03-01")
            self.assertEqual(midnight_by_id["year-before"].date_key, "2026-01-01")
            self.assertEqual(sum(s.characters_read for s in library.stats), 500)
            self.assertEqual(sum(s.reading_time_seconds for s in library.stats), 4500)
            self.assertEqual(
                sum(s.reading_time_seconds for s in library.stats),
                sum(s.reading_time_seconds for s in midnight.stats),
            )
            self.assertEqual(build_report_payload(library, "Asia/Shanghai")["dayStartHour"], 5)
            self.assertEqual(build_report_payload(midnight, "Asia/Shanghai")["dayStartHour"], 0)
            with self.assertRaises(ValueError):
                load_library(root, ZoneInfo("Asia/Shanghai"), day_start_hour=24)

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
