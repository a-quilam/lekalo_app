# tests/test_stage254_polling.py
r"""
Poster 2.5.4 §8 (тест I) — устойчивость polling к сбоям VPN-прокси.

Контракт (спецификация этапа F, §8):

1. Приложение собирается с ЯВНЫМИ HTTP-таймаутами, рассчитанными на
   медленный TLS-handshake через прокси (`HTTPS_PROXY=127.0.0.1:3130`,
   xray). Дефолты PTB 22.8 — 5 с connect/read/write и 1 с pool — тесны:
   журнал 2026-11-05 (23:40:55) показывает `httpx.ConnectTimeout` →
   `telegram.error.TimedOut` на обычном API-вызове, апдейт терялся.
2. Для `getUpdates` задан СВОЙ набор таймаутов: PTB строит отдельный
   `HTTPXRequest` для long-polling, без явных значений он оставил бы
   дефолты. Read-таймаут getUpdates не должен быть маленьким: PTB сам
   прибавляет серверный long-poll `timeout` к read.
3. `main()` запускает polling с `bootstrap_retries=-1`: дефолт PTB 22.8
   равен 0, и один сбой инициализации через прокси при старте завершал
   процесс (инцидент 2026-11-05: «Network Retry Loop (Bootstrap Initialize
   Application) ... Aborting»), бота поднимал только systemd Restart.
4. `allowed_updates` по-прежнему передаётся явно (регрессия P0) — этот
   тест лишь дополняет `tests/test_p0_allowed_updates`.

Тест контрактный и без сети: таймауты читаются из собранного
`HTTPXRequest` (`app.bot._request` — пара (get_updates, обычный), см.
`telegram._bot.Bot.__init__`), а `main()` вызывается с подменёнными
`setup_logging`/`init_db`/`build_application`, как в
`tests/test_p0_allowed_updates.py`.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage254_polling -v
"""

import unittest
from unittest.mock import MagicMock, patch

# Пакет tests подставляет фиктивный токен в окружение (tests/__init__.py)
# до импорта bot/config — импорт стоит первым намеренно.
import tests  # noqa: F401

try:
    import httpx
    from telegram import Update
    from telegram.ext import Application
    from telegram.request import BaseRequest

    import bot as bot_module

    IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    IMPORT_ERROR = str(e)


@unittest.skipIf(IMPORT_ERROR is not None, f"импорт недоступен: {IMPORT_ERROR}")
class ProxyTimeoutConstantsTest(unittest.TestCase):
    """Значения констант — контракт этапа F §8 (меняются осознанно)."""

    def test_common_timeouts_larger_than_ptb_defaults(self) -> None:
        """5 с дефолта тесны для TLS через прокси — запас должен быть больше."""
        self.assertEqual(bot_module.PROXY_CONNECT_TIMEOUT, 15.0)
        self.assertEqual(bot_module.PROXY_READ_TIMEOUT, 15.0)
        self.assertEqual(bot_module.PROXY_WRITE_TIMEOUT, 15.0)
        self.assertEqual(bot_module.PROXY_POOL_TIMEOUT, 5.0)
        for value in (
            bot_module.PROXY_CONNECT_TIMEOUT,
            bot_module.PROXY_READ_TIMEOUT,
            bot_module.PROXY_WRITE_TIMEOUT,
        ):
            self.assertGreater(value, 5.0, "таймаут не больше дефолта PTB (5 с)")

    def test_get_updates_timeouts_are_not_too_small(self) -> None:
        """Маленький read обрывал бы long-poll: PTB прибавляет server timeout."""
        self.assertEqual(bot_module.POLLING_GET_UPDATES_CONNECT_TIMEOUT, 15.0)
        self.assertEqual(bot_module.POLLING_GET_UPDATES_READ_TIMEOUT, 15.0)
        self.assertEqual(bot_module.POLLING_GET_UPDATES_WRITE_TIMEOUT, 15.0)
        self.assertEqual(bot_module.POLLING_GET_UPDATES_POOL_TIMEOUT, 5.0)
        self.assertGreaterEqual(
            bot_module.POLLING_GET_UPDATES_READ_TIMEOUT,
            bot_module.PROXY_READ_TIMEOUT,
            "read-таймаут getUpdates меньше обычного — long-poll оборвётся",
        )

    def test_bootstrap_retries_infinite(self) -> None:
        """-1 = бесконечный retry bootstrap (дефолт PTB 0 убивал процесс)."""
        self.assertEqual(bot_module.POLLING_BOOTSTRAP_RETRIES, -1)


@unittest.skipIf(IMPORT_ERROR is not None, f"импорт недоступен: {IMPORT_ERROR}")
class BuildApplicationTimeoutsTest(unittest.TestCase):
    """Собранное приложение реально применяет таймауты (без сети)."""

    app: Application
    get_updates_request: BaseRequest
    request: BaseRequest

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = bot_module.build_application()
        # Bot хранит пару запросов: (getUpdates, обычный) — telegram._bot.Bot.
        cls.get_updates_request, cls.request = cls.app.bot._request

    def test_build_application_returns_application(self) -> None:
        """Обычное построение не ломается новыми таймаутами."""
        self.assertIsNotNone(self.app.updater)
        self.assertTrue(self.app.error_handlers, "error_handler не зарегистрирован")
        self.assertTrue(self.app.handlers[0], "обработчики не зарегистрированы")

    def _timeout(self, request: object) -> httpx.Timeout:
        kwargs = getattr(request, "_client_kwargs")
        return kwargs["timeout"]

    def test_regular_request_timeouts(self) -> None:
        """Обычные API-вызовы должны ждать прокси, а не падать за 5 с."""
        timeout = self._timeout(self.request)
        self.assertEqual(timeout.connect, bot_module.PROXY_CONNECT_TIMEOUT)
        self.assertEqual(timeout.read, bot_module.PROXY_READ_TIMEOUT)
        self.assertEqual(timeout.write, bot_module.PROXY_WRITE_TIMEOUT)
        self.assertEqual(timeout.pool, bot_module.PROXY_POOL_TIMEOUT)

    def test_get_updates_request_timeouts(self) -> None:
        """У long-polling свой HTTPXRequest — таймауты заданы и там."""
        timeout = self._timeout(self.get_updates_request)
        self.assertEqual(
            timeout.connect, bot_module.POLLING_GET_UPDATES_CONNECT_TIMEOUT
        )
        self.assertEqual(timeout.read, bot_module.POLLING_GET_UPDATES_READ_TIMEOUT)
        self.assertEqual(timeout.write, bot_module.POLLING_GET_UPDATES_WRITE_TIMEOUT)
        self.assertEqual(timeout.pool, bot_module.POLLING_GET_UPDATES_POOL_TIMEOUT)

    def test_requests_are_distinct_objects(self) -> None:
        """PTB строит отдельные запросы: иначе getUpdates получил бы дефолты."""
        self.assertIsNot(self.get_updates_request, self.request)

    def test_read_timeout_property_matches(self) -> None:
        """Публичное свойство BaseRequest согласуется с клиентом."""
        self.assertEqual(
            self.get_updates_request.read_timeout,
            bot_module.POLLING_GET_UPDATES_READ_TIMEOUT,
        )
        self.assertEqual(self.request.read_timeout, bot_module.PROXY_READ_TIMEOUT)


@unittest.skipIf(IMPORT_ERROR is not None, f"импорт недоступен: {IMPORT_ERROR}")
class RunPollingBootstrapTest(unittest.TestCase):
    """main() передаёт в run_polling бесконечный retry bootstrap."""

    def _run_main_capturing_polling(self) -> dict:
        app = MagicMock()
        with (
            patch.object(bot_module, "setup_logging"),
            patch.object(bot_module, "logger"),
            patch.object(bot_module, "init_db"),
            patch.object(bot_module, "build_application", return_value=app),
        ):
            bot_module.main()

        app.run_polling.assert_called_once()
        return app.run_polling.call_args.kwargs

    def test_bootstrap_retries_passed_to_run_polling(self) -> None:
        """Без -1 сбой bootstrap при старте завершает процесс (инцидент)."""
        kwargs = self._run_main_capturing_polling()
        self.assertIn(
            "bootstrap_retries",
            kwargs,
            "run_polling вызван без bootstrap_retries — дефолт 0 не переживает "
            "сбой VPN-прокси при старте",
        )
        self.assertEqual(
            kwargs["bootstrap_retries"], bot_module.POLLING_BOOTSTRAP_RETRIES
        )
        self.assertEqual(kwargs["bootstrap_retries"], -1)

    def test_allowed_updates_still_explicit(self) -> None:
        """Регрессия P0: allowed_updates не потерян при добавлении retry."""
        kwargs = self._run_main_capturing_polling()
        self.assertEqual(kwargs["allowed_updates"], Update.ALL_TYPES)


if __name__ == "__main__":
    unittest.main()
