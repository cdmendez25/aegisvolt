"""Persistencia del log de SAGAs en aegisvolt-sagas-<env>."""

from dataclasses import asdict, fields
from decimal import Decimal

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from aegis_common.aws import dynamodb_resource
from aegis_common.config import Settings

from .saga import SagaRecord

_FIELDS = {f.name for f in fields(SagaRecord)}


def _to_item(s: SagaRecord) -> dict:
    def conv(v):
        if isinstance(v, float):
            return Decimal(str(v))
        if isinstance(v, list):
            return [conv(x) for x in v]
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        return v

    return {k: conv(v) for k, v in asdict(s).items() if v is not None}


def _from_item(i: dict) -> SagaRecord:
    def conv(v):
        if isinstance(v, Decimal):
            return (
                int(v)
                if v == v.to_integral_value() and not isinstance(v, float) and v.as_tuple().exponent >= 0
                else float(v)
            )
        if isinstance(v, list):
            return [conv(x) for x in v]
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        return v

    data = {k: conv(v) for k, v in i.items() if k in _FIELDS}
    for k in ("energy_kwh", "amount"):
        if data.get(k) is not None:
            data[k] = float(data[k])
    return SagaRecord(**data)


class DynamoSagaStore:
    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def table(self):
        return dynamodb_resource(self.s.aws_region, self.s.aws_endpoint_url).Table(self.s.table("sagas"))

    def create(self, saga: SagaRecord) -> bool:
        try:
            self.table.put_item(Item=_to_item(saga), ConditionExpression="attribute_not_exists(saga_id)")
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get(self, saga_id: str) -> SagaRecord | None:
        item = self.table.get_item(Key={"saga_id": saga_id}).get("Item")
        return _from_item(item) if item else None

    def save(self, saga: SagaRecord) -> None:
        self.table.put_item(Item=_to_item(saga))

    def list(self, limit: int = 50, status: str | None = None) -> list[SagaRecord]:
        if status:
            items = self.table.query(
                IndexName="by_status",
                KeyConditionExpression=Key("status").eq(status),
                ScanIndexForward=False,
                Limit=limit,
            )["Items"]
        else:
            items = self.table.scan()["Items"]
        items.sort(key=lambda i: i["created_at"], reverse=True)
        return [_from_item(i) for i in items[:limit]]
