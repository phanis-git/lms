"""S3-compatible private object storage (AWS S3 in production, MinIO locally)."""

import logging
import math
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings

log = logging.getLogger(__name__)
MAX_PARTS = 10_000


def _client(endpoint: str | None):
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key_id,
        aws_secret_access_key=s.s3_secret_access_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path" if endpoint else "auto"}),
    )


@lru_cache
def internal_client():
    return _client(get_settings().s3_endpoint_url)


def _presign_client(public_origin: str | None):
    s = get_settings()
    if s.s3_presign_same_origin and public_origin:
        return _client(public_origin)
    return _client(s.s3_public_endpoint_url or s.s3_endpoint_url)


def ensure_bucket() -> None:
    s = get_settings()
    c = internal_client()
    try:
        c.head_bucket(Bucket=s.s3_bucket)
    except ClientError:
        c.create_bucket(Bucket=s.s3_bucket)
        log.info("Created bucket %s", s.s3_bucket)


def part_size_for(size_bytes: int) -> int:
    base = get_settings().multipart_part_size_mb * 1024 * 1024
    return max(base, math.ceil(size_bytes / MAX_PARTS))


def start_multipart(key: str, content_type: str) -> str:
    s = get_settings()
    res = internal_client().create_multipart_upload(Bucket=s.s3_bucket, Key=key, ContentType=content_type)
    return res["UploadId"]


def presign_parts(key: str, upload_id: str, part_count: int, public_origin: str | None) -> list[str]:
    s = get_settings()
    c = _presign_client(public_origin)
    return [
        c.generate_presigned_url(
            "upload_part",
            Params={"Bucket": s.s3_bucket, "Key": key, "UploadId": upload_id, "PartNumber": n},
            ExpiresIn=s.upload_url_ttl_seconds,
        )
        for n in range(1, part_count + 1)
    ]


def complete_multipart(key: str, upload_id: str, parts: list[dict]) -> None:
    s = get_settings()
    internal_client().complete_multipart_upload(
        Bucket=s.s3_bucket,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": [{"PartNumber": p["part_number"], "ETag": p["etag"]} for p in parts]},
    )


def abort_multipart(key: str, upload_id: str) -> None:
    s = get_settings()
    try:
        internal_client().abort_multipart_upload(Bucket=s.s3_bucket, Key=key, UploadId=upload_id)
    except ClientError:
        log.info("Multipart upload already gone for %s", key)


def head(key: str) -> dict | None:
    s = get_settings()
    try:
        return internal_client().head_object(Bucket=s.s3_bucket, Key=key)
    except ClientError:
        return None


def read_prefix(key: str, nbytes: int = 64) -> bytes:
    s = get_settings()
    res = internal_client().get_object(Bucket=s.s3_bucket, Key=key, Range=f"bytes=0-{nbytes - 1}")
    return res["Body"].read()


def put_object(key: str, body, content_type: str) -> None:
    s = get_settings()
    internal_client().upload_fileobj(body, s.s3_bucket, key, ExtraArgs={"ContentType": content_type})


def delete_object(key: str) -> None:
    s = get_settings()
    try:
        internal_client().delete_object(Bucket=s.s3_bucket, Key=key)
    except ClientError:
        log.warning("Failed to delete object")


def _cloudfront_url(key: str, ttl: int) -> str | None:
    s = get_settings()
    if not (s.cloudfront_domain and s.cloudfront_key_pair_id and s.cloudfront_private_key_path):
        return None
    from botocore.signers import CloudFrontSigner
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    with open(s.cloudfront_private_key_path, "rb") as fh:
        private_key = serialization.load_pem_private_key(fh.read(), password=None)

    def rsa_signer(message: bytes) -> bytes:
        return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303 - CloudFront requires SHA1

    signer = CloudFrontSigner(s.cloudfront_key_pair_id, rsa_signer)
    return signer.generate_presigned_url(
        f"https://{s.cloudfront_domain}/{key}", date_less_than=datetime.now(UTC) + timedelta(seconds=ttl)
    )


def signed_get_url(
    key: str,
    public_origin: str | None,
    filename: str | None = None,
    download: bool = False,
    ttl: int | None = None,
) -> str:
    s = get_settings()
    ttl = ttl or s.playback_url_ttl_seconds
    if not download:
        cf = _cloudfront_url(key, ttl)
        if cf:
            return cf
    params: dict = {"Bucket": s.s3_bucket, "Key": key}
    if download and filename:
        safe = filename.replace('"', "")
        params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
    return _presign_client(public_origin).generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)


def origin_from(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
