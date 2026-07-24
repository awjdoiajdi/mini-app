import hashlib
import hmac
import json
import os
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123456:test-token-for-local-check")

from datetime import datetime, timezone

from bot import APP_VERSION, ai_messages, app_url, normalize_places, normalize_reminders, normalize_reminder_tasks, places_request, pop_capture, parse_ai_json, reminder_events, remember_capture, valid_timezone_offset, validate_init_data


def signed_data(token, now=1_700_000_000):
    values = {"auth_date": str(now), "query_id": "test", "user": json.dumps({"id": 42})}
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


if __name__ == "__main__":
    token = os.environ["BOT_TOKEN"]
    data = signed_data(token)
    assert validate_init_data(data, token, now=1_700_000_000)["id"] == 42
    try:
        validate_init_data(data.replace("test", "fake"), token, now=1_700_000_000)
        raise AssertionError("tampered data was accepted")
    except ValueError:
        pass
    assert parse_ai_json('```json\n{"ok": true}\n```')["ok"] is True
    assert f"v={APP_VERSION}" in app_url()
    safety = ai_messages("chat", {})[0]["content"]
    assert "jailbreak" in safety and "Я DailyOS" in safety
    capture_id = remember_capture("secret task", 42, now=10)
    assert pop_capture(capture_id, 7, now=11) is None
    assert pop_capture(capture_id, 42, now=11) == "secret task"
    request = places_request("аптека", 55.75, 37.61)
    assert request["origin"]["coordinates"] == [37.61, 55.75] and request["preferences"]["geometry"] == request["origin"]
    places = normalize_places({"results": [{"id": "poi-1", "title": "Аптека", "distanceInMeters": 124.6, "poiTypes": [{"name": "Pharmacy"}], "address": {"street": "Тверская", "houseNumber": "1", "municipality": "Москва", "country": "Россия"}}]})
    assert places == [{"id": "poi-1", "title": "Аптека", "category": "Pharmacy", "address": "Тверская 1, Москва, Россия", "distance": 125}]
    assert normalize_places({"results": ["bad", {"title": ""}]}) == []
    settings = normalize_reminders({"waterTimes": ["09:00", "wrong"], "sleepTime": "23:15"})
    assert settings["waterTimes"] == ["09:00"] and settings["sleepTime"] == "23:15"
    assert valid_timezone_offset(-180) and not valid_timezone_offset("-180") and not valid_timezone_offset(900)
    assert normalize_reminder_tasks([{"id": "t1", "title": "Созвон", "date": "2026-07-24", "time": "10:15"}, {"title": "bad", "date": "tomorrow", "time": "10:00"}]) == [{"id": "t1", "title": "Созвон", "date": "2026-07-24", "time": "10:15"}]
    events = reminder_events("42", {"timezoneOffset": 0, "settings": {"water": False, "exercise": False, "sleep": False, "tasks": True}, "tasks": [{"id": "t1", "title": "Созвон", "date": "2026-07-24", "time": "10:15"}], "snoozes": []}, datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc))
    assert events and events[0][0].startswith("task:t1:")
    assert reminder_events("42", {"timezoneOffset": "bad", "snoozes": ["bad", {"at": "2026-07-24T10:00", "id": "s1", "text": "Сделай паузу"}]}, datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc))[-1][0] == "snooze:s1"
    print("Telegram initData validation: OK")
