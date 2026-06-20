# Japanese Reading Stats

Generate a local HTML report from reading statistics produced by
[Hoshi Reader Mac](https://github.com/W1ght/Hoshi-Reader-Mac). The parser is
also compatible with the data layout used by
[ebook-reader](https://github.com/ttu-ttu/ebook-reader).

The report focuses on reading progress, reading time, and reading speed changes.
It is intentionally static: one Python command reads the JSON files and writes a
self-contained HTML report.

## Preview

![English report preview](assets/report-preview-en.png)

## Usage

```bash
uv run scripts/visualize_books.py
```

By default, this reads Hoshi Reader Mac data from:

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

## Publishing to GitHub Pages

You can opt in to publishing the generated report to a branch for GitHub Pages:

```bash
uv run scripts/visualize_books.py \
  --books-dir "$HOME/Library/Application Support/Books" \
  --publish-pages
```

By default this infers the profile name from the Books data directory name, then
pushes a static site branch named `reports/<profile>`. For the default path, the
branch is `reports/Books`.

The published branch contains only:

- `index.html`: a small landing page linking to the report
- `books_reading_report.html`

Publishing options:

- `--publish-pages`: enable the publish step after local report generation
- `--pages-remote`: git remote to push to, default `origin`
- `--pages-branch-prefix`: branch prefix, default `reports/`
- `--profile-name`: override the inferred profile name

One-time GitHub Pages setup:

1. Open the repository on GitHub.
2. Go to `Settings -> Pages`.
3. Set `Build and deployment` to `Deploy from a branch`.
4. Select the generated branch, for example `reports/Books`.
5. Select folder `/root`.

The report HTML contains your book titles, shelf names, and reading statistics.
If the repository is public, the published GitHub Pages site is public too.

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
