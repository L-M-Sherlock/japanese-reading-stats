# Japanese Reading Stats

Generate a local HTML report from reading statistics produced by
[Hoshi Reader Mac](https://github.com/W1ght/Hoshi-Reader-Mac). The parser is
also compatible with the data layout used by
[ebook-reader](https://github.com/ttu-ttu/ebook-reader).

The report focuses on reading progress, reading time, and reading speed changes.
It is intentionally static: one Python command reads the JSON files and writes a
self-contained HTML report.

## Usage

```bash
uv run scripts/visualize_books.py
```

By default, this reads Hoshi Reader Mac / ebook-reader data from:

```text
~/Library/Application Support/Books
```

and writes:

```text
output/books_reading_report.html
```

Common options:

```bash
uv run scripts/visualize_books.py \
  --books-dir "$HOME/Library/Application Support/Books" \
  --output output/books_reading_report.html \
  --timezone Asia/Shanghai \
  --top 20
```

## What It Shows

- Total books, total characters, recorded characters read, reading hours, active
  days, and reading date range
- Daily, weekly, and monthly trends for characters read, reading time, and
  weighted average reading speed
- Raw reading speed and moving average speed, with outlier speed days marked
- Book rankings by characters, time, speed, and progress
- Shelf summaries
- Book and shelf rankings by characters, time, speed, and progress

Reading speed is calculated as `charactersRead / readingTime` and displayed as
characters per hour. The report also keeps the original speed fields from
`statistics.json` in chart tooltips.

## Files

- `scripts/visualize_books.py`: report generator and CLI
- `tests/`: focused parser and aggregation tests
- `output/`: generated report files, ignored by git
