import os
import uuid

ENV = f"it{uuid.uuid4().hex[:6]}"
ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
os.environ.update(
    ENV=ENV,
    AWS_ENDPOINT_URL=ENDPOINT,
    AWS_ACCESS_KEY_ID="test",
    AWS_SECRET_ACCESS_KEY="test",
    AWS_REGION="us-east-1",
    AWS_DEFAULT_REGION="us-east-1",
    CLOUDWATCH_ENABLED="false",
    TELEMETRY_LOOP="false",
    PEAK_HOURS="0-24",
    FLAG_ADMIN_ENABLED="true",
)

import boto3  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from boto3.dynamodb.types import TypeSerializer  # noqa: E402

PK_SK = dict(
    AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
    KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
)


def _ddb():
    return boto3.client("dynamodb", endpoint_url=ENDPOINT, region_name="us-east-1")


def _create_tables():
    ddb = _ddb()
    for logical in ("reservations", "billing", "dispatch"):
        ddb.create_table(TableName=f"aegisvolt-{logical}-{ENV}", BillingMode="PAY_PER_REQUEST", **PK_SK)
    ddb.create_table(
        TableName=f"aegisvolt-flags-{ENV}",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "flag_name", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "flag_name", "KeyType": "HASH"}],
    )
    ddb.create_table(
        TableName=f"aegisvolt-sagas-{ENV}",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": a, "AttributeType": "S"} for a in ("saga_id", "status", "created_at", "idempotency_key")
        ],
        KeySchema=[{"AttributeName": "saga_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "by_status",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "by_idempotency_key",
                "KeySchema": [{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )


def _seed():
    ser = TypeSerializer()
    ddb = _ddb()

    def put(table, item):
        ddb.put_item(TableName=f"aegisvolt-{table}-{ENV}", Item={k: ser.serialize(v) for k, v in item.items()})

    for cid, ctype, kw in [("C1", "FAST", 50), ("C2", "FAST", 50), ("C3", "ULTRA", 350)]:
        put(
            "reservations",
            {
                "pk": "STATION#IT-01",
                "sk": f"CONNECTOR#{cid}",
                "entity": "CONNECTOR",
                "station_id": "IT-01",
                "station_name": "Estación Integración",
                "city": "Bogotá",
                "connector_id": cid,
                "type": ctype,
                "power_kw": kw,
                "status": "AVAILABLE",
            },
        )
    for fleet, bal in [("FLEET-IT", 200), ("FLEET-BROKE", 1)]:
        put(
            "billing",
            {
                "pk": f"WALLET#{fleet}",
                "sk": "META",
                "entity": "WALLET",
                "fleet_id": fleet,
                "name": fleet,
                "balance": bal,
                "currency": "USD",
            },
        )
    for name in ("ENABLE_DYNAMIC_SURGE_PRICING", "ENABLE_FAST_CHARGE_BOOST", "SIMULATE_CHARGER_HARDWARE_FAULT"):
        put("flags", {"flag_name": name, "enabled": False, "description": name})


@pytest.fixture(scope="session")
def floci_tables():
    try:
        _ddb().list_tables(Limit=1)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Floci no disponible en {ENDPOINT}: {exc}")
    _create_tables()
    _seed()
    yield ENV
    ddb = _ddb()
    for logical in ("reservations", "billing", "dispatch", "flags", "sagas"):
        ddb.delete_table(TableName=f"aegisvolt-{logical}-{ENV}")


@pytest.fixture(scope="session")
def gateway(floci_tables):
    from fastapi.testclient import TestClient

    import billing_wallet.app as billing
    import energy_dispatch.app as dispatch
    import graphql_gateway.app as gw
    import station_reservation.app as reservation
    from graphql_gateway.clients import ServiceClient, Services

    services = Services(
        reservation=ServiceClient("reservation", "http://reservation", transport=httpx.ASGITransport(reservation.app)),
        billing=ServiceClient("billing", "http://billing", transport=httpx.ASGITransport(billing.app)),
        dispatch=ServiceClient("dispatch", "http://dispatch", transport=httpx.ASGITransport(dispatch.app)),
    )
    gw.app.state.deps = gw.build_deps(services)
    for m in (reservation, billing, dispatch, gw):
        m.flags.ttl = 0
    return TestClient(gw.app)


@pytest.fixture
def gql(gateway):
    def run(query: str, variables: dict | None = None) -> dict:
        r = gateway.post("/graphql", json={"query": query, "variables": variables or {}})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "errors" not in body, body["errors"]
        return body["data"]

    return run


@pytest.fixture
def table():
    res = boto3.resource("dynamodb", endpoint_url=ENDPOINT, region_name="us-east-1")
    return lambda logical: res.Table(f"aegisvolt-{logical}-{ENV}")
