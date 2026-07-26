# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SortPhotos is a Python 3.9+ CLI tool that sorts photos and videos into date-based directory hierarchies using EXIF metadata. It wraps a vendored copy of ExifTool (Perl) to extract timestamps from media files.

## Running

```bash
# Install in a virtual environment
python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"

# Run directly
python src/sortphotos.py <source_dir> <dest_dir>

# Run after install
sortphotos <source_dir> <dest_dir>

# Dry run (simulate without moving/copying)
sortphotos -t <source_dir> <dest_dir>
```

**Requires Perl** — ExifTool is a Perl application vendored at `src/Image-ExifTool/`.

## Testing

```bash
pytest              # run all tests
pytest tests/test_sortphotos.py::TestParseDateExif  # run one test class
pytest -k "test_basic_datetime"                      # run by name
```

Tests use `unittest.mock` to patch `ExifTool` for integration tests, avoiding the need for perl in CI. The ExifTool context manager test is skipped if perl is not available.

Because ExifTool is mocked, `pytest` cannot catch vendored-ExifTool regressions. Verify those directly: `perl src/Image-ExifTool/exiftool -ver`, a real extraction (`perl src/Image-ExifTool/exiftool -j -time:all <file>`), and an end-to-end dry run over a few files copied from `src/Image-ExifTool/t/images/`.

## Architecture

Single-file application (`src/sortphotos.py`) with these key components:

- **`ExifTool` class** — Context manager that keeps a persistent ExifTool subprocess open. Communicates via stdin/stdout with JSON output.
- **`parse_date_exif()`** — Parses EXIF date strings (`YYYY:MM:DD HH:MM:SS`) including timezone offsets into `datetime` objects.
- **`collect_candidate_tags()`** — Applies the ignore/use-only rules and returns only date-eligible tags. Note the GPS filter is a substring match (`'GPS' in key`), not a group match.
- **`get_oldest_timestamp()`** — Iterates candidate tags and returns the oldest valid date, its source tag(s), and the source file.
- **`check_for_early_morning_photos()`** — Shifts photos taken before `--day-begins` into the previous day for classification.
- **`log_file_decision()`** — Groups per-file details into one verbose log block.
- **`sortPhotos()`** — Core engine: extracts metadata, builds destination paths using `strftime`, handles duplicates via `filecmp.cmp`, collects file transfers, then executes them (optionally in parallel with `--jobs`). Returns a stats dict.
- **`_transfer_file()`** — Helper for moving/copying a single file with error handling. Used by both serial and parallel code paths.
- **`main()`** — CLI entry point with argparse. Configures logging levels.

**Data flow:** CLI args → ExifTool extracts all timestamps (JSON) → oldest date per file → destination path via `strftime(sort_format)` → collision/duplicate check → collect transfers → execute (serial or parallel) → print summary stats.

## Key Details

- Python 3.9+ required. Uses `pathlib`, type hints, f-strings throughout.
- Runtime dependency: `tqdm` (progress bar). Dev dependency: `pytest`.
- Uses `logging` module — levels controlled by `--verbose` (DEBUG), default (INFO), `--quiet`/`--silent` (WARNING).
- ExifTool path auto-resolved: `Path(__file__).resolve().parent / 'Image-ExifTool' / 'exiftool'`
- Default sort format: `%Y/%m-%b` (e.g., `2024/02-Feb/`)
- Forward slashes in `--sort` format create subdirectories
- Hidden files (dotfiles) are automatically skipped
- `ICC_Profile` group and `XMP:HistoryWhen` tag are always ignored for date extraction
- Any tag whose key contains `GPS` is always ignored (substring match — catches `Composite:GPSDateTime`, `EXIF:GPSDateStamp`, …)
- The `File` tag group is ignored by default (contains filesystem timestamps, not EXIF data)
- Subdirectories are **not** traversed unless `-r/--recursive` is given
- `--day-begins N` groups photos taken before hour N with the previous day
- Timestamps are compared as tz-naive datetimes normalized to UTC; `parse_date_exif(..., normalize_to_utc=False)` yields the local wall clock used for the folder name
- Duplicate detection compares both filename and file content via `filecmp.cmp`
- `--exclude` patterns use `fnmatch` for glob-style filtering
- `--jobs N` enables parallel file transfers via `ThreadPoolExecutor`
- Build config is in `pyproject.toml` (no setup.py)

## Upgrading vendored ExifTool

- Get the tarball from the GitHub tag (`https://github.com/exiftool/exiftool/archive/refs/tags/<ver>.tar.gz`) — `exiftool.org` hosts only the current release and 404s otherwise.
- Sync against the distribution `MANIFEST`, not the GitHub tree, which carries ~30 files the distribution omits (`LICENSE`, `validate`, `windows_exiftool`, html PDFs). New tag modules (e.g. `Garmin.pm` in 13.59) *are* in MANIFEST, so a bump is not always update-only — diff MANIFEST against `git ls-files` before assuming otherwise.
- Four MANIFEST fixtures (`t/images/EXE.so`, `Text.csv`, `Geotag_DJI_*.csv`, `LNK.lnk`) stay untracked because `*.so`/`*.csv`/`*.lnk` are gitignored. This gap predates 13.55 — it is the baseline, not a regression.

## Agent Notes

- Do not assume sorting also renames files. Filenames stay unchanged unless `--rename` is explicitly provided; collisions may still append numeric suffixes.
- Do not casually remove `File:*` timestamp fallback logic. In real usage, metadata-less Dropbox-synced, saved, or forwarded images may have no usable date other than filesystem timestamps.
- Be careful with “cleanup” around `--ignore-groups`. Passing an empty group list changes behavior materially because the default is to ignore the `File` group.
- Timezone invariant (do not break): the *oldest* tag is chosen by UTC-normalized instant, but the destination folder uses that tag's **local wall-clock** date. On equal UTC instants the **latest local date** wins, so a photographer's midnight photo lands on their own day. Both halves are covered by `test_mixed_offsets_keep_oldest_actual_instant_but_local_date` and `test_equal_instants_pick_latest_local_date_regardless_of_tag_order`; validate against real synced examples before changing.
