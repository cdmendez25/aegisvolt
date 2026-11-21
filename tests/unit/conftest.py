import os

os.environ.update(
    ENV="unit",
    AWS_ACCESS_KEY_ID="test",
    AWS_SECRET_ACCESS_KEY="test",
    AWS_DEFAULT_REGION="us-east-1",
    AWS_REGION="us-east-1",
    CLOUDWATCH_ENABLED="false",
    TELEMETRY_LOOP="false",
    PEAK_HOURS="0-24",
)
os.environ.pop("AWS_ENDPOINT_URL", None)

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from aegis_common import aws as aws_factory  # noqa: E402

TABLES = ["reservations", "billing", "dispatch"]


def _create_tables(env: str = "unit") -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    for logical in TABLES:
        ddb.create_table(
            TableName=f"aegisvolt-{logical}-{env}",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            BillingMode="PAY_PER_REQUEST",
        )
    ddb.create_table(
        TableName=f"aegisvolt-flags-{env}",
        AttributeDefinitions=[{"AttributeName": "flag_name", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "flag_name", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )


def seed(env: str = "unit") -> None:
    r = boto3.resource("dynamodb", region_name="us-east-1")
    for cid, ctype, kw in [("C1", "FAST", 50), ("C3", "ULTRA", 350)]:
        r.Table(f"aegisvolt-reservations-{env}").put_item(
            Item={
                "pk": "STATION#BOG-01",
                "sk": f"CONNECTOR#{cid}",
                "entity": "CONNECTOR",
                "station_id": "BOG-01",
                "station_name": "Bogotá Norte",
                "city": "Bogotá",
                "connector_id": cid,
                "type": ctype,
                "power_kw": kw,
                "status": "AVAILABLE",
            }
        )
    for fleet, bal in [("FLEET-A", 100), ("FLEET-POOR", 1)]:
        r.Table(f"aegisvolt-billing-{env}").put_item(
            Item={
                "pk": f"WALLET#{fleet}",
                "sk": "META",
                "entity": "WALLET",
                "fleet_id": fleet,
                "name": fleet,
                "balance": bal,
                "currency": "USD",
            }
        )
    for name, enabled in [
        ("ENABLE_DYNAMIC_SURGE_PRICING", False),
        ("ENABLE_FAST_CHARGE_BOOST", False),
        ("SIMULATE_CHARGER_HARDWARE_FAULT", False),
    ]:
        r.Table(f"aegisvolt-flags-{env}").put_item(Item={"flag_name": name, "enabled": enabled})


def set_flag(name: str, enabled: bool, env: str = "unit") -> None:
    boto3.resource("dynamodb", region_name="us-east-1").Table(f"aegisvolt-flags-{env}").put_item(
        Item={"flag_name": name, "enabled": enabled}
    )


@pytest.fixture
def aws():
    with mock_aws():
        aws_factory.dynamodb_resource.cache_clear()
        aws_factory.dynamodb_client.cache_clear()
        aws_factory.client.cache_clear()
        _create_tables()
        seed()
        yield
        aws_factory.dynamodb_resource.cache_clear()
        aws_factory.dynamodb_client.cache_clear()
        aws_factory.client.cache_clear()


def _fresh_flags(app_module):
    app_module.flags._loaded_at = 0.0


@pytest.fixture
def reservation_api(aws):
    from fastapi.testclient import TestClient

    import station_reservation.app as m

    _fresh_flags(m)
    return TestClient(m.app)


@pytest.fixture
def billing_api(aws):
    from fastapi.testclient import TestClient

    import billing_wallet.app as m

    _fresh_flags(m)
    return TestClient(m.app)


@pytest.fixture
def dispatch_api(aws):
    from fastapi.testclient import TestClient

    import energy_dispatch.app as m

    _fresh_flags(m)
    return TestClient(m.app)
