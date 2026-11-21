import asyncio
from collections.abc import AsyncGenerator
from enum import Enum

import strawberry
from strawberry.types import Info

from aegis_common.flags import KNOWN_FLAGS

from .saga import ReserveCommand, SagaRecord


@strawberry.enum
class ConnectorStatus(Enum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    CHARGING = "CHARGING"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"


@strawberry.enum
class SagaStatus(Enum):
    STARTED = "STARTED"
    RESERVED = "RESERVED"
    PAID = "PAID"
    CHARGING = "CHARGING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"


@strawberry.type
class Connector:
    connector_id: str
    type: str
    power_kw: int
    status: ConnectorStatus
    reserved_by: str | None = None
    fleet_id: str | None = None


@strawberry.type
class Station:
    station_id: str
    name: str
    city: str
    connectors: list[Connector]

    @strawberry.field
    def available_connectors(self) -> int:
        return sum(1 for c in self.connectors if c.status == ConnectorStatus.AVAILABLE)


@strawberry.type
class Wallet:
    fleet_id: str
    name: str
    balance: float
    currency: str


@strawberry.type
class WalletTransaction:
    saga_id: str
    fleet_id: str
    type: str
    amount: float
    status: str
    created_at: str
    reason: str | None = None
    price_per_kwh: float | None = None
    surge_applied: bool = False


@strawberry.type
class SagaStep:
    name: str
    action: str
    status: str
    detail: str
    attempts: int
    at: str


@strawberry.type
class Saga:
    saga_id: str
    status: SagaStatus
    station_id: str
    connector_id: str
    fleet_id: str
    energy_kwh: float
    idempotency_key: str
    created_at: str
    updated_at: str
    steps: list[SagaStep]
    amount: float | None = None
    connector_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    failed_step: str | None = None
    environment: str | None = None

    @strawberry.field(description="Igual al saga_id: úsalo para buscar en CloudWatch")
    def correlation_id(self) -> str:
        return self.saga_id

    @classmethod
    def from_record(cls, r: SagaRecord) -> "Saga":
        return cls(
            saga_id=r.saga_id,
            status=SagaStatus(r.status),
            station_id=r.station_id,
            connector_id=r.connector_id,
            fleet_id=r.fleet_id,
            energy_kwh=r.energy_kwh,
            idempotency_key=r.idempotency_key,
            created_at=r.created_at,
            updated_at=r.updated_at,
            steps=[SagaStep(**{**s, "attempts": int(s.get("attempts", 1))}) for s in r.steps],
            amount=r.amount,
            connector_type=r.connector_type,
            error_code=r.error_code,
            error_message=r.error_message,
            failed_step=r.failed_step,
            environment=r.environment,
        )


@strawberry.type
class FeatureFlag:
    name: str
    enabled: bool
    description: str
    updated_at: str | None = None
    updated_by: str | None = None


@strawberry.type
class EnvironmentInfo:
    name: str
    version: str
    revision: str
    flag_admin_enabled: bool


@strawberry.type
class PriceQuote:
    connector_type: str
    energy_kwh: float
    base_price_per_kwh: float
    surge_enabled: bool
    peak_hour: bool
    multiplier: float
    price_per_kwh: float
    amount: float
    peak_window: str


@strawberry.type
class ChargingSession:
    saga_id: str
    station_id: str
    connector_id: str
    fleet_id: str
    status: str
    energy_target_kwh: float
    power_kw: int
    energy_delivered_kwh: float
    started_at: str | None = None
    stopped_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@strawberry.type
class TelemetryReading:
    timestamp: str
    elapsed_min: float
    power_kw: float
    energy_kwh: float
    voltage_v: float
    current_a: float
    temperature_c: float
    progress_pct: float
    completed: bool


@strawberry.type
class Telemetry:
    session: ChargingSession
    current: TelemetryReading | None
    samples: list[TelemetryReading]


@strawberry.input
class ReserveAndStartChargingInput:
    station_id: str
    connector_id: str
    fleet_id: str
    energy_kwh: float = 30.0
    idempotency_key: str | None = strawberry.field(
        default=None, description="Misma clave => misma SAGA (reintentos seguros)"
    )


def _ctx(info: Info):
    return info.context["deps"]


def _station(s: dict) -> Station:
    return Station(
        station_id=s["station_id"],
        name=s["name"],
        city=s["city"],
        connectors=[
            Connector(
                connector_id=c["connector_id"],
                type=c["type"],
                power_kw=c["power_kw"],
                status=ConnectorStatus(c["status"]),
                reserved_by=c.get("reserved_by"),
                fleet_id=c.get("fleet_id"),
            )
            for c in s["connectors"]
        ],
    )


def _reading(r: dict) -> TelemetryReading:
    keys = TelemetryReading.__annotations__.keys()
    return TelemetryReading(**{k: r[k] for k in keys})


@strawberry.type
class Query:
    @strawberry.field(description="Ambiente al que está conectado el frontend")
    def environment(self, info: Info) -> EnvironmentInfo:
        d = _ctx(info)
        return EnvironmentInfo(
            name=d.settings.env,
            version=d.settings.app_version,
            revision=d.settings.revision,
            flag_admin_enabled=d.flag_admin_enabled,
        )

    @strawberry.field
    def feature_flags(self, info: Info) -> list[FeatureFlag]:
        return [
            FeatureFlag(
                name=f.name,
                enabled=f.enabled,
                description=f.description,
                updated_at=f.updated_at or None,
                updated_by=f.updated_by or None,
            )
            for f in _ctx(info).flags.all()
        ]

    @strawberry.field
    async def stations(self, info: Info) -> list[Station]:
        return [_station(s) for s in await _ctx(info).services.reservation.get("/stations")]

    @strawberry.field
    async def wallets(self, info: Info) -> list[Wallet]:
        return [Wallet(**w) for w in await _ctx(info).services.billing.get("/wallets")]

    @strawberry.field
    async def transactions(
        self, info: Info, fleet_id: str | None = None, saga_id: str | None = None, limit: int = 50
    ) -> list[WalletTransaction]:
        rows = await _ctx(info).services.billing.get("/transactions", fleet_id=fleet_id, saga_id=saga_id, limit=limit)
        return [WalletTransaction(**r) for r in rows]

    @strawberry.field
    async def price_quote(self, info: Info, connector_type: str, energy_kwh: float) -> PriceQuote:
        q = await _ctx(info).services.billing.get(
            "/pricing/quote", connector_type=connector_type, energy_kwh=energy_kwh
        )
        return PriceQuote(**q)

    @strawberry.field
    def sagas(self, info: Info, limit: int = 30, status: SagaStatus | None = None) -> list[Saga]:
        recs = _ctx(info).store.list(limit=limit, status=status.value if status else None)
        return [Saga.from_record(r) for r in recs]

    @strawberry.field
    def saga(self, info: Info, saga_id: str) -> Saga | None:
        r = _ctx(info).store.get(saga_id)
        return Saga.from_record(r) if r else None

    @strawberry.field
    async def sessions(self, info: Info, limit: int = 30) -> list[ChargingSession]:
        return [ChargingSession(**s) for s in await _ctx(info).services.dispatch.get("/sessions", limit=limit)]

    @strawberry.field
    async def telemetry(self, info: Info, saga_id: str) -> Telemetry:
        t = await _ctx(info).services.dispatch.get(f"/sessions/{saga_id}/telemetry")
        return Telemetry(
            session=ChargingSession(**t["session"]),
            current=_reading(t["current"]) if t["current"] else None,
            samples=[_reading(s) for s in t["samples"]],
        )


@strawberry.type
class Mutation:
    @strawberry.mutation(description="Dispara la SAGA Reservar -> Cobrar -> Activar carga")
    async def reserve_and_start_charging(self, info: Info, input: ReserveAndStartChargingInput) -> Saga:
        saga = await _ctx(info).orchestrator.reserve_and_start_charging(
            ReserveCommand(
                station_id=input.station_id,
                connector_id=input.connector_id,
                fleet_id=input.fleet_id,
                energy_kwh=input.energy_kwh,
                idempotency_key=input.idempotency_key,
            )
        )
        return Saga.from_record(saga)

    @strawberry.mutation(description="Detiene la carga y libera el conector")
    async def finish_charging(self, info: Info, saga_id: str) -> Saga:
        return Saga.from_record(await _ctx(info).orchestrator.finish_charging(saga_id))

    @strawberry.mutation(description="Compensa una SAGA huérfana (usada por Prefect)")
    async def recover_saga(self, info: Info, saga_id: str) -> Saga:
        return Saga.from_record(await _ctx(info).orchestrator.recover(saga_id))

    @strawberry.mutation(description="Cambia una flag de ESTE ambiente en caliente (sin redeploy)")
    def set_feature_flag(self, info: Info, name: str, enabled: bool) -> FeatureFlag:
        d = _ctx(info)
        if not d.flag_admin_enabled:
            raise PermissionError(f"La administración de flags desde la API está deshabilitada en {d.settings.env}")
        if name not in KNOWN_FLAGS:
            raise ValueError(f"Flag desconocida: {name}")
        f = d.flags.set(name, enabled, updated_by="graphql-ui")
        import logging

        logging.getLogger("graphql-gateway").warning(
            f"Feature flag {name} -> {enabled}",
            extra={"event": "FEATURE_FLAG_CHANGED", "flag": name, "enabled": enabled},
        )
        return FeatureFlag(
            name=f.name, enabled=f.enabled, description=f.description, updated_at=f.updated_at, updated_by=f.updated_by
        )


@strawberry.type
class Subscription:
    @strawberry.subscription(description="Estado de estaciones/conectores cada 2 s (WSS)")
    async def stations_updated(self, info: Info, interval_seconds: float = 2.0) -> AsyncGenerator[list[Station]]:
        while True:
            yield [_station(s) for s in await _ctx(info).services.reservation.get("/stations")]
            await asyncio.sleep(max(interval_seconds, 0.5))


schema = strawberry.Schema(query=Query, mutation=Mutation, subscription=Subscription)
