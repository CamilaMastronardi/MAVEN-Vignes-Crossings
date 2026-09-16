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

The Sun-State coordinate system is used because it is the appropriate starting
coordinate system for comparison with the Vignes et al. empirical boundaries.
The 4-degree solar-wind aberration used by Vignes is NOT applied in this file.
"""

import argparse
import gzip
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry


LASP_MAG_L2 = "https://lasp.colorado.edu/maven/sdc/public/data/sci/mag/l2"

DEFAULT_START_DATE = date(2014, 9, 21)
DEFAULT_OUTPUT_PATH = Path("data/positions")

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
    r'mvn_mag_l2_(?P<year>\d{4})(?P<doy>\d{3})ss1s_'
    r'(?P<date>\d{8})_v(?P<version>\d{2})_r(?P<revision>\d{2})\.sts'
)


def create_session():
    """
    Create a requests session with automatic retries.

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

    response = session.get(month_url, timeout=60)

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


def find_available_files(session, start_date, end_date):
    """
    Find the newest ss1s file for every available day in a date range.

    Parameters
    ----------
    session : requests.Session
        HTTP session.
    start_date : datetime.date
        First date to include.
    end_date : datetime.date
        Last date to include.

    Returns
    -------
    list
        List of tuples (date, filename), sorted chronologically.
    """
    available_files = []

    months = list(month_range(start_date, end_date))

    for year, month in tqdm(months, desc="Scanning LASP directories"):
        monthly_files = list_ss1s_files_for_month(session, year, month)

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
    directory = output_path / f"{file_date.year:04d}" / f"{file_date.month:02d}"

    return directory / f"maven_position_ss1s_{file_date:%Y%m%d}.csv.gz"


def extract_positions(session, file_date, filename, output_path, overwrite=False):
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
    str
        'downloaded', 'exists', or 'empty'.
    """
    destination = output_file_path(file_date, output_path)

    if destination.exists() and not overwrite:
        return "exists"

    destination.parent.mkdir(parents=True, exist_ok=True)

    file_url = (
        f"{LASP_MAG_L2}/"
        f"{file_date.year:04d}/"
        f"{file_date.month:02d}/"
        f"{filename}"
    )

    response = session.get(file_url, stream=True, timeout=(30, 180))
    response.raise_for_status()
    response.encoding = "utf-8"

    rows_written = 0
    temporary_file = destination.with_suffix(destination.suffix + ".part")

    try:
        with gzip.open(temporary_file, "wt", encoding="utf-8", newline="") as output:
            output.write("datetime_utc,x_ss_km,y_ss_km,z_ss_km\n")

            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue

                columns = line.split()

                if len(columns) <= POSITION_COLUMN_INDICES[-1]:
                    continue

                # Data records begin with year and day-of-year.
                if not columns[0].isdigit() or len(columns[0]) != 4:
                    continue

                try:
                    timestamp = build_datetime(columns)

                    x = float(columns[POSITION_COLUMN_INDICES[0]])
                    y = float(columns[POSITION_COLUMN_INDICES[1]])
                    z = float(columns[POSITION_COLUMN_INDICES[2]])

                except (ValueError, OverflowError):
                    continue

                output.write(
                    f"{timestamp.isoformat(timespec='milliseconds')},"
                    f"{x:.6f},{y:.6f},{z:.6f}\n"
                )

                rows_written += 1

        if rows_written == 0:
            temporary_file.unlink(missing_ok=True)
            return "empty"

        temporary_file.replace(destination)

    except Exception:
        temporary_file.unlink(missing_ok=True)
        raise

    return "downloaded"


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
        default=DEFAULT_START_DATE,
        help="First date to process (YYYY-MM-DD). Default: 2014-09-21.",
    )

    parser.add_argument(
        "--end",
        type=parse_date,
        default=date.today(),
        help="Last date to process (YYYY-MM-DD). Default: today.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Output directory. Default: data/positions.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite position files that already exist.",
    )

    args = parser.parse_args()

    if args.end < args.start:
        parser.error("--end must be equal to or later than --start.")

    session = create_session()

    print(
        f"Searching MAVEN MAG ss1s files from "
        f"{args.start} to {args.end}..."
    )

    files = find_available_files(session, args.start, args.end)

    if not files:
        print("No MAG ss1s files were found in the requested range.")
        return

    print(f"Found {len(files)} daily files.")

    downloaded = 0
    existing = 0
    empty = 0
    failed = []

    for file_date, filename in tqdm(files, desc="Reading MAVEN positions"):
        try:
            result = extract_positions(
                session=session,
                file_date=file_date,
                filename=filename,
                output_path=args.output,
                overwrite=args.overwrite,
            )

            if result == "downloaded":
                downloaded += 1
            elif result == "exists":
                existing += 1
            elif result == "empty":
                empty += 1

        except requests.RequestException as error:
            failed.append((file_date, filename, str(error)))

    print()
    print("Finished.")
    print(f"New files:       {downloaded}")
    print(f"Already present: {existing}")
    print(f"Empty files:     {empty}")
    print(f"Failed files:    {len(failed)}")

    if failed:
        print("\nFiles that failed:")
        for file_date, filename, error in failed:
            print(f"  {file_date}  {filename}")
            print(f"    {error}")


if __name__ == "__main__":
    main()
