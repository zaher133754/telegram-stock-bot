from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any


logger = logging.getLogger(__name__)

SUPPORTED_TIMEFRAMES = ("1m", "10m", "1h", "1d", "1w", "1mo")
BASE_NOTIFICATION_TIMEFRAMES = ("1d", "1w", "1mo")
OPTIONAL_NOTIFICATION_TIMEFRAMES = ("1m", "10m", "1h")
TIMEFRAME_ALIASES = {
    "5m": "1m",
    "15m": "10m",
}


@dataclass(frozen=True)
class LastReport:
    text: str
    candle_time: str | None
    created_at: str


@dataclass(frozen=True)
class UserSettings:
    chat_id: int
    user_id: int
    selected_timeframe: str
    notification_timeframes: list[str]
    auto_notifications_enabled: bool
    last_sent_candle_times: dict[str, str] = field(default_factory=dict)
    streaks: dict[str, dict[str, int]] = field(default_factory=dict)
    last_reports: dict[str, LastReport] = field(default_factory=dict)

    @property
    def timeframe(self) -> str:
        return self.selected_timeframe


class UserSettingsStore:
    def __init__(
        self,
        path: Path,
        *,
        default_timeframe: str = "1d",
        default_notification_timeframes: list[str] | tuple[str, ...] | None = None,
        default_auto_notifications: bool = True,
    ) -> None:
        self.path = path
        self.default_timeframe = normalize_supported_timeframe(default_timeframe)
        self.default_notification_timeframes = normalize_notification_timeframes(
            default_notification_timeframes or BASE_NOTIFICATION_TIMEFRAMES
        )
        self.default_auto_notifications = default_auto_notifications
        self._lock = RLock()
        self._data: dict[str, Any] = {"users": {}}
        self._load()

    def ensure_user(self, *, chat_id: int, user_id: int) -> UserSettings:
        with self._lock:
            users = self._users()
            key = str(user_id)
            if key not in users:
                users[key] = self._default_user(chat_id=chat_id, user_id=user_id)
            else:
                users[key]["chat_id"] = chat_id
                users[key]["user_id"] = user_id
                self._fill_missing_user_fields(users[key])
            self._save()
            return self._to_user_settings(users[key])

    def bootstrap_user(self, *, chat_id: int, user_id: int) -> UserSettings:
        return self.ensure_user(chat_id=chat_id, user_id=user_id)

    def get_user(self, user_id: int) -> UserSettings | None:
        with self._lock:
            raw = self._users().get(str(user_id))
            if raw is None:
                return None
            self._fill_missing_user_fields(raw)
            return self._to_user_settings(raw)

    def list_users(self) -> list[UserSettings]:
        with self._lock:
            result: list[UserSettings] = []
            for raw in self._users().values():
                self._fill_missing_user_fields(raw)
                result.append(self._to_user_settings(raw))
            self._save()
            return result

    def reset_user(self, *, user_id: int, chat_id: int) -> UserSettings:
        with self._lock:
            self._users()[str(user_id)] = self._default_user(
                chat_id=chat_id,
                user_id=user_id,
                timeframe="1d",
                notification_timeframes=BASE_NOTIFICATION_TIMEFRAMES,
                auto_notifications_enabled=True,
            )
            self._save()
            return self._to_user_settings(self._users()[str(user_id)])

    def set_timeframe(self, user_id: int, timeframe: str) -> UserSettings:
        with self._lock:
            raw = self._require_user(user_id)
            raw["selected_timeframe"] = normalize_supported_timeframe(timeframe)
            raw.pop("timeframe", None)
            self._save()
            return self._to_user_settings(raw)

    def toggle_notification_timeframe(self, user_id: int, timeframe: str) -> UserSettings:
        normalized_timeframe = normalize_supported_timeframe(timeframe)
        with self._lock:
            raw = self._require_user(user_id)
            current = set(raw["notification_timeframes"])

            if normalized_timeframe in BASE_NOTIFICATION_TIMEFRAMES:
                current.add(normalized_timeframe)
            elif normalized_timeframe in current:
                current.remove(normalized_timeframe)
            else:
                current.add(normalized_timeframe)

            raw["notification_timeframes"] = normalize_notification_timeframes(current)
            self._save()
            return self._to_user_settings(raw)

    def toggle_auto_notifications(self, user_id: int) -> UserSettings:
        with self._lock:
            raw = self._require_user(user_id)
            raw["auto_notifications_enabled"] = not bool(
                raw.get("auto_notifications_enabled", True)
            )
            self._save()
            return self._to_user_settings(raw)

    def save_last_report(
        self,
        *,
        user_id: int,
        timeframe: str,
        text: str,
        candle_time: str | None,
        created_at: str,
    ) -> UserSettings:
        with self._lock:
            raw = self._require_user(user_id)
            reports = raw.setdefault("last_reports", {})
            reports[normalize_supported_timeframe(timeframe)] = {
                "text": text,
                "candle_time": candle_time,
                "created_at": created_at,
            }
            self._save()
            return self._to_user_settings(raw)

    def record_auto_result(
        self,
        *,
        user_id: int,
        timeframe: str,
        candle_time: str,
        matched_tickers: list[str],
    ) -> UserSettings:
        normalized_timeframe = normalize_supported_timeframe(timeframe)
        with self._lock:
            raw = self._require_user(user_id)
            last_sent = raw.setdefault("last_sent_candle_times", {})
            last_sent[normalized_timeframe] = candle_time

            streaks = raw.setdefault("streaks", {})
            current_streaks = normalize_streaks(streaks).get(normalized_timeframe, {})
            streaks[normalized_timeframe] = calculate_next_streaks(
                current_streaks,
                matched_tickers,
            )
            raw["streaks"] = normalize_streaks(streaks)
            self._save()
            return self._to_user_settings(raw)

    def get_last_report(self, user_id: int, timeframe: str) -> LastReport | None:
        with self._lock:
            raw = self._users().get(str(user_id))
            if raw is None:
                return None
            report = raw.get("last_reports", {}).get(normalize_supported_timeframe(timeframe))
            if not isinstance(report, dict):
                return None
            text = str(report.get("text", ""))
            if not text:
                return None
            return LastReport(
                text=text,
                candle_time=report.get("candle_time"),
                created_at=str(report.get("created_at", "")),
            )

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.exception("Failed to read user settings from %s", self.path)
                return
            if isinstance(data, dict):
                self._data = data
                self._data.setdefault("users", {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _users(self) -> dict[str, Any]:
        users = self._data.setdefault("users", {})
        if not isinstance(users, dict):
            self._data["users"] = {}
            users = self._data["users"]
        return users

    def _default_user(
        self,
        *,
        chat_id: int,
        user_id: int,
        timeframe: str | None = None,
        notification_timeframes: list[str] | tuple[str, ...] | None = None,
        auto_notifications_enabled: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "chat_id": chat_id,
            "user_id": user_id,
            "selected_timeframe": normalize_supported_timeframe(
                timeframe or self.default_timeframe
            ),
            "notification_timeframes": normalize_notification_timeframes(
                notification_timeframes or self.default_notification_timeframes
            ),
            "auto_notifications_enabled": (
                self.default_auto_notifications
                if auto_notifications_enabled is None
                else auto_notifications_enabled
            ),
            "last_sent_candle_times": {},
            "streaks": {},
            "last_reports": {},
        }

    def _fill_missing_user_fields(self, raw: dict[str, Any]) -> None:
        old_timeframe = raw.pop("timeframe", None)
        selected_timeframe = raw.get("selected_timeframe", old_timeframe)
        raw["selected_timeframe"] = normalize_supported_timeframe(
            str(selected_timeframe or self.default_timeframe)
        )

        legacy_notification_timeframe = raw.pop("notification_timeframe", None)
        raw["notification_timeframes"] = normalize_notification_timeframes(
            [
                *self.default_notification_timeframes,
                *to_timeframe_list(raw.get("notification_timeframes", [])),
                *to_timeframe_list(legacy_notification_timeframe),
            ]
        )

        if "auto_notifications_enabled" not in raw:
            legacy_auto_notifications = raw.pop("auto_notifications", None)
            legacy_flags = [
                raw.get("auto_daily_report"),
                raw.get("auto_weekly_report"),
                raw.get("auto_monthly_report"),
            ]
            legacy_flags = [flag for flag in legacy_flags if flag is not None]
            if legacy_auto_notifications is not None:
                raw["auto_notifications_enabled"] = bool(legacy_auto_notifications)
            elif legacy_flags:
                raw["auto_notifications_enabled"] = any(bool(flag) for flag in legacy_flags)
            else:
                raw["auto_notifications_enabled"] = self.default_auto_notifications

        raw["last_sent_candle_times"] = normalize_last_sent_candle_times(
            raw.get("last_sent_candle_times", {})
        )
        legacy_single_candle = raw.pop("last_sent_candle_time", None)
        if legacy_single_candle:
            legacy_timeframe = normalize_supported_timeframe(
                str(legacy_notification_timeframe or self.default_notification_timeframes[0])
            )
            raw["last_sent_candle_times"].setdefault(
                legacy_timeframe,
                str(legacy_single_candle),
            )

        legacy_last_sent = {
            "1d": raw.get("last_sent_daily_candle"),
            "1w": raw.get("last_sent_weekly_candle"),
            "1mo": raw.get("last_sent_monthly_candle"),
        }
        for timeframe, candle_time in legacy_last_sent.items():
            if candle_time:
                raw["last_sent_candle_times"].setdefault(timeframe, str(candle_time))

        raw.pop("auto_daily_report", None)
        raw.pop("auto_weekly_report", None)
        raw.pop("auto_monthly_report", None)
        raw.pop("last_sent_daily_candle", None)
        raw.pop("last_sent_weekly_candle", None)
        raw.pop("last_sent_monthly_candle", None)

        raw["streaks"] = normalize_streaks(raw.get("streaks", {}))
        raw.setdefault("last_reports", {})
        raw["last_reports"] = migrate_last_reports(raw["last_reports"])

    def _require_user(self, user_id: int) -> dict[str, Any]:
        raw = self._users().get(str(user_id))
        if raw is None:
            raise KeyError(f"User settings not found for user_id={user_id}")
        self._fill_missing_user_fields(raw)
        return raw

    @staticmethod
    def _to_user_settings(raw: dict[str, Any]) -> UserSettings:
        reports: dict[str, LastReport] = {}
        for timeframe, report in raw.get("last_reports", {}).items():
            if not isinstance(report, dict):
                continue
            text = str(report.get("text", ""))
            if not text:
                continue
            reports[normalize_supported_timeframe(str(timeframe))] = LastReport(
                text=text,
                candle_time=report.get("candle_time"),
                created_at=str(report.get("created_at", "")),
            )

        return UserSettings(
            chat_id=int(raw["chat_id"]),
            user_id=int(raw["user_id"]),
            selected_timeframe=normalize_supported_timeframe(str(raw["selected_timeframe"])),
            notification_timeframes=normalize_notification_timeframes(
                raw.get("notification_timeframes", [])
            ),
            auto_notifications_enabled=bool(raw["auto_notifications_enabled"]),
            last_sent_candle_times=normalize_last_sent_candle_times(
                raw.get("last_sent_candle_times", {})
            ),
            streaks=normalize_streaks(raw.get("streaks", {})),
            last_reports=reports,
        )


def normalize_timeframe(timeframe: str) -> str:
    return TIMEFRAME_ALIASES.get(timeframe.strip().lower(), timeframe.strip().lower())


def normalize_supported_timeframe(timeframe: str) -> str:
    value = normalize_timeframe(timeframe)
    if value not in SUPPORTED_TIMEFRAMES:
        return "1d"
    return value


def normalize_notification_timeframes(value: Any) -> list[str]:
    requested = {
        normalize_supported_timeframe(timeframe)
        for timeframe in to_timeframe_list(value)
    }
    requested.update(BASE_NOTIFICATION_TIMEFRAMES)
    return [timeframe for timeframe in SUPPORTED_TIMEFRAMES if timeframe in requested]


def to_timeframe_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def normalize_last_sent_candle_times(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, str] = {}
    for timeframe, candle_time in value.items():
        normalized_timeframe = normalize_timeframe(str(timeframe))
        if normalized_timeframe not in SUPPORTED_TIMEFRAMES or not candle_time:
            continue
        normalized[normalized_timeframe] = str(candle_time)
    return normalized


def calculate_next_streaks(
    current_streaks: dict[str, int],
    matched_tickers: list[str],
) -> dict[str, int]:
    result: dict[str, int] = {}
    for ticker in matched_tickers:
        normalized_ticker = ticker.strip().upper()
        if not normalized_ticker:
            continue
        result[normalized_ticker] = int(current_streaks.get(normalized_ticker, 0)) + 1
    return result


def normalize_streaks(value: Any) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, dict[str, int]] = {}
    for timeframe, raw_streaks in value.items():
        normalized_timeframe = normalize_timeframe(str(timeframe))
        if normalized_timeframe not in SUPPORTED_TIMEFRAMES:
            continue
        if not isinstance(raw_streaks, dict):
            continue

        timeframe_streaks: dict[str, int] = {}
        for ticker, count in raw_streaks.items():
            ticker_name = str(ticker).strip().upper()
            if not ticker_name:
                continue
            try:
                normalized_count = int(count)
            except (TypeError, ValueError):
                continue
            if normalized_count > 0:
                timeframe_streaks[ticker_name] = normalized_count
        normalized[normalized_timeframe] = timeframe_streaks
    return normalized


def migrate_last_reports(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}

    migrated: dict[str, Any] = {}
    for timeframe, report in value.items():
        if not isinstance(report, dict):
            continue
        text = str(report.get("text", ""))
        if "Объём" in text:
            continue
        normalized_timeframe = normalize_timeframe(str(timeframe))
        if normalized_timeframe in SUPPORTED_TIMEFRAMES:
            migrated[normalized_timeframe] = report
    return migrated
