"""Выходы автомата: виртуальные (GUI) или реальные GPIO Raspberry Pi."""

from __future__ import annotations

import logging

logger = logging.getLogger("snack_machine.gpio")


class VirtualOutputs:
    """Заглушка для режима без GPIO: всё рисует графический интерфейс."""

    def set_indicator(self, color: str) -> None:
        logger.debug("Виртуальный индикатор: %s", color)

    def set_shutter(self, is_open: bool) -> None:
        logger.debug("Виртуальная заслонка: %s", "открыта" if is_open else "закрыта")

    def close(self) -> None:
        pass


class GpioOutputs:
    """Зелёный/красный светодиод и сервопривод заслонки на Raspberry Pi."""

    def __init__(
        self,
        green_pin: int,
        red_pin: int,
        shutter_pin: int,
        open_value: float,
        closed_value: float,
    ) -> None:
        try:
            from gpiozero import LED, Servo
        except ImportError as exc:
            raise RuntimeError(
                "Для GPIO_MODE=hardware установите gpiozero: pip install gpiozero"
            ) from exc

        self._open_value = open_value
        self._closed_value = closed_value
        self._green = LED(green_pin)
        self._red = LED(red_pin)
        self._shutter = Servo(shutter_pin)
        self._green.off()
        self._red.off()
        self._shutter.value = closed_value

    def set_indicator(self, color: str) -> None:
        if color == "GREEN":
            self._green.on()
            self._red.off()
        elif color == "RED":
            self._green.off()
            self._red.on()
        else:
            self._green.off()
            self._red.off()

    def set_shutter(self, is_open: bool) -> None:
        self._shutter.value = (
            self._open_value if is_open else self._closed_value
        )

    def close(self) -> None:
        try:
            self._green.off()
            self._red.off()
            self._shutter.value = self._closed_value
        finally:
            self._green.close()
            self._red.close()
            self._shutter.close()


def create_outputs(config) -> VirtualOutputs | GpioOutputs:
    config.validate_gpio()
    if config.gpio_mode == "hardware":
        logger.info(
            "GPIO: green=%s red=%s shutter=%s",
            config.green_pin,
            config.red_pin,
            config.shutter_pin,
        )
        return GpioOutputs(
            green_pin=config.green_pin,
            red_pin=config.red_pin,
            shutter_pin=config.shutter_pin,
            open_value=config.shutter_open_value,
            closed_value=config.shutter_closed_value,
        )
    return VirtualOutputs()
