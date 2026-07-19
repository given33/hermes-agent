"""Durable account-scoped context for iOS intelligence MCP servers.

The phone and watch are edge collectors.  They upload immutable events and
poll a durable command queue; MCP servers read the resulting snapshots rather
than depending on an app process being awake.  Precise trajectory rows have no
TTL or pruning path in this module by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import gzip
import hashlib
import base64
import hmac
import math
import os
from pathlib import Path
import secrets
import sqlite3
import statistics
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from hermes_cli.config import get_hermes_home
from hermes_cli.sqlite_util import write_txn


WEATHER_MONTHLY_LIMIT = 30_000
WEATHER_SOFT_LIMIT = 28_500
DEFAULT_TIMEZONE = "Asia/Shanghai"
_CURRENT_COLLECTION_INDEX_KINDS = {
    "calendar": "calendar-index",
    "reminder": "reminder-index",
}
_BLOCKED_EVENT_KINDS = frozenset({"apns-token"})
_HOT_ENVELOPE_PREFIX = "HERMES-HOT-AESGCM-1:"
_SHARED_CACHE_OWNER = "__hermes-shared-cache__"
_PLAINTEXT_DEFAULTS = {
    "ios_device_commands.result_json": frozenset({"{}"}),
    "ios_notification_outbox.device_deliveries_json": frozenset({"{}"}),
    "ios_place_graph.name": frozenset({""}),
}
_HOT_JSON_FIELDS = (
    ("ios_events", "payload_json", "ios_events.payload_json"),
    ("ios_snapshots", "payload_json", "ios_snapshots.payload_json"),
    ("ios_trajectory", "payload_json", "ios_trajectory.payload_json"),
    ("ios_places", "payload_json", "ios_places.payload_json"),
    ("ios_device_commands", "payload_json", "ios_device_commands.payload_json"),
    ("ios_device_commands", "result_json", "ios_device_commands.result_json"),
    ("ios_active_forecasts", "payload_json", "ios_active_forecasts.payload_json"),
    ("ios_notification_outbox", "payload_json", "ios_notification_outbox.payload_json"),
    ("ios_notification_outbox", "device_deliveries_json", "ios_notification_outbox.device_deliveries_json"),
    ("ios_place_graph", "metadata_json", "ios_place_graph.metadata_json"),
    ("ios_route_graph", "metadata_json", "ios_route_graph.metadata_json"),
    ("ios_behavior_models", "payload_json", "ios_behavior_models.payload_json"),
    ("ios_behavior_feedback", "payload_json", "ios_behavior_feedback.payload_json"),
    ("ios_weather_quiet_summary", "payload_json", "ios_weather_quiet_summary.payload_json"),
)
_HOT_TEXT_FIELDS = (
    ("ios_trajectory", "motion", "ios_trajectory.motion"),
    ("ios_places", "name", "ios_places.name"),
    ("ios_place_graph", "name", "ios_place_graph.name"),
)
_HOT_NUMBER_FIELDS = (
    ("ios_trajectory", "latitude", "ios_trajectory.latitude"),
    ("ios_trajectory", "longitude", "ios_trajectory.longitude"),
    ("ios_trajectory", "horizontal_accuracy", "ios_trajectory.horizontal_accuracy"),
    ("ios_trajectory", "altitude", "ios_trajectory.altitude"),
    ("ios_trajectory", "speed", "ios_trajectory.speed"),
    ("ios_trajectory", "course", "ios_trajectory.course"),
    ("ios_places", "latitude", "ios_places.latitude"),
    ("ios_places", "longitude", "ios_places.longitude"),
    ("ios_place_graph", "latitude", "ios_place_graph.latitude"),
    ("ios_place_graph", "longitude", "ios_place_graph.longitude"),
)


class _ClosingSQLiteConnection(sqlite3.Connection):
    """Close SQLite handles when used as a context manager on Windows."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ios_events (
    owner_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    PRIMARY KEY (owner_id, device_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_ios_events_owner_kind_time
    ON ios_events(owner_id, kind, observed_at DESC);

CREATE TABLE IF NOT EXISTS ios_snapshots (
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    device_id TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (owner_id, kind, device_id)
);
CREATE INDEX IF NOT EXISTS idx_ios_snapshots_owner_kind_time
    ON ios_snapshots(owner_id, kind, observed_at DESC);

CREATE TABLE IF NOT EXISTS ios_trajectory (
    owner_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    horizontal_accuracy REAL,
    altitude REAL,
    speed REAL,
    course REAL,
    motion TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    PRIMARY KEY (owner_id, device_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_ios_trajectory_owner_time
    ON ios_trajectory(owner_id, observed_at);

CREATE TABLE IF NOT EXISTS ios_places (
    owner_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    place_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    arrived_at INTEGER NOT NULL,
    departed_at INTEGER,
    indoor INTEGER,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (owner_id, device_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_ios_places_owner_arrival
    ON ios_places(owner_id, arrived_at DESC);

CREATE TABLE IF NOT EXISTS ios_upload_cursors (
    owner_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    cursor TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (owner_id, device_id)
);

CREATE TABLE IF NOT EXISTS ios_device_commands (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    device_id TEXT NOT NULL DEFAULT '',
    capability TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    not_before INTEGER NOT NULL,
    expires_at INTEGER,
    created_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at INTEGER,
    acknowledged_at INTEGER,
    result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ios_commands_owner_idempotency
    ON ios_device_commands(owner_id, idempotency_key)
    WHERE idempotency_key <> '';
CREATE INDEX IF NOT EXISTS idx_ios_commands_pull
    ON ios_device_commands(owner_id, device_id, status, not_before);

CREATE TABLE IF NOT EXISTS ios_weather_usage (
    month TEXT PRIMARY KEY,
    request_count INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ios_external_cache (
    provider TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(provider, cache_key)
);

CREATE TABLE IF NOT EXISTS ios_active_forecasts (
    id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_until INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(owner_id, id)
);
CREATE INDEX IF NOT EXISTS idx_ios_forecasts_owner_active
    ON ios_active_forecasts(owner_id, valid_until, valid_from);

CREATE TABLE IF NOT EXISTS ios_notification_outbox (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    deliveries INTEGER NOT NULL DEFAULT 0,
    device_deliveries_json TEXT NOT NULL DEFAULT '{}',
    not_before INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    lease_token TEXT NOT NULL DEFAULT '',
    leased_until INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(owner_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_ios_notification_pending
    ON ios_notification_outbox(state, not_before, expires_at);

CREATE TABLE IF NOT EXISTS ios_account_activity (
    owner_id TEXT PRIMARY KEY,
    last_seen_at INTEGER NOT NULL
);

-- Aggregates are derived from immutable events.  Keeping them in the same
-- account-scoped database makes model updates and device uploads atomic while
-- retaining the original event/trajectory rows as the source of truth.
CREATE TABLE IF NOT EXISTS ios_place_graph (
    owner_id TEXT NOT NULL,
    place_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    visits INTEGER NOT NULL DEFAULT 0,
    weight REAL NOT NULL DEFAULT 0,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    is_home INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(owner_id, place_id)
);
CREATE INDEX IF NOT EXISTS idx_ios_place_graph_owner_weight
    ON ios_place_graph(owner_id, weight DESC, last_seen DESC);

CREATE TABLE IF NOT EXISTS ios_route_graph (
    owner_id TEXT NOT NULL,
    origin_place_id TEXT NOT NULL,
    destination_place_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT '',
    trips INTEGER NOT NULL DEFAULT 0,
    total_duration_seconds REAL NOT NULL DEFAULT 0,
    total_distance_meters REAL NOT NULL DEFAULT 0,
    total_outdoor_minutes REAL NOT NULL DEFAULT 0,
    last_seen INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(owner_id, origin_place_id, destination_place_id, mode)
);
CREATE INDEX IF NOT EXISTS idx_ios_route_graph_owner_source
    ON ios_route_graph(owner_id, origin_place_id, trips DESC, last_seen DESC);

CREATE TABLE IF NOT EXISTS ios_route_samples (
    owner_id TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(owner_id, sample_id)
);

CREATE TABLE IF NOT EXISTS ios_behavior_models (
    owner_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(owner_id, model_name)
);

CREATE TABLE IF NOT EXISTS ios_behavior_feedback (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    label TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ios_feedback_owner_time
    ON ios_behavior_feedback(owner_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS ios_weather_quiet_summary (
    owner_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    delivered_at INTEGER,
    PRIMARY KEY(owner_id, local_date)
);

CREATE TABLE IF NOT EXISTS ios_cold_segments (
    owner_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    start_at INTEGER NOT NULL,
    end_at INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    point_count INTEGER NOT NULL DEFAULT 0,
    checksum TEXT NOT NULL DEFAULT '',
    encrypted INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(owner_id, segment_id)
);
CREATE INDEX IF NOT EXISTS idx_ios_cold_segments_owner_time
    ON ios_cold_segments(owner_id, start_at, end_at);

CREATE TABLE IF NOT EXISTS ios_account_deletion_tombstones (
    owner_id TEXT PRIMARY KEY,
    owner_scope TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    requested_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ios_account_deletions_pending
    ON ios_account_deletion_tombstones(status, updated_at);
"""

_ACCOUNT_OWNED_TABLES = (
    "ios_events",
    "ios_snapshots",
    "ios_trajectory",
    "ios_places",
    "ios_upload_cursors",
    "ios_device_commands",
    "ios_active_forecasts",
    "ios_notification_outbox",
    "ios_account_activity",
    "ios_place_graph",
    "ios_route_graph",
    "ios_route_samples",
    "ios_behavior_models",
    "ios_behavior_feedback",
    "ios_weather_quiet_summary",
    "ios_cold_segments",
)
_ACCOUNT_HOT_TABLES = _ACCOUNT_OWNED_TABLES[:-1]


def _owner(value: Any) -> str:
    result = str(value or "").strip().replace("\x00", "")[:512]
    if not result:
        raise ValueError("owner_id is required")
    return result


def _device(value: Any) -> str:
    return str(value or "").strip().replace("\x00", "")[:256]


def _kind(value: Any) -> str:
    result = str(value or "").strip().lower().replace("_", "-")[:128]
    if not result:
        raise ValueError("event kind is required")
    return result


def _epoch(value: Any = None) -> int:
    if value is None:
        return int(time.time())
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:  # milliseconds from Apple/JS clients
            number /= 1000.0
        return int(number)
    text = str(value).strip()
    if text.isdigit():
        return _epoch(int(text))
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: str) -> dict[str, Any]:
    decoded = json.loads(value or "{}")
    return decoded if isinstance(decoded, dict) else {"value": decoded}


def _advance_upload_cursor(previous: str, candidate: str) -> str:
    """Keep a numeric device cursor monotonic while preserving opaque cursors.

    Native iOS queues use their durable sequence number as the cursor. A
    delayed batch can therefore contain a new event while carrying an older
    cursor; blindly replacing the stored value would make the next upload
    start behind an already acknowledged sequence. Some integrations still
    use opaque cursors, for which ordering is unknowable, so those retain the
    historical last-write behavior.
    """

    prior = str(previous or "")
    next_cursor = str(candidate or "")
    if not prior:
        return next_cursor
    if not next_cursor:
        return prior
    try:
        return str(max(int(prior), int(next_cursor)))
    except (TypeError, ValueError):
        # Do not replace a known numeric cursor with an opaque value.
        if prior.isdigit() and not next_cursor.isdigit():
            return prior
        return next_cursor


def _timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or DEFAULT_TIMEZONE))
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {name}") from exc


def _configured_secret(*values: str | None) -> str | None:
    for value in values:
        candidate = str(value or "").strip()
        if candidate and not (candidate.startswith("${") and candidate.endswith("}")):
            return candidate
    return None


def _redact_secret_text(value: Any, *secrets: str | None) -> str:
    text = str(value)
    for secret in secrets:
        candidate = str(secret or "")
        if candidate:
            text = text.replace(candidate, "[REDACTED]")
    return text


def _distance_meters(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    radius = 6_371_000.0
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


# Map behavior snapshot kinds / logical features onto the MCP capability that
# owns them. When that capability is DISABLED/QUARANTINED the scheduler passes a
# reduced weight so evaluate_behavior stops treating the stale snapshot as truth.
_FEATURE_WEIGHT_CAPABILITY_BY_KIND = {
    "motion": "ios-motion",
    "power": "ios-power",
    "health-sleep": "ios-health-sleep",
    "health-heart": "ios-health-heart",
    "health-oxygen": "ios-health-oxygen",
    "health-activity": "ios-health-activity",
    "screen-time": "ios-screen-time",
    "device": "ios-device",
    "watch": "ios-watch",
    "reminder": "ios-reminders",
    "calendar": "ios-calendar",
}

_CONTEXT_FEATURE_KINDS = {
    "power": "power",
    "sleep": "health-sleep",
    "heart": "health-heart",
    "oxygen": "health-oxygen",
    "activity": "health-activity",
    "screen_time": "screen-time",
    "device": "device",
    "watch": "watch",
    "reminder": "reminder",
}


def _feature_weight(feature_weights: Mapping[str, Any] | None, capability: str) -> float:
    """Clamp a per-MCP weight to [0, 1]; missing capabilities default to full trust."""

    if not feature_weights:
        return 1.0
    raw = feature_weights.get(capability, 1.0)
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 1.0


_FEATURE_WEIGHT_STATE_MULTIPLIER = {
    "DISABLED": 0.0,
    "QUARANTINED": 0.2,
    "DEGRADED": 0.5,
    "RECOVERING": 0.6,
    "UPGRADING": 0.8,
}

# Capabilities whose snapshots feed behavior_predict / evaluate_behavior.
KNOWN_FEATURE_CAPABILITIES = tuple(sorted({
    *_FEATURE_WEIGHT_CAPABILITY_BY_KIND.values(),
    "ios-motion",
    "ios-calendar",
}))

# When the MCP supervisor cannot be read, never fail open to full trust.
_SUPERVISOR_UNAVAILABLE_FEATURE_WEIGHT = 0.5


def load_ios_feature_weights(supervisor: Any | None = None) -> dict[str, float]:
    """Map MCP supervisor service health onto per-capability feature weights.

    Healthy / unknown services default to full trust (1.0) via
    :func:`_feature_weight` when the capability is absent from the map.
    Supervisor construction or status-read failures return a conservative
    fail-closed map so behavior_predict does not treat stale sensors as truth.
    """

    if supervisor is None:
        try:
            from hermes_cli.ios_mcp_supervisor import IOSMCPSupervisor

            supervisor = IOSMCPSupervisor()
        except Exception:
            return {
                capability: _SUPERVISOR_UNAVAILABLE_FEATURE_WEIGHT
                for capability in KNOWN_FEATURE_CAPABILITIES
            }
    try:
        statuses = supervisor.statuses()
    except Exception:
        return {
            capability: _SUPERVISOR_UNAVAILABLE_FEATURE_WEIGHT
            for capability in KNOWN_FEATURE_CAPABILITIES
        }
    weights: dict[str, float] = {}
    for item in statuses or ():
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        state = str(item.get("state") or "RUNNING").upper()
        weights[name] = _FEATURE_WEIGHT_STATE_MULTIPLIER.get(state, 1.0)
    return weights


class IOSIntelligenceStore:
    """SQLite source of truth shared by the independent iOS MCP processes."""

    schema_version = 9
    _schema_lock = threading.RLock()

    def __init__(self, base_dir: str | os.PathLike[str] | None = None):
        root = Path(base_dir) if base_dir is not None else Path(get_hermes_home())
        self.path = root if root.suffix in {".db", ".sqlite", ".sqlite3"} else root / "ios-intelligence.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._master_secret = self._load_master_secret()
        with self._schema_lock:
            with self._connect() as conn:
                current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if current_version > self.schema_version:
                    raise RuntimeError(
                        "ios-intelligence.db was created by a newer Hermes version "
                        f"(schema {current_version} > {self.schema_version})"
                    )
                conn.executescript(_SCHEMA)
                conn.executescript("\n".join(
                    f"CREATE TRIGGER IF NOT EXISTS guard_deleted_{table} "
                    f"BEFORE INSERT ON {table} "
                    "WHEN EXISTS (SELECT 1 FROM ios_account_deletion_tombstones "
                    "WHERE owner_id=NEW.owner_id) "
                    "BEGIN SELECT RAISE(ABORT, 'account deletion tombstone is active'); END;"
                    for table in _ACCOUNT_OWNED_TABLES
                ))
                # Every isolated MCP process opens this database at startup. Hold
                # one immediate writer lock across column checks and ALTERs so the
                # first deployment of a new schema is race-free.
                needs_compaction = False
                with write_txn(conn):
                    locked_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    if locked_version > self.schema_version:
                        raise RuntimeError(
                            "ios-intelligence.db was upgraded while this process was waiting "
                            f"(schema {locked_version} > {self.schema_version})"
                        )
                    columns = {
                        str(row["name"])
                        for row in conn.execute("PRAGMA table_info(ios_notification_outbox)")
                    }
                    if "device_deliveries_json" not in columns:
                        conn.execute(
                            "ALTER TABLE ios_notification_outbox "
                            "ADD COLUMN device_deliveries_json TEXT NOT NULL DEFAULT '{}'"
                        )
                    if "lease_token" not in columns:
                        conn.execute(
                            "ALTER TABLE ios_notification_outbox "
                            "ADD COLUMN lease_token TEXT NOT NULL DEFAULT ''"
                        )
                    if "leased_until" not in columns:
                        conn.execute(
                            "ALTER TABLE ios_notification_outbox "
                            "ADD COLUMN leased_until INTEGER NOT NULL DEFAULT 0"
                        )
                    if "attempts" not in {
                        str(row["name"])
                        for row in conn.execute("PRAGMA table_info(ios_device_commands)")
                    }:
                        conn.execute(
                            "ALTER TABLE ios_device_commands "
                            "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
                        )
                    deletion_columns = {
                        str(row["name"])
                        for row in conn.execute("PRAGMA table_info(ios_account_deletion_tombstones)")
                    }
                    if "owner_scope" not in deletion_columns:
                        conn.execute(
                            "ALTER TABLE ios_account_deletion_tombstones "
                            "ADD COLUMN owner_scope TEXT NOT NULL DEFAULT ''"
                        )
                    if locked_version < 4:
                        placeholders = ",".join("?" for _kind_name in _BLOCKED_EVENT_KINDS)
                        blocked_kinds = tuple(_BLOCKED_EVENT_KINDS)
                        conn.execute(
                            f"DELETE FROM ios_events WHERE kind IN ({placeholders})",
                            blocked_kinds,
                        )
                        conn.execute(
                            f"DELETE FROM ios_snapshots WHERE kind IN ({placeholders})",
                            blocked_kinds,
                        )
                    if locked_version < 5:
                        self._migrate_hot_encryption(conn)
                    needs_compaction = locked_version < self.schema_version
                    # Leave a recoverable intermediate marker until the secure
                    # checkpoint/VACUUM has completed. A crash retries compaction
                    # on the next process rather than trusting stale free pages.
                    if needs_compaction:
                        conn.execute("PRAGMA user_version=5")
                    else:
                        conn.execute(f"PRAGMA user_version={self.schema_version}")
                if needs_compaction:
                    self._secure_compact(conn)
                    with write_txn(conn):
                        conn.execute(f"PRAGMA user_version={self.schema_version}")
            self._cleanup_orphan_cold_files()

    def _cleanup_orphan_cold_files(self) -> None:
        """Remove cold files left between atomic install and index commit."""

        root = self.path.parent / "ios-cold"
        if not root.is_dir():
            return
        try:
            with self._connect() as conn:
                indexed = {
                    str(row[0])
                    for row in conn.execute("SELECT file_path FROM ios_cold_segments")
                    if str(row[0] or "").strip()
                }
            for owner_dir in root.iterdir():
                if not owner_dir.is_dir():
                    continue
                for candidate in owner_dir.iterdir():
                    if not candidate.is_file():
                        continue
                    if candidate.name.startswith(".ios-cold-") or str(candidate) not in indexed:
                        try:
                            candidate.unlink()
                        except OSError:
                            continue
                try:
                    owner_dir.rmdir()
                except OSError:
                    pass
        except (OSError, sqlite3.Error):
            # Cleanup is best effort; registered paths remain recoverable via the
            # account deletion tombstone when the next scheduler pass runs.
            return

    def _load_master_secret(self) -> bytes:
        """Load the deployment key or create a private local-development key."""

        configured = _configured_secret(
            os.getenv("HERMES_IOS_DATA_KEY"),
            os.getenv("HERMES_DATA_ENCRYPTION_KEY"),
        )
        if configured:
            return configured.encode("utf-8")
        # Production deployment validates the environment key. A sidecar key
        # keeps local/test stores encrypted as well without making a predictable
        # fallback part of the account boundary.
        key_path = self.path.with_name(self.path.name + ".key")
        key_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                value = secrets.token_urlsafe(48).encode("ascii")
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                fd = os.open(str(key_path), flags, 0o600)
                try:
                    os.write(fd, value)
                finally:
                    os.close(fd)
                try:
                    os.chmod(key_path, 0o600)
                except OSError:
                    pass
                return value
            except FileExistsError:
                try:
                    value = key_path.read_bytes().strip()
                except OSError:
                    continue
                if value:
                    return value

    @staticmethod
    def _is_hot_envelope(value: Any) -> bool:
        return isinstance(value, str) and value.startswith(_HOT_ENVELOPE_PREFIX)

    def _account_key(self, owner_id: str) -> bytes:
        owner = _owner(owner_id).encode("utf-8")
        return hmac.new(
            self._master_secret,
            b"hermes-ios-account-v1\0" + owner,
            hashlib.sha256,
        ).digest()

    def _pseudonymous_place_id(self, owner_id: str, latitude: float, longitude: float) -> str:
        digest = hmac.new(
            self._account_key(owner_id),
            f"place\0{latitude:.5f}\0{longitude:.5f}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]
        return f"geo:{digest}"

    def _seal_text(self, owner_id: str, value: Any, purpose: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        owner = _owner(owner_id)
        aad = f"{owner}\0{purpose}".encode("utf-8")
        ciphertext = AESGCM(self._account_key(owner)).encrypt(
            nonce,
            str(value).encode("utf-8"),
            aad,
        )
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return _HOT_ENVELOPE_PREFIX + encoded

    def _open_text(
        self,
        owner_id: str,
        value: Any,
        purpose: str,
    ) -> str:
        if not self._is_hot_envelope(value):
            if (
                purpose in _PLAINTEXT_DEFAULTS
                and str(value or "") in _PLAINTEXT_DEFAULTS[purpose]
            ):
                return str(value or "")
            raise RuntimeError("iOS hot data envelope is missing")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            raw = base64.urlsafe_b64decode(str(value)[len(_HOT_ENVELOPE_PREFIX):].encode("ascii"))
            nonce, ciphertext = raw[:12], raw[12:]
            owner = _owner(owner_id)
            aad = f"{owner}\0{purpose}".encode("utf-8")
            return AESGCM(self._account_key(owner)).decrypt(
                nonce,
                ciphertext,
                aad,
            ).decode("utf-8")
        except Exception as exc:
            raise RuntimeError("iOS hot data authentication failed") from exc

    def _seal_json(self, owner_id: str, value: Any, purpose: str) -> str:
        return self._seal_text(owner_id, _json(value), purpose)

    def _open_json(
        self,
        owner_id: str,
        value: Any,
        purpose: str,
    ) -> dict[str, Any]:
        decoded = json.loads(self._open_text(owner_id, value, purpose) or "{}")
        return decoded if isinstance(decoded, dict) else {"value": decoded}

    def _seal_number(self, owner_id: str, value: Any, purpose: str) -> str | None:
        if value is None:
            return None
        return self._seal_text(owner_id, repr(float(value)), purpose)

    def _open_number(self, owner_id: str, value: Any, purpose: str) -> float | None:
        if value is None:
            return None
        try:
            return float(self._open_text(owner_id, value, purpose))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("iOS hot numeric data is invalid") from exc

    def _migrate_hot_encryption(self, conn: sqlite3.Connection) -> None:
        """Seal legacy JSON, text, and coordinate columns in-place."""

        for table, column, purpose in _HOT_JSON_FIELDS:
            rows = conn.execute(f"SELECT rowid,owner_id,{column} FROM {table}").fetchall()
            for row in rows:
                value = row[column]
                if self._is_hot_envelope(value):
                    continue
                conn.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?",
                    (self._seal_text(str(row["owner_id"]), value or "{}", purpose), row["rowid"]),
                )
        for table, column, purpose in _HOT_TEXT_FIELDS:
            rows = conn.execute(f"SELECT rowid,owner_id,{column} FROM {table}").fetchall()
            for row in rows:
                value = row[column]
                if self._is_hot_envelope(value):
                    continue
                conn.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?",
                    (self._seal_text(str(row["owner_id"]), value or "", purpose), row["rowid"]),
                )
        for table, column, purpose in _HOT_NUMBER_FIELDS:
            rows = conn.execute(f"SELECT rowid,owner_id,{column} FROM {table}").fetchall()
            for row in rows:
                value = row[column]
                if value is None or self._is_hot_envelope(value):
                    continue
                conn.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?",
                    (self._seal_number(str(row["owner_id"]), value, purpose), row["rowid"]),
                )
        # The external cache is shared and short-lived. Dropping legacy keys
        # avoids retaining raw coordinates in cache_key while preserving quota
        # correctness through the new hashed key path.
        conn.execute("DELETE FROM ios_external_cache")
        self._migrate_legacy_geo_place_ids(conn)

    def _migrate_legacy_geo_place_ids(self, conn: sqlite3.Connection) -> None:
        """Replace coordinate-bearing legacy place IDs with keyed pseudonyms."""

        replacements: dict[tuple[str, str], str] = {}
        rows_to_update: list[tuple[str, int, str, str]] = []
        for table in ("ios_places", "ios_place_graph"):
            for row in conn.execute(f"SELECT rowid,owner_id,place_id FROM {table}").fetchall():
                old = str(row["place_id"] or "")
                parts = old.split(":")
                if len(parts) != 3 or parts[0] != "geo":
                    continue
                try:
                    latitude, longitude = float(parts[1]), float(parts[2])
                except (TypeError, ValueError):
                    continue
                new = self._pseudonymous_place_id(str(row["owner_id"]), latitude, longitude)
                if new == old:
                    continue
                temporary = "migrating:" + hashlib.sha256(
                    f"{row['owner_id']}\0{old}\0{row['rowid']}".encode("utf-8")
                ).hexdigest()[:40]
                rows_to_update.append((table, int(row["rowid"]), temporary, new))
                replacements[(str(row["owner_id"]), old)] = new
        for table, rowid, temporary, _new in rows_to_update:
            conn.execute(f"UPDATE {table} SET place_id=? WHERE rowid=?", (temporary, rowid))
        for (owner_id, old), new in replacements.items():
            conn.execute(
                "UPDATE ios_route_graph SET origin_place_id=? WHERE owner_id=? AND origin_place_id=?",
                (new, owner_id, old),
            )
            conn.execute(
                "UPDATE ios_route_graph SET destination_place_id=? WHERE owner_id=? AND destination_place_id=?",
                (new, owner_id, old),
            )
        for table, rowid, temporary, new in rows_to_update:
            conn.execute(f"UPDATE {table} SET place_id=? WHERE rowid=?", (new, rowid))

    def _decode_hot_row(self, owner_id: str, table: str, row: Mapping[str, Any]) -> dict[str, Any]:
        """Return an account export/archive row with its envelope fields opened."""

        item = dict(row)
        for field_table, column, purpose in _HOT_JSON_FIELDS:
            if field_table == table and column in item:
                item[column] = self._open_text(
                    owner_id,
                    item[column],
                    purpose,
                )
        for field_table, column, purpose in _HOT_TEXT_FIELDS:
            if field_table == table and column in item:
                item[column] = self._open_text(
                    owner_id,
                    item[column],
                    purpose,
                )
        for field_table, column, purpose in _HOT_NUMBER_FIELDS:
            if field_table == table and column in item:
                item[column] = self._open_number(owner_id, item[column], purpose)
        return item

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=_ClosingSQLiteConnection)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA secure_delete=ON")
            # Changing journal mode takes a database-wide lock and can race
            # when all 21 MCP processes start together. SQLite does not always
            # honor busy_timeout for this PRAGMA, so retry the locked operation
            # explicitly until the same bounded connection timeout elapses.
            deadline = time.monotonic() + 30.0
            while True:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except Exception:
            conn.close()
            raise

    def schema_status(self) -> dict[str, Any]:
        """Report code schema vs live SQLite ``user_version`` for /health."""

        with self._connect() as conn:
            db_user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        code_schema_version = int(self.schema_version)
        return {
            "code_schema_version": code_schema_version,
            "db_user_version": db_user_version,
            "schema_version": code_schema_version,
            "migrated": db_user_version >= code_schema_version,
            "compatible": db_user_version == code_schema_version,
        }

    def _secure_compact(self, conn: sqlite3.Connection) -> None:
        """Erase legacy free pages and WAL copies after an encryption migration."""

        deadline = time.monotonic() + 30.0
        while True:
            try:
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is not None and int(checkpoint[0]) != 0:
                    raise sqlite3.OperationalError("database is locked")
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    @staticmethod
    def _touch(conn: sqlite3.Connection, owner_id: str, now: int) -> None:
        conn.execute(
            "INSERT INTO ios_account_activity(owner_id,last_seen_at) VALUES(?,?) "
            "ON CONFLICT(owner_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (owner_id, now),
        )

    def _place_id_for_event(
        self,
        conn: sqlite3.Connection,
        owner_id: str,
        payload: Mapping[str, Any],
        event_id: str,
    ) -> str:
        explicit = str(payload.get("place_id") or payload.get("id") or "").strip()
        if explicit:
            return explicit[:256]
        latitude = payload.get("latitude", payload.get("lat"))
        longitude = payload.get("longitude", payload.get("lon", payload.get("lng")))
        if latitude is None or longitude is None:
            return event_id[:256]
        latitude, longitude = float(latitude), float(longitude)
        accuracy = payload.get("horizontal_accuracy", payload.get("accuracy", 50))
        try:
            threshold = max(50.0, min(200.0, float(accuracy or 50) * 1.5))
        except (TypeError, ValueError):
            threshold = 75.0
        rows = conn.execute(
            "SELECT place_id,latitude,longitude FROM ios_place_graph "
            "WHERE owner_id=? AND latitude IS NOT NULL AND longitude IS NOT NULL",
            (owner_id,),
        ).fetchall()
        source = "ios_place_graph"
        if not rows:
            source = "ios_places"
            rows = conn.execute(
                "SELECT place_id,latitude,longitude FROM ios_places "
                "WHERE owner_id=? AND latitude IS NOT NULL AND longitude IS NOT NULL "
                "ORDER BY arrived_at DESC LIMIT 1000",
                (owner_id,),
            ).fetchall()
        nearest: tuple[float, str] | None = None
        for row in rows:
            row_latitude = self._open_number(owner_id, row["latitude"], f"{source}.latitude")
            row_longitude = self._open_number(owner_id, row["longitude"], f"{source}.longitude")
            if row_latitude is None or row_longitude is None:
                continue
            distance = _distance_meters(latitude, longitude, row_latitude, row_longitude)
            if distance <= threshold and (nearest is None or distance < nearest[0]):
                nearest = (distance, str(row["place_id"]))
        if nearest is not None:
            return nearest[1]
        # Keep the deterministic fallback key finer than the visit merge
        # radius while keeping raw coordinates out of persistent identifiers.
        return self._pseudonymous_place_id(owner_id, latitude, longitude)

    def ingest_events(
        self,
        owner_id: str,
        device_id: str,
        events: Sequence[Mapping[str, Any]],
        cursor: str | int | None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """Idempotently ingest device events and advance its upload cursor."""

        owner_id, device_id = _owner(owner_id), _device(device_id)
        if timezone is not None:
            _timezone(timezone)
        if not device_id:
            raise ValueError("device_id is required")
        if len(events) > 10_000:
            raise ValueError("A batch may contain at most 10000 events")
        now = int(time.time())
        accepted = duplicates = discarded = 0
        max_cursor = str(cursor or "")
        learned_places: list[dict[str, Any]] = []
        feedback_events: list[tuple[str, dict[str, Any], int, str, str]] = []
        with self._connect() as conn, write_txn(conn):
            if conn.execute(
                "SELECT 1 FROM ios_account_deletion_tombstones WHERE owner_id=?",
                (owner_id,),
            ).fetchone() is not None:
                raise PermissionError("account deletion tombstone is active")
            prior_cursor_row = conn.execute(
                "SELECT cursor FROM ios_upload_cursors WHERE owner_id=? AND device_id=?",
                (owner_id, device_id),
            ).fetchone()
            for index, raw in enumerate(events):
                event = dict(raw)
                event_id = str(event.get("event_id") or event.get("id") or "").strip()
                if not event_id:
                    event_id = f"{max_cursor}:{index}" if max_cursor else uuid.uuid4().hex
                kind = _kind(event.get("kind") or event.get("type"))
                if kind in _BLOCKED_EVENT_KINDS:
                    # Delivery tokens use the dedicated APNs registration
                    # store. Treat legacy queued token events as consumed so
                    # old clients can acknowledge them without retaining the
                    # credential in behavioral history or account exports.
                    discarded += 1
                    duplicates += 1
                    continue
                observed_at = _epoch(event.get("observed_at") or event.get("timestamp"))
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    payload = {
                        key: value for key, value in event.items()
                        if key not in {"id", "event_id", "kind", "type", "observed_at", "timestamp"}
                    }
                event_device_id = _device(
                    event.get("source_device_id")
                    or payload.get("source_device_id")
                    or device_id
                ) or device_id
                # Independent devices (ones that upload under their own cursor)
                # may only be written by themselves. Companion/watch ids that
                # never self-uploaded may still be attributed by a relay phone.
                if event_device_id != device_id:
                    owns_cursor = conn.execute(
                        "SELECT 1 FROM ios_upload_cursors "
                        "WHERE owner_id=? AND device_id=?",
                        (owner_id, event_device_id),
                    ).fetchone()
                    if owns_cursor is not None:
                        event_device_id = device_id
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO ios_events VALUES(?,?,?,?,?,?,?)",
                    (
                        owner_id, event_device_id, event_id, kind, observed_at,
                        self._seal_json(owner_id, payload, "ios_events.payload_json"), now,
                    ),
                ).rowcount
                if not inserted:
                    duplicates += 1
                    continue
                accepted += 1
                feedback_label = {
                    "place": "actual-destination",
                    "place-visit": "actual-destination",
                    "visit": "actual-destination",
                    "motion": "actual-motion",
                    "notification-feedback": "notification-value",
                    "weather-feedback": "notification-value",
                }.get(kind)
                if feedback_label:
                    feedback_events.append(
                        (feedback_label, dict(payload), observed_at, event_id, event_device_id)
                    )
                conn.execute(
                    "INSERT INTO ios_snapshots VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(owner_id,kind,device_id) DO UPDATE SET "
                    "observed_at=excluded.observed_at,payload_json=excluded.payload_json,updated_at=excluded.updated_at "
                    "WHERE excluded.observed_at >= ios_snapshots.observed_at",
                    (
                        owner_id, kind, event_device_id, observed_at,
                        self._seal_json(owner_id, payload, "ios_snapshots.payload_json"), now,
                    ),
                )
                if kind in {"location", "trajectory", "location-point"}:
                    latitude = payload.get("latitude", payload.get("lat"))
                    longitude = payload.get("longitude", payload.get("lon", payload.get("lng")))
                    if latitude is None or longitude is None:
                        raise ValueError("Location events require latitude and longitude")
                    conn.execute(
                        "INSERT OR IGNORE INTO ios_trajectory VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            owner_id, event_device_id, event_id, observed_at,
                            self._seal_number(owner_id, latitude, "ios_trajectory.latitude"),
                            self._seal_number(owner_id, longitude, "ios_trajectory.longitude"),
                            self._seal_number(owner_id, payload.get("horizontal_accuracy"), "ios_trajectory.horizontal_accuracy"),
                            self._seal_number(owner_id, payload.get("altitude"), "ios_trajectory.altitude"),
                            self._seal_number(owner_id, payload.get("speed"), "ios_trajectory.speed"),
                            self._seal_number(owner_id, payload.get("course"), "ios_trajectory.course"),
                            self._seal_text(owner_id, str(payload.get("motion") or "")[:64], "ios_trajectory.motion"),
                            self._seal_json(owner_id, payload, "ios_trajectory.payload_json"),
                        ),
                    )
                if kind in {"place", "place-visit", "visit"}:
                    arrived_at = _epoch(payload.get("arrived_at") or observed_at)
                    departed = payload.get("departed_at")
                    departed_at = _epoch(departed) if departed is not None else None
                    place_id = self._place_id_for_event(conn, owner_id, payload, event_id)
                    projected = conn.execute(
                        "SELECT device_id,event_id,departed_at,payload_json FROM ios_places "
                        "WHERE owner_id=? AND place_id=? AND arrived_at=? LIMIT 1",
                        (owner_id, place_id, arrived_at),
                    ).fetchone()
                    if projected is not None:
                        # Core Location emits the arrival first and the same
                        # visit again after departure. Keep both raw events in
                        # ios_events, but maintain one canonical visit row so
                        # an obsolete open callback cannot become current
                        # place forever or count as a second visit.
                        merged_payload = {
                            **self._open_json(owner_id, projected["payload_json"], "ios_places.payload_json"),
                            **payload,
                        }
                        canonical_departure = departed_at or projected["departed_at"]
                        if canonical_departure is not None:
                            merged_payload["departed_at"] = canonical_departure
                        clean_name = str(payload.get("name") or "")[:512]
                        conn.execute(
                            "UPDATE ios_places SET "
                            "name=CASE WHEN ?<>'' THEN ? ELSE name END,"
                            "latitude=COALESCE(?,latitude),longitude=COALESCE(?,longitude),"
                            "departed_at=COALESCE(?,departed_at),indoor=COALESCE(?,indoor),payload_json=? "
                            "WHERE owner_id=? AND device_id=? AND event_id=?",
                            (
                                clean_name,
                                self._seal_text(owner_id, clean_name, "ios_places.name") if clean_name else "",
                                self._seal_number(owner_id, payload.get("latitude"), "ios_places.latitude"),
                                self._seal_number(owner_id, payload.get("longitude"), "ios_places.longitude"),
                                departed_at,
                                None if payload.get("indoor") is None else int(bool(payload.get("indoor"))),
                                self._seal_json(owner_id, merged_payload, "ios_places.payload_json"),
                                owner_id, projected["device_id"], projected["event_id"],
                            ),
                        )
                    else:
                        conn.execute(
                            "INSERT OR IGNORE INTO ios_places VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                owner_id, event_device_id, event_id, place_id,
                                self._seal_text(owner_id, str(payload.get("name") or "")[:512], "ios_places.name"),
                                self._seal_number(owner_id, payload.get("latitude"), "ios_places.latitude"),
                                self._seal_number(owner_id, payload.get("longitude"), "ios_places.longitude"),
                                arrived_at, departed_at,
                                None if payload.get("indoor") is None else int(bool(payload.get("indoor"))),
                                self._seal_json(owner_id, payload, "ios_places.payload_json"),
                            ),
                        )
                        learned_places.append({
                            "place_id": place_id,
                            "name": str(payload.get("name") or ""),
                            "latitude": payload.get("latitude"),
                            "longitude": payload.get("longitude"),
                            "arrived_at": arrived_at,
                            "departed_at": departed,
                            "indoor": payload.get("indoor"),
                            "metadata": payload,
                        })
            if accepted or discarded or prior_cursor_row is None:
                stored_cursor = _advance_upload_cursor(
                    "" if prior_cursor_row is None else str(prior_cursor_row["cursor"]),
                    max_cursor,
                )
                conn.execute(
                    "INSERT INTO ios_upload_cursors VALUES(?,?,?,?) "
                    "ON CONFLICT(owner_id,device_id) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                    (owner_id, device_id, stored_cursor, now),
                )
                max_cursor = stored_cursor
            else:
                max_cursor = str(prior_cursor_row["cursor"])
            self._touch(conn, owner_id, now)
        # Derived aggregates use their own short transaction so a malformed
        # aggregate cannot invalidate an already accepted immutable event.
        for place in learned_places:
            try:
                self.learn_place(owner_id, **place)
            except (TypeError, ValueError):
                continue
        for label, payload, observed_at, event_id, event_device_id in feedback_events:
            self.record_behavior_feedback(
                owner_id,
                label,
                payload,
                observed_at=observed_at,
                feedback_id=f"event:{event_device_id}:{event_id}",
            )
        return {"accepted": accepted, "duplicates": duplicates, "next_cursor": max_cursor}

    def record_snapshot(
        self,
        owner_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        device_id: str = "server",
        observed_at: Any = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": event_id or uuid.uuid4().hex,
            "kind": kind,
            "observed_at": observed_at,
            "payload": dict(payload),
        }
        return self.ingest_events(owner_id, device_id, [event], cursor=event["event_id"])

    def latest_snapshot(self, owner_id: str, kind: str) -> dict[str, Any] | None:
        owner_id, kind = _owner(owner_id), _kind(kind)
        if kind in _CURRENT_COLLECTION_INDEX_KINDS:
            items = self.list_snapshots(owner_id, kind, limit=1)
            return items[0] if items else None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT device_id,observed_at,payload_json FROM ios_snapshots "
                "WHERE owner_id=? AND kind=? ORDER BY observed_at DESC LIMIT 1",
                (owner_id, kind),
            ).fetchone()
        if row is None:
            return None
        return {
            "kind": kind,
            "device_id": row["device_id"],
            "observed_at": row["observed_at"],
            "data": self._open_json(owner_id, row["payload_json"], "ios_snapshots.payload_json"),
        }

    def list_snapshots(self, owner_id: str, kind: str, limit: int = 100) -> list[dict[str, Any]]:
        owner_id, kind = _owner(owner_id), _kind(kind)
        limit = max(1, min(int(limit), 1000))
        if kind in _CURRENT_COLLECTION_INDEX_KINDS:
            return self._list_current_collection(owner_id, kind, limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id,device_id,observed_at,payload_json FROM ios_events "
                "WHERE owner_id=? AND kind=? ORDER BY observed_at DESC LIMIT ?",
                (owner_id, kind, limit),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "device_id": row["device_id"],
                "observed_at": row["observed_at"],
                "data": self._open_json(owner_id, row["payload_json"], "ios_events.payload_json"),
            }
            for row in rows
        ]

    def _list_current_collection(
        self,
        owner_id: str,
        kind: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Project mutable EventKit collections without deleting immutable history."""

        index_kind = _CURRENT_COLLECTION_INDEX_KINDS[kind]
        with self._connect() as conn:
            index_rows = conn.execute(
                "SELECT device_id,payload_json,updated_at FROM ios_snapshots "
                "WHERE owner_id=? AND kind=?",
                (owner_id, index_kind),
            ).fetchall()
            rows = conn.execute(
                "SELECT rowid,event_id,device_id,observed_at,payload_json,received_at "
                "FROM ios_events WHERE owner_id=? AND kind=? "
                "ORDER BY received_at DESC,rowid DESC",
                (owner_id, kind),
            ).fetchall()

        indexes: dict[str, dict[str, Any]] = {}
        for row in index_rows:
            payload = self._open_json(owner_id, row["payload_json"], "ios_snapshots.payload_json")
            raw_ids = payload.get("ids")
            raw_versions = payload.get("versions")
            if not isinstance(raw_ids, list) or not isinstance(raw_versions, dict):
                continue
            ids = {str(value) for value in raw_ids if str(value)}
            versions = {
                str(item_id): str(event_id)
                for item_id, event_id in raw_versions.items()
                if str(item_id) and str(event_id)
            }
            indexes[row["device_id"]] = {
                "ids": ids,
                "versions": versions,
                "updated_at": int(row["updated_at"]),
            }

        selected: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
        for row in rows:
            data = self._open_json(owner_id, row["payload_json"], "ios_events.payload_json")
            raw_item_id = data.get("id")
            item_id = str(raw_item_id).strip() if raw_item_id is not None else ""
            index = indexes.get(row["device_id"])
            if index is not None:
                expected_event_id = index["versions"].get(item_id)
                if item_id not in index["ids"] or expected_event_id != row["event_id"]:
                    continue
                freshness = int(index["updated_at"])
            else:
                # Events uploaded by older app versions have no collection
                # index. Keep them visible and collapse content revisions by
                # their stable EventKit identifier.
                freshness = int(row["received_at"])
            identity = item_id or f"{row['device_id']}:{row['event_id']}"
            score = (freshness, int(row["rowid"]))
            current = selected.get(identity)
            if current is None or score > current[0]:
                selected[identity] = (
                    score,
                    {
                        "event_id": row["event_id"],
                        "device_id": row["device_id"],
                        "observed_at": row["observed_at"],
                        "data": data,
                    },
                )

        items = [entry[1] for entry in selected.values()]
        items.sort(key=lambda item: (item["observed_at"], item["event_id"]), reverse=True)
        return items[:limit]

    def list_visit_history(self, owner_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        """Return visits with the server-resolved stable place identifiers."""

        owner_id = _owner(owner_id)
        limit = max(1, min(int(limit), 10_000))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id,device_id,place_id,name,latitude,longitude,arrived_at,"
                "departed_at,indoor,payload_json FROM ios_places WHERE owner_id=? "
                "ORDER BY arrived_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = self._open_json(owner_id, row["payload_json"], "ios_places.payload_json")
            data["place_id"] = row["place_id"]
            if not data.get("name"):
                data["name"] = self._open_text(owner_id, row["name"], "ios_places.name")
            data["latitude"] = self._open_number(owner_id, row["latitude"], "ios_places.latitude")
            data["longitude"] = self._open_number(owner_id, row["longitude"], "ios_places.longitude")
            data["arrived_at"] = row["arrived_at"]
            data["departed_at"] = row["departed_at"]
            result.append(
                {
                    "event_id": row["event_id"],
                    "device_id": row["device_id"],
                    "observed_at": row["arrived_at"],
                    "data": data,
                }
            )
        return result

    def list_trajectory_between(
        self,
        owner_id: str,
        start: Any,
        end: Any,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Return hot trajectory samples in an observed time window.

        Route learning runs before the normal cold-archive cutoff, so keeping
        this projection hot avoids decrypting every historical segment during
        the scheduler's high-frequency pass.
        """

        owner_id = _owner(owner_id)
        start_at, end_at = _epoch(start), _epoch(end)
        if end_at < start_at:
            start_at, end_at = end_at, start_at
        bounded_limit = max(1, min(int(limit), 50_000))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id,device_id,observed_at,latitude,longitude,"
                "horizontal_accuracy,altitude,speed,course,motion,payload_json "
                "FROM ios_trajectory WHERE owner_id=? AND observed_at>=? "
                "AND observed_at<=? ORDER BY observed_at LIMIT ?",
                (owner_id, start_at, end_at, bounded_limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "event_id": row["event_id"],
                    "device_id": row["device_id"],
                    "observed_at": row["observed_at"],
                    "latitude": self._open_number(owner_id, row["latitude"], "ios_trajectory.latitude"),
                    "longitude": self._open_number(owner_id, row["longitude"], "ios_trajectory.longitude"),
                    "horizontal_accuracy": self._open_number(owner_id, row["horizontal_accuracy"], "ios_trajectory.horizontal_accuracy"),
                    "altitude": self._open_number(owner_id, row["altitude"], "ios_trajectory.altitude"),
                    "speed": self._open_number(owner_id, row["speed"], "ios_trajectory.speed"),
                    "course": self._open_number(owner_id, row["course"], "ios_trajectory.course"),
                    "motion": self._open_text(owner_id, row["motion"], "ios_trajectory.motion"),
                    "data": self._open_json(owner_id, row["payload_json"], "ios_trajectory.payload_json"),
                }
            )
        return result

    trajectory_between = list_trajectory_between

    def today_snapshot(self, owner_id: str, timezone_name: str = DEFAULT_TIMEZONE) -> dict[str, Any]:
        owner_id = _owner(owner_id)
        tz = _timezone(timezone_name)
        now = datetime.now(tz)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        next_day_start = day_start + timedelta(days=1)
        start = int(day_start.timestamp())
        end = int(next_day_start.timestamp())
        with self._connect() as conn:
            tracks = conn.execute(
                "SELECT observed_at,latitude,longitude,horizontal_accuracy,speed,motion "
                "FROM ios_trajectory WHERE owner_id=? AND observed_at>=? AND observed_at<? "
                "ORDER BY observed_at",
                (owner_id, start, end),
            ).fetchall()
            places = conn.execute(
                "SELECT place_id,name,latitude,longitude,arrived_at,departed_at,indoor,payload_json "
                "FROM ios_places WHERE owner_id=? AND arrived_at<? "
                "AND (departed_at IS NULL OR departed_at>?) ORDER BY arrived_at",
                (owner_id, end, start),
            ).fetchall()
        local_date = now.date()
        track_result = []
        for row in tracks:
            if datetime.fromtimestamp(row["observed_at"], tz).date() != local_date:
                continue
            track_result.append(
                {
                    "observed_at": row["observed_at"],
                    "latitude": self._open_number(owner_id, row["latitude"], "ios_trajectory.latitude"),
                    "longitude": self._open_number(owner_id, row["longitude"], "ios_trajectory.longitude"),
                    "horizontal_accuracy": self._open_number(owner_id, row["horizontal_accuracy"], "ios_trajectory.horizontal_accuracy"),
                    "speed": self._open_number(owner_id, row["speed"], "ios_trajectory.speed"),
                    "motion": self._open_text(owner_id, row["motion"], "ios_trajectory.motion"),
                }
            )
        place_result = []
        for row in places:
            item = dict(row)
            item["name"] = self._open_text(owner_id, item["name"], "ios_places.name")
            item["latitude"] = self._open_number(owner_id, item["latitude"], "ios_places.latitude")
            item["longitude"] = self._open_number(owner_id, item["longitude"], "ios_places.longitude")
            item["indoor"] = None if item["indoor"] is None else bool(item["indoor"])
            item["data"] = self._open_json(owner_id, item.pop("payload_json"), "ios_places.payload_json")
            place_result.append(item)
        forecasts = self.active_forecast(owner_id)
        return {
            "date": local_date.isoformat(), "timezone": str(tz),
            "trajectory": track_result, "places": place_result,
            "current_location": self.latest_snapshot(owner_id, "location"),
            "active_forecast": forecasts,
            "active_forecasts": forecasts,
            "server_time": int(time.time()),
        }

    def get_upload_cursor(self, owner_id: str, device_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cursor FROM ios_upload_cursors WHERE owner_id=? AND device_id=?",
                (_owner(owner_id), _device(device_id)),
            ).fetchone()
        return None if row is None else str(row["cursor"])

    def queue_device_command(
        self,
        owner_id: str,
        capability: str,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        device_id: str = "",
        idempotency_key: str = "",
        not_before: Any = None,
        expires_at: Any = None,
    ) -> dict[str, Any]:
        owner_id = _owner(owner_id)
        now = int(time.time())
        available = _epoch(not_before)
        expiry = _epoch(expires_at) if expires_at is not None else None
        command_id = uuid.uuid4().hex
        with self._connect() as conn, write_txn(conn):
            if idempotency_key:
                existing = conn.execute(
                    "SELECT id,status FROM ios_device_commands WHERE owner_id=? AND idempotency_key=?",
                    (owner_id, idempotency_key),
                ).fetchone()
                if existing:
                    return {"id": existing["id"], "status": existing["status"], "duplicate": True}
            conn.execute(
                "INSERT INTO ios_device_commands(id,owner_id,device_id,capability,action,payload_json,"
                "idempotency_key,status,not_before,expires_at,created_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    command_id, owner_id, _device(device_id), _kind(capability), str(action)[:128],
                    self._seal_json(owner_id, dict(payload or {}), "ios_device_commands.payload_json"),
                    str(idempotency_key)[:512], available,
                    expiry, now,
                ),
            )
            self._touch(conn, owner_id, now)
        result: dict[str, Any] = {
            "id": command_id,
            "status": "pending",
            "duplicate": False,
        }
        try:
            from hermes_cli.dashboard_auth.mobile_notifications import (
                deliver_account_background_wake,
            )

            result["wake"] = deliver_account_background_wake(
                owner_id=owner_id,
                command_id=command_id,
                expires_at=expiry,
            )
        except Exception as exc:
            # The durable command remains authoritative. A foreground poll,
            # BGTask, or later APNs attempt can still drain it.
            result["wake"] = {
                "state": "retry",
                "error": f"device_relay_wake_failed:{type(exc).__name__}",
            }
        return result

    # Stable short aliases used by the cloud plugin.
    queue_command = queue_device_command

    def recover_delivered_commands(self, owner_id: str, *, lease_seconds: int = 120) -> int:
        """Return commands stranded after a device pull but before ACK."""

        owner_id = _owner(owner_id)
        cutoff = int(time.time()) - max(15, int(lease_seconds))
        with self._connect() as conn, write_txn(conn):
            changed = conn.execute(
                "UPDATE ios_device_commands SET status='pending',delivered_at=NULL "
                "WHERE owner_id=? AND status='delivered' AND delivered_at IS NOT NULL "
                "AND delivered_at<=? AND (expires_at IS NULL OR expires_at>?)",
                (owner_id, cutoff, int(time.time())),
            ).rowcount
        return int(changed)

    def pull_device_commands(
        self,
        owner_id: str,
        device_id: str,
        limit: int = 50,
        cursor: str = "",
        *,
        lease_seconds: int = 120,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        owner_id, device_id = _owner(owner_id), _device(device_id)
        now = int(time.time())
        limit = max(1, min(int(limit), 200))
        bounded_attempts = None if max_attempts is None else max(1, min(int(max_attempts), 1000))
        with self._connect() as conn, write_txn(conn):
            conn.execute(
                "UPDATE ios_device_commands SET status='expired' WHERE owner_id=? AND status IN ('pending','delivered') "
                "AND expires_at IS NOT NULL AND expires_at<=?",
                (owner_id, now),
            )
            if bounded_attempts is not None:
                conn.execute(
                    "UPDATE ios_device_commands SET status='expired',acknowledged_at=? "
                    "WHERE owner_id=? AND status IN ('pending','delivered') AND attempts>=?",
                    (now, owner_id, bounded_attempts),
                )
            # A crashed client can leave a delivered command unacknowledged;
            # leases make the durable queue resume without manual repair.
            cutoff = now - max(15, int(lease_seconds))
            conn.execute(
                "UPDATE ios_device_commands SET status='pending',delivered_at=NULL "
                "WHERE owner_id=? AND status='delivered' AND delivered_at IS NOT NULL "
                "AND delivered_at<=? AND (expires_at IS NULL OR expires_at>?)"
                + (" AND attempts<?" if bounded_attempts is not None else ""),
                (owner_id, cutoff, now)
                if bounded_attempts is None
                else (owner_id, cutoff, now, bounded_attempts),
            )
            attempt_clause = " AND attempts<?" if bounded_attempts is not None else ""
            select_params = (owner_id, now, device_id, limit)
            if bounded_attempts is not None:
                select_params = (owner_id, now, device_id, bounded_attempts, limit)
            rows = conn.execute(
                "SELECT * FROM ios_device_commands WHERE owner_id=? AND status='pending' "
                "AND not_before<=? AND (device_id='' OR device_id=?)" + attempt_clause
                + " ORDER BY created_at LIMIT ?",
                select_params,
            ).fetchall()
            if rows:
                conn.executemany(
                    "UPDATE ios_device_commands SET status='delivered',delivered_at=?,attempts=attempts+1 WHERE id=?",
                    [(now, row["id"]) for row in rows],
                )
        commands = [
            {"id": row["id"], "capability": row["capability"], "action": row["action"],
             "payload": self._open_json(owner_id, row["payload_json"], "ios_device_commands.payload_json"),
             "created_at": row["created_at"],
             "expires_at": row["expires_at"]}
            for row in rows
        ]
        return {
            "commands": commands,
            "cursor": commands[-1]["id"] if commands else str(cursor or ""),
            "server_time": now,
        }

    pull_commands = pull_device_commands

    def ack_device_command(
        self,
        owner_id: str,
        device_or_command_id: str,
        command_id: str | None = None,
        *,
        status: str | None = None,
        result: Mapping[str, Any] | None = None,
        error: str = "",
        success: bool | None = None,
    ) -> bool:
        owner_id = _owner(owner_id)
        device_id = "" if command_id is None else _device(device_or_command_id)
        command_id = str(command_id or device_or_command_id)
        normalized = str(status or "").strip().lower()
        succeeded = success if success is not None else normalized not in {"failed", "error", "cancelled"}
        stored_result = dict(result or {})
        if error:
            stored_result["error"] = str(error)[:2000]
        with self._connect() as conn, write_txn(conn):
            changed = conn.execute(
                "UPDATE ios_device_commands SET status=?,acknowledged_at=?,result_json=? "
                "WHERE id=? AND owner_id=? AND status IN ('pending','delivered') "
                "AND (?='' OR device_id='' OR device_id=?)",
                (
                    "completed" if succeeded else "failed", int(time.time()),
                    self._seal_json(owner_id, stored_result, "ios_device_commands.result_json"),
                    command_id, owner_id, device_id, device_id,
                ),
            ).rowcount
        return bool(changed)

    ack_command = ack_device_command

    def active_accounts(self, since_seconds: int | None = None) -> list[str]:
        args: list[Any] = []
        where = ""
        if since_seconds is not None:
            where = " WHERE last_seen_at>=?"
            args.append(int(time.time()) - max(0, int(since_seconds)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT owner_id FROM ios_account_activity{where} ORDER BY last_seen_at DESC", args
            ).fetchall()
        return [str(row["owner_id"]) for row in rows]

    def evaluate_behavior(
        self,
        owner_id: str,
        now: Any = None,
        *,
        feature_weights: Mapping[str, Any] | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """Return an explainable, lightweight leave/place prediction.

        This deliberately supplies features and a calibrated baseline; a cloud
        model may refine the prediction without running an LLM in the phone's
        high-frequency location loop.
        """

        owner_id = _owner(owner_id)
        current_time = _epoch(now)
        local_tz = _timezone(timezone or DEFAULT_TIMEZONE)
        local_now = datetime.fromtimestamp(current_time, local_tz)
        weekday = local_now.weekday()
        with self._connect() as conn:
            visits = conn.execute(
                "SELECT place_id,name,latitude,longitude,arrived_at,departed_at,indoor FROM ios_places "
                "WHERE owner_id=? ORDER BY arrived_at DESC LIMIT 1000",
                (owner_id,),
            ).fetchall()
            feedback_rows = conn.execute(
                "SELECT payload_json FROM ios_behavior_feedback "
                "WHERE owner_id=? AND label='notification-value' "
                "ORDER BY observed_at DESC LIMIT 100",
                (owner_id,),
            ).fetchall()
        visits = [
            {
                **dict(row),
                "name": self._open_text(owner_id, row["name"], "ios_places.name"),
                "latitude": self._open_number(owner_id, row["latitude"], "ios_places.latitude"),
                "longitude": self._open_number(owner_id, row["longitude"], "ios_places.longitude"),
            }
            for row in visits
        ]
        current = visits[0] if visits and visits[0]["departed_at"] is None else None
        completed: list[dict[str, Any]] = []
        for row in visits:
            departed_value = row.get("departed_at")
            if not departed_value:
                continue
            try:
                departed_at = _epoch(departed_value)
                arrived_at = _epoch(row.get("arrived_at"))
            except (TypeError, ValueError, OverflowError):
                continue
            if departed_at <= arrived_at:
                continue
            completed.append({**row, "departed_at": departed_at, "arrived_at": arrived_at})
        same_place = [
            row for row in completed
            if current is not None and row["place_id"] == current["place_id"]
            and datetime.fromtimestamp(row["arrived_at"], local_tz).weekday() == weekday
        ]
        durations = [row["departed_at"] - row["arrived_at"] for row in same_place]
        expected_departure = None
        leave_probability = 0.15
        if current is not None and durations:
            expected_departure = int(current["arrived_at"] + statistics.median(durations))
            delta = expected_departure - current_time
            leave_probability = max(0.02, min(0.98, 0.5 - delta / 7200.0))
        elif current is None:
            # A missing CLVisit means the current place is unknown, not that a
            # departure is imminent. Motion or another learned signal may
            # still raise this baseline below.
            leave_probability = 0.15
        applied_feature_weights: dict[str, float] = {}
        for capability in sorted({
            *_FEATURE_WEIGHT_CAPABILITY_BY_KIND.values(),
            "ios-motion",
            "ios-calendar",
        }):
            applied_feature_weights[capability] = round(
                _feature_weight(feature_weights, capability),
                3,
            )
        motion_weight = _feature_weight(feature_weights, "ios-motion")
        motion = self.latest_snapshot(owner_id, "motion")
        motion_state = str((motion or {}).get("data", {}).get("state") or "").lower()
        motion_is_moving = motion_state in {
            "walking",
            "running",
            "cycling",
            "automotive",
            "driving",
        }
        if motion_is_moving and motion_weight > 0.0:
            motion_probability = max(leave_probability, 0.9)
            leave_probability += (motion_probability - leave_probability) * motion_weight
        place_counts: dict[str, dict[str, Any]] = {}
        for row in completed:
            place_id = str(row.get("place_id") or "")
            item = place_counts.setdefault(
                place_id,
                {"place_id": place_id, "name": str(row.get("name") or ""), "visits": 0},
            )
            item["visits"] += 1
        destinations = sorted(place_counts.values(), key=lambda item: item["visits"], reverse=True)[:5]
        chronological = sorted(completed, key=lambda row: row["arrived_at"])
        transitions: dict[str, dict[str, Any]] = {}
        current_place_id = current["place_id"] if current is not None else ""
        for source, destination in zip(chronological, chronological[1:]):
            if source["place_id"] != current_place_id or destination["place_id"] == current_place_id:
                continue
            if destination["arrived_at"] - source["departed_at"] > 24 * 3600:
                continue
            candidate = transitions.setdefault(
                destination["place_id"],
                {"place_id": destination["place_id"], "name": destination["name"], "transitions": 0},
            )
            candidate["transitions"] += 1
        destination_candidates = sorted(
            transitions.values(), key=lambda item: item["transitions"], reverse=True
        )[:5]
        confidence = min(0.95, 0.25 + min(len(same_place), 14) * 0.05)
        indoor = current is not None and current["indoor"] == 1
        effective_motion_state = motion_state if motion_weight > 0.0 else ""
        calendar_weight = _feature_weight(feature_weights, "ios-calendar")
        calendar_items = (
            self.list_snapshots(owner_id, "calendar", limit=50)
            if calendar_weight > 0.0
            else []
        )
        holiday_words = ("holiday", "festival", "节", "假期", "休息")
        day_start = int(local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        day_end = int((local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp())

        def calendar_item_is_today(item: Mapping[str, Any]) -> bool:
            data = item.get("data", {})
            try:
                start = _epoch(data.get("start") or item.get("observed_at"))
                end = _epoch(data.get("end") or start)
            except (TypeError, ValueError):
                return False
            return start < day_end and end >= day_start

        calendar_holiday = any(
            calendar_item_is_today(item)
            and any(word in str(item.get("data", {}).get("title") or "").lower() for word in holiday_words)
            for item in calendar_items
        )
        is_weekend = weekday >= 5
        month = local_now.month
        season = (
            "spring" if 3 <= month <= 5
            else "summer" if 6 <= month <= 8
            else "autumn" if 9 <= month <= 11
            else "winter"
        )
        context_features: dict[str, Any] = {}
        excluded_context_features: list[str] = []
        for feature, kind in _CONTEXT_FEATURE_KINDS.items():
            capability = _FEATURE_WEIGHT_CAPABILITY_BY_KIND.get(kind, "")
            weight = _feature_weight(feature_weights, capability) if capability else 1.0
            if weight <= 0.0:
                excluded_context_features.append(feature)
                continue
            snapshot = self.latest_snapshot(owner_id, kind)
            if snapshot is not None:
                context_features[feature] = {
                    **snapshot,
                    "feature_weight": round(weight, 3),
                    "capability": capability,
                }
        useful_feedback = 0
        for row in feedback_rows:
            payload = self._open_json(owner_id, row["payload_json"], "ios_behavior_feedback.payload_json")
            action = str(payload.get("action") or "").lower()
            useful = payload.get("useful")
            if useful is True or action in {"opened", "accepted", "helpful"}:
                useful_feedback += 1
        feedback_count = len(feedback_rows)
        notification_value_score = (
            useful_feedback / feedback_count if feedback_count else 0.5
        )
        return {
            "evaluated_at": current_time,
            "current_place": None if current is None else {
                "place_id": current["place_id"], "name": current["name"],
                "latitude": current["latitude"], "longitude": current["longitude"],
                "arrived_at": current["arrived_at"], "indoor": None if current["indoor"] is None else bool(current["indoor"]),
            },
            "motion_state": motion_state or None,
            "effective_motion_state": effective_motion_state or None,
            "motion_weight": round(motion_weight, 3),
            "calendar_weight": round(calendar_weight, 3),
            "applied_feature_weights": applied_feature_weights,
            "likely_to_leave": leave_probability >= 0.6,
            "leave_probability": round(leave_probability, 3),
            "expected_departure_at": expected_departure,
            "frequent_destinations": destinations,
            "destination_candidates": destination_candidates,
            "suppress_weather_query": bool(indoor and leave_probability < 0.6),
            "confidence": round(confidence, 3),
            "samples": len(same_place),
            "timezone": str(local_tz.key if hasattr(local_tz, "key") else timezone or DEFAULT_TIMEZONE),
            "calendar_context": {
                "local_hour": local_now.hour,
                "weekday": weekday,
                "is_weekend": is_weekend,
                "is_holiday": bool(is_weekend or calendar_holiday),
                "day_type": "holiday" if calendar_holiday else "weekend" if is_weekend else "weekday",
                "season": season,
                "calendar_weight": round(calendar_weight, 3),
            },
            "context_features": context_features,
            "excluded_context_features": excluded_context_features,
            "notification_value": {
                "samples": feedback_count,
                "useful": useful_feedback,
                "score": round(notification_value_score, 3),
            },
        }

    def record_active_forecast(self, owner_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a still-relevant weather card for the native map page."""

        owner_id = _owner(owner_id)
        data = dict(payload)
        valid_from = _epoch(data.get("valid_from") or data.get("starts_at"))
        valid_until_value = data.get("valid_until") or data.get("ends_at") or data.get("expires_at")
        if valid_until_value is None:
            raise ValueError("forecast requires valid_until, ends_at, or expires_at")
        valid_until = _epoch(valid_until_value)
        if valid_until <= valid_from:
            raise ValueError("forecast valid_until must be after valid_from")
        forecast_id = str(data.get("id") or data.get("forecast_id") or uuid.uuid4().hex)[:512]
        now = int(time.time())
        with self._connect() as conn, write_txn(conn):
            conn.execute(
                "INSERT INTO ios_active_forecasts VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(owner_id,id) DO UPDATE SET payload_json=excluded.payload_json,valid_from=excluded.valid_from,"
                "valid_until=excluded.valid_until,updated_at=excluded.updated_at",
                (
                    forecast_id, owner_id, valid_from, valid_until,
                    self._seal_json(owner_id, data, "ios_active_forecasts.payload_json"), now, now,
                ),
            )
            self._touch(conn, owner_id, now)
        return {"id": forecast_id, "valid_from": valid_from, "valid_until": valid_until}

    def active_forecast(self, owner_id: str, now: Any = None) -> list[dict[str, Any]]:
        owner_id, current = _owner(owner_id), _epoch(now)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,valid_from,valid_until,payload_json FROM ios_active_forecasts "
                "WHERE owner_id=? AND valid_until>? ORDER BY valid_from",
                (owner_id, current),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "valid_from": row["valid_from"],
                "valid_until": row["valid_until"],
                "data": self._open_json(owner_id, row["payload_json"], "ios_active_forecasts.payload_json"),
            }
            for row in rows
        ]

    def enqueue_notification(
        self,
        owner_id: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        expires_at: Any,
        not_before: Any = None,
        now: Any = None,
        dedupe_key: str = "",
        dedupe_window_seconds: int = 0,
    ) -> dict[str, Any]:
        owner_id = _owner(owner_id)
        key = str(idempotency_key or "").strip()[:512]
        if not key:
            raise ValueError("idempotency_key is required")
        created_at = _epoch(now)
        expiry = _epoch(expires_at)
        if expiry <= created_at:
            raise ValueError("notification expires_at must be in the future")
        available = _epoch(not_before) if not_before is not None else created_at
        serialized_payload = self._seal_json(owner_id, dict(payload), "ios_notification_outbox.payload_json")
        notification_id = uuid.uuid4().hex
        with self._connect() as conn, write_txn(conn):
            normalized_dedupe = str(dedupe_key or "").strip()[:400]
            dedupe_window = max(0, int(dedupe_window_seconds))
            if normalized_dedupe and dedupe_window:
                recent = conn.execute(
                    "SELECT id,state FROM ios_notification_outbox "
                    "WHERE owner_id=? AND idempotency_key LIKE ? AND created_at>? "
                    "AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                    (
                        owner_id,
                        f"{normalized_dedupe}:%",
                        created_at - dedupe_window,
                        created_at,
                    ),
                ).fetchone()
                if recent:
                    if recent["state"] in {"pending", "retry"}:
                        conn.execute(
                            "UPDATE ios_notification_outbox SET payload_json=?,not_before=?,"
                            "expires_at=?,updated_at=? WHERE id=?",
                            (serialized_payload, available, expiry, created_at, recent["id"]),
                        )
                    return {
                        "id": recent["id"],
                        "state": recent["state"],
                        "duplicate": True,
                    }
            existing = conn.execute(
                "SELECT id,state FROM ios_notification_outbox WHERE owner_id=? AND idempotency_key=?",
                (owner_id, key),
            ).fetchone()
            if existing:
                return {"id": existing["id"], "state": existing["state"], "duplicate": True}
            conn.execute(
                "INSERT INTO ios_notification_outbox(id,owner_id,idempotency_key,payload_json,not_before,"
                "expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    notification_id,
                    owner_id,
                    key,
                    serialized_payload,
                    available,
                    expiry,
                    created_at,
                    created_at,
                ),
            )
            self._touch(conn, owner_id, created_at)
        return {"id": notification_id, "state": "pending", "duplicate": False}

    def expire_pending_weather_notifications(
        self,
        owner_id: str,
        *,
        now: Any = None,
        keep_event_key: str = "",
    ) -> dict[str, Any]:
        """Expire stale weather deliveries and their derived map cards."""

        owner_id = _owner(owner_id)
        instant = _epoch(now)
        keep = str(keep_event_key or "").strip()
        expired_ids: list[str] = []
        removed_forecasts: list[str] = []
        with self._connect() as conn, write_txn(conn):
            # Never steal an in-flight 'delivering' lease: a worker mid-APNs
            # must finish under CAS. Only reclaim after leased_until has passed.
            outbox_rows = conn.execute(
                "SELECT id,payload_json,state,leased_until FROM ios_notification_outbox "
                "WHERE owner_id=? AND state IN ('pending','retry','delivering')",
                (owner_id,),
            ).fetchall()
            for row in outbox_rows:
                payload = self._open_json(owner_id, row["payload_json"], "ios_notification_outbox.payload_json")
                if payload.get("category") != "smart-weather":
                    continue
                if keep and str(payload.get("event_key") or "") == keep:
                    continue
                if str(row["state"]) == "delivering" and int(row["leased_until"] or 0) > instant:
                    continue
                expired_ids.append(str(row["id"]))
            if expired_ids:
                placeholders = ",".join("?" for _ in expired_ids)
                conn.execute(
                    f"UPDATE ios_notification_outbox SET state='expired',lease_token='',"
                    f"leased_until=0,updated_at=? WHERE owner_id=? "
                    f"AND state IN ('pending','retry','delivering') "
                    f"AND id IN ({placeholders}) "
                    f"AND (state!='delivering' OR leased_until<=?)",
                    (instant, owner_id, *expired_ids, instant),
                )

            delivered_rows = conn.execute(
                "SELECT id,payload_json,device_deliveries_json,state FROM ios_notification_outbox "
                "WHERE owner_id=? AND state IN ('delivered','retry','expired') AND expires_at<=?",
                (owner_id, instant),
            ).fetchall()
            existing_feedback: set[str] = set()
            for feedback_row in conn.execute(
                "SELECT payload_json FROM ios_behavior_feedback "
                "WHERE owner_id=? AND label='notification-value'",
                (owner_id,),
            ).fetchall():
                feedback_payload = self._open_json(
                    owner_id,
                    feedback_row["payload_json"],
                    "ios_behavior_feedback.payload_json",
                )
                feedback_notification = str(
                    feedback_payload.get("notification_id") or ""
                ).strip()
                if feedback_notification:
                    existing_feedback.add(feedback_notification)
            delivered_weather_ids: list[str] = []
            for row in delivered_rows:
                payload = self._open_json(
                    owner_id,
                    row["payload_json"],
                    "ios_notification_outbox.payload_json",
                )
                if payload.get("category") != "smart-weather":
                    continue
                notification_id = str(row["id"])
                device_deliveries = self._open_json(
                    owner_id,
                    row["device_deliveries_json"],
                    "ios_notification_outbox.device_deliveries_json",
                )
                was_delivered = row["state"] == "delivered" or any(
                    isinstance(delivery, Mapping)
                    and str(delivery.get("state") or "") == "delivered"
                    for delivery in (device_deliveries.values() if isinstance(device_deliveries, Mapping) else [])
                )
                if was_delivered:
                    delivered_weather_ids.append(notification_id)
                if notification_id in existing_feedback:
                    continue
                if not was_delivered:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO ios_behavior_feedback VALUES(?,?,?,?,?,?)",
                    (
                        self._expired_notification_feedback_id(notification_id),
                        owner_id,
                        "notification-value",
                        self._seal_json(
                            owner_id,
                            {
                                "action": "expired-unopened",
                                "notification_id": notification_id,
                                "useful": False,
                            },
                            "ios_behavior_feedback.payload_json",
                        ),
                        instant,
                        int(time.time()),
                    ),
                )
            if delivered_weather_ids:
                placeholders = ",".join("?" for _ in delivered_weather_ids)
                conn.execute(
                    f"UPDATE ios_notification_outbox SET state='expired',lease_token='',"
                    f"leased_until=0,updated_at=? WHERE owner_id=? "
                    f"AND state IN ('delivered','retry','expired') AND id IN ({placeholders})",
                    (instant, owner_id, *delivered_weather_ids),
                )

            forecast_rows = conn.execute(
                "SELECT id,payload_json FROM ios_active_forecasts WHERE owner_id=?",
                (owner_id,),
            ).fetchall()
            for row in forecast_rows:
                payload = self._open_json(owner_id, row["payload_json"], "ios_active_forecasts.payload_json")
                if payload.get("category") != "smart-weather":
                    continue
                if keep and str(payload.get("event_key") or "") == keep:
                    continue
                removed_forecasts.append(str(row["id"]))
            if removed_forecasts:
                placeholders = ",".join("?" for _ in removed_forecasts)
                conn.execute(
                    f"DELETE FROM ios_active_forecasts WHERE owner_id=? AND id IN ({placeholders})",
                    (owner_id, *removed_forecasts),
                )
        return {
            "expired": len(expired_ids),
            "forecasts_removed": len(removed_forecasts),
        }

    def pending_notifications(
        self,
        limit: int = 100,
        *,
        now: Any = None,
    ) -> list[dict[str, Any]]:
        now = _epoch(now)
        limit = max(1, min(int(limit), 1000))
        with self._connect() as conn, write_txn(conn):
            conn.execute(
                "UPDATE ios_notification_outbox SET state='expired',lease_token='',"
                "leased_until=0,updated_at=? "
                "WHERE state IN ('pending','retry','delivering') AND expires_at<=?",
                (now, now),
            )
            rows = conn.execute(
                "SELECT id,owner_id,payload_json,deliveries,device_deliveries_json,expires_at "
                "FROM ios_notification_outbox "
                "WHERE state IN ('pending','retry') AND not_before<=? AND expires_at>? "
                "AND leased_until<=? "
                "ORDER BY created_at LIMIT ?",
                (now, now, now, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "owner_id": row["owner_id"],
                "payload": self._open_json(row["owner_id"], row["payload_json"], "ios_notification_outbox.payload_json"),
                "deliveries": row["deliveries"],
                "device_deliveries": self._open_json(
                    row["owner_id"], row["device_deliveries_json"], "ios_notification_outbox.device_deliveries_json"
                ),
                "expires_at": row["expires_at"],
            }
            for row in rows
        ]

    def claim_pending_notifications(
        self,
        limit: int = 100,
        *,
        now: Any = None,
        lease_seconds: int = 300,
        exclude_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically lease due notifications to one delivery worker."""

        instant = _epoch(now)
        limit = max(1, min(int(limit), 1000))
        lease_until = instant + max(15, min(int(lease_seconds), 3600))
        lease_token = uuid.uuid4().hex
        excluded = [str(item) for item in (exclude_ids or ()) if str(item)]
        exclusion_sql = ""
        if excluded:
            exclusion_sql = f" AND id NOT IN ({','.join('?' for _ in excluded)})"
        with self._connect() as conn, write_txn(conn):
            conn.execute(
                "UPDATE ios_notification_outbox SET state='expired',lease_token='',"
                "leased_until=0,updated_at=? "
                "WHERE state IN ('pending','retry','delivering') "
                "AND expires_at<=?",
                (instant, instant),
            )
            ids = [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM ios_notification_outbox "
                    "WHERE state IN ('pending','retry','delivering') AND not_before<=? "
                    "AND expires_at>? AND leased_until<=? "
                    f"{exclusion_sql} "
                    "ORDER BY created_at LIMIT ?",
                    (instant, instant, instant, *excluded, limit),
                ).fetchall()
            ]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE ios_notification_outbox SET state='delivering',lease_token=?,"
                f"leased_until=?,updated_at=? WHERE id IN ({placeholders}) "
                f"AND state IN ('pending','retry','delivering') AND not_before<=? "
                f"AND expires_at>? AND leased_until<=?",
                (
                    lease_token,
                    lease_until,
                    instant,
                    *ids,
                    instant,
                    instant,
                    instant,
                ),
            )
            rows = conn.execute(
                "SELECT id,owner_id,payload_json,deliveries,device_deliveries_json,"
                "expires_at,lease_token,leased_until FROM ios_notification_outbox "
                "WHERE state='delivering' AND lease_token=? ORDER BY created_at",
                (lease_token,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "owner_id": row["owner_id"],
                "payload": self._open_json(
                    row["owner_id"], row["payload_json"],
                    "ios_notification_outbox.payload_json",
                ),
                "deliveries": row["deliveries"],
                "device_deliveries": self._open_json(
                    row["owner_id"], row["device_deliveries_json"],
                    "ios_notification_outbox.device_deliveries_json",
                ),
                "expires_at": row["expires_at"],
                "lease_token": row["lease_token"],
                "leased_until": row["leased_until"],
            }
            for row in rows
        ]

    def update_notification_device_deliveries(
        self,
        notification_id: str,
        device_deliveries: Mapping[str, Any],
        *,
        lease_token: str = "",
        lease_seconds: int = 300,
        now: Any = None,
    ) -> bool:
        instant = _epoch(now)
        with self._connect() as conn, write_txn(conn):
            owner_row = conn.execute(
                "SELECT owner_id FROM ios_notification_outbox WHERE id=?",
                (str(notification_id),),
            ).fetchone()
            if owner_row is None:
                return False
            token = str(lease_token or "")
            serialized_deliveries = self._seal_json(
                owner_row["owner_id"],
                dict(device_deliveries),
                "ios_notification_outbox.device_deliveries_json",
            )
            if token:
                changed = conn.execute(
                    "UPDATE ios_notification_outbox SET device_deliveries_json=?,"
                    "leased_until=?,updated_at=? WHERE id=? AND state='delivering' AND lease_token=? "
                    "AND leased_until>? AND expires_at>?",
                    (
                        serialized_deliveries,
                        instant + max(15, min(int(lease_seconds), 3600)),
                        instant,
                        str(notification_id),
                        token,
                        instant,
                        instant,
                    ),
                ).rowcount
            else:
                changed = conn.execute(
                    "UPDATE ios_notification_outbox SET device_deliveries_json=?,"
                    "updated_at=? WHERE id=? AND state IN ('pending','retry') "
                    "AND expires_at>?",
                    (
                        serialized_deliveries,
                        instant,
                        str(notification_id),
                        instant,
                    ),
                ).rowcount
        return bool(changed)

    def update_notification_delivery(
        self,
        notification_id: str,
        state: str,
        deliveries: int,
        error: str = "",
        device_deliveries: Mapping[str, Any] | None = None,
        *,
        lease_token: str = "",
        now: Any = None,
    ) -> bool:
        normalized = str(state).strip().lower()
        if normalized not in {"pending", "retry", "delivered", "failed", "expired"}:
            raise ValueError("invalid notification delivery state")
        instant = _epoch(now)
        with self._connect() as conn, write_txn(conn):
            owner_row = conn.execute(
                "SELECT owner_id FROM ios_notification_outbox WHERE id=?",
                (str(notification_id),),
            ).fetchone()
            if owner_row is None:
                return False
            token = str(lease_token or "")
            values = (
                normalized,
                max(0, int(deliveries)),
                str(error or "")[:2000],
                None if device_deliveries is None else self._seal_json(
                    owner_row["owner_id"],
                    dict(device_deliveries),
                    "ios_notification_outbox.device_deliveries_json",
                ),
                instant,
                str(notification_id),
            )
            if token:
                changed = conn.execute(
                    "UPDATE ios_notification_outbox SET state=?,deliveries=?,last_error=?,"
                    "device_deliveries_json=COALESCE(?,device_deliveries_json),"
                    "lease_token='',leased_until=0,updated_at=? "
                    "WHERE id=? AND state='delivering' AND lease_token=? "
                    "AND leased_until>? AND expires_at>?",
                    (*values, token, instant, instant),
                ).rowcount
            else:
                changed = conn.execute(
                    "UPDATE ios_notification_outbox SET state=?,deliveries=?,last_error=?,"
                    "device_deliveries_json=COALESCE(?,device_deliveries_json),"
                    "lease_token='',leased_until=0,updated_at=? "
                    "WHERE id=? AND state IN ('pending','retry') AND expires_at>?",
                    (*values, instant),
                ).rowcount
        return bool(changed)

    @staticmethod
    def weather_month(now: Any = None, timezone: str | None = None) -> str:
        instant = datetime.fromtimestamp(
            _epoch(now),
            _timezone(timezone or DEFAULT_TIMEZONE),
        )
        return instant.strftime("%Y-%m")

    def weather_quota_status(
        self,
        now: Any = None,
        *,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        month = self.weather_month(now, timezone=timezone)
        with self._connect() as conn:
            row = conn.execute("SELECT request_count FROM ios_weather_usage WHERE month=?", (month,)).fetchone()
        count = 0 if row is None else int(row["request_count"])
        return {
            "month": month, "used": count, "remaining": max(0, WEATHER_MONTHLY_LIMIT - count),
            "soft_limited": count >= WEATHER_SOFT_LIMIT, "exhausted": count >= WEATHER_MONTHLY_LIMIT,
        }

    def reserve_weather_requests(
        self,
        count: int = 1,
        now: Any = None,
        *,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        count = int(count)
        if count < 1 or count > WEATHER_MONTHLY_LIMIT:
            raise ValueError("count must be between 1 and 30000")
        month = self.weather_month(now, timezone=timezone)
        timestamp = int(time.time())
        with self._connect() as conn, write_txn(conn):
            row = conn.execute("SELECT request_count FROM ios_weather_usage WHERE month=?", (month,)).fetchone()
            current = 0 if row is None else int(row["request_count"])
            if current + count > WEATHER_MONTHLY_LIMIT:
                return {
                    "allowed": False, "month": month, "used": current,
                    "remaining": max(0, WEATHER_MONTHLY_LIMIT - current),
                    "soft_limited": True, "exhausted": current >= WEATHER_MONTHLY_LIMIT,
                }
            updated = current + count
            conn.execute(
                "INSERT INTO ios_weather_usage VALUES(?,?,?) "
                "ON CONFLICT(month) DO UPDATE SET request_count=excluded.request_count,updated_at=excluded.updated_at",
                (month, updated, timestamp),
            )
        return {
            "allowed": True, "month": month, "used": updated,
            "remaining": WEATHER_MONTHLY_LIMIT - updated,
            "soft_limited": updated >= WEATHER_SOFT_LIMIT,
            "exhausted": updated >= WEATHER_MONTHLY_LIMIT,
        }

    def get_cache(self, provider: str, cache_key: str) -> dict[str, Any] | None:
        cache_key = self._cache_storage_key(provider, cache_key)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM ios_external_cache WHERE provider=? AND cache_key=? AND expires_at>?",
                (provider, cache_key, int(time.time())),
            ).fetchone()
        return None if row is None else self._open_json(
            _SHARED_CACHE_OWNER, row["payload_json"], "ios_external_cache.payload_json"
        )

    def put_cache(self, provider: str, cache_key: str, payload: Mapping[str, Any], ttl_seconds: int) -> None:
        cache_key = self._cache_storage_key(provider, cache_key)
        now = int(time.time())
        with self._connect() as conn, write_txn(conn):
            conn.execute(
                "INSERT INTO ios_external_cache VALUES(?,?,?,?,?) "
                "ON CONFLICT(provider,cache_key) DO UPDATE SET payload_json=excluded.payload_json,"
                "expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                (
                    provider,
                    cache_key,
                    self._seal_json(_SHARED_CACHE_OWNER, dict(payload), "ios_external_cache.payload_json"),
                    now + max(1, int(ttl_seconds)),
                    now,
                ),
            )

    def _cache_storage_key(self, provider: str, cache_key: str) -> str:
        digest = hmac.new(
            self._master_secret,
            f"cache\0{provider}\0{cache_key}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"v1:{digest}"

    # ------------------------------------------------------------------
    # Long-lived derived data
    # ------------------------------------------------------------------
    def learn_place(
        self,
        owner_id: str,
        place_id: str,
        *,
        name: str = "",
        latitude: float | None = None,
        longitude: float | None = None,
        arrived_at: Any = None,
        departed_at: Any = None,
        indoor: bool | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upsert a place in the account's weighted place graph.

        New places start at weight 1 and repeated visits raise the weight.  The
        raw place visit remains immutable in ``ios_places``; this aggregate is
        safe to rebuild and is what prediction paths read at high frequency.
        """
        owner_id = _owner(owner_id)
        place_id = str(place_id or "").strip()[:256]
        if not place_id:
            raise ValueError("place_id is required")
        now = _epoch(arrived_at)
        clean_name = str(name or "")[:512]
        stored_name = self._seal_text(owner_id, clean_name, "ios_place_graph.name") if clean_name else ""
        meta = dict(metadata or {})
        with self._connect() as conn, write_txn(conn):
            existing = conn.execute(
                "SELECT visits,weight,first_seen,is_home FROM ios_place_graph "
                "WHERE owner_id=? AND place_id=?", (owner_id, place_id)
            ).fetchone()
            visits = int(existing["visits"]) + 1 if existing else 1
            # A bounded logarithmic weight prevents one place from drowning
            # out newer locations while still promoting repeated visits.
            weight = round(min(100.0, 1.0 + (visits - 1) ** 0.5), 6)
            first_seen = int(existing["first_seen"]) if existing else now
            is_home = int(existing["is_home"]) if existing else 0
            conn.execute(
                "INSERT INTO ios_place_graph(owner_id,place_id,name,latitude,longitude,visits,weight,"
                "first_seen,last_seen,is_home,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(owner_id,place_id) DO UPDATE SET name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE ios_place_graph.name END,"
                "latitude=COALESCE(excluded.latitude,ios_place_graph.latitude),longitude=COALESCE(excluded.longitude,ios_place_graph.longitude),"
                "visits=excluded.visits,weight=excluded.weight,last_seen=excluded.last_seen,metadata_json=excluded.metadata_json",
                (
                    owner_id,
                    place_id,
                    stored_name,
                    self._seal_number(owner_id, latitude, "ios_place_graph.latitude"),
                    self._seal_number(owner_id, longitude, "ios_place_graph.longitude"),
                    visits,
                    weight,
                    first_seen,
                    now,
                    is_home,
                    self._seal_json(owner_id, meta, "ios_place_graph.metadata_json"),
                ),
            )
            self._touch(conn, owner_id, int(time.time()))
        return self.get_place(owner_id, place_id) or {"owner_id": owner_id, "place_id": place_id}

    def get_place(self, owner_id: str, place_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ios_place_graph WHERE owner_id=? AND place_id=?",
                (_owner(owner_id), str(place_id)),
            ).fetchone()
        return self._place_row(_owner(owner_id), row) if row else None

    def _place_row(self, owner_id: str, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["name"] = self._open_text(owner_id, item.get("name"), "ios_place_graph.name")
        item["latitude"] = self._open_number(owner_id, item.get("latitude"), "ios_place_graph.latitude")
        item["longitude"] = self._open_number(owner_id, item.get("longitude"), "ios_place_graph.longitude")
        item["is_home"] = bool(item.get("is_home"))
        item["metadata"] = self._open_json(
            owner_id, item.pop("metadata_json", "{}"), "ios_place_graph.metadata_json"
        )
        return item

    def list_places(self, owner_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 10_000))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ios_place_graph WHERE owner_id=? ORDER BY weight DESC,last_seen DESC LIMIT ?",
                (_owner(owner_id), limit),
            ).fetchall()
        owner_id = _owner(owner_id)
        return [self._place_row(owner_id, row) for row in rows]

    place_graph = list_places
    places = list_places

    def learn_route(
        self,
        owner_id: str,
        origin_place_id: str,
        destination_place_id: str,
        *,
        mode: str = "",
        duration_seconds: float | None = None,
        distance_meters: float | None = None,
        outdoor_minutes: float | None = None,
        observed_at: Any = None,
        metadata: Mapping[str, Any] | None = None,
        sample_id: str = "",
    ) -> dict[str, Any]:
        owner_id = _owner(owner_id)
        origin = str(origin_place_id or "").strip()[:256]
        destination = str(destination_place_id or "").strip()[:256]
        if not origin or not destination:
            raise ValueError("origin_place_id and destination_place_id are required")
        normalized_mode = str(mode or "unknown").strip().lower()[:64]
        observed = _epoch(observed_at)
        duration = max(0.0, float(duration_seconds or 0.0))
        distance = max(0.0, float(distance_meters or 0.0))
        outdoor = max(0.0, float(outdoor_minutes or 0.0))
        with self._connect() as conn, write_txn(conn):
            duplicate_sample = False
            normalized_sample = str(sample_id or "").strip()[:512]
            if normalized_sample:
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO ios_route_samples(owner_id,sample_id,created_at) VALUES(?,?,?)",
                    (owner_id, normalized_sample, int(time.time())),
                ).rowcount
                duplicate_sample = not bool(inserted)
            if not duplicate_sample:
                existing = conn.execute(
                    "SELECT metadata_json FROM ios_route_graph WHERE owner_id=? "
                    "AND origin_place_id=? AND destination_place_id=? AND mode=?",
                    (owner_id, origin, destination, normalized_mode),
                ).fetchone()
                previous_metadata = (
                    self._open_json(owner_id, existing["metadata_json"], "ios_route_graph.metadata_json")
                    if existing else {}
                )
                incoming_metadata = dict(metadata or {})
                next_metadata = {**previous_metadata, **incoming_metadata}
                time_bucket = str(incoming_metadata.get("time_bucket") or "unknown")[:64]
                weather_condition = str(
                    incoming_metadata.get("weather_condition") or "unknown"
                )[:64]
                context_key = f"{time_bucket}|{weather_condition}"
                context_stats = dict(previous_metadata.get("context_stats") or {})
                previous_context = dict(context_stats.get(context_key) or {})
                context_trips = max(0, int(previous_context.get("trips") or 0)) + 1
                context_duration = max(
                    0.0,
                    float(previous_context.get("total_duration_seconds") or 0.0),
                ) + duration
                context_stats[context_key] = {
                    "time_bucket": time_bucket,
                    "weather_condition": weather_condition,
                    "trips": context_trips,
                    "total_duration_seconds": context_duration,
                    "average_duration_seconds": context_duration / context_trips,
                }
                next_metadata["context_stats"] = context_stats
                conn.execute(
                "INSERT INTO ios_route_graph(owner_id,origin_place_id,destination_place_id,mode,trips,"
                "total_duration_seconds,total_distance_meters,total_outdoor_minutes,last_seen,metadata_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,origin_place_id,destination_place_id,mode) DO UPDATE SET "
                "trips=ios_route_graph.trips+1,total_duration_seconds=ios_route_graph.total_duration_seconds+excluded.total_duration_seconds,"
                "total_distance_meters=ios_route_graph.total_distance_meters+excluded.total_distance_meters,"
                "total_outdoor_minutes=ios_route_graph.total_outdoor_minutes+excluded.total_outdoor_minutes,last_seen=excluded.last_seen,"
                "metadata_json=excluded.metadata_json",
                    (
                        owner_id, origin, destination, normalized_mode, 1, duration, distance, outdoor, observed,
                        self._seal_json(owner_id, next_metadata, "ios_route_graph.metadata_json"),
                    ),
                )
            self._touch(conn, owner_id, int(time.time()))
        return self.get_route(owner_id, origin, destination, normalized_mode) or {}

    def get_route(self, owner_id: str, origin_place_id: str, destination_place_id: str, mode: str = "") -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ios_route_graph WHERE owner_id=? AND origin_place_id=? AND destination_place_id=? AND mode=?",
                (_owner(owner_id), str(origin_place_id), str(destination_place_id), str(mode or "unknown").lower()),
            ).fetchone()
        return self._route_row(_owner(owner_id), row) if row else None

    def _route_row(self, owner_id: str, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        trips = max(1, int(item.get("trips") or 1))
        item["average_duration_seconds"] = float(item.get("total_duration_seconds") or 0.0) / trips
        item["average_distance_meters"] = float(item.get("total_distance_meters") or 0.0) / trips
        item["average_outdoor_minutes"] = float(item.get("total_outdoor_minutes") or 0.0) / trips
        item["metadata"] = self._open_json(
            owner_id, item.pop("metadata_json", "{}"), "ios_route_graph.metadata_json"
        )
        return item

    def list_routes(self, owner_id: str, origin_place_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 10_000))
        args: list[Any] = [_owner(owner_id)]
        where = ""
        if origin_place_id:
            where = " AND origin_place_id=?"
            args.append(str(origin_place_id))
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ios_route_graph WHERE owner_id=?" + where + " ORDER BY trips DESC,last_seen DESC LIMIT ?", args
            ).fetchall()
        owner_id = _owner(owner_id)
        return [self._route_row(owner_id, row) for row in rows]

    route_graph = list_routes
    routes = list_routes

    def learn_home(self, owner_id: str, timezone_name: str = DEFAULT_TIMEZONE) -> dict[str, Any] | None:
        """Infer home from repeated overnight indoor stays.

        A candidate receives one vote for each visit intersecting 23:00-06:00;
        the highest-vote location is marked home only after two observations.
        """
        owner_id = _owner(owner_id)
        tz = _timezone(timezone_name)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT place_id,name,arrived_at,departed_at,indoor,payload_json FROM ios_places "
                "WHERE owner_id=? ORDER BY arrived_at", (owner_id,)
            ).fetchall()
        votes: dict[str, int] = {}
        current_time = int(time.time())
        for row in rows:
            if row["indoor"] == 0:
                continue
            # The configured study room is a known fixed destination, not a
            # signal for the household location even when visited overnight.
            if (
                row["place_id"] == "study-quanzhou-91-bainaohui"
                or self._open_json(owner_id, row["payload_json"], "ios_places.payload_json").get("fixed") is True
            ):
                continue
            arrived = int(row["arrived_at"])
            departed = int(row["departed_at"] or current_time)
            if departed <= arrived:
                continue
            # Check the local midnight window on both endpoint dates.
            # A stay beginning after midnight intersects the previous day's
            # 23:00-06:00 window, so start one local date earlier.
            cursor = datetime.fromtimestamp(arrived, tz).date() - timedelta(days=1)
            end_date = datetime.fromtimestamp(departed, tz).date()
            while cursor <= end_date:
                midnight = datetime(cursor.year, cursor.month, cursor.day, tzinfo=tz)
                night_start = int(midnight.replace(hour=23).timestamp())
                night_end = int((midnight + timedelta(days=1, hours=6)).timestamp())
                if max(arrived, night_start) < min(departed, night_end):
                    votes[row["place_id"]] = votes.get(row["place_id"], 0) + 1
                    # Repeated visits, not multiple nights inside one stale or
                    # unusually long visit, establish the household pattern.
                    break
                cursor += timedelta(days=1)
        if not votes:
            return None
        place_id, count = max(votes.items(), key=lambda item: item[1])
        if count < 2:
            return None
        with self._connect() as conn, write_txn(conn):
            conn.execute("UPDATE ios_place_graph SET is_home=0 WHERE owner_id=?", (owner_id,))
            conn.execute("UPDATE ios_place_graph SET is_home=1 WHERE owner_id=? AND place_id=?", (owner_id, place_id))
        return self.get_place(owner_id, place_id)

    infer_home = learn_home
    learn_home_location = learn_home

    def save_model(self, owner_id: str, model_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        owner_id = _owner(owner_id)
        name = str(model_name or "behavior").strip()[:128]
        now = int(time.time())
        with self._connect() as conn, write_txn(conn):
            conn.execute(
                "INSERT INTO ios_behavior_models VALUES(?,?,?,?) ON CONFLICT(owner_id,model_name) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                (owner_id, name, self._seal_json(owner_id, dict(payload), "ios_behavior_models.payload_json"), now),
            )
            self._touch(conn, owner_id, now)
        return {"owner_id": owner_id, "model_name": name, "payload": dict(payload), "updated_at": now}

    update_model = save_model

    def load_model(self, owner_id: str, model_name: str = "behavior") -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json,updated_at FROM ios_behavior_models WHERE owner_id=? AND model_name=?",
                (_owner(owner_id), str(model_name or "behavior")[:128]),
            ).fetchone()
        if row is None:
            return None
        owner_id = _owner(owner_id)
        return {
            "owner_id": owner_id,
            "model_name": str(model_name or "behavior"),
            "payload": self._open_json(owner_id, row["payload_json"], "ios_behavior_models.payload_json"),
            "updated_at": row["updated_at"],
        }

    get_model = load_model
    model_parameters = load_model

    def record_behavior_feedback(self, owner_id: str, label: str, payload: Mapping[str, Any], observed_at: Any = None, feedback_id: str | None = None) -> dict[str, Any]:
        owner_id = _owner(owner_id)
        identifier = str(feedback_id or uuid.uuid4().hex)[:256]
        observed = _epoch(observed_at)
        feedback_payload = dict(payload)
        with self._connect() as conn, write_txn(conn):
            action = str(feedback_payload.get("action") or "").lower()
            notification_id = str(feedback_payload.get("notification_id") or "").strip()
            if (
                str(label or "") == "notification-value"
                and notification_id
                and (
                    feedback_payload.get("useful") is True
                    or action in {"opened", "accepted", "helpful"}
                )
            ):
                conn.execute(
                    "DELETE FROM ios_behavior_feedback WHERE id=? AND owner_id=?",
                    (self._expired_notification_feedback_id(notification_id), owner_id),
                )
            conn.execute(
                "INSERT OR IGNORE INTO ios_behavior_feedback VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    owner_id,
                    str(label or "event")[:128],
                    self._seal_json(owner_id, feedback_payload, "ios_behavior_feedback.payload_json"),
                    observed,
                    int(time.time()),
                ),
            )
            self._touch(conn, owner_id, int(time.time()))
        return {"id": identifier, "owner_id": owner_id, "label": str(label or "event"), "observed_at": observed}

    @staticmethod
    def _expired_notification_feedback_id(notification_id: str) -> str:
        digest = hashlib.sha256(str(notification_id).encode("utf-8")).hexdigest()[:40]
        return f"notification-expired:{digest}"

    record_feedback = record_behavior_feedback

    def weather_context_for_route(
        self,
        owner_id: str,
        started_at: Any,
        ended_at: Any,
    ) -> dict[str, Any] | None:
        """Return the weather observation nearest a completed route window."""

        owner_id = _owner(owner_id)
        started = _epoch(started_at)
        ended = max(started, _epoch(ended_at))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT observed_at,payload_json FROM ios_behavior_feedback "
                "WHERE owner_id=? AND label='weather-context' "
                "AND observed_at BETWEEN ? AND ? "
                "ORDER BY ABS(observed_at-?) LIMIT 1",
                (owner_id, started - 3600, ended + 3600, started),
            ).fetchone()
        if row is None:
            return None
        return {
            "observed_at": int(row["observed_at"]),
            **self._open_json(owner_id, row["payload_json"], "ios_behavior_feedback.payload_json"),
        }

    def save_quiet_summary(self, owner_id: str, local_date: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        owner_id = _owner(owner_id)
        date = str(local_date)[:32]
        now = int(time.time())
        with self._connect() as conn, write_txn(conn):
            conn.execute(
                "INSERT INTO ios_weather_quiet_summary VALUES(?,?,?,?,NULL) ON CONFLICT(owner_id,local_date) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at,delivered_at=NULL",
                (
                    owner_id,
                    date,
                    self._seal_json(owner_id, dict(payload), "ios_weather_quiet_summary.payload_json"),
                    now,
                ),
            )
        return {"owner_id": owner_id, "local_date": date, "payload": dict(payload), "delivered_at": None}

    def get_quiet_summary(self, owner_id: str, local_date: str, *, include_delivered: bool = False) -> dict[str, Any] | None:
        query = "SELECT payload_json,updated_at,delivered_at FROM ios_weather_quiet_summary WHERE owner_id=? AND local_date=?"
        args: tuple[Any, ...] = (_owner(owner_id), str(local_date))
        with self._connect() as conn:
            row = conn.execute(query, args).fetchone()
        if row is None or (row["delivered_at"] is not None and not include_delivered):
            return None
        owner_id = _owner(owner_id)
        return {
            "owner_id": owner_id,
            "local_date": str(local_date),
            "payload": self._open_json(owner_id, row["payload_json"], "ios_weather_quiet_summary.payload_json"),
            "updated_at": row["updated_at"],
            "delivered_at": row["delivered_at"],
        }

    def mark_quiet_summary_delivered(self, owner_id: str, local_date: str) -> bool:
        with self._connect() as conn, write_txn(conn):
            return bool(conn.execute(
                "UPDATE ios_weather_quiet_summary SET delivered_at=? WHERE owner_id=? AND local_date=? AND delivered_at IS NULL",
                (int(time.time()), _owner(owner_id), str(local_date)),
            ).rowcount)

    # ------------------------------------------------------------------
    # Cold storage and account lifecycle
    # ------------------------------------------------------------------
    def archive_cold_storage(
        self,
        owner_id: str,
        *,
        before: Any = None,
        destination: str | os.PathLike[str] | None = None,
        encrypt: bool = True,
        remove_hot: bool = True,
        limit: int = 50_000,
    ) -> dict[str, Any]:
        """Move a bounded trajectory batch into an immutable gzip segment."""
        owner_id = _owner(owner_id)
        deletion = self.account_deletion_status(owner_id)
        if deletion is not None:
            return {
                "owner_id": owner_id,
                "archived": False,
                "point_count": 0,
                "account_deleted": True,
            }
        cutoff = _epoch(before) if before is not None else int(time.time()) - 30 * 86400
        batch_limit = max(1, min(int(limit), 1_000_000))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ios_trajectory WHERE owner_id=? AND observed_at<? "
                "ORDER BY observed_at LIMIT ?",
                (owner_id, cutoff, batch_limit),
            ).fetchall()
        if not rows:
            return {"owner_id": owner_id, "archived": False, "point_count": 0}
        payload = (
            "\n".join(_json(self._decode_hot_row(owner_id, "ios_trajectory", row)) for row in rows) + "\n"
        ).encode("utf-8")
        compressed = gzip.compress(payload, mtime=0)
        encrypted = False
        if encrypt:
            compressed = self._encrypt_blob(compressed, owner_id)
            encrypted = True
        requested_destination = Path(destination) if destination else None
        if requested_destination and requested_destination.suffix in {".gz", ".enc"}:
            root = requested_destination.parent
        else:
            root = requested_destination or self.path.parent / "ios-cold"
        root.mkdir(parents=True, exist_ok=True)
        owner_dir = root / hashlib.sha256(owner_id.encode()).hexdigest()[:24]
        owner_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(compressed).hexdigest()
        segment_id = f"{rows[0]['observed_at']}-{rows[-1]['observed_at']}-{digest[:12]}"
        target = requested_destination if requested_destination and requested_destination.suffix in {".gz", ".enc"} else owner_dir / f"{segment_id}.jsonl.gz{'.enc' if encrypted else ''}"
        target = Path(target)
        removed_hot = 0
        tombstoned = False
        with self._connect() as conn, write_txn(conn):
            tombstoned = conn.execute(
                "SELECT 1 FROM ios_account_deletion_tombstones WHERE owner_id=?",
                (owner_id,),
            ).fetchone() is not None
            if not tombstoned:
                # Hold the same SQLite writer lock across file installation and
                # index insertion. Account deletion therefore runs either before
                # this block (and no file is created) or after it (and sees the
                # registered path); an unindexed archive cannot appear between.
                fd, temp_name = tempfile.mkstemp(prefix=".ios-cold-", dir=owner_dir)
                installed = False
                indexed = False
                target_existed = target.exists()
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(compressed)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp_name, target)
                    installed = True
                    conn.execute(
                        "INSERT OR IGNORE INTO ios_cold_segments VALUES(?,?,?,?,?,?,?,?,?)",
                        (owner_id, segment_id, rows[0]["observed_at"], rows[-1]["observed_at"], str(target), len(rows), digest, int(encrypted), int(time.time())),
                    )
                    indexed = True
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
                    if (installed or not target_existed) and not indexed:
                        try:
                            target.unlink()
                        except FileNotFoundError:
                            pass
                if remove_hot:
                    keys = [(owner_id, row["device_id"], row["event_id"]) for row in rows]
                    removed_hot = max(0, conn.executemany(
                        "DELETE FROM ios_trajectory WHERE owner_id=? AND device_id=? AND event_id=?",
                        keys,
                    ).rowcount)
                    conn.executemany(
                        "DELETE FROM ios_events WHERE owner_id=? AND device_id=? AND event_id=?",
                        keys,
                    )
        if tombstoned:
            return {
                "owner_id": owner_id,
                "archived": False,
                "point_count": 0,
                "account_deleted": True,
            }
        return {
            "owner_id": owner_id,
            "archived": True,
            "segment_id": segment_id,
            "path": str(target),
            "point_count": len(rows),
            "hot_points_removed": removed_hot,
            "checksum": digest,
            "encrypted": encrypted,
        }

    cold_archive = archive_cold_storage
    archive_trajectory = archive_cold_storage
    archive_old_trajectory = archive_cold_storage
    compact_trajectory = archive_cold_storage

    def list_cold_segments(self, owner_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ios_cold_segments WHERE owner_id=? ORDER BY start_at DESC LIMIT ?",
                (_owner(owner_id), max(1, min(int(limit), 10_000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_cold_segment(self, owner_id: str, segment_id: str, *, decrypt: bool = False) -> bytes:
        owner_id = _owner(owner_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT file_path,encrypted FROM ios_cold_segments WHERE owner_id=? AND segment_id=?",
                (owner_id, str(segment_id)),
            ).fetchone()
        if row is None:
            raise KeyError("cold segment not found")
        path = Path(str(row["file_path"]))
        if not path.is_file():
            raise FileNotFoundError(path)
        payload = path.read_bytes()
        return self._decrypt_blob(payload, owner_id) if decrypt and row["encrypted"] else payload

    def read_cold_trajectory(self, owner_id: str, segment_id: str) -> list[dict[str, Any]]:
        payload = self.read_cold_segment(owner_id, segment_id, decrypt=True)
        decoded = gzip.decompress(payload).decode("utf-8")
        return [json.loads(line) for line in decoded.splitlines() if line.strip()]

    cold_storage = list_cold_segments

    def _encrypt_blob(self, blob: bytes, owner_id: str) -> bytes:
        """Encrypt an archive with AES-GCM when the server key is configured."""
        key = self._account_key(owner_id)
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = os.urandom(12)
            return b"HERMES-AESGCM-1" + nonce + AESGCM(key).encrypt(nonce, blob, owner_id.encode())
        except ImportError as exc:
            raise RuntimeError("AES-GCM support is required for iOS cold storage") from exc

    def _decrypt_blob(self, blob: bytes, owner_id: str) -> bytes:
        aes_prefix = b"HERMES-AESGCM-1"
        if blob.startswith(aes_prefix):
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            except ImportError as exc:
                raise RuntimeError("AES-GCM support is required for iOS cold storage") from exc
            offset = len(aes_prefix)
            nonce, ciphertext = blob[offset:offset + 12], blob[offset + 12:]
            account_key = self._account_key(owner_id)
            try:
                return AESGCM(account_key).decrypt(nonce, ciphertext, owner_id.encode())
            except Exception:
                legacy_secret = _configured_secret(
                    os.getenv("HERMES_IOS_DATA_KEY"),
                    os.getenv("HERMES_DATA_ENCRYPTION_KEY"),
                ) or f"local-account-key:{owner_id}"
                legacy_key = hashlib.sha256(legacy_secret.encode("utf-8")).digest()
                try:
                    return AESGCM(legacy_key).decrypt(nonce, ciphertext, owner_id.encode())
                except Exception as exc:
                    raise RuntimeError("iOS cold segment authentication failed") from exc
        legacy_prefix = b"HERMES-KEYED-1"
        if blob.startswith(legacy_prefix):
            legacy_secret = _configured_secret(
                os.getenv("HERMES_IOS_DATA_KEY"),
                os.getenv("HERMES_DATA_ENCRYPTION_KEY"),
            ) or f"local-account-key:{owner_id}"
            key = hashlib.sha256(legacy_secret.encode("utf-8")).digest()
            stream = hashlib.sha256(key + owner_id.encode()).digest()
            payload = blob[len(legacy_prefix):]
            return bytes(value ^ stream[index % len(stream)] for index, value in enumerate(payload))
        return blob

    @staticmethod
    def _encrypt_export_blob(blob: bytes, owner_id: str, passphrase: str) -> bytes:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise RuntimeError("AES-GCM support is required for account exports") from exc
        salt = os.urandom(16)
        nonce = os.urandom(12)
        key = hashlib.scrypt(
            passphrase.encode("utf-8"),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )
        ciphertext = AESGCM(key).encrypt(nonce, blob, owner_id.encode("utf-8"))
        return b"HERMES-EXPORT-1" + salt + nonce + ciphertext

    @staticmethod
    def decrypt_account_export(blob: bytes, owner_id: str, passphrase: str) -> bytes:
        prefix = b"HERMES-EXPORT-1"
        if not blob.startswith(prefix):
            raise ValueError("unsupported account export envelope")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise RuntimeError("AES-GCM support is required for account exports") from exc
        offset = len(prefix)
        salt = blob[offset:offset + 16]
        nonce = blob[offset + 16:offset + 28]
        ciphertext = blob[offset + 28:]
        if len(salt) != 16 or len(nonce) != 12 or not ciphertext:
            raise ValueError("invalid account export envelope")
        key = hashlib.scrypt(
            passphrase.encode("utf-8"),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )
        return AESGCM(key).decrypt(nonce, ciphertext, owner_id.encode("utf-8"))

    def export_account(
        self,
        owner_id: str,
        destination: str | os.PathLike[str] | None = None,
        *,
        encrypt: bool = False,
        export_passphrase: str | None = None,
        include_cold: bool = True,
    ) -> dict[str, Any]:
        owner_id = _owner(owner_id)
        tables = (
            "ios_events", "ios_snapshots", "ios_trajectory", "ios_places", "ios_upload_cursors",
            "ios_device_commands", "ios_active_forecasts", "ios_notification_outbox", "ios_place_graph",
            "ios_route_graph", "ios_route_samples", "ios_behavior_models", "ios_behavior_feedback", "ios_weather_quiet_summary",
        )
        export: dict[str, Any] = {"format": "hermes-ios-account-v1", "owner_id": owner_id, "exported_at": int(time.time()), "tables": {}}
        with self._connect() as conn:
            for table in tables:
                if table in {"ios_events", "ios_snapshots"}:
                    placeholders = ",".join("?" for _kind_name in _BLOCKED_EVENT_KINDS)
                    rows = conn.execute(
                        f"SELECT * FROM {table} WHERE owner_id=? AND kind NOT IN ({placeholders})",
                        (owner_id, *_BLOCKED_EVENT_KINDS),
                    ).fetchall()
                else:
                    rows = conn.execute(f"SELECT * FROM {table} WHERE owner_id=?", (owner_id,)).fetchall()
                export["tables"][table] = [
                    self._decode_hot_row(owner_id, table, row) for row in rows
                ]
            if include_cold:
                rows = conn.execute("SELECT * FROM ios_cold_segments WHERE owner_id=?", (owner_id,)).fetchall()
                export["cold_segments"] = [dict(row) for row in rows]
        if include_cold:
            export["cold_trajectory"] = []
            for segment in export.get("cold_segments", []):
                export["cold_trajectory"].extend(
                    self.read_cold_trajectory(owner_id, str(segment["segment_id"]))
                )
        raw = _json(export).encode("utf-8")
        payload: bytes
        if encrypt and export_passphrase is not None:
            payload = self._encrypt_export_blob(raw, owner_id, export_passphrase)
        elif encrypt:
            payload = self._encrypt_blob(raw, owner_id)
        else:
            payload = raw
        if destination is not None:
            target = Path(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            return {"owner_id": owner_id, "path": str(target), "bytes": len(payload), "encrypted": encrypt}
        if encrypt:
            return {
                "owner_id": owner_id,
                "encrypted": True,
                "format": "hermes-account-export-v1",
                "algorithm": "AES-256-GCM",
                "kdf": "scrypt-N16384-r8-p1" if export_passphrase is not None else "account-key",
                "blob_base64": base64.b64encode(payload).decode("ascii"),
                "bytes": len(payload),
            }
        return export

    export_account_data = export_account

    def begin_account_deletion(self, owner_id: str, owner_scope: str = "") -> dict[str, Any]:
        """Commit the fail-closed deletion intent before any destructive work."""

        owner_id = _owner(owner_id)
        normalized_scope = str(owner_scope or "").strip()[:2048]
        now = int(time.time())
        with self._connect() as conn, write_txn(conn):
            conn.execute(
                "INSERT INTO ios_account_deletion_tombstones("
                "owner_id,owner_scope,status,attempts,last_error,requested_at,updated_at,completed_at"
                ") VALUES(?,?,'pending',0,'',?,?,NULL) "
                "ON CONFLICT(owner_id) DO UPDATE SET "
                "owner_scope=CASE WHEN excluded.owner_scope<>'' THEN excluded.owner_scope "
                "ELSE ios_account_deletion_tombstones.owner_scope END,"
                "status='pending',last_error='',updated_at=excluded.updated_at,completed_at=NULL",
                (owner_id, normalized_scope, now, now),
            )
        return {
            "owner_id": owner_id,
            "owner_scope": normalized_scope,
            "state": "pending",
            "requested_at": now,
        }

    def _purge_account_hot_data(self, owner_id: str) -> dict[str, int]:
        owner_id = _owner(owner_id)
        deleted: dict[str, int] = {}
        with self._connect() as conn, write_txn(conn):
            for table in _ACCOUNT_HOT_TABLES:
                deleted[table] = int(
                    conn.execute(
                        f"DELETE FROM {table} WHERE owner_id=?",
                        (owner_id,),
                    ).rowcount
                )
        return deleted

    def delete_account(self, owner_id: str, *, delete_cold: bool = True) -> dict[str, Any]:
        owner_id = _owner(owner_id)
        if delete_cold:
            self.begin_account_deletion(owner_id)
        deleted = self._purge_account_hot_data(owner_id)
        if not delete_cold:
            return {
                "owner_id": owner_id,
                "deleted": deleted,
                "cold_files_removed": 0,
                "state": "complete",
            }
        cleanup = self._retry_account_deletion(owner_id)
        return {"owner_id": owner_id, "deleted": deleted, **cleanup}

    def _retry_account_deletion(self, owner_id: str) -> dict[str, Any]:
        """Delete registered cold files without losing paths that need retry."""

        owner_id = _owner(owner_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT segment_id,file_path FROM ios_cold_segments "
                "WHERE owner_id=? ORDER BY created_at,segment_id",
                (owner_id,),
            ).fetchall()

        removable: list[str] = []
        removed_files = 0
        failed = 0
        last_error = ""
        path_outcomes: dict[str, bool] = {}
        for row in rows:
            segment_id = str(row["segment_id"])
            path_text = str(row["file_path"] or "").strip()
            if not path_text:
                removable.append(segment_id)
                continue
            if path_text in path_outcomes:
                if path_outcomes[path_text]:
                    removable.append(segment_id)
                else:
                    failed += 1
                continue
            path = Path(path_text)
            try:
                os.lstat(path)
            except FileNotFoundError:
                path_outcomes[path_text] = True
                removable.append(segment_id)
                continue
            except OSError as exc:
                path_outcomes[path_text] = False
                failed += 1
                last_error = type(exc).__name__
                continue
            try:
                path.unlink()
                removed_files += 1
                path_outcomes[path_text] = True
                removable.append(segment_id)
            except FileNotFoundError:
                path_outcomes[path_text] = True
                removable.append(segment_id)
            except OSError as exc:
                path_outcomes[path_text] = False
                failed += 1
                last_error = type(exc).__name__

        now = int(time.time())
        with self._connect() as conn, write_txn(conn):
            if removable:
                conn.executemany(
                    "DELETE FROM ios_cold_segments WHERE owner_id=? AND segment_id=?",
                    [(owner_id, segment_id) for segment_id in removable],
                )
            remaining = int(conn.execute(
                "SELECT COUNT(*) FROM ios_cold_segments WHERE owner_id=?",
                (owner_id,),
            ).fetchone()[0])
            state = "complete" if remaining == 0 else "pending"
            conn.execute(
                "UPDATE ios_account_deletion_tombstones SET "
                "status=?,attempts=attempts+1,last_error=?,updated_at=?,completed_at=? "
                "WHERE owner_id=?",
                (
                    state,
                    "" if state == "complete" else (last_error or "cold_segment_delete_failed"),
                    now,
                    now if state == "complete" else None,
                    owner_id,
                ),
            )

        if remaining == 0:
            root = self.path.parent / "ios-cold" / hashlib.sha256(owner_id.encode()).hexdigest()[:24]
            try:
                root.rmdir()
            except OSError:
                pass
        return {
            "cold_files_removed": removed_files,
            "cold_segments_pending": remaining,
            "cold_segments_failed": failed,
            "state": state,
        }

    def retry_account_deletions(
        self,
        *,
        owner_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Resume pending hot-row and cold-file deletion after restart."""

        clauses = ["status='pending'"]
        values: list[Any] = []
        if str(owner_id or "").strip():
            clauses.append("owner_id=?")
            values.append(_owner(owner_id))
        values.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT owner_id FROM ios_account_deletion_tombstones "
                f"WHERE {' AND '.join(clauses)} ORDER BY updated_at LIMIT ?",
                tuple(values),
            ).fetchall()
        outcomes: list[dict[str, Any]] = []
        for row in rows:
            pending_owner = str(row["owner_id"])
            self._purge_account_hot_data(pending_owner)
            outcomes.append(self._retry_account_deletion(pending_owner))
        return outcomes

    def account_deletion_status(self, owner_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ios_account_deletion_tombstones WHERE owner_id=?",
                (_owner(owner_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def account_deletion_sagas(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ios_account_deletion_tombstones "
                "ORDER BY requested_at LIMIT ?",
                (max(1, min(int(limit), 10_000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    delete_account_data = delete_account


@dataclass
class QWeatherClient:
    store: IOSIntelligenceStore
    api_key: str | None = None
    base_url: str | None = None
    client: httpx.Client | None = None
    timezone: str | None = None

    def __post_init__(self) -> None:
        self.api_key = _configured_secret(
            self.api_key, os.getenv("HERMES_QWEATHER_API_KEY"), os.getenv("QWEATHER_API_KEY")
        )
        self.base_url = (
            self.base_url
            or os.getenv("HERMES_QWEATHER_API_BASE_URL")
            or "https://api.qweather.com"
        ).rstrip("/")
        self.timezone = str(self.timezone or DEFAULT_TIMEZONE)

    def request(self, endpoint: str, params: Mapping[str, Any], *, cache_seconds: int = 300) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("QWeather server credential is not configured")
        safe_params = {str(key): str(value) for key, value in params.items() if value is not None}
        cache_key = f"{endpoint}?{_json(dict(sorted(safe_params.items())))}"
        cached = self.store.get_cache("qweather", cache_key)
        if cached is not None:
            return {**cached, "_cache": True}
        # Monthly quota buckets follow the configured product timezone, not the
        # process host TZ, so UTC hosts do not roll the counter early/late.
        if self.store.weather_quota_status(timezone=self.timezone)["soft_limited"]:
            cache_seconds = max(int(cache_seconds), 1800)
        quota = self.store.reserve_weather_requests(timezone=self.timezone)
        if not quota["allowed"]:
            raise RuntimeError("QWeather monthly request limit reached")
        try:
            if self.client is not None:
                response = self.client.get(
                    f"{self.base_url}{endpoint}",
                    params={**safe_params, "key": self.api_key},
                )
            else:
                with httpx.Client(timeout=12.0) as client:
                    response = client.get(
                        f"{self.base_url}{endpoint}",
                        params={**safe_params, "key": self.api_key},
                    )
            response.raise_for_status()
            result = {"code": "204"} if getattr(response, "status_code", None) == 204 else response.json()
        except Exception as exc:
            raise RuntimeError(_redact_secret_text(exc, self.api_key)) from exc
        if not isinstance(result, dict):
            raise RuntimeError("QWeather returned an invalid response")
        response_code = str(result.get("code") or "")
        if response_code and response_code not in {"200", "204"}:
            raise RuntimeError(
                _redact_secret_text(
                    f"QWeather request failed with code {response_code}",
                    self.api_key,
                )
            )
        self.store.put_cache("qweather", cache_key, result, cache_seconds)
        return {**result, "_cache": False}

    def minutely(self, latitude: float, longitude: float) -> dict[str, Any]:
        return self.request("/v7/minutely/5m", {"location": f"{longitude:.6f},{latitude:.6f}"}, cache_seconds=240)

    def current(self, latitude: float, longitude: float) -> dict[str, Any]:
        return self.request("/v7/weather/now", {"location": f"{longitude:.6f},{latitude:.6f}"}, cache_seconds=300)

    def hourly(self, latitude: float, longitude: float, hours: int = 24) -> dict[str, Any]:
        endpoint = "/v7/weather/24h" if int(hours) <= 24 else "/v7/weather/72h"
        return self.request(endpoint, {"location": f"{longitude:.6f},{latitude:.6f}"}, cache_seconds=600)

    def warnings(self, latitude: float, longitude: float) -> dict[str, Any]:
        return self.request("/v7/warning/now", {"location": f"{longitude:.6f},{latitude:.6f}"}, cache_seconds=300)


@dataclass
class AMapClient:
    api_key: str | None = None
    base_url: str | None = None
    client: httpx.Client | None = None

    def __post_init__(self) -> None:
        self.api_key = _configured_secret(
            self.api_key, os.getenv("HERMES_AMAP_WEB_API_KEY"), os.getenv("AMAP_WEB_API_KEY")
        )
        self.base_url = (
            self.base_url
            or os.getenv("HERMES_AMAP_API_BASE_URL")
            or os.getenv("HERMES_AMAP_WEB_API_BASE_URL")
            or "https://restapi.amap.com"
        ).rstrip("/")

    def request(self, endpoint: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("AMap server credential is not configured")
        try:
            request_params = {
                **{
                    str(key): value
                    for key, value in params.items()
                    if value is not None
                },
                "key": self.api_key,
            }
            if self.client is not None:
                response = self.client.get(
                    f"{self.base_url}{endpoint}",
                    params=request_params,
                )
            else:
                with httpx.Client(timeout=12.0) as client:
                    response = client.get(
                        f"{self.base_url}{endpoint}",
                        params=request_params,
                    )
            response.raise_for_status()
            result = response.json()
        except Exception as exc:
            raise RuntimeError(_redact_secret_text(exc, self.api_key)) from exc
        if not isinstance(result, dict):
            raise RuntimeError("AMap request failed: invalid response")
        status = result.get("status")
        errcode = result.get("errcode")
        failed_v3 = status is not None and str(status) != "1"
        failed_v4 = errcode is not None and str(errcode) != "0"
        if failed_v3 or failed_v4:
            detail = result.get("info") or result.get("errmsg") or "invalid response"
            raise RuntimeError(
                _redact_secret_text(f"AMap request failed: {detail}", self.api_key)
            )
        return result

    def reverse_geocode(self, latitude: float, longitude: float) -> dict[str, Any]:
        return self.request("/v3/geocode/regeo", {"location": f"{longitude:.6f},{latitude:.6f}", "extensions": "all"})

    def search_poi(
        self,
        keywords: str,
        *,
        city: str = "",
        location: str = "",
        types: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        query = str(keywords or "").strip()
        if not query:
            raise ValueError("keywords is required")
        return self.request(
            "/v3/place/text",
            {
                "keywords": query,
                "city": str(city or "").strip() or None,
                "location": str(location or "").strip() or None,
                "types": str(types or "").strip() or None,
                "page": max(1, int(page)),
                "offset": max(1, min(50, int(page_size))),
                "extensions": "all",
            },
        )

    poi = search_poi

    def route(self, origin: str, destination: str, mode: str = "walking", city: str = "") -> dict[str, Any]:
        mode = str(mode).lower()
        mode = {"cycling": "bicycling", "bike": "bicycling", "automotive": "driving"}.get(mode, mode)
        endpoints = {
            "walking": "/v3/direction/walking", "driving": "/v3/direction/driving",
            "transit": "/v3/direction/transit/integrated", "bicycling": "/v4/direction/bicycling",
        }
        if mode not in endpoints:
            raise ValueError("mode must be walking, bicycling, driving, or transit")
        return self.request(endpoints[mode], {"origin": origin, "destination": destination, "city": city or None})


__all__ = [
    "AMapClient",
    "DEFAULT_TIMEZONE",
    "IOSIntelligenceStore",
    "KNOWN_FEATURE_CAPABILITIES",
    "QWeatherClient",
    "WEATHER_MONTHLY_LIMIT",
    "WEATHER_SOFT_LIMIT",
    "load_ios_feature_weights",
]
