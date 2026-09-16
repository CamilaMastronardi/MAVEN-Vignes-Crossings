# MAVEN Vignes Crossings

Python tools to identify MAVEN crossings of the empirical boundaries described by Vignes et al. using MAVEN spacecraft position data from MAG Level 2 products.

## Project structure

- `src/`: Python scripts for downloading and processing MAVEN data.
- `data/raw/`: raw MAVEN data stored locally.
- `data/processed/`: processed trajectory and crossing data stored locally.

## Current objective

The first step is to retrieve MAVEN spacecraft position data from the MAG Level 2 archive. Later stages will use these trajectories to identify crossings of the Vignes empirical boundaries.
