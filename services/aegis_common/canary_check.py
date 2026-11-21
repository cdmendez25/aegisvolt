import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx

from .aws import client
from .config import Settings
from .logging import setup_logging

PROBES = {
    "station-reservation-service": ("GET", "/stations", None),
    "billing-wallet-service": ("GET", "/wallets", None),
    "energy-dispatch-service": ("GET", "/sessions?limit=5", None),
    "graphql-gateway": ("POST", "/graphql", {"query": "{ environment { name revision } featureFlags { name } }"}),
}


def synthetic_probes(base_url: str, service: str, n: int) -> tuple[int, int]:
    method, path, body = PROBES[service]
    errors = 0
    with httpx.Client(base_url=base_url, timeout=3) as c:
        for i in range(n):
            target = "/readyz" if i % 3 == 0 else path
            try:
                r = c.request("GET" if target == "/readyz" else method, target, json=body if target == path else None)
                if r.status_code >= 500 or (target == "/readyz" and r.status_code != 200):
                    errors += 1
            except httpx.HTTPError:
                errors += 1
            time.sleep(0.1)
    return n, errors


def cloudwatch_error_rate(s: Settings, service: str, revision: str, window_min: int) -> tuple[float, float]:
    cw = client("cloudwatch", s.aws_region, s.aws_endpoint_url)
    end = datetime.now(UTC) + timedelta(minutes=1)
    start = end - timedelta(minutes=window_min + 1)
    totals = {}
    for metric in ("Requests", "Http5xx"):
        resp = cw.get_metric_statistics(
            Namespace=s.metrics_namespace,
            MetricName=metric,
            Dimensions=[{"Name": "Service", "Value": service}, {"Name": "Revision", "Value": revision}],
            StartTime=start,
            EndTime=end,
            Period=60,
            Statistics=["Sum"],
        )
        totals[metric] = sum(dp["Sum"] for dp in resp.get("Datapoints", []))
    return totals["Requests"], totals["Http5xx"]


def main() -> int:
    service = os.environ["CANARY_SERVICE"]
    revision = os.environ["CANARY_HASH"]
    base_url = os.environ.get("CANARY_URL", f"http://{service}-canary:8000")
    n = int(os.environ.get("PROBE_REQUESTS", "30"))
    max_rate = float(os.environ.get("MAX_ERROR_RATE", "0.05"))
    window = int(os.environ.get("WINDOW_MINUTES", "5"))
    settle = float(os.environ.get("METRICS_SETTLE_SECONDS", "15"))

    s = Settings(service_name="canary-analysis")
    log = setup_logging(s)
    log.info(
        f"Analizando canary {service} rev={revision}",
        extra={"event": "CANARY_ANALYSIS_STARTED", "target_service": service, "canary_hash": revision},
    )

    sent, probe_errors = synthetic_probes(base_url, service, n)
    time.sleep(settle)
    try:
        cw_requests, cw_5xx = cloudwatch_error_rate(s, service, revision, window)
    except Exception as exc:
        log.error(f"No se pudo leer CloudWatch: {exc}", extra={"event": "CANARY_ANALYSIS_ERROR"})
        cw_requests, cw_5xx = 0.0, 0.0

    probe_rate = probe_errors / sent if sent else 1.0
    cw_rate = (cw_5xx / cw_requests) if cw_requests else 0.0
    passed = probe_rate <= max_rate and cw_rate <= max_rate
    result = {
        "target_service": service,
        "canary_hash": revision,
        "passed": passed,
        "threshold": max_rate,
        "probe_requests": sent,
        "probe_errors": probe_errors,
        "probe_error_rate": round(probe_rate, 4),
        "cloudwatch_requests": cw_requests,
        "cloudwatch_5xx": cw_5xx,
        "cloudwatch_error_rate": round(cw_rate, 4),
    }
    log.log(
        20 if passed else 40,
        "Canary APROBADO: se promueve al 100%" if passed else "Canary RECHAZADO: rollback a la versión stable",
        extra={"event": "CANARY_ANALYSIS_RESULT", **result},
    )
    for h in log.handlers + log.parent.handlers:
        h.flush()
    time.sleep(3)
    print(json.dumps(result))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
