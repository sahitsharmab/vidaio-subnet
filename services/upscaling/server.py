from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pathlib import Path
import subprocess
import os
from fastapi.responses import JSONResponse
import time
import asyncio
from vidaio_subnet_core import CONFIG
import re
from pydantic import BaseModel
from typing import Optional
from services.miner_utilities.redis_utils import schedule_file_deletion
from vidaio_subnet_core.utilities import storage_client, download_video
from loguru import logger
import traceback

app = FastAPI()

class UpscaleRequest(BaseModel):
    payload_url: str
    task_type: str
    # output_file_upscaled: Optional[str] = None
    

def get_frame_rate(input_file: Path) -> float:
    """
    Extracts the frame rate of the input video using FFmpeg.

    Args:
        input_file (Path): The path to the video file.

    Returns:
        float: The frame rate of the video.
    """
    frame_rate_command = [
        "ffmpeg",
        "-i", str(input_file),
        "-hide_banner"
    ]
    process = subprocess.run(frame_rate_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    output = process.stderr  # Frame rate is usually in stderr

    # Extract frame rate using regex
    match = re.search(r"(\d+(?:\.\d+)?) fps", output)
    if match:
        return float(match.group(1))
    else:
        raise HTTPException(status_code=500, detail="Unable to determine frame rate of the video.")


def upscale_video(payload_video_path: str, task_type: str):
    """
    Upscales a video using the video2x tool and returns the full paths of the upscaled video and the converted mp4 file.

    Args:
        payload_video_path (str): The path to the video to upscale.
        task_type (str): The type of upscaling task to perform.

    Returns:
        str: The full path to the upscaled video.
    """
    try:
        input_file = Path(payload_video_path)

        scale_factor = "2"

        if task_type == "SD24K":
            scale_factor = "4"

        # Validate input file
        if not input_file.exists() or not input_file.is_file():
            raise HTTPException(status_code=400, detail="Input file does not exist or is not a valid file.")

        # Get the frame rate of the video
        frame_rate = get_frame_rate(input_file)
        print(f"Frame rate detected: {frame_rate} fps")

        # Generate output file path
        output_file_upscaled = input_file.with_name(f"{input_file.stem}_upscaled.mp4")

        # Upscale video directly using video2x (removed frame duplication step)
        # Frame duplication was causing frame count mismatch with reference video,
        # resulting in zero scores from validators
        print("Upscaling video using video2x...")
        start_time = time.time()
        video2x_command = [
            "video2x",
            "-i", str(input_file),
            "-o", str(output_file_upscaled),
            "-p", "realesrgan",               # Best upscaling model
            "-s", scale_factor,
            "-c", "libx265",                  # HEVC for good compression
            "-e", "preset=medium",            # Balanced speed/quality
            "-e", "crf=18",                   # Higher quality (synthetic scoring doesn't penalize size)
            "-e", "profile=main",
            "-e", "pix_fmt=yuv420p",
            "-e", "sar=1:1",
            "-e", "color_primaries=bt709",
            "-e", "color_trc=bt709",
            "-e", "colorspace=bt709",
            "-e", "movflags=+faststart",
        ]
        video2x_process = subprocess.run(video2x_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        elapsed_time = time.time() - start_time
        if video2x_process.returncode != 0:
            print(f"Upscaling failed: {video2x_process.stderr.strip()}")
            raise HTTPException(status_code=500, detail=f"Upscaling failed: {video2x_process.stderr.strip()}")
        if not output_file_upscaled.exists():
            print("Upscaled MP4 video file was not created.")
            raise HTTPException(status_code=500, detail="Upscaled MP4 video file was not created.")
        print(f"Upscaling completed in {elapsed_time:.2f} seconds. Upscaled MP4 file: {output_file_upscaled}")

        # Cleanup input file
        if input_file.exists():
            input_file.unlink()
            print(f"Original file {input_file} deleted.")

        print(f"Returning from FastAPI: {output_file_upscaled}")
        return output_file_upscaled
    except Exception as e:
        print(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@app.post("/upscale-video")
async def video_upscaler(request: UpscaleRequest):
    try:
        payload_url = request.payload_url
        task_type = request.task_type

        logger.info("📻 Downloading video....")
        payload_video_path: str = await download_video(payload_url)
        logger.info(f"Download video finished, Path: {payload_video_path}")

        processed_video_path = upscale_video(payload_video_path, task_type)
        processed_video_name = Path(processed_video_path).name

        logger.info(f"Processed video path: {processed_video_path}, video name: {processed_video_name}")

        if processed_video_path is not None:
            object_name: str = processed_video_name
            
            await storage_client.upload_file(object_name, processed_video_path)
            logger.info("Video uploaded successfully.")
            
            # Delete the local file since we've already uploaded it to MinIO
            if os.path.exists(processed_video_path):
                os.remove(processed_video_path)
                logger.info(f"{processed_video_path} has been deleted.")
            else:
                logger.info(f"{processed_video_path} does not exist.")
                
            sharing_link: str | None = await storage_client.get_presigned_url(object_name)
            if not sharing_link:
                logger.error("Upload failed")
                return {"uploaded_video_url": None}
            
            # Schedule the file for deletion after 10 minutes (600 seconds)
            deletion_scheduled = schedule_file_deletion(object_name)
            if deletion_scheduled:
                logger.info(f"Scheduled deletion of {object_name} after 10 minutes")
            else:
                logger.warning(f"Failed to schedule deletion of {object_name}")
            
            logger.info(f"Public download link: {sharing_link}")  

            return {"uploaded_video_url": sharing_link}

    except Exception as e:
        logger.error(f"Failed to process upscaling request: {e}")
        traceback.print_exc()
        return {"uploaded_video_url": None}


if __name__ == "__main__":
    
    import uvicorn
    
    host = CONFIG.video_upscaler.host
    port = CONFIG.video_upscaler.port
    
    uvicorn.run(app, host=host, port=port)