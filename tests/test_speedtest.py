"""Тесты для speedtest.py.

Сеть не нужна: в setUpClass поднимается локальный HTTP-сервер на 127.0.0.1:0
(порт выбирает ОС) с фиксированным набором байт. Тесты детерминированные и изолированные:
счётчики сервера сбрасываются перед каждым тестом, сервер гасится в tearDownClass.
"""

from __future__ import annotations

import io
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Корень проекта добавляем в sys.path, чтобы тест мог импортировать speedtest.py рядом.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import speedtest  # noqa: E402  (импорт после правки sys.path)

# Эталонное «тяжёлое» тело ответа: размер фиксирован, чтобы точно сверять объём.
TEST_PAYLOAD = bytes(range(256)) * 1024  # 256 КБ
HEAVY_PATH = "/heavy.bin"
MISSING_PATH = "/missing.bin"
FLAKY_PATH = "/flaky.bin"


class TestHandler(BaseHTTPRequestHandler):
    """Обработчик: раздаёт эталонные байты, 404 и «мигающий» путь.

    Счётчики ведутся на уровне класса (сервер потоковый), доступ к ним под lock.
    """

    requests_total = 0   # сколько всего запросов пришло
    active = 0           # сколько запросов выполняется прямо сейчас
    max_active = 0       # максимум одновременных запросов — для проверки последовательности
    flaky_hits = 0       # сколько раз дёрнули «мигающий» путь
    lock = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        """Сбрасывает счётчики между тестами."""
        with cls.lock:
            cls.requests_total = 0
            cls.active = 0
            cls.max_active = 0
            cls.flaky_hits = 0

    def do_GET(self) -> None:  # noqa: N802  (имя задано BaseHTTPRequestHandler)
        """Обслуживает GET: /heavy.bin — 200 с телом, /flaky.bin — первый раз 500, /missing.bin — 404."""
        cls = type(self)
        with self.lock:
            cls.requests_total += 1
            cls.active += 1
            cls.max_active = max(cls.max_active, cls.active)
        try:
            if self.path == HEAVY_PATH:
                self._send_body(TEST_PAYLOAD)
            elif self.path == FLAKY_PATH:
                with self.lock:
                    cls.flaky_hits += 1
                    first_hit = cls.flaky_hits == 1
                if first_hit:
                    # Первый запрос специально ломаем: проверяем, что замер не прерывается.
                    self.send_error(500, "temporary failure")
                else:
                    self._send_body(TEST_PAYLOAD)
            else:
                self.send_error(404, "not found")
        finally:
            with self.lock:
                cls.active -= 1

    def _send_body(self, body: bytes) -> None:
        """Отдаёт 200 с полным телом и корректным Content-Length."""
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Глушим лог сервера, чтобы не засорять вывод тестов."""
        return None


class ServerTestCase(unittest.TestCase):
    """База: поднимает локальный HTTP-сервер на время класса и сбрасывает счётчики перед тестом."""

    server: ThreadingHTTPServer
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        # Порт 0 — ОС сама выдаст свободный, поэтому тесты не конфликтуют между собой.
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        # Корректно гасим сервер, иначе поток останется висеть и тест-раннер не завершится.
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        # Каждый тест начинается с нулевых счётчиков — изоляция тестов.
        TestHandler.reset()

    def url(self, path: str) -> str:
        """Собирает полный адрес локального ресурса."""
        return f"{self.base_url}{path}"


class DownloadOnceTests(ServerTestCase):
    """download_once: один запрос, полное тело, положительное время."""

    def test_reads_whole_body(self) -> None:
        # Объём должен совпасть с эталоном байт в байт — значит тело дочитано полностью.
        attempt = speedtest.download_once(self.url(HEAVY_PATH), timeout=5)
        self.assertEqual(attempt.bytes_received, len(TEST_PAYLOAD))
        self.assertGreater(attempt.seconds, 0.0)

    def test_missing_resource_raises(self) -> None:
        # 404 — это ошибка запроса, наружу уходит urllib.error.HTTPError.
        with self.assertRaises(speedtest.urllib.error.HTTPError) as ctx:
            speedtest.download_once(self.url(MISSING_PATH), timeout=5)
        # HTTPError — файловый объект; закрываем, иначе тест даст ResourceWarning.
        ctx.exception.close()


class MeasureTests(ServerTestCase):
    """measure: строго последовательные запросы, сбор ошибок без прерывания."""

    def test_makes_exactly_n_requests(self) -> None:
        attempts, errors = speedtest.measure(self.url(HEAVY_PATH), runs=10, timeout=5)
        self.assertEqual(len(attempts), 10)
        self.assertEqual(errors, [])
        # Сервер подтверждает: пришло ровно 10 запросов.
        self.assertEqual(TestHandler.requests_total, 10)

    def test_requests_are_sequential(self) -> None:
        # Если бы запросы шли параллельно, max_active был бы больше 1.
        speedtest.measure(self.url(HEAVY_PATH), runs=3, timeout=5)
        self.assertEqual(TestHandler.max_active, 1)

    def test_each_attempt_has_full_payload(self) -> None:
        attempts, _ = speedtest.measure(self.url(HEAVY_PATH), runs=2, timeout=5)
        for attempt in attempts:
            self.assertEqual(attempt.bytes_received, len(TEST_PAYLOAD))

    def test_failed_request_does_not_stop_measurement(self) -> None:
        # Первый запрос падает с 500, остальные успешны: ошибка копится, замер продолжается.
        attempts, errors = speedtest.measure(self.url(FLAKY_PATH), runs=3, timeout=5)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("попытка 1", errors[0])

    def test_all_failed_returns_errors(self) -> None:
        attempts, errors = speedtest.measure(self.url(MISSING_PATH), runs=2, timeout=5)
        self.assertEqual(attempts, [])
        self.assertEqual(len(errors), 2)


class SummaryTests(unittest.TestCase):
    """summarize и форматирование: арифметика на синтетических данных, без сети."""

    def test_totals_and_speed(self) -> None:
        # 1 МБ за 1 секунду = 8 Мбит/с; второй запрос — 0.5 МБ за 1 секунду.
        attempts = [
            speedtest.Attempt(bytes_received=1_000_000, seconds=1.0),
            speedtest.Attempt(bytes_received=500_000, seconds=1.0),
        ]
        summary = speedtest.summarize(attempts)
        self.assertEqual(summary.attempts, 2)
        self.assertEqual(summary.bytes_total, 1_500_000)
        self.assertEqual(summary.seconds_total, 2.0)
        # Среднее время запроса — суммарное время на число запросов.
        self.assertAlmostEqual(summary.avg_seconds, 1.0)
        # 1 500 000 байт / 2 с = 750 000 байт/с = 6 Мбит/с.
        self.assertAlmostEqual(summary.mbit_per_second, 6.0)

    def test_empty_attempts_raise(self) -> None:
        # Пустой список — это ошибка вызывающего кода, а не «нулевая скорость».
        with self.assertRaises(ValueError):
            speedtest.summarize([])

    def test_format_helpers(self) -> None:
        summary = speedtest.summarize([speedtest.Attempt(bytes_received=1024 ** 2, seconds=1.0)])
        self.assertEqual(speedtest.format_bytes(1024 ** 2), "1.00 МБ")
        text = speedtest.format_speed(summary)
        # В строке скорости обе единицы: Мбит/с — основная, МБ/с — в скобках.
        self.assertIn("Мбит/с", text)
        self.assertIn("МБ/с", text)


class ValidateUrlTests(unittest.TestCase):
    """validate_url: пропускает только абсолютные http/https адреса."""

    def test_accepts_http_and_https(self) -> None:
        # Валидные адреса не должны бросать исключений.
        speedtest.validate_url("http://127.0.0.1:8000/file.bin")
        speedtest.validate_url("https://example.com/heavy.jpg")

    def test_rejects_other_schemes_and_relative(self) -> None:
        for bad in ("ftp://example.com/x", "/local/path.bin", "example.com/x"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                speedtest.validate_url(bad)


class CliTests(ServerTestCase):
    """main: коды выхода и содержимое отчёта в консоли."""

    def test_prints_report_and_returns_zero(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = speedtest.main([self.url(HEAVY_PATH), "-n", "2", "--timeout", "5"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        # Проверяем ключевые строки отчёта, которые обещаны в README.
        self.assertIn("Успешных запросов: 2 из 2", text)
        self.assertIn("Среднее время запроса:", text)
        self.assertIn("Скорость скачивания:", text)
        self.assertIn("Мбит/с", text)
        # Скачано ровно два эталонных тела.
        self.assertIn(speedtest.format_bytes(2 * len(TEST_PAYLOAD)), text)

    def test_returns_one_when_all_requests_failed(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = speedtest.main([self.url(MISSING_PATH), "-n", "2", "--timeout", "5"])
        self.assertEqual(code, 1)
        self.assertIn("[ошибка]", err.getvalue())
        self.assertIn("Ни одна попытка не удалась", err.getvalue())

    def test_rejects_zero_runs(self) -> None:
        # parser.error завершает программу с кодом 2 — это стандартное поведение argparse.
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            speedtest.main([self.url(HEAVY_PATH), "-n", "0"])
        self.assertEqual(ctx.exception.code, 2)

    def test_rejects_bad_url_before_network(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            speedtest.main(["ftp://example.com/x"])
        self.assertEqual(ctx.exception.code, 2)
        # До сети дело не дошло — ни одного запроса на локальный сервер не пришло.
        self.assertEqual(TestHandler.requests_total, 0)


if __name__ == "__main__":
    unittest.main()
