import contextvars
import json
import logging
import queue
import sys
import threading
import time
from datetime import UTC, datetime

from .aws import client
from .config import Settings

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, settings: Settings):
        super().__init__()
        self.s = settings

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.s.service_name,
            "env": self.s.env,
            "version": self.s.app_version,
            "revision": self.s.revision,
            "pod": self.s.pod_name,
            "correlation_id": getattr(record, "correlation_id", None) or correlation_id_var.get(),
            "event": getattr(record, "event", None) or "LOG",
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class CloudWatchLogsHandler(logging.Handler):
    def __init__(self, settings: Settings, flush_interval: float = 2.0):
        super().__init__()
        self.s = settings
        self.flush_interval = flush_interval
        self.q: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=10_000)
        self.stream = f"{settings.pod_name}"
        self._ready = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="cw-logs")
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait((int(record.created * 1000), self.format(record)))
        except queue.Full:
            pass

    def _logs(self):
        return client("logs", self.s.aws_region, self.s.aws_endpoint_url)

    def _ensure_stream(self) -> None:
        if self._ready:
            return
        logs = self._logs()
        try:
            logs.create_log_group(logGroupName=self.s.log_group)
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        try:
            logs.create_log_stream(logGroupName=self.s.log_group, logStreamName=self.stream)
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        self._ready = True

    def _run(self) -> None:
        while True:
            time.sleep(self.flush_interval)
            self.flush()

    def flush(self) -> None:
        batch = []
        while not self.q.empty() and len(batch) < 500:
            batch.append(self.q.get_nowait())
        if not batch:
            return
        try:
            self._ensure_stream()
            batch.sort(key=lambda e: e[0])
            self._logs().put_log_events(
                logGroupName=self.s.log_group,
                logStreamName=self.stream,
                logEvents=[{"timestamp": ts, "message": msg} for ts, msg in batch],
            )
        except Exception as exc:
            self._ready = False
            sys.stderr.write(f'{{"level":"WARNING","message":"cloudwatch logs flush failed: {exc}"}}\n')


def setup_logging(settings: Settings) -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    fmt = JsonFormatter(settings)

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(fmt)
    root.addHandler(stdout)

    if settings.cloudwatch_enabled:
        cw = CloudWatchLogsHandler(settings)
        cw.setFormatter(fmt)
        root.addHandler(cw)

    for noisy in ("uvicorn.access", "botocore", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(settings.service_name)
