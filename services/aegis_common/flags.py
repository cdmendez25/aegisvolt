import logging
import time
from dataclasses import dataclass

from .aws import dynamodb_resource
from .config import Settings

log = logging.getLogger(__name__)

SURGE_PRICING = "ENABLE_DYNAMIC_SURGE_PRICING"
FAST_CHARGE_BOOST = "ENABLE_FAST_CHARGE_BOOST"
HARDWARE_FAULT = "SIMULATE_CHARGER_HARDWARE_FAULT"
KNOWN_FLAGS = (SURGE_PRICING, FAST_CHARGE_BOOST, HARDWARE_FAULT)


@dataclass
class Flag:
    name: str
    enabled: bool
    description: str = ""
    updated_at: str = ""
    updated_by: str = ""


class FlagClient:
    def __init__(self, settings: Settings, ttl_seconds: float = 3.0):
        self.s = settings
        self.ttl = ttl_seconds
        self._cache: dict[str, Flag] = {}
        self._loaded_at = 0.0

    @property
    def table(self):
        return dynamodb_resource(self.s.aws_region, self.s.aws_endpoint_url).Table(self.s.table("flags"))

    def _refresh(self) -> None:
        if time.monotonic() - self._loaded_at < self.ttl:
            return
        try:
            items = self.table.scan().get("Items", [])
            self._cache = {
                i["flag_name"]: Flag(
                    name=i["flag_name"],
                    enabled=bool(i.get("enabled", False)),
                    description=i.get("description", ""),
                    updated_at=i.get("updated_at", ""),
                    updated_by=i.get("updated_by", ""),
                )
                for i in items
            }
            self._loaded_at = time.monotonic()
        except Exception:
            log.exception("No se pudieron leer las feature flags", extra={"event": "FLAGS_READ_ERROR"})

    def is_enabled(self, name: str) -> bool:
        self._refresh()
        flag = self._cache.get(name)
        return bool(flag and flag.enabled)

    def all(self) -> list[Flag]:
        self._refresh()
        return sorted(self._cache.values(), key=lambda f: f.name)

    def set(self, name: str, enabled: bool, updated_by: str) -> Flag:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat(timespec="seconds")
        resp = self.table.update_item(
            Key={"flag_name": name},
            UpdateExpression="SET enabled = :e, updated_at = :t, updated_by = :u",
            ConditionExpression="attribute_exists(flag_name)",
            ExpressionAttributeValues={":e": enabled, ":t": now, ":u": updated_by},
            ReturnValues="ALL_NEW",
        )
        self._loaded_at = 0.0
        i = resp["Attributes"]
        return Flag(i["flag_name"], bool(i["enabled"]), i.get("description", ""), now, updated_by)
