# HABSIM
High Altitude Balloon Simulator

## Overview
This is an offshoot of the prediction server developed for the Stanford Space Initiative's Balloons team. It restores core functionality and introduces a simple UI that suits the current needs of the balloons team. 

## How It Works

1. **User Interface**: Web UI (`www/`) allows users to set launch parameters and visualize predictions
2. **API Server**: Flask app (`app.py`) receives requests and coordinates simulations
3. **Wind Data**: GEFS weather files from Supabase are cached locally (`gefs.py`)
4. **Simulation**: Physics engine (`simulate.py`) calculates balloon trajectory using wind data
5. **Results**: JSON trajectory data is returned to browser and rendered on Google Maps

## Files

### Core Application
- **`app.py`** - Flask web server exposing REST API endpoints for simulations
  - `/sim/singlezpb` - Single balloon prediction (ascent, coast, descent)
  - `/sim/spaceshot` - Ensemble prediction across multiple GEFS models
  - `/sim/elev` - Ground elevation lookup
  - Pre-warms cache on startup for faster first requests

### Simulation Engine
- **`simulate.py`** - Main simulation logic and orchestration
  - Coordinates wind data, elevation data, and balloon physics
  - Implements prediction caching (30 items, 1hr TTL) for performance
  - Math caching for coordinate transformations
  - Returns trajectory as list of timestamped lat/lon/altitude points

- **`windfile.py`** - GEFS wind data file parser and 4D interpolation
  - Loads `.npz` files containing wind vectors at pressure levels
  - Implements fast 4D interpolation (lat, lon, altitude, time)
  - Uses memory-mapped files for efficient large dataset handling

- **`classes.py`** - Balloon and trajectory data structures (legacy)

- **`habsim/classes1.py`** - Updated balloon physics classes
  - `Balloon` - Represents balloon state (position, altitude, ascent rate)
  - `Simulator` - Physics engine that steps balloon through time
  - `ElevationFile` - Ground elevation data wrapper

### Data Management
- **`gefs.py`** - GEFS weather file downloader and cache manager
  - Downloads files from Supabase Storage
  - LRU cache with 3 file limit (~450MB) to stay under 2GB RAM
  - Automatic cleanup of old files

- **`elev.py`** - Ground elevation data interface
  - Loads `worldelev.npy` (global elevation array)
  - Fast elevation lookup by lat/lon

- **`downloader.py`** - Script to fetch GEFS data from NOAA
  - Downloads GRIB2 files and converts to `.npz` format
  - Not used in production (data pre-downloaded to Supabase)

- **`save_elevation.py`** - One-time utility to convert elevation data to `.npy` format

### Frontend (www/)
- **`index.html`** - Single-page web application
  - Responsive design (desktop + mobile layouts)
  - Parameter inputs (launch time, location, ascent/descent rates)
  - Google Maps integration for visualization
  - Ensemble toggle for multi-model simulations

- **`paths.js`** - Trajectory fetching and map rendering
  - Fetches simulation results from API
  - Draws trajectory polylines on map
  - Manages waypoint circles and info windows

- **`style.js`** - UI mode switching logic (Standard/ZPB/Float modes)

- **`util.js`** - Map initialization, coordinate handling, elevation fetching

### Configuration
- **`gunicorn_config.py`** - Production server settings optimized for 2GB RAM / 1 CPU
  - 2 workers, 2 threads each (4 concurrent requests)
  - Preloads app to share memory between workers
  - Auto-recycles workers every 800 requests

- **`requirements.txt`** - Python dependencies
  - Flask, flask-cors, flask-compress
  - numpy, requests

- **`vercel.json`** - Legacy deployment config (not used on Render)

### Documentation
- **`OPTIMIZATIONS.md`** - Technical reference for performance optimizations
  - Explains caching strategies, memory budget, tuning parameters
  - Troubleshooting guide

- **`README.md`** - This file

### Data Directory
- **`data/gefs/`** - Cached GEFS weather files (`.npz` format)
  - `whichgefs` - Current model timestamp
  - `YYYYMMDDHH_NN.npz` - Wind data files (NN = model number 00-20)

- **`data/worldelev.npy`** - Global elevation data array

### Virtual Environment
- **`habsim/`** - Python virtual environment (`.venv`)
  - Contains all installed packages
  - Activate with `source habsim/bin/activate`
