from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

CATEGORIES = {
    "phigros": {"avatars", "charts", "illustrations", "illustrations-blur", "illustrations-lowres", "music", "metadata"},
    "rizline": {"covers", "audio", "charts", "metadata"},
    "kyou": {"data"},
}
BUCKETS = {"phigros": "rranker-phigros-data", "kyou": "rranker-phigros-data", "rizline": "rranker-rizline-data", "apk": "rranker", "dxtag": "rranker-maimai-data"}


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
        self._game = game
        self.bucket = os.environ["S3_BUCKET_NAME"]
        if self.bucket != BUCKETS[game]:
            raise ValueError(f"Wrong bucket for {game}")
        self.client = boto3.client(
            "s3", endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
            region_name=os.environ.get("S3_REGION", "us-east-1"),
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual" if game == "kyou" else "path"},
                          retries={"max_attempts": 4, "mode": "standard"}, max_pool_connections=16,
                          request_checksum_calculation="when_required", response_checksum_validation="when_required"),
        )
        if game == "kyou":
            def send_body(request, **kwargs):
                request.headers.pop("Expect", None)
            for operation in ("PutObject", "UploadPart"):
                self.client.meta.events.register(f"before-send.s3.{operation}", send_body)

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
        position = body.tell() if hasattr(body, "tell") else None
        sftp = self._game == "kyou" and not (etag or absent)
        multipart = self._game == "kyou" and immutable and bool(sha256)
        for attempt in range(3):
            try:
                if sftp:
                    return self._sftp_put(args)
                return self._multipart_put(args) if multipart else self.client.put_object(**args)["ETag"]
            except ClientError as error:
                status = error.response["ResponseMetadata"]["HTTPStatusCode"]
                if not (sftp or etag or absent or (immutable and sha256)) or status not in (404, 409, 412, 500, 502, 503, 504):
                    raise
                head = self.head(key)
                if head and (absent or head["ETag"] != etag) and (not sftp or (
                    head.get("ContentType") == args["ContentType"] and head.get("CacheControl") == args["CacheControl"]
                    and head.get("Metadata", {}) == args.get("Metadata", {})
                )):
                    if position is None:
                        size, expected = len(body), digest(body)
                    else:
                        body.seek(0, 2)
                        size = body.tell() - position
                        body.seek(position)
                        expected = sha256 or hashlib.file_digest(body, "sha256").hexdigest()
                        body.seek(position)
                    try:
                        return self.verify(key, size, expected, head["ETag"])
                    except ValueError:
                        raise error
                if status == 412 or (etag and head is None) or attempt == 2:
                    raise
                if immutable and sha256 and not etag and status >= 500:
                    multipart = True
                print(f"Retrying object write after HTTP {status}: {key}", flush=True)
                time.sleep(2 ** (attempt + 1))
                if position is not None:
                    body.seek(position)

    def _sftp_put(self, args):
        import paramiko

        endpoint = urlsplit(os.environ["SFTP_ENDPOINT"])
        if endpoint.scheme != "sftp" or not endpoint.hostname:
            raise ValueError("Invalid SFTP endpoint")
        key_type, key_data = os.environ["SFTP_HOST_KEY"].split()[:2]
        host_key = paramiko.PKey.from_type_string(key_type, base64.b64decode(key_data))
        port = endpoint.port or 22
        host = endpoint.hostname if port == 22 else f"[{endpoint.hostname}]:{port}"
        source = args["Body"] if hasattr(args["Body"], "read") else io.BytesIO(args["Body"])
        print(f"Uploading through SFTP: {args['Key']}", flush=True)
        with paramiko.SSHClient() as ssh:
            ssh.get_host_keys().add(host, host_key.get_name(), host_key)
            ssh.connect(endpoint.hostname, port=port, username=os.environ["S3_ACCESS_KEY_ID"],
                        password=os.environ["S3_SECRET_ACCESS_KEY"], allow_agent=False, look_for_keys=False,
                        timeout=30, banner_timeout=30, auth_timeout=30)
            with ssh.open_sftp() as sftp:
                sftp.get_channel().settimeout(120)
                sftp.putfo(source, f"/{self.bucket}/{args['Key']}", confirm=False)
        head = self.head(args["Key"])
        if head is None:
            raise ValueError(f"SFTP upload is not readable: {args['Key']}")
        return self.client.copy_object(
            Bucket=self.bucket, Key=args["Key"], CopySource={"Bucket": self.bucket, "Key": args["Key"]},
            CopySourceIfMatch=head["ETag"], MetadataDirective="REPLACE", Metadata=args.get("Metadata", {}),
            ContentType=args["ContentType"], CacheControl=args["CacheControl"],
        )["CopyObjectResult"]["ETag"]

    def _multipart_put(self, args):
        target = {name: args[name] for name in ("Bucket", "Key")}
        print(f"Starting multipart upload: {args['Key']}", flush=True)
        upload = self.client.create_multipart_upload(**{name: value for name, value in args.items()
                                                       if name not in ("Body", "IfMatch", "IfNoneMatch")})
        upload_id = upload["UploadId"]
        source = args["Body"] if hasattr(args["Body"], "read") else io.BytesIO(args["Body"])
        try:
            parts = []
            while chunk := source.read(8 * 1024 * 1024):
                print(f"Uploading part {len(parts) + 1} ({len(chunk)} bytes): {args['Key']}", flush=True)
                part = self.client.upload_part(**target, UploadId=upload_id, PartNumber=len(parts) + 1, Body=chunk)
                parts.append({"PartNumber": len(parts) + 1, "ETag": part["ETag"]})
            print(f"Completing multipart upload: {args['Key']}", flush=True)
            response = self.client.complete_multipart_upload(
                **target, UploadId=upload_id, MultipartUpload={"Parts": parts},
                **{name: args[name] for name in ("IfMatch", "IfNoneMatch") if name in args})
            upload_id = None
            return response["ETag"]
        finally:
            if upload_id:
                try:
                    self.client.abort_multipart_upload(**target, UploadId=upload_id)
                except ClientError as error:
                    if error.response["Error"]["Code"] != "NoSuchUpload":
                        raise

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
