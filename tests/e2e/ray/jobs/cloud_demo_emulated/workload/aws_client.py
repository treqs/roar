"""Shared S3 client helpers for the emulated cloud-demo pipeline."""

from __future__ import annotations

import os

import boto3


def resolve_s3_endpoint() -> str | None:
    return os.getenv("AWS_ENDPOINT_URL")


def s3_client(*, endpoint_url: str | None = None):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )
