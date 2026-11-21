from datetime import UTC, datetime

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from aegis_common.app import BusinessError, create_app
from aegis_common.aws import dynamodb_client, dynamodb_resource
from aegis_common.flags import FAST_CHARGE_BOOST

app, settings, log, flags = create_app(
    "station-reservation-service", "AegisVolt Station Reservation", readiness_tables=["reservations"]
)

TABLE_LOGICAL = "reservations"


def table():
    return dynamodb_resource(settings.aws_region, settings.aws_endpoint_url).Table(settings.table(TABLE_LOGICAL))


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class ReserveRequest(BaseModel):
    saga_id: str = Field(min_length=3)
    station_id: str
    connector_id: str
    fleet_id: str


class ReleaseRequest(BaseModel):
    reason: str = "COMPENSATION"


def _connector_out(i: dict) -> dict:
    return {
        "station_id": i["station_id"],
        "connector_id": i["connector_id"],
        "type": i["type"],
        "power_kw": int(i["power_kw"]),
        "status": i["status"],
        "reserved_by": i.get("reserved_by"),
        "fleet_id": i.get("fleet_id"),
        "updated_at": i.get("updated_at"),
    }


@app.get("/stations")
def list_stations():
    items = table().scan(FilterExpression=Attr("entity").eq("CONNECTOR")).get("Items", [])
    stations: dict[str, dict] = {}
    for i in items:
        st = stations.setdefault(
            i["station_id"],
            {"station_id": i["station_id"], "name": i["station_name"], "city": i["city"], "connectors": []},
        )
        st["connectors"].append(_connector_out(i))
    for st in stations.values():
        st["connectors"].sort(key=lambda c: c["connector_id"])
    return sorted(stations.values(), key=lambda s: s["station_id"])


def _get_connector(station_id: str, connector_id: str) -> dict:
    item = table().get_item(Key={"pk": f"STATION#{station_id}", "sk": f"CONNECTOR#{connector_id}"}).get("Item")
    if not item:
        raise BusinessError("CONNECTOR_NOT_FOUND", f"Conector {station_id}/{connector_id} no existe", 404)
    return item


def _get_reservation(saga_id: str) -> dict | None:
    return table().get_item(Key={"pk": f"RESERVATION#{saga_id}", "sk": "META"}).get("Item")


@app.get("/reservations/{saga_id}")
def get_reservation(saga_id: str):
    r = _get_reservation(saga_id)
    if not r:
        raise BusinessError("RESERVATION_NOT_FOUND", "Reserva no encontrada", 404)
    return r


@app.post("/reservations", status_code=201)
def reserve(req: ReserveRequest):
    connector = _get_connector(req.station_id, req.connector_id)

    if connector["type"] == "ULTRA" and not flags.is_enabled(FAST_CHARGE_BOOST):
        raise BusinessError(
            "ULTRA_CHARGE_DISABLED",
            f"La reserva prioritaria de ultra-carga está deshabilitada en {settings.env}",
            409,
        )

    existing = _get_reservation(req.saga_id)
    if existing and existing["status"] in ("RESERVED", "CHARGING"):
        log.info("Reserva repetida (idempotente)", extra={"event": "RESERVATION_IDEMPOTENT_HIT"})
        return existing
    if existing and existing["status"] == "RELEASED":
        raise BusinessError("RESERVATION_ALREADY_FINALIZED", "Esta SAGA ya liberó su reserva", 409)

    ts = now()
    ddb = dynamodb_client(settings.aws_region, settings.aws_endpoint_url)
    tname = settings.table(TABLE_LOGICAL)
    reservation = {
        "pk": f"RESERVATION#{req.saga_id}",
        "sk": "META",
        "entity": "RESERVATION",
        "saga_id": req.saga_id,
        "station_id": req.station_id,
        "connector_id": req.connector_id,
        "fleet_id": req.fleet_id,
        "connector_type": connector["type"],
        "power_kw": int(connector["power_kw"]),
        "status": "RESERVED",
        "created_at": ts,
        "updated_at": ts,
    }
    try:
        ddb.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": tname,
                        "Key": {
                            "pk": {"S": f"STATION#{req.station_id}"},
                            "sk": {"S": f"CONNECTOR#{req.connector_id}"},
                        },
                        "UpdateExpression": "SET #s = :reserved, reserved_by = :saga, fleet_id = :fleet, "
                        "updated_at = :ts",
                        "ConditionExpression": "#s = :available",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {
                            ":reserved": {"S": "RESERVED"},
                            ":available": {"S": "AVAILABLE"},
                            ":saga": {"S": req.saga_id},
                            ":fleet": {"S": req.fleet_id},
                            ":ts": {"S": ts},
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": tname,
                        "Item": _to_ddb(reservation),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ]
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            raise BusinessError(
                "CONNECTOR_NOT_AVAILABLE",
                f"El conector {req.station_id}/{req.connector_id} no está disponible",
                409,
            ) from exc
        raise

    log.info(
        f"Conector {req.station_id}/{req.connector_id} RESERVED",
        extra={"event": "CONNECTOR_RESERVED", "saga_id": req.saga_id, "fleet_id": req.fleet_id},
    )
    return reservation


@app.post("/reservations/{saga_id}/confirm")
def confirm(saga_id: str):
    r = _get_reservation(saga_id)
    if not r:
        raise BusinessError("RESERVATION_NOT_FOUND", "Reserva no encontrada", 404)
    if r["status"] == "CHARGING":
        return r
    ts = now()
    try:
        table().update_item(
            Key={"pk": f"STATION#{r['station_id']}", "sk": f"CONNECTOR#{r['connector_id']}"},
            UpdateExpression="SET #s = :charging, updated_at = :ts",
            ConditionExpression="reserved_by = :saga",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":charging": "CHARGING", ":saga": saga_id, ":ts": ts},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise BusinessError("RESERVATION_LOST", "El conector ya no pertenece a esta SAGA", 409) from exc
        raise
    r = table().update_item(
        Key={"pk": f"RESERVATION#{saga_id}", "sk": "META"},
        UpdateExpression="SET #s = :charging, updated_at = :ts",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":charging": "CHARGING", ":ts": ts},
        ReturnValues="ALL_NEW",
    )["Attributes"]
    log.info("Conector en CHARGING", extra={"event": "CONNECTOR_CHARGING", "saga_id": saga_id})
    return r


@app.post("/reservations/{saga_id}/release")
def release(saga_id: str, req: ReleaseRequest | None = None):
    reason = (req.reason if req else "COMPENSATION").upper()
    r = _get_reservation(saga_id)
    if not r:
        log.info("Compensación sin reserva previa: no-op", extra={"event": "RELEASE_NOOP", "saga_id": saga_id})
        return {"saga_id": saga_id, "status": "NOOP", "detail": "no reservation existed"}
    if r["status"] == "RELEASED":
        log.info("Reserva ya liberada: no-op idempotente", extra={"event": "RELEASE_IDEMPOTENT_HIT"})
        return {**r, "idempotent": True}

    ts = now()
    try:
        table().update_item(
            Key={"pk": f"STATION#{r['station_id']}", "sk": f"CONNECTOR#{r['connector_id']}"},
            UpdateExpression="SET #s = :available, updated_at = :ts, last_released_by = :saga "
            "REMOVE reserved_by, fleet_id",
            ConditionExpression="reserved_by = :saga",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":available": "AVAILABLE", ":saga": saga_id, ":ts": ts},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        log.warning("El conector ya no estaba asignado a esta SAGA", extra={"event": "RELEASE_CONNECTOR_SKIPPED"})

    try:
        r = table().update_item(
            Key={"pk": f"RESERVATION#{saga_id}", "sk": "META"},
            UpdateExpression="SET #s = :released, released_reason = :reason, updated_at = :ts",
            ConditionExpression="#s <> :released",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":released": "RELEASED", ":reason": reason, ":ts": ts},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return {**_get_reservation(saga_id), "idempotent": True}
        raise
    log.info(
        f"Conector {r['station_id']}/{r['connector_id']} AVAILABLE ({reason})",
        extra={"event": "CONNECTOR_RELEASED", "saga_id": saga_id, "reason": reason},
    )
    return r


def _to_ddb(item: dict) -> dict:
    from boto3.dynamodb.types import TypeSerializer

    ser = TypeSerializer()
    return {k: ser.serialize(v) for k, v in item.items()}
