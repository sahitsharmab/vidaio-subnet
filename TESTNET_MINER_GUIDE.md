# Vidaio Subnet Testnet Miner Guide

Complete guide for setting up a testnet miner with VMAF monitoring on a new pod.

## Project Architecture Overview

```
vidaio-subnet/
├── neurons/
│   └── miner.py              # Main miner process (handles validator requests)
├── services/
│   ├── compress/
│   │   ├── server.py         # Compression service API (FastAPI)
│   │   ├── encoder.py        # AI encoding logic with CQ prediction
│   │   ├── scene_detector.py # PySceneDetect integration
│   │   ├── vmaf_calculator.py# VMAF calculation for scenes
│   │   ├── validator_merger.py# Scene merging and validation
│   │   └── utils/
│   │       ├── encode_video.py       # FFmpeg encoding wrapper
│   │       ├── encoder_configs.py    # Codec settings (CQ/CRF values)
│   │       ├── analyze_video_fast.py # OpenCV video analysis (30 frames)
│   │       ├── calculate_vmaf_adv.py # VMAF calculation (simple & advanced)
│   │       └── classify_scene.py     # PyTorch scene classifier
│   ├── upscale/
│   │   └── server.py         # Upscaling service API
│   ├── miner_utilities/
│   │   └── miner_utils.py    # Request handling (compression/upscaling calls)
│   └── scoring/
│       └── scoring_function.py # Validator scoring logic (for reference)
├── vidaio_subnet_core/
│   ├── utilities/
│   │   ├── file_handler.py   # Video download/upload utilities
│   │   └── miner_wandb.py    # WandB metrics tracking
│   └── config.py             # Service configuration
└── tmp-compressor/           # Working directory for compression jobs
```

## Request Flow

```
Validator → Miner (miner.py) → Compressor (server.py) → Upload → Validator

1. Validator sends: video_url, vmaf_threshold, target_codec, codec_mode
2. Miner receives request, forwards to compressor service
3. Compressor:
   - Downloads video
   - Analyzes video (30 frames for metrics)
   - Detects scenes (PySceneDetect)
   - Classifies scene content (PyTorch MobileNet)
   - Encodes with AI-predicted CQ values (FFmpeg + NVENC)
   - Merges scenes
   - Uploads to S3
4. Miner returns compressed_url to validator
5. Validator calculates VMAF and scores the result
```

## GPU Compatibility Analysis

### RTX A6000 (Your Current GPU)

| Feature | Status | Notes |
|---------|--------|-------|
| Compute Capability | 8.6 (Ampere) | |
| HEVC NVENC | ✅ Full support | Primary codec used |
| H264 NVENC | ✅ Full support | Fallback option |
| **AV1 NVENC** | ❌ NOT supported | Requires RTX 4000+ (Ada) |
| VRAM | 48GB | Sufficient for 2-3 miners |

### Codec Routing (Automatic)

The compressor automatically handles codec fallback in `server.py:165-232`:

```python
# When validator requests AV1:
#   - A6000: Routes to hevc_nvenc (AV1 NVENC not available)
#   - RTX 4090: Uses av1_nvenc

# Current behavior:
'av1' request → 'hevc_nvenc' (on A6000)
'hevc' request → 'hevc_nvenc'
'h264' request → 'h264_nvenc'
```

**Impact**: Your miner returns HEVC-encoded video when AV1 is requested. This should still score well since:
- HEVC has excellent compression efficiency
- The validator evaluates VMAF and compression ratio, not specific codec

### GPU Recommendations by Model

| GPU | AV1 NVENC | Miners | Notes |
|-----|-----------|--------|-------|
| RTX A6000 | ❌ | 1-2 | HEVC fallback works well |
| RTX 4090 | ✅ | 2-3 | Full AV1 support |
| RTX 4080 | ✅ | 2 | Full AV1 support |
| A100 | ❌ | 2-3 | HEVC fallback, excellent throughput |
| H100 | ❌ | 3-4 | HEVC fallback, best throughput |

## Current Optimizations Applied

These optimizations reduce response time from ~85s to ~30-35s:

### 1. Scene Classification (Enabled, Optimized)
- **File**: `services/compress/utils/analyze_video_fast.py:7`
- **Change**: `max_frames=30` (was 150)
- **Impact**: Reduces analysis from ~70s to ~10-15s
- **Why keep**: Scene classification helps choose optimal encoder settings

### 2. Contrast Optimization (Disabled)
- **File**: `services/compress/utils/encode_video.py:289-299`
- **Change**: Disabled contrast parameter calculation
- **Impact**: Saves ~2-3s per scene
- **Trade-off**: Minimal quality impact for significant speed gain

### 3. VMAF Calculation (Disabled for Production)
- **File**: `services/compress/server.py:695-708`
- **Settings**:
  ```python
  'calculate_full_video_vmaf': False,
  'calculate_scene_vmaf': False,
  ```
- **Why**: Validator calculates VMAF - miner calculation is redundant

### 4. CQ Values (Increased +4)
- **File**: `services/compress/utils/encoder_configs.py`
- **Change**: Higher CQ = smaller files, slightly lower quality
- **Impact**: Better compression ratios (scoring is 70% compression)

### 5. Fastest Encoder Presets
- **File**: `services/compress/utils/encoder_configs.py:30-37`
- **Settings**: `preset: 'p1'` for all NVENC encoders
- **Impact**: Fastest encoding speed, slightly larger files

### 6. ThreadPool Workers
- **File**: `services/compress/server.py:21`
- **Setting**: `max_workers=3`
- **Impact**: Allows concurrent compression for multiple miners

## Validator Scoring Formula

From `services/scoring/scoring_function.py`:

```
Final Score = 0.70 × compression_component + 0.30 × quality_component
```

### Zero Score Conditions (Automatic Failure)
1. **No meaningful compression**: `compression_rate >= 0.80` (less than 1.25x)
2. **VMAF below hard cutoff**: `vmaf_score < (threshold - 5)`
   - Threshold 93 → Hard cutoff 88
   - Threshold 89 → Hard cutoff 84
   - Threshold 85 → Hard cutoff 80

### Scoring Zones
- **Hard fail zone**: VMAF < (threshold - 5) → Score = 0
- **Soft zone**: (threshold - 5) ≤ VMAF < threshold → Gradual recovery
- **Full scoring**: VMAF ≥ threshold → Full compression + quality rewards

## Testnet Configuration (VMAF Monitoring Enabled)

For testnet, enable VMAF calculation to monitor quality:

### Option 1: Server Configuration (Recommended)

Edit `services/compress/server.py` lines 695-708:

```python
'vmaf_calculation': {
    'calculate_full_video_vmaf': True,   # Enable for testnet monitoring
    'calculate_scene_vmaf': True,        # Enable for scene-level VMAF
    'use_simple_vmaf': True,             # Use robust calculation method
    'vmaf_use_sampling': False,
    'vmaf_num_clips': 0,
    'vmaf_scale_factor': 0.25,
    'vmaf_target_fps': 10.0,
    'ffmpeg_vmaf_binary': '/usr/local/bin/ffmpeg-vmaf'
}
```

### Option 2: Quick Toggle

```python
# In server.py, change these lines:
'calculate_full_video_vmaf': True,   # Line 696
'calculate_scene_vmaf': True,        # Line 697
```

## Testnet Setup Instructions

### 1. Initial Pod Setup

```bash
# Update system
apt-get update && apt-get install -y ffmpeg redis-server nodejs npm git python3-pip python3-venv curl wget

# Install PM2
npm install -g pm2

# Start Redis
redis-server --daemonize yes

# VS Code persistence
mkdir -p /workspace/.vscode-server
rm -rf ~/.vscode-server 2>/dev/null; ln -s /workspace/.vscode-server ~/.vscode-server
```

### 2. Clone and Setup Repository

```bash
cd /workspace
git clone https://github.com/vidaio-subnet/vidaio-subnet.git
cd vidaio-subnet

# Switch to your optimized branch if needed
git checkout my-changes

# Create required temp/working directories
mkdir -p /workspace/vidaio-subnet/tmp-compressor
mkdir -p /workspace/vidaio-subnet/output
mkdir -p /workspace/vidaio-subnet/videos/temp_scenes
mkdir -p /workspace/vidaio-subnet/services/upscaling/videos
```

### 3. Python Environment

```bash
cd /workspace
python3 -m venv venv
source /workspace/venv/bin/activate
pip install --upgrade pip
pip install -r /workspace/vidaio-subnet/requirements.txt
pip install bittensor-cli video2x
```

### 4. Install FFmpeg with VMAF Support

```bash
cd /tmp
wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar xf ffmpeg-release-amd64-static.tar.xz
cp ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ffmpeg-vmaf
chmod +x /usr/local/bin/ffmpeg-vmaf
rm -rf ffmpeg-*-amd64-static*

# Verify VMAF support
/usr/local/bin/ffmpeg-vmaf -filters 2>&1 | grep vmaf
```

### 5. Patch ffmpeg-quality-metrics Library

```bash
source /workspace/venv/bin/activate
python3 << 'PATCH'
file_path = "/workspace/venv/lib/python3.12/site-packages/ffmpeg_quality_metrics/ffmpeg_quality_metrics.py"

with open(file_path, 'r') as f:
    content = f.read()

# Fix scale2ref filter
content = content.replace(
    '[1][0]scale=rw:rh:flags={self.scaling_algorithm}[dist]',
    '[1][0]scale2ref=flags={self.scaling_algorithm}[dist][ref]'
)
content = content.replace(
    '[0]settb=AVTB,setpts=PTS-STARTPTS[refpts]',
    '[ref]settb=AVTB,setpts=PTS-STARTPTS[refpts]'
)

# Add ffmpeg_path parameter
if 'ffmpeg_path: Union[str, None] = None' not in content:
    content = content.replace(
        'tmp_dir: Union[str, None] = None,\n    ):',
        'tmp_dir: Union[str, None] = None,\n        ffmpeg_path: Union[str, None] = None,\n    ):'
    )
    content = content.replace(
        "self.tmp_dir = str(tmp_dir) if tmp_dir is not None else tempfile.gettempdir()\n\n        if not os.path.isfile(self.ref):",
        "self.tmp_dir = str(tmp_dir) if tmp_dir is not None else tempfile.gettempdir()\n        self.ffmpeg_path = str(ffmpeg_path) if ffmpeg_path is not None else \"ffmpeg\"\n\n        if not os.path.isfile(self.ref):"
    )
    content = content.replace('cmd = ["ffmpeg", "-filters"]', 'cmd = [self.ffmpeg_path, "-filters"]')
    import re
    content = re.sub(r'cmd = \[\n\s+"ffmpeg",\n\s+"-nostdin",', 'cmd = [\n            self.ffmpeg_path,\n            "-nostdin",', content)

with open(file_path, 'w') as f:
    f.write(content)
print("Patched successfully!")
PATCH
```

### 6. Configure Environment

```bash
cat > /workspace/vidaio-subnet/.env << 'EOF'
BUCKET_TYPE="backblaze"
BUCKET_NAME="your-bucket-name"
BUCKET_COMPATIBLE_ENDPOINT="s3.eu-west-2.wasabisys.com"
BUCKET_COMPATIBLE_ACCESS_KEY="your-access-key"
BUCKET_COMPATIBLE_SECRET_KEY="your-secret-key"
BUCKET_REGION="eu-west-2"

# WandB for metrics (optional but recommended)
WANDB_API_KEY="your-wandb-api-key"
WANDB_PROJECT="vidaio-testnet-miner"
WANDB_ENTITY="your-wandb-entity"
EOF
```

### 7. Regenerate Testnet Wallet

```bash
source /workspace/venv/bin/activate

# Create new testnet wallet
btcli wallet create --wallet.name testnet_miner

# Or regenerate existing
btcli wallet regen_coldkey --wallet.name testnet_miner
btcli wallet regen_hotkey --wallet.name testnet_miner --wallet.hotkey default
```

### 8. Enable VMAF Monitoring for Testnet

```bash
# Edit server.py to enable VMAF calculation
sed -i "s/'calculate_full_video_vmaf': False/'calculate_full_video_vmaf': True/" /workspace/vidaio-subnet/services/compress/server.py
sed -i "s/'calculate_scene_vmaf': False/'calculate_scene_vmaf': True/" /workspace/vidaio-subnet/services/compress/server.py
```

### 9. Start Testnet Services

```bash
source /workspace/venv/bin/activate

# Start compressor with VMAF monitoring
pm2 start "source /workspace/venv/bin/activate && PYTHONPATH=/workspace/vidaio-subnet python3 /workspace/vidaio-subnet/services/compress/server.py" --name testnet-compressor

# Start upscaler
pm2 start "source /workspace/venv/bin/activate && PYTHONPATH=/workspace/vidaio-subnet python3 /workspace/vidaio-subnet/services/upscale/server.py" --name testnet-upscaler

# Start testnet miner (netuid 181 for testnet)
pm2 start "source /workspace/venv/bin/activate && PYTHONPATH=/workspace/vidaio-subnet python3 /workspace/vidaio-subnet/neurons/miner.py --netuid 181 --wallet.name testnet_miner --wallet.hotkey default --axon.port 8092 --axon.external_port YOUR_EXTERNAL_PORT --subtensor.network test" --name testnet-miner

pm2 save
```

### 10. Monitoring Commands

```bash
# View all logs
pm2 logs

# View compressor logs (includes VMAF scores)
pm2 logs testnet-compressor --lines 100

# Monitor in real-time
pm2 monit

# Check VMAF calculation results
pm2 logs testnet-compressor 2>&1 | grep -E "VMAF|vmaf"
```

## Expected Log Output (With VMAF Enabled)

```
📊 Part 4: Scene VMAF Calculation
   🎬 Processing 3 scenes for VMAF calculation
   ✅ Scene 1: VMAF=91.45 in 8.2s
   ✅ Scene 2: VMAF=93.21 in 6.8s
   ✅ Scene 3: VMAF=89.78 in 7.1s
   📊 VMAF Statistics:
      Average: 91.48
      Range: 89.78 - 93.21
      Target (93.0) achieved: 1/3 scenes
```

## Troubleshooting

### AV1 Codec Requested But HEVC Returned
- **Expected behavior** on A6000 (no AV1 NVENC)
- Log shows: `"Routing AV1 requests to hevc_nvenc (av1_nvenc unreliable on this GPU)"`
- Validator should still score based on VMAF and compression ratio

### VMAF Calculation Returns Garbage Values
- Symptom: VMAF scores like 2.66 instead of ~90
- Fix: Use `calculate_vmaf_simple()` with `use_simple_vmaf: True`
- Verify: `/usr/local/bin/ffmpeg-vmaf -filters | grep vmaf`

### Timeout After 90s
- Check scene classification time in logs
- Verify `max_frames=30` in `analyze_video_fast.py`
- Check FFmpeg encoding time (should be <10s per scene)

### No Response from Compressor
- Check PM2 status: `pm2 list`
- Check compressor logs: `pm2 logs testnet-compressor`
- Verify port 5001 is listening: `netstat -tlnp | grep 5001`

## Key Configuration Files

| File | Purpose |
|------|---------|
| `server.py:650-710` | Main config including VMAF settings |
| `encoder_configs.py` | Codec CQ/CRF values and presets |
| `analyze_video_fast.py:7` | `max_frames` parameter |
| `encode_video.py:289-299` | Contrast optimization toggle |
| `miner_utils.py:128-177` | Request handling with semaphore |
