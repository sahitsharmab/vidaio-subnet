import uuid
import json
import asyncio
from pathlib import Path
import aiohttp
from fastapi import HTTPException
from loguru import logger
from vidaio_subnet_core import CONFIG
import os
import time
from typing import Optional

# Connection pool for reusing HTTP connections across downloads
_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    """Get or create a persistent aiohttp session for connection reuse."""
    global _session
    async with _session_lock:
        if _session is None or _session.closed:
            # Configure for better throughput
            connector = aiohttp.TCPConnector(
                limit=10,  # Max concurrent connections
                limit_per_host=5,
                ttl_dns_cache=300,  # Cache DNS for 5 minutes
                enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(total=300, connect=30)
            _session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return _session


async def download_video(video_url: str) -> Path:
    """
    Downloads a video from the given URL with connection pooling.

    Args:
        video_url (str): The URL of the video to download.

    Returns:
        Path: The local path of the downloaded video.

    Raises:
        HTTPException: If the download fails.
    """
    try:
        video_dir = Path(__file__).parent.parent / "upscaling" / "videos"
        video_dir.mkdir(parents=True, exist_ok=True)

        # Generate a unique filename
        filename = f"{uuid.uuid4()}.mp4"
        output_path = video_dir / filename

        logger.info(f"Downloading video from {video_url} to {output_path}")
        start_time = time.time()

        # Use persistent session for connection reuse
        session = await _get_session()
        async with session.get(video_url) as response:
            if response.status != 200:
                raise Exception(f"Failed to download video. HTTP status: {response.status}")

            # Write the content to the temp file in chunks
            with open(output_path, "wb") as f:
                async for chunk in response.content.iter_chunked(3 * 1024 * 1024):  # 3 MB chunks
                    f.write(chunk)

        elapsed_time = time.time() - start_time
        file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        speed_mbps = (file_size_mb * 8) / elapsed_time if elapsed_time > 0 else 0
        logger.info(f"Download complete: {file_size_mb:.1f} MB in {elapsed_time:.2f}s ({speed_mbps:.1f} Mbps)")

        # Verify the file was successfully downloaded
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise Exception(f"Download failed or file is empty: {output_path}")

        logger.info(f"File successfully downloaded to: {output_path}")
        return output_path

    except Exception as e:
        logger.error(f"Failed to download video from {video_url}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error downloading video: {str(e)}")


async def cleanup_session():
    """Clean up the aiohttp session on shutdown."""
    global _session
    async with _session_lock:
        if _session and not _session.closed:
            await _session.close()
            _session = None


async def video_upscaler(payload_url: str, task_type: str) -> str | None:
    """
    Sends a video file path to the upscaling service and retrieves the processed video path.
    
    Args:
        payload_url (str): The url of the video to be upscaled.
    
    Returns:
        str | None: The path of the upscaled video or None if an error occurs.
    """
    url = f"http://{CONFIG.video_upscaler.host}:{CONFIG.video_upscaler.port}/upscale-video"
    headers = {"Content-Type": "application/json"}
    data = {
        "payload_url": payload_url,
        "task_type": task_type,
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=json.dumps(data)) as response:
            if response.status == 200:
                result = await response.json()
                uploaded_video_url = result.get("uploaded_video_url")
                # logger.info(f"Processed video URL: {uploaded_video_url}")
                if uploaded_video_url is None:
                    logger.info("🩸 Received None response from video upscaler 🩸")
                    return None
                logger.info("✈️ Received response from video upscaler correctly ✈️")
                return uploaded_video_url
            logger.error(f"Upscaling service error: {response.status}")
            return None

# Limit concurrent compression calls per miner process to avoid overload
_COMPRESSOR_SEMAPHORE = asyncio.Semaphore(1)

# Persistent session for compressor calls (connection reuse)
_compressor_session: Optional[aiohttp.ClientSession] = None
_compressor_session_lock = asyncio.Lock()

# Validator timeout is 90s - we need margin for network overhead
VALIDATOR_TIMEOUT = 90
MAX_QUEUE_WAIT = 15  # Max seconds to wait in queue before giving up
SAFE_PROCESSING_TIME = 75  # Target max processing time to leave margin


async def _get_compressor_session() -> aiohttp.ClientSession:
    """Get or create a persistent session for compressor calls."""
    global _compressor_session
    async with _compressor_session_lock:
        if _compressor_session is None or _compressor_session.closed:
            connector = aiohttp.TCPConnector(
                limit=5,
                limit_per_host=3,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )
            # Total timeout slightly under validator timeout
            timeout = aiohttp.ClientTimeout(total=VALIDATOR_TIMEOUT - 5, connect=10)
            _compressor_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return _compressor_session


async def video_compressor(payload_url: str, vmaf_threshold: float, target_codec: str = "av1",
                          codec_mode: str = "CRF", target_bitrate: float = 10.0) -> str | None:
    """
    Sends a video file path to the compression service and retrieves the processed video path.

    Args:
        payload_url (str): The URL of the video to be compressed.
        vmaf_threshold (float): The VMAF threshold for quality control.
        target_codec (str): The target codec for compression (default: "av1").
        codec_mode (str): Codec mode - CBR, VBR, or CRF (default: "CRF").
        target_bitrate (float): Target bitrate in Mbps (default: 10.0).

    Returns:
        str | None: The URL of the compressed video or None if an error occurs.
    """
    url = f"http://{CONFIG.video_compressor.host}:{CONFIG.video_compressor.port}/compress-video"
    headers = {"Content-Type": "application/json"}
    data = {
        "payload_url": payload_url,
        "vmaf_threshold": vmaf_threshold,
        "target_codec": target_codec,
        "codec_mode": codec_mode,
        "target_bitrate": target_bitrate,
    }

    request_start = time.time()
    logger.info(f"🎬 Sending compression request: VMAF={vmaf_threshold}, Codec={target_codec}, Mode={codec_mode}, Bitrate={target_bitrate} Mbps")

    # Try to acquire semaphore with timeout to detect queue delays
    try:
        acquired = await asyncio.wait_for(
            _COMPRESSOR_SEMAPHORE.acquire(),
            timeout=MAX_QUEUE_WAIT
        )
    except asyncio.TimeoutError:
        queue_wait = time.time() - request_start
        logger.warning(f"⏳ Queue timeout after {queue_wait:.1f}s - another request is taking too long")
        return None

    queue_wait = time.time() - request_start
    if queue_wait > 2:
        logger.info(f"⏳ Waited {queue_wait:.1f}s in queue before processing")

    # Check if we still have enough time after queuing
    remaining_time = VALIDATOR_TIMEOUT - queue_wait - 5  # 5s safety margin
    if remaining_time < 60:
        logger.warning(f"⚠️ Only {remaining_time:.1f}s remaining after queue - may timeout")

    try:
        session = await _get_compressor_session()
        processing_start = time.time()

        try:
            async with session.post(url, headers=headers, data=json.dumps(data)) as response:
                if response.status == 200:
                    result = await response.json()
                    uploaded_video_url = result.get("uploaded_video_url")

                    processing_time = time.time() - processing_start
                    total_time = time.time() - request_start

                    if uploaded_video_url is None:
                        logger.info("🩸 Received None response from video compressor 🩸")
                        return None

                    # Log detailed timing
                    logger.info(f"✈️ Compression complete in {total_time:.1f}s (queue: {queue_wait:.1f}s, process: {processing_time:.1f}s) ✈️")

                    # Warn if we're cutting it close
                    margin = VALIDATOR_TIMEOUT - total_time
                    if margin < 10:
                        logger.warning(f"⚠️ Tight margin! Only {margin:.1f}s under {VALIDATOR_TIMEOUT}s timeout")

                    return uploaded_video_url

                logger.error(f"Compression service error: {response.status}")
                return None

        except asyncio.TimeoutError:
            elapsed = time.time() - request_start
            logger.error(f"❌ Compression request timed out after {elapsed:.1f}s")
            return None
        except Exception as e:
            logger.error(f"❌ Compression request failed: {e}")
            return None

    finally:
        _COMPRESSOR_SEMAPHORE.release()
