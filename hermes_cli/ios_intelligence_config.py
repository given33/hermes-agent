"""Typed, profile-scoped configuration for iOS intelligence services.

Behavioral settings are read from the active Hermes ``config.yaml``.  Vendor
credentials and the data-encryption master key intentionally remain environment
secrets and are not represented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class WeatherPolicy:
    enabled: bool = True
    evaluation_interval_seconds: int = 300
    quiet_start_hour: int = 23
    quiet_end_hour: int = 8
    minimum_leave_probability: float = 0.60
    minimum_alert_score: float = 0.55
    route_buffer_minutes: int = 20
    maximum_window_minutes: int = 180
    dedupe_window_seconds: int = 6 * 3600
    # Commercial default is the production QWeather host. Development API
    # (devapi.qweather.com) must be set explicitly via config/env for local work.
    qweather_base_url: str = "https://api.qweather.com"
    amap_base_url: str = "https://restapi.amap.com"


@dataclass(frozen=True)
class StoragePolicy:
    cold_after_days: int = 30
    archive_batch_points: int = 50_000
    require_encryption: bool = True


@dataclass(frozen=True)
class RelayPolicy:
    command_lease_seconds: int = 120
    command_max_attempts: int = 12
    require_device_token: bool = True
    maximum_event_batch: int = 500


@dataclass(frozen=True)
class SemanticPolicy:
    enabled: bool = True
    minimum_interval_seconds: int = 30 * 60
    timeout_seconds: int = 20
    maximum_schedule_items: int = 12
    anomaly_confidence_threshold: float = 0.45
    minimum_adjustment_confidence: float = 0.55


@dataclass(frozen=True)
class SupervisorPolicy:
    enabled: bool = True
    health_interval_seconds: int = 15
    failure_threshold: int = 3
    restart_backoff_seconds: int = 5
    drain_timeout_seconds: int = 30
    base_port: int = 8760
    blue_green_port_offset: int = 100


@dataclass(frozen=True)
class IOSIntelligenceConfig:
    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    owner_id: str = ""
    database_path: str = ""
    log_directory: str = "logs/ios-intelligence"
    weather: WeatherPolicy = field(default_factory=WeatherPolicy)
    storage: StoragePolicy = field(default_factory=StoragePolicy)
    relay: RelayPolicy = field(default_factory=RelayPolicy)
    semantic: SemanticPolicy = field(default_factory=SemanticPolicy)
    supervisor: SupervisorPolicy = field(default_factory=SupervisorPolicy)


def _section(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def load_ios_intelligence_config(
    config: Mapping[str, Any] | None = None,
) -> IOSIntelligenceConfig:
    """Load settings from an explicit mapping or the active profile config."""

    if config is None:
        from hermes_cli.config import load_config

        config = load_config()
    root = _section(config)
    ios = _section(root.get("ios_intelligence"))
    weather = _section(ios.get("weather"))
    storage = _section(ios.get("storage"))
    relay = _section(ios.get("relay"))
    semantic = _section(ios.get("semantic"))
    supervisor = _section(ios.get("supervisor"))

    weather_defaults = WeatherPolicy()
    storage_defaults = StoragePolicy()
    relay_defaults = RelayPolicy()
    semantic_defaults = SemanticPolicy()
    supervisor_defaults = SupervisorPolicy()
    return IOSIntelligenceConfig(
        enabled=bool(ios.get("enabled", True)),
        timezone=str(ios.get("timezone") or "Asia/Shanghai")[:128],
        owner_id=str(ios.get("owner_id") or "").strip()[:512],
        database_path=str(ios.get("database_path") or "").strip(),
        log_directory=str(ios.get("log_directory") or "logs/ios-intelligence").strip(),
        weather=WeatherPolicy(
            enabled=bool(weather.get("enabled", True)),
            evaluation_interval_seconds=_bounded_int(
                weather.get("evaluation_interval_seconds"),
                weather_defaults.evaluation_interval_seconds,
                30,
                86_400,
            ),
            quiet_start_hour=_bounded_int(
                weather.get("quiet_start_hour"), weather_defaults.quiet_start_hour, 0, 23
            ),
            quiet_end_hour=_bounded_int(
                weather.get("quiet_end_hour"), weather_defaults.quiet_end_hour, 0, 23
            ),
            minimum_leave_probability=_bounded_float(
                weather.get("minimum_leave_probability"),
                weather_defaults.minimum_leave_probability,
                0.0,
                1.0,
            ),
            minimum_alert_score=_bounded_float(
                weather.get("minimum_alert_score"),
                weather_defaults.minimum_alert_score,
                0.0,
                1.0,
            ),
            route_buffer_minutes=_bounded_int(
                weather.get("route_buffer_minutes"), weather_defaults.route_buffer_minutes, 0, 180
            ),
            maximum_window_minutes=_bounded_int(
                weather.get("maximum_window_minutes"), weather_defaults.maximum_window_minutes, 15, 360
            ),
            dedupe_window_seconds=_bounded_int(
                weather.get("dedupe_window_seconds"), weather_defaults.dedupe_window_seconds, 60, 7 * 86_400
            ),
            qweather_base_url=str(
                weather.get("qweather_base_url") or weather_defaults.qweather_base_url
            ).rstrip("/"),
            amap_base_url=str(
                weather.get("amap_base_url") or weather_defaults.amap_base_url
            ).rstrip("/"),
        ),
        storage=StoragePolicy(
            cold_after_days=_bounded_int(
                storage.get("cold_after_days"), storage_defaults.cold_after_days, 1, 3650
            ),
            archive_batch_points=_bounded_int(
                storage.get("archive_batch_points"), storage_defaults.archive_batch_points, 100, 1_000_000
            ),
            require_encryption=bool(storage.get("require_encryption", True)),
        ),
        relay=RelayPolicy(
            command_lease_seconds=_bounded_int(
                relay.get("command_lease_seconds"), relay_defaults.command_lease_seconds, 15, 3600
            ),
            command_max_attempts=_bounded_int(
                relay.get("command_max_attempts"), relay_defaults.command_max_attempts, 1, 100
            ),
            require_device_token=bool(relay.get("require_device_token", True)),
            maximum_event_batch=_bounded_int(
                relay.get("maximum_event_batch"), relay_defaults.maximum_event_batch, 1, 10_000
            ),
        ),
        semantic=SemanticPolicy(
            enabled=bool(semantic.get("enabled", True)),
            minimum_interval_seconds=_bounded_int(
                semantic.get("minimum_interval_seconds"),
                semantic_defaults.minimum_interval_seconds,
                300,
                86_400,
            ),
            timeout_seconds=_bounded_int(
                semantic.get("timeout_seconds"),
                semantic_defaults.timeout_seconds,
                5,
                120,
            ),
            maximum_schedule_items=_bounded_int(
                semantic.get("maximum_schedule_items"),
                semantic_defaults.maximum_schedule_items,
                1,
                50,
            ),
            anomaly_confidence_threshold=_bounded_float(
                semantic.get("anomaly_confidence_threshold"),
                semantic_defaults.anomaly_confidence_threshold,
                0.0,
                1.0,
            ),
            minimum_adjustment_confidence=_bounded_float(
                semantic.get("minimum_adjustment_confidence"),
                semantic_defaults.minimum_adjustment_confidence,
                0.0,
                1.0,
            ),
        ),
        supervisor=SupervisorPolicy(
            enabled=bool(supervisor.get("enabled", True)),
            health_interval_seconds=_bounded_int(
                supervisor.get("health_interval_seconds"),
                supervisor_defaults.health_interval_seconds,
                2,
                300,
            ),
            failure_threshold=_bounded_int(
                supervisor.get("failure_threshold"), supervisor_defaults.failure_threshold, 1, 20
            ),
            restart_backoff_seconds=_bounded_int(
                supervisor.get("restart_backoff_seconds"),
                supervisor_defaults.restart_backoff_seconds,
                1,
                300,
            ),
            drain_timeout_seconds=_bounded_int(
                supervisor.get("drain_timeout_seconds"),
                supervisor_defaults.drain_timeout_seconds,
                1,
                600,
            ),
            base_port=_bounded_int(
                supervisor.get("base_port"), supervisor_defaults.base_port, 1024, 64_000
            ),
            blue_green_port_offset=_bounded_int(
                supervisor.get("blue_green_port_offset"),
                supervisor_defaults.blue_green_port_offset,
                22,
                2000,
            ),
        ),
    )


__all__ = [
    "IOSIntelligenceConfig",
    "RelayPolicy",
    "SemanticPolicy",
    "StoragePolicy",
    "SupervisorPolicy",
    "WeatherPolicy",
    "load_ios_intelligence_config",
]
