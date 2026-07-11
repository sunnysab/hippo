"""S3 helpers for article image storage."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

_DEFAULT_PREFIX = 'mp/image/'


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str
    prefix: str


def _normalize_prefix(value: str | None) -> str:
    trimmed = (value or '').strip()
    if not trimmed:
        trimmed = _DEFAULT_PREFIX
    if not trimmed.endswith('/'):
        trimmed += '/'
    return trimmed


def load_s3_config() -> S3Config | None:
    endpoint = os.environ.get('HIPPO_S3_ENDPOINT')
    bucket = os.environ.get('HIPPO_S3_BUCKET')
    access_key = os.environ.get('HIPPO_S3_ACCESS_KEY')
    secret_key = os.environ.get('HIPPO_S3_SECRET_KEY')
    if not endpoint or not bucket or not access_key or not secret_key:
        return None
    region = os.environ.get('HIPPO_S3_REGION') or 'us-east-1'
    prefix = _normalize_prefix(os.environ.get('HIPPO_S3_PREFIX'))
    return S3Config(
        endpoint=endpoint,
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        prefix=prefix,
    )


@lru_cache(maxsize=1)
def get_s3_client() -> tuple[S3Config, Any] | None:
    config = load_s3_config()
    if not config:
        return None
    session = boto3.session.Session()
    client = session.client(
        's3',
        endpoint_url=config.endpoint,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name=config.region,
        config=Config(s3={'addressing_style': 'path'}),
    )
    return config, client


def with_prefix(config: S3Config, prefix: str | None) -> S3Config:
    if not prefix:
        return config
    return replace(config, prefix=_normalize_prefix(prefix))


def guess_extension(content_type: str | None) -> str:
    if not content_type:
        return 'bin'
    trimmed = content_type.split(';', 1)[0].strip().lower()
    mapping = {
        'image/jpeg': 'jpg',
        'image/jpg': 'jpg',
        'image/png': 'png',
        'image/gif': 'gif',
        'image/webp': 'webp',
        'image/bmp': 'bmp',
        'image/svg+xml': 'svg',
    }
    return mapping.get(trimmed, 'bin')


def build_image_key(prefix: str, image_id: int, content_type: str | None) -> str:
    ext = guess_extension(content_type)
    return f'{prefix}{image_id}.{ext}'


def fetch_object_bytes(
    client: Any,
    *,
    bucket: str,
    key: str,
) -> tuple[bytes, str | None]:
    response = client.get_object(Bucket=bucket, Key=key)
    body = response['Body'].read()
    content_type = response.get('ContentType')
    return body, content_type


def upload_object_bytes(
    client: Any,
    *,
    bucket: str,
    key: str,
    payload: bytes,
    content_type: str | None,
) -> str | None:
    extra: dict[str, Any] = {}
    if content_type:
        extra['ContentType'] = content_type
    response = client.put_object(Bucket=bucket, Key=key, Body=payload, **extra)
    return response.get('ETag')
