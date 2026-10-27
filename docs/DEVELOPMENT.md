# NutriAI Tracker — разработка и запуск

Техническая документация проекта: запуск, настройка ИИ, эндпоинты, проверки.
Описание предметной области и назначения системы — в [README](../README.md).

Система состоит из трёх частей:

1. **Backend** — Python, FastAPI, SQLite (на текущем этапе), ИИ через OpenAI-совместимый API.
2. **Android-приложение** — Java, Retrofit (следующий этап).
3. **Эмулятор автомата перекусов** — Python, Raspberry Pi / GPIO.

---

## Текущий этап (0–1)

**Этапы 0–1 завершены: backend стабилен, воспроизводимо запускается и проверен сквозным smoke-тестом.**

Готово:

- регистрация и вход по email с JWT;
- профиль (возраст, рост, вес, образ жизни) и расчёт суточной нормы КБЖУ;
- выбор и смена цели;
- дневные записи: вес, шаги, пульс, самочувствие;
- `PUT` и `DELETE` дневной записи, каскадное удаление блюд;
- список записей и статистика за выбранный день;
- загрузка фотографии блюда и анализ через ИИ;
- сохранение фотографий на диск и раздача по `/media/...`;
- тип приёма пищи (`breakfast/lunch/dinner/snack`) у каждого блюда;
- ручное добавление блюда, если ИИ не справился;
- ручная корректировка массы и КБЖУ с автоматическим пересчётом статистики;
- реальные персональные рекомендации и рецепты через ИИ с кэшированием в БД;
- правило-ориентированный fallback, если внешний ИИ недоступен;
- каталог снеков и выдача через `/snack-check` с JWT: снек записывается в дневник как обычное блюдо;
- журнал выдач `snack_issues` — сохраняются и разрешённые выдачи, и отказы;
- проверка лимита с учётом калорийности снека, обойти лимит повторными нажатиями нельзя;
- эмулятор автомата перекусов: виртуальный интерфейс, headless-режим и GPIO для Raspberry Pi;
- Docker Compose для запуска одной командой;
- smoke-тест всех основных сценариев.

Ещё не сделано (следующие этапы):

- Android-приложение;
- PostgreSQL, Celery и Redis (сейчас SQLite и синхронный анализ фото);
- pytest-набор и скрипт демо-данных.

---

## Быстрый старт (Docker)

Нужен установленный и запущенный Docker Desktop.

```powershell
cd "C:\Users\Дмитрий\Desktop\4kurs\tracker"

# 1. Создать файл локальных настроек
Copy-Item .env.example .env

# 2. Собрать и запустить backend
docker compose up -d --build

# 3. Проверить, что контейнер здоров
docker compose ps
```

После запуска открываются:

| Что | Адрес |
|---|---|
| Swagger UI | <http://localhost:8001/docs> |
| Health-check | <http://localhost:8001/health> |
| Статус ИИ | <http://localhost:8001/ai/status> |
| Проверка ИИ | <http://localhost:8001/ai/check> |

Порт **8001**, чтобы не конфликтовать с другим проектом на 8000.

Если Docker Hub недоступен и образ уже собран ранее, запускайте без пересборки:

```powershell
docker compose up -d --no-build
```

---

## Куда вставить ключ ИИ

Ключ хранится только в локальном файле `.env`. Этот файл добавлен в `.gitignore` и не попадает в репозиторий.

Рекомендуемый провайдер — **Groq**: бесплатно, быстро и поддерживает изображения.

### 1. Получите бесплатный ключ Groq

1. Откройте <https://console.groq.com/> и войдите.
2. Перейдите в <https://console.groq.com/keys>.
3. Нажмите **Create API Key** и скопируйте значение вида `gsk_...`.

### 2. Вставьте ключ в файл

Откройте файл:

```text
C:\Users\Дмитрий\Desktop\4kurs\tracker\.env
```

Найдите строку `AI_API_KEY=` и вставьте ключ после знака равенства:

```dotenv
AI_API_KEY=gsk_ВАШ_КЛЮЧ
AI_BASE_URL=https://api.groq.com/openai/v1
AI_MODEL=qwen/qwen3.8-27b
AI_TIMEOUT_SECONDS=30
AI_MAX_TOKENS=3000
AI_RECOMMENDATION_MAX_TOKENS=4000
```

Никаких кавычек и пробелов вокруг значения быть не должно.

`qwen/qwen3.8-27b` — мультимодальная модель: принимает и текст, и изображения. На ней анализ фото занимает около 1–2 секунд, рекомендации — около 2 секунд.

### 3. Перезапустите backend

```powershell
docker compose up -d --force-recreate
```

### 4. Проверьте ключ

Откройте в браузере <http://localhost:8001/ai/check>.

- `{"ok": true, ...}` — ключ и модель работают.
- `401` или `403` — ключ неверный.
- `400` или `404` — модель недоступна. Проверьте актуальный список моделей в консоли Groq или в разделе «Альтернатива».

Проверить распознавание конкретного фото можно локально:

```powershell
python scripts/check_ai.py "C:\путь\к\фото_еды.jpg"
```

Для этого локально должен быть установлен Python и зависимости (`pip install -r requirements.txt`).

### Альтернатива: OpenRouter

OpenRouter тоже подходит, но бесплатные vision-модели могут быть медленными (12–42 секунды на фото) и периодически отдавать `429`.

```dotenv
AI_API_KEY=sk-or-v1-ВАШ_КЛЮЧ
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=nvidia/nemotron-3.5-super-vl:free
AI_TIMEOUT_SECONDS=60
```

Список актуальных бесплатных vision-моделей:

```powershell
docker exec nutriaitracker-backend python scripts/list_openrouter_models.py
```

---

## Сквозная проверка

Backend должен быть запущен.

```powershell
docker exec -e API_URL=http://127.0.0.1:8000 nutriaitracker-backend python scripts/smoke_test.py
```

Скрипт создаёт тестового пользователя и проходит цепочку:

`health → регистрация → вход → профиль → цель → дневная запись → приём пищи с meal_type → каталог снеков → выдача снека в дневник → повторные нажатия до отказа → журнал выдач → статистика → PUT записи → рекомендации (генерация + кэш) → статус ИИ → DELETE записи`.

Ожидаемый результат в конце: `Все шаги пройдены.`

---

## Запуск без Docker

```powershell
cd "C:\Users\Дмитрий\Desktop\4kurs\tracker"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

Сервер поднимется на <http://localhost:8001>. Чтобы изменить порт, задайте переменную `PORT`.

---

## Основные эндпоинты

Полный контракт: [openapi.json](openapi.json) и Swagger UI.

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/auth/register` | Регистрация (JSON: `email`, `password`) |
| POST | `/auth/login` | Вход (form-urlencoded: `username` = email, `password`) |
| GET/PUT | `/profile` | Профиль и текущая норма КБЖУ |
| GET/POST | `/goals` | История целей и выбор активной цели |
| POST/GET | `/daily-records` | Создание/обновление и список дневных записей |
| PUT/DELETE | `/daily-records/{date}` | Обновление и удаление дневной записи |
| GET | `/daily-records/{date}` | Запись за дату, блюда и статистика |
| POST | `/daily-records/{date}/meals` | Ручное добавление блюда (с `meal_type`) |
| POST | `/daily-records/{date}/analyze-photo` | Загрузка фото (multipart) и анализ ИИ |
| PUT/DELETE | `/meals/{meal_id}` | Ручная корректировка и удаление блюда |
| GET | `/snacks` | Каталог снеков автомата |
| POST | `/snack-check` | Проверка лимита и выдача снека (нужен JWT) |
| GET | `/snack-status` | Статус готовности автомата, без записи в БД |
| GET | `/snack-issues` | Журнал выдач: разрешённые и отклонённые |
| GET | `/recommendations` | Рекомендации за день: из кэша или генерация через ИИ |
| POST | `/recommendations` | Принудительная перегенерация рекомендаций |
| GET | `/health`, `/ai/status`, `/ai/check` | Диагностика |
| GET | `/media/...` | Сохранённые фотографии |

`meal_type` принимает одно из значений: `breakfast`, `lunch`, `dinner`, `snack`. Если не передать его при добавлении блюда, сервер подставит тип по текущему времени суток.

Даты передаются в формате `YYYY-MM-DD` по часовому поясу `APP_TIMEZONE` (по умолчанию `Asia/Yekaterinburg`). Будущие даты отклоняются.

## Как работают рекомендации

- `GET /recommendations?date=YYYY-MM-DD` возвращает последнюю сохранённую рекомендацию за день. Если её нет, генерирует через ИИ и сохраняет в таблицу `recommendations`.
- `POST /recommendations?date=YYYY-MM-DD` принудительно генерирует рекомендации заново.
- Если после генерации пользователь изменил блюда или дневную запись, рекомендация считается устаревшей и перегенерируется при следующем `GET`.
- Если профиль не заполнен или за день нет блюд, возвращается `insufficient_data: true` с подсказкой, что нужно добавить.
- Если внешний ИИ недоступен, возвращается правило-ориентированный fallback: `ai_generated: false`, `fallback: true`, причина — в поле `error`.
- В ответе есть `summary`, `nutrition_advice`, `activity_advice`, `recipes`, а также `consumed`, `norm` и `balance`.

## Как работает автомат перекусов

- Каталог хранится в таблице `snacks`. При первом запуске засеваются три позиции: протеиновый батончик (180 ккал), орехи (200 ккал), яблоко (95 ккал). Список: `GET /snacks`.
- `POST /snack-check` принимает `device_id` и `snack_id` (по умолчанию — первая позиция).
- Сервер проверяет не «осталось больше нуля», а «хватает ли остатка на этот снек»: `consumed + snack.calories <= norm`.
- Если разрешено — создаётся обычное блюдо с `meal_type: "snack"`, названием и КБЖУ снека. Оно сразу попадает в дневник и в суточную статистику, поэтому повторное нажатие видит уже съеденные калории.
- Каждый запрос — и разрешённый, и отклонённый — пишется в журнал `snack_issues` с устройством, причиной и состоянием лимита до и после.
- В ответе: `allowed`, `indicator`, `reason`, `snack`, `meal`, `meal_id`, `issue_id`, `added_to_diary`, `consumed_before`, `consumed`, `limit`, `remaining`.
- Причины отказа: `LIMIT_EXCEEDED` (лимит исчерпан), `NOT_ENOUGH_CALORIES` (остатка не хватает на снек), `PROFILE_INCOMPLETE` (не заполнен профиль).

## Эмулятор автомата перекусов

Модуль лежит в `raspberry/`. Он входит в систему как пользователь, отправляет
`POST /snack-check` и управляет заслонкой и индикатором. При ошибке связи
заслонка закрыта, горит красный — принцип fail-closed.

Режимы:

- GUI на компьютере (Tkinter);
- headless для Raspberry Pi;
- GPIO через `gpiozero`: зелёный/красный светодиод, сервопривод заслонки,
  опциональная физическая кнопка.

Быстрый старт на компьютере:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r raspberry\requirements.txt
Copy-Item raspberry\.env.example raspberry\.env
# заполните NUTRIAI_EMAIL и NUTRIAI_PASSWORD
python raspberry\snack_machine.py
```

Проверка без окна:

```powershell
python raspberry\snack_machine.py --headless --status
python raspberry\snack_machine.py --headless --once
```

Подробная инструкция для Raspberry Pi и таблица GPIO:
[raspberry/README.md](../raspberry/README.md).

---

## Структура проекта

```text
tracker/
├── main.py                 # весь backend: модели, схемы, ИИ, эндпоинты
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example            # шаблон настроек
├── .env                    # локальные настройки и ключ (не коммитится)
├── data/                   # SQLite (не коммитится)
├── media/                  # загруженные фото (не коммитится)
├── scripts/
│   ├── smoke_test.py
│   ├── check_ai.py
│   └── list_openrouter_models.py
├── raspberry/              # эмулятор автомата: GUI, headless, GPIO
│   ├── snack_machine.py
│   ├── api_client.py
│   ├── config.py
│   ├── gpio_backend.py
│   └── README.md
├── docs/
│   ├── 01-domain.md ... 06-architecture.md
│   ├── DEVELOPMENT.md      # этот файл: запуск и API
│   └── openapi.json        # контракт API для Android и эмулятора
└── source/                 # исходный Word-документ
```

---

## Известные ограничения текущего этапа

- Основной провайдер — Groq + `qwen/qwen3.8-27b`: анализ фото около 1,4 секунды, рекомендации около 1,8 секунды. Бесплатный тариф Groq имеет лимиты запросов; при `429` просто повторите запрос.
- OpenRouter-модель Nemotron работает точно, но медленно: анализ фото 12–42 секунды, рекомендации 25–40 секунд. Используйте её только как резервный вариант.
- Бесплатные модели могут отвечать `429`. Анализ фото вернёт понятную ошибку с `photo_url` и предложением ввести блюдо вручную, а рекомендации переключатся на правило-ориентированный fallback.
- Анализ фото и генерация рекомендаций выполняются синхронно в пуле потоков. Celery и Redis ещё не подключены.
- Внешняя авторизация (`/auth/external`) — заглушка.
- Нет ограничения попыток входа после 5 ошибок.
- Фотографии раздаются без авторизации (допустимо для локальной демонстрации).
- pytest-набор и скрипт демо-данных ещё не добавлены.
- SQLite и синхронная обработка заменяются на PostgreSQL и Celery/Redis на следующем этапе.
