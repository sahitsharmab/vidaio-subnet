# AI Instance Setup Prompt for Vidaio Subnet Miner

Use this prompt to onboard an AI assistant to set up and manage this miner on a new pod.

---

## Prompt for AI Assistant

```
You are helping set up a Bittensor Vidaio Subnet (SN85) miner. This is a video compression mining operation where validators send videos to miners, miners compress them, and validators score based on compression ratio (70%) and VMAF quality (30%).

## Project Overview

- **Subnet**: 85 (Vidaio) on Bittensor mainnet
- **Task**: Compress videos to meet VMAF quality threshold while maximizing compression
- **Timeout**: 90 seconds per request from validator
- **Scoring**: 70% compression ratio + 30% VMAF quality score

## Critical Constraints

1. **Validator timeout is 90 seconds** - responses must complete within this window
2. **Codec must match request** - if validator requests AV1, you must return AV1 (not HEVC fallback) or get 0 score
3. **VMAF threshold**: Score must be >= (threshold - 5) or get 0 score
4. **Compression minimum**: Must achieve at least 1.25x compression or get 0 score

## GPU Compatibility

| GPU | AV1 NVENC | Notes |
|-----|-----------|-------|
| RTX A6000 | NO | Falls back to libsvtav1 (CPU) - slower |
| RTX 6000 Ada | YES | Native AV1 NVENC - fast |
| RTX 4090/4080 | YES | Native AV1 NVENC - fast |
| A100/H100 | NO | Falls back to libsvtav1 (CPU) |

## Key Files to Understand

### Miner Entry Point
- `neurons/miner.py` - Main miner process, handles validator requests

### Compression Service
- `services/compress/server.py` - FastAPI compression server (port 5001)
  - Lines 210-217: Codec mapping (AV1 → av1_nvenc or libsvtav1)
  - Lines 650-710: Main configuration dictionary
  - Lines 695-708: VMAF calculation settings

### Encoder Configuration
- `services/compress/utils/encoder_configs.py` - Codec presets and CQ values
  - ENCODER_SETTINGS: Base codec configurations
  - SCENE_SPECIFIC_PARAMS: Per-scene-type overrides

### Video Analysis
- `services/compress/utils/analyze_video_fast.py` - Video metrics extraction
  - Line 7: `max_frames=30` (optimized from 150 for speed)

### VMAF Calculation
- `services/compress/utils/calculate_vmaf_adv.py` - VMAF scoring
  - `calculate_vmaf_simple()` - Robust direct FFmpeg method
  - `calculate_vmaf_advanced()` - Clip-sampling method (less reliable)

### Request Handling
- `services/miner_utilities/miner_utils.py` - Miner request logic
  - Semaphore for concurrent request limiting
  - Timeout handling

## Current Optimizations Applied

1. **Scene classification**: Enabled with 30 frames (was 150)
2. **Contrast optimization**: Disabled for speed
3. **VMAF calculation**: Disabled in production (validator calculates)
4. **Encoder presets**: p1 (fastest) for all NVENC codecs
5. **SVT-AV1 preset**: 12 (fastest) for CPU fallback
6. **ThreadPool workers**: 3 for concurrent jobs

## Setup Steps

1. Follow `MINER_SETUP.md` or `TESTNET_MINER_GUIDE.md`
2. Create temp directories
3. Install dependencies and patch ffmpeg-quality-metrics
4. Configure .env with S3 credentials
5. Regenerate wallet from mnemonic
6. Start services with PM2

## Verification Commands

```bash
# Check services running
pm2 list

# Check compressor logs
pm2 logs video-compressor --lines 50

# Check miner logs
pm2 logs video-miner --lines 50

# Test AV1 NVENC availability
ffmpeg -encoders 2>&1 | grep av1_nvenc

# Test VMAF support
/usr/local/bin/ffmpeg-vmaf -filters 2>&1 | grep vmaf
```

## Common Issues

1. **0 score - codec mismatch**: Validator requested AV1 but got HEVC
   - Fix: Ensure libsvtav1 fallback works (not hevc_nvenc)

2. **Timeout after 90s**: Processing too slow
   - Check: Scene classification time, FFmpeg encoding time
   - Fix: Reduce max_frames, use faster presets

3. **VMAF garbage values**: FfmpegQualityMetrics library issues
   - Fix: Use calculate_vmaf_simple() with use_simple_vmaf: True

4. **Port not reachable**: External port mapping wrong
   - Fix: Use --axon.external_port matching RunPod/Vast mapping

## Configuration Locations

| Setting | File | Line |
|---------|------|------|
| VMAF enabled/disabled | server.py | 696-697 |
| Max analysis frames | analyze_video_fast.py | 7 |
| Encoder presets | encoder_configs.py | 8-37 |
| Codec fallback logic | server.py | 210-217 |
| Request timeout | miner_utils.py | 135-137 |

## For RTX 6000 Ada (AV1 NVENC Available)

The code auto-detects AV1 NVENC at startup (server.py lines 165-185). If available:
- AV1 requests → av1_nvenc (GPU, fast)
- No code changes needed

## For A6000 or GPUs without AV1 NVENC

- AV1 requests → libsvtav1 (CPU, slower but correct codec)
- May timeout on long videos
- Consider running fewer concurrent miners
```

---

## Quick Start Commands for New Pod

```bash
# 1. System setup
apt-get update && apt-get install -y ffmpeg redis-server nodejs npm git python3-pip python3-venv curl wget
npm install -g pm2
redis-server --daemonize yes

# 2. VS Code persistence (optional)
mkdir -p /workspace/.vscode-server
rm -rf ~/.vscode-server 2>/dev/null; ln -s /workspace/.vscode-server ~/.vscode-server

# 3. Clone repo
cd /workspace
git clone https://github.com/vidaio-subnet/vidaio-subnet.git
cd vidaio-subnet
git checkout my-changes  # or main

# 4. Create directories
mkdir -p tmp-compressor output videos/temp_scenes services/upscaling/videos

# 5. Python environment
python3 -m venv /workspace/venv
source /workspace/venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install bittensor-cli video2x

# 6. FFmpeg with VMAF
cd /tmp
wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar xf ffmpeg-release-amd64-static.tar.xz
cp ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ffmpeg-vmaf
chmod +x /usr/local/bin/ffmpeg-vmaf
rm -rf ffmpeg-*-amd64-static*

# 7. Configure .env (edit with your credentials)
cat > /workspace/vidaio-subnet/.env << 'EOF'
BUCKET_TYPE="backblaze"
BUCKET_NAME="your-bucket"
BUCKET_COMPATIBLE_ENDPOINT="s3.region.wasabisys.com"
BUCKET_COMPATIBLE_ACCESS_KEY="your-key"
BUCKET_COMPATIBLE_SECRET_KEY="your-secret"
BUCKET_REGION="region"
WANDB_API_KEY="optional-wandb-key"
EOF

# 8. Regenerate wallet
source /workspace/venv/bin/activate
btcli wallet regen_coldkey --wallet.name your_wallet
btcli wallet regen_hotkey --wallet.name your_wallet --wallet.hotkey miner

# 9. Start services
pm2 start "source /workspace/venv/bin/activate && PYTHONPATH=/workspace/vidaio-subnet python3 /workspace/vidaio-subnet/services/compress/server.py" --name video-compressor
pm2 start "source /workspace/venv/bin/activate && PYTHONPATH=/workspace/vidaio-subnet python3 /workspace/vidaio-subnet/services/upscale/server.py" --name video-upscaler
pm2 start "source /workspace/venv/bin/activate && PYTHONPATH=/workspace/vidaio-subnet python3 /workspace/vidaio-subnet/neurons/miner.py --netuid 85 --wallet.name your_wallet --wallet.hotkey miner --axon.port 8091 --axon.external_port YOUR_EXTERNAL_PORT" --name video-miner
pm2 save
```

---

## Files Modified from Upstream

These files have custom optimizations - don't overwrite with git pull:

1. `services/compress/server.py` - Codec fallback, timing, config
2. `services/compress/utils/encoder_configs.py` - Fast presets
3. `services/compress/utils/analyze_video_fast.py` - Reduced frames
4. `services/compress/utils/encode_video.py` - Disabled contrast opt
5. `services/compress/utils/calculate_vmaf_adv.py` - Added simple VMAF
6. `services/compress/vmaf_calculator.py` - Simple VMAF integration
7. `services/miner_utilities/miner_utils.py` - Semaphore, timeouts
