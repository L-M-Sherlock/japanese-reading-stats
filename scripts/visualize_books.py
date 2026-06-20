#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import math
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
LOCAL_OUTLIER_WINDOW_SIZE = 15
LOCAL_OUTLIER_MIN_POINTS = 8
MIN_READING_TIME_SECONDS = 60


@dataclass(frozen=True)
class ReadingStat:
    book_id: str
    title: str
    date_key: str
    week_key: str
    month_key: str
    characters_read: float
    reading_time_seconds: float
    last_reading_speed: float | None = None
    min_reading_speed: float | None = None
    max_reading_speed: float | None = None
    alt_min_reading_speed: float | None = None
    last_modified: datetime | None = None

    @property
    def reading_time_hours(self) -> float:
        return self.reading_time_seconds / 3600

    @property
    def speed(self) -> float | None:
        if self.characters_read <= 0 or self.reading_time_seconds <= 0:
            return None
        return self.characters_read / self.reading_time_hours


@dataclass
class BookRecord:
    id: str
    title: str
    folder_name: str
    folder_path: Path
    epub_name: str = ""
    total_characters: int = 0
    progress: float | None = None
    chapter_index: int | None = None
    last_access: datetime | None = None
    bookmark_modified: datetime | None = None
    shelves: list[str] = field(default_factory=list)
    chapter_count: int = 0
    stats: list[ReadingStat] = field(default_factory=list)
    has_statistics: bool = False
    has_bookmark: bool = False

    @property
    def recorded_characters(self) -> float:
        return sum(stat.characters_read for stat in self.stats)

    @property
    def reading_time_seconds(self) -> float:
        return sum(stat.reading_time_seconds for stat in self.stats)

    @property
    def active_days(self) -> int:
        return len({stat.date_key for stat in self.stats if stat.date_key})

    @property
    def average_speed(self) -> float | None:
        characters = sum(
            stat.characters_read for stat in self.stats if stat.speed is not None
        )
        seconds = sum(
            stat.reading_time_seconds for stat in self.stats if stat.speed is not None
        )
        return weighted_speed(characters, seconds)

    @property
    def clamped_progress(self) -> float | None:
        if self.progress is None:
            return None
        return clamp(float(self.progress), 0, 1)

    @property
    def progress_characters(self) -> float | None:
        if self.clamped_progress is None:
            return None
        return self.clamped_progress * self.total_characters

    @property
    def latest_stat_date(self) -> str:
        dates = [stat.date_key for stat in self.stats if stat.date_key]
        return max(dates) if dates else ""

@dataclass(frozen=True)
class LibraryData:
    books_dir: Path
    books: list[BookRecord]
    shelves: list[str]

    @property
    def stats(self) -> list[ReadingStat]:
        return [stat for book in self.books for stat in book.stats]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a local HTML report from Books reading data."
    )
    parser.add_argument(
        "--books-dir",
        type=Path,
        default=Path("~/Library/Application Support/Books"),
        help="Books data directory. Default: %(default)s",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/books_reading_report.html"),
        help="Output HTML path. Default: %(default)s",
    )
    parser.add_argument(
        "--timezone",
        default="",
        help="Timezone for generated timestamps, for example Asia/Shanghai.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of items to show in top lists. Default: %(default)s",
    )
    parser.add_argument(
        "--min-reading-seconds",
        type=float,
        default=MIN_READING_TIME_SECONDS,
        help=(
            "Exclude statistics records shorter than this many seconds. "
            "Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--publish-pages",
        action="store_true",
        help=(
            "After writing the local report, commit a minimal static site to a "
            "profile-specific Git branch and push it for GitHub Pages."
        ),
    )
    parser.add_argument(
        "--pages-remote",
        default="origin",
        help="Git remote used by --publish-pages. Default: %(default)s",
    )
    parser.add_argument(
        "--pages-branch-prefix",
        default="reports/",
        help=(
            "Prefix for the published branch. The profile name is appended to "
            "this value. Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--profile-name",
        default="",
        help=(
            "Profile name used for the published branch. If omitted, it is "
            "inferred from the Books data directory name."
        ),
    )
    return parser.parse_args()


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid JSON file: {path}\n{error}") from error


def as_float(value: Any, default: float = 0) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def as_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def as_int(value: Any, default: int = 0) -> int:
    return int(as_float(value, default))


def resolve_timezone(timezone_name: str):
    timezone_name = timezone_name.strip()
    if not timezone_name:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise SystemExit(f"Unknown timezone: {timezone_name}") from error


def timezone_display_name(time_zone) -> str:
    return str(getattr(time_zone, "key", None) or time_zone)


def run_git(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        command = " ".join(["git", *args])
        raise SystemExit(f"Git command failed: {command}\n{detail}")
    return result


def git_output(args: list[str], *, cwd: Path) -> str:
    return run_git(args, cwd=cwd).stdout.strip()


def infer_profile_name_from_books_dir(books_dir: Path) -> str:
    profile_name = books_dir.expanduser().resolve().name.strip()
    if not profile_name:
        raise SystemExit(f"Could not infer profile name from Books path: {books_dir}")
    return profile_name


def build_pages_branch_name(profile_name: str, branch_prefix: str = "reports/") -> str:
    branch = f"{branch_prefix}{profile_name.strip()}"
    if not branch:
        raise SystemExit("Published branch name is empty.")
    return branch


def validate_pages_branch_name(branch: str, *, repo_dir: Path | None = None) -> str:
    repo_dir = repo_dir or Path.cwd()
    result = run_git(["check-ref-format", "--branch", branch], cwd=repo_dir, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SystemExit(f"Invalid published branch name {branch!r}: {detail}")
    return branch


def render_pages_index(profile_name: str) -> str:
    title = html.escape(f"Japanese Reading Stats - {profile_name}")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #fbf7ef;
      color: #1c1f22;
    }}
    main {{
      width: min(720px, calc(100vw - 40px));
      border: 1px solid #d9d0c4;
      border-radius: 8px;
      background: #fffdf8;
      padding: 28px;
      box-shadow: 0 16px 40px rgba(46, 38, 28, 0.08);
    }}
    h1 {{ margin: 0 0 10px; font-size: 2rem; }}
    p {{ color: #68717a; line-height: 1.5; }}
    nav {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 22px; }}
    a {{
      color: #14665d;
      text-decoration: none;
      border: 1px solid #d9d0c4;
      border-radius: 999px;
      padding: 10px 16px;
      background: #f2eadf;
      font-weight: 700;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <p>Open the generated reading statistics report.</p>
    <nav>
      <a href="books_reading_report.html">Reading report</a>
    </nav>
  </main>
</body>
</html>
"""


def clear_publish_worktree(worktree: Path) -> None:
    for child in worktree.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def publish_report_to_pages(
    *,
    report_path: Path,
    books_dir: Path,
    remote: str = "origin",
    branch_prefix: str = "reports/",
    profile_name: str = "",
    repo_dir: Path | None = None,
) -> str:
    if not report_path.exists():
        raise SystemExit(f"Report does not exist: {report_path}")

    repo_dir = (repo_dir or Path.cwd()).resolve()
    profile = profile_name.strip() or infer_profile_name_from_books_dir(books_dir)
    branch = validate_pages_branch_name(
        build_pages_branch_name(profile, branch_prefix), repo_dir=repo_dir
    )
    remote_url = git_output(["remote", "get-url", remote], cwd=repo_dir)

    with tempfile.TemporaryDirectory(prefix="japanese-reading-pages-") as temp_root:
        publish_dir = Path(temp_root) / "site"
        publish_dir.mkdir()
        run_git(["init"], cwd=publish_dir)
        run_git(["config", "user.name", "Japanese Reading Stats"], cwd=publish_dir)
        run_git(
            ["config", "user.email", "japanese-reading-stats@example.invalid"],
            cwd=publish_dir,
        )
        run_git(["remote", "add", remote, remote_url], cwd=publish_dir)

        fetch_result = run_git(
            [
                "fetch",
                "--depth=1",
                remote,
                f"refs/heads/{branch}:refs/remotes/{remote}/{branch}",
            ],
            cwd=publish_dir,
            check=False,
        )
        if fetch_result.returncode == 0:
            run_git(
                ["checkout", "-B", branch, f"refs/remotes/{remote}/{branch}"],
                cwd=publish_dir,
            )
        else:
            run_git(["checkout", "--orphan", branch], cwd=publish_dir)

        clear_publish_worktree(publish_dir)
        (publish_dir / "index.html").write_text(
            render_pages_index(profile), encoding="utf-8"
        )
        shutil.copy2(report_path, publish_dir / "books_reading_report.html")

        if git_output(["status", "--porcelain"], cwd=publish_dir):
            run_git(["add", "--all"], cwd=publish_dir)
            run_git(
                ["commit", "-m", f"Update reading report for {profile}"],
                cwd=publish_dir,
            )
        run_git(["push", remote, f"HEAD:refs/heads/{branch}"], cwd=publish_dir)

    return branch


def apple_seconds_to_datetime(value: Any, time_zone) -> datetime | None:
    seconds = as_optional_float(value)
    if seconds is None:
        return None
    return (APPLE_EPOCH + timedelta(seconds=seconds)).astimezone(time_zone)


def unix_ms_to_datetime(value: Any, time_zone) -> datetime | None:
    millis = as_optional_float(value)
    if millis is None:
        return None
    return datetime.fromtimestamp(millis / 1000, time_zone)


def format_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")


def period_labels(date_key: str) -> tuple[str, str, str]:
    try:
        value = date.fromisoformat(date_key)
    except ValueError:
        return date_key, date_key, date_key[:7]
    iso_year, iso_week, _ = value.isocalendar()
    return value.isoformat(), f"{iso_year}-W{iso_week:02d}", f"{value.year}-{value.month:02d}"


def build_shelf_map(books_dir: Path) -> tuple[dict[str, list[str]], list[str]]:
    shelves = read_json(books_dir / "shelves.json", [])
    shelf_map: dict[str, list[str]] = defaultdict(list)
    shelf_names: list[str] = []
    if not isinstance(shelves, list):
        return {}, []
    for shelf in shelves:
        if not isinstance(shelf, dict):
            continue
        name = str(shelf.get("name") or "").strip()
        if not name:
            continue
        shelf_names.append(name)
        for book_id in shelf.get("bookIds") or []:
            shelf_map[str(book_id)].append(name)
    return dict(shelf_map), sorted(set(shelf_names))


def count_chapters(chapter_info: Any) -> int:
    if isinstance(chapter_info, dict):
        return len(chapter_info)
    if isinstance(chapter_info, list):
        return len(chapter_info)
    return 0


def load_book(
    book_dir: Path,
    shelf_map: dict[str, list[str]],
    time_zone,
    min_reading_seconds: float = MIN_READING_TIME_SECONDS,
) -> BookRecord | None:
    metadata_path = book_dir / "metadata.json"
    if not metadata_path.exists():
        return None

    metadata = read_json(metadata_path, {})
    if not isinstance(metadata, dict):
        raise SystemExit(f"metadata.json must contain an object: {metadata_path}")

    book_id = str(metadata.get("id") or book_dir.name)
    title = str(metadata.get("title") or book_dir.name)
    bookinfo = read_json(book_dir / "bookinfo.json", {})
    if not isinstance(bookinfo, dict):
        bookinfo = {}

    bookmark_path = book_dir / "bookmark.json"
    bookmark = read_json(bookmark_path, {})
    if not isinstance(bookmark, dict):
        bookmark = {}

    total_characters = as_int(
        bookinfo.get("characterCount", bookmark.get("characterCount", 0))
    )
    progress = as_optional_float(bookmark.get("progress"))
    chapter_index = (
        as_int(bookmark.get("chapterIndex"))
        if bookmark.get("chapterIndex") is not None
        else None
    )

    book = BookRecord(
        id=book_id,
        title=title,
        folder_name=str(metadata.get("folder") or book_dir.name),
        folder_path=book_dir,
        epub_name=str(metadata.get("epub") or ""),
        total_characters=total_characters,
        progress=progress,
        chapter_index=chapter_index,
        last_access=apple_seconds_to_datetime(metadata.get("lastAccess"), time_zone),
        bookmark_modified=apple_seconds_to_datetime(
            bookmark.get("lastModified"), time_zone
        ),
        shelves=sorted(shelf_map.get(book_id, [])),
        chapter_count=count_chapters(bookinfo.get("chapterInfo")),
        has_statistics=(book_dir / "statistics.json").exists(),
        has_bookmark=bookmark_path.exists(),
    )
    book.stats.extend(load_stats(book_dir, book, time_zone, min_reading_seconds))
    return book


def load_stats(
    book_dir: Path,
    book: BookRecord,
    time_zone,
    min_reading_seconds: float = MIN_READING_TIME_SECONDS,
) -> list[ReadingStat]:
    raw_stats = read_json(book_dir / "statistics.json", [])
    if not isinstance(raw_stats, list):
        return []

    stats: list[ReadingStat] = []
    for item in raw_stats:
        if not isinstance(item, dict):
            continue
        date_key = str(item.get("dateKey") or "").strip()
        if not date_key:
            continue
        characters_read = as_float(item.get("charactersRead"))
        if characters_read <= 0:
            continue
        reading_time_seconds = as_float(item.get("readingTime"))
        if reading_time_seconds < min_reading_seconds:
            continue
        day_key, week_key, month_key = period_labels(date_key)
        stats.append(
            ReadingStat(
                book_id=book.id,
                title=book.title,
                date_key=day_key,
                week_key=week_key,
                month_key=month_key,
                characters_read=characters_read,
                reading_time_seconds=reading_time_seconds,
                last_reading_speed=as_optional_float(item.get("lastReadingSpeed")),
                min_reading_speed=as_optional_float(item.get("minReadingSpeed")),
                max_reading_speed=as_optional_float(item.get("maxReadingSpeed")),
                alt_min_reading_speed=as_optional_float(item.get("altMinReadingSpeed")),
                last_modified=unix_ms_to_datetime(
                    item.get("lastStatisticModified"), time_zone
                ),
            )
        )
    return sorted(stats, key=lambda stat: (stat.date_key, stat.title))


def load_library(
    books_dir: Path,
    time_zone,
    min_reading_seconds: float = MIN_READING_TIME_SECONDS,
) -> LibraryData:
    books_dir = books_dir.expanduser().resolve()
    if not books_dir.exists():
        raise SystemExit(f"Books directory not found: {books_dir}")
    shelf_map, shelf_names = build_shelf_map(books_dir)
    books: list[BookRecord] = []
    for child in sorted(books_dir.iterdir(), key=lambda path: path.name.casefold()):
        if not child.is_dir():
            continue
        book = load_book(child, shelf_map, time_zone, min_reading_seconds)
        if book is not None:
            books.append(book)
    return LibraryData(books_dir=books_dir, books=books, shelves=shelf_names)


def weighted_speed(characters: float, seconds: float) -> float | None:
    if characters <= 0 or seconds <= 0:
        return None
    return characters / (seconds / 3600)


def aggregate_daily(stats: list[ReadingStat]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    book_slices: dict[str, Counter[str]] = defaultdict(Counter)
    speed_samples: dict[str, list[float]] = defaultdict(list)

    for stat in stats:
        row = buckets.setdefault(
            stat.date_key,
            {
                "date": stat.date_key,
                "week": stat.week_key,
                "month": stat.month_key,
                "charactersRead": 0.0,
                "readingTimeSeconds": 0.0,
                "speedCharacters": 0.0,
                "speedTimeSeconds": 0.0,
                "recordCount": 0,
            },
        )
        row["charactersRead"] += stat.characters_read
        row["readingTimeSeconds"] += stat.reading_time_seconds
        row["recordCount"] += 1
        book_slices[stat.date_key][stat.title] += int(round(stat.characters_read))
        if stat.speed is not None:
            row["speedCharacters"] += stat.characters_read
            row["speedTimeSeconds"] += stat.reading_time_seconds
            speed_samples[stat.date_key].append(stat.speed)

    rows = []
    for date_key, row in sorted(buckets.items()):
        row = dict(row)
        row["readingTimeHours"] = row["readingTimeSeconds"] / 3600
        row["speedTimeHours"] = row["speedTimeSeconds"] / 3600
        row["speed"] = weighted_speed(
            row["speedCharacters"], row["speedTimeSeconds"]
        )
        row["bookBreakdown"] = book_slices[date_key].most_common(8)
        row["sampleMinSpeed"] = min(speed_samples[date_key]) if speed_samples[date_key] else None
        row["sampleMaxSpeed"] = max(speed_samples[date_key]) if speed_samples[date_key] else None
        rows.append(row)

    mark_outliers(rows, "speed")
    return rows


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * pct
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[int(position)]
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def outlier_bounds(values: list[float]) -> tuple[float, float] | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if len(clean) < 4:
        return None
    q1 = percentile(clean, 0.25)
    q3 = percentile(clean, 0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return None
    return q1 - 1.5 * iqr, q3 + 1.5 * iqr


def mark_outliers(
    rows: list[dict[str, Any]],
    key: str,
    window_size: int = LOCAL_OUTLIER_WINDOW_SIZE,
    min_points: int = LOCAL_OUTLIER_MIN_POINTS,
) -> None:
    for row in rows:
        row["speedOutlier"] = False
        row["outlierLow"] = None
        row["outlierHigh"] = None

    valid_indexes = [
        index
        for index, row in enumerate(rows)
        if row.get(key) is not None and math.isfinite(float(row[key]))
    ]
    half_window = max(1, window_size // 2)

    for valid_position, row_index in enumerate(valid_indexes):
        start = max(0, valid_position - half_window)
        end = min(len(valid_indexes), valid_position + half_window + 1)
        window_indexes = valid_indexes[start:end]
        if len(window_indexes) < min_points:
            continue

        values = [float(rows[index][key]) for index in window_indexes]
        bounds = outlier_bounds(values)
        if bounds is None:
            continue

        low, high = bounds
        value = float(rows[row_index][key])
        rows[row_index]["outlierLow"] = low
        rows[row_index]["outlierHigh"] = high
        rows[row_index]["speedOutlier"] = value < low or value > high


def speed_for_rows(rows: list[dict[str, Any]]) -> float | None:
    characters = sum(
        float(row.get("speedCharacters", row.get("charactersRead")) or 0)
        for row in rows
    )
    seconds = sum(
        float(row.get("speedTimeSeconds", row.get("readingTimeSeconds")) or 0)
        for row in rows
    )
    return weighted_speed(characters, seconds)


def build_summary(books: list[BookRecord], daily_rows: list[dict[str, Any]]) -> dict[str, Any]:
    stats_books = [book for book in books if book.has_statistics]
    bookmark_books = [book for book in books if book.has_bookmark]
    total_recorded = sum(book.recorded_characters for book in books)
    total_seconds = sum(book.reading_time_seconds for book in books)
    speed_characters = sum(
        stat.characters_read for book in books for stat in book.stats if stat.speed is not None
    )
    speed_seconds = sum(
        stat.reading_time_seconds
        for book in books
        for stat in book.stats
        if stat.speed is not None
    )
    progress_values = [
        book.progress_characters for book in books if book.progress_characters is not None
    ]
    valid_speed_rows = [row for row in daily_rows if row.get("speed") is not None]
    active_days = len(daily_rows)
    date_start = daily_rows[0]["date"] if daily_rows else ""
    date_end = daily_rows[-1]["date"] if daily_rows else ""
    recent_7 = valid_speed_rows[-7:]
    early_14 = valid_speed_rows[:14]
    recent_14 = valid_speed_rows[-14:]
    early_speed = speed_for_rows(early_14)
    recent_speed = speed_for_rows(recent_14)
    speed_change = None
    if early_speed and recent_speed:
        speed_change = ((recent_speed - early_speed) / early_speed) * 100
    best_day = max(valid_speed_rows, key=lambda row: row["speed"], default=None)
    worst_day = min(valid_speed_rows, key=lambda row: row["speed"], default=None)

    return {
        "bookCount": len(books),
        "booksWithStats": len(stats_books),
        "booksWithBookmark": len(bookmark_books),
        "totalBookCharacters": sum(book.total_characters for book in books),
        "totalRecordedCharacters": total_recorded,
        "totalProgressCharacters": sum(progress_values),
        "totalReadingHours": total_seconds / 3600,
        "activeDays": active_days,
        "dateStart": date_start,
        "dateEnd": date_end,
        "activeDayAverageCharacters": total_recorded / active_days if active_days else 0,
        "weightedAverageSpeed": weighted_speed(speed_characters, speed_seconds),
        "medianDaySpeed": median([row["speed"] for row in valid_speed_rows])
        if valid_speed_rows
        else None,
        "recent7Speed": speed_for_rows(recent_7),
        "early14Speed": early_speed,
        "recent14Speed": recent_speed,
        "speedChangePercent": speed_change,
        "bestSpeedDay": best_day,
        "worstSpeedDay": worst_day,
    }


def round_or_none(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def book_to_payload(book: BookRecord) -> dict[str, Any]:
    return {
        "id": book.id,
        "title": book.title,
        "folder": book.folder_name,
        "epub": book.epub_name,
        "shelves": book.shelves,
        "totalCharacters": book.total_characters,
        "recordedCharacters": round(book.recorded_characters, 2),
        "progress": book.clamped_progress,
        "progressCharacters": round_or_none(book.progress_characters),
        "readingTimeHours": round(book.reading_time_seconds / 3600, 4),
        "activeDays": book.active_days,
        "averageSpeed": round_or_none(book.average_speed),
        "latestStatDate": book.latest_stat_date,
        "lastAccess": format_dt(book.last_access),
        "bookmarkModified": format_dt(book.bookmark_modified),
        "chapterIndex": book.chapter_index,
        "chapterCount": book.chapter_count,
        "hasStatistics": book.has_statistics,
        "hasBookmark": book.has_bookmark,
    }


def stat_to_payload(stat: ReadingStat) -> dict[str, Any]:
    return {
        "bookId": stat.book_id,
        "title": stat.title,
        "date": stat.date_key,
        "week": stat.week_key,
        "month": stat.month_key,
        "charactersRead": round(stat.characters_read, 2),
        "readingTimeHours": round(stat.reading_time_hours, 4),
        "readingTimeSeconds": round(stat.reading_time_seconds, 4),
        "speedCharacters": round(stat.characters_read, 2) if stat.speed is not None else 0,
        "speedTimeHours": round(stat.reading_time_hours, 4) if stat.speed is not None else 0,
        "speedTimeSeconds": round(stat.reading_time_seconds, 4) if stat.speed is not None else 0,
        "speed": round_or_none(stat.speed),
        "lastReadingSpeed": round_or_none(stat.last_reading_speed),
        "minReadingSpeed": round_or_none(stat.min_reading_speed),
        "maxReadingSpeed": round_or_none(stat.max_reading_speed),
        "altMinReadingSpeed": round_or_none(stat.alt_min_reading_speed),
        "lastModified": format_dt(stat.last_modified),
    }


def build_report_payload(
    library: LibraryData,
    time_zone_label: str,
    generated_at: datetime | None = None,
    top_n: int = 20,
) -> dict[str, Any]:
    daily_rows = aggregate_daily(library.stats)
    summary = build_summary(library.books, daily_rows)
    generated_at = generated_at or datetime.now().astimezone()
    return {
        "generatedAt": generated_at.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "timezone": time_zone_label,
        "topN": top_n,
        "shelves": library.shelves,
        "summary": summary,
        "books": [
            book_to_payload(book)
            for book in sorted(library.books, key=lambda item: item.title.casefold())
        ],
        "stats": [
            stat_to_payload(stat)
            for stat in sorted(library.stats, key=lambda item: (item.date_key, item.title))
        ],
        "daily": daily_rows,
    }


def safe_json_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")


def render_html(payload: dict[str, Any]) -> str:
    data_json = safe_json_script(payload)
    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-lang="zh">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Books Reading Report</title>
  <style>
    :root {{
      --bg: #f6f4ef;
      --surface: #fffdf8;
      --surface-2: #f0ede6;
      --ink: #202326;
      --muted: #626a70;
      --line: #d8d1c4;
      --accent: #176b62;
      --accent-2: #b45d36;
      --accent-3: #3867a8;
      --good: #247a4d;
      --warn: #b45d36;
      --bad: #a33f3f;
      --bar: #c8dcd8;
      --bar-dark: #176b62;
      --shadow: 0 10px 28px rgba(31, 35, 38, 0.07);
    }}
    html[data-lang="zh"] .lang-en,
    html[data-lang="en"] .lang-zh {{
      display: none;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", sans-serif;
    }}
    button,
    input,
    select {{
      font: inherit;
    }}
    .wrap {{
      width: min(1380px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 28px 0 48px;
    }}
    .hero,
    .panel,
    .summary-card,
    .book-card {{
      background: var(--surface);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
    }}
    .hero {{
      border-radius: 8px;
      padding: 22px;
    }}
    .hero-top {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      flex-wrap: wrap;
    }}
    h1,
    h2,
    h3 {{
      margin: 0;
      line-height: 1.15;
    }}
    h1 {{
      font-size: clamp(2rem, 3vw, 3.1rem);
      letter-spacing: 0;
    }}
    h2 {{
      font-size: 1.18rem;
      margin-bottom: 12px;
    }}
    h3 {{
      font-size: 1rem;
    }}
    .muted {{
      color: var(--muted);
    }}
    .actions {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .segmented {{
      display: inline-flex;
      flex-wrap: wrap;
      gap: 4px;
      padding: 4px;
      border: 1px solid var(--line);
      background: var(--surface-2);
      border-radius: 8px;
    }}
    .segmented-button,
    .lang-button {{
      border: 0;
      background: transparent;
      color: var(--muted);
      min-height: 32px;
      padding: 5px 10px;
      border-radius: 6px;
      cursor: pointer;
    }}
    .segmented-button.active,
    .lang-button.active {{
      background: var(--surface);
      color: var(--ink);
      box-shadow: 0 2px 8px rgba(31, 35, 38, 0.08);
    }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 16px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 5px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: var(--surface-2);
      font-size: 0.88rem;
      max-width: 100%;
      overflow-wrap: anywhere;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(176px, 1fr));
      gap: 12px;
      margin-top: 16px;
    }}
    .summary-card {{
      border-radius: 8px;
      padding: 16px;
      min-height: 100px;
    }}
    .summary-value {{
      font-size: 1.45rem;
      line-height: 1.1;
      font-weight: 750;
      overflow-wrap: anywhere;
    }}
    .summary-label {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .summary-note {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.78rem;
      line-height: 1.35;
    }}
    .panel {{
      border-radius: 8px;
      padding: 18px;
      margin-top: 16px;
    }}
    .controls {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
      align-items: end;
    }}
    .control-label {{
      display: block;
      color: var(--muted);
      font-size: 0.82rem;
      margin-bottom: 5px;
      font-weight: 650;
    }}
    input[type="search"],
    select {{
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fffefa;
      color: var(--ink);
      padding: 8px 10px;
    }}
    .chart-shell {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fffefa;
      padding-bottom: 22px;
    }}
    .chart-shell svg {{
      display: block;
      min-width: 100%;
      height: auto;
    }}
    .chart-axis {{
      stroke: #bdb5a8;
      stroke-width: 1;
    }}
    .chart-grid-line {{
      stroke: #e8e1d7;
      stroke-width: 1;
    }}
    .chart-label {{
      fill: var(--muted);
      font-size: 16px;
      font-weight: 650;
    }}
    .chart-axis-title {{
      fill: var(--muted);
      font-size: 17px;
      font-weight: 750;
    }}
    .chart-x-label {{
      fill: var(--muted);
      font-size: 14px;
      font-weight: 650;
    }}
    .bar-rect {{
      fill: var(--bar-dark);
    }}
    .chart-empty-hotspot {{
      fill: transparent;
      pointer-events: all;
    }}
    .chart-hotspot {{
      cursor: help;
    }}
    .chart-hotspot:focus {{
      outline: 2px solid var(--ink);
      outline-offset: 2px;
    }}
    .bar-rect.outlier {{
      fill: var(--accent-2);
    }}
    .line-raw {{
      fill: none;
      stroke: var(--accent-3);
      stroke-width: 2;
    }}
    .line-smooth {{
      fill: none;
      stroke: var(--accent-2);
      stroke-width: 3;
    }}
    .point {{
      fill: var(--accent-3);
      stroke: #fffefa;
      stroke-width: 2;
    }}
    .point.outlier {{
      fill: var(--bad);
      stroke: var(--ink);
    }}
    .chart-tooltip {{
      position: fixed;
      left: 0;
      top: 0;
      z-index: 100;
      max-width: min(430px, calc(100vw - 24px));
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 253, 248, 0.98);
      box-shadow: 0 10px 26px rgba(31, 35, 38, 0.16);
      color: var(--ink);
      font-size: 0.95rem;
      line-height: 1.45;
      white-space: pre-line;
      pointer-events: none;
      opacity: 0;
      visibility: hidden;
      transform: translate(-9999px, -9999px);
    }}
    .chart-tooltip.visible {{
      opacity: 1;
      visibility: visible;
    }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px 16px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 1rem;
      font-weight: 650;
    }}
    .legend-item {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .legend-swatch {{
      width: 18px;
      height: 4px;
      border-radius: 999px;
      background: var(--accent-3);
    }}
    .legend-swatch.smooth {{
      background: var(--accent-2);
    }}
    .legend-swatch.bar {{
      height: 10px;
      background: var(--bar-dark);
    }}
    .split {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(300px, 0.8fr);
      gap: 16px;
      align-items: start;
    }}
    .bars {{
      display: grid;
      gap: 8px;
    }}
    .bar-row {{
      display: grid;
      grid-template-columns: minmax(150px, 1fr) minmax(120px, 2fr) 112px;
      gap: 10px;
      align-items: center;
      min-height: 30px;
    }}
    .bar-label {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .bar-track {{
      height: 12px;
      background: #e7e1d8;
      border-radius: 999px;
      overflow: hidden;
    }}
    .bar-fill {{
      height: 100%;
      background: var(--bar-dark);
      border-radius: inherit;
    }}
    .bar-value {{
      color: var(--muted);
      font-variant-numeric: tabular-nums;
      text-align: right;
    }}
    .table-wrap {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.93rem;
    }}
    th,
    td {{
      padding: 9px 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 700;
      white-space: nowrap;
    }}
    td.number,
    th.number {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .empty {{
      color: var(--muted);
      padding: 18px;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #fffefa;
    }}
    @media (max-width: 900px) {{
      .split {{
        grid-template-columns: 1fr;
      }}
      .bar-row {{
        grid-template-columns: 1fr;
        gap: 4px;
        align-items: stretch;
      }}
      .bar-value {{
        text-align: left;
      }}
    }}
    @media (max-width: 640px) {{
      .wrap {{
        width: min(100vw - 20px, 1380px);
        padding-top: 14px;
      }}
      .hero,
      .panel {{
        padding: 14px;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <div class="hero-top">
        <div>
          <h1>
            <span class="lang lang-zh">阅读统计报告</span>
            <span class="lang lang-en">Books Reading Report</span>
          </h1>
        </div>
        <div class="actions">
          <div class="segmented" aria-label="Language">
            <button type="button" class="lang-button active" data-lang-set="zh">中文</button>
            <button type="button" class="lang-button" data-lang-set="en">EN</button>
          </div>
        </div>
      </div>
      <div class="meta" id="metaChips"></div>
      <div class="summary" id="summaryCards"></div>
    </header>

    <section class="panel">
      <h2><span class="lang lang-zh">筛选和趋势</span><span class="lang lang-en">Filters And Trends</span></h2>
      <div class="controls">
        <label>
          <span class="control-label"><span class="lang lang-zh">书架</span><span class="lang lang-en">Shelf</span></span>
          <select id="shelfFilter"></select>
        </label>
        <label>
          <span class="control-label"><span class="lang lang-zh">搜索</span><span class="lang lang-en">Search</span></span>
          <input id="bookSearch" type="search">
        </label>
        <div>
          <span class="control-label"><span class="lang lang-zh">时间粒度</span><span class="lang lang-en">Time Grain</span></span>
          <div class="segmented" id="periodButtons">
            <button type="button" class="segmented-button active" data-period="day"><span class="lang lang-zh">日</span><span class="lang lang-en">Day</span></button>
            <button type="button" class="segmented-button" data-period="week"><span class="lang lang-zh">周</span><span class="lang lang-en">Week</span></button>
            <button type="button" class="segmented-button" data-period="month"><span class="lang lang-zh">月</span><span class="lang lang-en">Month</span></button>
          </div>
        </div>
        <div>
          <span class="control-label"><span class="lang lang-zh">指标</span><span class="lang lang-en">Metric</span></span>
          <div class="segmented" id="metricButtons">
            <button type="button" class="segmented-button active" data-metric="characters"><span class="lang lang-zh">字数</span><span class="lang lang-en">Chars</span></button>
            <button type="button" class="segmented-button" data-metric="time"><span class="lang lang-zh">时长</span><span class="lang lang-en">Time</span></button>
            <button type="button" class="segmented-button" data-metric="speed"><span class="lang lang-zh">速度</span><span class="lang lang-en">Speed</span></button>
          </div>
        </div>
        <div id="speedModeControl">
          <span class="control-label"><span class="lang lang-zh">速度线</span><span class="lang lang-en">Speed Line</span></span>
          <div class="segmented" id="speedModeButtons">
            <button type="button" class="segmented-button" data-speed-mode="raw"><span class="lang lang-zh">原始</span><span class="lang lang-en">Raw</span></button>
            <button type="button" class="segmented-button active" data-speed-mode="smooth"><span class="lang lang-zh">平滑</span><span class="lang lang-en">Smooth</span></button>
            <button type="button" class="segmented-button" data-speed-mode="both"><span class="lang lang-zh">全部</span><span class="lang lang-en">Both</span></button>
          </div>
        </div>
      </div>
      <div class="chart-shell" style="margin-top: 16px;">
        <svg id="trendChart" role="img"></svg>
      </div>
      <div class="legend" id="chartLegend"></div>
    </section>

    <section class="panel split">
      <div>
        <h2><span class="lang lang-zh">书籍排行</span><span class="lang lang-en">Book Ranking</span></h2>
        <div class="segmented" id="rankButtons" style="margin-bottom: 12px;">
          <button type="button" class="segmented-button active" data-rank="characters"><span class="lang lang-zh">字数</span><span class="lang lang-en">Chars</span></button>
          <button type="button" class="segmented-button" data-rank="time"><span class="lang lang-zh">时长</span><span class="lang lang-en">Time</span></button>
          <button type="button" class="segmented-button" data-rank="speed"><span class="lang lang-zh">速度</span><span class="lang lang-en">Speed</span></button>
        </div>
        <div id="rankBars"></div>
      </div>
      <div>
        <h2><span class="lang lang-zh">速度摘要</span><span class="lang lang-en">Speed Summary</span></h2>
        <div class="table-wrap">
          <table>
            <tbody id="speedSummaryTable"></tbody>
          </table>
        </div>
      </div>
    </section>

    <section class="panel">
      <h2><span class="lang lang-zh">书架对比</span><span class="lang lang-en">Shelf Comparison</span></h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th><span class="lang lang-zh">书架</span><span class="lang lang-en">Shelf</span></th>
              <th class="number"><span class="lang lang-zh">书籍</span><span class="lang lang-en">Books</span></th>
              <th class="number"><span class="lang lang-zh">总字数</span><span class="lang lang-en">Total Chars</span></th>
              <th class="number"><span class="lang lang-zh">阅读字数</span><span class="lang lang-en">Read Chars</span></th>
              <th class="number"><span class="lang lang-zh">时长</span><span class="lang lang-en">Hours</span></th>
              <th class="number"><span class="lang lang-zh">均速</span><span class="lang lang-en">Avg Speed</span></th>
            </tr>
          </thead>
          <tbody id="shelfTable"></tbody>
        </table>
      </div>
    </section>

  </div>

  <script id="report-data" type="application/json">{data_json}</script>
  <script>
    const reportData = JSON.parse(document.getElementById('report-data').textContent);
    const OUTLIER_WINDOW_SIZE = 15;
    const OUTLIER_MIN_POINTS = 8;
    const root = document.documentElement;
    const state = {{
      lang: 'zh',
      period: 'day',
      metric: 'characters',
      speedMode: 'smooth',
      rank: 'characters',
      shelf: 'all',
      query: '',
    }};
    const chartTooltip = document.createElement('div');
    chartTooltip.className = 'chart-tooltip';
    document.body.appendChild(chartTooltip);
    const text = {{
      zh: {{
        allShelves: '全部书架',
        noShelf: '未分书架',
        searchPlaceholder: '按书名或书架筛选',
        books: '本书',
        statsBooks: '有统计',
        recordedChars: '记录阅读字数',
        hours: '阅读小时',
        activeDays: '活跃天数',
        avgSpeed: '加权均速',
        recentSpeed: '近 7 活跃日均速',
        medianSpeed: '活跃日速度中位数',
        speedChange: '近期较早期变化',
        bestSpeedDay: '最高速度日',
        worstSpeedDay: '最低速度日',
        range: '日期范围',
        generated: '生成',
        source: '数据源',
        timezone: '时区',
        chars: '字',
        charsPerHour: '字/小时',
        noData: '没有匹配数据',
        rawSpeed: '原始速度',
        smoothSpeed: '移动平均',
        outlier: '异常速度点',
        localOutlierRange: '局部阈值',
        total: '合计',
        days: '天',
        records: '条记录',
        progress: '进度',
        bookmarkPosition: '书签位置',
        bookmarkShort: '书签',
        recordedCoverage: '统计覆盖',
        lastAccess: '最后访问',
        noBookmark: '无书签',
        noBookmarkPosition: '无书签位置',
        noStats: '无统计',
      }},
      en: {{
        allShelves: 'All shelves',
        noShelf: 'No shelf',
        searchPlaceholder: 'Filter by title or shelf',
        books: 'Books',
        statsBooks: 'With stats',
        recordedChars: 'Recorded chars',
        hours: 'Reading hours',
        activeDays: 'Active days',
        avgSpeed: 'Weighted avg speed',
        recentSpeed: 'Recent 7-day speed',
        medianSpeed: 'Median day speed',
        speedChange: 'Recent vs early change',
        bestSpeedDay: 'Fastest day',
        worstSpeedDay: 'Slowest day',
        range: 'Date range',
        generated: 'Generated',
        source: 'Source',
        timezone: 'Timezone',
        chars: 'chars',
        charsPerHour: 'chars/hour',
        noData: 'No matching data',
        rawSpeed: 'Raw speed',
        smoothSpeed: 'Moving average',
        outlier: 'Speed outlier',
        localOutlierRange: 'Local threshold',
        total: 'Total',
        days: 'days',
        records: 'records',
        progress: 'Progress',
        bookmarkPosition: 'Bookmark position',
        bookmarkShort: 'Bookmark',
        recordedCoverage: 'Recorded coverage',
        lastAccess: 'Last access',
        noBookmark: 'No bookmark',
        noBookmarkPosition: 'No bookmark position',
        noStats: 'No stats',
      }},
    }};

    function t(key) {{
      return (text[state.lang] && text[state.lang][key]) || text.en[key] || key;
    }}

    function escapeHtml(value) {{
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}

    function escapeAttr(value) {{
      return escapeHtml(value).replaceAll('\\n', '&#10;');
    }}

    function fmtNumber(value, digits = 0) {{
      if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
      return Number(value).toLocaleString(state.lang === 'zh' ? 'zh-CN' : 'en-US', {{
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      }});
    }}

    function fmtCompact(value) {{
      if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
      return Number(value).toLocaleString(state.lang === 'zh' ? 'zh-CN' : 'en-US', {{
        notation: 'compact',
        maximumFractionDigits: 1,
      }});
    }}

    function fmtPercent(value, digits = 1) {{
      if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
      return `${{fmtNumber(Number(value) * 100, digits)}}%`;
    }}

    function fmtSignedPercent(value) {{
      if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
      const sign = Number(value) > 0 ? '+' : '';
      return `${{sign}}${{fmtNumber(value, 1)}}%`;
    }}

    function moveTooltipTo(clientX, clientY) {{
      const margin = 12;
      const rect = chartTooltip.getBoundingClientRect();
      let left = clientX + margin;
      let top = clientY - rect.height - margin;
      if (left + rect.width > window.innerWidth - margin) {{
        left = clientX - rect.width - margin;
      }}
      if (top < margin) {{
        top = clientY + margin;
      }}
      chartTooltip.style.transform =
        `translate(${{Math.max(margin, left)}}px, ${{Math.max(margin, top)}}px)`;
    }}

    function showTooltip(event) {{
      const text = event.currentTarget.dataset.tooltip;
      if (!text) return;
      chartTooltip.textContent = text;
      chartTooltip.classList.add('visible');
      moveTooltipTo(event.clientX, event.clientY);
    }}

    function moveTooltip(event) {{
      if (!chartTooltip.classList.contains('visible')) return;
      moveTooltipTo(event.clientX, event.clientY);
    }}

    function showTooltipForElement(event) {{
      const text = event.currentTarget.dataset.tooltip;
      if (!text) return;
      chartTooltip.textContent = text;
      chartTooltip.classList.add('visible');
      const rect = event.currentTarget.getBoundingClientRect();
      moveTooltipTo(rect.left + rect.width / 2, rect.top + rect.height / 2);
    }}

    function hideTooltip() {{
      chartTooltip.textContent = '';
      chartTooltip.classList.remove('visible');
      chartTooltip.style.transform = 'translate(-9999px, -9999px)';
    }}

    function bindChartTooltips() {{
      document.querySelectorAll('#trendChart [data-tooltip]').forEach((element) => {{
        element.addEventListener('mouseenter', showTooltip);
        element.addEventListener('mousemove', moveTooltip);
        element.addEventListener('mouseleave', hideTooltip);
        element.addEventListener('focus', showTooltipForElement);
        element.addEventListener('blur', hideTooltip);
      }});
    }}

    function updateActiveButtons(groupSelector, attr, value) {{
      document.querySelectorAll(`${{groupSelector}} [${{attr}}]`).forEach((button) => {{
        button.classList.toggle('active', button.getAttribute(attr) === value);
      }});
    }}

    function setupControls() {{
      document.querySelectorAll('[data-lang-set]').forEach((button) => {{
        button.addEventListener('click', () => {{
          state.lang = button.dataset.langSet;
          root.dataset.lang = state.lang;
          document.querySelectorAll('[data-lang-set]').forEach((item) => {{
            item.classList.toggle('active', item.dataset.langSet === state.lang);
          }});
          render();
        }});
      }});
      document.querySelectorAll('[data-period]').forEach((button) => {{
        button.addEventListener('click', () => {{
          state.period = button.dataset.period;
          updateActiveButtons('#periodButtons', 'data-period', state.period);
          render();
        }});
      }});
      document.querySelectorAll('[data-metric]').forEach((button) => {{
        button.addEventListener('click', () => {{
          state.metric = button.dataset.metric;
          updateActiveButtons('#metricButtons', 'data-metric', state.metric);
          render();
        }});
      }});
      document.querySelectorAll('[data-speed-mode]').forEach((button) => {{
        button.addEventListener('click', () => {{
          state.speedMode = button.dataset.speedMode;
          updateActiveButtons('#speedModeButtons', 'data-speed-mode', state.speedMode);
          render();
        }});
      }});
      document.querySelectorAll('[data-rank]').forEach((button) => {{
        button.addEventListener('click', () => {{
          state.rank = button.dataset.rank;
          updateActiveButtons('#rankButtons', 'data-rank', state.rank);
          render();
        }});
      }});
      document.getElementById('shelfFilter').addEventListener('change', (event) => {{
        state.shelf = event.target.value;
        render();
      }});
      document.getElementById('bookSearch').addEventListener('input', (event) => {{
        state.query = event.target.value;
        render();
      }});
    }}

    function renderSelects() {{
      const shelfFilter = document.getElementById('shelfFilter');
      const speedModeControl = document.getElementById('speedModeControl');
      shelfFilter.innerHTML = [
        `<option value="all">${{escapeHtml(t('allShelves'))}}</option>`,
        `<option value="__none__">${{escapeHtml(t('noShelf'))}}</option>`,
        ...reportData.shelves.map((shelf) => `<option value="${{escapeHtml(shelf)}}">${{escapeHtml(shelf)}}</option>`),
      ].join('');
      shelfFilter.value = state.shelf;
      document.getElementById('bookSearch').placeholder = t('searchPlaceholder');
      speedModeControl.hidden = state.metric !== 'speed';
    }}

    function filteredBooks() {{
      const query = state.query.trim().toLowerCase();
      return reportData.books.filter((book) => {{
        const shelves = book.shelves || [];
        if (state.shelf === '__none__' && shelves.length) return false;
        if (state.shelf !== 'all' && state.shelf !== '__none__' && !shelves.includes(state.shelf)) return false;
        if (!query) return true;
        const haystack = [book.title, book.folder, ...(book.shelves || [])].join(' ').toLowerCase();
        return haystack.includes(query);
      }});
    }}

    function filteredStats(books) {{
      const ids = new Set(books.map((book) => book.id));
      return reportData.stats.filter((stat) => ids.has(stat.bookId));
    }}

    function periodKey() {{
      if (state.period === 'week') return 'week';
      if (state.period === 'month') return 'month';
      return 'date';
    }}

    function parseDayLabel(label) {{
      const [year, month, day] = String(label).split('-').map(Number);
      return new Date(Date.UTC(year, month - 1, day));
    }}

    function formatDayLabel(date) {{
      return [
        date.getUTCFullYear(),
        String(date.getUTCMonth() + 1).padStart(2, '0'),
        String(date.getUTCDate()).padStart(2, '0'),
      ].join('-');
    }}

    function parseMonthLabel(label) {{
      const [year, month] = String(label).split('-').map(Number);
      return new Date(Date.UTC(year, month - 1, 1));
    }}

    function formatMonthLabel(date) {{
      return [
        date.getUTCFullYear(),
        String(date.getUTCMonth() + 1).padStart(2, '0'),
      ].join('-');
    }}

    function mondayOfIsoWeek(label) {{
      const [yearText, weekText] = String(label).split('-W');
      const year = Number(yearText);
      const week = Number(weekText);
      const jan4 = new Date(Date.UTC(year, 0, 4));
      const jan4Day = jan4.getUTCDay() || 7;
      const week1Monday = new Date(jan4);
      week1Monday.setUTCDate(jan4.getUTCDate() - jan4Day + 1);
      const monday = new Date(week1Monday);
      monday.setUTCDate(week1Monday.getUTCDate() + (week - 1) * 7);
      return monday;
    }}

    function formatWeekLabel(date) {{
      const target = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
      const day = target.getUTCDay() || 7;
      target.setUTCDate(target.getUTCDate() + 4 - day);
      const isoYear = target.getUTCFullYear();
      const yearStart = new Date(Date.UTC(isoYear, 0, 1));
      const week = Math.ceil((((target - yearStart) / 86400000) + 1) / 7);
      return `${{isoYear}}-W${{String(week).padStart(2, '0')}}`;
    }}

    function emptyPeriodRow(label) {{
      const day = state.period === 'day' ? label : '';
      const week = state.period === 'week' ? label : '';
      const month = state.period === 'month' ? label : '';
      return {{
        label,
        date: day,
        week,
        month,
        charactersRead: 0,
        readingTimeHours: 0,
        readingTimeSeconds: 0,
        speedCharacters: 0,
        speedTimeHours: 0,
        speedTimeSeconds: 0,
        recordCount: 0,
        books: new Map(),
        bookBreakdown: [],
        speed: null,
        smoothSpeed: null,
        outlier: false,
        outlierLow: null,
        outlierHigh: null,
        sampleMinSpeed: null,
        sampleMaxSpeed: null,
      }};
    }}

    function fillContinuousPeriods(rows) {{
      if (rows.length <= 1) return rows;
      const byLabel = new Map(rows.map((row) => [row.label, row]));
      const filled = [];
      if (state.period === 'day') {{
        const cursor = parseDayLabel(rows[0].label);
        const end = parseDayLabel(rows[rows.length - 1].label);
        while (cursor <= end) {{
          const label = formatDayLabel(cursor);
          filled.push(byLabel.get(label) || emptyPeriodRow(label));
          cursor.setUTCDate(cursor.getUTCDate() + 1);
        }}
        return filled;
      }}
      if (state.period === 'week') {{
        const cursor = mondayOfIsoWeek(rows[0].label);
        const end = mondayOfIsoWeek(rows[rows.length - 1].label);
        while (cursor <= end) {{
          const label = formatWeekLabel(cursor);
          filled.push(byLabel.get(label) || emptyPeriodRow(label));
          cursor.setUTCDate(cursor.getUTCDate() + 7);
        }}
        return filled;
      }}
      if (state.period === 'month') {{
        const cursor = parseMonthLabel(rows[0].label);
        const end = parseMonthLabel(rows[rows.length - 1].label);
        while (cursor <= end) {{
          const label = formatMonthLabel(cursor);
          filled.push(byLabel.get(label) || emptyPeriodRow(label));
          cursor.setUTCMonth(cursor.getUTCMonth() + 1);
        }}
        return filled;
      }}
      return rows;
    }}

    function aggregateStats(stats) {{
      const key = periodKey();
      const map = new Map();
      for (const stat of stats) {{
        const label = stat[key];
        if (!map.has(label)) {{
          map.set(label, {{
            label,
            charactersRead: 0,
            readingTimeHours: 0,
            readingTimeSeconds: 0,
            speedCharacters: 0,
            speedTimeHours: 0,
            speedTimeSeconds: 0,
            recordCount: 0,
            books: new Map(),
            outlier: false,
            sampleMinSpeed: null,
            sampleMaxSpeed: null,
          }});
        }}
        const row = map.get(label);
        row.charactersRead += Number(stat.charactersRead || 0);
        row.readingTimeHours += Number(stat.readingTimeHours || 0);
        row.readingTimeSeconds += Number(stat.readingTimeSeconds || 0);
        row.speedCharacters += Number(stat.speedCharacters || 0);
        row.speedTimeHours += Number(stat.speedTimeHours || 0);
        row.speedTimeSeconds += Number(stat.speedTimeSeconds || 0);
        row.recordCount += 1;
        row.books.set(stat.title, (row.books.get(stat.title) || 0) + Number(stat.charactersRead || 0));
        if (stat.speed !== null && stat.speed !== undefined) {{
          row.sampleMinSpeed = row.sampleMinSpeed === null ? stat.speed : Math.min(row.sampleMinSpeed, stat.speed);
          row.sampleMaxSpeed = row.sampleMaxSpeed === null ? stat.speed : Math.max(row.sampleMaxSpeed, stat.speed);
        }}
      }}
      let rows = Array.from(map.values()).sort((a, b) => a.label.localeCompare(b.label));
      for (const row of rows) {{
        row.speed = row.speedTimeSeconds > 0 && row.speedCharacters > 0
          ? row.speedCharacters / (row.speedTimeSeconds / 3600)
          : null;
        row.bookBreakdown = Array.from(row.books.entries())
          .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
          .slice(0, 8);
      }}
      rows = fillContinuousPeriods(rows);
      markOutliers(rows);
      addMovingAverage(rows);
      return rows;
    }}

    function percentile(values, pct) {{
      if (!values.length) return 0;
      const sorted = [...values].sort((a, b) => a - b);
      const position = (sorted.length - 1) * pct;
      const lower = Math.floor(position);
      const upper = Math.ceil(position);
      if (lower === upper) return sorted[lower];
      const fraction = position - lower;
      return sorted[lower] * (1 - fraction) + sorted[upper] * fraction;
    }}

    function markOutliers(rows) {{
      rows.forEach((row) => {{
        row.outlier = false;
        row.outlierLow = null;
        row.outlierHigh = null;
      }});
      const validRows = rows.filter((row) => row.speed !== null && Number.isFinite(row.speed));
      const halfWindow = Math.max(1, Math.floor(OUTLIER_WINDOW_SIZE / 2));
      validRows.forEach((row, index) => {{
        const windowRows = validRows.slice(
          Math.max(0, index - halfWindow),
          Math.min(validRows.length, index + halfWindow + 1),
        );
        if (windowRows.length < OUTLIER_MIN_POINTS) return;
        const speeds = windowRows.map((item) => item.speed);
        const q1 = percentile(speeds, 0.25);
        const q3 = percentile(speeds, 0.75);
        const iqr = q3 - q1;
        if (iqr <= 0) return;
        const low = q1 - 1.5 * iqr;
        const high = q3 + 1.5 * iqr;
        row.outlierLow = low;
        row.outlierHigh = high;
        row.outlier = row.speed < low || row.speed > high;
      }});
    }}

    function addMovingAverage(rows) {{
      const windowSize = state.period === 'day' ? 7 : 3;
      rows.forEach((row, index) => {{
        const slice = rows.slice(Math.max(0, index - windowSize + 1), index + 1);
        const chars = slice.reduce((sum, item) => sum + item.charactersRead, 0);
        const speedChars = slice.reduce((sum, item) => sum + item.speedCharacters, 0);
        const speedSeconds = slice.reduce((sum, item) => sum + item.speedTimeSeconds, 0);
        row.smoothSpeed = speedSeconds > 0 && speedChars > 0 ? speedChars / (speedSeconds / 3600) : null;
      }});
    }}

    function aggregateDailySummaryRows(stats) {{
      const map = new Map();
      for (const stat of stats) {{
        const label = stat.date;
        if (!map.has(label)) {{
          map.set(label, {{
            label,
            charactersRead: 0,
            readingTimeHours: 0,
            readingTimeSeconds: 0,
            speedCharacters: 0,
            speedTimeSeconds: 0,
            recordCount: 0,
          }});
        }}
        const row = map.get(label);
        row.charactersRead += Number(stat.charactersRead || 0);
        row.readingTimeHours += Number(stat.readingTimeHours || 0);
        row.readingTimeSeconds += Number(stat.readingTimeSeconds || 0);
        row.speedCharacters += Number(stat.speedCharacters || 0);
        row.speedTimeSeconds += Number(stat.speedTimeSeconds || 0);
        row.recordCount += 1;
      }}
      return Array.from(map.values())
        .sort((a, b) => a.label.localeCompare(b.label))
        .map((row) => ({{
          ...row,
          speed: row.speedTimeSeconds > 0 && row.speedCharacters > 0
            ? row.speedCharacters / (row.speedTimeSeconds / 3600)
            : null,
        }}));
    }}

    function filteredSummary(books, rows) {{
      const totalChars = books.reduce((sum, book) => sum + Number(book.totalCharacters || 0), 0);
      const recorded = books.reduce((sum, book) => sum + Number(book.recordedCharacters || 0), 0);
      const hours = books.reduce((sum, book) => sum + Number(book.readingTimeHours || 0), 0);
      const allStats = filteredStats(books);
      const dailySummaryRows = aggregateDailySummaryRows(allStats);
      const speedStats = allStats.filter((stat) => stat.speed !== null);
      const speedChars = speedStats.reduce((sum, stat) => sum + Number(stat.speedCharacters || 0), 0);
      const speedSeconds = speedStats.reduce((sum, stat) => sum + Number(stat.speedTimeSeconds || 0), 0);
      const validSpeedRows = dailySummaryRows.filter((row) => row.speed !== null);
      const recent7 = validSpeedRows.slice(-7);
      return {{
        bookCount: books.length,
        statsBooks: books.filter((book) => book.hasStatistics).length,
        totalChars,
        recorded,
        hours,
        activeDays: dailySummaryRows.length,
        avgSpeed: speedSeconds > 0 && speedChars > 0 ? speedChars / (speedSeconds / 3600) : null,
        medianSpeed: median(validSpeedRows.map((row) => row.speed)),
        recentSpeed: weightedSpeed(recent7),
        range: dailySummaryRows.length ? `${{dailySummaryRows[0].label}} - ${{dailySummaryRows[dailySummaryRows.length - 1].label}}` : '-',
      }};
    }}

    function median(values) {{
      const clean = values.filter((value) => value !== null && Number.isFinite(value)).sort((a, b) => a - b);
      if (!clean.length) return null;
      const mid = Math.floor(clean.length / 2);
      return clean.length % 2 ? clean[mid] : (clean[mid - 1] + clean[mid]) / 2;
    }}

    function weightedSpeed(rows) {{
      const chars = rows.reduce((sum, row) => sum + Number(row.speedCharacters ?? row.charactersRead ?? 0), 0);
      const seconds = rows.reduce((sum, row) => sum + Number(row.speedTimeSeconds ?? row.readingTimeSeconds ?? 0), 0);
      return seconds > 0 && chars > 0 ? chars / (seconds / 3600) : null;
    }}

    function renderMeta() {{
      const summary = reportData.summary;
      const chips = [
        `${{t('timezone')}}: ${{reportData.timezone}}`,
        `${{t('generated')}}: ${{reportData.generatedAt}}`,
        `${{t('range')}}: ${{summary.dateStart || '-'}} - ${{summary.dateEnd || '-'}}`,
      ];
      document.getElementById('metaChips').innerHTML = chips
        .map((chip) => `<span class="chip">${{escapeHtml(chip)}}</span>`)
        .join('');
    }}

    function renderSummaryCards(summary) {{
      const cards = [
        [t('books'), fmtNumber(summary.bookCount), `${{fmtNumber(summary.statsBooks)}} ${{t('statsBooks')}}`],
        [t('recordedChars'), fmtNumber(summary.recorded), `${{fmtNumber(summary.totalChars)}} ${{t('total')}}`],
        [t('hours'), fmtNumber(summary.hours, 1), t('activeDays') + `: ${{fmtNumber(summary.activeDays)}}`],
        [t('avgSpeed'), fmtNumber(summary.avgSpeed), t('charsPerHour')],
        [t('recentSpeed'), fmtNumber(summary.recentSpeed), t('charsPerHour')],
        [t('medianSpeed'), fmtNumber(summary.medianSpeed), t('charsPerHour')],
        [t('range'), summary.range, ''],
      ];
      document.getElementById('summaryCards').innerHTML = cards.map(([label, value, note]) => `
        <div class="summary-card">
          <div class="summary-value">${{escapeHtml(value)}}</div>
          <div class="summary-label">${{escapeHtml(label)}}</div>
          ${{note ? `<div class="summary-note">${{escapeHtml(note)}}</div>` : ''}}
        </div>
      `).join('');
    }}

    function metricValue(row) {{
      if (state.metric === 'time') return row.readingTimeHours;
      if (state.metric === 'speed') {{
        if (state.speedMode === 'smooth') return row.smoothSpeed;
        return row.speed;
      }}
      return row.charactersRead;
    }}

    function metricLabel() {{
      if (state.metric === 'time') return state.lang === 'zh' ? '小时' : 'hours';
      if (state.metric === 'speed') return t('charsPerHour');
      return t('chars');
    }}

    function rowTooltip(row, includeOutlier = true) {{
      const pieces = [
        row.label,
        `${{t('recordedChars')}}: ${{fmtNumber(row.charactersRead)}}`,
        `${{t('hours')}}: ${{fmtNumber(row.readingTimeHours, 2)}}`,
        `${{t('avgSpeed')}}: ${{fmtNumber(row.speed)}} ${{t('charsPerHour')}}`,
      ];
      if (row.smoothSpeed !== null) pieces.push(`${{t('smoothSpeed')}}: ${{fmtNumber(row.smoothSpeed)}} ${{t('charsPerHour')}}`);
      if (row.sampleMinSpeed !== null && row.sampleMaxSpeed !== null) {{
        pieces.push(`min/max: ${{fmtNumber(row.sampleMinSpeed)}} / ${{fmtNumber(row.sampleMaxSpeed)}}`);
      }}
      if (includeOutlier && row.outlier) {{
        pieces.push(t('outlier'));
        if (row.outlierLow !== null && row.outlierHigh !== null) {{
          pieces.push(`${{t('localOutlierRange')}}: ${{fmtNumber(row.outlierLow)}} - ${{fmtNumber(row.outlierHigh)}}`);
        }}
      }}
      if (row.bookBreakdown.length) {{
        pieces.push(...row.bookBreakdown.slice(0, 5).map(([title, chars]) => `${{title}}: ${{fmtNumber(chars)}}`));
      }}
      return pieces.join('\\n');
    }}

    function renderChart(rows) {{
      const svg = document.getElementById('trendChart');
      const shell = document.querySelector('.chart-shell');
      const containerWidth = Math.max(980, Math.floor(shell.getBoundingClientRect().width));
      const width = Math.max(containerWidth, rows.length * 42 + 150);
      const height = 460;
      const margin = {{ top: 36, right: 34, bottom: 118, left: 96 }};
      const plotW = width - margin.left - margin.right;
      const plotH = height - margin.top - margin.bottom;
      svg.removeAttribute('viewBox');
      svg.setAttribute('width', width);
      svg.setAttribute('height', height);
      svg.innerHTML = '';
      if (!rows.length) {{
        hideTooltip();
        svg.innerHTML = `<text x="32" y="52" class="chart-label">${{escapeHtml(t('noData'))}}</text>`;
        document.getElementById('chartLegend').innerHTML = '';
        return;
      }}
      const values = rows.map(metricValue).filter((value) => value !== null && Number.isFinite(value));
      const maxValue = Math.max(...values, 1);
      const yMax = maxValue * 1.1;
      const xStep = plotW / Math.max(rows.length, 1);
      const x = (index) => margin.left + xStep * index + xStep / 2;
      const y = (value) => margin.top + plotH - (Number(value || 0) / yMax) * plotH;
      const parts = [];
      for (let i = 0; i <= 4; i += 1) {{
        const value = yMax * (i / 4);
        const yy = y(value);
        parts.push(`<line x1="${{margin.left}}" y1="${{yy}}" x2="${{width - margin.right}}" y2="${{yy}}" class="chart-grid-line"></line>`);
        parts.push(`<text x="${{margin.left - 12}}" y="${{yy + 5}}" text-anchor="end" class="chart-label">${{fmtCompact(value)}}</text>`);
      }}
      parts.push(`<line x1="${{margin.left}}" y1="${{margin.top + plotH}}" x2="${{width - margin.right}}" y2="${{margin.top + plotH}}" class="chart-axis"></line>`);
      parts.push(`<line x1="${{margin.left}}" y1="${{margin.top}}" x2="${{margin.left}}" y2="${{margin.top + plotH}}" class="chart-axis"></line>`);
      parts.push(`<text x="${{margin.left}}" y="24" class="chart-axis-title">${{escapeHtml(metricLabel())}}</text>`);

      const labelEvery = Math.max(1, Math.ceil(rows.length / 11));
      rows.forEach((row, index) => {{
        if (index % labelEvery === 0 || index === rows.length - 1) {{
          parts.push(`<text x="${{x(index)}}" y="${{height - 62}}" text-anchor="end" transform="rotate(-35 ${{x(index)}} ${{height - 62}})" class="chart-x-label">${{escapeHtml(row.label)}}</text>`);
        }}
      }});

      if (state.metric === 'speed') {{
        const rawPoints = rows
          .map((row, index) => row.speed === null ? null : [x(index), y(row.speed), row])
          .filter(Boolean);
        const smoothPoints = rows
          .map((row, index) => row.smoothSpeed === null ? null : [x(index), y(row.smoothSpeed), row])
          .filter(Boolean);
        if ((state.speedMode === 'raw' || state.speedMode === 'both') && rawPoints.length) {{
          parts.push(`<path class="line-raw" d="${{pathFromPoints(rawPoints)}}"></path>`);
        }}
        if ((state.speedMode === 'smooth' || state.speedMode === 'both') && smoothPoints.length) {{
          parts.push(`<path class="line-smooth" d="${{pathFromPoints(smoothPoints)}}"></path>`);
        }}
        if (state.speedMode === 'raw' || state.speedMode === 'both') {{
          for (const [cx, cy, row] of rawPoints) {{
            const tooltip = escapeAttr(rowTooltip(row, true));
            parts.push(`<circle cx="${{cx}}" cy="${{cy}}" r="${{row.outlier ? 5 : 3.5}}" class="point chart-hotspot ${{row.outlier ? 'outlier' : ''}}" tabindex="0" data-tooltip="${{tooltip}}" aria-label="${{tooltip}}"></circle>`);
          }}
        }}
        if (state.speedMode === 'smooth') {{
          for (const [cx, cy, row] of smoothPoints) {{
            const tooltip = escapeAttr(rowTooltip(row, false));
            parts.push(`<circle cx="${{cx}}" cy="${{cy}}" r="3.5" class="point chart-hotspot" tabindex="0" data-tooltip="${{tooltip}}" aria-label="${{tooltip}}"></circle>`);
          }}
        }}
      }} else {{
        const barWidth = Math.max(4, Math.min(24, xStep * 0.62));
        rows.forEach((row, index) => {{
          const value = metricValue(row);
          if (value === null || !Number.isFinite(value)) return;
          const xx = x(index) - barWidth / 2;
          const tooltip = escapeAttr(rowTooltip(row, false));
          if (value <= 0) {{
            parts.push(`<rect x="${{xx}}" y="${{margin.top}}" width="${{barWidth}}" height="${{plotH}}" class="chart-empty-hotspot chart-hotspot" tabindex="0" data-tooltip="${{tooltip}}" aria-label="${{tooltip}}"></rect>`);
            return;
          }}
          const yy = y(value);
          const hh = margin.top + plotH - yy;
          parts.push(`<rect x="${{xx}}" y="${{yy}}" width="${{barWidth}}" height="${{Math.max(1, hh)}}" class="bar-rect chart-hotspot" tabindex="0" data-tooltip="${{tooltip}}" aria-label="${{tooltip}}"></rect>`);
        }});
      }}
      svg.innerHTML = parts.join('');
      bindChartTooltips();
      renderLegend();
    }}

    function pathFromPoints(points) {{
      return points.map(([px, py], index) => `${{index === 0 ? 'M' : 'L'}} ${{px.toFixed(2)}} ${{py.toFixed(2)}}`).join(' ');
    }}

    function renderLegend() {{
      const items = [];
      if (state.metric === 'speed') {{
        if (state.speedMode === 'raw' || state.speedMode === 'both') {{
          items.push(`<span class="legend-item"><span class="legend-swatch"></span>${{escapeHtml(t('rawSpeed'))}}</span>`);
        }}
        if (state.speedMode === 'smooth' || state.speedMode === 'both') {{
          items.push(`<span class="legend-item"><span class="legend-swatch smooth"></span>${{escapeHtml(t('smoothSpeed'))}}</span>`);
        }}
        if (state.speedMode !== 'smooth') {{
          items.push(`<span class="legend-item"><span class="legend-swatch" style="background: var(--bad); height: 10px; width: 10px;"></span>${{escapeHtml(t('outlier'))}}</span>`);
        }}
      }} else {{
        items.push(`<span class="legend-item"><span class="legend-swatch bar"></span>${{escapeHtml(metricLabel())}}</span>`);
      }}
      document.getElementById('chartLegend').innerHTML = items.join('');
    }}

    function rankingValue(book) {{
      if (state.rank === 'time') return Number(book.readingTimeHours || 0);
      if (state.rank === 'speed') return Number(book.averageSpeed || 0);
      return Number(book.recordedCharacters || 0);
    }}

    function rankingFormat(value) {{
      if (state.rank === 'time') return `${{fmtNumber(value, 1)}} h`;
      if (state.rank === 'speed') return `${{fmtNumber(value)}} ${{t('charsPerHour')}}`;
      return fmtNumber(value);
    }}

    function renderRanking(books) {{
      const rows = [...books]
        .filter((book) => rankingValue(book) > 0)
        .sort((a, b) => rankingValue(b) - rankingValue(a) || a.title.localeCompare(b.title))
        .slice(0, reportData.topN);
      if (!rows.length) {{
        document.getElementById('rankBars').innerHTML = `<div class="empty">${{escapeHtml(t('noData'))}}</div>`;
        return;
      }}
      const maxValue = rankingValue(rows[0]) || 1;
      document.getElementById('rankBars').innerHTML = `
        <div class="bars">
          ${{rows.map((book) => {{
            const value = rankingValue(book);
            const width = Math.max(2, (value / maxValue) * 100);
            return `
              <div class="bar-row">
                <div class="bar-label" title="${{escapeHtml(book.title)}}">${{escapeHtml(book.title)}}</div>
                <div class="bar-track"><div class="bar-fill" style="width:${{width}}%"></div></div>
                <div class="bar-value">${{escapeHtml(rankingFormat(value))}}</div>
              </div>
            `;
          }}).join('')}}
        </div>
      `;
    }}

    function renderSpeedSummary(statsRows) {{
      const validRows = statsRows.filter((row) => row.speed !== null);
      const best = validRows.length ? validRows.reduce((a, b) => a.speed > b.speed ? a : b) : null;
      const worst = validRows.length ? validRows.reduce((a, b) => a.speed < b.speed ? a : b) : null;
      const early = weightedSpeed(validRows.slice(0, 14));
      const recent = weightedSpeed(validRows.slice(-14));
      const change = early && recent ? ((recent - early) / early) * 100 : null;
      const rows = [
        [t('avgSpeed'), `${{fmtNumber(weightedSpeed(validRows))}} ${{t('charsPerHour')}}`],
        [t('medianSpeed'), `${{fmtNumber(median(validRows.map((row) => row.speed)))}} ${{t('charsPerHour')}}`],
        [t('recentSpeed'), `${{fmtNumber(weightedSpeed(validRows.slice(-7)))}} ${{t('charsPerHour')}}`],
        [t('speedChange'), fmtSignedPercent(change)],
        [t('bestSpeedDay'), best ? `${{best.label}} · ${{fmtNumber(best.speed)}}` : '-'],
        [t('worstSpeedDay'), worst ? `${{worst.label}} · ${{fmtNumber(worst.speed)}}` : '-'],
      ];
      document.getElementById('speedSummaryTable').innerHTML = rows.map(([label, value]) => `
        <tr>
          <td>${{escapeHtml(label)}}</td>
          <td class="number">${{escapeHtml(value)}}</td>
        </tr>
      `).join('');
    }}

    function renderShelfTable(books) {{
      const groups = new Map();
      for (const book of books) {{
        const shelves = book.shelves && book.shelves.length ? book.shelves : [t('noShelf')];
        for (const shelf of shelves) {{
          if (!groups.has(shelf)) {{
            groups.set(shelf, {{ shelf, books: 0, total: 0, recorded: 0, hours: 0 }});
          }}
          const group = groups.get(shelf);
          group.books += 1;
          group.total += Number(book.totalCharacters || 0);
          group.recorded += Number(book.recordedCharacters || 0);
          group.hours += Number(book.readingTimeHours || 0);
        }}
      }}
      const rows = Array.from(groups.values()).sort((a, b) => b.recorded - a.recorded || a.shelf.localeCompare(b.shelf));
      document.getElementById('shelfTable').innerHTML = rows.length ? rows.map((row) => `
        <tr>
          <td>${{escapeHtml(row.shelf)}}</td>
          <td class="number">${{fmtNumber(row.books)}}</td>
          <td class="number">${{fmtNumber(row.total)}}</td>
          <td class="number">${{fmtNumber(row.recorded)}}</td>
          <td class="number">${{fmtNumber(row.hours, 1)}}</td>
          <td class="number">${{fmtNumber(row.hours > 0 ? row.recorded / row.hours : null)}}</td>
        </tr>
      `).join('') : `<tr><td colspan="6">${{escapeHtml(t('noData'))}}</td></tr>`;
    }}

    function render() {{
      renderSelects();
      renderMeta();
      const books = filteredBooks();
      const stats = filteredStats(books);
      const rows = aggregateStats(stats);
      const summary = filteredSummary(books, rows);
      renderSummaryCards(summary);
      renderChart(rows);
      renderRanking(books);
      renderSpeedSummary(rows);
      renderShelfTable(books);
    }}

    setupControls();
    render();
  </script>
</body>
</html>
"""


def generate_report(
    books_dir: Path,
    output_path: Path,
    time_zone,
    top_n: int = 20,
    min_reading_seconds: float = MIN_READING_TIME_SECONDS,
) -> tuple[Path, dict[str, Any]]:
    library = load_library(books_dir, time_zone, min_reading_seconds)
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_report_payload(
        library,
        timezone_display_name(time_zone),
        top_n=top_n,
    )
    output_path.write_text(render_html(payload), encoding="utf-8")
    return output_path, payload


def main() -> int:
    args = parse_args()
    time_zone = resolve_timezone(args.timezone)
    output_path, payload = generate_report(
        args.books_dir,
        args.output,
        time_zone,
        top_n=args.top,
        min_reading_seconds=args.min_reading_seconds,
    )
    summary = payload["summary"]
    print(f"Wrote {output_path}")
    print(
        "Books: "
        f"{summary['bookCount']}, "
        f"recorded chars: {int(summary['totalRecordedCharacters']):,}, "
        f"reading hours: {summary['totalReadingHours']:.2f}, "
        f"active days: {summary['activeDays']}"
    )
    if args.publish_pages:
        branch = publish_report_to_pages(
            report_path=output_path,
            books_dir=args.books_dir,
            remote=args.pages_remote,
            branch_prefix=args.pages_branch_prefix,
            profile_name=args.profile_name,
        )
        print(f"Pushed GitHub Pages report to branch {branch!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
