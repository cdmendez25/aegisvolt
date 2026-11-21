import json
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import httpx
from boto3.dynamodb.conditions import Attr
from prefect import flow, get_run_logger, task
from prefect.artifacts import create_markdown_artifact, create_table_artifact
from prefect.futures import wait

AWS_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
GATEWAY_URLS = {
    "dev": os.environ.get("GATEWAY_URL_DEV", "http://localhost:30081"),
    "test": os.environ.get("GATEWAY_URL_TEST", "http://localhost:30082"),
    "prod": os.environ.get("GATEWAY_URL_PROD", "http://localhost:30083"),
}
IN_FLIGHT = {"STARTED", "RESERVED", "PAID", "COMPENSATING"}


def _aws(kind: str, service: str):
    kw = dict(
        endpoint_url=AWS_ENDPOINT, region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
    )
    return boto3.resource(service, **kw) if kind == "resource" else boto3.client(service, **kw)


def _table(env: str, logical: str):
    return _aws("resource", "dynamodb").Table(f"aegisvolt-{logical}-{env}")


def _scan(t, **kw) -> list[dict]:
    items, resp = [], t.scan(**kw)
    items += resp["Items"]
    while "LastEvaluatedKey" in resp:
        resp = t.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], **kw)
        items += resp["Items"]
    return items


@task(retries=2, retry_delay_seconds=3, task_run_name="health-{env}")
def check_environment_health(env: str) -> dict:
    log = get_run_logger()
    base = GATEWAY_URLS[env]
    result = {
        "env": env,
        "gateway_live": False,
        "gateway_ready": False,
        "graphql_ok": False,
        "tables_ok": False,
        "latency_ms": None,
    }
    try:
        t0 = time.perf_counter()
        result["gateway_live"] = httpx.get(f"{base}/healthz", timeout=5).status_code == 200
        result["gateway_ready"] = httpx.get(f"{base}/readyz", timeout=5).status_code == 200
        r = httpx.post(
            f"{base}/graphql", json={"query": "{ environment { name version } stations { stationId } }"}, timeout=10
        )
        data = r.json().get("data") or {}
        result["graphql_ok"] = data.get("environment", {}).get("name") == env and bool(data.get("stations"))
        result["version"] = data.get("environment", {}).get("version")
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except Exception as exc:
        log.warning(f"[{env}] gateway inaccesible: {exc}")
    ddb = _aws("client", "dynamodb")
    try:
        for logical in ("reservations", "billing", "dispatch", "sagas", "flags"):
            ddb.describe_table(TableName=f"aegisvolt-{logical}-{env}")
        result["tables_ok"] = True
    except Exception as exc:
        log.warning(f"[{env}] tabla faltante: {exc}")
    result["healthy"] = all(result[k] for k in ("gateway_live", "gateway_ready", "graphql_ok", "tables_ok"))
    log.info(f"[{env}] healthy={result['healthy']} {result}")
    return result


@task(task_run_name="audit-sagas-{env}")
def audit_saga_consistency(env: str, orphan_minutes: int = 2) -> dict:
    log = get_run_logger()
    sagas = _scan(_table(env, "sagas"))
    res_t, bill_t, disp_t = _table(env, "reservations"), _table(env, "billing"), _table(env, "dispatch")
    by_status = Counter(s["status"] for s in sagas)
    now = datetime.now(UTC)
    orphans, inconsistencies = [], []

    for s in sagas:
        sid, fleet, status = s["saga_id"], s["fleet_id"], s["status"]
        age = now - datetime.fromisoformat(s["updated_at"])
        if status in IN_FLIGHT and age > timedelta(minutes=orphan_minutes):
            orphans.append({"saga_id": sid, "status": status, "age_min": round(age.total_seconds() / 60, 1)})
            continue

        reservation = res_t.get_item(Key={"pk": f"RESERVATION#{sid}", "sk": "META"}).get("Item")
        charge = bill_t.get_item(Key={"pk": f"WALLET#{fleet}", "sk": f"TX#{sid}#CHARGE"}).get("Item")
        refund = bill_t.get_item(Key={"pk": f"WALLET#{fleet}", "sk": f"TX#{sid}#REFUND"}).get("Item")
        session = disp_t.get_item(Key={"pk": f"SESSION#{sid}", "sk": "META"}).get("Item")

        def bad(reason: str, sid=sid, status=status):
            inconsistencies.append({"saga_id": sid, "status": status, "problem": reason})

        if status == "COMPENSATED":
            if charge and not (refund and charge["status"] == "REFUNDED"):
                bad("cobro sin reembolso tras compensación")
            if refund and charge and Decimal(refund["amount"]) != Decimal(charge["amount"]):
                bad("monto reembolsado distinto al cobrado")
            if reservation and reservation["status"] != "RELEASED":
                bad("reserva no liberada tras compensación")
        elif status == "CHARGING":
            if not (charge and charge["status"] == "CAPTURED"):
                bad("carga activa sin cobro capturado")
            if not (session and session["status"] == "ACTIVE"):
                bad("SAGA en CHARGING sin sesión ACTIVE en dispatch")
        elif status == "FINISHED":
            if reservation and reservation["status"] != "RELEASED":
                bad("carga finalizada pero conector no liberado")
        elif status == "COMPENSATION_FAILED":
            bad("compensación fallida: requiere intervención")

    sagas_by_id = {s["saga_id"]: s for s in sagas}
    for c in _scan(res_t, FilterExpression=Attr("entity").eq("CONNECTOR") & Attr("status").ne("AVAILABLE")):
        owner = sagas_by_id.get(c.get("reserved_by", ""))
        if owner and owner["status"] in {"COMPENSATED", "FAILED", "FINISHED"}:
            inconsistencies.append(
                {
                    "saga_id": owner["saga_id"],
                    "status": owner["status"],
                    "problem": f"conector {c['station_id']}/{c['connector_id']} sigue {c['status']}",
                }
            )

    result = {
        "env": env,
        "total": len(sagas),
        "by_status": dict(by_status),
        "orphans": orphans,
        "inconsistencies": inconsistencies,
    }
    log.info(
        f"[{env}] SAGAs={len(sagas)} {dict(by_status)} huérfanas={len(orphans)} inconsistencias={len(inconsistencies)}"
    )
    return result


@task(task_run_name="recover-orphans-{env}")
def recover_orphan_sagas(env: str, orphans: list[dict]) -> list[dict]:
    log = get_run_logger()
    recovered = []
    for o in orphans:
        r = httpx.post(
            f"{GATEWAY_URLS[env]}/graphql",
            timeout=20,
            json={
                "query": "mutation($s:String!){ recoverSaga(sagaId:$s){ sagaId status } }",
                "variables": {"s": o["saga_id"]},
            },
        )
        data = (r.json().get("data") or {}).get("recoverSaga")
        log.warning(f"[{env}] SAGA huérfana {o['saga_id']} ({o['status']}) -> {data and data['status']}")
        recovered.append({"saga_id": o["saga_id"], "from": o["status"], "to": data and data["status"]})
    return recovered


@task(task_run_name="publish-metrics-{env}")
def publish_metrics(env: str, health: dict, audit: dict, recovered: list[dict]) -> None:
    by = audit["by_status"]
    metrics = {
        "EnvironmentHealthy": 1.0 if health["healthy"] else 0.0,
        "GatewayLatencyMs": float(health["latency_ms"] or 0),
        "SagaTotal": float(audit["total"]),
        "SagaCharging": float(by.get("CHARGING", 0)),
        "SagaCompensatedTotal": float(by.get("COMPENSATED", 0)),
        "SagaCompensationFailedTotal": float(by.get("COMPENSATION_FAILED", 0)),
        "SagaOrphans": float(len(audit["orphans"])),
        "SagaInconsistencies": float(len(audit["inconsistencies"])),
        "SagaRecovered": float(len(recovered)),
    }
    _aws("client", "cloudwatch").put_metric_data(
        Namespace=f"AegisVolt/{env}",
        MetricData=[
            {"MetricName": k, "Value": v, "Dimensions": [{"Name": "Source", "Value": "prefect"}]}
            for k, v in metrics.items()
        ],
    )
    logs = _aws("client", "logs")
    group, stream = f"/aegisvolt/{env}/prefect-observability", datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        logs.create_log_stream(logGroupName=group, logStreamName=stream)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_log_events(
        logGroupName=group,
        logStreamName=stream,
        logEvents=[
            {
                "timestamp": int(time.time() * 1000),
                "message": json.dumps(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "level": "INFO",
                        "service": "prefect-observability",
                        "env": env,
                        "event": "OBSERVABILITY_REPORT",
                        "correlation_id": "-",
                        "metrics": metrics,
                        "orphans": audit["orphans"],
                        "inconsistencies": audit["inconsistencies"],
                    },
                    default=str,
                ),
            }
        ],
    )
    get_run_logger().info(f"[{env}] métricas publicadas en CloudWatch AegisVolt/{env}: {metrics}")


@flow(name="aegisvolt-observability", log_prints=True)
def aegisvolt_observability(envs: list[str] | None = None, orphan_minutes: int = 2, auto_recover: bool = True) -> dict:
    envs = envs or ["dev", "test", "prod"]
    health_f = {e: check_environment_health.submit(e) for e in envs}
    audit_f = {e: audit_saga_consistency.submit(e, orphan_minutes) for e in envs}
    wait(list(health_f.values()) + list(audit_f.values()))

    rows, report = [], {}
    for e in envs:
        health, audit = health_f[e].result(), audit_f[e].result()
        recovered = recover_orphan_sagas(e, audit["orphans"]) if (auto_recover and audit["orphans"]) else []
        publish_metrics(e, health, audit, recovered)
        report[e] = {"health": health, "audit": audit, "recovered": recovered}
        rows.append(
            {
                "ambiente": e,
                "saludable": "✅" if health["healthy"] else "❌",
                "versión": health.get("version") or "-",
                "latencia_ms": health["latency_ms"],
                "sagas": audit["total"],
                "charging": audit["by_status"].get("CHARGING", 0),
                "finished": audit["by_status"].get("FINISHED", 0),
                "compensadas": audit["by_status"].get("COMPENSATED", 0),
                "fallidas": audit["by_status"].get("FAILED", 0),
                "huérfanas": len(audit["orphans"]),
                "recuperadas": len(recovered),
                "inconsistencias": len(audit["inconsistencies"]),
            }
        )

    create_table_artifact(
        key="aegisvolt-environments", table=rows, description="Salud de ambientes y auditoría de SAGAs"
    )
    md = ["# AegisVolt · reporte de observabilidad", f"_{datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC_", ""]
    for e, r in report.items():
        md.append(f"## {e} — {'✅ saludable' if r['health']['healthy'] else '❌ con problemas'}")
        md.append(f"- SAGAs por estado: `{r['audit']['by_status']}`")
        md.append(f"- Huérfanas: {len(r['audit']['orphans'])} · recuperadas: {len(r['recovered'])}")
        for i in r["audit"]["inconsistencies"][:10]:
            md.append(f"  - ⚠️ `{i['saga_id']}` ({i['status']}): {i['problem']}")
        md.append("")
    create_markdown_artifact(key="aegisvolt-saga-audit", markdown="\n".join(md))

    unhealthy = [e for e in envs if not report[e]["health"]["healthy"]]
    if unhealthy:
        raise RuntimeError(f"Ambientes con problemas: {unhealthy}")
    return {e: {"healthy": r["health"]["healthy"], **r["audit"]["by_status"]} for e, r in report.items()}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        aegisvolt_observability.serve(name="cada-5-minutos", interval=300, tags=["aegisvolt", "observability"])
    else:
        print(aegisvolt_observability())
