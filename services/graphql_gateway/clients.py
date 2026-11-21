import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from aegis_common.app import CORRELATION_HEADER


@dataclass
class StepResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    message: str | None = None
    uncertain: bool = False
    status_code: int | None = None


class ServiceClient:
    def __init__(
        self, name: str, base_url: str, timeout: float = 5.0, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, transport=self.transport)

    async def call(self, method: str, path: str, correlation_id: str, **kwargs) -> StepResult:
        headers = {CORRELATION_HEADER: correlation_id}
        try:
            async with self._client() as c:
                resp = await c.request(method, path, headers=headers, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return StepResult(
                ok=False, uncertain=True, error_code="SERVICE_UNREACHABLE", message=f"{self.name}: {type(exc).__name__}"
            )
        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        if resp.is_success:
            return StepResult(
                ok=True, data=body if isinstance(body, dict) else {"items": body}, status_code=resp.status_code
            )
        body = body if isinstance(body, dict) else {}
        return StepResult(
            ok=False,
            uncertain=resp.status_code >= 500,
            error_code=body.get("code", f"HTTP_{resp.status_code}"),
            message=body.get("message", f"{self.name} respondió {resp.status_code}"),
            data=body,
            status_code=resp.status_code,
        )

    async def get(self, path: str, correlation_id: str = "-", **params) -> Any:
        async with self._client() as c:
            resp = await c.get(
                path,
                params={k: v for k, v in params.items() if v is not None},
                headers={CORRELATION_HEADER: correlation_id},
            )
        resp.raise_for_status()
        return resp.json()


@dataclass
class Services:
    reservation: ServiceClient
    billing: ServiceClient
    dispatch: ServiceClient

    @classmethod
    def from_env(cls) -> "Services":
        return cls(
            reservation=ServiceClient(
                "station-reservation-service",
                os.environ.get("STATION_SERVICE_URL", "http://station-reservation-service:8000"),
            ),
            billing=ServiceClient(
                "billing-wallet-service", os.environ.get("BILLING_SERVICE_URL", "http://billing-wallet-service:8000")
            ),
            dispatch=ServiceClient(
                "energy-dispatch-service", os.environ.get("DISPATCH_SERVICE_URL", "http://energy-dispatch-service:8000")
            ),
        )
