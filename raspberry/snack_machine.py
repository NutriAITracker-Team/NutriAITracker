"""Эмулятор умного автомата перекусов NutriAI Tracker.

Запуск с виртуальным интерфейсом (Windows/Linux):
    python raspberry/snack_machine.py

Запуск без графики (Raspberry Pi и проверка):
    python raspberry/snack_machine.py --headless --status
    python raspberry/snack_machine.py --headless --once
    python raspberry/snack_machine.py --headless

Логика: нажатие кнопки -> POST /snack-check с JWT -> зелёный свет и
открытая заслонка либо красный свет и закрытая заслонка. Любая ошибка
связи трактуется как запрет (fail-closed).
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from datetime import datetime

try:
    from .api_client import SnackApiClient, SnackApiError
    from .config import Config
    from .gpio_backend import create_outputs
except ImportError:  # запуск как обычного скрипта
    from api_client import SnackApiClient, SnackApiError
    from config import Config
    from gpio_backend import create_outputs

try:
    import tkinter as tk
except ImportError:  # на Raspberry Pi может не быть python3-tk
    tk = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("snack_machine")


class SnackMachineApp:
    BG = "#0f1720"
    PANEL = "#1b2a36"
    TEXT = "#e8f0f7"
    MUTED = "#8aa0b4"
    GREEN = "#2ecc71"
    RED = "#e74c3c"
    GRAY = "#667788"

    def __init__(self, root, config: Config, client: SnackApiClient, outputs):
        self.root = root
        self.config = config
        self.client = client
        self.outputs = outputs
        self.busy = False
        self.shutter_open = False
        self._close_job = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(300, self.refresh_status)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self.root.title("NutriAI Tracker — автомат перекусов")
        self.root.geometry("620x820")
        self.root.configure(bg=self.BG)
        self.root.resizable(False, False)

        header = tk.Frame(self.root, bg=self.BG)
        header.pack(fill="x", padx=24, pady=(20, 6))
        tk.Label(
            header,
            text="NutriAI Tracker",
            font=("Segoe UI", 22, "bold"),
            fg=self.TEXT,
            bg=self.BG,
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Умный автомат перекусов",
            font=("Segoe UI", 11),
            fg=self.MUTED,
            bg=self.BG,
        ).pack(anchor="w")

        info = tk.Frame(self.root, bg=self.BG)
        info.pack(fill="x", padx=24)
        tk.Label(
            info,
            text=f"Устройство: {self.config.device_id}",
            font=("Segoe UI", 9),
            fg=self.MUTED,
            bg=self.BG,
        ).pack(side="left")
        tk.Label(
            info,
            text=f"Сервер: {self.config.api_url}",
            font=("Segoe UI", 9),
            fg=self.MUTED,
            bg=self.BG,
        ).pack(side="right")

        self.canvas = tk.Canvas(
            self.root,
            width=572,
            height=330,
            bg=self.PANEL,
            highlightthickness=0,
        )
        self.canvas.pack(padx=24, pady=14)
        self._draw_machine()

        self.status_label = tk.Label(
            self.root,
            text="Проверка связи с сервером…",
            font=("Segoe UI", 12, "bold"),
            fg=self.TEXT,
            bg=self.BG,
            wraplength=560,
            justify="center",
        )
        self.status_label.pack(padx=24, pady=(0, 8))

        self.details_label = tk.Label(
            self.root,
            text="",
            font=("Segoe UI", 10),
            fg=self.MUTED,
            bg=self.BG,
        )
        self.details_label.pack(padx=24, pady=(0, 12))

        self.request_button = tk.Button(
            self.root,
            text="ЗАПРОСИТЬ ПЕРЕКУС",
            font=("Segoe UI", 14, "bold"),
            bg=self.GREEN,
            fg="#0b2013",
            activebackground="#27ae60",
            activeforeground="#0b2013",
            relief="flat",
            bd=0,
            height=2,
            cursor="hand2",
            command=self.request_snack,
        )
        self.request_button.pack(fill="x", padx=24, pady=(0, 8))

        self.refresh_button = tk.Button(
            self.root,
            text="Обновить статус",
            font=("Segoe UI", 10),
            bg=self.PANEL,
            fg=self.TEXT,
            activebackground="#243746",
            activeforeground=self.TEXT,
            relief="flat",
            bd=0,
            height=1,
            cursor="hand2",
            command=self.refresh_status,
        )
        self.refresh_button.pack(fill="x", padx=24, pady=(0, 12))

        self.log = tk.Text(
            self.root,
            height=7,
            bg="#0b131a",
            fg=self.MUTED,
            insertbackground=self.TEXT,
            relief="flat",
            bd=0,
            font=("Consolas", 9),
            state="disabled",
        )
        self.log.pack(fill="both", padx=24, pady=(0, 20))

    def _draw_machine(self) -> None:
        c = self.canvas
        c.create_rectangle(
            20, 20, 552, 310, fill="#16232e", outline="#2c4254", width=2
        )
        c.create_text(
            286,
            45,
            text="SNACK  •  NUTRIAI",
            fill="#5f7d95",
            font=("Segoe UI", 10, "bold"),
        )
        self.indicator = c.create_oval(
            488, 70, 528, 110, fill=self.GRAY, outline="#0b131a", width=3
        )
        c.create_text(508, 122, text="СТАТУС", fill="#5f7d95", font=("Segoe UI", 7))

        # Окно выдачи и перекус внутри (перекус будет виден при открытой заслонке)
        c.create_rectangle(
            70, 160, 472, 230, fill="#05090c", outline="#2c4254", width=2
        )
        c.create_rectangle(
            210, 175, 332, 218, fill="#d9a441", outline="#f1c40f", width=2
        )
        self.snack_label = c.create_text(
            271,
            196,
            text="SNACK",
            fill="#3b2a08",
            font=("Segoe UI", 8, "bold"),
            width=110,
            justify="center",
        )

        self.shutter = c.create_rectangle(
            70, 160, 472, 230, fill="#5d7183", outline="#8aa0b4", width=2
        )
        c.create_text(
            271,
            195,
            text="ЗАСЛОНКА",
            fill="#1a2732",
            font=("Segoe UI", 10, "bold"),
            tags="shutter_text",
        )
        c.create_text(
            271,
            270,
            text="Нажмите кнопку, чтобы запросить перекус",
            fill="#5f7d95",
            font=("Segoe UI", 9),
        )

    # ------------------------------------------------------------- helpers

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.request_button.configure(
            state=state, bg="#2b3d4d" if busy else self.GREEN
        )
        self.refresh_button.configure(state=state)

    def _set_indicator(self, color: str) -> None:
        fill = {"GREEN": self.GREEN, "RED": self.RED}.get(color, self.GRAY)
        self.canvas.itemconfigure(self.indicator, fill=fill)

    def _set_shutter(self, is_open: bool) -> None:
        target = 90 if is_open else 160
        current = int(self.canvas.coords(self.shutter)[1])
        if current == target:
            self.shutter_open = is_open
            self.canvas.itemconfigure(
                "shutter_text", state="hidden" if is_open else "normal"
            )
            return

        step = -5 if target < current else 5
        self.canvas.itemconfigure("shutter_text", state="hidden")

        def animate() -> None:
            coords = self.canvas.coords(self.shutter)
            y1 = int(coords[1])
            if (step < 0 and y1 <= target) or (step > 0 and y1 >= target):
                self.canvas.coords(
                    self.shutter, coords[0], target, coords[2], target + 70
                )
                self.shutter_open = is_open
                self.canvas.itemconfigure(
                    "shutter_text", state="hidden" if is_open else "normal"
                )
                return
            y1 += step
            self.canvas.coords(self.shutter, coords[0], y1, coords[2], y1 + 70)
            self.root.after(15, animate)

        animate()

    def _format_details(self, result: dict) -> str:
        parts = []
        if result.get("remaining") is not None:
            parts.append(f"Осталось: {result['remaining']} ккал")
        if result.get("limit") is not None:
            parts.append(f"Лимит: {result['limit']} ккал")
        if result.get("consumed") is not None:
            parts.append(f"Съедено: {result['consumed']} ккал")
        if result.get("added_to_diary"):
            parts.append("Снек записан в дневник")
        return "   •   ".join(parts)

    def _set_snack_label(self, name: str | None, calories: float | None) -> None:
        if not name:
            return
        text = f"{name}  •  {calories:.0f} ккал" if calories else name
        self.canvas.itemconfigure(self.snack_label, text=text)

    def _run_async(self, func, on_success, on_error) -> None:
        def worker() -> None:
            try:
                result = func()
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: on_error(exc))
            else:
                self.root.after(0, lambda: on_success(result))

        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------------------- actions

    def refresh_status(self) -> None:
        if self.busy:
            return
        if self._close_job is not None:
            self.root.after_cancel(self._close_job)
            self._close_job = None
        self._set_busy(True)
        self.status_label.configure(
            text="Проверка связи с сервером…", fg=self.TEXT
        )

        def success(result: dict) -> None:
            self._set_busy(False)
            self._set_indicator("GRAY")
            self._set_shutter(False)
            self.outputs.set_indicator("GRAY")
            self.outputs.set_shutter(False)
            self._set_snack_label(
                result.get("snack_name"), result.get("snack_calories")
            )
            if result["allowed"]:
                text = "Готов к работе. Перекус будет разрешён."
                color = self.TEXT
            else:
                text = result["message"]
                color = self.RED
            self.status_label.configure(text=text, fg=color)
            self.details_label.configure(text=self._format_details(result))
            self._log(f"Статус: {text}")

        def error(exc: Exception) -> None:
            self._set_busy(False)
            self._set_indicator("RED")
            self._set_shutter(False)
            self.outputs.set_indicator("RED")
            self.outputs.set_shutter(False)
            self.status_label.configure(
                text="Нет связи с сервером. Выдача заблокирована.",
                fg=self.RED,
            )
            self.details_label.configure(text="")
            self._log(f"Ошибка: {exc}")

        self._run_async(self.client.status, success, error)

    def request_snack(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self._set_indicator("GRAY")
        self.outputs.set_indicator("GRAY")
        self.status_label.configure(text="Запрос разрешения…", fg=self.TEXT)
        self._log("Нажата кнопка «Запросить перекус»")

        def success(result: dict) -> None:
            self._set_busy(False)
            self._apply(result)

        def error(exc: Exception) -> None:
            self._set_busy(False)
            self._set_indicator("RED")
            self._set_shutter(False)
            self.outputs.set_indicator("RED")
            self.outputs.set_shutter(False)
            self.status_label.configure(
                text="Нет связи с сервером. Выдача заблокирована.",
                fg=self.RED,
            )
            self._log(f"Ошибка: {exc}")

        self._run_async(
            lambda: self.client.check_snack(
                self.config.device_id, self.config.snack_id
            ),
            success,
            error,
        )

    def _apply(self, result: dict) -> None:
        self._set_indicator(result["indicator"])
        self.outputs.set_indicator(result["indicator"])
        self.details_label.configure(text=self._format_details(result))
        self._set_snack_label(
            result.get("snack_name"), result.get("snack_calories")
        )

        if result["allowed"]:
            name = result.get("snack_name") or "Снек"
            calories = result.get("snack_calories")
            added = (
                f"«{name}» добавлен в дневник, +{calories:.0f} ккал."
                if result.get("added_to_diary") and calories
                else f"«{name}»."
            )
            self.status_label.configure(
                text=f"Перекус разрешён. {added}", fg=self.GREEN
            )
            self._set_shutter(True)
            self.outputs.set_shutter(True)
            self._log(f"Разрешено: {added} Осталось {result.get('remaining')} ккал")
            if self._close_job is not None:
                self.root.after_cancel(self._close_job)
            self._close_job = self.root.after(
                int(self.config.shutter_open_seconds * 1000),
                self._auto_close,
            )
        else:
            self.status_label.configure(text=result["message"], fg=self.RED)
            self._set_shutter(False)
            self.outputs.set_shutter(False)
            self._log(f"Запрещено: {result['message']}")

    def _auto_close(self) -> None:
        self._close_job = None
        self._set_shutter(False)
        self.outputs.set_shutter(False)
        self._log("Заслонка автоматически закрыта")
        self._set_indicator("GRAY")
        self.outputs.set_indicator("GRAY")
        self.status_label.configure(
            text="Заслонка закрыта. Можно запросить снова.", fg=self.TEXT
        )

    def on_close(self) -> None:
        if self._close_job is not None:
            self.root.after_cancel(self._close_job)
        try:
            self.client.close()
        finally:
            try:
                self.outputs.close()
            finally:
                self.root.destroy()


# ---------------------------------------------------------------- headless


def _print_result(result: dict) -> None:
    print(
        "INDICATOR={indicator} allowed={allowed} snack={snack!r} "
        "calories={calories} remaining={remaining} limit={limit} "
        "added_to_diary={added} meal_id={meal_id} issue_id={issue_id}".format(
            indicator=result["indicator"],
            allowed=result["allowed"],
            snack=result.get("snack_name"),
            calories=result.get("snack_calories"),
            remaining=result.get("remaining"),
            limit=result.get("limit"),
            added=result.get("added_to_diary"),
            meal_id=result.get("meal_id"),
            issue_id=result.get("issue_id"),
        )
    )
    print(f"MESSAGE={result.get('message')}")


def run_headless(
    config: Config,
    client: SnackApiClient,
    outputs,
    once: bool = False,
    status_only: bool = False,
) -> int:
    if status_only:
        try:
            result = client.status(config.snack_id)
        except SnackApiError as exc:
            print(f"INDICATOR=RED allowed=False error={exc}")
            return 2
        _print_result(result)
        return 0

    lock = threading.Lock()

    def do_request() -> int:
        if not lock.acquire(blocking=False):
            print("Запрос уже выполняется")
            return 2
        try:
            try:
                result = client.check_snack(
                    config.device_id, config.snack_id
                )
            except Exception as exc:  # noqa: BLE001
                outputs.set_indicator("RED")
                outputs.set_shutter(False)
                print(f"INDICATOR=RED allowed=False error={exc}")
                return 2

            outputs.set_indicator(result["indicator"])
            outputs.set_shutter(result["allowed"])
            _print_result(result)

            if result["allowed"]:
                time.sleep(config.shutter_open_seconds)
                outputs.set_shutter(False)
                outputs.set_indicator("GRAY")
                return 0
            return 1
        finally:
            lock.release()

    if once:
        return do_request()

    if config.gpio_mode == "hardware" and config.button_pin is not None:
        try:
            from gpiozero import Button
            from signal import pause
        except ImportError as exc:
            print(f"Для физической кнопки установите gpiozero: {exc}")
            return 2

        button = Button(config.button_pin, pull_up=True, bounce_time=0.2)
        button.when_pressed = do_request
        print(
            f"Ожидание нажатия кнопки на GPIO {config.button_pin}. "
            "Ctrl+C — выход."
        )
        try:
            pause()
        except KeyboardInterrupt:
            pass
        finally:
            button.close()
        return 0

    print("Эмулятор автомата перекусов. Enter — запросить, Ctrl+C — выход.")
    while True:
        try:
            input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        do_request()


# --------------------------------------------------------------------- main


def build_client(config: Config) -> SnackApiClient:
    return SnackApiClient(
        base_url=config.api_url,
        timeout=config.timeout,
        token=config.token,
        email=config.email,
        password=config.password,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Эмулятор умного автомата перекусов NutriAI Tracker"
    )
    parser.add_argument(
        "--headless", action="store_true", help="без графического интерфейса"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="один запрос и выход (включает headless)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="только показать статус (включает headless)",
    )
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
        config.validate_auth()
        config.validate_gpio()
    except ValueError as exc:
        print(f"Ошибка настроек: {exc}")
        return 2

    try:
        outputs = create_outputs(config)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка GPIO: {exc}")
        return 2

    client = build_client(config)
    headless = args.headless or args.once or args.status or tk is None

    try:
        if headless:
            if tk is None and not (args.headless or args.once or args.status):
                print("tkinter не установлен, запускаю headless-режим.")
            return run_headless(
                config,
                client,
                outputs,
                once=args.once,
                status_only=args.status,
            )

        try:
            root = tk.Tk()
        except Exception as exc:  # noqa: BLE001
            print(
                f"Не удалось открыть графический интерфейс: {exc}\n"
                "Запустите с флагом --headless."
            )
            return 2

        SnackMachineApp(root, config, client, outputs)
        root.mainloop()
        return 0
    finally:
        client.close()
        outputs.close()


if __name__ == "__main__":
    sys.exit(main())
