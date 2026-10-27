"""Сквозной smoke-тест API.

Запуск из корня проекта (backend должен быть уже запущен):
    python scripts/smoke_test.py

Адрес API можно переопределить:
    API_URL=http://127.0.0.1:8001 python scripts/smoke_test.py
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

API_URL = os.getenv("API_URL", "http://127.0.0.1:8001").rstrip("/")
PASSWORD = "Passw0rd!123"


class Smoke:
    def __init__(self) -> None:
        self.client = httpx.Client(base_url=API_URL, timeout=150)
        self.token: str | None = None
        self.failures = 0

    def step(self, name: str, func) -> None:
        try:
            func()
            print(f"[ OK ] {name}")
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            print(f"[FAIL] {name}: {exc}")

    @staticmethod
    def check(response: httpx.Response, expected: int = 200) -> dict:
        if response.status_code != expected:
            raise AssertionError(
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
        return response.json()

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


def main() -> int:
    smoke = Smoke()
    state: dict = {}

    def health() -> None:
        data = smoke.check(smoke.client.get("/health"))
        print(
            f"       today={data.get('today')} "
            f"ai_enabled={data.get('ai_enabled')} "
            f"timezone={data.get('timezone')}"
        )
        state["today"] = data["today"]

    smoke.step("GET /health", health)
    today = state.get("today") or datetime.now().date().isoformat()
    email = f"smoke_{int(time.time())}@example.com"

    def register() -> None:
        smoke.check(
            smoke.client.post(
                "/auth/register",
                json={"email": email, "password": PASSWORD},
            )
        )

    smoke.step("POST /auth/register", register)

    def login() -> None:
        data = smoke.check(
            smoke.client.post(
                "/auth/login",
                data={"username": email, "password": PASSWORD},
            )
        )
        smoke.token = data["access_token"]
        assert smoke.token

    smoke.step("POST /auth/login", login)

    def profile() -> None:
        smoke.check(
            smoke.client.put(
                "/profile",
                headers=smoke.headers(),
                json={
                    "age": 25,
                    "height_cm": 180,
                    "weight_kg": 80,
                    "lifestyle": "moderate",
                },
            )
        )
        data = smoke.check(
            smoke.client.get("/profile", headers=smoke.headers())
        )
        assert data["norm"] is not None, data
        state["norm"] = data["norm"]

    smoke.step("PUT /profile + GET /profile", profile)

    def goal() -> None:
        smoke.check(
            smoke.client.post(
                "/goals",
                headers=smoke.headers(),
                json={"goal_type": "maintain"},
            )
        )

    smoke.step("POST /goals", goal)

    def daily_record() -> None:
        smoke.check(
            smoke.client.post(
                "/daily-records",
                headers=smoke.headers(),
                json={
                    "date": today,
                    "weight_kg": 80,
                    "steps": 5000,
                    "pulse_avg": 70,
                    "pulse_min": 55,
                    "pulse_max": 120,
                    "wellbeing": "нормально",
                },
            )
        )

    smoke.step("POST /daily-records", daily_record)

    def snacks_catalog() -> None:
        data = smoke.check(smoke.client.get("/snacks"))
        snacks = data.get("snacks") or []
        assert snacks, data
        assert snacks[0]["calories"] > 0, snacks[0]
        state["snack"] = snacks[0]
        print(
            f"       снеков: {len(snacks)}, по умолчанию: "
            f"{snacks[0]['name']} ({snacks[0]['calories']} ккал)"
        )

    smoke.step("GET /snacks", snacks_catalog)

    def snack_issued_to_diary() -> None:
        meal = smoke.check(
            smoke.client.post(
                f"/daily-records/{today}/meals",
                headers=smoke.headers(),
                json={
                    "dish_name": "Тестовый салат",
                    "meal_type": "lunch",
                    "weight_g": 200,
                    "calories": 300,
                    "protein": 10,
                    "fat": 10,
                    "carbs": 30,
                },
            )
        )
        assert meal["meal"]["meal_type"] == "lunch", meal

        snack = state["snack"]
        before = smoke.check(
            smoke.client.get(
                f"/daily-records/{today}", headers=smoke.headers()
            )
        )["statistics"]["consumed"]["calories"]

        data = smoke.check(
            smoke.client.post(
                "/snack-check",
                headers=smoke.headers(),
                json={
                    "device_id": "smoke-test",
                    "snack_id": snack["id"],
                },
            )
        )
        assert data["allowed"] is True, data
        assert data["indicator"] == "GREEN", data
        assert data["added_to_diary"] is True, data
        assert data["snack"]["name"] == snack["name"], data
        assert data["issue_id"] is not None, data
        assert data["meal"]["meal_type"] == "snack", data["meal"]

        expected = round(before + snack["calories"], 1)
        assert abs(data["consumed"] - expected) < 0.01, (data, before, snack)

        record = smoke.check(
            smoke.client.get(
                f"/daily-records/{today}", headers=smoke.headers()
            )
        )
        snack_meals = [
            m for m in record["meals"] if m["meal_type"] == "snack"
        ]
        assert snack_meals, record
        assert abs(
            record["statistics"]["consumed"]["calories"] - expected
        ) < 0.01, record

    smoke.step(
        "snack-check выдаёт снек и пишет его в дневник",
        snack_issued_to_diary,
    )

    def snack_limit_not_bypassable() -> None:
        snack = state["snack"]
        norm = state["norm"]["calories"]
        allowed_count = 0
        blocked = None

        for _ in range(40):
            data = smoke.check(
                smoke.client.post(
                    "/snack-check",
                    headers=smoke.headers(),
                    json={
                        "device_id": "smoke-test",
                        "snack_id": snack["id"],
                    },
                )
            )
            if not data["allowed"]:
                blocked = data
                break
            allowed_count += 1

        assert blocked is not None, "лимит так и не был достигнут"
        assert blocked["indicator"] == "RED", blocked
        assert blocked["added_to_diary"] is False, blocked
        assert blocked["meal_id"] is None, blocked
        assert blocked["issue_id"] is not None, blocked
        assert allowed_count >= 1, allowed_count

        record = smoke.check(
            smoke.client.get(
                f"/daily-records/{today}", headers=smoke.headers()
            )
        )
        consumed = record["statistics"]["consumed"]["calories"]
        assert consumed <= norm + 0.01, (consumed, norm)
        state["snack_issues_expected"] = allowed_count

        print(
            f"       выдано снеков: {allowed_count}, "
            f"съедено {consumed} из {norm}, причина отказа: "
            f"{blocked['reason']}"
        )

    smoke.step(
        "snack-check не обходит лимит повторными нажатиями",
        snack_limit_not_bypassable,
    )

    def snack_journal() -> None:
        data = smoke.check(
            smoke.client.get(
                "/snack-issues", headers=smoke.headers(), params={"limit": 100}
            )
        )
        issues = data.get("issues") or []
        assert issues, data
        allowed = [i for i in issues if i["allowed"]]
        denied = [i for i in issues if not i["allowed"]]
        assert allowed, issues
        assert denied, issues
        assert all(i["snack_name"] for i in issues), issues
        assert all(i["meal_id"] for i in allowed), allowed
        assert all(not i["meal_id"] for i in denied), denied
        print(
            f"       записей: разрешено {len(allowed)}, "
            f"отклонено {len(denied)}"
        )

    smoke.step("GET /snack-issues (журнал выдач)", snack_journal)

    def statistics() -> None:
        data = smoke.check(
            smoke.client.get(
                f"/daily-records/{today}", headers=smoke.headers()
            )
        )
        assert data["statistics"]["consumed"]["calories"] > 0, data
        assert data["statistics"]["balance"] is not None, data

    smoke.step("GET /daily-records/{date}", statistics)

    def update_daily_record() -> None:
        smoke.check(
            smoke.client.put(
                f"/daily-records/{today}",
                headers=smoke.headers(),
                json={"steps": 7000, "wellbeing": "хорошо"},
            )
        )
        data = smoke.check(
            smoke.client.get(
                f"/daily-records/{today}", headers=smoke.headers()
            )
        )
        assert data["steps"] == 7000, data
        assert data["wellbeing"] == "хорошо", data

    smoke.step("PUT /daily-records/{date}", update_daily_record)

    def recommendations() -> None:
        data = smoke.check(
            smoke.client.get("/recommendations", headers=smoke.headers())
        )
        assert data["insufficient_data"] is False, data
        assert "recommendations" in data, data
        assert data["summary"] or data["nutrition_advice"] or data["recipes"], data
        cached = smoke.check(
            smoke.client.get("/recommendations", headers=smoke.headers())
        )
        assert cached["cached"] is True, cached

    smoke.step("GET /recommendations (генерация + кэш)", recommendations)

    def ai_status() -> None:
        data = smoke.check(smoke.client.get("/ai/status"))
        print(
            f"       ai configured={data.get('configured')} "
            f"model={data.get('model')}"
        )

    smoke.step("GET /ai/status", ai_status)

    def delete_daily_record() -> None:
        smoke.check(
            smoke.client.delete(
                f"/daily-records/{today}", headers=smoke.headers()
            )
        )
        response = smoke.client.get(
            f"/daily-records/{today}", headers=smoke.headers()
        )
        assert response.status_code == 404, response.text

    smoke.step("DELETE /daily-records/{date}", delete_daily_record)

    print()
    if smoke.failures:
        print(f"Провалено шагов: {smoke.failures}")
        return 1

    print("Все шаги пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
