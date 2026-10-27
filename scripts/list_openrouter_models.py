"""Показывает бесплатные модели OpenRouter, которые принимают изображения.

Запуск из корня проекта:
    python scripts/list_openrouter_models.py
"""

import sys

import httpx

URL = "https://openrouter.ai/api/v1/models"


def main() -> int:
    try:
        response = httpx.get(URL, timeout=30)
        response.raise_for_status()
    except Exception as exc:
        print(f"Не удалось получить список моделей: {exc}")
        print()
        print("Если ошибка 403 — OpenRouter блокирует ваш IP или регион.")
        print("Варианты: включить VPN, использовать Groq или другой")
        print("OpenAI-совместимый сервис (AI_BASE_URL и AI_MODEL в .env).")
        return 1

    models = response.json().get("data", [])
    rows = []

    for model in models:
        architecture = model.get("architecture") or {}
        modalities = architecture.get("input_modalities") or []
        pricing = model.get("pricing") or {}
        try:
            is_free = (
                float(pricing.get("prompt") or 0) == 0
                and float(pricing.get("completion") or 0) == 0
            )
        except (TypeError, ValueError):
            is_free = False

        if "image" in modalities and is_free:
            rows.append((model.get("id"), model.get("name") or ""))

    if not rows:
        print("Бесплатные vision-модели не найдены.")
        print("Список вручную: https://openrouter.ai/models?fmt=cards&input_modalities=image")
        return 1

    print(f"Бесплатные модели с поддержкой изображений ({len(rows)}):\n")
    for model_id, name in rows:
        print(f"  {model_id}")
        print(f"      {name}")

    print()
    print("Скопируйте id модели в .env в строку AI_MODEL=")
    return 0


if __name__ == "__main__":
    sys.exit(main())
