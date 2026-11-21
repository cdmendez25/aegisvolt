import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from aegis_common.app import BusinessError, create_app
from aegis_common.aws import dynamodb_resource
from aegis_common.flags import HARDWARE_FAULT

from .telemetry import reading


@asynccontextmanager
async def lifespan(_app):
    task = None
    if os.environ.get("TELEMETRY_LOOP", "true") == "true":
        task = asyncio.create_task(_telemetry_loop())
    yield
    if task:
        task.cancel()


app, settings, log, flags = create_app(
    "energy-dispatch-service", "AegisVolt Energy Dispatch", readiness_tables=["dispatch"], lifespan=lifespan
)

SPEEDUP = float(os.environ.get("SIM_SPEEDUP", "60"))
TELEMETRY_INTERVAL = float(os.environ.get("TELEMETRY_INTERVAL_SECONDS", "5"))


def table():
    return dynamodb_resource(settings.aws_region, settings.aws_endpoint_url).Table(settings.table("dispatch"))


def now() -> datetime:
    return datetime.now(UTC)


class StartRequest(BaseModel):
    saga_id: str = Field(min_length=3)
    station_id: str
    connector_id: str
    fleet_id: str
    energy_kwh: Decimal = Field(gt=0)
    power_kw: int = Field(gt=0)


def _session_out(i: dict) -> dict:
    return {
        "saga_id": i["saga_id"],
        "station_id": i["station_id"],
        "connector_id": i["connector_id"],
        "fleet_id": i["fleet_id"],
        "status": i["status"],
        "energy_target_kwh": float(i["energy_kwh"]),
        "power_kw": int(i["power_kw"]),
        "started_at": i.get("started_at"),
        "stopped_at": i.get("stopped_at"),
        "energy_delivered_kwh": float(i.get("energy_delivered_kwh", 0)),
        "error_code": i.get("error_code"),
        "error_message": i.get("error_message"),
    }


def _get_session(saga_id: str) -> dict | None:
    return table().get_item(Key={"pk": f"SESSION#{saga_id}", "sk": "META"}).get("Item")


@app.post("/sessions", status_code=201)
def start_session(req: StartRequest):
    existing = _get_session(req.saga_id)
    if existing:
        if existing["status"] == "FAILED":
            raise BusinessError(existing["error_code"], existing["error_message"], 422, idempotent=True)
        return _session_out(existing)

    ts = now().isoformat(timespec="milliseconds")
    base = {
        "pk": f"SESSION#{req.saga_id}",
        "sk": "META",
        "entity": "SESSION",
        "saga_id": req.saga_id,
        "station_id": req.station_id,
        "connector_id": req.connector_id,
        "fleet_id": req.fleet_id,
        "energy_kwh": req.energy_kwh,
        "power_kw": req.power_kw,
        "created_at": ts,
    }

    if flags.is_enabled(HARDWARE_FAULT):
        item = {
            **base,
            "status": "FAILED",
            "error_code": "CHARGER_HARDWARE_FAULT",
            "error_message": f"El cargador {req.station_id}/{req.connector_id} reportó falla física "
            "(GFCI trip / contactor no cierra)",
        }
        _put_once(item)
        log.error(
            "Falla física del cargador al iniciar la carga",
            extra={
                "event": "CHARGER_HARDWARE_FAULT",
                "saga_id": req.saga_id,
                "station_id": req.station_id,
                "connector_id": req.connector_id,
            },
        )
        raise BusinessError(item["error_code"], item["error_message"], 422)

    item = {**base, "status": "ACTIVE", "started_at": ts, "energy_delivered_kwh": Decimal("0")}
    _put_once(item)
    log.info(
        f"Flujo eléctrico iniciado en {req.station_id}/{req.connector_id}",
        extra={"event": "CHARGING_STARTED", "saga_id": req.saga_id, "power_kw": req.power_kw},
    )
    return _session_out(item)


def _put_once(item: dict) -> None:
    try:
        table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


@app.post("/sessions/{saga_id}/stop")
def stop_session(saga_id: str):
    s = _get_session(saga_id)
    if not s:
        raise BusinessError("SESSION_NOT_FOUND", "Sesión no encontrada", 404)
    if s["status"] != "ACTIVE":
        return _session_out(s)
    r = _current_reading(s)
    s = table().update_item(
        Key={"pk": f"SESSION#{saga_id}", "sk": "META"},
        UpdateExpression="SET #s = :done, stopped_at = :ts, energy_delivered_kwh = :e",
        ConditionExpression="#s = :active",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":done": "COMPLETED",
            ":active": "ACTIVE",
            ":ts": now().isoformat(timespec="milliseconds"),
            ":e": Decimal(str(r.energy_kwh)),
        },
        ReturnValues="ALL_NEW",
    )["Attributes"]
    log.info(
        "Sesión de carga finalizada",
        extra={"event": "CHARGING_STOPPED", "saga_id": saga_id, "energy_kwh": r.energy_kwh},
    )
    return _session_out(s)


@app.get("/sessions")
def list_sessions(limit: int = 50):
    items = table().scan(FilterExpression=Attr("entity").eq("SESSION")).get("Items", [])
    items.sort(key=lambda i: i["created_at"], reverse=True)
    return [_session_out(i) for i in items[:limit]]


@app.get("/sessions/{saga_id}")
def get_session(saga_id: str):
    s = _get_session(saga_id)
    if not s:
        raise BusinessError("SESSION_NOT_FOUND", "Sesión no encontrada", 404)
    return _session_out(s)


def _current_reading(s: dict):
    started = datetime.fromisoformat(s["started_at"])
    end = datetime.fromisoformat(s["stopped_at"]) if s.get("stopped_at") else now()
    return reading(started, end, float(s["power_kw"]), float(s["energy_kwh"]), SPEEDUP)


@app.get("/sessions/{saga_id}/telemetry")
def telemetry(saga_id: str, limit: int = 60):
    s = _get_session(saga_id)
    if not s:
        raise BusinessError("SESSION_NOT_FOUND", "Sesión no encontrada", 404)
    samples = table().query(
        KeyConditionExpression=Key("pk").eq(f"SESSION#{saga_id}") & Key("sk").begins_with("TEL#"),
        ScanIndexForward=False,
        Limit=limit,
    )["Items"]
    current = _current_reading(s).__dict__ if s["status"] in ("ACTIVE", "COMPLETED") and s.get("started_at") else None
    return {
        "session": _session_out(s),
        "current": current,
        "samples": [
            {k: (float(v) if isinstance(v, Decimal) else v) for k, v in i.items() if k not in ("pk", "sk", "entity")}
            for i in reversed(samples)
        ],
    }


def record_telemetry_once() -> int:
    active = table().scan(FilterExpression=Attr("entity").eq("SESSION") & Attr("status").eq("ACTIVE")).get("Items", [])
    bucket = int(now().timestamp() // TELEMETRY_INTERVAL * TELEMETRY_INTERVAL)
    for s in active:
        r = _current_reading(s)
        table().put_item(
            Item={
                "pk": s["pk"],
                "sk": f"TEL#{bucket}",
                "entity": "TELEMETRY",
                **{k: (Decimal(str(v)) if isinstance(v, float) else v) for k, v in r.__dict__.items()},
            }
        )
        table().update_item(
            Key={"pk": s["pk"], "sk": "META"},
            UpdateExpression="SET energy_delivered_kwh = :e",
            ExpressionAttributeValues={":e": Decimal(str(r.energy_kwh))},
        )
    return len(active)


async def _telemetry_loop():
    while True:
        await asyncio.sleep(TELEMETRY_INTERVAL)
        try:
            await asyncio.to_thread(record_telemetry_once)
        except Exception:
            log.exception("Error registrando telemetría", extra={"event": "TELEMETRY_ERROR"})
