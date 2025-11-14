#!/usr/bin/env python3
"""
Automated GEFS downloader daemon.
Downloads GEFS models every 6 hours and uploads to S3.
Number of models is controlled by downloader.DOWNLOAD_CONTROL and downloader.NUM_PERTURBED_MEMBERS.

Modes:
- Single run: Downloads next available model and exits (for scheduled jobs like GitHub Actions)
- Daemon mode: Continuously monitors and downloads new models (for long-running processes)
"""
import sys
import time
import logging
import argparse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path to import root modules
sys.path.insert(0, str(Path(__file__).parent.parent))

import gefs
import downloader

# Constants
CHECK_INTERVAL_DEFAULT = 300  # 5 minutes
EXTENDED_RANGE_HOUR = 384  # Forecast hour to check for extended range availability
URL_TIMEOUT = 10

# Setup argument parser
parser = argparse.ArgumentParser(description="Automated GEFS downloader daemon")
parser.add_argument("--logfile", default=None, help="Log file path (default: stdout)")
parser.add_argument("--savedir", default="/Applications/Emmanuel Zheng/habsim/data/gefs", 
                    help="Local directory for downloads")
parser.add_argument("--statusfile", default=None, 
                    help="File to write current model timestamp (default: {savedir}/whichgefs)")
parser.add_argument("--check-interval", type=int, default=CHECK_INTERVAL_DEFAULT,
                    help=f"Seconds between checks for new data (default: {CHECK_INTERVAL_DEFAULT})")
parser.add_argument("--daemon", action="store_true",
                    help="Daemon mode: continuously monitor and download (default: single run)")
args = parser.parse_args()

# Setup logging
logger = logging.getLogger(__name__)
logging.basicConfig(
    filename=args.logfile,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

if not args.logfile:
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logger.addHandler(console)

def fmt_timestamp(dt: datetime) -> str:
    """Format datetime as YYYYMMDDHH"""
    return dt.strftime("%Y%m%d%H")

def get_statusfile_path() -> Path:
    """Get status file path (defaults to savedir/whichgefs)"""
    if args.statusfile:
        return Path(args.statusfile)
    return Path(args.savedir) / "whichgefs"

def get_current_model() -> datetime | None:
    """Read current model timestamp from status file"""
    statusfile_path = get_statusfile_path()
    if not statusfile_path.exists():
        return None
    try:
        with open(statusfile_path) as f:
            return datetime.strptime(f.read().strip(), "%Y%m%d%H")
    except Exception:
        return None

def get_model_ids():
    """Get list of model IDs to download based on downloader settings"""
    model_ids = []
    if downloader.DOWNLOAD_CONTROL:
        model_ids.append(0)
    model_ids.extend(range(1, 1 + downloader.NUM_PERTURBED_MEMBERS))
    return model_ids

def check_data_available(timestamp: datetime, check_extended: bool = False) -> bool:
    """Check if data is available for given timestamp.
    
    Args:
        timestamp: Model timestamp to check
        check_extended: If True, also check for extended range forecast hour (f384)
    """
    y, m, d, h = timestamp.year, timestamp.month, timestamp.day, timestamp.hour
    m, d, h = map(lambda x: str(x).zfill(2), [m, d, h])
    
    # Check forecast hour 0 (basic availability)
    url_f000 = f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/gefs.{y}{m}{d}/{h}/atmos/pgrb2bp5/gep01.t{h}z.pgrb2b.0p50.f000"
    try:
        resp = urllib.request.urlopen(url_f000, timeout=URL_TIMEOUT)
        if resp.status != 200:
            return False
    except Exception:
        return False
    
    # If checking extended range, verify forecast hour 384 is available
    if check_extended:
        url_f384 = f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/gefs.{y}{m}{d}/{h}/atmos/pgrb2bp5/gep01.t{h}z.pgrb2b.0p50.f{EXTENDED_RANGE_HOUR}"
        try:
            resp = urllib.request.urlopen(url_f384, timeout=URL_TIMEOUT)
            return resp.status == 200
        except Exception:
            return False
    
    return True

def get_most_recent_available() -> datetime:
    """Get most recent 6-hourly model run time that has extended range available.
    Prefers model from 6 hours ago to get more recent data.
    """
    now = datetime.utcnow()
    hour = (now.hour // 6) * 6
    model_6h = datetime(now.year, now.month, now.day, hour) - timedelta(hours=6)
    model_12h = datetime(now.year, now.month, now.day, hour) - timedelta(hours=12)
    
    # Prefer 6h model if extended range is available, otherwise fall back to 12h
    if check_data_available(model_6h, check_extended=True):
        return model_6h
    elif check_data_available(model_12h, check_extended=True):
        return model_12h
    else:
        return model_6h  # Default to 6h even if extended range isn't ready

def get_next_model_to_download() -> datetime | None:
    """Determine the next model timestamp that should be downloaded.
    
    Returns:
        Next model timestamp to download, or None if no model should be downloaded yet.
    """
    current = get_current_model()
    now = datetime.utcnow()
    
    if current:
        # Calculate next model (6 hours after current)
        next_run = current + timedelta(hours=6)
        
        # Skip if too old (more than 6 hours in the past)
        if next_run < now - timedelta(hours=6):
            logger.warning(f"Next run {fmt_timestamp(next_run)} is too old, using most recent available")
            return get_most_recent_available()
        
        return next_run
    else:
        # No current model - use most recent available
        return get_most_recent_available()

def upload_model_files(timestamp: datetime, savedir: Path) -> int:
    """Upload model files to S3. Returns count of successful uploads."""
    timestamp_str = fmt_timestamp(timestamp)
    model_ids = get_model_ids()
    success_count = 0
    
    for model_id in model_ids:
        filename = f"{timestamp_str}_{str(model_id).zfill(2)}.npz"
        filepath = savedir / filename
        
        if not filepath.exists():
            logger.warning(f"File not found: {filepath}")
            continue
        
        if gefs.upload_gefs(filepath, filename):
            logger.info(f"Uploaded {filename}")
            success_count += 1
            filepath.unlink()  # Remove local file after successful upload
        else:
            logger.error(f"Failed to upload {filename}")
    
    return success_count

def cleanup_old_files(timestamp: datetime, old_model: datetime | None):
    """Clean up old model files from S3"""
    timestamp_str = fmt_timestamp(timestamp)
    model_ids = get_model_ids()
    current_model_files = {f"{timestamp_str}_{str(mid).zfill(2)}.npz" for mid in model_ids}
    
    # Delete previous model files if known
    if old_model and old_model != timestamp:
        old_timestamp_str = fmt_timestamp(old_model)
        logger.info(f"Cleaning up old model files: {old_timestamp_str}")
        for model_id in model_ids:
            old_filename = f"{old_timestamp_str}_{str(model_id).zfill(2)}.npz"
            if gefs.delete_gefs(old_filename):
                logger.info(f"Deleted old file: {old_filename}")
            else:
                logger.warning(f"Failed to delete old file: {old_filename}")
    
    # Clean up orphaned files
    try:
        all_files = gefs.listdir_gefs()
        orphaned_files = [f for f in all_files if f.endswith('.npz') and f not in current_model_files]
        if orphaned_files:
            logger.info(f"Found {len(orphaned_files)} orphaned .npz file(s) to clean up")
            for orphaned_file in orphaned_files:
                if gefs.delete_gefs(orphaned_file):
                    logger.info(f"Deleted orphaned file: {orphaned_file}")
                else:
                    logger.warning(f"Failed to delete orphaned file: {orphaned_file}")
    except Exception as e:
        logger.warning(f"Failed to list/clean orphaned files: {e}")

def update_status_file(timestamp: datetime):
    """Update local and S3 status files with new timestamp"""
    timestamp_str = fmt_timestamp(timestamp)
    statusfile_path = get_statusfile_path()
    
    # Write local status file
    statusfile_path.parent.mkdir(parents=True, exist_ok=True)
    with open(statusfile_path, 'w') as f:
        f.write(timestamp_str)
    logger.info(f"Updated local whichgefs to {timestamp_str}")
    
    # Upload to S3
    try:
        if gefs.upload_gefs(statusfile_path, "whichgefs"):
            logger.info(f"Uploaded whichgefs to S3: {timestamp_str}")
        else:
            logger.warning("Failed to upload whichgefs to S3")
    except Exception as e:
        logger.warning(f"Failed to upload whichgefs to S3: {e}")

def download_and_upload_model(timestamp: datetime) -> bool:
    """Download model using downloader.py and upload to S3"""
    timestamp_str = fmt_timestamp(timestamp)
    logger.info(f"Starting download for {timestamp_str}")
    
    savedir = Path(args.savedir)
    savedir.mkdir(parents=True, exist_ok=True)
    
    # Download model
    try:
        model_timestamp = datetime.strptime(timestamp_str, "%Y%m%d%H")
        downloader.logger = logging.getLogger('downloader')
        downloader.logger.setLevel(logging.INFO)
        if not downloader.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
            downloader.logger.addHandler(handler)
        
        downloader.complete_run(model_timestamp, timestamp_str=timestamp_str, savedir=str(savedir))
        logger.info(f"Downloader completed successfully for {timestamp_str}")
    except Exception as e:
        logger.error(f"Downloader failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False
    
    # Upload model files
    logger.info("Uploading files to S3...")
    model_ids = get_model_ids()
    success_count = upload_model_files(timestamp, savedir)
    
    if success_count != len(model_ids):
        logger.error(f"Only {success_count}/{len(model_ids)} files uploaded successfully")
        return False
    
    # Update status file and clean up
    old_model = get_current_model()
    update_status_file(timestamp)
    cleanup_old_files(timestamp, old_model)
    
    logger.info(f"Successfully completed download and upload for {timestamp_str}")
    return True

def run_single_cycle() -> int:
    """Run a single download cycle: check for next model and download if available.
    
    Returns:
        0 on success, 1 on failure or if no model available
    """
    logger.info("Running single download cycle")
    
    # Determine next model to download
    next_model = get_next_model_to_download()
    if not next_model:
        logger.warning("No model available to download")
        return 1
    
    logger.info(f"Next model to check: {fmt_timestamp(next_model)}")
    
    # Check if data is available (check extended range if model is recent)
    now = datetime.utcnow()
    is_recent = (now - next_model).total_seconds() < 6 * 3600
    if not check_data_available(next_model, check_extended=is_recent):
        logger.info(f"Data not yet available for {fmt_timestamp(next_model)}")
        return 1
    
    # Download and upload
    logger.info(f"Data available for {fmt_timestamp(next_model)}, starting download...")
    success = download_and_upload_model(next_model)
    
    if success:
        logger.info("Download cycle completed successfully")
        return 0
    else:
        logger.error("Download cycle failed")
        return 1

def run_daemon_mode():
    """Run in daemon mode: continuously monitor and download new models"""
    logger.info("Starting GEFS auto-downloader daemon")
    
    # Initialize next run time
    current = get_current_model()
    if current:
        next_run = current + timedelta(hours=6)
        logger.info(f"Current model: {fmt_timestamp(current)}, next run: {fmt_timestamp(next_run)}")
    else:
        next_run = get_most_recent_available()
        logger.info(f"No current model found, using: {fmt_timestamp(next_run)}")
    
    while True:
        now = datetime.utcnow()
        
        # Skip if too old
        if next_run < now - timedelta(hours=6):
            logger.warning(f"Next run {fmt_timestamp(next_run)} is too old, skipping")
            next_run = get_most_recent_available()
            continue
        
        # Check if data is available
        is_recent = (now - next_run).total_seconds() < 6 * 3600
        if check_data_available(next_run, check_extended=is_recent):
            logger.info(f"Data available for {fmt_timestamp(next_run)}, starting download...")
            if download_and_upload_model(next_run):
                next_run += timedelta(hours=6)
            else:
                logger.warning(f"Download failed, will retry in {args.check_interval}s")
                time.sleep(args.check_interval)
        else:
            # Not ready yet, wait
            wait_time = min(args.check_interval, CHECK_INTERVAL_DEFAULT)
            logger.debug(f"Waiting for {fmt_timestamp(next_run)} to become available...")
            time.sleep(wait_time)

def main():
    """Main entry point"""
    if args.daemon:
        run_daemon_mode()
        return 0  # Should never reach here in daemon mode
    else:
        # Single run mode (default) - for scheduled jobs like GitHub Actions
        return run_single_cycle()

if __name__ == "__main__":
    sys.exit(main() or 0)
