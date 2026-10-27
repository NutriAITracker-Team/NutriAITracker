"""Проверка ключа и модели ИИ.

Запуск из корня проекта:
    python scripts/check_ai.py
    python scripts/check_ai.py C:\\путь\\к\\фото_еды.jpg
"""

import base64
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

API_KEY = os.getenv("AI_API_KEY", "").strip()
BASE_URL = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/")
MODEL = os.getenv("AI_MODEL", "").strip()
TIMEOUT = float(os.getenv("AI_TIMEOUT_SECONDS", "15"))


def post_chat(payload: dict, headers: dict) -> dict | None:
    url = f"{BASE_URL}/chat/completions"
    try:
        response = httpx.post(
            url, headers=headers, json=payload, timeout=TIMEOUT + 15
        )
    except httpx.HTTPError as exc:
        print(f"Сетевая ошибка: {exc}")
        return None

    print(f"HTTP {response.status_code}")
    if response.status_code >= 400:
        print(response.text[:1000])
        if response.status_code in (401, 403):
            print()
            print("401/403: проверьте ключ, а также блокировку по IP/региону.")
            print("Для OpenRouter часто нужен VPN.")
        return None
    return response.json()


def print_answer(data: dict) -> None:
    try:
        print("Ответ:", data["choices"][0]["message"]["content"])
    except (KeyError, IndexError):
        print("Неожиданный ответ:", json.dumps(data, ensure_ascii=False)[:1000])


def main() -> int:
    if not API_KEY or not MODEL:
        print("В .env не заданы AI_API_KEY и/или AI_MODEL.")
        print(f"Файл: {ROOT / '.env'}")
        return 1

    print(f"Провайдер: {BASE_URL}")
    print(f"Модель:    {MODEL}")
    print(f"Ключ:      {API_KEY[:8]}...{API_KEY[-4:]}")
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    print("\n1) Проверка текстового запроса")
    data = post_chat(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Ответь одним словом: работает"}],
            "max_tokens": 20,
        },
        headers,
    )
    if data:
        print_answer(data)

    if len(sys.argv) < 2:
        print()
        print("Чтобы проверить распознавание фото, передайте путь к картинке:")
        print("  python scripts/check_ai.py C:\\путь\\к\\еде.jpg")
        return 0

    image_path = Path(sys.argv[1])
    if not image_path.exists():
        print(f"\nФайл не найден: {image_path}")
        return 1

    suffix = image_path.suffix.lower()
    mime = {".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg")
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")

    print(f"\n2) Проверка распознавания фото: {image_path}")
    prompt = (
        "Определи блюдо, массу порции, калории, белки, жиры и углеводы. "
        'Верни строго JSON: {"dish":"...","weight_g":250,"calories":350,'
        '"protein":20,"fat":15,"carbs":30}'
    )
    data = post_chat(
        {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                    ],
                }
            ],
            "max_tokens": 400,
        },
        headers,
    )
    if data:
        print_answer(data)

    return 0


if __name__ == "__main__":
    sys.exit(main())
