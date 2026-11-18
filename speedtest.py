#!/usr/bin/env python3
"""speedtest.py — замер скорости скачивания по N последовательным запросам.

Что делает скрипт:
  1) принимает адрес ресурса (URL) и число повторов (по умолчанию 10);
  2) последовательно скачивает ресурс, каждый раз дочитывая тело ответа до конца;
  3) считает среднее время одного запроса и общий объём скачанных данных;
  4) печатает скорость скачивания в Мбит/с (десятичные мегабиты, 1 Мбит = 1e6 бит).

Зависимостей нет — только стандартная библиотека Python 3.9+.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

# Константы по умолчанию: вынесены наверх, чтобы их было легко менять и тестировать.
DEFAULT_RUNS = 10          # сколько запросов делать по умолчанию
DEFAULT_TIMEOUT = 30.0     # таймаут одного запроса в секундах
CHUNK_SIZE = 64 * 1024     # размер блока при чтении тела ответа (64 КБ)
USER_AGENT = "etolegko-speedtest/1.0"

# Переводные коэффициенты для человекочитаемых единиц.
BYTES_IN_MB = 1024 ** 2    # 1 МиБ в байтах (для объёма)
BITS_IN_MBIT = 1_000_000   # 1 Мбит в битах (десятичное, как у провайдеров)


@dataclass(frozen=True)
class Attempt:
    """Результат одного запроса: сколько байт скачано и за сколько секунд."""

    bytes_received: int
    seconds: float


@dataclass(frozen=True)
class Summary:
    """Итоговая статистика по всем успешным попыткам."""

    attempts: int
    bytes_total: int
    seconds_total: float

    @property
    def avg_seconds(self) -> float:
        """Среднее время одного запроса в секундах."""
        # Деление защищено проверкой attempts > 0 в summarize(), но оставляем явный guard.
        return self.seconds_total / self.attempts if self.attempts else 0.0

    @property
    def bytes_per_second(self) -> float:
        """Средняя скорость в байтах в секунду (объём делим на суммарное время)."""
        return self.bytes_total / self.seconds_total if self.seconds_total > 0 else 0.0

    @property
    def mbit_per_second(self) -> float:
        """Средняя скорость в Мбит/с: байты -> биты -> мегабиты."""
        return self.bytes_per_second * 8 / BITS_IN_MBIT


def download_once(url: str, timeout: float = DEFAULT_TIMEOUT) -> Attempt:
    """Скачивает url один раз и возвращает объём и время запроса.

    Время засекается перед отправкой запроса и останавливается только после того,
    как тело ответа прочитано целиком — то есть мы ждём полного ответа, а не заголовков.
    Исключения urllib/OSError не перехватываются: решение об ошибке принимает вызывающий код.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        received = 0
        # Читаем блок за блоком до пустого ответа — это и есть "дождаться полного ответа".
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            received += len(chunk)
    elapsed = time.perf_counter() - started
    return Attempt(bytes_received=received, seconds=elapsed)


def measure(
    url: str,
    runs: int = DEFAULT_RUNS,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[list[Attempt], list[str]]:
    """Делает runs последовательных запросов к url.

    Возвращает пару: список успешных попыток и список текстов ошибок.
    Запросы строго последовательные: следующий стартует после полного завершения предыдущего.
    Ошибка одной попытки не прерывает замер — она попадает в список ошибок.
    """
    attempts: list[Attempt] = []
    errors: list[str] = []
    for index in range(1, runs + 1):
        try:
            attempts.append(download_once(url, timeout=timeout))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Сетевые сбои и кривой URL копим как текст ошибки, чтобы показать пользователю.
            errors.append(f"попытка {index}: {exc}")
    return attempts, errors


def summarize(attempts: list[Attempt]) -> Summary:
    """Считает итоговую статистику по успешным попыткам.

    Для пустого списка бросаем ValueError: молча возвращать нули было бы обманом.
    """
    if not attempts:
        raise ValueError("нет успешных попыток — считать статистику нечего")
    return Summary(
        attempts=len(attempts),
        bytes_total=sum(attempt.bytes_received for attempt in attempts),
        # Суммарное время: запросы шли последовательно, поэтому сумма корректна.
        seconds_total=sum(attempt.seconds for attempt in attempts),
    )


def format_bytes(size: float) -> str:
    """Переводит байты в МБ с двумя знаками после запятой."""
    return f"{size / BYTES_IN_MB:.2f} МБ"


def format_speed(summary: Summary) -> str:
    """Готовит строку скорости в Мбит/с и МБ/с — единицы указаны в README."""
    return (
        f"{summary.mbit_per_second:.2f} Мбит/с "
        f"({format_bytes(summary.bytes_per_second)}/с)"
    )


def build_parser() -> argparse.ArgumentParser:
    """Собирает парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="speedtest.py",
        description="Замеряет скорость скачивания по нескольким последовательным запросам.",
    )
    parser.add_argument("url", help="адрес ресурса, например https://example.com/heavy.jpg")
    parser.add_argument(
        "-n",
        "--runs",
        type=int,
        default=DEFAULT_RUNS,
        help=f"сколько запросов сделать последовательно (по умолчанию {DEFAULT_RUNS})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"таймаут одного запроса в секундах (по умолчанию {DEFAULT_TIMEOUT:g})",
    )
    return parser


def validate_url(url: str) -> None:
    """Проверяет, что адрес абсолютный и на http/https — иначе urllib упадёт невнятно."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"ожидается http(s):// адрес, получено: {url!r}")


def main(argv: list[str] | None = None) -> int:
    """Точка входа: разбирает аргументы, делает замер, печатает отчёт.

    Возвращает код завершения: 0 — успех, 1 — ошибка (нет данных или неверные аргументы).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Проверяем аргументы до сетевых запросов, чтобы не тратить время впустую.
    if args.runs < 1:
        parser.error("--runs должен быть >= 1")
    if args.timeout <= 0:
        parser.error("--timeout должен быть > 0")
    try:
        validate_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"Адрес: {args.url}")
    print(f"Запросов: {args.runs} последовательно")
    print("Скачиваю...")

    attempts, errors = measure(args.url, runs=args.runs, timeout=args.timeout)

    # Ошибки показываем всегда — пользователь должен видеть, что часть запросов упала.
    for message in errors:
        print(f"[ошибка] {message}", file=sys.stderr)

    if not attempts:
        print("Ни одна попытка не удалась — замер невозможен.", file=sys.stderr)
        return 1

    summary = summarize(attempts)

    print()
    print(f"Успешных запросов: {summary.attempts} из {args.runs}")
    print(f"Скачано всего: {format_bytes(summary.bytes_total)}")
    print(f"Суммарное время: {summary.seconds_total:.3f} с")
    print(f"Среднее время запроса: {summary.avg_seconds * 1000:.1f} мс")
    print(f"Скорость скачивания: {format_speed(summary)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
