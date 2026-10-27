"""HTTP-клиент эмулятора к backend NutriAI Tracker."""

from __future__ import annotations

from typing import Any

import httpx


class SnackApiError(RuntimeError):
    """Понятная ошибка для показа в интерфейсе."""


class SnackApiClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        token: str | None = None,
        email: str | None = None,
        password: str | None = None,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._token = (token or "").strip() or None
        self._email = (email or "").strip() or None
        self._password = password or None

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    def login(self) -> str:
        if not self._email or not self._password:
            raise SnackApiError(
                "Токен истёк, а email/пароль не заданы. "
                "Укажите NUTRIAI_EMAIL и NUTRIAI_PASSWORD в raspberry/.env"
            )
        try:
            response = self._client.post(
                "/auth/login",
                data={"username": self._email, "password": self._password},
            )
        except httpx.HTTPError as exc:
            raise SnackApiError("Нет связи с сервером") from exc

        if response.status_code == 401:
            raise SnackApiError("Неверный email или пароль")
        if response.status_code >= 400:
            raise SnackApiError(f"Ошибка входа: HTTP {response.status_code}")

        token = (response.json() or {}).get("access_token")
        if not token:
            raise SnackApiError("Сервер не вернул токен доступа")
        self._token = token
        return token

    def ensure_token(self) -> None:
        if not self._token:
            self.login()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        self.ensure_token()
        try:
            response = self._client.request(
                method, path, headers=self._headers(), **kwargs
            )
        except httpx.HTTPError as exc:
            raise SnackApiError("Нет связи с сервером") from exc

        if response.status_code == 401:
            # Токен мог истечь — один раз пробуем войти заново.
            self._token = None
            self.login()
            try:
                response = self._client.request(
                    method, path, headers=self._headers(), **kwargs
                )
            except httpx.HTTPError as exc:
                raise SnackApiError("Нет связи с сервером") from exc

        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text[:300]
            raise SnackApiError(
                f"Сервер вернул HTTP {response.status_code}: {detail}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise SnackApiError("Сервер вернул ответ не в формате JSON") from exc

    def check_snack(self, device_id: str, snack_id: int | None = None) -> dict:
        payload: dict = {"device_id": device_id}
        if snack_id is not None:
            payload["snack_id"] = snack_id
        return self._normalize(
            self._request("POST", "/snack-check", json=payload)
        )

    def status(self, snack_id: int | None = None) -> dict:
        params = {"snack_id": snack_id} if snack_id is not None else None
        return self._normalize(
            self._request("GET", "/snack-status", params=params)
        )

    def list_snacks(self) -> list[dict]:
        data = self._request("GET", "/snacks")
        snacks = data.get("snacks")
        return snacks if isinstance(snacks, list) else []

    @staticmethod
    def _normalize(data: dict) -> dict:
        allowed = bool(data.get("allowed"))
        indicator = str(data.get("indicator") or "").upper()
        if indicator not in {"GREEN", "RED"}:
            indicator = "GREEN" if allowed else "RED"

        snack = data.get("snack") or {}
        meal = data.get("meal") or None
        consumed_after = data.get("consumed")
        consumed_before = data.get("consumed_before")

        return {
            "allowed": allowed,
            "indicator": indicator,
            "message": data.get("message")
            or ("Перекус разрешён" if allowed else "Перекус запрещён"),
            "reason": data.get("reason"),
            "limit": data.get("limit"),
            "consumed": consumed_after,
            "consumed_before": consumed_before,
            "remaining": data.get("remaining"),
            "date": data.get("date"),
            "device_id": data.get("device_id"),
            "snack": snack,
            "snack_name": snack.get("name"),
            "snack_calories": snack.get("calories"),
            "meal": meal,
            "meal_id": data.get("meal_id"),
            "issue_id": data.get("issue_id"),
            "added_to_diary": bool(meal),
        }
