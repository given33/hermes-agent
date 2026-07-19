from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import importlib.util
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from hermes_cli.ios_intelligence import IOSIntelligenceStore
from hermes_cli.dashboard_auth.mobile_device_store import MobileDeviceInfo, MobileDeviceStore
from hermes_cli.ios_intelligence_scheduler import (
    CloudBehaviorSemanticAnalyzer,
    IOSIntelligenceScheduler,
    RouteEstimate,
    WeatherWindow,
)
from hermes_cli.ios_intelligence_config import load_ios_intelligence_config
from hermes_cli.ios_mcp_supervisor import IOSMCPSupervisor, MCPState


class FakeWeather:
    def __init__(self, fx_time: str):
        self.fx_time = fx_time
        self.calls: list[str] = []

    def minutely(self, latitude: float, longitude: float):
        self.calls.append("minutely")
        return {"minutely": [{"fxTime": self.fx_time, "precip": 1.0, "text": "rain"}]}

    def hourly(self, latitude: float, longitude: float, hours: int = 24):
        self.calls.append("hourly")
        return {"hourly": []}

    def warnings(self, latitude: float, longitude: float):
        self.calls.append("warnings")
        return {"warning": []}


def test_cloud_semantic_analyzer_uses_structured_host_model_call():
    captured = {}

    class FakeLLM:
        def complete_structured(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                parsed={
                    "should_adjust": False,
                    "leave_probability": None,
                    "expected_departure_at": None,
                    "destination_place_id": "",
                    "confidence": 0.8,
                    "anomaly": False,
                    "tags": ["schedule"],
                },
                provider="fixture-provider",
                model="fixture-model",
            )

    result = CloudBehaviorSemanticAnalyzer(FakeLLM()).analyze(
        {"candidate_place_ids": ["study"], "schedule": []},
        timeout_seconds=9,
    )

    assert captured["schema_name"] == "ios_behavior_semantic_adjustment"
    assert captured["purpose"] == "complex_schedule_anomaly_analysis"
    assert captured["timeout"] == 9.0
    assert captured["json_schema"]["additionalProperties"] is False
    assert result["provider"] == "fixture-provider"
    assert result["model"] == "fixture-model"


def test_complex_schedule_semantics_are_bounded_persisted_and_reused(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    now = 1_800_000_000
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "current-place",
            "kind": "place-visit",
            "observed_at": now - 3600,
            "payload": {
                "place_id": "home",
                "name": "Home",
                "arrived_at": now - 3600,
                "indoor": True,
            },
        }],
        "1",
    )
    store.record_snapshot("alice", "motion", {"state": "walking"}, observed_at=now)
    store.record_snapshot("alice", "power", {"level": 0.42}, observed_at=now)
    store.record_snapshot(
        "alice",
        "health-sleep",
        {"duration_minutes": 410},
        observed_at=now,
    )
    for index, offset in enumerate((1800, 5400), start=1):
        store.record_snapshot(
            "alice",
            "calendar",
            {
                "title": f"Event {index}",
                "location": "Known room",
                "start": now + offset,
                "end": now + offset + 1800,
            },
            observed_at=now,
            event_id=f"calendar-{index}",
        )

    calls = []

    class FakeAnalyzer:
        def analyze(self, context, *, timeout_seconds):
            calls.append((context, timeout_seconds))
            return {
                "should_adjust": True,
                "leave_probability": 0.0,
                "expected_departure_at": now + 1200,
                "destination_place_id": "invented-place",
                "confidence": 0.9,
                "anomaly": True,
                "tags": ["calendar-conflict"],
                "provider": "fixture-provider",
                "model": "fixture-model",
            }

    config = load_ios_intelligence_config({
        "ios_intelligence": {
            "weather": {"enabled": False},
            "semantic": {
                "minimum_interval_seconds": 300,
                "timeout_seconds": 9,
            },
        }
    })
    scheduler = IOSIntelligenceScheduler(
        store=store,
        config=config,
        semantic_analyzer=FakeAnalyzer(),
        supervisor=SimpleNamespace(statuses=lambda: []),
        clock=lambda: now,
    )

    first = scheduler.evaluate_account("alice", now=now)
    second = scheduler.evaluate_account("alice", now=now + 60)

    assert len(calls) == 1
    assert set(calls[0][0]["triggers"]) == {
        "complex_schedule",
        "behavior_anomaly",
        "multi_source_context",
    }
    assert calls[0][1] == 9
    # The semantic layer can move probability by at most 0.25 and cannot add
    # an invented destination to the deterministic candidate graph.
    assert first["behavior"]["leave_probability"] == 0.65
    assert first["behavior"]["expected_departure_at"] == now + 1200
    assert first["behavior"]["destination_candidates"] == []
    assert first["behavior"]["semantic"]["status"] == "applied"
    assert second["behavior"]["semantic"]["status"] == "reused"
    persisted = store.load_model("alice", "semantic-behavior")
    assert persisted["payload"]["provider"] == "fixture-provider"
    assert persisted["payload"]["adjustment"]["anomaly"] is True


def test_disabled_motion_mcp_is_removed_from_behavior_prediction(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    now = 1_800_000_000
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "current-indoor-place",
            "kind": "place-visit",
            "observed_at": now - 600,
            "payload": {
                "place_id": "home",
                "name": "Home",
                "arrived_at": now - 600,
                "indoor": True,
            },
        }],
        "1",
    )
    store.record_snapshot("alice", "motion", {"state": "walking"}, observed_at=now)
    supervisor = SimpleNamespace(
        statuses=lambda: [{"name": "ios-motion", "state": "DISABLED"}],
    )
    scheduler = IOSIntelligenceScheduler(
        store=store,
        config={
            "ios_intelligence": {
                "weather": {"enabled": False},
                "semantic": {"enabled": False},
            },
        },
        supervisor=supervisor,
    )

    result = scheduler.evaluate_account("alice", now=now)
    behavior = result["behavior"]

    assert behavior["motion_state"] == "walking"
    assert behavior["motion_weight"] == 0.0
    assert behavior["effective_motion_state"] is None
    assert behavior["feature_weights"]["ios-motion"] == 0.0
    assert behavior["leave_probability"] == 0.15
    assert behavior["likely_to_leave"] is False
    assert behavior["suppress_weather_query"] is True


def test_disabled_context_mcps_are_removed_from_behavior_prediction(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    now = 1_800_000_000
    store.record_snapshot(
        "alice",
        "calendar",
        {"title": "今天休息", "start": now, "end": now + 3600},
        observed_at=now,
    )
    store.record_snapshot(
        "alice",
        "power",
        {"battery_level": 0.08},
        observed_at=now,
    )
    store.record_snapshot(
        "alice",
        "screen-time",
        {"total_minutes": 300},
        observed_at=now,
    )
    store.record_snapshot(
        "alice",
        "motion",
        {"state": "walking"},
        observed_at=now,
    )
    supervisor = SimpleNamespace(
        statuses=lambda: [
            {"name": "ios-calendar", "state": "DISABLED"},
            {"name": "ios-power", "state": "QUARANTINED"},
            {"name": "ios-screen-time", "state": "DISABLED"},
            {"name": "ios-motion", "state": "RUNNING"},
        ],
    )
    scheduler = IOSIntelligenceScheduler(
        store=store,
        config={
            "ios_intelligence": {
                "weather": {"enabled": False},
                "semantic": {"enabled": False},
            },
        },
        supervisor=supervisor,
    )

    result = scheduler.evaluate_account("alice", now=now)
    behavior = result["behavior"]

    assert behavior["feature_weights"]["ios-calendar"] == 0.0
    assert behavior["feature_weights"]["ios-power"] == 0.2
    assert behavior["feature_weights"]["ios-screen-time"] == 0.0
    assert behavior["calendar_weight"] == 0.0
    assert behavior["calendar_context"]["is_holiday"] is False
    # QUARANTINED is weight 0.2 (>0) so power stays annotated; DISABLED screen-time is excluded.
    assert behavior["context_features"]["power"]["feature_weight"] == 0.2
    assert behavior["context_features"]["power"]["capability"] == "ios-power"
    assert "screen_time" not in behavior["context_features"]
    assert "screen_time" in behavior["excluded_context_features"]
    assert behavior["motion_weight"] == 1.0
    assert behavior["leave_probability"] >= 0.9


def test_cleanup_only_cycle_retries_deletions_without_prediction_or_weather(monkeypatch):
    calls: list[str] = []

    class CleanupStore:
        def retry_account_deletions(self, *, limit):
            calls.append(f"cold:{limit}")
            return [{"owner_id": "alice", "state": "complete"}]

    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.mobile_notifications.process_account_deletion_outbox",
        lambda *, limit: calls.append(f"mobile:{limit}") or [{"state": "complete"}],
    )
    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.owner_mobile.reconcile_deleted_owner_credentials",
        lambda: calls.append("credentials") or {"disabled": True, "config_cleared": True},
    )
    scheduler = IOSIntelligenceScheduler(
        store=CleanupStore(),
        config={"ios_intelligence": {"enabled": False}},
        cleanup_only=True,
    )
    monkeypatch.setattr(
        scheduler,
        "evaluate_all",
        lambda: pytest.fail("cleanup-only scheduler evaluated behavior"),
    )
    monkeypatch.setattr(
        scheduler,
        "deliver_pending_notifications",
        lambda: pytest.fail("cleanup-only scheduler delivered weather notifications"),
    )

    result = scheduler._run_cycle()

    assert result["cleanup_only"] is True
    assert result["account_deletions"]["cold"][0]["state"] == "complete"
    assert result["account_deletions"]["mobile"][0]["state"] == "complete"
    assert result["account_deletions"]["credentials"]["config_cleared"] is True
    assert calls == ["cold:100", "mobile:100", "credentials"]


def test_cleanup_reconciles_mobile_intent_after_cross_database_process_exit(
    tmp_path,
    monkeypatch,
):
    intelligence = IOSIntelligenceStore(tmp_path / "ios-intelligence.db")
    intelligence.begin_account_deletion(
        "alice",
        "https://hermes.example|alice",
    )
    mobile = MobileDeviceStore(tmp_path / "mobile-auth.db")
    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.mobile_device_store.MobileDeviceStore",
        lambda: mobile,
    )
    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.mobile_notifications.process_account_deletion_outbox",
        lambda *, limit: [{"state": mobile.account_deletion_status("alice")["state"]}],
    )
    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.owner_mobile.reconcile_deleted_owner_credentials",
        lambda: {"disabled": True, "config_cleared": True},
    )
    scheduler = IOSIntelligenceScheduler(
        store=intelligence,
        config={"ios_intelligence": {"enabled": False}},
        cleanup_only=True,
    )

    first = scheduler._resume_account_deletions()
    second = scheduler._resume_account_deletions()

    assert first["mobile_intents_reconciled"] == 1
    assert second["mobile_intents_reconciled"] == 0
    status = mobile.account_deletion_status("alice")
    assert status is not None
    assert status["owner_scope"] == "https://hermes.example|alice"


def test_semantic_configuration_is_bounded():
    config = load_ios_intelligence_config({
        "ios_intelligence": {
            "semantic": {
                "minimum_interval_seconds": 1,
                "timeout_seconds": 999,
                "maximum_schedule_items": 500,
                "anomaly_confidence_threshold": -1,
                "minimum_adjustment_confidence": 2,
            }
        }
    })

    assert config.semantic.minimum_interval_seconds == 300
    assert config.semantic.timeout_seconds == 120
    assert config.semantic.maximum_schedule_items == 50
    assert config.semantic.anomaly_confidence_threshold == 0.0
    assert config.semantic.minimum_adjustment_confidence == 1.0


def _visit(event_id: str, place_id: str, arrived: int, departed: int | None, *, indoor: bool = True):
    return {
        "event_id": event_id,
        "kind": "place-visit",
        "observed_at": arrived,
        "payload": {
            "place_id": place_id,
            "name": place_id,
            "latitude": 24.9 if place_id == "home" else 24.91,
            "longitude": 118.6 if place_id == "home" else 118.61,
            "arrived_at": arrived,
            "departed_at": departed,
            "indoor": indoor,
        },
    }


def test_scheduler_builds_route_window_queries_weather_and_persists_alert(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    tz = ZoneInfo("Asia/Shanghai")
    now = int(datetime(2026, 7, 18, 10, 0, tzinfo=tz).timestamp())
    store.ingest_events(
        "alice",
        "iphone",
        [
            _visit("old-home", "home", now - 3 * 86400, now - 3 * 86400 + 1800),
            _visit("old-study", "study", now - 3 * 86400 + 2400, now - 3 * 86400 + 4200),
            _visit("current", "home", now - 900, None),
        ],
        "visits",
    )
    store.record_snapshot("alice", "location", {"latitude": 24.9, "longitude": 118.6}, observed_at=now)
    weather = FakeWeather("2026-07-18T10:10:00+08:00")
    scheduler = IOSIntelligenceScheduler(
        store=store,
        qweather=weather,
        config={"ios_intelligence": {"timezone": "Asia/Shanghai"}},
        clock=lambda: now,
    )

    result = scheduler.evaluate_account("alice", force=True, now=now)

    assert result["weather_queried"] is True
    assert result["weather_window"]["arrival_at"] > result["weather_window"]["departure_at"]
    assert weather.calls == ["minutely", "hourly", "warnings"] * 2
    assert result["notification"]["state"] == "pending"
    forecast = store.active_forecast("alice", now)[0]["data"]
    assert "出发" not in forecast["body"]
    assert "可能有降雨" in forecast["body"]
    assert forecast["event_type"] == "precipitation"


def test_scheduler_suppresses_stationary_indoor_account_without_weather(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    now = 1_784_313_600
    store.ingest_events("alice", "iphone", [_visit("current", "home", now - 60, None)], "1")
    weather = FakeWeather("2026-07-18T10:10:00+08:00")
    scheduler = IOSIntelligenceScheduler(store=store, qweather=weather, clock=lambda: now)

    result = scheduler.evaluate_account("alice", force=False, now=now)

    assert result["suppressed_reason"] in {"low_leave_probability", "indoor_stationary"}
    assert weather.calls == []


def test_scheduler_does_not_query_weather_when_current_place_is_unknown(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    now = 1_784_313_600
    store.record_snapshot(
        "alice",
        "location",
        {"latitude": 24.9, "longitude": 118.6},
        observed_at=now,
    )
    weather = FakeWeather("2026-07-18T10:10:00+08:00")
    scheduler = IOSIntelligenceScheduler(store=store, qweather=weather, clock=lambda: now)

    result = scheduler.evaluate_account("alice", force=False, now=now)

    assert result["behavior"]["current_place"] is None
    assert result["behavior"]["leave_probability"] == 0.15
    assert result["weather_queried"] is False
    assert result["suppressed_reason"] == "low_leave_probability"
    assert weather.calls == []


def test_predicted_departure_command_matches_native_timestamp_contract(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    scheduler = IOSIntelligenceScheduler(store=store)
    now = 1_800_000_000
    result = {}

    scheduler._prepare_predicted_departure(
        "alice",
        {"expected_departure_at": now + 600},
        now,
        result,
    )

    command = store.pull_device_commands("alice", "iphone")["commands"][0]
    assert command["action"] == "set-predicted-departure"
    assert command["payload"] == {"timestamp": now + 600}


def test_route_estimate_never_reuses_history_for_another_destination(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    store.learn_route(
        "alice",
        "home",
        "office",
        mode="walking",
        duration_seconds=9_999,
        distance_meters=12_345,
        outdoor_minutes=99,
    )
    scheduler = IOSIntelligenceScheduler(store=store)

    route = scheduler._estimate_route(
        "alice",
        {"current_place": {"place_id": "home"}},
        {"place_id": "study"},
    )

    assert route.source == "baseline"
    assert route.duration_seconds == 1_200


def test_route_estimate_reads_amap_v4_bicycling_payload(tmp_path):
    class CyclingAMap:
        def route(self, origin, destination, mode):
            assert mode == "cycling"
            return {
                "errcode": 0,
                "data": {
                    "paths": [{
                        "distance": "3456",
                        "duration": "987",
                        "steps": [{"polyline": "118.6000,24.9000;118.6100,24.9100"}],
                    }],
                },
            }

    store = IOSIntelligenceStore(tmp_path)
    scheduler = IOSIntelligenceScheduler(store=store, amap=CyclingAMap())

    route = scheduler._estimate_route(
        "alice",
        {
            "current_place": {
                "place_id": "home",
                "latitude": 24.9,
                "longitude": 118.6,
            },
            "motion_state": "cycling",
        },
        {
            "place_id": "study",
            "latitude": 24.91,
            "longitude": 118.61,
        },
    )

    assert route.source == "amap"
    assert route.mode == "cycling"
    assert route.duration_seconds == 987
    assert route.distance_meters == 3456
    assert route.waypoints == ((24.9, 118.6), (24.91, 118.61))
    assert store.list_routes("alice") == []


def test_weather_notification_dedupe_uses_configured_window(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    scheduler = IOSIntelligenceScheduler(
        store=store,
        config={"ios_intelligence": {"weather": {"dedupe_window_seconds": 600}}},
    )
    # Straddle a fixed bucket boundary to prove this is a sliding window.
    now = 1_800_000_599
    route = RouteEstimate(duration_seconds=1_200)
    window = WeatherWindow(now, now + 3_600, now + 600, now + 1_800, 1_200)
    decision = {"event_types": ["precipitation"]}

    first = scheduler._enqueue_weather_notification(
        "alice", {"place_id": "study", "name": "study"}, route, window, decision, now
    )
    duplicate = scheduler._enqueue_weather_notification(
        "alice", {"place_id": "study", "name": "study"}, route, window, decision, now + 2
    )
    next_window = scheduler._enqueue_weather_notification(
        "alice", {"place_id": "study", "name": "study"}, route, window, decision, now + 600
    )

    assert first["duplicate"] is False
    assert duplicate == {"id": first["id"], "state": "pending", "duplicate": True}
    assert next_window["duplicate"] is False
    assert next_window["id"] != first["id"]


def test_future_weather_notification_does_not_bypass_not_before(tmp_path):
    delivered = []
    store = IOSIntelligenceStore(tmp_path)
    scheduler = IOSIntelligenceScheduler(
        store=store,
        notifier=delivered.append,
    )
    now = 1_800_000_000
    route = RouteEstimate(duration_seconds=1_200)
    window = WeatherWindow(
        now + 3_600,
        now + 7_200,
        now + 4_200,
        now + 5_400,
        1_200,
    )

    result = scheduler._enqueue_weather_notification(
        "alice",
        {"place_id": "study", "name": "study"},
        route,
        window,
        {"event_types": ["precipitation"]},
        now,
    )
    alert_payload = store.active_forecast("alice", now=now)[0]["data"]
    assert "destination_name" not in alert_payload
    assert "destination_place_id" not in alert_payload
    assert "expected_departure_at" not in alert_payload

    assert result["state"] == "pending"
    assert delivered == []
    assert store.pending_notifications(now=now) == []
    assert store.pending_notifications(now=window.starts_at - 900)[0]["id"] == result["id"]


def test_future_weather_notification_waits_until_quiet_hours_end(tmp_path):
    tz = ZoneInfo("Asia/Shanghai")
    now = int(datetime(2026, 7, 18, 22, 0, tzinfo=tz).timestamp())
    quiet_end = int(datetime(2026, 7, 19, 8, 0, tzinfo=tz).timestamp())
    store = IOSIntelligenceStore(tmp_path)
    scheduler = IOSIntelligenceScheduler(store=store)
    window = WeatherWindow(
        quiet_end - 15 * 60,
        quiet_end + 60 * 60,
        quiet_end,
        quiet_end + 45 * 60,
        45 * 60,
    )

    result = scheduler._enqueue_weather_notification(
        "alice",
        {"place_id": "study", "name": "study"},
        RouteEstimate(duration_seconds=45 * 60),
        window,
        {"event_types": ["precipitation"]},
        now,
    )

    assert result["state"] == "pending"
    assert store.pending_notifications(now=quiet_end - 1) == []
    assert store.pending_notifications(now=quiet_end)[0]["id"] == result["id"]


def test_behavior_suppression_retracts_a_stale_future_weather_alert(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    now = int(datetime(2026, 7, 18, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    store.ingest_events("alice", "iphone", [_visit("current", "home", now - 60, None)], "1")
    scheduler = IOSIntelligenceScheduler(store=store, clock=lambda: now)
    window = WeatherWindow(now + 3600, now + 7200, now + 4200, now + 5400, 1200)
    queued = scheduler._enqueue_weather_notification(
        "alice",
        {"place_id": "study", "name": "study"},
        RouteEstimate(duration_seconds=1200),
        window,
        {"event_types": ["precipitation"]},
        now,
    )
    assert queued["state"] == "pending"
    assert store.active_forecast("alice", now)

    result = scheduler.evaluate_account("alice", force=False, now=now + 1)

    assert result["suppressed_reason"] in {"low_leave_probability", "indoor_stationary"}
    assert result["weather_reconciliation"]["expired"] == 1
    assert store.pending_notifications(now=window.starts_at) == []
    assert store.active_forecast("alice", now + 1) == []


def test_scheduler_moves_old_trajectory_to_encrypted_cold_storage(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    old = 946_684_800
    store.ingest_events(
        "alice",
        "iphone",
        [{
            "event_id": "old-point",
            "kind": "location",
            "observed_at": old,
            "payload": {"latitude": 24.9, "longitude": 118.6},
        }],
        "old",
    )
    scheduler = IOSIntelligenceScheduler(
        store=store,
        config={"ios_intelligence": {"storage": {"cold_after_days": 1}}},
    )

    result = scheduler.evaluate_all(now=old + 2 * 86400)[0]

    assert result["cold_archive"]["encrypted"] is True
    assert result["cold_archive"]["hot_points_removed"] == 1
    assert store.list_cold_segments("alice")[0]["encrypted"] == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ios_trajectory WHERE owner_id='alice'"
        ).fetchone()[0] == 0


def test_quiet_hours_save_and_morning_flush_are_idempotent(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    tz = ZoneInfo("Asia/Shanghai")
    quiet_now = int(datetime(2026, 7, 18, 23, 30, tzinfo=tz).timestamp())
    store.ingest_events(
        "alice", "iphone",
        [
            _visit("old-home", "home", quiet_now - 7 * 86400, quiet_now - 7 * 86400 + 10 * 3600),
            _visit("old-study", "study", quiet_now - 7 * 86400 + 2400, quiet_now - 7 * 86400 + 4200),
            _visit("current", "home", quiet_now - 60, None),
        ],
        "1",
    )
    weather = FakeWeather("2026-07-19T09:30:00+08:00")
    scheduler = IOSIntelligenceScheduler(
        store=store,
        qweather=weather,
        config={"ios_intelligence": {"timezone": "Asia/Shanghai"}},
    )
    store.save_quiet_summary(
        "alice",
        "2026-07-18",
        {"window": {"arrival_at": quiet_now + 12 * 3600}},
    )
    quiet_result = scheduler.evaluate_account("alice", force=True, now=quiet_now)
    assert quiet_result["suppressed_reason"] == "quiet_hours"
    assert quiet_result["weather_queried"] is True
    assert weather.calls
    assert store.pending_notifications() == []
    assert store.mark_quiet_summary_delivered("alice", "2026-07-18") is True

    morning = int(datetime(2026, 7, 19, 8, 0, tzinfo=tz).timestamp())
    morning_result = scheduler.evaluate_account("alice", force=False, now=morning)
    assert morning_result["quiet_summary_notification"]["state"] == "pending"
    assert morning_result["notification"] is None
    summary = store.active_forecast("alice", morning)[0]["data"]
    assert summary["title"] == "今早出行天气汇总"
    assert summary["body"].startswith("夜间天气风险")
    assert store.get_quiet_summary("alice", "2026-07-18") is None
    assert store.get_quiet_summary("alice", "2026-07-19") is None
    assert scheduler.evaluate_account("alice", force=False, now=morning).get("quiet_summary_notification") is None


def test_quiet_hours_clear_cached_summary_when_risk_disappears(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    tz = ZoneInfo("Asia/Shanghai")
    now = int(datetime(2026, 7, 18, 23, 30, tzinfo=tz).timestamp())
    store.ingest_events(
        "alice",
        "iphone",
        [
            _visit("old-home", "home", now - 7 * 86400, now - 7 * 86400 + 10 * 3600),
            _visit("old-study", "study", now - 7 * 86400 + 2400, now - 7 * 86400 + 4200),
            _visit("current", "home", now - 60, None),
        ],
        "1",
    )
    weather = FakeWeather("2026-07-19T09:30:00+08:00")
    scheduler = IOSIntelligenceScheduler(store=store, qweather=weather)

    wet = scheduler.evaluate_account("alice", force=True, now=now)
    summary_date = datetime.fromtimestamp(
        wet["weather_window"]["departure_at"],
        tz,
    ).date().isoformat()
    assert store.get_quiet_summary("alice", summary_date) is not None

    weather.fx_time = "2026-07-20T09:30:00+08:00"
    dry = scheduler.evaluate_account("alice", force=True, now=now + 60)

    assert dry["alert"]["should_notify"] is False
    assert store.get_quiet_summary("alice", summary_date) is None


def test_weather_outbox_retry_skips_devices_already_delivered(monkeypatch, tmp_path):
    from hermes_cli.dashboard_auth import mobile_notifications

    store = IOSIntelligenceStore(tmp_path)
    now = 1_800_000_000
    notification = store.enqueue_notification(
        "alice",
        {"title": "天气提醒", "body": "十分钟后有雨", "category": "smart-weather"},
        idempotency_key="weather:partial-delivery",
        expires_at=now + 3600,
        now=now,
    )
    registrations = [
        {"id": "registration-a", "bundle_id": "app.sunstone1029.fig1171"},
        {"id": "registration-b", "bundle_id": "app.sunstone1029.fig1171"},
    ]
    calls: list[str] = []
    retrying = {"value": True}

    class DeviceStore:
        def list_active_apns_registrations(self, *, user_id, environment):
            assert (user_id, environment) == ("alice", "production")
            return registrations

        def disable_apns_registration(self, **_kwargs):
            return True

    def sender(registration, _payload, _collapse_id):
        calls.append(registration["id"])
        if registration["id"] == "registration-b" and retrying["value"]:
            return 503, "Shutdown"
        return 200, ""

    original_delivery = mobile_notifications.deliver_account_notification_push

    def deliver(**kwargs):
        return original_delivery(
            **kwargs,
            device_store=DeviceStore(),
            sender=sender,
        )

    monkeypatch.setenv("HERMES_APNS_BUNDLE_ID", "app.sunstone1029.fig1171")
    monkeypatch.setattr(mobile_notifications, "deliver_account_notification_push", deliver)
    scheduler = IOSIntelligenceScheduler(store=store, clock=lambda: now)

    first = scheduler.deliver_pending_notifications(now=now)

    assert first[0]["state"] == "retry"
    assert calls == ["registration-a", "registration-b"]
    pending = store.pending_notifications(now=now)
    assert pending[0]["id"] == notification["id"]
    assert {item["state"] for item in pending[0]["device_deliveries"].values()} == {
        "delivered",
        "retry",
    }

    calls.clear()
    retrying["value"] = False
    second = scheduler.deliver_pending_notifications(now=now + 1)

    assert second[0]["state"] == "delivered"
    assert calls == ["registration-b"]
    assert store.pending_notifications(now=now + 1) == []


def test_quiet_summary_retry_preserves_per_device_delivery(monkeypatch, tmp_path):
    from hermes_cli.dashboard_auth import mobile_notifications

    store = IOSIntelligenceStore(tmp_path)
    tz = ZoneInfo("Asia/Shanghai")
    now = int(datetime(2026, 7, 19, 8, 0, tzinfo=tz).timestamp())
    store.save_quiet_summary(
        "alice",
        "2026-07-18",
        {"window": {"arrival_at": now + 3600, "ends_at": now + 3600}},
    )
    registrations = [
        {"id": "registration-a", "bundle_id": "app.sunstone1029.fig1171"},
        {"id": "registration-b", "bundle_id": "app.sunstone1029.fig1171"},
    ]
    calls: list[str] = []
    retrying = {"value": True}

    class DeviceStore:
        def list_active_apns_registrations(self, *, user_id, environment):
            assert (user_id, environment) == ("alice", "production")
            return registrations

        def disable_apns_registration(self, **_kwargs):
            return True

    def sender(registration, _payload, _collapse_id):
        calls.append(registration["id"])
        if registration["id"] == "registration-b" and retrying["value"]:
            return 503, "Shutdown"
        return 200, ""

    original_delivery = mobile_notifications.deliver_account_notification_push

    def deliver(**kwargs):
        return original_delivery(
            **kwargs,
            device_store=DeviceStore(),
            sender=sender,
        )

    monkeypatch.setenv("HERMES_APNS_BUNDLE_ID", "app.sunstone1029.fig1171")
    monkeypatch.setattr(mobile_notifications, "deliver_account_notification_push", deliver)
    scheduler = IOSIntelligenceScheduler(store=store, clock=lambda: now)
    result: dict = {}

    scheduler._flush_quiet_summary("alice", datetime.fromtimestamp(now, tz), now, result)

    assert result["quiet_summary_notification"]["duplicate"] is False
    assert calls == ["registration-a", "registration-b"]
    pending = store.pending_notifications(now=now)
    assert len(pending) == 1
    assert {item["state"] for item in pending[0]["device_deliveries"].values()} == {
        "delivered",
        "retry",
    }

    calls.clear()
    retrying["value"] = False
    delivered = scheduler.deliver_pending_notifications(now=now + 1)

    assert delivered[0]["state"] == "delivered"
    assert calls == ["registration-b"]
    assert store.pending_notifications(now=now + 1) == []


def test_concurrent_schedulers_deliver_each_notification_once(monkeypatch, tmp_path):
    from hermes_cli.dashboard_auth import mobile_notifications

    store = IOSIntelligenceStore(tmp_path)
    now = int(datetime.now().timestamp())
    store.enqueue_notification(
        "alice",
        {"title": "Rain", "body": "Bring an umbrella"},
        idempotency_key="concurrent-rain-1",
        expires_at=now + 300,
        now=now,
    )
    calls: list[str] = []
    calls_lock = threading.Lock()

    def deliver(**kwargs):
        with calls_lock:
            calls.append(str(kwargs["notification_id"]))
        time.sleep(0.1)
        return {"state": "delivered", "deliveries": {}}

    monkeypatch.setattr(mobile_notifications, "deliver_account_notification_push", deliver)
    schedulers = [
        IOSIntelligenceScheduler(store=store, clock=lambda: now),
        IOSIntelligenceScheduler(store=store, clock=lambda: now),
    ]
    barrier = threading.Barrier(2)

    def run(scheduler):
        barrier.wait(timeout=5)
        return scheduler.deliver_pending_notifications(now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, schedulers))

    assert sum(len(batch) for batch in outcomes) == 1
    assert len(calls) == 1
    assert store.pending_notifications(now=now) == []


def test_quiet_summary_flush_and_outbox_worker_share_one_delivery_lease(
    monkeypatch, tmp_path
):
    from hermes_cli.dashboard_auth import mobile_notifications

    store = IOSIntelligenceStore(tmp_path)
    timezone = ZoneInfo("Asia/Shanghai")
    now = int(datetime(2026, 7, 19, 8, 0, tzinfo=timezone).timestamp())
    store.save_quiet_summary(
        "alice",
        "2026-07-18",
        {"window": {"arrival_at": now + 3600, "ends_at": now + 3600}},
    )

    enqueued = threading.Event()
    delivery_started = threading.Event()
    release_delivery = threading.Event()
    calls: list[str] = []
    calls_lock = threading.Lock()
    original_enqueue = store.enqueue_notification

    def enqueue(*args, **kwargs):
        result = original_enqueue(*args, **kwargs)
        enqueued.set()
        assert delivery_started.wait(timeout=5)
        return result

    def deliver(**kwargs):
        with calls_lock:
            calls.append(str(kwargs["notification_id"]))
            call_number = len(calls)
        if call_number == 1:
            delivery_started.set()
            assert release_delivery.wait(timeout=5)
        return {"state": "delivered", "deliveries": {}}

    monkeypatch.setattr(store, "enqueue_notification", enqueue)
    monkeypatch.setattr(
        mobile_notifications,
        "deliver_account_notification_push",
        deliver,
    )
    flush_scheduler = IOSIntelligenceScheduler(store=store, clock=lambda: now)
    outbox_scheduler = IOSIntelligenceScheduler(store=store, clock=lambda: now)
    result: dict = {}

    def run_outbox_worker():
        assert enqueued.wait(timeout=5)
        return outbox_scheduler.deliver_pending_notifications(now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker_future = pool.submit(run_outbox_worker)
        flush_future = pool.submit(
            flush_scheduler._flush_quiet_summary,
            "alice",
            datetime.fromtimestamp(now, timezone),
            now,
            result,
        )
        try:
            assert delivery_started.wait(timeout=5)
            flush_future.result(timeout=5)
        finally:
            release_delivery.set()
        worker_future.result(timeout=5)

    assert result["quiet_summary_notification"]["duplicate"] is False
    assert len(calls) == 1
    assert store.pending_notifications(now=now) == []


def test_route_learning_uses_server_resolved_coordinate_places(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    base = 1_700_000_000
    store.ingest_events(
        "alice",
        "iphone",
        [
            {
                "event_id": "visit-a",
                "kind": "place-visit",
                "observed_at": base,
                "payload": {
                    "latitude": 24.9000,
                    "longitude": 118.6000,
                    "arrived_at": base,
                    "departed_at": base + 300,
                },
            },
            {
                "event_id": "visit-b",
                "kind": "place-visit",
                "observed_at": base + 600,
                "payload": {
                    "latitude": 24.9100,
                    "longitude": 118.6100,
                    "arrived_at": base + 600,
                    "departed_at": base + 900,
                },
            },
        ],
        "routes",
    )
    scheduler = IOSIntelligenceScheduler(store=store)

    scheduler._learn_graph("alice")

    routes = store.list_routes("alice")
    assert len(routes) == 1
    assert routes[0]["origin_place_id"].startswith("geo:")
    assert routes[0]["destination_place_id"].startswith("geo:")
    assert routes[0]["origin_place_id"] != routes[0]["destination_place_id"]


def test_route_learning_uses_trajectory_motion_distance_and_waypoints(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    base = 1_700_000_000
    store.ingest_events(
        "alice",
        "iphone",
        [
            {
                "event_id": "visit-a",
                "kind": "place-visit",
                "observed_at": base,
                "payload": {
                    "place_id": "home", "latitude": 24.9000, "longitude": 118.6000,
                    "arrived_at": base, "departed_at": base + 300,
                },
            },
            {
                "event_id": "point-1", "kind": "location", "observed_at": base + 360,
                "payload": {"latitude": 24.9000, "longitude": 118.6000, "motion": "cycling"},
            },
            {
                "event_id": "point-2", "kind": "location", "observed_at": base + 420,
                "payload": {"latitude": 24.9050, "longitude": 118.6050, "motion": "cycling"},
            },
            {
                "event_id": "point-3", "kind": "location", "observed_at": base + 480,
                "payload": {"latitude": 24.9100, "longitude": 118.6100, "motion": "cycling"},
            },
            {
                "event_id": "visit-b", "kind": "place-visit", "observed_at": base + 600,
                "payload": {
                    "place_id": "study", "latitude": 24.9100, "longitude": 118.6100,
                    "arrived_at": base + 600, "departed_at": base + 900,
                },
            },
        ],
        "route-with-motion",
    )

    IOSIntelligenceScheduler(store=store)._learn_graph("alice")
    route = store.list_routes("alice")[0]

    assert route["mode"] == "cycling"
    assert route["average_distance_meters"] > 0
    assert len(route["metadata"]["waypoints"]) == 3


def test_route_learning_keeps_time_and_weather_duration_buckets(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    tz = ZoneInfo("Asia/Shanghai")
    base = int(datetime(2026, 7, 18, 8, 0, tzinfo=tz).timestamp())
    store.record_behavior_feedback(
        "alice",
        "weather-context",
        {"condition": "wet", "event_types": ["precipitation"]},
        observed_at=base + 360,
        feedback_id="weather-route-1",
    )
    store.ingest_events(
        "alice",
        "iphone",
        [
            _visit("home-1", "home", base, base + 300),
            _visit("study-1", "study", base + 900, base + 1_200),
        ],
        "conditioned-route",
    )

    IOSIntelligenceScheduler(store=store)._learn_graph("alice")
    route = store.list_routes("alice")[0]
    context = route["metadata"]["context_stats"]["morning-peak|wet"]

    assert route["mode"] == "walking"
    assert context["trips"] == 1
    assert context["average_duration_seconds"] == 600


def test_route_learning_falls_back_to_place_distance_and_speed_mode(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    base = 1_700_000_000
    store.ingest_events(
        "alice",
        "iphone",
        [
            {
                "event_id": "origin",
                "kind": "place-visit",
                "observed_at": base,
                "payload": {
                    "place_id": "origin", "latitude": 24.9000, "longitude": 118.6000,
                    "arrived_at": base, "departed_at": base + 60,
                },
            },
            {
                "event_id": "sparse-point",
                "kind": "location",
                "observed_at": base + 120,
                "payload": {
                    "latitude": 24.9050, "longitude": 118.6050,
                    "speed": 9.0,
                },
            },
            {
                "event_id": "destination",
                "kind": "place-visit",
                "observed_at": base + 180,
                "payload": {
                    "place_id": "destination", "latitude": 24.9100, "longitude": 118.6100,
                    "arrived_at": base + 180, "departed_at": base + 300,
                },
            },
        ],
        "route-with-speed",
    )

    IOSIntelligenceScheduler(store=store)._learn_graph("alice")

    route = store.list_routes("alice", "origin")[0]
    assert route["mode"] == "driving"
    assert route["average_distance_meters"] > 1


def test_supervisor_clears_degraded_state_after_healthy_probe(tmp_path):
    healthy = False
    supervisor = IOSMCPSupervisor(tmp_path / "health.db", failure_threshold=2)
    supervisor.register("ios-location", health_check=lambda: healthy)
    assert supervisor.health_check("ios-location")["state"] == MCPState.DEGRADED.value

    healthy = True
    recovered = supervisor.health_check("ios-location")

    assert recovered["state"] == MCPState.RUNNING.value
    assert recovered["failures"] == 0
    assert recovered["last_error"] == ""


def test_supervisor_queues_a_new_restart_generation_after_later_crash(tmp_path):
    supervisor = IOSMCPSupervisor(tmp_path / "restart-generation.db", failure_threshold=3)
    supervisor.register("ios-location")

    supervisor.record_failure("ios-location", "first crash")
    first = supervisor.pull()
    assert len(first) == 1
    assert first[0]["action"] == "restart"
    assert supervisor.ack(first[0]["id"]) is True

    with sqlite3.connect(supervisor.path) as conn:
        conn.execute(
            "UPDATE mcp_services SET state='RUNNING',failures=0 WHERE name='ios-location'"
        )
    supervisor.record_failure("ios-location", "later crash")
    second = supervisor.pull()

    assert len(second) == 1
    assert second[0]["id"] != first[0]["id"]


def test_supervisor_recovers_quarantines_and_rolls_back_blue_green(tmp_path):
    supervisor = IOSMCPSupervisor(tmp_path / "supervisor.db", failure_threshold=2)
    supervisor.register("ios-location", version="1.0.0", health_check=lambda: False)
    assert supervisor.health_check("ios-location")["state"] == MCPState.DEGRADED.value
    assert supervisor.health_check("ios-location")["state"] == MCPState.QUARANTINED.value

    supervisor.set_state("ios-location", MCPState.RUNNING)
    result = supervisor.blue_green_upgrade(
        "ios-location",
        "2.0.0",
        start_new=lambda version: None,
        health_check=lambda version: False,
    )
    assert result["rolled_back"] is True
    assert result["active_version"] == "1.0.0"
    assert result["state"] == MCPState.RECOVERING.value

    command = supervisor.enqueue("ios-location", "refresh", idempotency_key="refresh-1")
    duplicate = supervisor.enqueue("ios-location", "refresh", idempotency_key="refresh-1")
    assert duplicate["id"] == command["id"]
    pulled = supervisor.pull()
    pulled_command = next(item for item in pulled if item["id"] == command["id"])
    assert pulled_command["service"] == "ios-location"
    assert supervisor.pull() == []
    with sqlite3.connect(supervisor.path) as conn:
        conn.execute(
            "UPDATE mcp_supervisor_queue SET delivered_at=0 WHERE id=?",
            (command["id"],),
        )
    recovered = supervisor.pull()
    assert recovered[0]["id"] == command["id"]
    assert recovered[0]["attempts"] == 2
    assert supervisor.ack(command["id"]) is True


def test_plugin_lifespan_wires_real_weather_route_and_mcp_runtime(monkeypatch, tmp_path):
    plugin_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "ios-intelligence"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location("test_ios_intelligence_plugin", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    store = type("Store", (), {"path": tmp_path / "ios-intelligence.db"})()
    qweather = object()
    amap = object()
    captured = {}
    runtime_captured = {}

    class FakeScheduler:
        running = False

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    class FakeRuntime:
        running = False
        supervisor = object()

        def __init__(self, **kwargs):
            runtime_captured.update(kwargs)

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    monkeypatch.setattr(module, "intelligence_store", lambda: store)
    monkeypatch.setattr(module, "QWeatherClient", lambda value, **kwargs: qweather if value is store else None)
    monkeypatch.setattr(module, "AMapClient", lambda **kwargs: amap)
    monkeypatch.setattr(module, "IOSIntelligenceScheduler", FakeScheduler)
    monkeypatch.setattr(module, "IOSMCPRuntimeSupervisor", FakeRuntime)

    async def exercise_lifespan():
        async with module.ios_intelligence_lifespan(None):
            assert module._SCHEDULER is not None
            assert module._SCHEDULER.running is True
            assert module._MCP_RUNTIME is not None
            assert module._MCP_RUNTIME.running is True
        assert module._SCHEDULER is None
        assert module._MCP_RUNTIME is None

    import asyncio

    asyncio.run(exercise_lifespan())
    assert captured["store"] is store
    assert captured["qweather"] is qweather
    assert captured["amap"] is amap
    assert captured["supervisor"] is FakeRuntime.supervisor
    assert runtime_captured["db_dir"] == store.path


def test_plugin_lifespan_starts_only_deletion_recovery_when_profile_disabled(monkeypatch, tmp_path):
    plugin_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "ios-intelligence"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location("test_ios_intelligence_disabled_plugin", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    scheduler_state = {}
    runtime_started = []

    store = type("Store", (), {"path": tmp_path / "ios-intelligence.db"})()

    class CleanupScheduler:
        running = False

        def __init__(self, **kwargs):
            scheduler_state.update(kwargs)

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    class ShouldNotStart:
        def __init__(self, **_kwargs):
            runtime_started.append(True)

    monkeypatch.setattr(module, "intelligence_store", lambda: store)
    monkeypatch.setattr(module, "IOSIntelligenceScheduler", CleanupScheduler)
    monkeypatch.setattr(module, "IOSMCPRuntimeSupervisor", ShouldNotStart)
    monkeypatch.setattr(module, "QWeatherClient", ShouldNotStart)
    monkeypatch.setattr(module, "AMapClient", ShouldNotStart)
    monkeypatch.setattr(
        module,
        "load_ios_intelligence_config",
        lambda: load_ios_intelligence_config({"ios_intelligence": {"enabled": False}}),
    )

    import asyncio

    async def exercise():
        async with module.ios_intelligence_lifespan(None):
            assert module._SCHEDULER is not None
            assert module._SCHEDULER.running is True
            assert module._MCP_RUNTIME is None
        assert module._SCHEDULER is None

    asyncio.run(exercise())
    assert scheduler_state["store"] is store
    assert scheduler_state["cleanup_only"] is True
    assert scheduler_state["qweather"] is None
    assert scheduler_state["amap"] is None
    assert scheduler_state["supervisor"] is None
    assert scheduler_state["semantic_analyzer"] is None
    assert runtime_started == []


def test_plugin_relay_policy_limits_event_batches_and_command_retries(monkeypatch):
    plugin_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "ios-intelligence"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location("test_ios_intelligence_relay_plugin", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    policy = load_ios_intelligence_config({
        "ios_intelligence": {
            "relay": {
                "command_lease_seconds": 45,
                "command_max_attempts": 3,
                "require_device_token": False,
                "maximum_event_batch": 1,
            }
        }
    })
    monkeypatch.setattr(module, "load_ios_intelligence_config", lambda: policy)
    monkeypatch.setattr(module, "owner_id_from_request", lambda _request: "alice")

    events = [
        module.ContextEvent(id=f"event-{index}", kind="power", timestamp=1, payload={})
        for index in range(2)
    ]
    with pytest.raises(module.HTTPException) as exc_info:
        module.ingest_event_batch(
            object(),
            module.ContextEventBatch(device_id="iphone", cursor="2", events=events),
        )
    assert exc_info.value.status_code == 422

    captured = {}

    class FakeStore:
        def pull_device_commands(self, owner_id, device_id, **kwargs):
            captured.update({"owner_id": owner_id, "device_id": device_id, **kwargs})
            return {"commands": []}

    monkeypatch.setattr(module, "intelligence_store", lambda: FakeStore())
    module.pull_commands(
        object(),
        module.DeviceCommandPull(device_id="iphone", lease_seconds=600),
    )
    assert captured["lease_seconds"] == 45
    assert captured["max_attempts"] == 3


def test_account_delete_route_always_purges_cold_and_mobile_data(monkeypatch):
    plugin_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "ios-intelligence"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location("test_ios_intelligence_delete_plugin", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls = {}

    class FakeStore:
        def begin_account_deletion(self, owner_id, owner_scope):
            calls["intent"] = (owner_id, owner_scope)
            return {"owner_id": owner_id, "state": "pending"}

        def delete_account(self, owner_id, *, delete_cold):
            calls["store"] = (owner_id, delete_cold)
            return {"owner_id": owner_id}

    class FakeMobileStore:
        def begin_account_deletion(self, owner_id, owner_scope):
            calls["mobile"] = (owner_id, owner_scope)
            return {
                "id": "delete-1",
                "state": "pending",
                "devices": 1,
                "sessions": 1,
                "apns": 1,
            }

    def deliver_cleanup(*, owner_id, device_store, limit):
        calls["cleanup"] = (owner_id, limit, type(device_store).__name__)
        return [{"state": "delivered", "deliveries": {"device-a": {}}}]

    monkeypatch.setattr(module, "intelligence_store", lambda: FakeStore())
    monkeypatch.setattr(module, "MobileDeviceStore", FakeMobileStore)
    monkeypatch.setattr(module, "process_account_deletion_outbox", deliver_cleanup)
    def delete_credentials(owner_id):
        calls["credentials"] = owner_id
        return {"disabled": True, "config_cleared": True}

    monkeypatch.setattr(module, "delete_owner_account_credentials", delete_credentials)
    monkeypatch.setattr(module, "owner_id_from_request", lambda _request: "alice")

    result = module.delete_account(
        object(),
        module.AccountDeleteBody(
            confirm=True,
            owner_scope="https://hermes.example|alice",
        ),
    )

    assert "delete_cold" not in module.AccountDeleteBody.model_fields
    assert calls == {
        "cleanup": ("alice", 1, "FakeMobileStore"),
        "credentials": "alice",
        "intent": ("alice", "https://hermes.example|alice"),
        "store": ("alice", True),
        "mobile": ("alice", "https://hermes.example|alice"),
    }
    assert result["mobile_auth"] == {"devices": 1, "sessions": 1, "apns": 1}
    assert result["device_cleanup"] == {
        "state": "delivered",
        "devices": 1,
        "error": "",
    }
    assert result["accepted"] is True


def test_account_delete_route_keeps_intent_and_returns_pending_when_cleanup_fails(
    tmp_path,
    monkeypatch,
):
    plugin_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "ios-intelligence"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location(
        "test_ios_intelligence_delete_failure_plugin",
        plugin_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    intelligence = IOSIntelligenceStore(tmp_path / "ios-intelligence.db")
    intelligence.ingest_events(
        "alice",
        "iphone",
        [{
            "id": "event-before-delete",
            "kind": "power",
            "timestamp": 1_800_000_000,
            "payload": {"battery_level": 0.5},
        }],
        "event-before-delete",
    )
    mobile = MobileDeviceStore(tmp_path / "mobile-auth.db")
    tokens = mobile.create_session(
        user_id="alice",
        device=MobileDeviceInfo(id="device-primary", name="Alice iPhone"),
    )
    original_delete = intelligence.delete_account
    cleanup_calls = [0]

    def fail_first_cleanup(owner_id, *, delete_cold):
        cleanup_calls[0] += 1
        if cleanup_calls[0] == 1:
            raise sqlite3.OperationalError("fixture write failure")
        return original_delete(owner_id, delete_cold=delete_cold)

    monkeypatch.setattr(intelligence, "delete_account", fail_first_cleanup)
    monkeypatch.setattr(module, "intelligence_store", lambda: intelligence)
    monkeypatch.setattr(module, "MobileDeviceStore", lambda: mobile)
    monkeypatch.setattr(
        module,
        "delete_owner_account_credentials",
        lambda _owner_id: {"disabled": True, "config_cleared": True},
    )
    monkeypatch.setattr(module, "owner_id_from_request", lambda _request: "alice")

    result = module.delete_account(
        object(),
        module.AccountDeleteBody(
            confirm=True,
            owner_scope="https://hermes.example|alice",
        ),
    )

    assert result["accepted"] is True
    assert result["state"] == "pending"
    assert result["error"] == "OperationalError"
    assert intelligence.account_deletion_status("alice")["status"] == "pending"
    assert mobile.verify_access(tokens.access_token, touch=False) is None
    assert mobile.account_deletion_status("alice") is not None

    recovered = intelligence.retry_account_deletions(owner_id="alice")

    assert recovered[0]["state"] == "complete"
    assert intelligence.account_deletion_status("alice")["status"] == "complete"
    with sqlite3.connect(intelligence.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ios_events WHERE owner_id='alice'"
        ).fetchone()[0] == 0


def test_account_delete_route_rejects_a_tombstone_for_another_owner(monkeypatch):
    plugin_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "ios-intelligence"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location(
        "test_ios_intelligence_delete_scope_plugin", plugin_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "owner_id_from_request", lambda _request: "alice")

    with pytest.raises(module.HTTPException) as exc_info:
        module.delete_account(
            object(),
            module.AccountDeleteBody(
                confirm=True,
                owner_scope="https://hermes.example|bob",
            ),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "owner_scope does not match account"
