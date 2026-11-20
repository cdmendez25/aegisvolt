"""Métricas HTTP publicadas a CloudWatch (namespace AegisVolt/<env>).

Requests y Http5xx por servicio, con dos juegos de dimensiones:
  {Service}            -> alarmas generales del ambiente
  {Service, Revision}  -> el análisis Canary compara SOLO la revisión nueva
"""

import logging
import threading
import time
from collections import Counter

from .aws import client
from .config import Settings

log = logging.getLogger(__name__)


class HttpMetrics:
    def __init__(self, settings: Settings, flush_interval: float = 10.0):
        self.s = settings
        self.flush_interval = flush_interval
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        if settings.cloudwatch_enabled:
            threading.Thread(target=self._run, daemon=True, name="cw-metrics").start()

    def record(self, status_code: int) -> None:
        with self._lock:
            self._counts["Requests"] += 1
            if status_code >= 500:
                self._counts["Http5xx"] += 1

    def _run(self) -> None:
        while True:
            time.sleep(self.flush_interval)
            self.flush()

    def flush(self) -> None:
        with self._lock:
            counts, self._counts = self._counts, Counter()
        if not counts:
            return
        dims_sets = [
            [{"Name": "Service", "Value": self.s.service_name}],
            [
                {"Name": "Service", "Value": self.s.service_name},
                {"Name": "Revision", "Value": self.s.revision},
            ],
        ]
        data = [
            {"MetricName": name, "Dimensions": dims, "Value": float(counts.get(name, 0)), "Unit": "Count"}
            for name in ("Requests", "Http5xx")
            for dims in dims_sets
        ]
        try:
            client("cloudwatch", self.s.aws_region, self.s.aws_endpoint_url).put_metric_data(
                Namespace=self.s.metrics_namespace, MetricData=data
            )
        except Exception as exc:
            log.warning("No se pudieron publicar métricas: %s", exc, extra={"event": "METRICS_FLUSH_ERROR"})
