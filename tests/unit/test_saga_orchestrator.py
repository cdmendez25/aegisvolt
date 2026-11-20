"""Pruebas unitarias del orquestador SAGA con dobles de prueba (sin red ni AWS).

Verifican el ORDEN de las compensaciones, los reintentos, la idempotencia y la
recuperación de SAGAs huérfanas.
"""

import copy

import pytest

from graphql_gateway.clients import Services, StepResult
from graphql_gateway.saga import ReserveCommand, SagaOrchestrator, SagaRecord, saga_id_for

OK_RESERVE = StepResult(ok=True, data={"status": "RESERVED", "connector_type": "FAST", "power_kw": 50})
OK_CHARGE = StepResult(ok=True, data={"amount": 18.0, "status": "CAPTURED"})
OK_SESSION = StepResult(ok=True, data={"status": "ACTIVE"})
OK = StepResult(ok=True, data={"status": "OK"})
FAULT = StepResult(ok=False, error_code="CHARGER_HARDWARE_FAULT", message="falla física", status_code=422)
NO_FUNDS = StepResult(ok=False, error_code="INSUFFICIENT_FUNDS", message="sin saldo", status_code=402)
BUSY = StepResult(ok=False, error_code="CONNECTOR_NOT_AVAILABLE", message="ocupado", status_code=409)
TIMEOUT = StepResult(ok=False, uncertain=True, error_code="SERVICE_UNREACHABLE", message="timeout")


class FakeClient:
    """Responde según un guion {(method, path_prefix): [respuestas...]} y registra las llamadas."""

    def __init__(self, name: str, journal: list, script: dict):
        self.name, self.journal, self.script = name, journal, script

    async def call(self, method, path, correlation_id, **kwargs):
        self.journal.append((self.name, method, path, correlation_id))
        for (m, key), responses in self.script.items():
            # "/reservations" coincide exacto; "/release", "/refund"... coinciden por sufijo
            if m == method and (path == key or (path.endswith(key) and path.count("/") > 1)):
                return responses.pop(0) if len(responses) > 1 else responses[0]
        raise AssertionError(f"llamada inesperada {self.name} {method} {path}")


class MemoryStore:
    def __init__(self):
        self.data: dict[str, SagaRecord] = {}

    def create(self, saga):
        if saga.saga_id in self.data:
            return False
        self.data[saga.saga_id] = copy.deepcopy(saga)
        return True

    def get(self, saga_id):
        return copy.deepcopy(self.data.get(saga_id))

    def save(self, saga):
        self.data[saga.saga_id] = copy.deepcopy(saga)

    def list(self, limit=50, status=None):
        return list(self.data.values())[:limit]


def make(reservation=None, billing=None, dispatch=None):
    journal: list = []
    services = Services(
        reservation=FakeClient(
            "reservation",
            journal,
            reservation
            or {("POST", "/reservations"): [OK_RESERVE], ("POST", "/confirm"): [OK], ("POST", "/release"): [OK]},
        ),
        billing=FakeClient(
            "billing", journal, billing or {("POST", "/charges"): [OK_CHARGE], ("POST", "/refund"): [OK]}
        ),
        dispatch=FakeClient("dispatch", journal, dispatch or {("POST", "/sessions"): [OK_SESSION]}),
    )
    store = MemoryStore()

    async def no_sleep(_):
        return None

    orch = SagaOrchestrator(services, store, "unit", compensation_attempts=3, sleep=no_sleep)
    return orch, journal, store


CMD = ReserveCommand(station_id="BOG-01", connector_id="C1", fleet_id="FLEET-A", energy_kwh=40)


def calls(journal):
    return [f"{svc}:{path.split('/')[-1] if path.count('/') > 1 else path}" for svc, _, path, _ in journal]


async def test_happy_path_reaches_charging():
    orch, journal, store = make()
    saga = await orch.reserve_and_start_charging(CMD)
    assert saga.status == "CHARGING"
    assert saga.amount == 18.0
    assert calls(journal) == [
        "reservation:/reservations",
        "billing:/charges",
        "dispatch:/sessions",
        "reservation:confirm",
    ]
    assert store.get(saga.saga_id).status == "CHARGING"


async def test_step3_failure_refunds_then_releases_in_reverse_order():
    orch, journal, _ = make(dispatch={("POST", "/sessions"): [FAULT]})
    saga = await orch.reserve_and_start_charging(CMD)
    assert saga.status == "COMPENSATED"
    assert saga.failed_step == "START_CHARGING" and saga.error_code == "CHARGER_HARDWARE_FAULT"
    # Orden inverso: primero billing (refund), luego reservas (release)
    assert calls(journal)[-2:] == ["billing:refund", "reservation:release"]
    comp = [s["name"] for s in saga.steps if s["action"] == "COMPENSATE"]
    assert comp == ["REFUND_WALLET", "RELEASE_CONNECTOR"]


async def test_step2_insufficient_funds_only_releases_connector():
    orch, journal, _ = make(billing={("POST", "/charges"): [NO_FUNDS], ("POST", "/refund"): [OK]})
    saga = await orch.reserve_and_start_charging(CMD)
    assert saga.status == "COMPENSATED" and saga.error_code == "INSUFFICIENT_FUNDS"
    assert "billing:refund" not in calls(journal)  # el cobro nunca ocurrió: no se reembolsa
    assert calls(journal)[-1] == "reservation:release"
    assert not any(c.startswith("dispatch") for c in calls(journal))


async def test_step1_business_failure_needs_no_compensation():
    orch, journal, _ = make(reservation={("POST", "/reservations"): [BUSY]})
    saga = await orch.reserve_and_start_charging(CMD)
    assert saga.status == "FAILED" and saga.error_code == "CONNECTOR_NOT_AVAILABLE"
    assert calls(journal) == ["reservation:/reservations"]


async def test_step2_timeout_is_uncertain_so_refund_is_also_attempted():
    orch, journal, _ = make(billing={("POST", "/charges"): [TIMEOUT], ("POST", "/refund"): [OK]})
    saga = await orch.reserve_and_start_charging(CMD)
    assert saga.status == "COMPENSATED"
    assert calls(journal)[-2:] == ["billing:refund", "reservation:release"]


async def test_compensation_is_retried_until_it_succeeds():
    orch, _, _ = make(
        dispatch={("POST", "/sessions"): [FAULT]},
        billing={("POST", "/charges"): [OK_CHARGE], ("POST", "/refund"): [TIMEOUT, TIMEOUT, OK]},
    )
    saga = await orch.reserve_and_start_charging(CMD)
    refund = next(s for s in saga.steps if s["name"] == "REFUND_WALLET")
    assert refund["status"] == "SUCCEEDED" and refund["attempts"] == 3
    assert saga.status == "COMPENSATED"


async def test_compensation_exhausted_marks_compensation_failed():
    orch, _, _ = make(
        dispatch={("POST", "/sessions"): [FAULT]},
        billing={("POST", "/charges"): [OK_CHARGE], ("POST", "/refund"): [TIMEOUT]},
    )
    saga = await orch.reserve_and_start_charging(CMD)
    assert saga.status == "COMPENSATION_FAILED"
    refund = next(s for s in saga.steps if s["name"] == "REFUND_WALLET")
    assert refund["status"] == "FAILED" and refund["attempts"] == 3
    # Aun así se intenta liberar el conector (no se abandona el resto)
    assert any(s["name"] == "RELEASE_CONNECTOR" and s["status"] == "SUCCEEDED" for s in saga.steps)


async def test_same_idempotency_key_returns_same_saga_without_side_effects():
    orch, journal, _ = make()
    cmd = ReserveCommand(**{**CMD.__dict__, "idempotency_key": "retry-123"})
    first = await orch.reserve_and_start_charging(cmd)
    n_calls = len(journal)
    second = await orch.reserve_and_start_charging(cmd)
    assert first.saga_id == second.saga_id == saga_id_for("retry-123")
    assert len(journal) == n_calls  # el reintento NO volvió a llamar a ningún servicio


async def test_correlation_id_is_the_saga_id_in_every_call():
    orch, journal, _ = make(dispatch={("POST", "/sessions"): [FAULT]})
    saga = await orch.reserve_and_start_charging(CMD)
    assert {cid for *_, cid in journal} == {saga.saga_id}


async def test_recover_orphan_saga_compensates_everything():
    orch, journal, store = make()
    orphan = SagaRecord(
        saga_id="orphan-1",
        status="PAID",
        station_id="BOG-01",
        connector_id="C1",
        fleet_id="FLEET-A",
        energy_kwh=40,
        idempotency_key="k",
        created_at="t",
        updated_at="t",
    )
    store.create(orphan)
    saga = await orch.recover("orphan-1")
    assert saga.status == "COMPENSATED"
    assert calls(journal) == ["billing:refund", "reservation:release"]


async def test_finish_charging_requires_charging_state():
    orch, _, store = make()
    saga = await orch.reserve_and_start_charging(CMD)
    store.data[saga.saga_id].status = "COMPENSATED"
    with pytest.raises(ValueError):
        await orch.finish_charging(saga.saga_id)
