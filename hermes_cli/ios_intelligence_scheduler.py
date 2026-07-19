"""Continuous behavior and smart-weather evaluation for iOS intelligence.

The scheduler is intentionally small and deterministic.  It consumes the
SQLite store's immutable events, calls the external clients only when a leave
prediction justifies a query, and writes notifications to the durable outbox.
An LLM can enrich an evaluation after the fact; it is never required for the
high-frequency location loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from collections import Counter
import hashlib
import json
import logging
import threading
import time
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from hermes_cli.ios_intelligence import (
    AMapClient,
    DEFAULT_TIMEZONE,
    IOSIntelligenceStore,
    QWeatherClient,
    _distance_meters,
    _epoch,
)
from hermes_cli.ios_intelligence_config import IOSIntelligenceConfig, load_ios_intelligence_config


logger = logging.getLogger(__name__)
FIXED_STUDY_PLACE_ID = "study-quanzhou-91-bainaohui"
FIXED_STUDY_PLACE_NAME = "泉州九一百脑汇"


@dataclass(frozen=True)
class RouteEstimate:
    mode: str = "walking"
    duration_seconds: int = 20 * 60
    distance_meters: float = 0.0
    outdoor_minutes: float = 20.0
    source: str = "baseline"
    waypoints: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class WeatherWindow:
    starts_at: int
    ends_at: int
    departure_at: int
    arrival_at: int
    duration_seconds: int

    def as_dict(self) -> dict[str, int]:
        return {
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "departure_at": self.departure_at,
            "arrival_at": self.arrival_at,
            "duration_seconds": self.duration_seconds,
            "start_minute": self.starts_at // 60,
            "end_minute": self.ends_at // 60,
        }


class CloudBehaviorSemanticAnalyzer:
    """Bounded cloud-model analysis for complex schedules and anomalies."""

    _SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "should_adjust",
            "leave_probability",
            "expected_departure_at",
            "destination_place_id",
            "confidence",
            "anomaly",
            "tags",
        ],
        "properties": {
            "should_adjust": {"type": "boolean"},
            "leave_probability": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
            "expected_departure_at": {"type": ["integer", "null"]},
            "destination_place_id": {"type": "string", "maxLength": 256},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "anomaly": {"type": "boolean"},
            "tags": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string", "maxLength": 64},
            },
        },
    }

    def __init__(self, llm: Any | None = None) -> None:
        self._llm = llm

    def analyze(
        self,
        context: Mapping[str, Any],
        *,
        timeout_seconds: int = 20,
    ) -> dict[str, Any]:
        if self._llm is None:
            from agent.plugin_llm import PluginLlm

            self._llm = PluginLlm(plugin_id="ios-intelligence")
        response = self._llm.complete_structured(
            instructions=(
                "Analyze the supplied personal schedule and behavior features only when they "
                "clarify a complex schedule, an anomalous pattern, or conflicting context. "
                "Do not invent places or events. destination_place_id must be empty or one of "
                "candidate_place_ids. Return a conservative adjustment as JSON."
            ),
            input=[{
                "type": "text",
                "text": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
            }],
            json_schema=self._SCHEMA,
            schema_name="ios_behavior_semantic_adjustment",
            system_prompt=(
                "You are the low-frequency semantic layer of a personal behavior model. "
                "The deterministic model remains authoritative when evidence is weak."
            ),
            temperature=0.1,
            max_tokens=600,
            timeout=float(timeout_seconds),
            purpose="complex_schedule_anomaly_analysis",
        )
        parsed = response.parsed if isinstance(response.parsed, Mapping) else {}
        return {
            **dict(parsed),
            "provider": str(response.provider or ""),
            "model": str(response.model or ""),
        }


class IOSIntelligenceScheduler:
    """Evaluate active accounts and enqueue useful weather reminders."""

    def __init__(
        self,
        store: IOSIntelligenceStore | None = None,
        *,
        config: IOSIntelligenceConfig | Mapping[str, Any] | None = None,
        qweather: QWeatherClient | Any | None = None,
        amap: AMapClient | Any | None = None,
        notifier: Callable[[Mapping[str, Any]], Any] | None = None,
        supervisor: Any | None = None,
        semantic_analyzer: Any | None = None,
        clock: Callable[[], Any] | None = None,
        cleanup_only: bool = False,
    ) -> None:
        self.store = store or IOSIntelligenceStore()
        if isinstance(config, IOSIntelligenceConfig):
            self.config = config
        elif isinstance(config, Mapping):
            self.config = load_ios_intelligence_config(config)
        else:
            self.config = load_ios_intelligence_config()
        self.qweather = qweather
        self.amap = amap
        self.notifier = notifier
        self.supervisor = supervisor
        self.semantic_analyzer = semantic_analyzer
        self.clock = clock or (lambda: int(time.time()))
        self.cleanup_only = bool(cleanup_only)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> "IOSIntelligenceScheduler":
        with self._lock:
            if self.running:
                return self
            self._stop_event.clear()
            self.running = True
            thread_name = (
                "ios-account-deletion-recovery"
                if self.cleanup_only
                else "ios-intelligence"
            )
            self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self.running = False
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(max(0.1, float(timeout)))
        with self._lock:
            self._thread = None

    close = stop

    def _run(self) -> None:
        interval = max(5, int(self.config.weather.evaluation_interval_seconds))
        while not self._stop_event.is_set():
            try:
                self._run_cycle()
            except Exception:
                logger.exception("iOS intelligence scheduler evaluation failed")
            self._stop_event.wait(interval)

    def _run_cycle(self) -> dict[str, Any]:
        """Run one recovery cycle and, when enabled, one prediction cycle."""

        result: dict[str, Any] = {
            "account_deletions": self._resume_account_deletions(),
            "cleanup_only": self.cleanup_only,
        }
        if self.cleanup_only:
            return result
        result["evaluations"] = self.evaluate_all()
        result["notification_deliveries"] = self.deliver_pending_notifications()
        return result

    def _resume_account_deletions(self) -> dict[str, Any]:
        """Retry both cold-storage and device tombstones after any restart."""

        cold = self.store.retry_account_deletions(limit=100)
        reconciled = 0
        try:
            from hermes_cli.dashboard_auth.mobile_device_store import MobileDeviceStore
            from hermes_cli.dashboard_auth.mobile_notifications import (
                process_account_deletion_outbox,
            )

            mobile_store = MobileDeviceStore()
            reconciled = 0
            saga_loader = getattr(self.store, "account_deletion_sagas", None)
            sagas = saga_loader(limit=1000) if callable(saga_loader) else []
            for saga in sagas:
                owner_id = str(saga.get("owner_id") or "")
                owner_scope = str(saga.get("owner_scope") or "")
                if (
                    owner_id
                    and owner_scope
                    and mobile_store.account_deletion_status(owner_id) is None
                ):
                    mobile_store.begin_account_deletion(owner_id, owner_scope)
                    reconciled += 1
            mobile = process_account_deletion_outbox(limit=100)
            from hermes_cli.dashboard_auth.owner_mobile import (
                reconcile_deleted_owner_credentials,
            )

            credentials = reconcile_deleted_owner_credentials()
        except Exception:
            logger.exception("mobile account deletion recovery failed")
            mobile = []
            credentials = {"disabled": False, "config_cleared": False}
        return {
            "cold": cold,
            "mobile": mobile,
            "mobile_intents_reconciled": reconciled,
            "credentials": credentials,
        }

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def evaluate_all(self, *, force: bool = False, now: Any = None) -> list[dict[str, Any]]:
        instant = _epoch(now) if now is not None else _epoch(self.clock())
        accounts = self.store.active_accounts()
        results: list[dict[str, Any]] = []
        cold_before = instant - int(self.config.storage.cold_after_days) * 86400
        for account in accounts:
            result = self.evaluate_account(account, force=force, now=instant)
            try:
                result["cold_archive"] = self.store.archive_cold_storage(
                    account,
                    before=cold_before,
                    encrypt=self.config.storage.require_encryption,
                    remove_hot=True,
                    limit=self.config.storage.archive_batch_points,
                )
            except Exception as exc:
                logger.error("iOS trajectory cold archive failed for %s: %s", account, type(exc).__name__)
                result["cold_archive"] = {"archived": False, "error": type(exc).__name__}
            results.append(result)
        return results

    def evaluate_account(self, owner_id: str, *, force: bool = False, now: Any = None) -> dict[str, Any]:
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            raise ValueError("owner_id is required")
        instant = _epoch(now) if now is not None else _epoch(self.clock())
        feature_weights = self._feature_weights()
        behavior = self.store.evaluate_behavior(
            owner_id,
            instant,
            feature_weights=feature_weights,
            timezone=self.config.timezone,
        )
        behavior["feature_weights"] = feature_weights
        behavior["semantic"] = self._maybe_semantic_enrich(owner_id, behavior, instant)
        # Persist the lightweight model on every real evaluation.  This is
        # cheap and gives a cloud learner stable features and labels to refine.
        try:
            self.store.save_model(owner_id, "behavior", behavior)
            self._ensure_fixed_study_place(owner_id, instant)
            self._learn_graph(owner_id)
            self.store.learn_home(owner_id, self.config.timezone)
        except Exception:
            logger.exception("failed to update derived behavior state for %s", owner_id)

        result: dict[str, Any] = {
            "owner_id": owner_id,
            "evaluated_at": instant,
            "behavior": behavior,
            "weather_queried": False,
            "notification": None,
            "suppressed_reason": None,
        }
        self._prepare_predicted_departure(owner_id, behavior, instant, result)
        if not self.config.enabled or not self.config.weather.enabled:
            result["weather_reconciliation"] = self._expire_stale_weather(owner_id, instant)
            result["suppressed_reason"] = "disabled"
            return result
        local = datetime.fromtimestamp(instant, ZoneInfo(self.config.timezone))
        self._flush_quiet_summary(owner_id, local, instant, result)
        if result.get("quiet_summary_notification") is not None:
            result["suppressed_reason"] = "quiet_summary"
            return result
        if not force and behavior.get("leave_probability", 0.0) < self.config.weather.minimum_leave_probability:
            result["weather_reconciliation"] = self._expire_stale_weather(owner_id, instant)
            result["suppressed_reason"] = "low_leave_probability"
            return result
        if behavior.get("suppress_weather_query") and not force:
            result["weather_reconciliation"] = self._expire_stale_weather(owner_id, instant)
            result["suppressed_reason"] = "indoor_stationary"
            return result
        destination = self._choose_destination(owner_id, behavior)
        if destination is None:
            result["weather_reconciliation"] = self._expire_stale_weather(owner_id, instant)
            result["suppressed_reason"] = "no_destination"
            return result
        route = self._estimate_route(owner_id, behavior, destination)
        departure = int(behavior.get("expected_departure_at") or instant + 15 * 60)
        if departure < instant - 5 * 60:
            departure = instant
        window = self._weather_window(departure, route)
        result["route"] = route.__dict__.copy()
        result["weather_window"] = window.as_dict()
        forecast = self._query_weather(owner_id, destination, window, route)
        result["weather_queried"] = bool(forecast.get("queried"))
        result["weather"] = forecast
        decision = self._weather_decision(forecast, window)
        value_model = behavior.get("notification_value") or {}
        if int(value_model.get("samples") or 0) >= 3:
            learned_score = float(value_model.get("score") or 0.0)
            learned_threshold = max(
                0.35,
                min(0.9, self.config.weather.minimum_alert_score + (0.5 - learned_score) * 0.2),
            )
            decision["threshold"] = round(learned_threshold, 3)
            decision["should_notify"] = float(decision.get("score") or 0.0) >= learned_threshold
            decision["reason"] = "weather_event" if decision["should_notify"] else "learned_threshold"
        result["alert"] = decision
        if self._is_quiet(local):
            result["weather_reconciliation"] = self._expire_stale_weather(owner_id, instant)
            result["suppressed_reason"] = "quiet_hours"
            if decision["should_notify"]:
                result["quiet_summary"] = self._save_quiet_summary(
                    owner_id,
                    destination,
                    route,
                    window,
                    instant,
                    decision,
                )
            else:
                summary_date = datetime.fromtimestamp(
                    window.departure_at,
                    ZoneInfo(self.config.timezone),
                ).date().isoformat()
                self.store.mark_quiet_summary_delivered(owner_id, summary_date)
            return result
        if decision["should_notify"]:
            notification = self._enqueue_weather_notification(owner_id, destination, route, window, decision, instant)
            result["notification"] = notification
        else:
            if forecast.get("queried") and not forecast.get("errors"):
                result["weather_reconciliation"] = self._expire_stale_weather(owner_id, instant)
            result["suppressed_reason"] = decision.get("reason") or "low_weather_impact"
        return result

    evaluate = evaluate_account

    def _prepare_predicted_departure(
        self,
        owner_id: str,
        behavior: Mapping[str, Any],
        now: int,
        result: dict[str, Any],
    ) -> None:
        result["location_prepare_command"] = None
        departure_value = behavior.get("expected_departure_at")
        if departure_value is None:
            return
        try:
            departure = _epoch(departure_value)
        except (TypeError, ValueError):
            return
        if departure < now - 5 * 60 or departure > now + 45 * 60:
            return
        queue = getattr(self.store, "queue_device_command", None)
        if not callable(queue):
            return
        try:
            result["location_prepare_command"] = queue(
                owner_id,
                "ios-location",
                "set-predicted-departure",
                {"timestamp": departure},
                idempotency_key=f"predicted-departure:{owner_id}:{departure // 300}",
                expires_at=departure + 15 * 60,
            )
        except Exception:
            logger.debug("predicted-departure location command unavailable", exc_info=True)

    def _feature_weights(self) -> dict[str, float]:
        """Down-weight only the feature owned by an unhealthy MCP."""

        supervisor = self.supervisor
        if supervisor is None:
            try:
                from hermes_cli.ios_mcp_supervisor import IOSMCPSupervisor

                supervisor = IOSMCPSupervisor()
            except Exception:
                return {}
        try:
            statuses = supervisor.statuses()
        except Exception:
            return {}
        weights: dict[str, float] = {}
        for item in statuses:
            state = str(item.get("state") or "RUNNING")
            weights[str(item.get("name") or "")] = {
                "DISABLED": 0.0,
                "QUARANTINED": 0.2,
                "DEGRADED": 0.5,
                "RECOVERING": 0.6,
                "UPGRADING": 0.8,
            }.get(state, 1.0)
        return weights

    def _maybe_semantic_enrich(
        self,
        owner_id: str,
        behavior: dict[str, Any],
        now: int,
    ) -> dict[str, Any]:
        policy = self.config.semantic
        if not policy.enabled:
            return {"status": "disabled", "applied": False}
        if self.semantic_analyzer is None:
            return {"status": "unavailable", "applied": False}

        context, triggers = self._semantic_context(owner_id, behavior, now)
        if not triggers:
            return {"status": "not_needed", "applied": False, "triggers": []}
        context["triggers"] = triggers
        fingerprint_context = {
            key: value for key, value in context.items() if key != "evaluated_at"
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_context,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        previous = self.store.load_model(owner_id, "semantic-behavior")
        previous_payload = dict((previous or {}).get("payload") or {})
        previous_at = int(previous_payload.get("analyzed_at") or 0)
        if previous_at and now - previous_at < policy.minimum_interval_seconds:
            applied = False
            if previous_payload.get("fingerprint") == fingerprint:
                applied = self._apply_semantic_adjustment(
                    behavior,
                    dict(previous_payload.get("adjustment") or {}),
                    now,
                )
            return {
                "status": "reused" if applied else "throttled",
                "applied": applied,
                "analyzed_at": previous_at,
                "triggers": triggers,
            }

        analyzer = self.semantic_analyzer
        try:
            if callable(getattr(analyzer, "analyze", None)):
                raw = analyzer.analyze(context, timeout_seconds=policy.timeout_seconds)
            elif callable(analyzer):
                raw = analyzer(context, timeout_seconds=policy.timeout_seconds)
            else:
                raise TypeError("semantic analyzer is not callable")
            if not isinstance(raw, Mapping):
                raise TypeError("semantic analyzer returned a non-object")
            adjustment = self._normalize_semantic_adjustment(raw)
            applied = self._apply_semantic_adjustment(behavior, adjustment, now)
            payload = {
                "analyzed_at": now,
                "fingerprint": fingerprint,
                "triggers": triggers,
                "adjustment": adjustment,
                "provider": str(raw.get("provider") or "")[:128],
                "model": str(raw.get("model") or "")[:256],
                "status": "applied" if applied else "observed",
            }
            self.store.save_model(owner_id, "semantic-behavior", payload)
            return {
                "status": payload["status"],
                "applied": applied,
                "analyzed_at": now,
                "triggers": triggers,
                "anomaly": bool(adjustment.get("anomaly")),
                "tags": list(adjustment.get("tags") or []),
                "provider": payload["provider"],
                "model": payload["model"],
            }
        except Exception as exc:
            self.store.save_model(owner_id, "semantic-behavior", {
                "analyzed_at": now,
                "fingerprint": fingerprint,
                "triggers": triggers,
                "adjustment": {},
                "status": "error",
                "error": type(exc).__name__,
            })
            logger.warning(
                "iOS semantic behavior analysis failed for %s: %s",
                owner_id,
                type(exc).__name__,
            )
            return {
                "status": "error",
                "applied": False,
                "analyzed_at": now,
                "triggers": triggers,
                "error": type(exc).__name__,
            }

    def _semantic_context(
        self,
        owner_id: str,
        behavior: Mapping[str, Any],
        now: int,
    ) -> tuple[dict[str, Any], list[str]]:
        maximum = self.config.semantic.maximum_schedule_items
        schedule: list[dict[str, Any]] = []
        for kind in ("calendar", "reminder"):
            for item in self.store.list_snapshots(owner_id, kind, limit=maximum):
                data = dict(item.get("data") or {})
                raw_time = (
                    data.get("start")
                    or data.get("due")
                    or data.get("due_at")
                    or data.get("dueDate")
                    or item.get("observed_at")
                )
                try:
                    event_at = _epoch(raw_time)
                except (TypeError, ValueError):
                    continue
                if event_at < now - 3600 or event_at > now + 24 * 3600:
                    continue
                schedule.append({
                    "kind": kind,
                    "title": str(data.get("title") or data.get("name") or "")[:160],
                    "location": str(data.get("location") or "")[:160],
                    "notes": str(data.get("notes") or "")[:240],
                    "starts_at": event_at,
                    "ends_at": self._optional_epoch(data.get("end")),
                })
        schedule.sort(key=lambda item: item["starts_at"])
        schedule = schedule[:maximum]

        feature_summary: dict[str, Any] = {}
        for name, snapshot in dict(behavior.get("context_features") or {}).items():
            data = snapshot.get("data", {}) if isinstance(snapshot, Mapping) else {}
            bounded = self._bounded_semantic_value(data)
            if bounded not in ({}, [], "", None):
                feature_summary[str(name)[:64]] = bounded

        candidates = []
        seen = set()
        for key in ("destination_candidates", "frequent_destinations"):
            for item in behavior.get(key, []) or []:
                place_id = str(item.get("place_id") or "")[:256]
                if not place_id or place_id in seen:
                    continue
                seen.add(place_id)
                candidates.append({
                    "place_id": place_id,
                    "name": str(item.get("name") or "")[:160],
                })

        triggers: list[str] = []
        if len(schedule) >= 2 or any(item.get("location") or item.get("notes") for item in schedule):
            triggers.append("complex_schedule")
        current_place = behavior.get("current_place")
        effective_motion_state = (
            behavior.get("effective_motion_state")
            if "effective_motion_state" in behavior
            else behavior.get("motion_state")
        )
        anomalous = bool(current_place) and (
            float(behavior.get("confidence") or 0.0)
            < self.config.semantic.anomaly_confidence_threshold
            or int(behavior.get("samples") or 0) < 2
            or (bool(effective_motion_state) and not candidates)
        )
        if anomalous:
            triggers.append("behavior_anomaly")
        if schedule and len(feature_summary) >= 2:
            triggers.append("multi_source_context")

        context = {
            "evaluated_at": now,
            "timezone": self.config.timezone,
            "baseline": {
                "leave_probability": behavior.get("leave_probability"),
                "expected_departure_at": behavior.get("expected_departure_at"),
                "motion_state": effective_motion_state,
                "confidence": behavior.get("confidence"),
                "samples": behavior.get("samples"),
                "suppress_weather_query": behavior.get("suppress_weather_query"),
                "current_place": self._bounded_semantic_value(current_place or {}),
            },
            "candidate_place_ids": [item["place_id"] for item in candidates],
            "candidates": candidates,
            "schedule": schedule,
            "context_features": feature_summary,
        }
        return context, triggers

    @staticmethod
    def _optional_epoch(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return _epoch(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _bounded_semantic_value(cls, value: Any, depth: int = 0) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:240]
        if depth >= 2:
            return None
        if isinstance(value, Mapping):
            return {
                str(key)[:64]: cls._bounded_semantic_value(item, depth + 1)
                for key, item in list(value.items())[:12]
            }
        if isinstance(value, (list, tuple)):
            return [cls._bounded_semantic_value(item, depth + 1) for item in value[:8]]
        return str(value)[:240]

    @staticmethod
    def _normalize_semantic_adjustment(raw: Mapping[str, Any]) -> dict[str, Any]:
        probability = raw.get("leave_probability")
        try:
            probability = None if probability is None else max(0.0, min(1.0, float(probability)))
        except (TypeError, ValueError):
            probability = None
        departure = raw.get("expected_departure_at")
        try:
            departure = None if departure is None else _epoch(departure)
        except (TypeError, ValueError):
            departure = None
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        raw_tags = raw.get("tags")
        tags = raw_tags if isinstance(raw_tags, (list, tuple)) else []
        return {
            "should_adjust": bool(raw.get("should_adjust")),
            "leave_probability": probability,
            "expected_departure_at": departure,
            "destination_place_id": str(raw.get("destination_place_id") or "")[:256],
            "confidence": confidence,
            "anomaly": bool(raw.get("anomaly")),
            "tags": [str(item)[:64] for item in tags[:8]],
        }

    def _apply_semantic_adjustment(
        self,
        behavior: dict[str, Any],
        adjustment: Mapping[str, Any],
        now: int,
    ) -> bool:
        if not adjustment.get("should_adjust"):
            return False
        confidence = float(adjustment.get("confidence") or 0.0)
        if confidence < self.config.semantic.minimum_adjustment_confidence:
            return False
        applied = False
        probability = adjustment.get("leave_probability")
        if probability is not None:
            baseline = float(behavior.get("leave_probability") or 0.0)
            bounded = max(baseline - 0.25, min(baseline + 0.25, float(probability)))
            behavior["leave_probability"] = round(max(0.0, min(1.0, bounded)), 3)
            behavior["likely_to_leave"] = behavior["leave_probability"] >= 0.6
            applied = True
        departure = adjustment.get("expected_departure_at")
        if departure is not None and now - 5 * 60 <= int(departure) <= now + 24 * 3600:
            behavior["expected_departure_at"] = int(departure)
            applied = True
        destination = str(adjustment.get("destination_place_id") or "")
        if destination:
            for key in ("destination_candidates", "frequent_destinations"):
                items = list(behavior.get(key) or [])
                index = next(
                    (idx for idx, item in enumerate(items) if item.get("place_id") == destination),
                    None,
                )
                if index is not None:
                    behavior[key] = [items[index], *items[:index], *items[index + 1:]]
                    applied = True
        if applied and behavior.get("leave_probability", 0.0) >= 0.6:
            behavior["suppress_weather_query"] = False
        return applied

    def deliver_pending_notifications(
        self,
        limit: int = 100,
        *,
        now: Any = None,
    ) -> list[dict[str, Any]]:
        """Retry the durable weather outbox independently of model evaluation."""

        try:
            from hermes_cli.dashboard_auth.mobile_notifications import deliver_account_notification_push
        except Exception:
            logger.exception("APNs delivery module is unavailable")
            return []
        outcomes: list[dict[str, Any]] = []
        instant = _epoch(now) if now is not None else _epoch(self.clock())
        remaining = max(1, min(int(limit), 1000))
        processed_ids: set[str] = set()
        while remaining > 0:
            claim_now = instant if now is not None else _epoch(self.clock())
            claimed = self.store.claim_pending_notifications(
                1,
                now=claim_now,
                exclude_ids=list(processed_ids),
            )
            if not claimed:
                break
            item = claimed[0]
            remaining -= 1
            processed_ids.add(str(item["id"]))
            payload = dict(item.get("payload") or {})
            notification_id = str(item["id"])
            lease_token = str(item.get("lease_token") or "")
            previous_deliveries = dict(item.get("device_deliveries") or {})

            def persist_progress(deliveries: dict[str, dict[str, Any]]) -> None:
                renewed = self.store.update_notification_device_deliveries(
                    notification_id,
                    deliveries,
                    lease_token=lease_token,
                    now=self.clock(),
                )
                if not renewed:
                    raise RuntimeError("notification delivery lease lost")

            try:
                outcome = deliver_account_notification_push(
                    owner_id=str(item["owner_id"]),
                    notification_id=notification_id,
                    title=str(payload.get("title") or "Hermes"),
                    body=str(payload.get("body") or payload.get("summary") or ""),
                    category=str(payload.get("category") or "smart-weather"),
                    deep_link=str(payload.get("deep_link") or "hermes-agent://weather"),
                    data={
                        key: value
                        for key, value in payload.items()
                        if key in {"event_type", "valid_from", "valid_until", "severity"}
                    },
                    previous_deliveries=previous_deliveries,
                    progress_callback=persist_progress,
                )
                state = str(outcome.get("state") or "retry")
                stored_state = (
                    "delivered" if state == "delivered"
                    else "failed" if state == "permanent_failure"
                    else "retry"
                )
                self.store.update_notification_delivery(
                    notification_id,
                    stored_state,
                    int(item.get("deliveries") or 0) + 1,
                    str(outcome.get("error") or ""),
                    dict(outcome.get("deliveries") or previous_deliveries),
                    lease_token=lease_token,
                    now=self.clock(),
                )
                outcomes.append({"id": item["id"], **outcome})
            except Exception as exc:
                self.store.update_notification_delivery(
                    notification_id,
                    "retry",
                    int(item.get("deliveries") or 0) + 1,
                    str(exc),
                    lease_token=lease_token,
                    now=self.clock(),
                )
                outcomes.append({"id": item["id"], "state": "retry", "error": str(exc)})
        return outcomes
    process_account = evaluate_account
    run_account = evaluate_account

    def run_once(self, now: Any = None, force: bool = False) -> list[dict[str, Any]]:
        return self.evaluate_all(now=now, force=force)

    tick = run_once
    evaluate_due_accounts = run_once

    def _choose_destination(self, owner_id: str, behavior: Mapping[str, Any]) -> dict[str, Any] | None:
        current = behavior.get("current_place") or {}
        current_id = current.get("place_id")
        for key in ("destination_candidates", "frequent_destinations"):
            for candidate in behavior.get(key, []) or []:
                if candidate.get("place_id") and candidate.get("place_id") != current_id:
                    place = self.store.get_place(owner_id, candidate["place_id"])
                    return place or dict(candidate)
        places = [place for place in self.store.list_places(owner_id, limit=20) if place.get("place_id") != current_id]
        return places[0] if places else None

    def _ensure_fixed_study_place(self, owner_id: str, observed_at: int) -> None:
        existing = self.store.get_place(owner_id, FIXED_STUDY_PLACE_ID)
        if (
            existing
            and existing.get("latitude") is not None
            and existing.get("longitude") is not None
        ):
            return
        latitude = longitude = None
        if self.amap is not None:
            try:
                search = getattr(self.amap, "search_poi", None)
                if callable(search):
                    result = search(FIXED_STUDY_PLACE_NAME, city="泉州")
                    pois = result.get("pois") or []
                    location = str(pois[0].get("location") or "") if pois else ""
                    if "," in location:
                        longitude_text, latitude_text = location.split(",", 1)
                        longitude, latitude = float(longitude_text), float(latitude_text)
            except Exception:
                logger.debug("fixed study-room POI resolution unavailable", exc_info=True)
        if existing and (latitude is None or longitude is None):
            # Keep retrying resolution on later evaluations instead of leaving
            # a permanent coordinate-less sentinel after one transient outage.
            return
        self.store.learn_place(
            owner_id,
            FIXED_STUDY_PLACE_ID,
            name=FIXED_STUDY_PLACE_NAME,
            latitude=latitude,
            longitude=longitude,
            arrived_at=observed_at,
            metadata={"fixed": True, "kind": "study-room"},
        )

    def _estimate_route(self, owner_id: str, behavior: Mapping[str, Any], destination: Mapping[str, Any]) -> RouteEstimate:
        current = behavior.get("current_place") or {}
        effective_motion_state = (
            behavior.get("effective_motion_state")
            if "effective_motion_state" in behavior
            else behavior.get("motion_state")
        )
        origin_id = str(current.get("place_id") or "current")
        destination_id = str(destination.get("place_id") or "destination")
        routes = self.store.list_routes(owner_id, origin_id, limit=20)
        destination_routes = [
            item for item in routes if item.get("destination_place_id") == destination_id
        ]
        if destination_routes:
            preferred_mode = self._normalize_route_mode(effective_motion_state)
            route = (
                next(
                    (item for item in destination_routes if item.get("mode") == preferred_mode),
                    None,
                )
                if preferred_mode
                else None
            ) or destination_routes[0]
            return RouteEstimate(
                mode=str(route.get("mode") or "walking"),
                duration_seconds=max(60, int(route.get("average_duration_seconds") or 1200)),
                distance_meters=float(route.get("average_distance_meters") or 0),
                outdoor_minutes=float(route.get("average_outdoor_minutes") or 20),
                source="history",
                waypoints=self._normalize_waypoints((route.get("metadata") or {}).get("waypoints")),
            )
        if self.amap and current.get("latitude") is not None and destination.get("latitude") is not None:
            try:
                origin = f"{float(current['longitude']):.6f},{float(current['latitude']):.6f}"
                target = f"{float(destination['longitude']):.6f},{float(destination['latitude']):.6f}"
                preferred_mode = self._normalize_route_mode(effective_motion_state) or "walking"
                request_mode = "walking" if preferred_mode == "running" else preferred_mode
                route_data = self.amap.route(origin, target, mode=request_mode)
                route_payload = route_data.get("route")
                data_payload = route_data.get("data")
                route_payload = route_payload if isinstance(route_payload, Mapping) else {}
                data_payload = data_payload if isinstance(data_payload, Mapping) else {}
                paths = (
                    route_payload.get("paths")
                    or data_payload.get("paths")
                    or route_payload.get("transits")
                    or []
                )
                item = paths[0] if isinstance(paths, list) and paths else {}
                if not isinstance(item, Mapping):
                    item = {}
                duration = max(60, int(float(item.get("duration") or 1200)))
                distance = float(item.get("distance") or 0)
                waypoints = self._amap_waypoints(item)
                # A route plan is a prediction, not a training label. Only
                # _learn_graph writes completed, trajectory-backed trips into
                # the durable route graph after the user actually travels.
                return RouteEstimate(
                    preferred_mode,
                    duration,
                    distance,
                    duration / 60.0,
                    "amap",
                    waypoints,
                )
            except Exception:
                logger.debug("AMap route lookup unavailable", exc_info=True)
        return RouteEstimate()

    @staticmethod
    def _normalize_waypoints(value: Any) -> tuple[tuple[float, float], ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        result: list[tuple[float, float]] = []
        for item in value[:5]:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    result.append((float(item[0]), float(item[1])))
                except (TypeError, ValueError):
                    continue
        return tuple(result)

    @classmethod
    def _amap_waypoints(cls, path: Mapping[str, Any]) -> tuple[tuple[float, float], ...]:
        coordinates: list[tuple[float, float]] = []
        for step in path.get("steps") or []:
            if not isinstance(step, Mapping):
                continue
            for pair in str(step.get("polyline") or "").split(";"):
                if "," not in pair:
                    continue
                longitude, latitude = pair.split(",", 1)
                try:
                    point = (float(latitude), float(longitude))
                except ValueError:
                    continue
                if not coordinates or point != coordinates[-1]:
                    coordinates.append(point)
        if len(coordinates) <= 2:
            return tuple(coordinates)
        indexes = sorted({len(coordinates) // 4, len(coordinates) // 2, 3 * len(coordinates) // 4})
        return tuple(coordinates[index] for index in indexes)

    def _weather_window(self, departure: int, route: RouteEstimate) -> WeatherWindow:
        duration = max(60, int(route.duration_seconds))
        buffer_seconds = max(0, int(self.config.weather.route_buffer_minutes)) * 60
        starts = int(departure) - 10 * 60
        ends = int(departure) + duration + buffer_seconds
        max_end = starts + max(15, int(self.config.weather.maximum_window_minutes)) * 60
        ends = min(ends, max_end)
        if ends <= starts:
            ends = starts + 15 * 60
        return WeatherWindow(starts, ends, int(departure), int(departure) + duration, duration)

    build_weather_window = _weather_window

    def _query_weather(
        self,
        owner_id: str,
        destination: Mapping[str, Any],
        window: WeatherWindow,
        route: RouteEstimate,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"queried": False, "locations": [], "events": []}
        if not self.qweather:
            return result
        quota = self.store.weather_quota_status()
        if quota.get("exhausted"):
            logger.warning("QWeather hard monthly limit reached; evaluation skipped")
            return result
        soft_throttled = bool(quota.get("soft_limited"))
        if soft_throttled:
            logger.info("QWeather soft monthly limit reached; reducing evaluation requests")
        # The current location is queried through the latest location snapshot;
        # destination and route waypoints are queried when coordinates exist.
        locations: list[tuple[float, float, str]] = []
        current = self.store.latest_snapshot(owner_id, "location")
        if current:
            data = current.get("data", {})
            if data.get("latitude") is not None and data.get("longitude") is not None:
                locations.append((float(data["latitude"]), float(data["longitude"]), "origin"))
        if destination.get("latitude") is not None and destination.get("longitude") is not None:
            locations.append((float(destination["latitude"]), float(destination["longitude"]), "destination"))
        for index, (latitude, longitude) in enumerate(route.waypoints, start=1):
            locations.append((latitude, longitude, f"route-{index}"))
        if soft_throttled:
            locations = locations[:1]
        # Avoid duplicate API calls for the same coordinate while retaining the
        # semantic location labels in the result.
        seen: set[tuple[float, float]] = set()
        for latitude, longitude, label in locations:
            key = (round(latitude, 4), round(longitude, 4))
            if key in seen:
                continue
            seen.add(key)
            try:
                minute = self.qweather.minutely(latitude, longitude)
                hourly = {} if soft_throttled else self.qweather.hourly(latitude, longitude, 24)
                warning = self.qweather.warnings(latitude, longitude)
                current_weather = None
                current_method = getattr(self.qweather, "current", None)
                if callable(current_method) and not soft_throttled:
                    current_weather = current_method(latitude, longitude)
                result["queried"] = True
                result["locations"].append({"label": label, "latitude": latitude, "longitude": longitude,
                                             "minutely": minute, "hourly": hourly, "warnings": warning, "current": current_weather})
                result["events"].extend(self._extract_events(minute, hourly, warning, window, current_weather))
                if isinstance(minute.get("events"), list):
                    result["events"].extend(dict(item) for item in minute["events"] if isinstance(item, Mapping))
            except Exception as exc:
                result.setdefault("errors", []).append(str(exc)[:300])
        if result["queried"]:
            condition = self._weather_condition(result["events"])
            context_payload = {
                "condition": condition,
                "event_types": sorted({
                    str(item.get("type") or "weather")
                    for item in result["events"]
                    if isinstance(item, Mapping)
                }),
                "route_mode": route.mode,
                "window": window.as_dict(),
            }
            context_hash = hashlib.sha256(
                f"{owner_id}:{window.departure_at}:{condition}".encode()
            ).hexdigest()[:24]
            self.store.record_behavior_feedback(
                owner_id,
                "weather-context",
                context_payload,
                observed_at=window.departure_at,
                feedback_id=f"weather-context:{context_hash}",
            )
        return result

    query_weather = _query_weather

    @staticmethod
    def _weather_condition(events: list[Mapping[str, Any]]) -> str:
        kinds = {str(item.get("type") or "").lower() for item in events}
        if "warning" in kinds:
            return "severe"
        if "precipitation" in kinds:
            return "wet"
        if "heat" in kinds:
            return "hot"
        if "cold" in kinds:
            return "cold"
        return "clear"

    @staticmethod
    def _extract_events(minute: Mapping[str, Any], hourly: Mapping[str, Any], warning: Mapping[str, Any], window: WeatherWindow, current: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        summary_text = str(minute.get("summary") or minute.get("text") or "").lower()
        if any(word in summary_text for word in ("rain", "storm", "snow", "hail", "雨", "雪", "雷")):
            events.append({"type": "precipitation", "at": window.starts_at, "precip": 1.0, "text": summary_text})
        now_data = current.get("now", current) if isinstance(current, Mapping) else {}
        temperature_value = now_data.get("temp")
        try:
            temperature = float(temperature_value) if temperature_value is not None else None
        except (TypeError, ValueError):
            temperature = None
        if temperature is not None and (temperature >= 35 or temperature <= 3):
            events.append({"type": "heat" if temperature >= 35 else "cold", "at": window.starts_at, "temperature": temperature, "text": "temperature risk"})
        for item in minute.get("minutely", []) if isinstance(minute.get("minutely"), list) else []:
            if not isinstance(item, Mapping):
                continue
            at = item.get("fxTime") or item.get("time")
            try:
                timestamp = _epoch(at)
            except Exception:
                continue
            if timestamp < window.starts_at - 300 or timestamp > window.ends_at + 300:
                continue
            precip = float(item.get("precip") or item.get("precipitation") or 0)
            text = str(item.get("type") or item.get("text") or item.get("summary") or "").lower()
            if precip > 0 or any(word in text for word in ("rain", "shower", "storm", "snow", "hail", "雨", "雪", "雷")):
                events.append({"type": "precipitation", "at": timestamp, "precip": precip, "text": text})
        warning_items = warning.get("warning", [])
        if isinstance(warning_items, Mapping):
            warning_items = [warning_items]
        for item in warning_items if isinstance(warning_items, list) else []:
            if isinstance(item, Mapping):
                events.append({"type": "warning", "at": window.starts_at, "text": str(item.get("title") or item.get("typeName") or "weather warning")})
        for item in hourly.get("hourly", []) if isinstance(hourly.get("hourly"), list) else []:
            if not isinstance(item, Mapping):
                continue
            text = str(item.get("textDay") or item.get("text") or "").lower()
            try:
                at = _epoch(item.get("fxTime"))
            except Exception:
                continue
            if window.starts_at <= at <= window.ends_at and any(word in text for word in ("rain", "storm", "snow", "hail", "雨", "雪", "雷")):
                events.append({"type": "precipitation", "at": at, "text": text, "precip": float(item.get("precip") or 0)})
            try:
                temperature = float(item.get("temp"))
            except (TypeError, ValueError):
                temperature = None
            if window.starts_at <= at <= window.ends_at and temperature is not None and (temperature >= 35 or temperature <= 3):
                events.append({"type": "heat" if temperature >= 35 else "cold", "at": at, "temperature": temperature, "text": "temperature risk"})
        return events

    def _weather_decision(self, forecast: Mapping[str, Any], window: WeatherWindow) -> dict[str, Any]:
        events = list(forecast.get("events") or [])
        score = 0.0
        kinds: list[str] = []
        for event in events:
            event_type = str(event.get("type") or "weather")
            kinds.append(event_type)
            text = str(event.get("text") or "").lower()
            if event_type == "warning" or any(word in text for word in ("typhoon", "thunder", "storm", "台风", "强对流")):
                score = max(score, 1.0)
            elif event_type == "precipitation":
                score = max(score, 0.8 if float(event.get("precip") or 0) >= 0.5 else 0.65)
            else:
                score = max(score, 0.55)
        return {
            "should_notify": score >= self.config.weather.minimum_alert_score,
            "score": round(score, 3),
            "threshold": self.config.weather.minimum_alert_score,
            "event_types": sorted(set(kinds)),
            "reason": "weather_event" if score >= self.config.weather.minimum_alert_score else "below_threshold",
        }

    decide_weather = _weather_decision

    def _expire_stale_weather(
        self,
        owner_id: str,
        now: int,
        *,
        keep_event_key: str = "",
    ) -> dict[str, Any]:
        reconcile = getattr(self.store, "expire_pending_weather_notifications", None)
        if not callable(reconcile):
            return {"expired": 0, "forecasts_removed": 0}
        return reconcile(owner_id, now=now, keep_event_key=keep_event_key)

    def _enqueue_weather_notification(self, owner_id: str, destination: Mapping[str, Any], route: RouteEstimate, window: WeatherWindow, decision: Mapping[str, Any], now: int) -> dict[str, Any]:
        event_types = list(decision.get("event_types") or ["weather"])
        events = ", ".join(event_types)
        event_labels = {
            "precipitation": "降雨",
            "warning": "灾害预警",
            "heat": "高温",
            "cold": "低温",
            "weather": "天气变化",
        }
        display_events = "、".join(event_labels.get(item, item) for item in event_types)
        title = "出行天气提醒"
        timezone = ZoneInfo(self.config.timezone)
        impact_start = datetime.fromtimestamp(window.starts_at, timezone)
        impact_end = datetime.fromtimestamp(window.ends_at, timezone)
        body = f"{impact_start:%H:%M}-{impact_end:%H:%M} 可能有{display_events}。建议提前查看天气并准备相应装备。"
        dedupe_seconds = max(60, int(self.config.weather.dedupe_window_seconds))
        destination_id = str(destination.get("place_id") or destination.get("name") or "destination")
        event_hash = hashlib.sha256(
            f"{owner_id}:{destination_id}:{events}".encode()
        ).hexdigest()[:24]
        expires = window.arrival_at
        payload = {
            "category": "smart-weather",
            "title": title,
            "body": body,
            "event_type": events,
            "event_key": event_hash,
            "valid_from": window.starts_at,
            "valid_until": expires,
            "impact_until": window.ends_at,
            "suggestion": "请根据天气调整出行装备或路线。",
        }
        if expires <= now:
            self._expire_stale_weather(owner_id, now)
            return {"id": "", "state": "expired", "duplicate": False, "reason": "travel_window_elapsed"}
        delivery_not_before = max(now, window.starts_at - 15 * 60)
        delivery_not_before = self._next_non_quiet_at(delivery_not_before)
        if delivery_not_before >= expires:
            self._expire_stale_weather(owner_id, now)
            return {"id": "", "state": "expired", "duplicate": False, "reason": "quiet_window_elapsed"}
        self._expire_stale_weather(owner_id, now, keep_event_key=event_hash)
        result = self.store.enqueue_notification(
            owner_id,
            payload,
            idempotency_key=f"weather:{event_hash}:{now}",
            expires_at=expires,
            not_before=delivery_not_before,
            now=now,
            dedupe_key=f"weather:{event_hash}",
            dedupe_window_seconds=dedupe_seconds,
        )
        if self.notifier and not result.get("duplicate") and delivery_not_before <= now:
            try:
                self.notifier(payload)
            except Exception:
                logger.exception("weather notification callback failed")
        if not result.get("duplicate") or result.get("state") in {"pending", "retry"}:
            # Keep the map's active-alert surface and APNs outbox tied to the
            # same idempotent weather event. Delivery remains best-effort: an
            # absent APNs credential leaves the durable outbox pending.
            self.store.record_active_forecast(owner_id, {
                **payload,
                "id": result.get("id") or f"weather:{event_hash}",
                "valid_from": window.starts_at,
                "valid_until": expires,
            })
            # Delivery is intentionally left to the durable outbox worker.
            # Sending here would bypass not_before for future travel windows.
        return result

    def _next_non_quiet_at(self, timestamp: int) -> int:
        timezone = ZoneInfo(self.config.timezone)
        local = datetime.fromtimestamp(timestamp, timezone)
        if not self._is_quiet(local):
            return timestamp
        start = int(self.config.weather.quiet_start_hour)
        end = int(self.config.weather.quiet_end_hour)
        if start > end and local.hour >= start:
            target_date = local.date() + timedelta(days=1)
        else:
            target_date = local.date()
        target = datetime.combine(target_date, datetime.min.time(), timezone).replace(hour=end)
        return max(timestamp, int(target.timestamp()))

    def _is_quiet(self, local: datetime) -> bool:
        start = self.config.weather.quiet_start_hour
        end = self.config.weather.quiet_end_hour
        if start == end:
            return False
        return local.hour >= start or local.hour < end if start > end else start <= local.hour < end

    def _save_quiet_summary(
        self,
        owner_id: str,
        destination: Mapping[str, Any],
        route: RouteEstimate,
        window: WeatherWindow,
        now: int,
        decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        local_date = datetime.fromtimestamp(window.departure_at, ZoneInfo(self.config.timezone)).date().isoformat()
        payload = {
            "destination": {
                "name": destination.get("name"),
                "place_id": destination.get("place_id"),
            },
            "route": route.__dict__,
            "window": window.as_dict(),
            "risk": {
                "event_types": list(decision.get("event_types") or []),
                "score": float(decision.get("score") or 0.0),
            },
            "captured_at": now,
        }
        return self.store.save_quiet_summary(owner_id, local_date, payload)

    def _flush_quiet_summary(self, owner_id: str, local: datetime, now: int, result: dict[str, Any]) -> None:
        # At 08:00, deliver a summary accumulated in the previous quiet period.
        result.setdefault("quiet_summary_notification", None)
        if self._is_quiet(local):
            return
        candidates = [(local.date() - timedelta(days=1)).isoformat(), local.date().isoformat()]
        summary = None
        summary_date = ""
        for candidate in candidates:
            item = self.store.get_quiet_summary(owner_id, candidate)
            if item:
                payload = dict(item.get("payload") or {})
                window = payload.get("window") or {}
                if int(window.get("arrival_at") or window.get("ends_at") or 0) > now:
                    summary, summary_date = item, candidate
                    break
                self.store.mark_quiet_summary_delivered(owner_id, candidate)
        if not summary:
            return
        payload = dict(summary.get("payload") or {})
        window = payload.get("window") or {}
        valid_until = int(window.get("arrival_at") or window.get("ends_at") or now)
        if valid_until <= now:
            self.store.mark_quiet_summary_delivered(owner_id, summary_date)
            return
        key = f"weather:quiet:{owner_id}:{summary_date}"
        summary_title = "今早出行天气汇总"
        summary_body = "夜间天气风险仍可能影响今天的出行，请查看当前天气并准备相应装备。"
        self._expire_stale_weather(owner_id, now)
        notification = self.store.enqueue_notification(owner_id, {
            "category": "smart-weather", "title": summary_title,
            "body": summary_body,
            "event_type": "overnight-summary", "valid_from": now, "valid_until": valid_until,
            "suggestion": "请根据天气调整出行装备或路线。",
        }, idempotency_key=key, expires_at=max(now + 60, valid_until), not_before=now, now=now)
        self.store.mark_quiet_summary_delivered(owner_id, summary_date)
        result["quiet_summary_notification"] = notification
        if not notification.get("duplicate"):
            self.store.record_active_forecast(owner_id, {
                "id": notification.get("id") or key,
                "category": "smart-weather",
                "title": summary_title,
                "body": summary_body,
                "summary": summary_body,
                "valid_from": now,
                "valid_until": valid_until,
            })
            if self.notifier is None:
                # Reuse the durable claim/lease worker even for the immediate
                # 08:00 flush. A direct APNs call here races the scheduler's
                # normal outbox pass and can deliver the same summary twice.
                self.deliver_pending_notifications(now=now)

    def _learn_graph(self, owner_id: str) -> None:
        visits = self.store.list_visit_history(owner_id, limit=1000)
        # Visit history is newest-first; chronological transitions are
        # learned only from completed visits with a bounded inter-visit gap.
        rows = sorted(visits, key=lambda item: item.get("observed_at", 0))
        for previous, current in zip(rows, rows[1:]):
            source = previous.get("data", {})
            destination = current.get("data", {})
            if source.get("place_id") == destination.get("place_id"):
                continue
            departed = source.get("departed_at") or previous.get("observed_at")
            arrived = current.get("arrived_at") or current.get("observed_at")
            try:
                duration = max(0, _epoch(arrived) - _epoch(departed))
            except Exception:
                continue
            if duration > 24 * 3600:
                continue
            trajectory_reader = getattr(self.store, "list_trajectory_between", None)
            try:
                trajectory = (
                    trajectory_reader(owner_id, departed, arrived)
                    if callable(trajectory_reader)
                    else []
                )
            except Exception:
                logger.debug("trajectory window unavailable for route transition", exc_info=True)
                trajectory = []
            mode = self._normalize_route_mode(
                self._predominant_motion(trajectory)
                or destination.get("motion")
                or source.get("motion")
            ) or "unknown"
            origin = self._visit_coordinate(source)
            target = self._visit_coordinate(destination)
            endpoints = tuple(item for item in (origin, target) if item is not None)
            distance = self._trajectory_distance(trajectory, endpoints=endpoints)
            waypoints = self._trajectory_waypoints(trajectory, endpoints=endpoints)
            speeds = [
                float(point["speed"])
                for point in trajectory
                if isinstance(point.get("speed"), (int, float)) and float(point["speed"]) >= 0
            ]
            average_speed = sum(speeds) / len(speeds) if speeds else distance / max(1, duration)
            if mode == "unknown":
                mode = (
                    "driving" if average_speed >= 8.0
                    else "cycling" if average_speed >= 3.0
                    else "walking" if average_speed >= 0.7
                    else "unknown"
                )
            weather_context = self.store.weather_context_for_route(
                owner_id,
                departed,
                arrived,
            )
            self.store.learn_route(
                owner_id,
                str(source.get("place_id") or "unknown"),
                str(destination.get("place_id") or "unknown"),
                mode=mode,
                duration_seconds=duration,
                distance_meters=distance,
                outdoor_minutes=duration / 60.0,
                observed_at=current.get("observed_at"),
                metadata={
                    "waypoints": waypoints,
                    "trajectory_points": len(trajectory),
                    "average_speed_mps": round(max(0.0, average_speed), 3),
                    "source": "trajectory" if trajectory else "place-transition",
                    "time_bucket": self._route_time_bucket(departed),
                    "weather_condition": str(
                        (weather_context or {}).get("condition") or "unknown"
                    ),
                },
                sample_id=f"transition:{previous.get('event_id')}:{current.get('event_id')}",
            )

    def _route_time_bucket(self, value: Any) -> str:
        hour = datetime.fromtimestamp(
            _epoch(value),
            ZoneInfo(self.config.timezone),
        ).hour
        if 7 <= hour < 10:
            return "morning-peak"
        if 17 <= hour < 20:
            return "evening-peak"
        if 10 <= hour < 17:
            return "daytime"
        return "night"

    @staticmethod
    def _visit_coordinate(place: Mapping[str, Any]) -> tuple[float, float] | None:
        nested = place.get("data")
        data: Mapping[str, Any] = nested if isinstance(nested, Mapping) else place
        latitude = data.get("latitude")
        longitude = data.get("longitude")
        if latitude is None or longitude is None:
            return None
        try:
            return float(latitude), float(longitude)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_route_mode(value: Any) -> str:
        mode = str(value or "").strip().lower()
        return {
            "automotive": "driving",
            "car": "driving",
            "bike": "cycling",
            "bicycling": "cycling",
        }.get(mode, mode if mode in {"walking", "running", "cycling", "driving", "transit"} else "")

    @classmethod
    def _predominant_motion(cls, trajectory: list[Mapping[str, Any]]) -> str:
        counts = Counter(
            mode
            for point in trajectory
            if (mode := cls._normalize_route_mode(point.get("motion") or (point.get("data") or {}).get("motion")))
        )
        if counts:
            return counts.most_common(1)[0][0]
        speeds = [
            float(point["speed"])
            for point in trajectory
            if isinstance(point.get("speed"), (int, float)) and float(point["speed"]) >= 0
        ]
        if not speeds:
            return ""
        average = sum(speeds) / len(speeds)
        return (
            "driving" if average >= 8.0
            else "cycling" if average >= 3.0
            else "walking" if average >= 0.7
            else ""
        )

    @staticmethod
    def _trajectory_distance(
        trajectory: list[Mapping[str, Any]],
        *,
        endpoints: tuple[tuple[float, float], ...] = (),
    ) -> float:
        points: list[tuple[float, float]] = list(endpoints[:1])
        for point in trajectory:
            try:
                coordinate = (float(point["latitude"]), float(point["longitude"]))
            except (KeyError, TypeError, ValueError):
                continue
            if not points or coordinate != points[-1]:
                points.append(coordinate)
        if len(endpoints) > 1 and (not points or points[-1] != endpoints[-1]):
            points.append(endpoints[-1])
        distance = 0.0
        for previous, current in zip(points, points[1:]):
            distance += _distance_meters(previous[0], previous[1], current[0], current[1])
        return round(distance, 3)

    @staticmethod
    def _trajectory_waypoints(
        trajectory: list[Mapping[str, Any]],
        *,
        endpoints: tuple[tuple[float, float], ...] = (),
    ) -> tuple[tuple[float, float], ...]:
        points: list[tuple[float, float]] = list(endpoints[:1])
        for point in trajectory:
            try:
                coordinate = (float(point["latitude"]), float(point["longitude"]))
            except (KeyError, TypeError, ValueError):
                continue
            if not points or coordinate != points[-1]:
                points.append(coordinate)
        if len(endpoints) > 1 and (not points or points[-1] != endpoints[-1]):
            points.append(endpoints[-1])
        if len(points) <= 3:
            return tuple(points)
        indexes = sorted({len(points) // 4, len(points) // 2, 3 * len(points) // 4})
        return tuple(points[index] for index in indexes)


WeatherScheduler = IOSIntelligenceScheduler

__all__ = [
    "FIXED_STUDY_PLACE_ID", "FIXED_STUDY_PLACE_NAME", "IOSIntelligenceScheduler",
    "RouteEstimate", "WeatherScheduler", "WeatherWindow",
]
