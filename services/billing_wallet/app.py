"""billing-wallet-service

Dueño ÚNICO de la tabla aegisvolt-billing-<env>.
PASO 2 de la SAGA (cobro) y su transacción compensatoria (reembolso).

Modelo:
  pk=WALLET#<fleet> sk=META               -> billetera (balance)
  pk=WALLET#<fleet> sk=TX#<saga>#CHARGE   -> cobro (status CAPTURED|REFUNDED)
  pk=WALLET#<fleet> sk=TX#<saga>#REFUND   -> reembolso (evento de reversión)
  pk=WALLET#<fleet> sk=TX#<saga>#REJECTED -> intento rechazado (fondos insuficientes)

IDEMPOTENCIA: la clave del movimiento es (saga_id, tipo). La escritura usa
attribute_not_exists, así que un cobro o reembolso repetido NUNCA se aplica dos
veces: la transacción DynamoDB se cancela y se devuelve el resultado original.
"""

import os
from datetime import UTC, datetime
from decimal import Decimal

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from aegis_common.app import BusinessError, create_app
from aegis_common.aws import dynamodb_client, dynamodb_resource
from aegis_common.flags import SURGE_PRICING

from .pricing import parse_peak_hours, quote

app, settings, log, flags = create_app(
    "billing-wallet-service", "AegisVolt Billing Wallet", readiness_tables=["billing"]
)

PEAK = parse_peak_hours(os.environ.get("PEAK_HOURS", "17-21"))
_ser = TypeSerializer()


def table():
    return dynamodb_resource(settings.aws_region, settings.aws_endpoint_url).Table(settings.table("billing"))


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _ddb(item: dict) -> dict:
    return {k: _ser.serialize(v) for k, v in item.items()}


class ChargeRequest(BaseModel):
    saga_id: str = Field(min_length=3)
    fleet_id: str
    energy_kwh: Decimal = Field(gt=0, le=500)
    connector_type: str


class RefundRequest(BaseModel):
    fleet_id: str
    reason: str = "SAGA_COMPENSATION"


def _wallet_out(i: dict) -> dict:
    return {
        "fleet_id": i["fleet_id"],
        "name": i.get("name", i["fleet_id"]),
        "balance": float(i["balance"]),
        "currency": i.get("currency", "USD"),
    }


def _tx_out(i: dict) -> dict:
    return {
        "saga_id": i["saga_id"],
        "fleet_id": i["fleet_id"],
        "type": i["type"],
        "amount": float(i["amount"]),
        "status": i["status"],
        "reason": i.get("reason"),
        "created_at": i["created_at"],
        "price_per_kwh": float(i["price_per_kwh"]) if "price_per_kwh" in i else None,
        "surge_applied": bool(i.get("surge_applied", False)),
    }


@app.get("/wallets")
def list_wallets():
    items = table().scan(FilterExpression=Attr("entity").eq("WALLET")).get("Items", [])
    return sorted((_wallet_out(i) for i in items), key=lambda w: w["fleet_id"])


@app.get("/wallets/{fleet_id}")
def get_wallet(fleet_id: str):
    item = table().get_item(Key={"pk": f"WALLET#{fleet_id}", "sk": "META"}).get("Item")
    if not item:
        raise BusinessError("WALLET_NOT_FOUND", f"La flota {fleet_id} no tiene billetera", 404)
    return _wallet_out(item)


@app.get("/transactions")
def list_transactions(fleet_id: str | None = None, saga_id: str | None = None, limit: int = 50):
    if fleet_id:
        items = table().query(KeyConditionExpression=Key("pk").eq(f"WALLET#{fleet_id}") & Key("sk").begins_with("TX#"))[
            "Items"
        ]
    else:
        items = table().scan(FilterExpression=Attr("entity").eq("TX")).get("Items", [])
    if saga_id:
        items = [i for i in items if i["saga_id"] == saga_id]
    items.sort(key=lambda i: i["created_at"], reverse=True)
    return [_tx_out(i) for i in items[:limit]]


@app.get("/pricing/quote")
def price_quote(connector_type: str, energy_kwh: Decimal):
    try:
        q = quote(connector_type, energy_kwh, flags.is_enabled(SURGE_PRICING), datetime.now(UTC), PEAK)
    except ValueError as exc:
        raise BusinessError("INVALID_QUOTE", str(exc), 400) from exc
    return {
        "connector_type": q.connector_type,
        "energy_kwh": float(q.energy_kwh),
        "base_price_per_kwh": float(q.base_price_per_kwh),
        "surge_enabled": q.surge_enabled,
        "peak_hour": q.peak_hour,
        "multiplier": float(q.multiplier),
        "price_per_kwh": float(q.price_per_kwh),
        "amount": float(q.amount),
        "peak_window": f"{PEAK[0]:02d}:00-{PEAK[1]:02d}:00",
    }


def _get_tx(fleet_id: str, saga_id: str, kind: str) -> dict | None:
    return table().get_item(Key={"pk": f"WALLET#{fleet_id}", "sk": f"TX#{saga_id}#{kind}"}).get("Item")


@app.post("/charges", status_code=201)
def charge(req: ChargeRequest):
    """PASO 2: descuenta el saldo estimado de la billetera de la flota."""
    existing = _get_tx(req.fleet_id, req.saga_id, "CHARGE")
    if existing:
        log.info("Cobro repetido (idempotente)", extra={"event": "CHARGE_IDEMPOTENT_HIT"})
        return _tx_out(existing)

    get_wallet(req.fleet_id)  # 404 si no existe
    try:
        q = quote(req.connector_type, req.energy_kwh, flags.is_enabled(SURGE_PRICING), datetime.now(UTC), PEAK)
    except ValueError as exc:
        raise BusinessError("INVALID_QUOTE", str(exc), 400) from exc

    ts = now()
    tx = {
        "pk": f"WALLET#{req.fleet_id}",
        "sk": f"TX#{req.saga_id}#CHARGE",
        "entity": "TX",
        "type": "CHARGE",
        "saga_id": req.saga_id,
        "fleet_id": req.fleet_id,
        "amount": q.amount,
        "energy_kwh": req.energy_kwh,
        "price_per_kwh": q.price_per_kwh,
        "surge_applied": q.multiplier > 1,
        "status": "CAPTURED",
        "created_at": ts,
    }
    tname = settings.table("billing")
    try:
        dynamodb_client(settings.aws_region, settings.aws_endpoint_url).transact_write_items(
            TransactItems=[
                {"Put": {"TableName": tname, "Item": _ddb(tx), "ConditionExpression": "attribute_not_exists(pk)"}},
                {
                    "Update": {
                        "TableName": tname,
                        "Key": {"pk": {"S": f"WALLET#{req.fleet_id}"}, "sk": {"S": "META"}},
                        "UpdateExpression": "SET balance = balance - :amt, updated_at = :ts",
                        "ConditionExpression": "balance >= :amt",
                        "ExpressionAttributeValues": {":amt": {"N": str(q.amount)}, ":ts": {"S": ts}},
                    }
                },
            ]
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
        if reasons and reasons[0] == "ConditionalCheckFailed":
            # Carrera: otra petición con la misma saga ya cobró
            return _tx_out(_get_tx(req.fleet_id, req.saga_id, "CHARGE"))
        table().put_item(
            Item={
                **tx,
                "sk": f"TX#{req.saga_id}#REJECTED",
                "type": "CHARGE_REJECTED",
                "status": "REJECTED",
                "reason": "INSUFFICIENT_FUNDS",
            }
        )
        balance = get_wallet(req.fleet_id)["balance"]
        raise BusinessError(
            "INSUFFICIENT_FUNDS",
            f"Fondos insuficientes: saldo {balance:.2f} USD, requerido {q.amount} USD",
            402,
            required=float(q.amount),
            balance=balance,
        ) from exc

    log.info(
        f"Cobro de {q.amount} USD a {req.fleet_id}",
        extra={
            "event": "WALLET_CHARGED",
            "saga_id": req.saga_id,
            "amount": float(q.amount),
            "surge_applied": q.multiplier > 1,
        },
    )
    return _tx_out(tx)


@app.post("/charges/{saga_id}/refund")
def refund(saga_id: str, req: RefundRequest):
    """TRANSACCIÓN COMPENSATORIA: reembolsa el cobro y registra el evento de reversión.

    Idempotente:
      - Si no hubo cobro para la saga  => NOOP (nada que reembolsar)
      - Si ya existe el REFUND        => devuelve el mismo reembolso (no reembolsa 2 veces)
    """
    charge_tx = _get_tx(req.fleet_id, saga_id, "CHARGE")
    if not charge_tx:
        log.info("Reembolso sin cobro previo: no-op", extra={"event": "REFUND_NOOP", "saga_id": saga_id})
        return {"saga_id": saga_id, "status": "NOOP", "detail": "no charge existed"}
    existing = _get_tx(req.fleet_id, saga_id, "REFUND")
    if existing:
        log.info("Reembolso repetido (idempotente)", extra={"event": "REFUND_IDEMPOTENT_HIT"})
        return {**_tx_out(existing), "idempotent": True}

    ts = now()
    amount = charge_tx["amount"]
    refund_tx = {
        "pk": f"WALLET#{req.fleet_id}",
        "sk": f"TX#{saga_id}#REFUND",
        "entity": "TX",
        "type": "REFUND",
        "saga_id": saga_id,
        "fleet_id": req.fleet_id,
        "amount": amount,
        "status": "COMPLETED",
        "reason": req.reason,
        "created_at": ts,
    }
    tname = settings.table("billing")
    try:
        dynamodb_client(settings.aws_region, settings.aws_endpoint_url).transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": tname,
                        "Item": _ddb(refund_tx),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Update": {
                        "TableName": tname,
                        "Key": {"pk": {"S": f"WALLET#{req.fleet_id}"}, "sk": {"S": "META"}},
                        "UpdateExpression": "SET balance = balance + :amt, updated_at = :ts",
                        "ExpressionAttributeValues": {":amt": {"N": str(amount)}, ":ts": {"S": ts}},
                    }
                },
                {
                    "Update": {
                        "TableName": tname,
                        "Key": {"pk": {"S": f"WALLET#{req.fleet_id}"}, "sk": {"S": f"TX#{saga_id}#CHARGE"}},
                        "UpdateExpression": "SET #s = :refunded, refunded_at = :ts",
                        "ConditionExpression": "#s = :captured",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {
                            ":refunded": {"S": "REFUNDED"},
                            ":captured": {"S": "CAPTURED"},
                            ":ts": {"S": ts},
                        },
                    }
                },
            ]
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            # Otra petición concurrente ya reembolsó: devolver ese resultado
            done = _get_tx(req.fleet_id, saga_id, "REFUND")
            if done:
                return {**_tx_out(done), "idempotent": True}
        raise

    log.info(
        f"Reembolso de {amount} USD a {req.fleet_id}",
        extra={"event": "WALLET_REFUNDED", "saga_id": saga_id, "amount": float(amount), "reason": req.reason},
    )
    return _tx_out(refund_tx)
