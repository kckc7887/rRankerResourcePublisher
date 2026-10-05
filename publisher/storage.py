from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

CATEGORIES = {
    "phigros": {"avatars", "charts", "illustrations", "illustrations-blur", "illustrations-lowres", "music", "metadata"},
    "rizline": {"covers", "audio", "charts", "metadata"},
    "kyou": {"data"},
}
BUCKETS = {"phigros": "rranker-phigros-data", "kyou": "rranker-phigros-data", "rizline": "rranker-rizline-data", "apk": "rranker"}


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def safe_path(key):
    if not isinstance(key, str) or any(part in ("", ".", "..") for part in key.split("/")) or "\\" in key or ":" in key:
        raise ValueError(f"Invalid object path: {key!r}")
    return key


def owned_key(game, key, manifests=False):
    safe_path(key)
    parts = key.split("/")
    categories = CATEGORIES[game] | ({"manifests"} if manifests else set())
    if len(parts) != 3 or parts[0] != game or parts[1] not in categories or not re.fullmatch(r"[0-9a-f]{64}\.[a-z0-9]+", parts[2]):
        raise ValueError(f"Object outside managed hash directories: {key}")
    if parts[1] == "manifests" and not key.endswith(".json"):
        raise ValueError("Manifest must be JSON")
    return key


def object_key(game, logical, sha256):
    safe_path(logical)
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("Invalid SHA-256")
    category = logical.split("/")[0]
    if game == "kyou":
        category = "data"
    elif category not in CATEGORIES[game]:
        if logical != "catalog.json":
            raise ValueError(f"Unsupported resource category: {logical}")
        category = "metadata"
    suffix = Path(logical).suffix.lower()
    return owned_key(game, f"{game}/{category}/{sha256}{suffix}")


def parallel(function, items, workers=8):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(function, items))


class Storage:
    def __init__(self, game):
        self.bucket = os.environ["S3_BUCKET_NAME"]
        if self.bucket != BUCKETS[game]:
            raise ValueError(f"Wrong bucket for {game}")
        self.client = boto3.client(
            "s3", endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
            region_name=os.environ.get("S3_REGION", "us-east-1"),
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          retries={"max_attempts": 4, "mode": "standard"}, max_pool_connections=16,
                          request_checksum_calculation="when_required", response_checksum_validation="when_required"),
        )

    def head(self, key):
        try:
            return self.client.head_object(Bucket=self.bucket, Key=safe_path(key))
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                return None
            raise

    def get(self, key, etag=None):
        args = {"Bucket": self.bucket, "Key": safe_path(key)}
        if etag:
            args["IfMatch"] = etag
        response = self.client.get_object(**args)
        try:
            return response["Body"].read(), response["ETag"]
        finally:
            response["Body"].close()

    def json(self, key):
        data, etag = self.get(key)
        return json.loads(data), etag, data

    def list(self, prefix):
        result = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for row in page.get("Contents", []):
                result[row["Key"]] = row
        return result

    def verify(self, key, size, sha256, etag=None):
        args = {"Bucket": self.bucket, "Key": safe_path(key)}
        if etag:
            args["IfMatch"] = etag
        response = self.client.get_object(**args)
        actual = hashlib.sha256()
        count = 0
        try:
            while chunk := response["Body"].read(4 * 1024 * 1024):
                actual.update(chunk)
                count += len(chunk)
        finally:
            response["Body"].close()
        if count != size or actual.hexdigest() != sha256:
            raise ValueError(f"Content verification failed: {key}")
        return response["ETag"]

    def put(self, key, body, content_type, *, etag=None, absent=False, sha256=None, immutable=False):
        args = {"Bucket": self.bucket, "Key": safe_path(key), "Body": body, "ContentType": content_type,
                "CacheControl": "public,max-age=31536000,immutable" if immutable else "no-cache,no-store,must-revalidate"}
        if etag:
            args["IfMatch"] = etag
        if absent:
            args["IfNoneMatch"] = "*"
        if sha256:
            args["Metadata"] = {"sha256": sha256}
        return self.client.put_object(**args)["ETag"]

    def copy(self, source, etag, target, sha256, content_type, *, immutable=True):
        return self.client.copy_object(
            Bucket=self.bucket, Key=safe_path(target), CopySource={"Bucket": self.bucket, "Key": safe_path(source)},
            CopySourceIfMatch=etag, MetadataDirective="REPLACE", Metadata={"sha256": sha256},
            ContentType=content_type, CacheControl="public,max-age=31536000,immutable" if immutable else "no-cache,no-store,must-revalidate",
        )["CopyObjectResult"]["ETag"]

    def delete(self, key, etag):
        head = self.head(key)
        if head is None:
            return
        if head["ETag"] != etag:
            raise ValueError(f"Cleanup object changed: {key}")
        self.client.delete_object(Bucket=self.bucket, Key=safe_path(key), IfMatch=etag)
