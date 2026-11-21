from functools import lru_cache

import boto3
from botocore.config import Config

_BOTO_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=2, read_timeout=5)


def _kwargs(region: str, endpoint: str | None) -> dict:
    kw = {"region_name": region, "config": _BOTO_CONFIG}
    if endpoint:
        kw["endpoint_url"] = endpoint
    return kw


@lru_cache(maxsize=8)
def dynamodb_resource(region: str, endpoint: str | None):
    return boto3.resource("dynamodb", **_kwargs(region, endpoint))


@lru_cache(maxsize=8)
def dynamodb_client(region: str, endpoint: str | None):
    return boto3.client("dynamodb", **_kwargs(region, endpoint))


@lru_cache(maxsize=8)
def client(service: str, region: str, endpoint: str | None):
    return boto3.client(service, **_kwargs(region, endpoint))
