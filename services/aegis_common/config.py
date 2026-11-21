import os
import socket
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    service_name: str
    env: str = field(default_factory=lambda: _env("ENV", "dev"))
    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", "us-east-1"))
    aws_endpoint_url: str | None = field(default_factory=lambda: _env("AWS_ENDPOINT_URL") or None)
    app_version: str = field(default_factory=lambda: _env("APP_VERSION", "local"))
    revision: str = field(default_factory=lambda: _env("POD_TEMPLATE_HASH", "local"))
    pod_name: str = field(default_factory=lambda: _env("POD_NAME", socket.gethostname()))
    cloudwatch_enabled: bool = field(default_factory=lambda: _env("CLOUDWATCH_ENABLED", "false") == "true")
    canary_simulate_errors: bool = field(default_factory=lambda: _env("CANARY_SIMULATE_ERRORS", "false") == "true")

    @property
    def project(self) -> str:
        return "aegisvolt"

    def table(self, logical: str) -> str:
        override = _env(f"TABLE_{logical.upper()}")
        return override or f"{self.project}-{logical}-{self.env}"

    @property
    def log_group(self) -> str:
        return f"/{self.project}/{self.env}/{self.service_name}"

    @property
    def metrics_namespace(self) -> str:
        return f"AegisVolt/{self.env}"
