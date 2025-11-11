#!/usr/bin/env python3
"""Quick script to check worldelev.npy dimensions"""
import numpy as np
from pathlib import Path

elev_path = Path('data/worldelev.npy')
if not elev_path.exists():
    print(f"File not found: {elev_path}")
    exit(1)

# Load just to check shape (memory-mapped, very fast)
data = np.load(elev_path, mmap_mode='r')
print(f"File: {elev_path}")
print(f"Shape: {data.shape}")
print(f"Data type: {data.dtype}")
print(f"File size: {elev_path.stat().st_size:,} bytes")

lat_dim, lon_dim = data.shape
print(f"\nLatitude dimension: {lat_dim:,}")
print(f"Longitude dimension: {lon_dim:,}")

# Calculate resolution
lat_resolution = lat_dim / 180
lon_resolution = lon_dim / 360

print(f"\nPoints per degree (latitude): {lat_resolution:.2f}")
print(f"Points per degree (longitude): {lon_resolution:.2f}")

# Convert to arc-seconds
lat_arcsec = 3600 / lat_resolution
lon_arcsec = 3600 / lon_resolution

print(f"\nResolution:")
print(f"  Latitude: {lat_arcsec:.1f} arc-seconds per point")
print(f"  Longitude: {lon_arcsec:.1f} arc-seconds per point")

# Check if it matches expected 120 points/degree (30 arc-seconds)
expected_res = 120
if abs(lat_resolution - expected_res) < 1 and abs(lon_resolution - expected_res) < 1:
    print(f"\n✓ Matches expected resolution: {expected_res} points/degree (30 arc-seconds)")
else:
    print(f"\n✗ Does NOT match expected {expected_res} points/degree")
    print(f"  Actual: ~{lat_resolution:.0f} points/degree ({lat_arcsec:.1f} arc-seconds)")

