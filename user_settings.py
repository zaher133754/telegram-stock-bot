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
    selected_tickers: list[str] = field(default_factory=list)
    last_processed_candle_keys: dict[str, str] = field(default_factory=dict)
    last_sent_candle_keys: dict[str, str] = field(default_factory=dict)
    last_auto_report_tickers: dict[str, list[str]] = field(default_factory=dict)
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

    def reset_user(
        self,
        *,
        user_id: int,
        chat_id: int,
        all_tickers: list[str] | tuple[str, ...] | None = None,
    ) -> UserSettings:
        with self._lock:
            self._users()[str(user_id)] = self._default_user(
                chat_id=chat_id,
                user_id=user_id,
                timeframe="1d",
                notification_timeframes=BASE_NOTIFICATION_TIMEFRAMES,
                auto_notifications_enabled=True,
                selected_tickers=all_tickers,
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

    def get_selected_tickers(self, user_id: int) -> list[str]:
        with self._lock:
            raw = self._require_user(user_id)
            return normalize_ticker_list(raw.get("selected_tickers", []))

    def set_selected_tickers(self, user_id: int, tickers: Any) -> UserSettings:
        with self._lock:
            raw = self._require_user(user_id)
            raw["selected_tickers"] = normalize_ticker_list(tickers)
            self._save()
            return self._to_user_settings(raw)

    def toggle_selected_ticker(
        self,
        user_id: int,
        ticker: str,
        all_tickers: Any,
    ) -> UserSettings:
        available_tickers = normalize_ticker_list(all_tickers)
        normalized_ticker = normalize_ticker_list([ticker])
        if not normalized_ticker:
            return self.get_user(user_id) or self._missing_user(user_id)

        ticker_name = normalized_ticker[0]
        if available_tickers and ticker_name not in set(available_tickers):
            return self.get_user(user_id) or self._missing_user(user_id)

        with self._lock:
            raw = self._require_user(user_id)
            self._ensure_selected_tickers_raw(raw, available_tickers)
            selected = set(normalize_ticker_list(raw.get("selected_tickers", [])))
            if ticker_name in selected:
                selected.remove(ticker_name)
            else:
                selected.add(ticker_name)
            raw["selected_tickers"] = order_tickers(selected, available_tickers)
            self._save()
            return self._to_user_settings(raw)

    def select_all_tickers(self, user_id: int, all_tickers: Any) -> UserSettings:
        with self._lock:
            raw = self._require_user(user_id)
            raw["selected_tickers"] = normalize_ticker_list(all_tickers)
            self._save()
            return self._to_user_settings(raw)

    def clear_selected_tickers(self, user_id: int) -> UserSettings:
        with self._lock:
            raw = self._require_user(user_id)
            raw["selected_tickers"] = []
            self._save()
            return self._to_user_settings(raw)

    def ensure_selected_tickers(self, user_id: int, all_tickers: Any) -> UserSettings:
        with self._lock:
            raw = self._require_user(user_id)
            changed = self._ensure_selected_tickers_raw(
                raw,
                normalize_ticker_list(all_tickers),
            )
            if changed:
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
        candle_key: str,
        previous_candle_key: str | None,
        matched_tickers: list[str],
        sent: bool = True,
    ) -> UserSettings:
        normalized_timeframe = normalize_supported_timeframe(timeframe)
        normalized_tickers = normalize_ticker_list(matched_tickers)
        with self._lock:
            raw = self._require_user(user_id)
            last_processed = raw.setdefault("last_processed_candle_keys", {})
            last_processed_candle_key = last_processed.get(normalized_timeframe)
            last_sent = raw.setdefault("last_sent_candle_keys", {})
            last_tickers = normalize_auto_report_tickers(
                raw.setdefault("last_auto_report_tickers", {})
            )
            raw["last_auto_report_tickers"] = last_tickers
            if last_processed_candle_key == candle_key:
                changed = False
                previous_tickers = last_tickers.get(normalized_timeframe, [])
                current_tickers = merge_ticker_lists(
                    previous_tickers,
                    normalized_tickers,
                )
                if current_tickers != previous_tickers:
                    last_tickers[normalized_timeframe] = current_tickers
                    streaks = raw.setdefault("streaks", {})
                    normalized_streaks = normalize_streaks(streaks)
                    current_streaks = dict(
                        normalized_streaks.get(normalized_timeframe, {})
                    )
                    for ticker in current_tickers:
                        current_streaks.setdefault(ticker, 1)
                    normalized_streaks[normalized_timeframe] = {
                        ticker: current_streaks[ticker]
                        for ticker in current_tickers
                        if ticker in current_streaks
                    }
                    raw["streaks"] = normalized_streaks
                    changed = True

                if sent and last_sent.get(normalized_timeframe) != candle_key:
                    last_sent[normalized_timeframe] = candle_key
                    changed = True

                if changed:
                    self._save()
                return self._to_user_settings(raw)

            streaks = raw.setdefault("streaks", {})
            current_streaks = normalize_streaks(streaks).get(normalized_timeframe, {})
            streaks[normalized_timeframe] = calculate_streaks_for_new_candle(
                current_streaks,
                normalized_tickers,
                last_processed_candle_key=last_processed_candle_key,
                previous_candle_key=previous_candle_key,
            )
            raw["streaks"] = normalize_streaks(streaks)
            last_processed[normalized_timeframe] = candle_key
            last_tickers[normalized_timeframe] = normalized_tickers
            if sent:
                last_sent[normalized_timeframe] = candle_key
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
        selected_tickers: Any = None,
    ) -> dict[str, Any]:
        user = {
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
            "last_processed_candle_keys": {},
            "last_sent_candle_keys": {},
            "last_auto_report_tickers": {},
            "streaks": {},
            "last_reports": {},
        }
        if selected_tickers is not None:
            user["selected_tickers"] = normalize_ticker_list(selected_tickers)
        return user

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

        raw["last_sent_candle_keys"] = normalize_last_sent_candle_keys(
            raw.get("last_sent_candle_keys", {})
        )
        legacy_candle_times = normalize_last_sent_candle_keys(
            raw.pop("last_sent_candle_times", {})
        )
        for timeframe, candle_key in legacy_candle_times.items():
            raw["last_sent_candle_keys"].setdefault(timeframe, candle_key)

        legacy_single_candle = raw.pop("last_sent_candle_time", None)
        if legacy_single_candle:
            legacy_timeframe = normalize_supported_timeframe(
                str(legacy_notification_timeframe or self.default_notification_timeframes[0])
            )
            raw["last_sent_candle_keys"].setdefault(
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
                raw["last_sent_candle_keys"].setdefault(timeframe, str(candle_time))

        raw["last_processed_candle_keys"] = normalize_last_sent_candle_keys(
            raw.get("last_processed_candle_keys", {})
        )
        for timeframe, candle_key in raw["last_sent_candle_keys"].items():
            raw["last_processed_candle_keys"].setdefault(timeframe, candle_key)

        raw["last_auto_report_tickers"] = normalize_auto_report_tickers(
            raw.get("last_auto_report_tickers", {})
        )

        raw.pop("auto_daily_report", None)
        raw.pop("auto_weekly_report", None)
        raw.pop("auto_monthly_report", None)
        raw.pop("last_sent_daily_candle", None)
        raw.pop("last_sent_weekly_candle", None)
        raw.pop("last_sent_monthly_candle", None)

        raw["streaks"] = normalize_streaks(raw.get("streaks", {}))
        if "selected_tickers" in raw:
            raw["selected_tickers"] = normalize_ticker_list(raw.get("selected_tickers"))
        raw.setdefault("last_reports", {})
        raw["last_reports"] = migrate_last_reports(raw["last_reports"])

    def _require_user(self, user_id: int) -> dict[str, Any]:
        raw = self._users().get(str(user_id))
        if raw is None:
            raise KeyError(f"User settings not found for user_id={user_id}")
        self._fill_missing_user_fields(raw)
        return raw

    @staticmethod
    def _missing_user(user_id: int) -> UserSettings:
        raise KeyError(f"User settings not found for user_id={user_id}")

    @staticmethod
    def _ensure_selected_tickers_raw(
        raw: dict[str, Any],
        all_tickers: list[str],
    ) -> bool:
        if "selected_tickers" not in raw:
            if not all_tickers:
                return False
            raw["selected_tickers"] = list(all_tickers)
            return True

        selected_tickers = normalize_ticker_list(raw.get("selected_tickers"))
        if all_tickers:
            selected_tickers = order_tickers(selected_tickers, all_tickers)
        if selected_tickers != raw.get("selected_tickers"):
            raw["selected_tickers"] = selected_tickers
            return True
        return False

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
            selected_tickers=normalize_ticker_list(raw.get("selected_tickers", [])),
            last_processed_candle_keys=normalize_last_sent_candle_keys(
                raw.get("last_processed_candle_keys", {})
            ),
            last_sent_candle_keys=normalize_last_sent_candle_keys(
                raw.get("last_sent_candle_keys", {})
            ),
            last_auto_report_tickers=normalize_auto_report_tickers(
                raw.get("last_auto_report_tickers", {})
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


def normalize_last_sent_candle_keys(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, str] = {}
    for timeframe, candle_time in value.items():
        normalized_timeframe = normalize_timeframe(str(timeframe))
        if normalized_timeframe not in SUPPORTED_TIMEFRAMES or not candle_time:
            continue
        normalized[normalized_timeframe] = str(candle_time)
    return normalized


def normalize_ticker_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_values = [str(part).strip() for part in value]
    else:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for ticker in raw_values:
        normalized_ticker = ticker.upper()
        if not normalized_ticker or normalized_ticker in seen:
            continue
        normalized.append(normalized_ticker)
        seen.add(normalized_ticker)
    return normalized


def merge_ticker_lists(*values: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for ticker in normalize_ticker_list(value):
            if ticker in seen:
                continue
            merged.append(ticker)
            seen.add(ticker)
    return merged


def order_tickers(selected_tickers: Any, all_tickers: Any) -> list[str]:
    selected = set(normalize_ticker_list(selected_tickers))
    ordered = [
        ticker
        for ticker in normalize_ticker_list(all_tickers)
        if ticker in selected
    ]
    extras = [
        ticker
        for ticker in normalize_ticker_list(selected_tickers)
        if ticker not in set(ordered)
    ]
    return [*ordered, *extras]


def normalize_auto_report_tickers(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, list[str]] = {}
    for timeframe, tickers in value.items():
        normalized_timeframe = normalize_timeframe(str(timeframe))
        if normalized_timeframe not in SUPPORTED_TIMEFRAMES:
            continue
        normalized[normalized_timeframe] = normalize_ticker_list(tickers)
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


def calculate_streaks_for_new_candle(
    current_streaks: dict[str, int],
    matched_tickers: list[str],
    *,
    last_processed_candle_key: str | None,
    previous_candle_key: str | None,
) -> dict[str, int]:
    if last_processed_candle_key != previous_candle_key:
        current_streaks = {}
    return calculate_next_streaks(current_streaks, matched_tickers)


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
