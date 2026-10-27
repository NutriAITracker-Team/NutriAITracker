"""Настройки эмулятора автомата перекусов.

Все значения читаются из raspberry/.env (пример — raspberry/.env.example).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Config:
    api_url: str
    email: str
    password: str
    token: str
    device_id: str
    snack_id: int
    timeout: float
    gpio_mode: str
    green_pin: int
    red_pin: int
    shutter_pin: int
    button_pin: int | None
    shutter_open_value: float
    shutter_closed_value: float
    shutter_open_seconds: float

    @classmethod
    def from_env(cls) -> "Config":
        button_raw = _env("GPIO_BUTTON_PIN")
        return cls(
            api_url=_env("NUTRIAI_API_URL", "http://localhost:8001").rstrip("/"),
            email=_env("NUTRIAI_EMAIL"),
            password=_env("NUTRIAI_PASSWORD"),
            token=_env("NUTRIAI_TOKEN"),
            device_id=_env("NUTRIAI_DEVICE_ID", "snack-pi-01"),
            snack_id=int(_env("NUTRIAI_SNACK_ID", "1")),
            timeout=float(_env("NUTRIAI_TIMEOUT", "5")),
            gpio_mode=_env("GPIO_MODE", "virtual").lower(),
            green_pin=int(_env("GPIO_GREEN_PIN", "17")),
            red_pin=int(_env("GPIO_RED_PIN", "27")),
            shutter_pin=int(_env("GPIO_SHUTTER_PIN", "22")),
            button_pin=int(button_raw) if button_raw else None,
            shutter_open_value=float(_env("SHUTTER_OPEN_VALUE", "1")),
            shutter_closed_value=float(_env("SHUTTER_CLOSED_VALUE", "0")),
            shutter_open_seconds=float(_env("SHUTTER_OPEN_SECONDS", "4")),
        )

    def validate_auth(self) -> None:
        if not self.token and not (self.email and self.password):
            raise ValueError(
                "Задайте NUTRIAI_TOKEN или пару NUTRIAI_EMAIL/NUTRIAI_PASSWORD "
                "в raspberry/.env"
            )

    def validate_gpio(self) -> None:
        if self.gpio_mode not in {"virtual", "hardware"}:
            raise ValueError("GPIO_MODE должен быть virtual или hardware")
