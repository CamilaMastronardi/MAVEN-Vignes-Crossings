#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Find MAVEN crossings of the Vignes et al. (2000) empirical boundaries.

The script reads the daily compressed position files produced by
DownloadMAVENPosition.py and detects geometric crossings of the Vignes MPB,
bow shock, or both.

The Vignes boundaries are represented by axisymmetric conic sections:

    r = L / (1 + epsilon * cos(theta))

where r and theta are measured from a focus at (X0, 0, 0) in the 4-degree
aberrated MSO frame.

For crossing detection the equation is written as the signed function:

    F = sqrt((x - X0)^2 + y^2 + z^2)
        + epsilon * (x - X0) - L

The boundary is F = 0. Negative F is the inner side of the boundary and
positive F is the outer side.

Reference
---------
Vignes, D. et al. (2000), The solar wind interaction with Mars: Locations and
shapes of the bow shock and the magnetic pile-up boundary from observations
of the MAG/ER experiment onboard Mars Global Surveyor,
Geophysical Research Letters, 27(1), 49-52.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm


MARS_RADIUS_KM = 3390.0
DEFAULT_INPUT_PATH = Path("data/positions")
DEFAULT_OUTPUT_PATH = Path("data/crossings")
DEFAULT_ABERRATION_DEG = 4.0
DEFAULT_MAX_GAP_SECONDS = 10.0

REQUIRED_COLUMNS = (
    "datetime_utc",
    "x_ss_km",
    "y_ss_km",
    "z_ss_km",
)


@dataclass(frozen=True)
class VignesBoundary:
    """
    Parameters of one Vignes et al. empirical boundary.

    Parameters
    ----------
    name : str
        Boundary name.
    x0_rm : float
        X coordinate of the conic focus in Mars radii.
    epsilon : float
        Conic eccentricity.
    l_rm : float
        Semi-latus rectum in Mars radii.
    """

    name: str
    x0_rm: float
    epsilon: float
    l_rm: float


BOUNDARIES = {
    "mpb": VignesBoundary(
        name="mpb",
        x0_rm=0.78,
        epsilon=0.90,
        l_rm=0.96,
    ),
    "bow_shock": VignesBoundary(
        name="bow_shock",
        x0_rm=0.64,
        epsilon=1.03,
        l_rm=2.04,
    ),
}


def aberrate_coordinates(x_rm, y_rm, z_rm, angle_deg):
    """
    Rotate Sun-State/MSO coordinates into the aberrated Vignes frame.

    Positive angle_deg means that the +X' axis is rotated from +X toward -Y
    in the Sun-State/MSO frame. This follows the Vignes convention that +X'
    is opposite to the mean solar-wind flow in the Mars frame.

    Parameters
    ----------
    x_rm : array-like
        Sun-State X coordinate in Mars radii.
    y_rm : array-like
        Sun-State Y coordinate in Mars radii.
    z_rm : array-like
        Sun-State Z coordinate in Mars radii.
    angle_deg : float
        Aberration angle in degrees.

    Returns
    -------
    tuple of numpy.ndarray
        x', y', z' coordinates in Mars radii.
    """
    angle_rad = np.deg2rad(angle_deg)
    cos_a = np.cos(angle_rad)
    sin_a = np.sin(angle_rad)

    x_ab = x_rm * cos_a - y_rm * sin_a
    y_ab = x_rm * sin_a + y_rm * cos_a
    z_ab = z_rm

    return x_ab, y_ab, z_ab


def boundary_function(x_ab_rm, y_ab_rm, z_ab_rm, boundary):
    """
    Evaluate the signed Vignes conic function.

    Parameters
    ----------
    x_ab_rm : array-like
        Aberrated X coordinate in Mars radii.
    y_ab_rm : array-like
        Aberrated Y coordinate in Mars radii.
    z_ab_rm : array-like
        Aberrated Z coordinate in Mars radii.
    boundary : VignesBoundary
        Boundary parameters.

    Returns
    -------
    numpy.ndarray
        Signed value F. F < 0 is inside and F > 0 is outside.
    """
    dx = x_ab_rm - boundary.x0_rm
    r_focus = np.sqrt(dx**2 + y_ab_rm**2 + z_ab_rm**2)

    return r_focus + boundary.epsilon * dx - boundary.l_rm


def boundary_reference_points(boundary):
    """
    Calculate subsolar and terminator distances for a boundary.

    These values are useful as a consistency check against the values
    reported by Vignes et al. (2000).

    Parameters
    ----------
    boundary : VignesBoundary
        Boundary parameters.

    Returns
    -------
    tuple
        (subsolar_distance_rm, terminator_radius_rm)
    """
    subsolar = (
        boundary.x0_rm
        + boundary.l_rm / (1.0 + boundary.epsilon)
    )

    dx_terminator = -boundary.x0_rm
    r_focus = (
        boundary.l_rm
        - boundary.epsilon * dx_terminator
    )

    rho_squared = r_focus**2 - dx_terminator**2
    terminator = np.sqrt(max(rho_squared, 0.0))

    return subsolar, terminator


def find_position_files(input_path):
    """
    Find all daily MAVEN position files.

    Parameters
    ----------
    input_path : pathlib.Path
        Root directory containing daily CSV.GZ position files.

    Returns
    -------
    list of pathlib.Path
        Chronologically sorted position files.
    """
    files = list(
        input_path.rglob("maven_position_ss1s_*.csv.gz")
    )

    return sorted(files)


def read_position_file(file_path):
    """
    Read and validate one daily position file.

    Parameters
    ----------
    file_path : pathlib.Path
        CSV.GZ file to read.

    Returns
    -------
    pandas.DataFrame
        Valid, chronologically sorted position samples.
    """
    dataframe = pd.read_csv(
        file_path,
        usecols=list(REQUIRED_COLUMNS),
    )

    missing = [
        column
        for column in REQUIRED_COLUMNS
        if column not in dataframe.columns
    ]

    if missing:
        raise ValueError(
            f"{file_path} is missing columns: {missing}"
        )

    dataframe["datetime_utc"] = pd.to_datetime(
        dataframe["datetime_utc"],
        errors="coerce",
        utc=True,
    )

    for column in ("x_ss_km", "y_ss_km", "z_ss_km"):
        dataframe[column] = pd.to_numeric(
            dataframe[column],
            errors="coerce",
        )

    dataframe = dataframe.dropna(
        subset=list(REQUIRED_COLUMNS)
    )

    dataframe = dataframe.sort_values("datetime_utc")

    dataframe = dataframe.drop_duplicates(
        subset="datetime_utc",
        keep="first",
    )

    return dataframe.reset_index(drop=True)


def interpolate_crossing(
    time_1,
    time_2,
    position_1,
    position_2,
    value_1,
    value_2,
):
    """
    Linearly interpolate the boundary crossing between two samples.

    Parameters
    ----------
    time_1 : pandas.Timestamp
        Time of the first sample.
    time_2 : pandas.Timestamp
        Time of the second sample.
    position_1 : numpy.ndarray
        First position vector.
    position_2 : numpy.ndarray
        Second position vector.
    value_1 : float
        Boundary function at the first sample.
    value_2 : float
        Boundary function at the second sample.

    Returns
    -------
    tuple
        Crossing time, interpolated position, and interpolation fraction.
    """
    denominator = abs(value_1) + abs(value_2)

    if denominator == 0.0:
        fraction = 0.5
    else:
        fraction = abs(value_1) / denominator

    delta_time = time_2 - time_1
    crossing_time = time_1 + fraction * delta_time

    crossing_position = (
        position_1
        + fraction * (position_2 - position_1)
    )

    return crossing_time, crossing_position, fraction


def classify_direction(value_before, value_after):
    """
    Classify a crossing as inbound or outbound.

    Parameters
    ----------
    value_before : float
        Boundary function before the crossing.
    value_after : float
        Boundary function after the crossing.

    Returns
    -------
    str
        'inbound' for outside-to-inside and 'outbound' for
        inside-to-outside.
    """
    if value_before > 0.0 and value_after < 0.0:
        return "inbound"

    if value_before < 0.0 and value_after > 0.0:
        return "outbound"

    return "undetermined"


def find_crossings_in_samples(
    dataframe,
    boundary,
    aberration_deg,
    max_gap_seconds,
):
    """
    Find all crossings of one Vignes boundary in consecutive samples.

    Parameters
    ----------
    dataframe : pandas.DataFrame
        Position samples, optionally including the final sample from the
        previous daily file.
    boundary : VignesBoundary
        Boundary parameters.
    aberration_deg : float
        Aberration angle in degrees.
    max_gap_seconds : float
        Maximum allowed time gap between samples for a crossing.

    Returns
    -------
    tuple
        List of crossing dictionaries and number of sign changes ignored
        because the time gap was too large.
    """
    if len(dataframe) < 2:
        return [], 0

    times = dataframe["datetime_utc"].to_numpy()

    positions_ss_km = dataframe[
        ["x_ss_km", "y_ss_km", "z_ss_km"]
    ].to_numpy(dtype=float)

    positions_ss_rm = positions_ss_km / MARS_RADIUS_KM

    x_ab, y_ab, z_ab = aberrate_coordinates(
        positions_ss_rm[:, 0],
        positions_ss_rm[:, 1],
        positions_ss_rm[:, 2],
        aberration_deg,
    )

    positions_ab_rm = np.column_stack(
        (x_ab, y_ab, z_ab)
    )

    values = boundary_function(
        x_ab,
        y_ab,
        z_ab,
        boundary,
    )

    finite = np.isfinite(values)

    sign_change = (
        finite[:-1]
        & finite[1:]
        & (
            np.signbit(values[:-1])
            != np.signbit(values[1:])
        )
    )

    candidate_indices = np.flatnonzero(sign_change)

    crossings = []
    skipped_gaps = 0

    for index in candidate_indices:
        time_1 = pd.Timestamp(times[index])
        time_2 = pd.Timestamp(times[index + 1])

        gap_seconds = (
            time_2 - time_1
        ).total_seconds()

        if gap_seconds <= 0.0:
            continue

        if gap_seconds > max_gap_seconds:
            skipped_gaps += 1
            continue

        value_1 = float(values[index])
        value_2 = float(values[index + 1])

        crossing_time, crossing_ss_km, fraction = (
            interpolate_crossing(
                time_1=time_1,
                time_2=time_2,
                position_1=positions_ss_km[index],
                position_2=positions_ss_km[index + 1],
                value_1=value_1,
                value_2=value_2,
            )
        )

        crossing_ss_rm = crossing_ss_km / MARS_RADIUS_KM

        crossing_ab_rm = np.array(
            aberrate_coordinates(
                crossing_ss_rm[0],
                crossing_ss_rm[1],
                crossing_ss_rm[2],
                aberration_deg,
            ),
            dtype=float,
        )

        rho_ab_rm = np.sqrt(
            crossing_ab_rm[1] ** 2
            + crossing_ab_rm[2] ** 2
        )

        crossings.append(
            {
                "boundary": boundary.name,
                "direction": classify_direction(
                    value_1,
                    value_2,
                ),
                "datetime_utc": crossing_time.isoformat(),
                "x_ss_km": crossing_ss_km[0],
                "y_ss_km": crossing_ss_km[1],
                "z_ss_km": crossing_ss_km[2],
                "x_ab_rm": crossing_ab_rm[0],
                "y_ab_rm": crossing_ab_rm[1],
                "z_ab_rm": crossing_ab_rm[2],
                "rho_ab_rm": rho_ab_rm,
                "vignes_value_before": value_1,
                "vignes_value_after": value_2,
                "sample_gap_s": gap_seconds,
                "interpolation_fraction": fraction,
            }
        )

    return crossings, skipped_gaps


def select_boundaries(boundary_option):
    """
    Convert the command-line boundary option to boundary objects.

    Parameters
    ----------
    boundary_option : str
        'mpb', 'bow_shock', or 'both'.

    Returns
    -------
    list of VignesBoundary
        Boundaries to evaluate.
    """
    if boundary_option == "both":
        return [
            BOUNDARIES["mpb"],
            BOUNDARIES["bow_shock"],
        ]

    return [BOUNDARIES[boundary_option]]


def output_filename(boundary_option):
    """
    Return the default output filename.

    Parameters
    ----------
    boundary_option : str
        Boundary selection.

    Returns
    -------
    str
        CSV filename.
    """
    if boundary_option == "both":
        return "vignes_crossings.csv"

    return f"vignes_{boundary_option}_crossings.csv"


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Find MAVEN geometric crossings of the Vignes et al. "
            "(2000) empirical MPB and bow-shock surfaces."
        )
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT_PATH,
        help=(
            "Directory containing daily MAVEN position CSV.GZ files. "
            "Default: data/positions."
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Output CSV file. By default it is written under "
            "data/crossings/."
        ),
    )

    parser.add_argument(
        "--boundary",
        choices=("mpb", "bow_shock", "both"),
        default="mpb",
        help="Boundary to evaluate. Default: mpb.",
    )

    parser.add_argument(
        "--aberration-deg",
        type=float,
        default=DEFAULT_ABERRATION_DEG,
        help=(
            "Solar-wind aberration angle in degrees. "
            "Default: 4.0."
        ),
    )

    parser.add_argument(
        "--max-gap-seconds",
        type=float,
        default=DEFAULT_MAX_GAP_SECONDS,
        help=(
            "Do not count a sign change as a crossing when the two "
            "samples are separated by more than this value. "
            "Default: 10 s."
        ),
    )

    args = parser.parse_args()

    if args.max_gap_seconds <= 0.0:
        parser.error("--max-gap-seconds must be positive.")

    if args.output is None:
        args.output = (
            DEFAULT_OUTPUT_PATH
            / output_filename(args.boundary)
        )

    files = find_position_files(args.input)

    if not files:
        raise SystemExit(
            f"No MAVEN position files were found under {args.input}."
        )

    selected_boundaries = select_boundaries(
        args.boundary
    )

    print(f"Found {len(files)} daily position files.")
    print(
        f"Mars radius: {MARS_RADIUS_KM:.1f} km"
    )
    print(
        f"Aberration angle: {args.aberration_deg:.2f} deg"
    )

    for boundary in selected_boundaries:
        subsolar, terminator = boundary_reference_points(
            boundary
        )

        print(
            f"{boundary.name}: "
            f"X0={boundary.x0_rm:.2f} RM, "
            f"epsilon={boundary.epsilon:.2f}, "
            f"L={boundary.l_rm:.2f} RM, "
            f"subsolar={subsolar:.3f} RM, "
            f"terminator={terminator:.3f} RM"
        )

    all_crossings = []
    previous_sample = None
    total_samples = 0
    skipped_gap_crossings = {
        boundary.name: 0
        for boundary in selected_boundaries
    }

    for file_path in tqdm(
        files,
        desc="Finding Vignes crossings",
        unit="day",
    ):
        try:
            dataframe = read_position_file(file_path)
        except (
            ValueError,
            OSError,
            pd.errors.ParserError,
        ) as error:
            tqdm.write(
                f"Skipping {file_path}: {error}"
            )
            previous_sample = None
            continue

        if dataframe.empty:
            previous_sample = None
            continue

        total_samples += len(dataframe)

        if previous_sample is not None:
            first_time = dataframe["datetime_utc"].iloc[0]
            previous_time = previous_sample[
                "datetime_utc"
            ].iloc[0]

            if previous_time < first_time:
                dataframe_for_crossings = pd.concat(
                    [previous_sample, dataframe],
                    ignore_index=True,
                )
            else:
                dataframe_for_crossings = dataframe
        else:
            dataframe_for_crossings = dataframe

        for boundary in selected_boundaries:
            crossings, skipped_gaps = (
                find_crossings_in_samples(
                    dataframe=dataframe_for_crossings,
                    boundary=boundary,
                    aberration_deg=args.aberration_deg,
                    max_gap_seconds=args.max_gap_seconds,
                )
            )

            all_crossings.extend(crossings)
            skipped_gap_crossings[
                boundary.name
            ] += skipped_gaps

        previous_sample = dataframe.tail(1).copy()

    if all_crossings:
        crossings_df = pd.DataFrame(all_crossings)

        crossings_df["datetime_utc"] = pd.to_datetime(
            crossings_df["datetime_utc"],
            utc=True,
        )

        crossings_df = crossings_df.sort_values(
            ["datetime_utc", "boundary"]
        ).reset_index(drop=True)

        crossings_df.insert(
            1,
            "crossing_number",
            crossings_df.groupby(
                "boundary"
            ).cumcount() + 1,
        )

    else:
        crossings_df = pd.DataFrame(
            columns=[
                "boundary",
                "crossing_number",
                "direction",
                "datetime_utc",
                "x_ss_km",
                "y_ss_km",
                "z_ss_km",
                "x_ab_rm",
                "y_ab_rm",
                "z_ab_rm",
                "rho_ab_rm",
                "vignes_value_before",
                "vignes_value_after",
                "sample_gap_s",
                "interpolation_fraction",
            ]
        )

    args.output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    crossings_df.to_csv(
        args.output,
        index=False,
    )

    print()
    print("Finished.")
    print(f"Position samples read: {total_samples:,}")
    print(f"Output: {args.output}")

    for boundary in selected_boundaries:
        boundary_crossings = crossings_df[
            crossings_df["boundary"]
            == boundary.name
        ]

        inbound = (
            boundary_crossings["direction"]
            == "inbound"
        ).sum()

        outbound = (
            boundary_crossings["direction"]
            == "outbound"
        ).sum()

        print()
        print(f"{boundary.name}:")
        print(
            f"  crossings: {len(boundary_crossings):,}"
        )
        print(f"  inbound:   {inbound:,}")
        print(f"  outbound:  {outbound:,}")
        print(
            "  sign changes ignored because of "
            f"data gaps: "
            f"{skipped_gap_crossings[boundary.name]:,}"
        )


if __name__ == "__main__":
    main()
