#!/usr/bin/env python3
"""
Automated GEFS downloader daemon.
Downloads GEFS models every 6 hours and uploads to Supabase.
Number of models is controlled by downloader.DOWNLOAD_CONTROL and downloader.NUM_PERTURBED_MEMBERS.
"""
import os
import sys
import time
import logging
import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path to import root modules
sys.path.insert(0, str(Path(__file__).parent.parent))

import gefs
import downloader  # Import to access DOWNLOAD_CONTROL and NUM_PERTURBED_MEMBERS

# Setup logging
parser = argparse.ArgumentParser(description="Automated GEFS downloader daemon")
parser.add_argument("--logfile", default=None, help="Log file path (default: stdout)")
parser.add_argument("--savedir", default="/Applications/Emmanuel Zheng/habsim/data/gefs", help="Local directory for downloads")
parser.add_argument("--statusfile", default="./gefs/whichgefs", 
                    help="File to write current model timestamp")
parser.add_argument("--check-interval", type=int, default=300,
                    help="Seconds between checks for new data (default: 300)")
parser.add_argument("--test", action="store_true",
                    help="Test mode: run once and exit (downloads most recent available model)")
parser.add_argument("--test-timestamp", default=None,
                    help="Test with specific timestamp (format: YYYYMMDDHH)")
args = parser.parse_args()

logger = logging.getLogger(__name__)
logging.basicConfig(
    filename=args.logfile,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

if not args.logfile:
    # Also log to console if no file specified
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logger.addHandler(console)

def fmt_timestamp(dt: datetime) -> str:
    """Format datetime as YYYYMMDDHH"""
    return dt.strftime("%Y%m%d%H")

def get_current_model() -> datetime | None:
    """Read current model timestamp from status file"""
    if not os.path.exists(args.statusfile):
        return None
    try:
        with open(args.statusfile) as f:
            return datetime.strptime(f.read().strip(), "%Y%m%d%H")
    except Exception:
        return None

def get_most_recent_available() -> datetime:
    """Get most recent 6-hourly model run time that has extended range available.
    Prefers model from 12 hours ago to ensure all forecast hours (up to 384h) are available.
    Extended range forecasts take time to process, so newer models may not have all hours yet.
    """
    now = datetime.utcnow()
    # GEFS runs at 00, 06, 12, 18 UTC
    hour = (now.hour // 6) * 6
    # Prefer model from 12 hours ago to ensure extended range is fully available
    # Fall back to 6 hours ago if 12h model doesn't exist
    model_12h = datetime(now.year, now.month, now.day, hour) - timedelta(hours=12)
    model_6h = datetime(now.year, now.month, now.day, hour) - timedelta(hours=6)
    
    # Check if 12h model has extended range available
    if check_data_available(model_12h, check_extended=True):
        return model_12h
    # Fall back to 6h model if 12h doesn't have extended range yet
    elif check_data_available(model_6h, check_extended=False):
        return model_6h
    else:
        # Default to 12h model even if extended range isn't ready yet
        return model_12h

def check_data_available(timestamp: datetime, check_extended: bool = False) -> bool:
    """Check if data is available for given timestamp.
    
    Args:
        timestamp: Model timestamp to check
        check_extended: If True, also check for extended range forecast hour (f384)
                       to ensure all forecast hours are available
    """
    y, m, d, h = timestamp.year, timestamp.month, timestamp.day, timestamp.hour
    m, d, h = map(lambda x: str(x).zfill(2), [m, d, h])
    
    # Always check forecast hour 0 (basic availability)
    url_f000 = f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/gefs.{y}{m}{d}/{h}/atmos/pgrb2bp5/gep01.t{h}z.pgrb2b.0p50.f000"
    
    import urllib.request
    try:
        resp = urllib.request.urlopen(url_f000, timeout=10)
        if resp.status != 200:
            return False
    except Exception:
        return False
    
    # If checking extended range, verify forecast hour 384 is available
    if check_extended:
        url_f384 = f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/gefs.{y}{m}{d}/{h}/atmos/pgrb2bp5/gep01.t{h}z.pgrb2b.0p50.f384"
        try:
            resp = urllib.request.urlopen(url_f384, timeout=10)
            return resp.status == 200
        except Exception:
            return False
    
    return True

def download_and_upload_model(timestamp: datetime) -> bool:
    """Download model using downloader.py and upload to Supabase"""
    timestamp_str = fmt_timestamp(timestamp)
    logger.info(f"Starting download for {timestamp_str}")
    
    # Call downloader functions directly instead of subprocess
    savedir = Path(args.savedir)
    savedir.mkdir(parents=True, exist_ok=True)
    
    try:
        from datetime import datetime
        model_timestamp = datetime.strptime(timestamp_str, "%Y%m%d%H")
        # Set up minimal logging for downloader
        import logging
        downloader.logger = logging.getLogger('downloader')
        downloader.logger.setLevel(logging.INFO)
        if not downloader.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
            downloader.logger.addHandler(handler)
        
        # Call downloader's complete_run function directly
        downloader.complete_run(model_timestamp, timestamp_str=timestamp_str, savedir=str(savedir))
        logger.info(f"Downloader completed successfully for {timestamp_str}")
    except Exception as e:
        logger.error(f"Downloader failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False
    
    # Upload model files to Supabase (respects DOWNLOAD_CONTROL and NUM_PERTURBED_MEMBERS)
    logger.info(f"Uploading files to Supabase...")
    # Determine which models to upload based on downloader settings
    model_ids = []
    if downloader.DOWNLOAD_CONTROL:
        model_ids.append(0)
    model_ids.extend(range(1, 1 + downloader.NUM_PERTURBED_MEMBERS))
    expected_count = len(model_ids)
    
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
            # Remove local file after successful upload
            filepath.unlink()
        else:
            logger.error(f"Failed to upload {filename}")
    
    if success_count == expected_count:
        # Update whichgefs file in Supabase
        try:
            status_content = timestamp_str
            status_bytes = status_content.encode('utf-8')
            resp = gefs._SESSION.put(
                gefs._object_url(f"{gefs._BUCKET}/whichgefs"),
                headers={
                    **gefs._COMMON_HEADERS,
                    "Content-Type": "text/plain",
                    "Content-Length": str(len(status_bytes)),
                },
                data=status_bytes,
                timeout=(10, 30),
            )
            resp.raise_for_status()
            logger.info(f"Updated whichgefs to {timestamp_str}")
        except Exception as e:
            logger.warning(f"Failed to update whichgefs in Supabase: {e}")
        
        # Clean up old model files from Supabase
        old_model = get_current_model()
        if old_model and old_model != timestamp:
            old_timestamp_str = fmt_timestamp(old_model)
            logger.info(f"Cleaning up old model files: {old_timestamp_str}")
            # Use same model_ids list for cleanup
            for model_id in model_ids:
                old_filename = f"{old_timestamp_str}_{str(model_id).zfill(2)}.npz"
                if gefs.delete_gefs(old_filename):
                    logger.info(f"Deleted old file: {old_filename}")
                else:
                    logger.warning(f"Failed to delete old file: {old_filename}")
        
        # Update local status file
        with open(args.statusfile, 'w') as f:
            f.write(timestamp_str)
        
        logger.info(f"Successfully completed download and upload for {timestamp_str}")
        return True
    else:
        logger.error(f"Only {success_count}/{expected_count} files uploaded successfully")
        return False

def main():
    """Main daemon loop"""
    logger.info("Starting GEFS auto-downloader daemon")
    
    # Test mode: run once and exit
    if args.test:
        logger.info("TEST MODE: Running single download/upload cycle")
        if args.test_timestamp:
            test_timestamp = datetime.strptime(args.test_timestamp, "%Y%m%d%H")
            logger.info(f"Testing with specified timestamp: {fmt_timestamp(test_timestamp)}")
        else:
            test_timestamp = get_most_recent_available()
            logger.info(f"Testing with most recent available: {fmt_timestamp(test_timestamp)}")
        
        # In test mode, check extended range if using a recent model
        is_recent = (datetime.utcnow() - test_timestamp).total_seconds() < 12 * 3600
        if check_data_available(test_timestamp, check_extended=is_recent):
            logger.info(f"Data available, proceeding with download/upload...")
            success = download_and_upload_model(test_timestamp)
            if success:
                logger.info("TEST SUCCESS: Download and upload completed")
                return 0
            else:
                logger.error("TEST FAILED: Download/upload failed")
                return 1
        else:
            logger.warning(f"TEST: Data not yet available for {fmt_timestamp(test_timestamp)}")
            return 1
    
    # Normal daemon mode
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
        if next_run < now - timedelta(hours=12):
            logger.warning(f"Next run {fmt_timestamp(next_run)} is too old, skipping")
            next_run = get_most_recent_available()
            continue
        
        # Check if data is available (check extended range if model is recent)
        # For models older than 12 hours, extended range should definitely be available
        is_recent = (now - next_run).total_seconds() < 12 * 3600
        if check_data_available(next_run, check_extended=is_recent):
            logger.info(f"Data available for {fmt_timestamp(next_run)}, starting download...")
            if download_and_upload_model(next_run):
                next_run += timedelta(hours=6)
            else:
                logger.warning(f"Download failed, will retry in {args.check_interval}s")
                time.sleep(args.check_interval)
        else:
            # Not ready yet, wait
            wait_time = min(args.check_interval, 300)  # Check at most every 5 min
            logger.debug(f"Waiting for {fmt_timestamp(next_run)} to become available...")
            time.sleep(wait_time)

if __name__ == "__main__":
    sys.exit(main() or 0)
