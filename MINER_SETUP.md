# Vidaio Subnet Miner Setup Guide

Quick setup guide for a new RunPod/Vast.ai pod with NVIDIA GPU.

## Prerequisites

- Ubuntu 24.04 pod with NVIDIA GPU (A6000 recommended for 1-2 miners)
- Registered wallet with hotkey on subnet 85
- S3-compatible storage (Wasabi, Backblaze, etc.)

## Quick Setup (Copy-Paste Commands)

### 1. Initial Setup

```bash
# Update and install dependencies
apt-get update && apt-get install -y ffmpeg redis-server nodejs npm git python3-pip python3-venv curl wget

# Install PM2
npm install -g pm2

# Start Redis
redis-server --daemonize yes

# VS Code persistence (optional but recommended)
mkdir -p /workspace/.vscode-server
rm -rf ~/.vscode-server 2>/dev/null; ln -s /workspace/.vscode-server ~/.vscode-server
```

### 2. Clone Repository

```bash
cd /workspace
git clone https://github.com/vidaio-subnet/vidaio-subnet.git
cd vidaio-subnet

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

#validators only
PEXELS_API_KEY="Your Pexels account api key"
WANDB_API_KEY="Your wandb api key"
EOF
```

**Edit the .env file with your actual credentials!**

### 7. Regenerate Wallet

```bash
source /workspace/venv/bin/activate

# Regenerate coldkey (enter your mnemonic when prompted)
btcli wallet regen_coldkey --wallet.name tao_reborn

# Regenerate hotkey (enter your mnemonic when prompted)
btcli wallet regen_hotkey --wallet.name tao_reborn --wallet.hotkey miner
```

### 8. Update VMAF Calculation Code

Add the FFMPEG_VMAF_BINARY constant to calculate_vmaf_adv.py:

```bash
# Check if already has the constant
grep -q "FFMPEG_VMAF_BINARY" /workspace/vidaio-subnet/services/compress/utils/calculate_vmaf_adv.py || \
sed -i '8a\\n# Default ffmpeg binary for VMAF calculations (must have libvmaf support)\nFFMPEG_VMAF_BINARY = "/usr/local/bin/ffmpeg-vmaf"' /workspace/vidaio-subnet/services/compress/utils/calculate_vmaf_adv.py
```

### 9. Start Services

```bash
source /workspace/venv/bin/activate

# Start video compressor
pm2 start "source /workspace/venv/bin/activate && PYTHONPATH=/workspace/vidaio-subnet python3 /workspace/vidaio-subnet/services/compress/server.py" --name video-compressor

# Start video upscaler
pm2 start "source /workspace/venv/bin/activate && PYTHONPATH=/workspace/vidaio-subnet python3 /workspace/vidaio-subnet/services/upscale/server.py" --name video-upscaler

# Start miner (adjust ports for your RunPod mapping)
# Internal port 8091 -> External port from RunPod (e.g., 36022)
pm2 start "source /workspace/venv/bin/activate && PYTHONPATH=/workspace/vidaio-subnet python3 /workspace/vidaio-subnet/neurons/miner.py --netuid 85 --wallet.name tao_reborn --wallet.hotkey miner --axon.port 8091 --axon.external_port 36022" --name video-miner

# Save PM2 config
pm2 save
```

### 10. Verify Setup

```bash
# Check all services running
pm2 list

# Check miner logs
pm2 logs video-miner --lines 50

# Check compressor logs
pm2 logs video-compressor --lines 50

# Test external port connectivity
nc -zv YOUR_POD_IP YOUR_EXTERNAL_PORT
```

## Port Mapping (RunPod)

In RunPod, expose TCP ports:
- `8091` (internal) → maps to external port (e.g., `36022`)

Find your mapping in RunPod dashboard under "Connect" → TCP Port Mappings.

## Recommended Miners Per GPU

| GPU | Miners |
|-----|--------|
| A6000 (48GB) | 1-2 |
| A100 (40-80GB) | 2-3 |
| H100 (80GB) | 3-4 |

## Troubleshooting

### VMAF Calculation Fails
- Ensure `/usr/local/bin/ffmpeg-vmaf` exists and has libvmaf
- Check: `/usr/local/bin/ffmpeg-vmaf -filters | grep vmaf`

### Port Not Reachable
- Verify RunPod TCP port mapping
- Use `--axon.external_port` matching the external mapped port

### S3 Upload Fails
- Use `BUCKET_TYPE="backblaze"` for Wasabi (not `amazon_s3`)
- Verify credentials in `.env`

## Monitoring Commands

```bash
pm2 logs                    # All logs
pm2 logs video-miner        # Miner logs only
pm2 logs video-compressor   # Compressor logs only
pm2 monit                   # Real-time monitoring
pm2 restart all             # Restart all services
```
