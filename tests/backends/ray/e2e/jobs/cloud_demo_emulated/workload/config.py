"""Configuration for the emulated cloud-demo pipeline."""

from __future__ import annotations

import os

SHARD_COUNT = int(os.getenv("CLOUD_DEMO_EMULATED_SHARD_COUNT", "25"))
NUM_FRAMES_PER_FILE = int(os.getenv("CLOUD_DEMO_EMULATED_FRAMES", "5"))
NUM_EPOCHS = int(os.getenv("CLOUD_DEMO_EMULATED_EPOCHS", "1"))
EVAL_LIMIT = int(os.getenv("CLOUD_DEMO_EMULATED_EVAL_LIMIT", "20"))

S3_DATA_BUCKET = os.getenv("S3_DATA_BUCKET", "test-bucket")
S3_MODELS_BUCKET = os.getenv("S3_MODELS_BUCKET", "output-bucket")
S3_RESULTS_BUCKET = os.getenv("S3_RESULTS_BUCKET", "output-bucket")
