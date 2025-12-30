import os
import datetime
import wandb
from dotenv import load_dotenv
from loguru import logger
from vidaio_subnet_core import __version__ as version

load_dotenv()

class MinerWandbManager:
    """Wandb manager for miner metrics tracking."""

    def __init__(self, miner=None, miner_uid=None):
        self.wandb = None
        self.wandb_start = datetime.date.today()
        self.miner = miner
        self.miner_uid = miner_uid or (miner.uid if miner else "unknown")
        self._initialized = False

        if os.getenv("WANDB_API_KEY") and os.getenv("WANDB_API_KEY") != "Your wandb api key":
            self.init_wandb()
        else:
            logger.warning("WANDB_API_KEY not set. Miner metrics will not be logged to wandb.")

    def init_wandb(self):
        """Initialize wandb for miner logging."""
        try:
            self.wandb_start = datetime.date.today()
            current = datetime.datetime.now().strftime("%Y-%m-%d_%H:%M:%S")

            name = f"miner-{self.miner_uid}--{version}--{current}"
            wandb_project = os.getenv("WANDB_PROJECT", "vidaio-miner")
            wandb_entity = os.getenv("WANDB_ENTITY", None)

            hotkey = self.miner.wallet.hotkey.ss58_address if self.miner else "unknown"

            self.wandb = wandb.init(
                name=name,
                project=wandb_project,
                entity=wandb_entity,
                config={
                    "uid": self.miner_uid,
                    "hotkey": hotkey,
                    "version": version,
                    "type": "miner",
                },
                allow_val_change=True,
                reinit=True
            )
            self._initialized = True
            logger.info(f"Initialized Wandb for miner: {name}")
        except Exception as e:
            logger.error(f"Failed to initialize wandb: {e}")
            self._initialized = False

    def log_compression(self, validator_uid: int, vmaf_threshold: float,
                        processing_time: float, success: bool,
                        vmaf_achieved: float = None, compression_ratio: float = None,
                        codec: str = None, codec_mode: str = None):
        """Log compression request metrics."""
        if not self._initialized:
            return

        try:
            metrics = {
                "compression/validator_uid": validator_uid,
                "compression/vmaf_threshold": vmaf_threshold,
                "compression/processing_time_s": processing_time,
                "compression/success": 1 if success else 0,
            }

            if vmaf_achieved is not None:
                metrics["compression/vmaf_achieved"] = vmaf_achieved
                metrics["compression/vmaf_margin"] = vmaf_achieved - vmaf_threshold

            if compression_ratio is not None:
                metrics["compression/ratio"] = compression_ratio

            if codec:
                metrics["compression/codec"] = codec

            if codec_mode:
                metrics["compression/mode"] = codec_mode

            self.wandb.log(metrics)
        except Exception as e:
            logger.error(f"Failed to log compression metrics: {e}")

    def log_upscaling(self, validator_uid: int, task_type: str,
                      processing_time: float, success: bool):
        """Log upscaling request metrics."""
        if not self._initialized:
            return

        try:
            self.wandb.log({
                "upscaling/validator_uid": validator_uid,
                "upscaling/task_type": task_type,
                "upscaling/processing_time_s": processing_time,
                "upscaling/success": 1 if success else 0,
            })
        except Exception as e:
            logger.error(f"Failed to log upscaling metrics: {e}")

    def log_request_stats(self, total_requests: int, successful_requests: int,
                          avg_processing_time: float):
        """Log aggregated request statistics."""
        if not self._initialized:
            return

        try:
            self.wandb.log({
                "stats/total_requests": total_requests,
                "stats/successful_requests": successful_requests,
                "stats/success_rate": successful_requests / total_requests if total_requests > 0 else 0,
                "stats/avg_processing_time_s": avg_processing_time,
            })
        except Exception as e:
            logger.error(f"Failed to log request stats: {e}")

    def finish(self):
        """Finish wandb run."""
        if self.wandb:
            self.wandb.finish()


# Global instance for easy access
_miner_wandb: MinerWandbManager = None

def get_miner_wandb(miner=None, miner_uid=None) -> MinerWandbManager:
    """Get or create the global miner wandb manager."""
    global _miner_wandb
    if _miner_wandb is None:
        _miner_wandb = MinerWandbManager(miner=miner, miner_uid=miner_uid)
    return _miner_wandb
