#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Download MAVEN spacecraft positions from MAG Level 2 Sun-State 1-second files.

The script:
1. Scans the LASP MAG L2 monthly directories.
2. Finds the available ss1s .sts files.
3. Selects the newest version/revision for each day.
4. Streams each file without storing the complete MAG file.
5. Saves only time and spacecraft position (x, y, z) as compressed CSV files.
6. Stores persistent progress so an interrupted run can continue automatically.
7. Retries transient network errors and writes a detailed log.

The Sun-State coordinate system is used because it is the appropriate starting
coordinate system for comparison with the Vignes et al. empirical boundaries.
The 4-degree solar-wind aberration used by Vignes is NOT applied in this file.
"""

import argparse
import gzip
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry


LASP_MAG_L2 = "https://lasp.colorado.edu/maven/sdc/public/data/sci/mag/l2"

DEFAULT_START_DATE = date(2014, 9, 21)
DEFAULT_OUTPUT_PATH = Path("data/positions")
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_RETRY_WAIT = 5.0

# MAG L2 STS columns used here:
# 0: year
# 1: day of year
# 2: hour
# 3: minute
# 4: second
# 5: millisecond
# 11: spacecraft X position
# 12: spacecraft Y position
# 13: spacecraft Z position
POSITION_COLUMN_INDICES = (11, 12, 13)

SS1S_PATTERN = re.compile(
    r"mvn_mag_l2_(?P<year>\d{4})(?P<doy>\d{3})ss1s_"
    r"(?P<date>\d{8})_v(?P<version>\d{2})_r(?P<revision>\d{2})\.sts"
)


def utc_now_string():
    """
    Return the current UTC time as an ISO-8601 string.

    Returns
    -------
    str
        Current UTC timestamp.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_session():
    """
    Create a requests session with automatic low-level retries.

    Returns
    -------
    requests.Session
        Configured HTTP session.
    """
    session = requests.Session()

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)

    session.headers.update(
        {
            "User-Agent": (
                "MAVEN-Vignes-Crossings/1.0 "
                "(academic research; MAVEN MAG position retrieval)"
            )
        }
    )

    return session


def setup_logging(log_file):
    """
    Configure logging to terminal and file.

    Parameters
    ----------
    log_file : pathlib.Path
        Path to the log file.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def load_state(state_file):
    """
    Load persistent download state.

    Parameters
    ----------
    state_file : pathlib.Path
        JSON state file.

    Returns
    -------
    dict
        Existing state or an empty dictionary.
    """
    if not state_file.exists():
        return {}

    try:
        with open(state_file, "r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        logging.warning("Could not read state file %s: %s", state_file, error)
        return {}


def save_state(state_file, state):
    """
    Save state atomically.

    A temporary file is written first and then renamed, so an interruption
    during the write does not corrupt the previous state file.

    Parameters
    ----------
    state_file : pathlib.Path
        JSON state file.
    state : dict
        State to save.
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = state_file.with_suffix(state_file.suffix + ".tmp")

    state["updated_at_utc"] = utc_now_string()

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, sort_keys=True)

    temporary_file.replace(state_file)


def initialize_state(state, start_date, end_date):
    """
    Initialize or update persistent state for the current run.

    Parameters
    ----------
    state : dict
        Previously loaded state.
    start_date : datetime.date
        First requested date.
    end_date : datetime.date
        Last requested date.

    Returns
    -------
    dict
        Updated state.
    """
    state.setdefault("version", 1)
    state.setdefault("failed_days", {})

    state["requested_range"] = {
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
    }
    state["status"] = "running"
    state["last_run_started_at_utc"] = utc_now_string()

    return state


def recover_range_from_state(state):
    """
    Recover an unfinished date range from persistent state.

    Parameters
    ----------
    state : dict
        Loaded state.

    Returns
    -------
    tuple or None
        (start_date, end_date) when an unfinished run can be resumed.
    """
    unfinished_statuses = {
        "running",
        "interrupted",
        "finished_with_errors",
    }

    if state.get("status") not in unfinished_statuses:
        return None

    requested_range = state.get("requested_range", {})

    try:
        start_date = datetime.strptime(
            requested_range["start"], "%Y-%m-%d"
        ).date()
        end_date = datetime.strptime(
            requested_range["end"], "%Y-%m-%d"
        ).date()
    except (KeyError, ValueError):
        return None

    return start_date, end_date


def month_range(start_date, end_date):
    """
    Generate all year-month pairs between two dates.

    Parameters
    ----------
    start_date : datetime.date
        First date to include.
    end_date : datetime.date
        Last date to include.

    Yields
    ------
    tuple
        (year, month)
    """
    current = date(start_date.year, start_date.month, 1)
    last = date(end_date.year, end_date.month, 1)

    while current <= last:
        yield current.year, current.month

        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def list_ss1s_files_for_month(session, year, month):
    """
    Find all MAVEN MAG ss1s files available for one month.

    If several versions/revisions exist for a day, only the newest one
    is returned.

    Parameters
    ----------
    session : requests.Session
        HTTP session.
    year : int
        Year.
    month : int
        Month.

    Returns
    -------
    dict
        Dictionary with date objects as keys and filenames as values.
    """
    month_url = f"{LASP_MAG_L2}/{year:04d}/{month:02d}/"

    response = session.get(month_url, timeout=(30, 120))

    if response.status_code == 404:
        return {}

    response.raise_for_status()

    newest_files = {}

    for match in SS1S_PATTERN.finditer(response.text):
        file_date = datetime.strptime(match.group("date"), "%Y%m%d").date()
        version = int(match.group("version"))
        revision = int(match.group("revision"))
        filename = match.group(0)

        previous = newest_files.get(file_date)

        if previous is None or (version, revision) > previous[0]:
            newest_files[file_date] = ((version, revision), filename)

    return {
        file_date: value[1]
        for file_date, value in newest_files.items()
    }


def find_available_files(
    session,
    start_date,
    end_date,
    max_attempts,
    retry_wait,
):
    """
    Find the newest ss1s file for every available day in a date range.

    Monthly directory requests are retried explicitly in addition to the
    low-level retries configured in the requests session.

    Parameters
    ----------
    session : requests.Session
        HTTP session.
    start_date : datetime.date
        First date to include.
    end_date : datetime.date
        Last date to include.
    max_attempts : int
        Maximum attempts per monthly directory.
    retry_wait : float
        Initial wait in seconds between attempts.

    Returns
    -------
    list
        List of tuples (date, filename), sorted chronologically.
    """
    available_files = []
    months = list(month_range(start_date, end_date))

    for year, month in tqdm(
        months,
        desc="Scanning LASP directories",
        unit="month",
    ):
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                monthly_files = list_ss1s_files_for_month(
                    session,
                    year,
                    month,
                )
                break

            except requests.RequestException as error:
                last_error = error

                if attempt == max_attempts:
                    logging.error(
                        "Could not scan %04d-%02d after %d attempts: %s",
                        year,
                        month,
                        max_attempts,
                        error,
                    )
                    raise

                wait = retry_wait * (2 ** (attempt - 1))
                logging.warning(
                    "Error scanning %04d-%02d (attempt %d/%d): %s. "
                    "Retrying in %.1f s.",
                    year,
                    month,
                    attempt,
                    max_attempts,
                    error,
                    wait,
                )
                time.sleep(wait)
        else:
            raise last_error

        for file_date, filename in monthly_files.items():
            if start_date <= file_date <= end_date:
                available_files.append((file_date, filename))

    available_files.sort(key=lambda item: item[0])

    return available_files


def build_datetime(columns):
    """
    Build a datetime from the first six columns of a MAG STS record.

    Parameters
    ----------
    columns : list of str
        Split STS line.

    Returns
    -------
    datetime.datetime
        UTC timestamp.
    """
    year = int(columns[0])
    doy = int(columns[1])
    hour = int(columns[2])
    minute = int(columns[3])
    second = int(float(columns[4]))
    millisecond = int(float(columns[5]))

    timestamp = (
        datetime(year, 1, 1)
        + timedelta(
            days=doy - 1,
            hours=hour,
            minutes=minute,
            seconds=second,
            milliseconds=millisecond,
        )
    )

    return timestamp


def output_file_path(file_date, output_path):
    """
    Return the local output filename for one day.

    Parameters
    ----------
    file_date : datetime.date
        Date of the MAVEN data.
    output_path : pathlib.Path
        Base output directory.

    Returns
    -------
    pathlib.Path
        Output CSV.GZ path.
    """
    directory = (
        output_path
        / f"{file_date.year:04d}"
        / f"{file_date.month:02d}"
    )

    return directory / f"maven_position_ss1s_{file_date:%Y%m%d}.csv.gz"


def extract_positions(
    session,
    file_date,
    filename,
    output_path,
    overwrite=False,
):
    """
    Stream one MAG ss1s file and save only MAVEN position data.

    Parameters
    ----------
    session : requests.Session
        HTTP session.
    file_date : datetime.date
        Date corresponding to the MAG file.
    filename : str
        MAG ss1s filename.
    output_path : pathlib.Path
        Base output directory.
    overwrite : bool, optional
        Replace an existing output file. Default is False.

    Returns
    -------
    tuple
        (status, rows_written), where status is 'downloaded', 'exists',
        or 'empty'.
    """
    destination = output_file_path(file_date, output_path)

    if destination.exists() and not overwrite:
        return "exists", None

    destination.parent.mkdir(parents=True, exist_ok=True)

    file_url = (
        f"{LASP_MAG_L2}/"
        f"{file_date.year:04d}/"
        f"{file_date.month:02d}/"
        f"{filename}"
    )

    rows_written = 0
    temporary_file = destination.with_suffix(destination.suffix + ".part")

    # Remove a stale partial file left by a hard interruption.
    temporary_file.unlink(missing_ok=True)

    try:
        with session.get(
            file_url,
            stream=True,
            timeout=(30, 180),
        ) as response:
            response.raise_for_status()
            response.encoding = "utf-8"

            with gzip.open(
                temporary_file,
                "wt",
                encoding="utf-8",
                newline="",
            ) as output:
                output.write(
                    "datetime_utc,x_ss_km,y_ss_km,z_ss_km\n"
                )

                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue

                    columns = line.split()

                    if len(columns) <= POSITION_COLUMN_INDICES[-1]:
                        continue

                    if not columns[0].isdigit() or len(columns[0]) != 4:
                        continue

                    try:
                        timestamp = build_datetime(columns)

                        x = float(
                            columns[POSITION_COLUMN_INDICES[0]]
                        )
                        y = float(
                            columns[POSITION_COLUMN_INDICES[1]]
                        )
                        z = float(
                            columns[POSITION_COLUMN_INDICES[2]]
                        )

                    except (ValueError, OverflowError):
                        continue

                    output.write(
                        f"{timestamp.isoformat(timespec='milliseconds')},"
                        f"{x:.6f},{y:.6f},{z:.6f}\n"
                    )

                    rows_written += 1

        if rows_written == 0:
            temporary_file.unlink(missing_ok=True)
            return "empty", 0

        temporary_file.replace(destination)

    except BaseException:
        temporary_file.unlink(missing_ok=True)
        raise

    return "downloaded", rows_written


def download_day_with_retries(
    session,
    file_date,
    filename,
    output_path,
    overwrite,
    max_attempts,
    retry_wait,
):
    """
    Download one day with explicit retries for network errors.

    Parameters
    ----------
    session : requests.Session
        HTTP session.
    file_date : datetime.date
        Date to process.
    filename : str
        MAG filename.
    output_path : pathlib.Path
        Output directory.
    overwrite : bool
        Whether existing output should be replaced.
    max_attempts : int
        Maximum attempts for this day.
    retry_wait : float
        Initial wait in seconds.

    Returns
    -------
    tuple
        (status, rows_written, attempts_used)
    """
    for attempt in range(1, max_attempts + 1):
        try:
            status, rows = extract_positions(
                session=session,
                file_date=file_date,
                filename=filename,
                output_path=output_path,
                overwrite=overwrite,
            )

            return status, rows, attempt

        except requests.RequestException as error:
            if attempt == max_attempts:
                raise

            wait = retry_wait * (2 ** (attempt - 1))

            logging.warning(
                "%s failed on attempt %d/%d: %s. Retrying in %.1f s.",
                file_date,
                attempt,
                max_attempts,
                error,
                wait,
            )

            time.sleep(wait)

    raise RuntimeError("Unexpected retry loop exit.")


def parse_date(value):
    """
    Parse a YYYY-MM-DD command-line date.

    Parameters
    ----------
    value : str
        Date string.

    Returns
    -------
    datetime.date
        Parsed date.
    """
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"Invalid date '{value}'. Use YYYY-MM-DD."
        ) from error


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Extract MAVEN spacecraft positions from MAG L2 ss1s files "
            "available at the LASP MAVEN Science Data Center."
        )
    )

    parser.add_argument(
        "--start",
        type=parse_date,
        default=None,
        help=(
            "First date to process (YYYY-MM-DD). If omitted, an "
            "unfinished previous run is resumed; otherwise 2014-09-21."
        ),
    )

    parser.add_argument(
        "--end",
        type=parse_date,
        default=None,
        help=(
            "Last date to process (YYYY-MM-DD). If omitted, an unfinished "
            "previous run is resumed; otherwise today."
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Output directory. Default: data/positions.",
    )

    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help=(
            "Persistent JSON state file. "
            "Default: <output parent>/download_state.json."
        ),
    )

    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help=(
            "Log file. Default: <output parent>/download.log."
        ),
    )

    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help="Maximum attempts per request/day. Default: 4.",
    )

    parser.add_argument(
        "--retry-wait",
        type=float,
        default=DEFAULT_RETRY_WAIT,
        help=(
            "Initial wait between retries in seconds. "
            "The delay doubles after each failure. Default: 5."
        ),
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite position files that already exist.",
    )

    args = parser.parse_args()

    if args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1.")

    if args.retry_wait < 0:
        parser.error("--retry-wait must be non-negative.")

    state_file = (
        args.state_file
        if args.state_file is not None
        else args.output.parent / "download_state.json"
    )

    log_file = (
        args.log_file
        if args.log_file is not None
        else args.output.parent / "download.log"
    )

    setup_logging(log_file)
    state = load_state(state_file)

    recovered_range = recover_range_from_state(state)

    if args.start is None and args.end is None and recovered_range:
        start_date, end_date = recovered_range
        logging.info(
            "Resuming unfinished run: %s to %s.",
            start_date,
            end_date,
        )
    else:
        start_date = (
            args.start
            if args.start is not None
            else DEFAULT_START_DATE
        )
        end_date = (
            args.end
            if args.end is not None
            else date.today()
        )

    if end_date < start_date:
        parser.error("--end must be equal to or later than --start.")

    state = initialize_state(state, start_date, end_date)
    save_state(state_file, state)

    session = create_session()

    logging.info(
        "Searching MAVEN MAG ss1s files from %s to %s.",
        start_date,
        end_date,
    )
    logging.info("Output directory: %s", args.output)
    logging.info("State file: %s", state_file)
    logging.info("Log file: %s", log_file)

    try:
        files = find_available_files(
            session=session,
            start_date=start_date,
            end_date=end_date,
            max_attempts=args.max_attempts,
            retry_wait=args.retry_wait,
        )

        if not files:
            state["status"] = "finished"
            state["total_available_files"] = 0
            state["completed_files"] = 0
            state["failed_files"] = 0
            save_state(state_file, state)

            logging.info(
                "No MAG ss1s files were found in the requested range."
            )
            return

        existing_before = sum(
            output_file_path(file_date, args.output).exists()
            for file_date, _ in files
        )

        if args.overwrite:
            pending_files = files
            existing_before = 0
        else:
            pending_files = [
                item
                for item in files
                if not output_file_path(item[0], args.output).exists()
            ]

        state["total_available_files"] = len(files)
        state["completed_files"] = existing_before
        state["pending_files"] = len(pending_files)
        state["failed_files"] = len(state.get("failed_days", {}))
        save_state(state_file, state)

        logging.info("Found %d daily files.", len(files))
        logging.info(
            "%d already complete; %d pending.",
            existing_before,
            len(pending_files),
        )

        downloaded = 0
        empty = 0
        failed_this_run = 0

        progress = tqdm(
            total=len(files),
            initial=existing_before,
            desc="MAVEN positions",
            unit="day",
        )

        try:
            for file_date, filename in pending_files:
                date_key = file_date.isoformat()

                progress.set_postfix_str(date_key)

                state["current_date"] = date_key
                state["current_filename"] = filename
                save_state(state_file, state)

                logging.info(
                    "Processing %s (%s).",
                    file_date,
                    filename,
                )

                try:
                    status, rows, attempts = download_day_with_retries(
                        session=session,
                        file_date=file_date,
                        filename=filename,
                        output_path=args.output,
                        overwrite=args.overwrite,
                        max_attempts=args.max_attempts,
                        retry_wait=args.retry_wait,
                    )

                    if status == "downloaded":
                        downloaded += 1
                        logging.info(
                            "Completed %s: %d rows written in %d attempt(s).",
                            file_date,
                            rows,
                            attempts,
                        )

                        state["failed_days"].pop(date_key, None)

                    elif status == "exists":
                        logging.info(
                            "Skipped %s: output already exists.",
                            file_date,
                        )

                        state["failed_days"].pop(date_key, None)

                    elif status == "empty":
                        empty += 1
                        logging.warning(
                            "%s produced no valid position rows.",
                            file_date,
                        )

                        state["failed_days"][date_key] = {
                            "filename": filename,
                            "error": "No valid position rows were found.",
                            "updated_at_utc": utc_now_string(),
                        }

                except requests.RequestException as error:
                    failed_this_run += 1

                    logging.error(
                        "Failed %s after %d attempts: %s",
                        file_date,
                        args.max_attempts,
                        error,
                    )

                    state["failed_days"][date_key] = {
                        "filename": filename,
                        "error": str(error),
                        "updated_at_utc": utc_now_string(),
                    }

                completed_now = sum(
                    output_file_path(day, args.output).exists()
                    for day, _ in files
                )

                state["last_processed_date"] = date_key
                state["completed_files"] = completed_now
                state["pending_files"] = len(files) - completed_now
                state["failed_files"] = len(state["failed_days"])
                state.pop("current_date", None)
                state.pop("current_filename", None)
                save_state(state_file, state)

                progress.update(1)

        finally:
            progress.close()

        remaining_failed = len(state["failed_days"])

        state["status"] = (
            "finished_with_errors"
            if remaining_failed > 0
            else "finished"
        )
        state["completed_files"] = sum(
            output_file_path(day, args.output).exists()
            for day, _ in files
        )
        state["pending_files"] = (
            len(files) - state["completed_files"]
        )
        state["failed_files"] = remaining_failed
        state.pop("current_date", None)
        state.pop("current_filename", None)
        save_state(state_file, state)

        logging.info("Finished current run.")
        logging.info("New files: %d", downloaded)
        logging.info("Empty files: %d", empty)
        logging.info("Failures this run: %d", failed_this_run)
        logging.info(
            "Completed overall: %d/%d",
            state["completed_files"],
            state["total_available_files"],
        )

        if remaining_failed:
            logging.warning(
                "%d day(s) remain failed/pending. "
                "Run the same command again to retry them automatically.",
                remaining_failed,
            )

    except KeyboardInterrupt:
        state["status"] = "interrupted"
        state.pop("current_date", None)
        state.pop("current_filename", None)
        save_state(state_file, state)

        logging.warning(
            "Interrupted by user. Progress was saved. "
            "Run the script again to continue."
        )
        raise SystemExit(130)

    except Exception:
        state["status"] = "interrupted"
        save_state(state_file, state)
        logging.exception(
            "The run stopped unexpectedly. Progress was saved."
        )
        raise


if __name__ == "__main__":
    main()
