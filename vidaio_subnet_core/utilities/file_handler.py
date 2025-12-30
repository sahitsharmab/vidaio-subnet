import time
import httpx
import os
import sys
import uuid
import hashlib
import shutil
import fcntl
from firerequests import FireRequests
from loguru import logger
from pathlib import Path
from rich.progress import Progress, TaskID

fire_downloader = FireRequests()

CACHE_DIR = Path("cache/inputs")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def clean_tmp_directory():
    """Clean the tmp directory if running as validator and delete only .mp4 files."""
    if (
        __name__ != "__main__"
        and os.path.basename(os.path.abspath(sys.argv[0])) == "validator.py"
    ):
        tmp_dir = Path("tmp")
        tmp_dir.mkdir(exist_ok=True)  # Create the tmp directory if it doesn't exist
        
        # Iterate over all files in the tmp directory
        for file in track(tmp_dir.iterdir(), description="Cleaning .mp4 files in tmp directory"):
            if file.suffix == ".mp4":  # Only delete .mp4 files
                os.remove(file)
                print(f"Deleted: {file}").remove(os.path.join("tmp", file))

def _generate_filename(url: str) -> str:
    """Generate a unique filename for downloaded file."""
    tmp_dir = Path("tmp")
    tmp_dir.mkdir(exist_ok=True)  # Create the tmp directory if it doesn't exist
    return os.path.join("tmp", str(uuid.uuid4()) + ".mp4")

def _cache_paths(url: str):
    """Return cache file path and lock path for a url."""
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    cache_file = CACHE_DIR / f"{h}.mp4"
    lock_file = CACHE_DIR / f"{h}.lock"
    return cache_file, lock_file

async def download_video(url: str) -> str:
    """
    Simple function to download a video file from a URL.
    
    Args:
        url (str): The URL of the video to download.
        
    Returns:
        str: The local file path of the downloaded video.
    """
    cache_file, lock_file = _cache_paths(url)
    tmp_target = Path(_generate_filename(url))

    # Try cache first with a simple file lock to avoid duplicate downloads across processes.
    lock_fd = open(lock_file, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if cache_file.exists() and cache_file.stat().st_size > 0:
            # Reuse cached file by hardlinking into tmp (fallback to copy).
            try:
                os.link(cache_file, tmp_target)
            except Exception:
                shutil.copy2(cache_file, tmp_target)
            print(f"Cache hit, reused: {cache_file} -> {tmp_target}")
            return str(tmp_target)

        # Cache miss: download to a temp then move into cache.
        download_tmp = cache_file.with_suffix(".partial")
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                total_size = int(response.headers.get("Content-Length", 0))

                download_tmp.parent.mkdir(parents=True, exist_ok=True)
                with open(download_tmp, "wb") as f:
                    with Progress() as progress:
                        task = progress.add_task("[cyan]Downloading...", total=total_size)
                        async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                            f.write(chunk)
                            progress.update(task, advance=len(chunk))
        download_tmp.rename(cache_file)
        try:
            os.link(cache_file, tmp_target)
        except Exception:
            shutil.copy2(cache_file, tmp_target)
        print(f"Video downloaded to: {tmp_target} (cached at {cache_file})")
        return str(tmp_target)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass
