import os
import time
import json
import tempfile
import subprocess
import concurrent.futures
from ffmpeg_quality_metrics import FfmpegQualityMetrics
import shutil

# Default ffmpeg binary for VMAF calculations (must have libvmaf support)
FFMPEG_VMAF_BINARY = '/usr/local/bin/ffmpeg-vmaf'

def _extract_clip_optimized(input_file, output_file, start_time, duration, logging_enabled=False):
    """Extract clip with keyframe-aware seeking for better quality."""
    try:
        input_ext = os.path.splitext(input_file)[1].lower()
        raw_formats = ['.y4m', '.yuv', '.raw', '.rgb']
        is_raw_format = input_ext in raw_formats
        
        if is_raw_format:
            cmd = [
                'ffmpeg', '-y', '-ss', str(start_time), '-i', input_file,
                '-t', str(duration),
                '-c:v', 'libx264', '-preset', 'ultrafast', '-qp', '0',
                '-pix_fmt', 'yuv420p', '-an', output_file
            ]
        else:
            cmd = [
                'ffmpeg', '-y', 
                '-ss', str(max(0, start_time - 1)),  
                '-i', input_file,
                '-ss', '1',  
                '-t', str(duration),
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '10',  
                '-pix_fmt', 'yuv420p', '-an', output_file
            ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        success = result.returncode == 0 and os.path.exists(output_file) and os.path.getsize(output_file) > 0
        
        if logging_enabled:
            method = "lossless encoding" if is_raw_format else "keyframe-aligned encoding"
            status = "✅" if success else "❌"
            print(f"   {status} Clip extraction ({method}): {start_time}s")
        
        return success
        
    except Exception as e:
        if logging_enabled:
            print(f"   ❌ Clip extraction error: {e}")
        return False

def _process_single_clip(args):
    """Process a single clip for VMAF calculation (used for parallel processing)."""
    i, start_time, input_file, encoded_file, clip_duration, logging_enabled = args

    temp_dir = tempfile.mkdtemp()
    ref_clip = os.path.join(temp_dir, f"ref_clip_{i+1}.mp4")
    enc_clip = os.path.join(temp_dir, f"enc_clip_{i+1}.mp4")

    try:
        # Extract reference clip
        ref_cmd = [
            'ffmpeg', '-y', '-ss', str(start_time), '-i', input_file,
            '-t', str(clip_duration), '-c:v', 'libx264', '-preset', 'ultrafast',
            '-crf', '10', '-force_key_frames', 'expr:gte(t,0)',
            '-pix_fmt', 'yuv420p', '-avoid_negative_ts', 'make_zero', ref_clip
        ]

        # Extract encoded clip
        enc_cmd = [
            'ffmpeg', '-y', '-ss', str(start_time), '-i', encoded_file,
            '-t', str(clip_duration), '-c:v', 'libx264', '-preset', 'ultrafast',
            '-crf', '15', '-pix_fmt', 'yuv420p', '-avoid_negative_ts', 'make_zero', enc_clip
        ]

        ref_result = subprocess.run(ref_cmd, capture_output=True, text=True, timeout=60)
        enc_result = subprocess.run(enc_cmd, capture_output=True, text=True, timeout=60)

        if ref_result.returncode == 0 and enc_result.returncode == 0:
            # Calculate VMAF for this clip
            ffqm = FfmpegQualityMetrics(ref_clip, enc_clip, scaling_algorithm='bicubic', ffmpeg_path=FFMPEG_VMAF_BINARY)
            metrics = ffqm.calculate(["vmaf"])

            if 'vmaf' in metrics and metrics['vmaf']:
                clip_vmaf = sum([frame["vmaf"] for frame in metrics["vmaf"]]) / len(metrics["vmaf"])
                return (i, round(clip_vmaf, 2), None)
            else:
                return (i, None, "VMAF calculation failed")
        else:
            return (i, None, "Clip extraction failed")

    except Exception as e:
        return (i, None, str(e))
    finally:
        # Cleanup
        for temp_file in [ref_clip, enc_clip]:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass
        try:
            os.rmdir(temp_dir)
        except:
            pass


def extract_vmaf_clips_with_keyframe_detection(input_file, encoded_file, num_clips=5, clip_duration=3, logging_enabled=True, ffmpeg_path=None, use_parallel=True):
    """Enhanced clip extraction with proper keyframe alignment and parallel processing.

    Args:
        input_file: Path to reference video
        encoded_file: Path to encoded video
        num_clips: Number of clips to sample
        clip_duration: Duration of each clip in seconds
        logging_enabled: Whether to print progress messages
        ffmpeg_path: Optional custom ffmpeg path
        use_parallel: Whether to use parallel processing for clips (default: True)
    """

    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
            '-of', 'json', input_file
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        duration = float(data['format']['duration'])

        if duration < clip_duration * num_clips:
            if logging_enabled:
                print(f"   ⚠️ Video too short ({duration:.1f}s) for {num_clips} clips")
            return None

    except Exception as e:
        if logging_enabled:
            print(f"   ❌ Could not determine video duration: {e}")
        return None

    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
            '-show_frames', '-show_entries', 'frame=pts_time,key_frame',
            '-of', 'csv=p=0', input_file
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        keyframes = []
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split(',')
                if len(parts) >= 2 and parts[1] == '1':  # key_frame=1
                    keyframes.append(float(parts[0]))

        if logging_enabled:
            print(f"   🔑 Found {len(keyframes)} keyframes")

    except Exception as e:
        if logging_enabled:
            print(f"   ⚠️ Keyframe detection failed: {e}, using standard timing")
        keyframes = []

    clip_positions = []

    target_positions = []
    for i in range(num_clips):
        position = 0.15 + (i * (0.85 - 0.15) / (num_clips - 1)) if num_clips > 1 else 0.5
        target_positions.append(duration * position)

    for target_pos in target_positions:
        if keyframes:
            nearest_keyframe = min(keyframes, key=lambda x: abs(x - target_pos))
            if nearest_keyframe + clip_duration <= duration:
                clip_positions.append(nearest_keyframe)
            else:
                valid_keyframes = [kf for kf in keyframes if kf + clip_duration <= duration]
                if valid_keyframes:
                    clip_positions.append(max(valid_keyframes))
                else:
                    clip_positions.append(max(0, duration - clip_duration))
        else:
            clip_positions.append(max(0, min(target_pos, duration - clip_duration)))

    if logging_enabled:
        print(f"   📍 Clip positions: {[f'{pos:.2f}s' for pos in clip_positions]}")

    vmaf_scores = []

    # Use parallel processing for clip VMAF calculations
    if use_parallel and num_clips > 1:
        if logging_enabled:
            print(f"   🚀 Processing {num_clips} clips in parallel...")

        # Prepare arguments for parallel processing
        clip_args = [
            (i, start_time, input_file, encoded_file, clip_duration, False)  # logging_enabled=False for threads
            for i, start_time in enumerate(clip_positions)
        ]

        # Use ThreadPoolExecutor for parallel clip processing
        # Limit workers to avoid overwhelming the system (VMAF is CPU-intensive)
        max_workers = min(num_clips, max(2, os.cpu_count() // 2))

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_single_clip, args): args[0] for args in clip_args}

            for future in concurrent.futures.as_completed(futures):
                clip_idx = futures[future]
                try:
                    idx, score, error = future.result()
                    if score is not None:
                        vmaf_scores.append((idx, score))
                        if logging_enabled:
                            print(f"   ✅ Clip {idx+1} VMAF: {score:.2f}")
                    else:
                        if logging_enabled:
                            print(f"   ❌ Clip {idx+1} failed: {error}")
                except Exception as e:
                    if logging_enabled:
                        print(f"   ❌ Clip {clip_idx+1} exception: {e}")

        # Sort by clip index and extract just the scores
        vmaf_scores.sort(key=lambda x: x[0])
        vmaf_scores = [score for idx, score in vmaf_scores]

    else:
        # Sequential processing (original behavior)
        for i, start_time in enumerate(clip_positions):
            if logging_enabled:
                print(f"   🎬 Extracting clip {i+1} at {start_time:.2f}s...")

            temp_dir = tempfile.mkdtemp()
            ref_clip = os.path.join(temp_dir, f"ref_clip_{i+1}.mp4")
            enc_clip = os.path.join(temp_dir, f"enc_clip_{i+1}.mp4")

            try:
                ref_cmd = [
                    'ffmpeg', '-y', '-ss', str(start_time), '-i', input_file,
                    '-t', str(clip_duration), '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-crf', '10', '-force_key_frames', 'expr:gte(t,0)',
                    '-pix_fmt', 'yuv420p', '-avoid_negative_ts', 'make_zero', ref_clip
                ]

                enc_cmd = [
                    'ffmpeg', '-y', '-ss', str(start_time), '-i', encoded_file,
                    '-t', str(clip_duration), '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-crf', '15', '-pix_fmt', 'yuv420p', '-avoid_negative_ts', 'make_zero', enc_clip
                ]

                ref_result = subprocess.run(ref_cmd, capture_output=True, text=True, timeout=60)
                enc_result = subprocess.run(enc_cmd, capture_output=True, text=True, timeout=60)

                if ref_result.returncode == 0 and enc_result.returncode == 0:
                    # Use FfmpegQualityMetrics with custom ffmpeg binary that has libvmaf
                    ffqm = FfmpegQualityMetrics(ref_clip, enc_clip, scaling_algorithm='bicubic', ffmpeg_path=FFMPEG_VMAF_BINARY)
                    metrics = ffqm.calculate(["vmaf"])

                    if 'vmaf' in metrics and metrics['vmaf']:
                        clip_vmaf = sum([frame["vmaf"] for frame in metrics["vmaf"]]) / len(metrics["vmaf"])
                        vmaf_scores.append(round(clip_vmaf, 2))

                        if logging_enabled:
                            print(f"   ✅ Clip {i+1} VMAF: {clip_vmaf:.2f}")
                    else:
                        if logging_enabled:
                            print(f"   ❌ Clip {i+1} VMAF calculation failed")
                else:
                    if logging_enabled:
                        print(f"   ❌ Clip {i+1} extraction failed")

            except Exception as e:
                if logging_enabled:
                    print(f"   ❌ Clip {i+1} processing failed: {e}")
            finally:
                # Cleanup
                for temp_file in [ref_clip, enc_clip]:
                    if os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                        except:
                            pass
                try:
                    os.rmdir(temp_dir)
                except:
                    pass

    if vmaf_scores:
        if len(vmaf_scores) >= 3:
            sorted_scores = sorted(vmaf_scores)
            median_score = sorted_scores[len(sorted_scores)//2]
            min_score = sorted_scores[0]
            max_score = sorted_scores[-1]
            
            outliers = [s for s in vmaf_scores if abs(s - median_score) > 15]
            
            if outliers and logging_enabled:
                print(f"   ⚠️ Detected outlier scores: {outliers}")
                print(f"   📊 Score distribution: min={min_score:.1f}, median={median_score:.1f}, max={max_score:.1f}")
                
                filtered_scores = [s for s in vmaf_scores if abs(s - median_score) <= 15]
                if len(filtered_scores) >= 2:
                    final_vmaf = sum(filtered_scores) / len(filtered_scores)
                    print(f"   🔧 Using filtered scores (removed {len(outliers)} outliers): {final_vmaf:.2f}")
                    return final_vmaf
        
        final_vmaf = sum(vmaf_scores) / len(vmaf_scores)
        if logging_enabled:
            print(f"   📊 Individual scores: {[f'{s:.2f}' for s in vmaf_scores]}")
            print(f"   📊 Average VMAF: {final_vmaf:.2f}")
        return final_vmaf
    else:
        return None

def calculate_vmaf_advanced(input_file, encoded_file, 
                           use_sampling=True, num_clips=3, clip_duration=2,
                           use_downscaling=False, scale_factor=0.5,
                           use_parallel=False, # This parameter is for the caller of parallel, not used inside this single func
                           use_vmafneg=False, 
                           default_vmaf_model_path_config: str = None,
                           vmafneg_model_path_config: str = None,
                           use_frame_rate_scaling=False,    
                           target_fps=15.0,                 
                           frame_rate_scaling_method='uniform', 
                           logger=None,logging_enabled=True,
                           ffmpeg_binary_path: str = None):
    """
    Calculate VMAF with multiple sampling methods
    
    Args:
        input_file: Path to the reference video file
        encoded_file: Path to the encoded video file
        use_sampling: Whether to use sampling for VMAF calculation
        num_clips: Number of clips to sample for VMAF calculation
        clip_duration: Duration of each clip in seconds
        use_downscaling: Whether to downscale videos for faster VMAF calculation
        scale_factor: Scale factor for downscaling (0.5 = 50% resolution)
        use_vmafneg: Whether to use VMAFNEG model
        default_vmaf_model_path_config: Full path to the default VMAF model.
        vmafneg_model_path_config: Full path to the VMAFNEG model.
        logger: Optional logger instance for logging messages
    Returns:
        VMAF score as a float, or None if calculation fails
        
    """  
    def log_message(message, level="info"):
        if logger:
            if level == "error": logger.error(message)
            elif level == "warning": logger.warning(message)
            elif level == "debug" and hasattr(logger, 'debug'): logger.debug(message)
            else: logger.info(message)
        else:
            print(message) # Fallback if no logger

    # Resolve optional ffmpeg binary for VMAF-only calculations by creating a temp bin dir with symlinks
    original_path = os.environ.get("PATH", "")
    temp_bin_dir = None
    if ffmpeg_binary_path:
        try:
            resolved_ffmpeg = shutil.which(ffmpeg_binary_path) or ffmpeg_binary_path
            temp_bin_dir = tempfile.mkdtemp()
            os.symlink(resolved_ffmpeg, os.path.join(temp_bin_dir, "ffmpeg"))
            # Try to locate matching ffprobe (either ffprobe-vmaf or alongside ffmpeg)
            resolved_ffprobe = shutil.which("ffprobe-vmaf") or shutil.which("ffprobe") or os.path.join(os.path.dirname(resolved_ffmpeg), "ffprobe")
            if resolved_ffprobe and os.path.exists(resolved_ffprobe):
                os.symlink(resolved_ffprobe, os.path.join(temp_bin_dir, "ffprobe"))
            os.environ["PATH"] = f"{temp_bin_dir}:{original_path}"
        except Exception as e:
            if logger:
                logger.warning(f"Failed to set custom ffmpeg for VMAF: {e}")
            else:
                print(f"Failed to set custom ffmpeg for VMAF: {e}")

    if not os.path.exists(input_file):
        log_message(f"Error: Input file '{input_file}' does not exist", "error")
        return None
        
    if not os.path.exists(encoded_file):
        log_message(f"Error: Encoded file '{encoded_file}' does not exist", "error")
        return None
    
    if use_frame_rate_scaling:
        log_message(f"Using frame rate scaling: {target_fps} FPS with {frame_rate_scaling_method} method")
    
    # Determine which VMAF model path to use directly from parameters
    target_model_path = None
    model_type_for_log = "libvmaf_default" # Default assumption

    if use_vmafneg:
        log_message(f"VMAFNEG flag is True. Using VMAFNEG model path: '{vmafneg_model_path_config}'", "debug")
        target_model_path = vmafneg_model_path_config
        model_type_for_log = "VMAFNEG"
    else:
        log_message(f"VMAFNEG flag is False. Using Default VMAF model path: '{default_vmaf_model_path_config}'", "debug")
        target_model_path = default_vmaf_model_path_config
        model_type_for_log = "Default VMAF"

    vmaf_options_dict = None
    
    if target_model_path and os.path.exists(target_model_path):
        log_message(f"Using VMAF model ({model_type_for_log}): {target_model_path}")
        vmaf_options_dict = {"model_path": target_model_path}
    elif target_model_path: # Path provided but not found
        log_message(f"Warning: Specified {model_type_for_log} model file not found at '{target_model_path}'. "
                    "ffmpeg-quality-metrics will use libvmaf's default model.", "warning")
        model_type_for_log = "libvmaf_default (model file not found)" # Update log type
    else: # No path provided for the selected option
        log_message(f"Warning: No model path provided for {model_type_for_log} configuration. "
                    "ffmpeg-quality-metrics will use libvmaf's default model.", "warning")
        model_type_for_log = "libvmaf_default (no model path provided)" # Update log type
    
    log_message(f"Effective VMAF model for this calculation: {model_type_for_log}", "debug")

    temp_dir = tempfile.mkdtemp()
    temp_files = []

    def extract_vmaf_score(metrics):
        """Extract VMAF score from metrics dictionary"""
        try:
            if 'vmaf' in metrics and metrics['vmaf']:
                # Calculate average of frame VMAF scores
                vmaf_score = sum([frame["vmaf"] for frame in metrics["vmaf"]]) / len(metrics["vmaf"])
                return round(vmaf_score, 2)
            else:
                print("No VMAF data found in metrics")
                return None
        except Exception as e:
            print(f"Error extracting VMAF score: {e}")
            return None
    
    try:
        # ---------- Handle downscaling if enabled ----------
        if use_downscaling:
            if logging_enabled:
                print(f"Downscaling videos to {int(scale_factor * 100)}% for faster VMAF calculation")
            
            # ✅ Check if input is raw/uncompressed format
            input_ext = os.path.splitext(input_file)[1].lower()
            encoded_ext = os.path.splitext(encoded_file)[1].lower()
            
            # Raw formats that cannot use stream copy
            raw_formats = ['.y4m', '.yuv', '.raw', '.rgb']
            
            is_input_raw = input_ext in raw_formats
            is_encoded_raw = encoded_ext in raw_formats
            
            scaled_ref = os.path.join(temp_dir, "ref_scaled.mp4")
            scaled_enc = os.path.join(temp_dir, "enc_scaled.mp4")
            temp_files.extend([scaled_ref, scaled_enc])
            
            # Get video dimensions
            cmd = [
                'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height', '-of', 'json', input_file
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            data = json.loads(result.stdout)
            
            try:
                width = int(int(data['streams'][0]['width']) * scale_factor)
                height = int(int(data['streams'][0]['height']) * scale_factor)
                
                # Must be even numbers for YUV formats
                width = width - (width % 2)
                height = height - (height % 2)
                
                # ✅ Smart codec selection for reference video
                if is_input_raw:
                    # Raw format - must encode (use lossless to preserve quality)
                    if logging_enabled:
                        print(f"   📹 Raw format detected ({input_ext}) - using lossless encoding")
                    ref_cmd = [
                        'ffmpeg', '-y', '-i', input_file, 
                        '-vf', f'scale={width}:{height}',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-qp', '0',  # Lossless
                        '-pix_fmt', 'yuv420p', scaled_ref
                    ]
                else:
                    # Already encoded - try to use stream copy with scaling
                    if logging_enabled:
                        print(f"   📼 Encoded format detected ({input_ext}) - trying stream copy with scaling")
                    ref_cmd = [
                        'ffmpeg', '-y', '-i', input_file, 
                        '-vf', f'scale={width}:{height}',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '10',  # Very high quality fallback
                        '-pix_fmt', 'yuv420p', scaled_ref
                    ]
                
                if is_encoded_raw:
                    # Raw format - must encode
                    enc_cmd = [
                        'ffmpeg', '-y', '-i', encoded_file, 
                        '-vf', f'scale={width}:{height}',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '15',  # High quality
                        '-pix_fmt', 'yuv420p', scaled_enc
                    ]
                else:
                    # Already encoded - use moderate quality
                    enc_cmd = [
                        'ffmpeg', '-y', '-i', encoded_file, 
                        '-vf', f'scale={width}:{height}',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18',  # Standard quality
                        '-pix_fmt', 'yuv420p', scaled_enc
                    ]
                
                # Execute commands
                ref_result = subprocess.run(ref_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                enc_result = subprocess.run(enc_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                if ref_result.returncode == 0 and enc_result.returncode == 0:
                    # Replace original paths with scaled versions
                    input_file = scaled_ref
                    encoded_file = scaled_enc
                    if logging_enabled:
                        quality_note = "lossless reference" if is_input_raw else "high quality reference"
                        print(f"   ✅ Videos downscaled to {width}x{height} ({quality_note})")
                else:
                    if logging_enabled:
                        print(f"   ❌ Error downscaling videos, using original resolution")
                        if ref_result.returncode != 0:
                            print(f"   Reference error: {ref_result.stderr.decode()}")
                        if enc_result.returncode != 0:
                            print(f"   Encoded error: {enc_result.stderr.decode()}")
                
            except (KeyError, ValueError, subprocess.SubprocessError) as e:
                if logging_enabled:
                    print(f"   ❌ Error downscaling videos, using original resolution: {e}")
                
        
        if use_frame_rate_scaling:
            if logging_enabled:
                print(f"Scaling frame rate to {target_fps} FPS for faster VMAF calculation")
            
            # Apply frame rate scaling to both input and encoded videos
            fps_scaled_ref, fps_scaled_enc = apply_frame_rate_scaling(
                input_file, encoded_file, target_fps, frame_rate_scaling_method, temp_dir, logging_enabled
            )
            
            if fps_scaled_ref and fps_scaled_enc:
                temp_files.extend([fps_scaled_ref, fps_scaled_enc])
                # Update file paths to use frame rate scaled versions
                input_file = fps_scaled_ref
                encoded_file = fps_scaled_enc
                if logging_enabled:
                    print(f"Videos scaled to {target_fps} FPS for VMAF calculation")
            else:
                log_message(f"Frame rate scaling failed, using original frame rate", "warning")

        # ---------- Handle clip sampling if enabled ----------
        if use_sampling:
            vmaf_score = extract_vmaf_clips_with_keyframe_detection(
                input_file=input_file,
                encoded_file=encoded_file,
                num_clips=num_clips,
                clip_duration=clip_duration,
                logging_enabled=logging_enabled
            )
            
            if vmaf_score is not None:
                return vmaf_score
            else:
                if logging_enabled:
                    print("Enhanced sampling failed, falling back to original method...")
        
            # Get video duration
            cmd = [
                'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'json', encoded_file
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            data = json.loads(result.stdout)
            
            try:
                duration = float(data['format']['duration'])
                print(f"Video duration: {duration}s")
            except (KeyError, ValueError) as e:
                print(f"Could not determine video duration: {e}, falling back to full calculation")
                # Calculate VMAF on full videos
                ffmpeg_path = ffmpeg_binary_path or FFMPEG_VMAF_BINARY
                ffqm = FfmpegQualityMetrics(input_file, encoded_file, scaling_algorithm='bicubic', ffmpeg_path=ffmpeg_path)
                metrics = ffqm.calculate(["vmaf"], vmaf_options=vmaf_options_dict)
                return extract_vmaf_score(metrics)

            # Skip sampling for short videos
            if duration <= num_clips * clip_duration * 2:
                log_message(f"Video too short ({duration}s), calculating full VMAF")
                ffmpeg_path = ffmpeg_binary_path or FFMPEG_VMAF_BINARY
                ffqm = FfmpegQualityMetrics(input_file, encoded_file, scaling_algorithm='bicubic', ffmpeg_path=ffmpeg_path)
                metrics = ffqm.calculate(["vmaf"], vmaf_options=vmaf_options_dict)
                return extract_vmaf_score(metrics)
            
            # Calculate strategic sample points (beginning, middle, end)
            sample_points = []
            
            # Beginning segment (first 30%)
            if num_clips >= 1:
                begin_point = duration * 0.1  # 10% into the video
                sample_points.append(begin_point)
            
            # Middle segment (middle 40%)
            if num_clips >= 2:
                middle_point = duration * 0.5  # 50% into the video
                sample_points.append(middle_point)
            
            # End segment (last 30%)
            if num_clips >= 3:
                end_point = duration * 0.9  # 90% into the video
                sample_points.append(end_point)
            
            # Add more evenly distributed points if requested
            if num_clips > 3:
                for i in range(4, num_clips + 1):
                    pos = duration * (i - 0.5) / (num_clips + 1)
                    sample_points.append(pos)
            
            print(f"Using {len(sample_points)} clips for VMAF calculation")
            
            # ---------- Define clip processing function for parallel/sequential use ----------
            def process_clip(start_time):
                """Process a single clip for VMAF calculation"""
                # Adjust start time to ensure we don't go beyond video duration
                start_time = max(0, min(start_time, duration - clip_duration))
                
                # Create temp files for this clip
                ref_clip = os.path.join(temp_dir, f"ref_clip_{start_time:.2f}.mp4")
                enc_clip = os.path.join(temp_dir, f"enc_clip_{start_time:.2f}.mp4")
                
                # ✅ Use optimized extraction
                ref_success = _extract_clip_optimized(input_file, ref_clip, start_time, clip_duration, logging_enabled)
                if not ref_success:
                    print(f"Failed to extract reference clip at {start_time}s")
                    return None
                
                enc_success = _extract_clip_optimized(encoded_file, enc_clip, start_time, clip_duration, logging_enabled)
                if not enc_success:
                    print(f"Failed to extract encoded clip at {start_time}s")
                    return None
                
                temp_files.extend([ref_clip, enc_clip])
                
                # Calculate VMAF for this clip
                try:
                    ffmpeg_path = ffmpeg_binary_path or FFMPEG_VMAF_BINARY
                    ffqm = FfmpegQualityMetrics(ref_clip, enc_clip, scaling_algorithm='bicubic', ffmpeg_path=ffmpeg_path)
                    metrics = ffqm.calculate(["vmaf"], vmaf_options=vmaf_options_dict)
                    clip_vmaf = extract_vmaf_score(metrics)
                    if clip_vmaf is not None:
                        print(f"Clip at {start_time:.2f}s VMAF: {clip_vmaf}")
                    return clip_vmaf
                except Exception as e:
                    print(f"Error calculating VMAF for clip at {start_time:.2f}s: {e}")
                    return None
            
            # ---------- Process clips in parallel or sequentially ----------
            vmaf_scores = []
            
            if use_parallel:
                # Process clips in parallel
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(num_clips, os.cpu_count())) as executor:
                    futures = [executor.submit(process_clip, start_time) for start_time in sample_points]
                    
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            result = future.result()
                            if result is not None:
                                vmaf_scores.append(result)
                        except Exception as e:
                            print(f"Error processing clip: {e}")
            else:
                # Process clips sequentially
                for start_time in sample_points:
                    result = process_clip(start_time)
                    if result is not None:
                        vmaf_scores.append(result)
            
            # Average the scores
            if vmaf_scores:
                avg_vmaf = sum(vmaf_scores) / len(vmaf_scores)
                return round(avg_vmaf, 2)
            else:
                print("No valid VMAF scores calculated from clips, trying direct calculation")
                # Fall back to original calculation
                try:
                    ffmpeg_path = ffmpeg_binary_path or FFMPEG_VMAF_BINARY
                    ffqm = FfmpegQualityMetrics(input_file, encoded_file, scaling_algorithm='bicubic', ffmpeg_path=ffmpeg_path)
                    metrics = ffqm.calculate(["vmaf"], vmaf_options=vmaf_options_dict)
                    return extract_vmaf_score(metrics)
                except Exception as e:
                    print(f"Direct VMAF calculation failed: {e}")
                    return None
        else:
            # No sampling, calculate VMAF on entire video
            try:
                print("Calculating full VMAF (no sampling)...")
                ffmpeg_path = ffmpeg_binary_path or FFMPEG_VMAF_BINARY
                ffqm = FfmpegQualityMetrics(input_file, encoded_file, scaling_algorithm='bicubic', ffmpeg_path=ffmpeg_path)
                metrics = ffqm.calculate(["vmaf"], vmaf_options=vmaf_options_dict)
                return extract_vmaf_score(metrics)
            except Exception as e:
                print(f"Full VMAF calculation failed: {e}")
                return None
            
    except Exception as e:
        log_message(f"Error in VMAF calculation: {e}", "error")
        # Fall back to direct FFmpeg VMAF calculation
        try:
            log_message("Attempting direct FFmpeg VMAF calculation...")
            libvmaf_options_list = ["log_fmt=json"]
            if target_model_path and os.path.exists(target_model_path): # Check target_model_path directly
                model_path_for_cli = target_model_path.replace("\\", "/") 
                libvmaf_options_list.append(f"model_path='{model_path_for_cli}'")
            libvmaf_filter_options = ":".join(libvmaf_options_list)
            cmd = [
                'ffmpeg', '-i', input_file, '-i', encoded_file,
                '-filter_complex', f'[0:v]setpts=PTS-STARTPTS[reference];[1:v]setpts=PTS-STARTPTS[distorted];[reference][distorted]libvmaf={libvmaf_filter_options}',
                '-f', 'null', '-'
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            # Extract VMAF score from output
            for line in result.stderr.splitlines():
                if "VMAF score:" in line:
                    score = float(line.split("VMAF score:")[1].strip())
                    print(f"Direct FFmpeg VMAF score: {score}")
                    return round(score, 2)
            
            print("Could not extract VMAF score from FFmpeg output")
            return None
        except Exception as e2:
            print(f"Fatal error calculating VMAF: {e2}")
            return None
        
    finally:
        # Clean up temp files and directory
        for file in temp_files:
            try:
                if os.path.exists(file):
                    os.unlink(file)
            except Exception as e:
                print(f"Error cleaning up temp file {file}: {e}")
        
        try:
            os.rmdir(temp_dir)
        except Exception as e:
            print(f"Error removing temp directory: {e}")
        
        # Restore original PATH so encoding keeps using system ffmpeg (NVENC-capable)
        os.environ["PATH"] = original_path
        if temp_bin_dir and os.path.exists(temp_bin_dir):
            try:
                for fname in ("ffmpeg", "ffprobe"):
                    fpath = os.path.join(temp_bin_dir, fname)
                    if os.path.exists(fpath):
                        os.unlink(fpath)
                os.rmdir(temp_bin_dir)
            except Exception:
                pass

def apply_frame_rate_scaling(input_file, encoded_file, target_fps, scaling_method, temp_dir, logging_enabled=True):
    """Apply frame rate scaling while preserving reference quality."""
    try:
        import subprocess
        import json
        
        # Get original video properties
        def get_video_info(video_path):
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_streams', '-select_streams', 'v:0', video_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                stream = data['streams'][0]
                fps_str = stream.get('r_frame_rate', '30/1')
                if '/' in fps_str:
                    num, den = fps_str.split('/')
                    fps = float(num) / float(den) if float(den) != 0 else 30
                else:
                    fps = float(fps_str)
                return fps
            return None
        
        # Get original frame rates
        ref_fps = get_video_info(input_file)
        enc_fps = get_video_info(encoded_file)
        
        if not ref_fps or not enc_fps:
            if logging_enabled:
                print("Could not determine original frame rates")
            return None, None
        
        # Check if scaling is beneficial
        min_original_fps = min(ref_fps, enc_fps)
        if target_fps >= min_original_fps:
            if logging_enabled:
                print(f"Target FPS ({target_fps}) >= original FPS ({min_original_fps}), skipping frame rate scaling")
            return input_file, encoded_file  # Return original files
        
        # Create output paths
        scaled_ref = os.path.join(temp_dir, "ref_fps_scaled.mp4")
        scaled_enc = os.path.join(temp_dir, "enc_fps_scaled.mp4")
        
        # Build FFmpeg filter based on scaling method
        if scaling_method == 'uniform':
            filter_complex = f'fps={target_fps}'
        elif scaling_method == 'smart':
            interval = max(1, int(min_original_fps / target_fps))
            filter_complex = f'select=not(mod(n\\,{interval})),setpts=N/FRAME_RATE/TB'
        else:
            filter_complex = f'fps={target_fps}'
        
        # ✅ Smart codec selection based on input format
        input_ext = os.path.splitext(input_file)[1].lower()
        encoded_ext = os.path.splitext(encoded_file)[1].lower()
        
        raw_formats = ['.y4m', '.yuv', '.raw', '.rgb']
        is_input_raw = input_ext in raw_formats
        is_encoded_raw = encoded_ext in raw_formats
        
        # Scale reference video with appropriate quality
        if is_input_raw:
            # Raw format - use lossless encoding
            ref_cmd = [
                'ffmpeg', '-y', '-i', input_file,
                '-vf', filter_complex,
                '-c:v', 'libx264', '-preset', 'ultrafast', '-qp', '0',  # Lossless
                '-pix_fmt', 'yuv420p', scaled_ref
            ]
        else:
            # Already encoded - use very high quality
            ref_cmd = [
                'ffmpeg', '-y', '-i', input_file,
                '-vf', filter_complex,
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '10',  # Very high quality
                '-pix_fmt', 'yuv420p', scaled_ref
            ]
        
        # Scale encoded video (can use standard quality)
        enc_cmd = [
            'ffmpeg', '-y', '-i', encoded_file,
            '-vf', filter_complex,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18',
            '-pix_fmt', 'yuv420p', scaled_enc
        ]
        
        # Execute commands
        ref_result = subprocess.run(ref_cmd, capture_output=True, text=True)
        if ref_result.returncode != 0:
            if logging_enabled:
                print(f"Reference frame rate scaling failed: {ref_result.stderr}")
            return None, None
        
        enc_result = subprocess.run(enc_cmd, capture_output=True, text=True)
        if enc_result.returncode != 0:
            if logging_enabled:
                print(f"Encoded frame rate scaling failed: {enc_result.stderr}")
            return None, None
        
        # Verify scaled files exist and have content
        if (os.path.exists(scaled_ref) and os.path.getsize(scaled_ref) > 0 and
            os.path.exists(scaled_enc) and os.path.getsize(scaled_enc) > 0):
            
            if logging_enabled:
                quality_note = "lossless" if is_input_raw else "high quality"
                print(f"Frame rate scaling successful: {min_original_fps:.1f} → {target_fps} FPS ({quality_note} reference)")
            return scaled_ref, scaled_enc
        else:
            if logging_enabled:
                print("Frame rate scaling produced empty files")
            return None, None
            
    except Exception as e:
        if logging_enabled:
            print(f"Frame rate scaling error: {e}")
        return None, None


def calculate_vmaf_simple(reference_file: str, encoded_file: str,
                          scale_factor: float = 0.5,
                          target_fps: float = 15.0,
                          ffmpeg_binary: str = None,
                          logging_enabled: bool = True) -> float | None:
    """
    Simple, robust VMAF calculation using direct ffmpeg commands.

    This is a streamlined alternative to calculate_vmaf_advanced that:
    - Uses direct ffmpeg libvmaf filter (no FfmpegQualityMetrics library)
    - Optionally downscales and reduces framerate for speed
    - Parses VMAF score directly from ffmpeg output
    - Much more reliable than clip-based sampling

    Args:
        reference_file: Path to the original/reference video
        encoded_file: Path to the encoded/compressed video
        scale_factor: Downscale factor (0.5 = 50% resolution, 1.0 = no downscale)
        target_fps: Target FPS for VMAF calculation (lower = faster)
        ffmpeg_binary: Path to ffmpeg binary with libvmaf (default: ffmpeg-vmaf)
        logging_enabled: Whether to print progress messages

    Returns:
        VMAF score as float (0-100), or None if calculation fails
    """
    ffmpeg_bin = ffmpeg_binary or FFMPEG_VMAF_BINARY

    # Verify files exist
    if not os.path.exists(reference_file):
        if logging_enabled:
            print(f"❌ Reference file not found: {reference_file}")
        return None
    if not os.path.exists(encoded_file):
        if logging_enabled:
            print(f"❌ Encoded file not found: {encoded_file}")
        return None

    temp_dir = tempfile.mkdtemp(prefix="vmaf_simple_")

    try:
        start_time = time.time()

        # Get video dimensions for proper scaling
        probe_cmd = [
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height', '-of', 'json', reference_file
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)

        try:
            probe_data = json.loads(probe_result.stdout)
            orig_width = int(probe_data['streams'][0]['width'])
            orig_height = int(probe_data['streams'][0]['height'])
        except (json.JSONDecodeError, KeyError, IndexError):
            # Default to common dimensions if probe fails
            orig_width, orig_height = 1920, 1080
            if logging_enabled:
                print(f"⚠️ Could not probe dimensions, assuming {orig_width}x{orig_height}")

        # Calculate scaled dimensions (must be even for YUV)
        scaled_width = int(orig_width * scale_factor)
        scaled_height = int(orig_height * scale_factor)
        scaled_width = scaled_width - (scaled_width % 2)
        scaled_height = scaled_height - (scaled_height % 2)

        if logging_enabled:
            print(f"📊 VMAF Calculation:")
            print(f"   Reference: {os.path.basename(reference_file)}")
            print(f"   Encoded: {os.path.basename(encoded_file)}")
            if scale_factor < 1.0:
                print(f"   Scale: {orig_width}x{orig_height} → {scaled_width}x{scaled_height} ({int(scale_factor*100)}%)")
            if target_fps > 0:
                print(f"   FPS target: {target_fps}")

        # Build filter chains for both videos
        # Reference: scale and fps reduce
        ref_filter = []
        enc_filter = []

        if scale_factor < 1.0:
            ref_filter.append(f"scale={scaled_width}:{scaled_height}")
            enc_filter.append(f"scale={scaled_width}:{scaled_height}")

        if target_fps > 0:
            ref_filter.append(f"fps={target_fps}")
            enc_filter.append(f"fps={target_fps}")

        ref_filter_str = ",".join(ref_filter) if ref_filter else "null"
        enc_filter_str = ",".join(enc_filter) if enc_filter else "null"

        # Build the libvmaf filter complex
        # [0:v] = reference, [1:v] = encoded (distorted)
        filter_complex = (
            f"[0:v]{ref_filter_str},setpts=PTS-STARTPTS[ref];"
            f"[1:v]{enc_filter_str},setpts=PTS-STARTPTS[dist];"
            f"[dist][ref]libvmaf=log_fmt=json:log_path={temp_dir}/vmaf.json"
        )

        # Run ffmpeg with libvmaf
        cmd = [
            ffmpeg_bin,
            '-i', reference_file,
            '-i', encoded_file,
            '-filter_complex', filter_complex,
            '-f', 'null', '-'
        ]

        if logging_enabled:
            print(f"   Running VMAF calculation...")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        # Parse VMAF score from JSON output
        vmaf_json_path = os.path.join(temp_dir, "vmaf.json")
        vmaf_score = None

        if os.path.exists(vmaf_json_path):
            try:
                with open(vmaf_json_path, 'r') as f:
                    vmaf_data = json.load(f)

                # Extract pooled VMAF score (mean)
                if 'pooled_metrics' in vmaf_data and 'vmaf' in vmaf_data['pooled_metrics']:
                    vmaf_score = vmaf_data['pooled_metrics']['vmaf']['mean']
                elif 'frames' in vmaf_data and vmaf_data['frames']:
                    # Calculate mean from frame scores
                    frame_scores = [f['metrics']['vmaf'] for f in vmaf_data['frames'] if 'vmaf' in f.get('metrics', {})]
                    if frame_scores:
                        vmaf_score = sum(frame_scores) / len(frame_scores)

            except (json.JSONDecodeError, KeyError) as e:
                if logging_enabled:
                    print(f"   ⚠️ Error parsing VMAF JSON: {e}")

        # Fallback: try to parse from stderr
        if vmaf_score is None:
            for line in result.stderr.splitlines():
                if "VMAF score:" in line or "VMAF score =" in line:
                    try:
                        # Handle formats like "VMAF score: 92.5" or "VMAF score = 92.5"
                        parts = line.replace("=", ":").split(":")
                        for i, part in enumerate(parts):
                            if "VMAF score" in part and i + 1 < len(parts):
                                vmaf_score = float(parts[i + 1].strip().split()[0])
                                break
                    except (ValueError, IndexError):
                        continue

        elapsed = time.time() - start_time

        if vmaf_score is not None:
            vmaf_score = round(vmaf_score, 2)
            if logging_enabled:
                print(f"   ✅ VMAF: {vmaf_score} (calculated in {elapsed:.1f}s)")
            return vmaf_score
        else:
            if logging_enabled:
                print(f"   ❌ Failed to extract VMAF score")
                if result.returncode != 0:
                    # Show last few lines of stderr for debugging
                    stderr_lines = result.stderr.splitlines()[-5:]
                    for line in stderr_lines:
                        print(f"      {line}")
            return None

    except subprocess.TimeoutExpired:
        if logging_enabled:
            print(f"   ❌ VMAF calculation timed out")
        return None
    except Exception as e:
        if logging_enabled:
            print(f"   ❌ VMAF calculation error: {e}")
        return None
    finally:
        # Cleanup temp directory
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        except:
            pass
