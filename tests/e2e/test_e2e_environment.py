import os
import time
import uuid

import boto3
import httpx
import pytest

pytestmark = pytest.mark.e2e

ENV = os.environ.get("E2E_ENV", "dev")
BASE_URL = os.environ.get("BASE_URL", f"http://{ENV}.aegisvolt.localhost")
GRAPHQL_URL = f"{BASE_URL}/graphql"
APIGW_URL = os.environ.get("APIGW_URL")
MODE = os.environ.get("E2E_MODE", "smoke" if ENV == "prod" else "full")
AWS_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
FULL = pytest.mark.skipif(MODE != "full", reason="solo en modo full (dev/test)")


def gql(query: str, variables: dict | None = None, url: str = GRAPHQL_URL) -> dict:
    r = httpx.post(url, json={"query": query, "variables": variables or {}}, timeout=20)
    r.raise_for_status()
    body = r.json()
    assert "errors" not in body, body
    return body["data"]


def table(logical: str):
    ddb = boto3.resource(
        "dynamodb",
        endpoint_url=AWS_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    return ddb.Table(f"aegisvolt-{logical}-{ENV}")


def free_fast_connector() -> tuple[str, str]:
    for s in gql("{ stations { stationId connectors { connectorId type status } } }")["stations"]:
        for c in s["connectors"]:
            if c["type"] == "FAST" and c["status"] == "AVAILABLE":
                return s["stationId"], c["connectorId"]
    pytest.skip("No hay conectores FAST disponibles")


def rich_fleet() -> str:
    wallets = gql("{ wallets { fleetId balance } }")["wallets"]
    return max(wallets, key=lambda w: w["balance"])["fleetId"]


RESERVE = """mutation($i: ReserveAndStartChargingInput!) { reserveAndStartCharging(input: $i) {
  sagaId status failedStep errorCode amount steps { name action status } } }"""
SET_FLAG = "mutation($n:String!,$e:Boolean!){ setFeatureFlag(name:$n, enabled:$e){ enabled } }"


def test_frontend_is_served():
    r = httpx.get(BASE_URL, timeout=10)
    assert r.status_code == 200 and "AegisVolt" in r.text


def test_graphql_points_to_the_right_environment():
    env = gql("{ environment { name version } }")["environment"]
    assert env["name"] == ENV


def test_feature_flags_are_isolated_per_environment():
    flags = {f["name"]: f["enabled"] for f in gql("{ featureFlags { name enabled } }")["featureFlags"]}
    assert set(flags) == {"ENABLE_DYNAMIC_SURGE_PRICING", "ENABLE_FAST_CHARGE_BOOST", "SIMULATE_CHARGER_HARDWARE_FAULT"}
    if ENV == "prod":
        assert flags["SIMULATE_CHARGER_HARDWARE_FAULT"] is False


def test_stations_wallets_and_sagas_queries():
    data = gql("{ stations { stationId } wallets { fleetId } sagas(limit: 5) { sagaId status } }")
    assert len(data["stations"]) >= 3 and len(data["wallets"]) >= 1


@pytest.mark.skipif(not APIGW_URL, reason="APIGW_URL no definida")
def test_api_gateway_routes_to_the_same_environment():
    env = gql("{ environment { name } }", url=APIGW_URL)["environment"]
    assert env["name"] == ENV


@FULL
def test_full_saga_happy_path_and_finish():
    station, connector = free_fast_connector()
    saga = gql(
        RESERVE,
        {
            "i": {
                "stationId": station,
                "connectorId": connector,
                "fleetId": rich_fleet(),
                "energyKwh": 5,
                "idempotencyKey": f"e2e-{uuid.uuid4()}",
            }
        },
    )
    saga = saga["reserveAndStartCharging"]
    assert saga["status"] == "CHARGING", saga
    time.sleep(6)
    t = gql(
        "query($s:String!){ telemetry(sagaId:$s){ current { energyKwh } samples { energyKwh } } }",
        {"s": saga["sagaId"]},
    )["telemetry"]
    assert t["current"]["energyKwh"] > 0
    done = gql("mutation($s:String!){ finishCharging(sagaId:$s){ status } }", {"s": saga["sagaId"]})
    assert done["finishCharging"]["status"] == "FINISHED"


@FULL
def test_hardware_fault_triggers_compensation_visible_in_dynamodb():
    station, connector = free_fast_connector()
    fleet = rich_fleet()
    gql(SET_FLAG, {"n": "SIMULATE_CHARGER_HARDWARE_FAULT", "e": True})
    try:
        time.sleep(4)
        saga = gql(
            RESERVE,
            {
                "i": {
                    "stationId": station,
                    "connectorId": connector,
                    "fleetId": fleet,
                    "energyKwh": 5,
                    "idempotencyKey": f"e2e-{uuid.uuid4()}",
                }
            },
        )
        saga = saga["reserveAndStartCharging"]
    finally:
        gql(SET_FLAG, {"n": "SIMULATE_CHARGER_HARDWARE_FAULT", "e": False})

    assert saga["status"] == "COMPENSATED" and saga["errorCode"] == "CHARGER_HARDWARE_FAULT"
    sid = saga["sagaId"]
    billing = table("billing")
    charge = billing.get_item(Key={"pk": f"WALLET#{fleet}", "sk": f"TX#{sid}#CHARGE"})["Item"]
    refund = billing.get_item(Key={"pk": f"WALLET#{fleet}", "sk": f"TX#{sid}#REFUND"})["Item"]
    assert charge["status"] == "REFUNDED" and refund["amount"] == charge["amount"]
    conn = table("reservations").get_item(Key={"pk": f"STATION#{station}", "sk": f"CONNECTOR#{connector}"})["Item"]
    assert conn["status"] == "AVAILABLE" and conn["last_released_by"] == sid


@FULL
def test_insufficient_funds_compensates_reservation():
    station, connector = free_fast_connector()
    saga = gql(
        RESERVE, {"i": {"stationId": station, "connectorId": connector, "fleetId": "FLEET-SINSALDO", "energyKwh": 50}}
    )["reserveAndStartCharging"]
    assert saga["status"] == "COMPENSATED" and saga["errorCode"] == "INSUFFICIENT_FUNDS"
    assert [s["name"] for s in saga["steps"] if s["action"] == "COMPENSATE"] == ["RELEASE_CONNECTOR"]
