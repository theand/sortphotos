#!/usr/bin/env python3
"""
sortphotos.py

Created on 3/2/2013
Copyright (c) S. Andrew Ning. All rights reserved.

"""

from __future__ import annotations

import argparse
import concurrent.futures
import filecmp
import json
import locale
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

# Setting locale to the 'local' value
locale.setlocale(locale.LC_ALL, '')

logger = logging.getLogger('sortphotos')

exiftool_location: str = str(Path(__file__).resolve().parent / 'Image-ExifTool' / 'exiftool')


# -------- convenience methods -------------

def parse_date_exif(date_string: str, group: str, normalize_to_utc: bool = True) -> datetime | None:
    """
    extract date info from EXIF data
    YYYY:MM:DD HH:MM:SS
    or YYYY:MM:DD HH:MM:SS+HH:MM
    or YYYY:MM:DD HH:MM:SS-HH:MM
    or YYYY:MM:DD HH:MM:SSZ
    """

    # split into date and time
    elements = str(date_string).strip().split()  # ['YYYY:MM:DD', 'HH:MM:SS']

    if len(elements) < 1:
        return None

    # parse year, month, day
    date_entries = elements[0].split(':')  # ['YYYY', 'MM', 'DD']

    # check if three entries, nonzero data, and no decimal (which occurs for timestamps with only time but no date)
    if len(date_entries) == 3 and date_entries[0] > '0000' and '.' not in ''.join(date_entries):
        year = int(date_entries[0])
        month = int(date_entries[1])
        day = int(date_entries[2])
    else:
        return None

    # parse hour, min, second
    time_zone_adjust = False
    hour = 12  # defaulting to noon if no time data provided
    minute = 0
    second = 0
    dateadd = timedelta(0)
    if len(elements) > 1:
        time_entries = re.split(r'(\+|-|Z)', elements[1])  # ['HH:MM:SS', '+', 'HH:MM']
        time = time_entries[0].split(':')  # ['HH', 'MM', 'SS']

        if len(time) == 3:
            hour = int(time[0])
            minute = int(time[1])
            second = int(time[2].split('.')[0])
        elif len(time) == 2:
            hour = int(time[0])
            minute = int(time[1])

        # adjust for time-zone if needed
        if len(time_entries) > 2:
            time_zone = time_entries[2].split(':')  # ['HH', 'MM']

            if len(time_zone) == 2 and group != "File" and normalize_to_utc:
                time_zone_hour = int(time_zone[0])
                time_zone_min = int(time_zone[1])

                # check if + or -
                if time_entries[1] == '+':
                    time_zone_hour *= -1
                    time_zone_min *= -1

                dateadd = timedelta(hours=time_zone_hour, minutes=time_zone_min)
                time_zone_adjust = True


    # form date object
    try:
        date = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None  # errors in time format

    # try converting it (some "valid" dates are way before 1900 and cannot be parsed by strtime later)
    try:
        date.strftime('%Y/%m-%b')  # any format with year, month, day, would work here.
    except ValueError:
        return None  # errors in time format

    # adjust for time zone if necessary
    if time_zone_adjust:
        date += dateadd

    return date


def collect_candidate_tags(
    data: dict[str, Any],
    additional_groups_to_ignore: list[str],
    additional_tags_to_ignore: list[str],
) -> dict[str, str]:
    """Return metadata entries that are eligible for date extraction."""

    ignore_groups = ['ICC_Profile'] + additional_groups_to_ignore
    ignore_tags = ['SourceFile', 'XMP:HistoryWhen'] + additional_tags_to_ignore
    candidates: dict[str, str] = {}

    for key, value in data.items():
        if key in ignore_tags or key.split(':')[0] in ignore_groups or 'GPS' in key:
            continue

        if isinstance(value, list):
            if not value:
                continue
            value = value[0]

        candidates[key] = str(value)

    return candidates


def get_oldest_timestamp(
    data: dict[str, Any],
    additional_groups_to_ignore: list[str],
    additional_tags_to_ignore: list[str],
) -> tuple[str, datetime | None, list[str]]:
    """data as dictionary from json.  Should contain only time stamps except SourceFile"""

    # save only the oldest date
    date_available = False
    oldest_compare_date = datetime.now()
    oldest_local_date: datetime | None = None
    oldest_keys: list[str] = []

    # save src file
    src_file = data['SourceFile']

    candidate_tags = collect_candidate_tags(data, additional_groups_to_ignore, additional_tags_to_ignore)

    # run through all keys
    for key, date in candidate_tags.items():
        try:
            exifdate = parse_date_exif(date, key.split(':')[0])  # actual instant for comparison
            localdate = parse_date_exif(date, key.split(':')[0], normalize_to_utc=False)
        except Exception:
            exifdate = None
            localdate = None

        if exifdate and localdate and exifdate < oldest_compare_date:
            date_available = True
            oldest_compare_date = exifdate
            oldest_local_date = localdate
            oldest_keys = [key]

        elif exifdate and localdate and exifdate == oldest_compare_date:
            if oldest_local_date is None or localdate > oldest_local_date:
                oldest_local_date = localdate
            oldest_keys.append(key)

    if not date_available:
        oldest_local_date = None

    return src_file, oldest_local_date, oldest_keys


def check_for_early_morning_photos(date: datetime, day_begins: int) -> datetime:
    """check for early hour photos to be grouped with previous day"""

    if date.hour < day_begins:
        logger.debug(f'moving this photo to the previous day for classification purposes (day_begins={day_begins})')
        date = date - timedelta(hours=date.hour+1)  # push it to the day before for classification purposes

    return date


def log_file_decision(
    index: int,
    total: int,
    status: str,
    src_file: str,
    date: datetime | None = None,
    matched_keys: list[str] | None = None,
    candidate_tags: dict[str, str] | None = None,
    destination: str | None = None,
    notes: list[str] | None = None,
) -> None:
    """Emit a readable per-file debug log block."""

    if not logger.isEnabledFor(logging.DEBUG):
        return

    lines = [f'[{index}/{total}] {status}', f'  Source: {src_file}']

    if date is not None:
        lines.append(f'  Date/Time: {date}')

    if matched_keys:
        lines.append('  Tags used:')
        for key in matched_keys:
            value = candidate_tags.get(key) if candidate_tags else None
            if value is None:
                lines.append(f'    - {key}')
            else:
                lines.append(f'    - {key}: {value}')

    if destination is not None:
        lines.append(f'  Destination: {destination}')

    if notes:
        for note in notes:
            lines.append(f'  Note: {note}')

    logger.debug('\n'.join(lines))


def _transfer_file(
    src: str,
    dest: str,
    copy: bool,
) -> tuple[str, str, str | None]:
    """Move or copy a single file. Returns (src, dest, error_message_or_None)."""
    try:
        if copy:
            shutil.copy2(src, dest)
        else:
            shutil.move(src, dest)
        return (src, dest, None)
    except (PermissionError, OSError) as e:
        return (src, dest, str(e))


#  this class is based on code from Sven Marnach (http://stackoverflow.com/questions/10075115/call-exiftool-from-a-python-script)
class ExifTool:
    """used to run ExifTool from Python and keep it open"""

    sentinel: str = "{ready}"

    def __init__(self, executable: str = exiftool_location) -> None:
        self.executable = executable

    def __enter__(self) -> ExifTool:
        self.process = subprocess.Popen(
            ['perl', self.executable, "-stay_open", "True",  "-@", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        return self

    def __exit__(self, exc_type: type | None, exc_value: BaseException | None, traceback: Any) -> None:
        try:
            self.process.stdin.write(b'-stay_open\nFalse\n')
            self.process.stdin.flush()
            self.process.stdin.close()
            self.process.stdout.close()
            self.process.wait(timeout=30)
        except Exception:
            self.process.kill()
            self.process.wait()

    def execute(self, *args: str) -> str:
        args = args + ("-execute\n",)
        self.process.stdin.write(str.join("\n", args).encode('utf-8'))
        self.process.stdin.flush()
        output = ""
        fd = self.process.stdout.fileno()
        while not output.rstrip(' \t\n\r').endswith(self.sentinel):
            increment = os.read(fd, 4096)
            output += increment.decode('utf-8')
        return output.rstrip(' \t\n\r')[:-len(self.sentinel)]

    def get_metadata(self, *args: str) -> list[dict[str, Any]]:
        raw = self.execute(*args)
        if not raw.strip():
            return []
        try:
            return json.loads(raw)
        except ValueError as e:
            raise RuntimeError('No files to parse or invalid data') from e


# ---------------------------------------


def sortPhotos(
    src_dir: str,
    dest_dir: str,
    sort_format: str,
    rename_format: str | None,
    recursive: bool = False,
    copy_files: bool = False,
    test: bool = False,
    remove_duplicates: bool = True,
    day_begins: int = 0,
    additional_groups_to_ignore: list[str] | None = None,
    additional_tags_to_ignore: list[str] | None = None,
    use_only_groups: list[str] | None = None,
    use_only_tags: list[str] | None = None,
    keep_filename: bool = False,
    exclude_patterns: list[str] | None = None,
    jobs: int = 1,
) -> dict[str, int]:
    """
    This function is a convenience wrapper around ExifTool based on common usage scenarios for sortphotos.py

    Parameters
    ---------------
    src_dir : str
        directory containing files you want to process
    dest_dir : str
        directory where you want to move/copy the files to
    sort_format : str
        date format code for how you want your photos sorted
        (https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior)
    rename_format : str
        date format code for how you want your files renamed
        (https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior)
        None to not rename file
    recursive : bool
        True if you want src_dir to be searched recursively for files (False to search only in top-level of src_dir)
    copy_files : bool
        True if you want files to be copied over from src_dir to dest_dir rather than moved
    test : bool
        True if you just want to simulate how the files will be moved without actually doing any moving/copying
    remove_duplicates : bool
        True to remove files that are exactly the same in name and a file hash
    keep_filename : bool
        True to append original filename in case of duplicates instead of increasing number
    day_begins : int
        what hour of the day you want the day to begin (only for classification purposes).  Defaults at 0 as midnight.
        Can be used to group early morning photos with the previous day.  must be a number between 0-23
    additional_groups_to_ignore : list[str]
        tag groups that will be ignored when searching for file data.  By default File is ignored
    additional_tags_to_ignore : list[str]
        specific tags that will be ignored when searching for file data.
    use_only_groups : list[str]
        a list of groups that will be exclusively searched across for date info
    use_only_tags : list[str]
        a list of tags that will be exclusively searched across for date info
    exclude_patterns : list[str]
        glob patterns for files to exclude (e.g., ['*.raw', 'backup/*'])
    jobs : int
        number of parallel workers for file operations (default: 1 for serial)

    Returns
    -------
    dict[str, int]
        Statistics about the operation (files processed, skipped, errors, etc.)
    """

    if additional_groups_to_ignore is None:
        additional_groups_to_ignore = ['File']
    if additional_tags_to_ignore is None:
        additional_tags_to_ignore = []

    # some error checking
    if not Path(src_dir).exists():
        raise Exception('Source directory does not exist')

    # setup arguments to exiftool
    args = ['-j', '-a', '-G']

    # setup tags to ignore
    if use_only_tags is not None:
        additional_groups_to_ignore = []
        additional_tags_to_ignore = []
        for t in use_only_tags:
            args += [f'-{t}']

    elif use_only_groups is not None:
        additional_groups_to_ignore = []
        for g in use_only_groups:
            args += [f'-{g}:Time:All']

    else:
        args += ['-time:all']

    if recursive:
        args += ['-r']

    args += [src_dir]

    # statistics tracking
    stats: dict[str, int] = {
        'processed': 0,
        'skipped_no_date': 0,
        'skipped_hidden': 0,
        'skipped_duplicate': 0,
        'skipped_excluded': 0,
        'renamed_collision': 0,
        'errors': 0,
    }

    # get all metadata
    with ExifTool() as e:
        logger.info('Preprocessing with ExifTool.  May take a while for a large number of files.')
        sys.stdout.flush()
        metadata = e.get_metadata(*args)

    # setup output to screen
    num_files = len(metadata)

    if test:
        test_file_dict: dict[str, str] = {}

    # collect pending file transfers for parallel execution
    pending_transfers: list[tuple[str, str]] = []

    # determine if we should show progress bar
    show_progress = (
        not logger.isEnabledFor(logging.DEBUG)
        and logger.isEnabledFor(logging.INFO)
        and num_files > 0
    )
    try:
        from tqdm import tqdm
        progress = tqdm(total=num_files, disable=not show_progress, unit='file')
    except ImportError:
        progress = None

    # parse output extracting oldest relevant date
    for idx, data in enumerate(metadata, start=1):

        # extract timestamp date for photo
        src_file, date, keys = get_oldest_timestamp(data, additional_groups_to_ignore, additional_tags_to_ignore)
        candidate_tags = None
        if logger.isEnabledFor(logging.DEBUG):
            candidate_tags = collect_candidate_tags(data, additional_groups_to_ignore, additional_tags_to_ignore)

        # update progress bar
        if progress is not None:
            progress.update(1)

        # check for excluded patterns
        if exclude_patterns:
            src_path = Path(src_file)
            if any(fnmatch(src_path.name, pat) or fnmatch(str(src_path), pat) for pat in exclude_patterns):
                log_file_decision(idx, num_files, 'SKIP (excluded)', src_file, notes=['Matched an exclude pattern.'])
                stats['skipped_excluded'] += 1
                continue

        # check if no valid date found
        if not date:
            log_file_decision(idx, num_files, 'SKIP (no valid date)', src_file, notes=['No usable date metadata was found.'])
            stats['skipped_no_date'] += 1
            continue

        # ignore hidden files
        if Path(src_file).name.startswith('.'):
            log_file_decision(
                idx,
                num_files,
                'SKIP (hidden)',
                src_file,
                date=date,
                matched_keys=keys,
                candidate_tags=candidate_tags,
                notes=['Hidden files are ignored.'],
            )
            stats['skipped_hidden'] += 1
            continue

        original_date = date
        file_notes: list[str] = []

        # early morning photos can be grouped with previous day (depending on user setting)
        date = check_for_early_morning_photos(date, day_begins)
        if date != original_date:
            file_notes.append(f'Classified under previous day because --day-begins={day_begins}.')

        # create folder structure
        dir_structure = date.strftime(sort_format)
        dest_path = Path(dest_dir) / dir_structure
        if not test:
            try:
                dest_path.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                logger.error(f'Permission denied creating directory: {dest_path}')
                stats['errors'] += 1
                continue
            except OSError as e:
                logger.error(f'Error creating directory {dest_path}: {e}')
                stats['errors'] += 1
                continue

        # rename file if necessary
        filename = Path(src_file).name

        if rename_format is not None and date is not None:
            ext = Path(filename).suffix
            filename = date.strftime(rename_format) + ext.lower()

        # setup destination file
        dest_file = str(dest_path / filename)
        root, ext = os.path.splitext(dest_file)

        # check for collisions
        append = 1
        fileIsIdentical = False
        rename_count = 0

        while True:

            if (not test and Path(dest_file).is_file()) or (test and dest_file in test_file_dict):  # check for existing name
                if test:
                    dest_compare = test_file_dict[dest_file]
                else:
                    dest_compare = dest_file
                if remove_duplicates and filecmp.cmp(src_file, dest_compare):  # check for identical files
                    fileIsIdentical = True
                    stats['skipped_duplicate'] += 1
                    break

                else:  # name is same, but file is different
                    if keep_filename:
                        orig_filename = Path(src_file).stem
                        dest_file = f'{root}_{orig_filename}_{append}{ext}'
                    else:
                        dest_file = f'{root}_{append}{ext}'
                    append += 1
                    rename_count += 1
                    stats['renamed_collision'] += 1

            else:
                break

        if rename_count:
            suffix = 's' if rename_count != 1 else ''
            file_notes.append(f'Renamed to avoid {rename_count} collision{suffix}.')

        if fileIsIdentical:
            log_file_decision(
                idx,
                num_files,
                'SKIP (duplicate)',
                src_file,
                date=original_date,
                matched_keys=keys,
                candidate_tags=candidate_tags,
                destination=dest_file,
                notes=file_notes + ['Identical file already exists at the destination.'],
            )

        # finally move or copy the file
        if test:
            test_file_dict[dest_file] = src_file
            if not fileIsIdentical:
                stats['processed'] += 1
                action = 'PLAN COPY' if copy_files else 'PLAN MOVE'
                log_file_decision(
                    idx,
                    num_files,
                    action,
                    src_file,
                    date=original_date,
                    matched_keys=keys,
                    candidate_tags=candidate_tags,
                    destination=dest_file,
                    notes=file_notes,
                )

        else:

            if fileIsIdentical:
                continue  # ignore identical files
            else:
                pending_transfers.append((src_file, dest_file))
                stats['processed'] += 1
                action = 'QUEUED COPY' if copy_files else 'QUEUED MOVE'
                log_file_decision(
                    idx,
                    num_files,
                    action,
                    src_file,
                    date=original_date,
                    matched_keys=keys,
                    candidate_tags=candidate_tags,
                    destination=dest_file,
                    notes=file_notes,
                )

    if progress is not None:
        progress.close()

    # execute file transfers
    if not test and pending_transfers:
        if jobs > 1:
            logger.info(f'Transferring {len(pending_transfers)} files with {jobs} workers...')
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = {
                    pool.submit(_transfer_file, s, d, copy_files): (s, d)
                    for s, d in pending_transfers
                }
                for future in concurrent.futures.as_completed(futures):
                    src, dest, error = future.result()
                    if error:
                        logger.error(f'Error: {src} -> {dest}: {error}')
                        stats['errors'] += 1
                        stats['processed'] -= 1
        else:
            for src, dest in pending_transfers:
                src, dest, error = _transfer_file(src, dest, copy_files)
                if error:
                    logger.error(f'Error: {src} -> {dest}: {error}')
                    stats['errors'] += 1
                    stats['processed'] -= 1

    # print summary
    action = 'copy' if copy_files else 'move'
    prefix = 'Would ' if test else ''
    logger.info('')
    logger.info(f'--- {"Dry Run " if test else ""}Summary ---')
    logger.info(f'{prefix}{action}: {stats["processed"]} files')
    if stats['skipped_no_date']:
        logger.info(f'Skipped (no date): {stats["skipped_no_date"]}')
    if stats['skipped_hidden']:
        logger.info(f'Skipped (hidden): {stats["skipped_hidden"]}')
    if stats['skipped_duplicate']:
        logger.info(f'Skipped (duplicate): {stats["skipped_duplicate"]}')
    if stats['skipped_excluded']:
        logger.info(f'Skipped (excluded): {stats["skipped_excluded"]}')
    if stats['renamed_collision']:
        logger.info(f'Renamed (collision): {stats["renamed_collision"]}')
    if stats['errors']:
        logger.info(f'Errors: {stats["errors"]}')

    return stats



def main() -> None:

    # setup command line parsing
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter,
                                     description='Sort files (primarily photos and videos) into folders by date\nusing EXIF and other metadata')
    parser.add_argument('src_dir', type=str, help='source directory')
    parser.add_argument('dest_dir', type=str, help='destination directory')
    parser.add_argument('-r', '--recursive', action='store_true', help='search src_dir recursively')
    parser.add_argument('-c', '--copy', action='store_true', help='copy files instead of move')
    parser.add_argument('-s', '--silent', action='store_true', help='suppress all output except errors (alias for --quiet)')
    parser.add_argument('-t', '--test', action='store_true', help='run a test.  files will not be moved/copied\ninstead you will just a list of would happen')
    parser.add_argument('-v', '--verbose', action='store_true', help='show detailed file processing information')
    parser.add_argument('--quiet', action='store_true', help='suppress all output except errors')
    parser.add_argument('--sort', type=str, default='%Y/%m-%b',
                        help="choose destination folder structure using datetime format \n\
    https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior. \n\
    Use forward slashes / to indicate subdirectory(ies) (independent of your OS convention). \n\
    The default is '%%Y/%%m-%%b', which separates by year then month \n\
    with both the month number and name (e.g., 2012/02-Feb).")
    parser.add_argument('--rename', type=str, default=None,
                        help="rename file using format codes \n\
    https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior. \n\
    default is None which just uses original filename")
    parser.add_argument('--keep-filename', action='store_true',
                        help='In case of duplicated output filenames an increasing number and the original file name will be appended',
                        default=False)
    parser.add_argument('--keep-duplicates', action='store_true',
                        help='If file is a duplicate keep it anyway (after renaming).')
    parser.add_argument('--day-begins', type=int, default=0, help='hour of day that new day begins (0-23), \n\
    defaults to 0 which corresponds to midnight.  Useful for grouping pictures with previous day.')
    parser.add_argument('--ignore-groups', type=str, nargs='+',
                    default=[],
                    help='a list of tag groups that will be ignored for date informations.\n\
    list of groups and tags here: http://www.sno.phy.queensu.ca/~phil/exiftool/TagNames/\n\
    by default the group \'File\' is ignored which contains file timestamp data')
    parser.add_argument('--ignore-tags', type=str, nargs='+',
                    default=[],
                    help='a list of tags that will be ignored for date informations.\n\
    list of groups and tags here: http://www.sno.phy.queensu.ca/~phil/exiftool/TagNames/\n\
    the full tag name needs to be included (e.g., EXIF:CreateDate)')
    parser.add_argument('--use-only-groups', type=str, nargs='+',
                    default=None,
                    help='specify a restricted set of groups to search for date information\n\
    e.g., EXIF')
    parser.add_argument('--use-only-tags', type=str, nargs='+',
                    default=None,
                    help='specify a restricted set of tags to search for date information\n\
    e.g., EXIF:CreateDate')
    parser.add_argument('--exclude', type=str, nargs='+',
                    default=None,
                    help='glob patterns for files to exclude\n\
    e.g., --exclude "*.raw" "backup/*"')
    parser.add_argument('-j', '--jobs', type=int, default=1,
                    help='number of parallel workers for file operations (default: 1)')

    # parse command line arguments
    args = parser.parse_args()

    # configure logging
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format='%(message)s')
    elif args.silent or args.quiet:
        logging.basicConfig(level=logging.WARNING, format='%(message)s')
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')

    sortPhotos(args.src_dir, args.dest_dir, args.sort, args.rename, args.recursive,
        args.copy, args.test, not args.keep_duplicates, args.day_begins,
        args.ignore_groups, args.ignore_tags, args.use_only_groups,
        args.use_only_tags, args.keep_filename, args.exclude, args.jobs)


if __name__ == '__main__':
    main()
