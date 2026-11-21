#!/usr/bin/env python3

import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3


def main(env: str) -> None:
    ddb = boto3.resource(
        "dynamodb",
        endpoint_url="http://localhost:4566",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    sid = str(uuid.uuid4())
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(timespec="milliseconds")
    station, connector, fleet, amount = "MDE-01", "C1", "FLEET-PACIFICO", Decimal("9.00")

    res = ddb.Table(f"aegisvolt-reservations-{env}")
    res.update_item(
        Key={"pk": f"STATION#{station}", "sk": f"CONNECTOR#{connector}"},
        UpdateExpression="SET #s = :r, reserved_by = :sid, fleet_id = :f",
        ConditionExpression="#s = :a",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "RESERVED", ":a": "AVAILABLE", ":sid": sid, ":f": fleet},
    )
    res.put_item(
        Item={
            "pk": f"RESERVATION#{sid}",
            "sk": "META",
            "entity": "RESERVATION",
            "saga_id": sid,
            "station_id": station,
            "connector_id": connector,
            "fleet_id": fleet,
            "connector_type": "FAST",
            "power_kw": 60,
            "status": "RESERVED",
            "created_at": old,
            "updated_at": old,
        }
    )

    bill = ddb.Table(f"aegisvolt-billing-{env}")
    bill.put_item(
        Item={
            "pk": f"WALLET#{fleet}",
            "sk": f"TX#{sid}#CHARGE",
            "entity": "TX",
            "type": "CHARGE",
            "saga_id": sid,
            "fleet_id": fleet,
            "amount": amount,
            "status": "CAPTURED",
            "created_at": old,
            "energy_kwh": Decimal(30),
            "price_per_kwh": Decimal("0.30"),
        }
    )
    bill.update_item(
        Key={"pk": f"WALLET#{fleet}", "sk": "META"},
        UpdateExpression="SET balance = balance - :a",
        ExpressionAttributeValues={":a": amount},
    )

    ddb.Table(f"aegisvolt-sagas-{env}").put_item(
        Item={
            "saga_id": sid,
            "status": "PAID",
            "station_id": station,
            "connector_id": connector,
            "fleet_id": fleet,
            "energy_kwh": Decimal(30),
            "amount": amount,
            "idempotency_key": f"orphan-{sid}",
            "created_at": old,
            "updated_at": old,
            "environment": env,
            "connector_type": "FAST",
            "power_kw": 60,
            "steps": [
                {
                    "name": "RESERVE_CONNECTOR",
                    "action": "EXECUTE",
                    "status": "SUCCEEDED",
                    "detail": "Conector RESERVED",
                    "attempts": 1,
                    "at": old,
                },
                {
                    "name": "CHARGE_WALLET",
                    "action": "EXECUTE",
                    "status": "SUCCEEDED",
                    "detail": "Cobrado 9.00 USD",
                    "attempts": 1,
                    "at": old,
                },
            ],
        }
    )
    print(f"SAGA huérfana creada en {env}: {sid}  (PAID hace 10 min, conector {station}/{connector} bloqueado)")
    print("Ejecuta el flow en Prefect o espera la siguiente corrida programada (cada 5 min).")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dev")
