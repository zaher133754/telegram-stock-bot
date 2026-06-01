from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Literal


logger = logging.getLogger(__name__)
ReportType = Literal["daily", "weekly", "monthly"]
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
    auto_daily_report: bool
    auto_weekly_report: bool
    auto_monthly_report: bool
    last_sent_daily_candle: str | None = None
    last_sent_weekly_candle: str | None = None
    last_sent_monthly_candle: str | None = None
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
        default_auto_daily_report: bool = True,
        default_auto_weekly_report: bool = True,
        default_auto_monthly_report: bool = True,
    ) -> None:
        self.path = path
        self.default_timeframe = normalize_timeframe(default_timeframe)
        self.default_auto_daily_report = default_auto_daily_report
        self.default_auto_weekly_report = default_auto_weekly_report
        self.default_auto_monthly_report = default_auto_monthly_report
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
                auto_daily_report=True,
                auto_weekly_report=True,
                auto_monthly_report=True,
            )
            self._save()
            return self._to_user_settings(self._users()[str(user_id)])

    def set_timeframe(self, user_id: int, timeframe: str) -> UserSettings:
        with self._lock:
            raw = self._require_user(user_id)
            raw["selected_timeframe"] = normalize_timeframe(timeframe)
            raw.pop("timeframe", None)
            self._save()
            return self._to_user_settings(raw)

    def toggle_daily_report(self, user_id: int) -> UserSettings:
        return self._toggle_report_flag(user_id, "auto_daily_report")

    def toggle_weekly_report(self, user_id: int) -> UserSettings:
        return self._toggle_report_flag(user_id, "auto_weekly_report")

    def toggle_monthly_report(self, user_id: int) -> UserSettings:
        return self._toggle_report_flag(user_id, "auto_monthly_report")

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
            reports[normalize_timeframe(timeframe)] = {
                "text": text,
                "candle_time": candle_time,
                "created_at": created_at,
            }
            self._save()
            return self._to_user_settings(raw)

    def record_auto_report(
        self,
        *,
        user_id: int,
        report_type: ReportType,
        candle_time: str,
    ) -> UserSettings:
        with self._lock:
            raw = self._require_user(user_id)
            raw[f"last_sent_{report_type}_candle"] = candle_time
            self._save()
            return self._to_user_settings(raw)

    def get_last_report(self, user_id: int, timeframe: str) -> LastReport | None:
        with self._lock:
            raw = self._users().get(str(user_id))
            if raw is None:
                return None
            report = raw.get("last_reports", {}).get(normalize_timeframe(timeframe))
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

    def _toggle_report_flag(self, user_id: int, field: str) -> UserSettings:
        with self._lock:
            raw = self._require_user(user_id)
            raw[field] = not bool(raw.get(field, True))
            self._save()
            return self._to_user_settings(raw)

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
        auto_daily_report: bool | None = None,
        auto_weekly_report: bool | None = None,
        auto_monthly_report: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "chat_id": chat_id,
            "user_id": user_id,
            "selected_timeframe": normalize_timeframe(timeframe or self.default_timeframe),
            "auto_daily_report": (
                self.default_auto_daily_report
                if auto_daily_report is None
                else auto_daily_report
            ),
            "auto_weekly_report": (
                self.default_auto_weekly_report
                if auto_weekly_report is None
                else auto_weekly_report
            ),
            "auto_monthly_report": (
                self.default_auto_monthly_report
                if auto_monthly_report is None
                else auto_monthly_report
            ),
            "last_sent_daily_candle": None,
            "last_sent_weekly_candle": None,
            "last_sent_monthly_candle": None,
            "last_reports": {},
        }

    def _fill_missing_user_fields(self, raw: dict[str, Any]) -> None:
        old_timeframe = raw.pop("timeframe", None)
        selected_timeframe = raw.get("selected_timeframe", old_timeframe)
        raw["selected_timeframe"] = normalize_timeframe(
            str(selected_timeframe or self.default_timeframe)
        )

        old_auto_notifications = raw.pop("auto_notifications", None)
        fallback_auto = (
            bool(old_auto_notifications)
            if old_auto_notifications is not None
            else None
        )
        raw.setdefault(
            "auto_daily_report",
            fallback_auto
            if fallback_auto is not None
            else self.default_auto_daily_report,
        )
        raw.setdefault(
            "auto_weekly_report",
            fallback_auto
            if fallback_auto is not None
            else self.default_auto_weekly_report,
        )
        raw.setdefault(
            "auto_monthly_report",
            fallback_auto
            if fallback_auto is not None
            else self.default_auto_monthly_report,
        )
        raw.setdefault("last_sent_daily_candle", None)
        raw.setdefault("last_sent_weekly_candle", None)
        raw.setdefault("last_sent_monthly_candle", None)
        raw.pop("last_sent_candle_time", None)
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
            reports[normalize_timeframe(str(timeframe))] = LastReport(
                text=text,
                candle_time=report.get("candle_time"),
                created_at=str(report.get("created_at", "")),
            )

        return UserSettings(
            chat_id=int(raw["chat_id"]),
            user_id=int(raw["user_id"]),
            selected_timeframe=normalize_timeframe(str(raw["selected_timeframe"])),
            auto_daily_report=bool(raw["auto_daily_report"]),
            auto_weekly_report=bool(raw["auto_weekly_report"]),
            auto_monthly_report=bool(raw["auto_monthly_report"]),
            last_sent_daily_candle=raw.get("last_sent_daily_candle"),
            last_sent_weekly_candle=raw.get("last_sent_weekly_candle"),
            last_sent_monthly_candle=raw.get("last_sent_monthly_candle"),
            last_reports=reports,
        )


def normalize_timeframe(timeframe: str) -> str:
    return TIMEFRAME_ALIASES.get(timeframe.strip().lower(), timeframe.strip().lower())


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
        migrated[normalize_timeframe(str(timeframe))] = report
    return migrated
