# MAVEN Vignes Crossings

Python tools to identify geometric crossings of the Martian Magnetic Pile-up Boundary (MPB) using MAVEN spacecraft trajectories and the empirical conic model of Vignes et al. (2000).

## Overview

The workflow has two main steps:

1. Download MAVEN spacecraft positions from MAVEN MAG Level 2 `ss1s` files available at the LASP MAVEN Science Data Center.
2. Use those positions to identify crossings of the Vignes et al. (2000) MPB model.

The full MAG files are streamed from LASP, but only spacecraft time and position are stored locally.

## Project structure

```text
MAVEN-Vignes-Crossings/
├── README.md
├── requirements.txt
├── .gitignore
├── src/
│   ├── DownloadMAVENPosition.py
│   └── ComputeVignesCrossings.py
└── data/
    ├── positions/        # Local daily spacecraft positions; not tracked by Git
    ├── crossings/        # Crossing tables; tracked by Git
    ├── download.log      # Local download log; not tracked by Git
    └── download_state.json
```

## Installation

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the required packages:

```bash
pip install -r requirements.txt
```

## 1. Download MAVEN positions

Run:

```bash
python src/DownloadMAVENPosition.py
```

By default, the script searches the MAVEN MAG Level 2 archive from the beginning of the mission to the current date.

A specific interval can also be selected:

```bash
python src/DownloadMAVENPosition.py --start 2014-12-25 --end 2014-12-31
```

For each available day, the script reads the MAG Level 2 Sun-State 1-second (`ss1s`) file and stores only:

```text
datetime_utc
x_ss_km
y_ss_km
z_ss_km
```

The resulting files are compressed daily CSV files:

```text
data/positions/YYYY/MM/maven_position_ss1s_YYYYMMDD.csv.gz
```

### Resume and retry behavior

The downloader is designed for long runs.

It:

- skips daily files that were already completed;
- writes temporary `.part` files while a day is being processed;
- retries transient network errors;
- stores persistent progress in `data/download_state.json`;
- writes execution information and failures to `data/download.log`;
- can be restarted after interruption by running the same command again.

## 2. Vignes MPB model

The mean MPB model from Vignes et al. (2000) is represented by the conic

```text
r = L / (1 + epsilon cos(theta))
```

with the reported parameters

```text
X0      = 0.78 ± 0.01 RM
epsilon = 0.90 ± 0.01
L       = 0.96 ± 0.01 RM
```

where `RM` is the Mars radius.

The spacecraft positions are first converted from km to Mars radii and then rotated by 4 degrees around the Z axis to account for the solar-wind aberration used in the Vignes model.

### Inner and outer uncertainty envelopes

The reported uncertainties define two possible values for each of the three conic parameters:

```text
X0      = 0.77 or 0.79 RM
epsilon = 0.89 or 0.91
L       = 0.95 or 0.97 RM
```

This gives `2 × 2 × 2 = 8` parameter combinations.

The crossing code evaluates all eight conics and constructs:

- `inner`: the intersection of all eight conics;
- `outer`: the union of all eight conics.

These are conservative geometric envelopes derived from the published parameter uncertainties. They should not be interpreted as two distinct physical MPB surfaces explicitly proposed by Vignes et al.

## 3. Compute MPB crossings

Run:

```bash
python src/ComputeVignesCrossings.py
```

The script processes all available daily position files under `data/positions/`.

It reads the files one day at a time rather than loading the full mission into memory.

For each envelope it detects sign changes of the boundary function between consecutive spacecraft samples.

A crossing is classified as:

```text
inbound   outside -> inside
outbound  inside  -> outside
```

The crossing time and position are linearly interpolated between the two samples surrounding the boundary.

### Data-gap filter

A sign change is only accepted as a crossing when the two consecutive samples are separated by no more than 10 seconds by default.

For example:

```text
12:00:00   outside
12:00:01   inside
```

is accepted as a crossing.

However:

```text
12:00:00   outside
12:03:20   inside
```

contains a large data gap. Although the spacecraft may have crossed the modeled boundary somewhere inside that interval, the crossing time cannot be reliably determined from the available samples.

These cases are therefore not counted and are reported at the end as:

```text
sign changes ignored because of data gaps
```

The maximum allowed gap can be changed with:

```bash
python src/ComputeVignesCrossings.py --max-gap-seconds 20
```

## Crossing output

The default output is:

```text
data/crossings/vignes_mpb_extreme_crossings.csv
```

The output includes:

```text
envelope
crossing_number
direction
datetime_utc
x_ss_km
y_ss_km
z_ss_km
x_ab_rm
y_ab_rm
z_ab_rm
rho_ab_rm
active_x0_rm
active_epsilon
active_l_rm
sample_gap_s
interpolation_fraction
```

The `active_x0_rm`, `active_epsilon`, and `active_l_rm` columns indicate which of the eight parameter combinations locally defines the selected uncertainty envelope at the crossing.

## Interpretation

The crossings found by this repository are geometric intersections between the MAVEN trajectory and empirical MPB surfaces.

They are therefore candidate MPB encounters and are not, by themselves, confirmations of a physical MPB crossing in the plasma data.

A later validation step can compare these candidate times with MAVEN MAG, SWEA, SWIA, or other instrument observations.

## Data policy

Daily spacecraft position files can contain tens of millions of samples and are therefore not stored in the Git repository.

Only the much smaller derived crossing tables in `data/crossings/` are intended to be version controlled.

## Reference

Vignes, D., Mazelle, C., Rème, H., Acuña, M. H., Connerney, J. E. P., Lin, R. P., Mitchell, D. L., Cloutier, P., Crider, D. H., & Ness, N. F. (2000). *The solar wind interaction with Mars: Locations and shapes of the bow shock and the magnetic pile-up boundary from the observations of the MAG/ER Experiment onboard Mars Global Surveyor*. Geophysical Research Letters, 27(1), 49-52. https://doi.org/10.1029/1999GL010703
