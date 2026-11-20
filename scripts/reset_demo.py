#!/usr/bin/env python3
"""Deja un ambiente listo para la demo: libera conectores y restaura saldos semilla.

NO borra el historial de SAGAs ni de transacciones (sirve como evidencia).
Uso:  uv run scripts/reset_demo.py dev
"""

import sys
from decimal import Decimal

import boto3
import httpx
from boto3.dynamodb.conditions import Attr, Key

NODE_PORTS = {"dev": 30081, "test": 30082, "prod": 30083}

SEED_BALANCES = {"FLEET-ANDES": 500, "FLEET-PACIFICO": 150, "FLEET-SINSALDO": 2}


def main(env: str) -> None:
    ddb = boto3.resource(
        "dynamodb",
        endpoint_url="http://localhost:4566",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    # 1) Cargas en curso: se finalizan por la API (dispatch stop + liberar conector + saga FINISHED)
    sagas = ddb.Table(f"aegisvolt-sagas-{env}")
    charging = sagas.query(IndexName="by_status", KeyConditionExpression=Key("status").eq("CHARGING"))["Items"]
    for s in charging:
        httpx.post(
            f"http://127.0.0.1:{NODE_PORTS[env]}/graphql",
            json={
                "query": "mutation($s:String!){ finishCharging(sagaId:$s){ status } }",
                "variables": {"s": s["saga_id"]},
            },
            timeout=15,
        )
    print(f"[{env}] cargas finalizadas: {len(charging)}")

    # 2) Cualquier conector que siga bloqueado se libera directamente
    res = ddb.Table(f"aegisvolt-reservations-{env}")
    freed = 0
    for c in res.scan(FilterExpression=Attr("entity").eq("CONNECTOR"))["Items"]:
        if c["status"] != "AVAILABLE":
            res.update_item(
                Key={"pk": c["pk"], "sk": c["sk"]},
                UpdateExpression="SET #s = :a REMOVE reserved_by, fleet_id",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":a": "AVAILABLE"},
            )
            freed += 1
    billing = ddb.Table(f"aegisvolt-billing-{env}")
    for fleet, bal in SEED_BALANCES.items():
        billing.update_item(
            Key={"pk": f"WALLET#{fleet}", "sk": "META"},
            UpdateExpression="SET balance = :b",
            ExpressionAttributeValues={":b": Decimal(bal)},
        )
    flags = ddb.Table(f"aegisvolt-flags-{env}")
    flags.update_item(
        Key={"flag_name": "SIMULATE_CHARGER_HARDWARE_FAULT"},
        UpdateExpression="SET enabled = :f",
        ExpressionAttributeValues={":f": False},
    )
    print(f"[{env}] conectores liberados: {freed}; saldos restaurados; flag de falla apagada")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dev")
