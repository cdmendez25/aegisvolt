"""Orquestador SAGA: "Reservar y Activar Carga".

Patrón SAGA por ORQUESTACIÓN: un coordinador central (este módulo, en la capa
GraphQL) invoca cada paso y, si alguno falla, ejecuta las transacciones
compensatorias en ORDEN INVERSO.

  Paso 1  station-reservation  POST /reservations           comp: POST /reservations/{id}/release
  Paso 2  billing-wallet       POST /charges                comp: POST /charges/{id}/refund
  Paso 3  energy-dispatch      POST /sessions               (último paso: no requiere compensación)

  Falla paso 3 -> refund (billing)  y luego release (reservas)
  Falla paso 2 -> release (reservas)
  Falla paso 1 -> nada que compensar (FAILED)

Estados: STARTED -> RESERVED -> PAID -> CHARGING -> FINISHED
                     \\-> COMPENSATING -> COMPENSATED | COMPENSATION_FAILED
         STARTED -> FAILED

Idempotencia:
  * saga_id = uuid5(idempotency_key): reintentar la mutación con la misma clave
    devuelve la MISMA saga (no se cobra ni reserva dos veces).
  * Todas las llamadas llevan saga_id; cada servicio deduplica por saga_id.
  * Las compensaciones se reintentan con backoff y son no-op si ya se aplicaron.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from aegis_common.logging import correlation_id_var

from .clients import Services, StepResult

log = logging.getLogger("graphql-gateway.saga")

SAGA_NAMESPACE = uuid.UUID("6f1c3f0e-5c3a-4b8e-9a51-0a0e615a9a01")  # namespace fijo de AegisVolt
TERMINAL = {"CHARGING", "FINISHED", "FAILED", "COMPENSATED", "COMPENSATION_FAILED"}
IN_FLIGHT = {"STARTED", "RESERVED", "PAID", "COMPENSATING"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class ReserveCommand:
    station_id: str
    connector_id: str
    fleet_id: str
    energy_kwh: float
    idempotency_key: str | None = None


@dataclass
class SagaRecord:
    saga_id: str
    status: str
    station_id: str
    connector_id: str
    fleet_id: str
    energy_kwh: float
    idempotency_key: str
    created_at: str
    updated_at: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    amount: float | None = None
    connector_type: str | None = None
    power_kw: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    failed_step: str | None = None
    environment: str | None = None

    def add_step(self, name: str, action: str, status: str, detail: str = "", attempts: int = 1) -> None:
        self.steps.append(
            {"name": name, "action": action, "status": status, "detail": detail, "attempts": attempts, "at": now()}
        )
        self.updated_at = now()


class SagaStore(Protocol):
    def create(self, saga: SagaRecord) -> bool: ...  # False si ya existía
    def get(self, saga_id: str) -> SagaRecord | None: ...
    def save(self, saga: SagaRecord) -> None: ...
    def list(self, limit: int = 50, status: str | None = None) -> list[SagaRecord]: ...


def saga_id_for(key: str) -> str:
    return str(uuid.uuid5(SAGA_NAMESPACE, key))


class SagaOrchestrator:
    def __init__(
        self,
        services: Services,
        store: SagaStore,
        env: str,
        compensation_attempts: int = 4,
        backoff_base: float = 0.2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.svc = services
        self.store = store
        self.env = env
        self.compensation_attempts = compensation_attempts
        self.backoff_base = backoff_base
        self.sleep = sleep

    # ------------------------------------------------------------------ run
    async def reserve_and_start_charging(self, cmd: ReserveCommand) -> SagaRecord:
        key = cmd.idempotency_key or str(uuid.uuid4())
        saga_id = saga_id_for(key)
        ts = now()
        saga = SagaRecord(
            saga_id=saga_id,
            status="STARTED",
            station_id=cmd.station_id,
            connector_id=cmd.connector_id,
            fleet_id=cmd.fleet_id,
            energy_kwh=cmd.energy_kwh,
            idempotency_key=key,
            created_at=ts,
            updated_at=ts,
            environment=self.env,
        )
        if not self.store.create(saga):
            existing = self.store.get(saga_id)
            log.info(
                "SAGA repetida con la misma idempotency_key: se devuelve la existente",
                extra={"event": "SAGA_IDEMPOTENT_HIT", "correlation_id": saga_id},
            )
            return existing  # type: ignore[return-value]

        token = correlation_id_var.set(saga_id)  # todos los logs llevan correlation_id = saga_id
        try:
            return await self._execute(saga)
        finally:
            correlation_id_var.reset(token)

    async def _execute(self, saga: SagaRecord) -> SagaRecord:
        sid = saga.saga_id
        log.info(
            "SAGA iniciada",
            extra={
                "event": "SAGA_STARTED",
                "fleet_id": saga.fleet_id,
                "station_id": saga.station_id,
                "connector_id": saga.connector_id,
            },
        )

        # ---------------- Paso 1: reservar conector ----------------
        r1 = await self.svc.reservation.call(
            "POST",
            "/reservations",
            sid,
            json={
                "saga_id": sid,
                "station_id": saga.station_id,
                "connector_id": saga.connector_id,
                "fleet_id": saga.fleet_id,
            },
        )
        if not r1.ok:
            saga.add_step("RESERVE_CONNECTOR", "EXECUTE", "FAILED", f"{r1.error_code}: {r1.message}")
            if r1.uncertain:
                return await self._compensate(saga, r1, "RESERVE_CONNECTOR", ["RELEASE_CONNECTOR"])
            return self._fail(saga, r1, "RESERVE_CONNECTOR")
        saga.connector_type = r1.data.get("connector_type")
        saga.power_kw = r1.data.get("power_kw")
        saga.add_step("RESERVE_CONNECTOR", "EXECUTE", "SUCCEEDED", "Conector RESERVED")
        saga.status = "RESERVED"
        self.store.save(saga)

        # ---------------- Paso 2: cobrar ----------------
        r2 = await self.svc.billing.call(
            "POST",
            "/charges",
            sid,
            json={
                "saga_id": sid,
                "fleet_id": saga.fleet_id,
                "energy_kwh": saga.energy_kwh,
                "connector_type": saga.connector_type,
            },
        )
        if not r2.ok:
            saga.add_step("CHARGE_WALLET", "EXECUTE", "FAILED", f"{r2.error_code}: {r2.message}")
            comps = (["REFUND_WALLET"] if r2.uncertain else []) + ["RELEASE_CONNECTOR"]
            return await self._compensate(saga, r2, "CHARGE_WALLET", comps)
        saga.amount = r2.data.get("amount")
        saga.add_step("CHARGE_WALLET", "EXECUTE", "SUCCEEDED", f"Cobrado {saga.amount} USD")
        saga.status = "PAID"
        self.store.save(saga)

        # ---------------- Paso 3: activar cargador ----------------
        r3 = await self.svc.dispatch.call(
            "POST",
            "/sessions",
            sid,
            json={
                "saga_id": sid,
                "station_id": saga.station_id,
                "connector_id": saga.connector_id,
                "fleet_id": saga.fleet_id,
                "energy_kwh": saga.energy_kwh,
                "power_kw": saga.power_kw or 50,
            },
        )
        if not r3.ok:
            saga.add_step("START_CHARGING", "EXECUTE", "FAILED", f"{r3.error_code}: {r3.message}")
            return await self._compensate(saga, r3, "START_CHARGING", ["REFUND_WALLET", "RELEASE_CONNECTOR"])
        saga.add_step("START_CHARGING", "EXECUTE", "SUCCEEDED", "Flujo eléctrico activo")

        # Post-éxito: marcar el conector CHARGING (no crítico: la carga ya inició)
        c = await self.svc.reservation.call("POST", f"/reservations/{sid}/confirm", sid)
        if not c.ok:
            log.warning(
                "No se pudo marcar CHARGING el conector", extra={"event": "CONFIRM_FAILED", "error_code": c.error_code}
            )
        saga.status = "CHARGING"
        self.store.save(saga)
        log.info("SAGA completada: carga activa", extra={"event": "SAGA_COMPLETED", "amount": saga.amount})
        return saga

    # ------------------------------------------------------------------ fallos
    def _fail(self, saga: SagaRecord, r: StepResult, step: str) -> SagaRecord:
        saga.status = "FAILED"
        saga.failed_step, saga.error_code, saga.error_message = step, r.error_code, r.message
        self.store.save(saga)
        log.warning(
            "SAGA fallida sin efectos que compensar",
            extra={"event": "SAGA_FAILED", "failed_step": step, "error_code": r.error_code},
        )
        return saga

    async def _compensate(self, saga: SagaRecord, r: StepResult, step: str, compensations: list[str]) -> SagaRecord:
        saga.status = "COMPENSATING"
        saga.failed_step, saga.error_code, saga.error_message = step, r.error_code, r.message
        self.store.save(saga)
        log.warning(
            f"Paso {step} falló ({r.error_code}); compensando {compensations}",
            extra={
                "event": "SAGA_COMPENSATING",
                "failed_step": step,
                "error_code": r.error_code,
                "compensations": compensations,
            },
        )
        all_ok = await self._run_compensations(saga, compensations)
        saga.status = "COMPENSATED" if all_ok else "COMPENSATION_FAILED"
        self.store.save(saga)
        if all_ok:
            log.info(
                "SAGA compensada: rollback distribuido completo",
                extra={"event": "SAGA_COMPENSATED", "failed_step": step, "compensations": compensations},
            )
        else:
            log.error(
                "SAGA NO pudo compensarse: requiere intervención",
                extra={"event": "SAGA_COMPENSATION_FAILED", "failed_step": step},
            )
        return saga

    async def _run_compensations(self, saga: SagaRecord, compensations: list[str]) -> bool:
        sid = saga.saga_id
        actions: dict[str, Callable[[], Awaitable[StepResult]]] = {
            "REFUND_WALLET": lambda: self.svc.billing.call(
                "POST",
                f"/charges/{sid}/refund",
                sid,
                json={"fleet_id": saga.fleet_id, "reason": f"SAGA_COMPENSATION:{saga.error_code}"},
            ),
            "RELEASE_CONNECTOR": lambda: self.svc.reservation.call(
                "POST", f"/reservations/{sid}/release", sid, json={"reason": "COMPENSATION"}
            ),
        }
        all_ok = True
        for name in compensations:
            result, attempts = await self._retry(actions[name])
            if result.ok:
                detail = result.data.get("status", "OK")
                saga.add_step(name, "COMPENSATE", "SUCCEEDED", f"{detail}", attempts)
                log.info(f"Compensación {name} aplicada", extra={"event": f"COMPENSATION_{name}", "attempts": attempts})
            else:
                all_ok = False
                saga.add_step(name, "COMPENSATE", "FAILED", f"{result.error_code}: {result.message}", attempts)
                log.error(
                    f"Compensación {name} falló tras {attempts} intentos",
                    extra={"event": "COMPENSATION_FAILED", "compensation": name},
                )
            self.store.save(saga)
        return all_ok

    async def _retry(self, fn: Callable[[], Awaitable[StepResult]]) -> tuple[StepResult, int]:
        result = StepResult(ok=False)
        for attempt in range(1, self.compensation_attempts + 1):
            result = await fn()
            # Un 4xx de negocio en una compensación no mejora reintentando
            if result.ok or not result.uncertain:
                return result, attempt
            await self.sleep(self.backoff_base * 2 ** (attempt - 1))
        return result, self.compensation_attempts

    # ------------------------------------------------------------------ otros
    async def finish_charging(self, saga_id: str) -> SagaRecord:
        saga = self.store.get(saga_id)
        if not saga:
            raise ValueError("SAGA no encontrada")
        if saga.status == "FINISHED":
            return saga
        if saga.status != "CHARGING":
            raise ValueError(f"La SAGA está en {saga.status}, no en CHARGING")
        token = correlation_id_var.set(saga_id)
        try:
            stop = await self.svc.dispatch.call("POST", f"/sessions/{saga_id}/stop", saga_id)
            rel = await self.svc.reservation.call(
                "POST", f"/reservations/{saga_id}/release", saga_id, json={"reason": "COMPLETED"}
            )
            if not (stop.ok and rel.ok):
                raise ValueError(f"No se pudo finalizar: {stop.error_code or rel.error_code}")
            saga.add_step(
                "FINISH_CHARGING",
                "EXECUTE",
                "SUCCEEDED",
                f"Energía entregada {stop.data.get('energy_delivered_kwh')} kWh",
            )
            saga.status = "FINISHED"
            self.store.save(saga)
            log.info("Carga finalizada, conector liberado", extra={"event": "SAGA_FINISHED"})
            return saga
        finally:
            correlation_id_var.reset(token)

    async def recover(self, saga_id: str) -> SagaRecord:
        """Recuperación de SAGAs HUÉRFANAS (ej. el pod murió a mitad del flujo).
        Prefect la invoca: como las compensaciones son idempotentes, compensar
        todo lo posible es seguro aunque no sepamos en qué paso quedó."""
        saga = self.store.get(saga_id)
        if not saga:
            raise ValueError("SAGA no encontrada")
        if saga.status not in IN_FLIGHT:
            return saga
        token = correlation_id_var.set(saga_id)
        try:
            log.warning("Recuperando SAGA huérfana", extra={"event": "SAGA_RECOVERY", "stuck_status": saga.status})
            r = StepResult(ok=False, error_code="ORPHAN_RECOVERY", message=f"Huérfana en {saga.status}")
            return await self._compensate(
                saga, r, saga.failed_step or saga.status, ["REFUND_WALLET", "RELEASE_CONNECTOR"]
            )
        finally:
            correlation_id_var.reset(token)
