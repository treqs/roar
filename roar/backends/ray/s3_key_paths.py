from __future__ import annotations

_S3_KEY_PLACEHOLDER_PREFIX = "roar+s3key://"


def build_s3_path_or_placeholder(
    raw_value: str | None,
    *,
    bucket_name: str | None = None,
    bucket_hint: str = "",
) -> str | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    if text.startswith("s3://"):
        return text

    key = text.lstrip("/")
    if not key:
        return None

    bucket = str(bucket_name or "").strip()
    if bucket:
        return f"s3://{bucket}/{key}"
    if bucket_hint:
        return f"{_S3_KEY_PLACEHOLDER_PREFIX}{bucket_hint}/{key}"
    return f"{_S3_KEY_PLACEHOLDER_PREFIX}_{key}"


def parse_s3_key_placeholder(path: str) -> tuple[str, str] | None:
    text = str(path or "").strip()
    if not text.startswith(_S3_KEY_PLACEHOLDER_PREFIX):
        return None

    remainder = text[len(_S3_KEY_PLACEHOLDER_PREFIX) :]
    bucket_hint, separator, key = remainder.partition("/")
    if not separator or not key:
        return None
    return bucket_hint, key


def s3_object_key(path: str) -> str | None:
    text = str(path or "").strip()
    if not text:
        return None

    placeholder = parse_s3_key_placeholder(text)
    if placeholder is not None:
        return placeholder[1]

    if not text.startswith("s3://"):
        return None
    remainder = text[len("s3://") :]
    _bucket, separator, key = remainder.partition("/")
    if not separator or not key:
        return None
    return key
