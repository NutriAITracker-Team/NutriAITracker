# Эмулятор умного автомата перекусов

Программный модуль NutriAI Tracker, который имитирует настольный автомат выдачи
снеков: виртуальная кнопка, заслонка и зелёный/красный индикатор.

Логика:

1. Пользователь нажимает кнопку.
2. Модуль отправляет `POST /snack-check` на backend с JWT и `device_id`.
3. Если дневной лимит калорий не превышен — загорается зелёный индикатор и
   открывается заслонка.
4. Если лимит превышен или остатка не хватает на снек — красный индикатор,
   заслонка остаётся закрытой.
5. При ошибке сети, таймауте или некорректном ответе — **fail-closed**:
   красный индикатор и закрытая заслонка.

При разрешённой выдаче сервер записывает снек в дневник пользователя как
обычный приём пищи с типом `snack`. Поэтому съеденные калории сразу попадают
в суточную статистику, и следующие нажатия учитывают их — обойти лимит
повторными нажатиями нельзя. Каждый запрос, включая отказы, попадает в журнал
`GET /snack-issues`.

---

## Быстрый старт на компьютере (виртуальный интерфейс)

Нужен Python 3.10+ с tkinter (в стандартной установке Windows он есть).

```powershell
cd "C:\Users\Дмитрий\Desktop\4kurs\tracker"

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r raspberry\requirements.txt

Copy-Item raspberry\.env.example raspberry\.env
```

Откройте `raspberry\.env` и укажите данные пользователя:

```dotenv
NUTRIAI_API_URL=http://localhost:8001
NUTRIAI_EMAIL=demo@example.com
NUTRIAI_PASSWORD=Passw0rd!123
NUTRIAI_DEVICE_ID=snack-pi-01
```

Запуск:

```powershell
python raspberry\snack_machine.py
```

Откроется окно с кнопкой, заслонкой и индикатором.

---

## Где взять пользователя для автомата

Автомат входит как обычный пользователь, поэтому у него должны быть профиль и
цель — иначе сервер не сможет рассчитать дневной лимит и вернёт отказ.

1. Откройте <http://localhost:8001/docs>.
2. `POST /auth/register` — создайте пользователя, например
   `demo@example.com` / `Passw0rd!123`.
3. Нажмите **Authorize**, введите email в поле `username` и пароль.
4. `PUT /profile` — заполните возраст, рост, вес и образ жизни.
5. `POST /goals` — выберите цель.
6. Эти же email и пароль укажите в `raspberry\.env`.

Проверить, что пользователь готов:

```powershell
python raspberry\snack_machine.py --headless --status
```

Если профиль заполнен, в ответе будет `allowed=True` и `remaining=...`.

---

## Запуск на Raspberry Pi

### 1. Подготовка системы

```bash
sudo apt update
sudo apt install -y python3-venv python3-tk
cd ~/tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r raspberry/requirements.txt gpiozero
```

### 2. Настройки

```bash
cp raspberry/.env.example raspberry/.env
nano raspberry/.env
```

Укажите IP компьютера с backend:

```dotenv
NUTRIAI_API_URL=http://192.168.1.10:8001
NUTRIAI_EMAIL=demo@example.com
NUTRIAI_PASSWORD=Passw0rd!123
NUTRIAI_DEVICE_ID=snack-pi-01
```

И включите GPIO:

```dotenv
GPIO_MODE=hardware
GPIO_GREEN_PIN=17
GPIO_RED_PIN=27
GPIO_SHUTTER_PIN=22
GPIO_BUTTON_PIN=
```

### 3. Подключение GPIO

| Элемент | Пин по умолчанию | Примечание |
|---|---|---|
| Зелёный светодиод | GPIO 17 | через резистор 220–330 Ом |
| Красный светодиод | GPIO 27 | через резистор 220–330 Ом |
| Сервопривод заслонки | GPIO 22 | питание сервопривода — отдельное 5 В |
| Кнопка (опционально) | не задан | `GPIO_BUTTON_PIN`, замыкание на GND |

Если на вашем сервоприводе направления инвертированы, поменяйте местами
`SHUTTER_OPEN_VALUE` и `SHUTTER_CLOSED_VALUE`.

### 4. Запуск

С монитором:

```bash
python raspberry/snack_machine.py
```

Без монитора (кнопка — Enter в терминале):

```bash
python raspberry/snack_machine.py --headless
```

Без монитора с физической кнопкой на GPIO:

```bash
GPIO_BUTTON_PIN=23 python raspberry/snack_machine.py --headless
```

---

## Режимы проверки

```bash
# Только статус лимита, без запроса
python raspberry/snack_machine.py --headless --status

# Один запрос и выход
python raspberry/snack_machine.py --headless --once
```

Коды возврата `--once`:

- `0` — перекус разрешён (зелёный, заслонка открыта);
- `1` — перекус запрещён (красный, заслонка закрыта);
- `2` — ошибка связи или настроек (fail-closed).

Чтобы проверить красный индикатор, добавьте за день блюдо с калорийностью
больше суточной нормы через Swagger или мобильное приложение и нажмите кнопку
ещё раз.

---

## Настройки

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `NUTRIAI_API_URL` | `http://localhost:8001` | Адрес backend |
| `NUTRIAI_EMAIL` | — | Email пользователя автомата |
| `NUTRIAI_PASSWORD` | — | Пароль пользователя |
| `NUTRIAI_TOKEN` | — | Готовый JWT вместо email/пароля |
| `NUTRIAI_DEVICE_ID` | `snack-pi-01` | Идентификатор устройства |
| `NUTRIAI_SNACK_ID` | `1` | Позиция в автомате, список: `GET /snacks` |
| `NUTRIAI_TIMEOUT` | `5` | Таймаут запроса, секунды |
| `GPIO_MODE` | `virtual` | `virtual` или `hardware` |
| `GPIO_GREEN_PIN` | `17` | Пин зелёного светодиода |
| `GPIO_RED_PIN` | `27` | Пин красного светодиода |
| `GPIO_SHUTTER_PIN` | `22` | Пин сервопривода заслонки |
| `GPIO_BUTTON_PIN` | — | Пин физической кнопки (опционально) |
| `SHUTTER_OPEN_VALUE` | `1` | Положение сервопривода «открыто» |
| `SHUTTER_CLOSED_VALUE` | `0` | Положение сервопривода «закрыто» |
| `SHUTTER_OPEN_SECONDS` | `4` | Время открытой заслонки |

---

## Структура

```text
raspberry/
├── snack_machine.py   # GUI, headless-режим и основная логика
├── api_client.py      # вход по JWT и запросы к /snack-check, /snack-status
├── config.py          # чтение raspberry/.env
├── gpio_backend.py    # виртуальные выходы и GPIO через gpiozero
├── requirements.txt
├── .env.example
└── README.md
```
