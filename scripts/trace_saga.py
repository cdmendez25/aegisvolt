#!/usr/bin/env python3
"""Rastrea una SAGA en CloudWatch Logs por su correlation_id.

Busca en los 4 grupos /aegisvolt/<env>/<servicio> con un filtro JSON
{ $.correlation_id = "<id>" } y muestra la línea de tiempo unificada.

Uso:  uv run scripts/trace_saga.py <correlation_id> [--env dev]
"""

import argparse
import json
import os

import boto3

SERVICES = ["graphql-gateway", "station-reservation-service", "billing-wallet-service", "energy-dispatch-service"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("correlation_id")
    ap.add_argument("--env", default="dev", choices=["dev", "test", "prod"])
    ap.add_argument("--endpoint", default=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"))
    a = ap.parse_args()

    logs = boto3.client(
        "logs", endpoint_url=a.endpoint, region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
    )
    pattern = f'{{ $.correlation_id = "{a.correlation_id}" }}'
    events = []
    for svc in SERVICES:
        group = f"/aegisvolt/{a.env}/{svc}"
        kwargs = {"logGroupName": group, "filterPattern": pattern}
        while True:
            resp = logs.filter_log_events(**kwargs)
            events += [json.loads(e["message"]) for e in resp["events"]]
            if not resp.get("nextToken"):
                break
            kwargs["nextToken"] = resp["nextToken"]

    print(f"filter pattern: {pattern}   grupos: /aegisvolt/{a.env}/*")
    print(f"{len(events)} eventos\n")
    for e in sorted(events, key=lambda e: e["timestamp"]):
        print(f"{e['timestamp'][11:23]}  {e['service']:<28} {e['level']:<7} {e['event']:<28} {e['message'][:70]}")


if __name__ == "__main__":
    main()
