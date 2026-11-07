# tests/test_stage255_acceptance_d.py
"""
Poster 2.5.5, этап D — независимая приёмка пользовательских требований §9–§12
раздела «🛠 Репорты» административного кабинета.

Проверяется ТРЕБОВАНИЕ, а не устройство реализации: сценарии идут через
настоящее приложение (`Application.process_update`), FakeBot и временную
SQLite (`tests/integration_harness.py`); рабочая `post_bot.db`, `.env` и сеть
Telegram не используются. Ожидания формулировок берутся из действующего
каталога `config/ui_texts.yaml` через `tests/ui_texts_expect.py` (yaml —
канон).

Что закреплено.

А. Доступ (§9):
- обычный администратор площадки не видит строку «Репорты» в меню кабинета и
  получает alert-отказ на КАЖДЫЙ `rpts_*`-callback, экран не меняется;
- обычный пользователь — те же отказы;
- суперадмин проходит весь раздел;
- права перепроверяются на каждом действии, а не только на входе: подделанное
  состояние кабинета и отзыв роли посреди диалога не дают ни открыть карточку,
  ни отправить ответ.

Б. Меню и список (§9–§10):
- строка «🛠 Репорты» и число действующих в ОДНОЙ строке; кнопка числа —
  no-op (спиннер закрыт, экран и состояние не меняются);
- список: кнопка на каждый действующий репорт, название = раздел + @username
  (иначе имя, иначе ID), новые сверху; пагинация по пять, страницы листаются,
  смещение выравнивается при изменении списка;
- внизу «Архив» и «Назад».

В. Карточка (§10):
- автор, раздел, дата/время создания, полный текст; .txt-снимок отдельным
  сообщением; кнопки «Решено» и «Назад»;
- длинный текст не теряется: экран в лимите Telegram, сокращение помечено
  явно, в БД текст цел, снимок отправлен.

Г. Ответ (§11):
- «Решено» НЕ архивирует: «Напишите ответ пользователю.» + «Отмена»;
- текст ответа обычным сообщением → предпросмотр с «Отменить | Изменить |
  Отправить»;
- «Изменить» возвращает к вводу и сохраняет текст; «Отменить» возвращает к
  карточке, репорт остаётся действующим;
- отправка: автору на `reports.chat_id` уходит «Спасибо за репорт! Ответ
  разработчика:» с текстом (переносы сохранены), после доставки репорт
  архивный, счётчик уменьшился, карточка/список отражают новый статус;
- сбой доставки: репорт действующий, админу сообщено, повтор успешен;
- повторный `rpts_reply_send` не дублирует ответ;
- текст в другой фазе (список) не перехватывается как ответ.

Д. Два суперадмина (§11):
- репорт, закрытый в другом окне, не даёт второму отправить ответ; второй
  получает «уже закрыт» и архивную карточку с чужим ответом.

Е. Архив (§12):
- закрытые репорты с теми же названиями и пагинацией; карточка показывает
  ответ разработчика и дату закрытия; управления ответом нет;
- архивные не входят в счётчик действующих.

Ж. Гигиена журнала:
- события `admin_reports.*` пишутся, но ни текст ответа, ни текст жалобы в
  `detail` не попадают.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage255_acceptance_d -v
"""

from __future__ import annotations

import re
import unittest
from io import BytesIO
from unittest.mock import patch

from sqlalchemy import text as sqltext

try:
    import report_store
    from admin_state import load_admin_session, save_admin_session
    from tests import ui_texts_expect as cat
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage20_admin_flow import (
        callback_answers,
        insert_place,
        last_alert,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


SUPERADMIN_ID = 500
SECOND_SUPERADMIN_ID = 501
PLACE_ADMIN_ID = 9_115_001
PLAIN_USER_ID = 777_333
AUTHOR_ID = 7_500_001
AUTHOR_CHAT_ID = 880_001
SECOND_ADMIN_CHAT_ID = 900_777

REPORT_TEXT = "Нажал дату — календарь не открылся"
REPLY_TEXT = "Спасибо, поправили календарь"
SNAPSHOT_TEXT = "2026-11-07 10:00 | screen.open | ok"

DENIED_ALERT = cat.text("admin.module.dostup_administrativnomu_razdelu")
STALE_ALERT = cat.text("errors.stale.admin")

DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}")


# --- помощники -------------------------------------------------------------


def _seed_report(
    h,
    *,
    body: str = REPORT_TEXT,
    screen_label: str = "главное меню",
    author_user_id: int = AUTHOR_ID,
    username: str | None = "author",
    display_name: str | None = "Автор Репортов",
    chat_id: int = AUTHOR_CHAT_ID,
    events: str = SNAPSHOT_TEXT,
) -> int:
    """Строка репорта напрямую в БД (как её создаёт этап C)."""
    with h.session_factory() as session:
        report = report_store.create_report(
            session,
            author_user_id=author_user_id,
            chat_id=chat_id,
            body=body,
            screen_label=screen_label,
            events_snapshot=events,
            username=username,
            display_name=display_name,
        )
        return report.id


def _report(h, report_id: int):
    with h.session_factory() as session:
        return report_store.get_report(session, report_id)


def _make_place_admin(h, place_id: int, telegram_user_id: int) -> None:
    """Действующая роль администратора площадки для пользователя."""
    from sqlalchemy import select

    from models import PlaceAdmin, User
    from user_store import get_or_create_user

    with h.session_factory() as session:
        user = session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        if user is None:
            user = get_or_create_user(session, telegram_user_id, username="pa")
        session.add(PlaceAdmin(place_id=place_id, user_id=user.id))
        session.commit()


def _keyboard_rows(h) -> list[list[tuple[str, str | None]]]:
    """Фактическая клавиатура текущего экрана — строками (как у Telegram)."""
    record = h.screen()
    markup = record.get("reply_markup") if record else None
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [
        [(button.text, getattr(button, "callback_data", None)) for button in row]
        for row in markup.inline_keyboard
    ]


def _labels(h) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _callbacks(h) -> list[str | None]:
    return [data for _label, data in h.screen_buttons()]


def _documents(h) -> list[dict]:
    return [call for call in h.calls if call["type"] == "document"]


def _document_bytes(record: dict) -> bytes:
    document = record.get("document")
    if isinstance(document, BytesIO):
        return document.getvalue()
    raise AssertionError(f"вложение не BytesIO: {document!r}")


def _delivered(h, chat_id: int = AUTHOR_CHAT_ID) -> list[dict]:
    """Сообщения, реально ушедшие в чат автора репорта."""
    return [
        call
        for call in h.calls
        if call["type"] == "message" and call.get("chat_id") == chat_id
    ]


def _replies_to_author(h) -> list[dict]:
    marker = cat.fragment("admin.reports.user_reply")
    return [call for call in _delivered(h) if marker in (call.get("text") or "")]


def _journal(h) -> list[tuple]:
    with h.session_factory() as session:
        return session.execute(
            sqltext(
                "SELECT action, screen, result, detail FROM user_event_log "
                "WHERE action LIKE 'admin_reports.%' ORDER BY id"
            )
        ).all()


async def _open_reports(
    h, *, user_id: int = SUPERADMIN_ID, chat_id: int | None = None
) -> None:
    """Кабинет → «🛠 Репорты»."""
    await h.text("/admin", user_id=user_id, chat_id=chat_id)
    await h.callback("rpts_list", user_id=user_id, chat_id=chat_id)


async def _open_card(
    h, report_id: int, *, user_id: int = SUPERADMIN_ID, chat_id: int | None = None
) -> None:
    await _open_reports(h, user_id=user_id, chat_id=chat_id)
    await h.callback(f"rpts_card:{report_id}", user_id=user_id, chat_id=chat_id)


async def _start_reply(
    h, report_id: int, *, user_id: int = SUPERADMIN_ID, chat_id: int | None = None
) -> None:
    await _open_card(h, report_id, user_id=user_id, chat_id=chat_id)
    await h.callback(f"rpts_reply:{report_id}", user_id=user_id, chat_id=chat_id)


class ReportsAcceptanceBase(unittest.TestCase):
    """Оба суперадмина активны на время сценария."""

    def setUp(self) -> None:
        patcher = patch(
            "config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID, SECOND_SUPERADMIN_ID)
        )
        patcher.start()
        self.addCleanup(patcher.stop)


# --- А. Доступ -------------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsAccessTests(ReportsAcceptanceBase):
    """§9: раздел закрыт для всех, кроме суперадмина; права — на каждом шаге."""

    @scenario_test
    async def test_place_admin_has_no_entry_and_gets_alerts(self, h) -> None:
        place_id = insert_place(h, "Своё место")
        _make_place_admin(h, place_id, PLACE_ADMIN_ID)
        report_id = _seed_report(h)

        await h.text("/admin", user_id=PLACE_ADMIN_ID)
        cabinet_text = h.screen_text()
        rows = _keyboard_rows(h)
        self.assertNotIn(
            cat.text("buttons.reports.open"),
            _labels(h),
            f"админ площадки видит вход в «Репорты»: {rows}",
        )
        self.assertFalse(
            [data for data in _callbacks(h) if data and data.startswith("rpts_")],
            f"админ площадки видит rpts_-кнопки: {rows}",
        )

        # Подделанные callback — отказ на КАЖДОМ действии раздела.
        for data in (
            "rpts_list",
            f"rpts_card:{report_id}",
            "rpts_page:0",
            "rpts_archive",
            "rpts_archive_page:0",
            f"rpts_acard:{report_id}",
            f"rpts_reply:{report_id}",
            "rpts_reply_change",
            "rpts_reply_cancel",
            "rpts_reply_send",
            "rpts_back",
            "rpts_cabinet",
        ):
            with self.subTest(data=data):
                before = len(callback_answers(h))
                await h.callback(data, user_id=PLACE_ADMIN_ID)
                self.assertGreater(len(callback_answers(h)), before)
                self.assertEqual(last_alert(h), DENIED_ALERT)
                self.assertNotEqual(
                    h.screen_text(),
                    cat.text("admin.reports.title"),
                    "экран раздела открыт по подделанному callback",
                )
        # Кабинет не сломан: отказы alert'ом, экран «Мои места» тот же.
        self.assertEqual(h.screen_text(), cabinet_text)
        self.assertTrue(_report(h, report_id).is_active)
        self.assertFalse(_delivered(h))

    @scenario_test
    async def test_plain_user_is_denied(self, h) -> None:
        report_id = _seed_report(h)
        screen_before = h.screen_text()
        for data in (
            "rpts_list",
            f"rpts_card:{report_id}",
            "rpts_archive",
            f"rpts_reply:{report_id}",
            "rpts_reply_send",
        ):
            with self.subTest(data=data):
                before = len(callback_answers(h))
                await h.callback(data, user_id=PLAIN_USER_ID)
                self.assertEqual(len(callback_answers(h)), before + 1)
                self.assertEqual(last_alert(h), DENIED_ALERT)
                self.assertEqual(
                    h.screen_text(), screen_before, "экран раздела открыт постороннему"
                )
        self.assertTrue(_report(h, report_id).is_active)
        self.assertFalse(_delivered(h))

    @scenario_test
    async def test_superadmin_has_full_access(self, h) -> None:
        insert_place(h, "Кофейня")
        report_id = _seed_report(h)

        await h.text("/admin", user_id=SUPERADMIN_ID)
        self.assertIn(cat.text("buttons.reports.open"), _labels(h))

        await h.callback("rpts_list", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.title")
        self.assertIn(f"rpts_card:{report_id}", _callbacks(h))

        await h.callback(f"rpts_card:{report_id}", user_id=SUPERADMIN_ID)
        cat.assert_shows(
            self, h.screen_text(), "admin.reports.card_title", report_id=report_id
        )

        await h.callback(f"rpts_reply:{report_id}", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.reply_prompt")

        await h.text(REPLY_TEXT, user_id=SUPERADMIN_ID)
        self.assertIn(
            cat.text("buttons.reports.reply_send"),
            _labels(h),
            "суперадмин не дошёл до предпросмотра ответа",
        )

        await h.callback("rpts_reply_cancel", user_id=SUPERADMIN_ID)
        self.assertTrue(_report(h, report_id).is_active)

        await h.callback("rpts_back", user_id=SUPERADMIN_ID)
        await h.callback("rpts_archive", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.archive_title")

    @scenario_test
    async def test_rights_checked_on_each_callback_not_only_on_entry(self, h) -> None:
        """Подделанное состояние кабинета не заменяет проверку роли."""
        insert_place(h, "Чужое место")
        report_id = _seed_report(h)

        # Админ площадки «вручную» получает состояние раздела из БД: вход мимо
        # UI не должен дать ни карточки, ни отправки ответа.
        save_admin_session(
            PLACE_ADMIN_ID,
            {
                "phase": "report_reply_preview",
                "report_id": report_id,
                "report_reply": "Взломанный ответ",
            },
        )
        for data in (
            f"rpts_card:{report_id}",
            f"rpts_reply:{report_id}",
            "rpts_reply_change",
            "rpts_reply_send",
            "rpts_archive",
        ):
            with self.subTest(data=data):
                await h.callback(data, user_id=PLACE_ADMIN_ID)
                self.assertEqual(last_alert(h), DENIED_ALERT)
        self.assertTrue(_report(h, report_id).is_active)
        self.assertIsNone(_report(h, report_id).developer_reply)
        self.assertFalse(_replies_to_author(h))

        # Отзыв роли посреди диалога: отправка не проходит.
        await _start_reply(h, report_id)
        await h.text(REPLY_TEXT, user_id=SUPERADMIN_ID)
        self.assertIn(cat.text("buttons.reports.reply_send"), _labels(h))
        with patch("config.SUPERADMIN_USER_IDS", ()):
            await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        self.assertEqual(last_alert(h), DENIED_ALERT)
        self.assertTrue(_report(h, report_id).is_active)
        self.assertFalse(_replies_to_author(h))


# --- Б. Меню кабинета и список --------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsEntryTests(ReportsAcceptanceBase):
    """§9: строка «Репорты | N» в меню кабинета и no-op счётчик."""

    @scenario_test
    async def test_entry_and_counter_share_one_row(self, h) -> None:
        insert_place(h, "Кофейня")
        _seed_report(h)
        _seed_report(h, body="Вторая жалоба", author_user_id=AUTHOR_ID + 1)

        await h.text("/admin", user_id=SUPERADMIN_ID)

        rows = _keyboard_rows(h)
        entry_rows = [row for row in rows if any(d == "rpts_list" for _t, d in row)]
        self.assertEqual(len(entry_rows), 1, f"нет строки входа: {rows}")
        row = entry_rows[0]
        self.assertEqual(len(row), 2, f"счётчик не в строке входа: {row}")
        self.assertEqual(row[0][0], cat.text("buttons.reports.open"))
        self.assertEqual(row[0][1], "rpts_list")
        self.assertEqual(row[1][0], cat.text("buttons.reports.counter", count=2))
        self.assertIsNotNone(row[1][1])
        self.assertTrue(
            str(row[1][1]).startswith("noop:"),
            f"счётчик не инертный: {row[1][1]!r}",
        )

    @scenario_test
    async def test_counter_is_noop(self, h) -> None:
        insert_place(h, "Кофейня")
        _seed_report(h)
        await h.text("/admin", user_id=SUPERADMIN_ID)

        counter_data = next(
            data
            for label, data in h.screen_buttons()
            if label == cat.text("buttons.reports.counter", count=1)
        )
        screen_before = h.screen_text()
        state_before = load_admin_session(SUPERADMIN_ID)
        calls_before = len(h.calls)

        await h.callback(counter_data, user_id=SUPERADMIN_ID)

        self.assertEqual(h.screen_text(), screen_before)
        self.assertEqual(load_admin_session(SUPERADMIN_ID), state_before)
        self.assertEqual(
            [call["type"] for call in h.calls[calls_before:]],
            ["callback_answer"],
            "нажатие счётчика что-то сделало",
        )

    @scenario_test
    async def test_counter_counts_only_active_reports(self, h) -> None:
        insert_place(h, "Кофейня")
        _seed_report(h)
        archived_id = _seed_report(h, body="Уже закрыт")
        with h.session_factory() as session:
            report_store.archive_report(session, archived_id, reply="Готово")

        await h.text("/admin", user_id=SUPERADMIN_ID)
        self.assertIn(cat.text("buttons.reports.counter", count=1), _labels(h))
        self.assertNotIn(cat.text("buttons.reports.counter", count=2), _labels(h))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsListTests(ReportsAcceptanceBase):
    """§9: список действующих репортов, подписи, порядок, пагинация, низ."""

    @scenario_test
    async def test_list_labels_order_and_bottom_rows(self, h) -> None:
        first = _seed_report(h, screen_label="конструктор анонса", username="author")
        second = _seed_report(
            h,
            body="Второй",
            screen_label="шаблоны",
            username=None,
            display_name="Без Ника",
            author_user_id=AUTHOR_ID + 1,
        )
        third = _seed_report(
            h,
            body="Третий",
            screen_label="места",
            username=None,
            display_name=None,
            author_user_id=AUTHOR_ID + 2,
        )

        await _open_reports(h)

        cat.assert_shows(self, h.screen_text(), "admin.reports.active_total", total=3)
        item_rows = [
            (label, data)
            for label, data in h.screen_buttons()
            if data and data.startswith("rpts_card:")
        ]
        # Новые к старым.
        self.assertEqual(
            [data for _label, data in item_rows],
            [f"rpts_card:{third}", f"rpts_card:{second}", f"rpts_card:{first}"],
        )
        # Раздел — @username / имя / ID.
        self.assertEqual(
            item_rows[0][0],
            cat.text(
                "buttons.reports.item",
                screen_label="места",
                author_label=str(AUTHOR_ID + 2),
            ),
        )
        self.assertEqual(
            item_rows[1][0],
            cat.text(
                "buttons.reports.item",
                screen_label="шаблоны",
                author_label="Без Ника",
            ),
        )
        self.assertEqual(
            item_rows[2][0],
            cat.text(
                "buttons.reports.item",
                screen_label="конструктор анонса",
                author_label="@author",
            ),
        )

        # Внизу «Архив» и «Назад» — по одной кнопке в строке.
        rows = _keyboard_rows(h)
        self.assertEqual(
            rows[-2:],
            [
                [(cat.text("buttons.reports.archive"), "rpts_archive")],
                [(cat.text("buttons.nav.back"), "rpts_cabinet")],
            ],
        )

    @scenario_test
    async def test_pagination_and_clamp_when_list_shrinks(self, h) -> None:
        ids = [_seed_report(h, body=f"Жалоба {index}") for index in range(7)]

        await _open_reports(h)

        first_page = [
            data for data in _callbacks(h) if data and data.startswith("rpts_card:")
        ]
        self.assertEqual(len(first_page), 5, f"страница не по пять: {first_page}")
        labels = _labels(h)
        self.assertIn("1/2", labels)
        self.assertNotIn(cat.text("buttons.pagination.prev"), labels)
        self.assertIn("rpts_page:5", _callbacks(h))

        await h.callback("rpts_page:5", user_id=SUPERADMIN_ID)
        second_page = [
            data for data in _callbacks(h) if data and data.startswith("rpts_card:")
        ]
        self.assertEqual(len(second_page), 2)
        self.assertIn("2/2", _labels(h))
        self.assertIn("rpts_page:0", _callbacks(h))
        self.assertNotIn(cat.text("buttons.pagination.next"), _labels(h))

        # Смещение за пределами списка — последняя страница, а не пустой экран.
        await h.callback("rpts_page:100", user_id=SUPERADMIN_ID)
        self.assertEqual(
            [data for data in _callbacks(h) if data and data.startswith("rpts_card:")],
            second_page,
        )

        # Список сократился: смещение выравнивается, страница не пустая.
        with h.session_factory() as session:
            for report_id in ids[:4]:
                report_store.archive_report(session, report_id, reply="Закрыто")
        await h.callback("rpts_page:5", user_id=SUPERADMIN_ID)

        remaining = [
            data for data in _callbacks(h) if data and data.startswith("rpts_card:")
        ]
        self.assertEqual(len(remaining), 3, f"страница ушла в пустоту: {remaining}")
        cat.assert_shows(self, h.screen_text(), "admin.reports.active_total", total=3)
        self.assertFalse(
            [data for data in _callbacks(h) if data and data.startswith("rpts_page:")],
            "пагинация осталась у списка из трёх записей",
        )
        state = load_admin_session(SUPERADMIN_ID) or {}
        self.assertEqual(state.get("reports_offset"), 0)


# --- В. Карточка -----------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsCardTests(ReportsAcceptanceBase):
    """§10: карточка действующего репорта, .txt-снимок, «Решено»/«Назад»."""

    @scenario_test
    async def test_card_shows_author_section_date_body_and_snapshot(self, h) -> None:
        report_id = _seed_report(h)
        await _open_card(h, report_id)

        screen = h.screen_text()
        cat.assert_shows(self, screen, "admin.reports.card_title", report_id=report_id)
        cat.assert_shows(self, screen, "admin.reports.author", author_label="@author")
        self.assertIn(str(AUTHOR_ID), screen)
        cat.assert_shows(
            self, screen, "admin.reports.section", screen_label="главное меню"
        )
        self.assertRegex(screen, DATE_RE, "в карточке нет даты и времени создания")
        cat.assert_shows(self, screen, "admin.reports.body", body=REPORT_TEXT)

        self.assertEqual(
            _keyboard_rows(h),
            [
                [(cat.text("buttons.reports.resolved"), f"rpts_reply:{report_id}")],
                [(cat.text("buttons.nav.back"), "rpts_back")],
            ],
        )

        documents = _documents(h)
        self.assertEqual(len(documents), 1, "нет .txt-снимка отдельным сообщением")
        self.assertEqual(_document_bytes(documents[0]).decode("utf-8"), SNAPSHOT_TEXT)
        self.assertRegex(
            str(documents[0].get("filename") or ""), r"^report_\d{8}_\d{6}\.txt$"
        )
        # Снимок — отдельное сообщение админу, а не правка экрана.
        self.assertEqual(documents[0].get("chat_id"), h.chat_id)

    @scenario_test
    async def test_long_body_is_not_silently_lost(self, h) -> None:
        # Текст в пределах формы /report (3500) виден полностью.
        plain_body = "Ж" * 3500
        plain_id = _seed_report(h, body=plain_body)
        await _open_card(h, plain_id)
        screen = h.screen_text()
        self.assertLessEqual(len(screen), 4096, "экран длиннее лимита Telegram")
        self.assertIn(plain_body, screen, "текст репорта потерян без пометки")
        self.assertEqual(_report(h, plain_id).body, plain_body)

        # Текст, который после экранирования HTML не влезает: сокращение
        # помечено явно, в БД он цел, снимок отправлен.
        markup_body = ("<b>x</b> & " * 300 + "конец")[:3500]
        markup_id = _seed_report(h, body=markup_body)
        documents_before = len(_documents(h))
        await h.text("/admin", user_id=SUPERADMIN_ID)
        await h.callback("rpts_list", user_id=SUPERADMIN_ID)
        await h.callback(f"rpts_card:{markup_id}", user_id=SUPERADMIN_ID)

        screen = h.screen_text()
        self.assertLessEqual(len(screen), 4096)
        self.assertIn(
            cat.fragment("screen.module.pokazano_nachalo_znacheniya_ekran"),
            screen,
            "сокращение текста не помечено",
        )
        self.assertEqual(_report(h, markup_id).body, markup_body)
        self.assertEqual(len(_documents(h)), documents_before + 1)


# --- Г. Ответ --------------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsReplyTests(ReportsAcceptanceBase):
    """§11: ввод, предпросмотр, изменение, отмена и доставка ответа."""

    @scenario_test
    async def test_resolved_does_not_archive_and_prompts_for_reply(self, h) -> None:
        report_id = _seed_report(h)
        await _open_card(h, report_id)

        await h.callback(f"rpts_reply:{report_id}", user_id=SUPERADMIN_ID)

        cat.assert_shows(self, h.screen_text(), "admin.reports.reply_prompt")
        self.assertEqual(
            _keyboard_rows(h),
            [[(cat.text("buttons.reports.reply_cancel"), "rpts_reply_cancel")]],
        )
        report = _report(h, report_id)
        self.assertTrue(report.is_active, "«Решено» закрыло репорт без ответа")
        self.assertIsNone(report.developer_reply)

    @scenario_test
    async def test_reply_text_leads_to_preview(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)

        await h.text(REPLY_TEXT, user_id=SUPERADMIN_ID)

        cat.assert_shows(
            self, h.screen_text(), "admin.reports.reply_preview", reply=REPLY_TEXT
        )
        self.assertEqual(
            [(label, data) for label, data in h.screen_buttons()],
            [
                (cat.text("buttons.reports.reply_cancel"), "rpts_reply_cancel"),
                (cat.text("buttons.reports.reply_change"), "rpts_reply_change"),
                (cat.text("buttons.reports.reply_send"), "rpts_reply_send"),
            ],
        )
        self.assertTrue(_report(h, report_id).is_active)

    @scenario_test
    async def test_change_keeps_text_and_cancel_returns_to_card(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text("Первый вариант", user_id=SUPERADMIN_ID)

        await h.callback("rpts_reply_change", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.reply_prompt_change")
        self.assertEqual(
            _keyboard_rows(h),
            [[(cat.text("buttons.reports.reply_cancel"), "rpts_reply_cancel")]],
        )
        # Введённый текст сохранён и не потерян при возврате к вводу.
        self.assertEqual(
            (load_admin_session(SUPERADMIN_ID) or {}).get("report_reply"),
            "Первый вариант",
        )

        # Пустой ввод не затирает сохранённый текст.
        await h.text("   ", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.reply_empty")
        self.assertEqual(
            (load_admin_session(SUPERADMIN_ID) or {}).get("report_reply"),
            "Первый вариант",
        )

        # Новый текст заменяет предыдущий.
        await h.text("Второй вариант", user_id=SUPERADMIN_ID)
        cat.assert_shows(
            self, h.screen_text(), "admin.reports.reply_preview", reply="Второй вариант"
        )

        # «Отменить» — назад к карточке, репорт действующий, ответ не ушёл.
        await h.callback("rpts_reply_cancel", user_id=SUPERADMIN_ID)
        cat.assert_shows(
            self, h.screen_text(), "admin.reports.card_title", report_id=report_id
        )
        self.assertEqual(_callbacks(h), [f"rpts_reply:{report_id}", "rpts_back"])
        report = _report(h, report_id)
        self.assertTrue(report.is_active)
        self.assertIsNone(report.developer_reply)
        self.assertFalse(_replies_to_author(h))

    @scenario_test
    async def test_empty_and_too_long_reply_are_refused(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)

        await h.text("   ", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.reply_empty")
        self.assertEqual(_callbacks(h), ["rpts_reply_cancel"])

        limit = 3500
        await h.text("Ж" * (limit + 10), user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "report.screen.too_long", limit=limit)
        self.assertEqual(_callbacks(h), ["rpts_reply_cancel"])
        report = _report(h, report_id)
        self.assertTrue(report.is_active)
        self.assertIsNone(report.developer_reply)

    @scenario_test
    async def test_send_delivers_to_author_and_archives(self, h) -> None:
        # chat_id репорта — отдельный чат автора, а не чат администратора.
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text("Первая строка\nВторая строка", user_id=SUPERADMIN_ID)

        # Предпросмотр не отправляет и не закрывает.
        self.assertTrue(_report(h, report_id).is_active)
        self.assertFalse(_replies_to_author(h))

        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)

        delivered = _replies_to_author(h)
        self.assertEqual(len(delivered), 1, f"нет ответа автору: {delivered}")
        self.assertEqual(delivered[0].get("chat_id"), AUTHOR_CHAT_ID)
        # Адресат — chat_id из репорта, а не чат администратора.
        self.assertFalse(
            [
                call
                for call in _delivered(h, h.chat_id)
                if cat.fragment("admin.reports.user_reply") in (call.get("text") or "")
            ],
            "ответ ушёл в чат администратора вместо автора репорта",
        )
        self.assertEqual(
            delivered[0]["text"],
            cat.text(
                "admin.reports.user_reply",
                reply="Первая строка\nВторая строка",
            ),
        )
        self.assertIn("Первая строка\nВторая строка", delivered[0]["text"])

        archived = _report(h, report_id)
        self.assertTrue(archived.is_archived)
        self.assertEqual(archived.developer_reply, "Первая строка\nВторая строка")
        self.assertIsNotNone(archived.closed_at)

        # Экран после успеха — архивная карточка с ответом, без управления.
        screen = h.screen_text()
        cat.assert_shows(
            self,
            screen,
            "admin.reports.reply_label",
            reply="Первая строка\nВторая строка",
        )
        self.assertNotIn("rpts_reply_send", _callbacks(h))
        cat.assert_shows(self, screen, "admin.reports.reply_sent")

        # Счётчик в кабинете уменьшился; архив показывает запись и её ответ.
        await h.callback("rpts_archive", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.archive_total", total=1)
        await h.callback(f"rpts_acard:{report_id}", user_id=SUPERADMIN_ID)
        cat.assert_shows(
            self,
            h.screen_text(),
            "admin.reports.reply_label",
            reply="Первая строка\nВторая строка",
        )

        await h.callback("rpts_archive", user_id=SUPERADMIN_ID)
        await h.callback("rpts_cabinet", user_id=SUPERADMIN_ID)
        self.assertIn(cat.text("buttons.reports.counter", count=0), _labels(h))
        await h.callback("rpts_list", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.empty")

    @scenario_test
    async def test_text_in_list_phase_is_not_a_reply(self, h) -> None:
        report_id = _seed_report(h)
        await _open_reports(h)

        before_state = dict(load_admin_session(SUPERADMIN_ID) or {})
        await h.text("Это не ответ разработчика", user_id=SUPERADMIN_ID)

        self.assertNotIn(
            cat.fragment("admin.reports.reply_preview"),
            h.screen_text(),
            "текст в фазе списка принят как ответ",
        )
        self.assertNotIn(
            "rpts_reply_send", _callbacks(h), "в списке появилось управление ответом"
        )
        state = load_admin_session(SUPERADMIN_ID) or {}
        self.assertEqual(state.get("phase"), before_state.get("phase"))
        self.assertNotIn("report_reply", state)
        self.assertTrue(_report(h, report_id).is_active)
        self.assertIsNone(_report(h, report_id).developer_reply)
        self.assertFalse(_replies_to_author(h))

    @scenario_test
    async def test_journal_has_no_report_or_reply_text(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text(REPLY_TEXT, user_id=SUPERADMIN_ID)
        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)

        rows = _journal(h)
        actions = {row[0] for row in rows}
        for action in (
            "admin_reports.list_opened",
            "admin_reports.report_opened",
            "admin_reports.reply_started",
            "admin_reports.reply_sent",
        ):
            self.assertIn(action, actions, f"нет события {action}")
        for action, screen, _result, detail in rows:
            with self.subTest(action=action):
                self.assertEqual(screen, "admin_reports")
                self.assertNotIn(REPLY_TEXT, detail or "", "текст ответа в журнале")
                self.assertNotIn(REPORT_TEXT, detail or "", "текст жалобы в журнале")


# --- Д. Сбой доставки и повтор --------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsDeliveryFailureTests(ReportsAcceptanceBase):
    """§11: сбой доставки не закрывает репорт; повтор не дублирует успех."""

    @scenario_test
    async def test_failure_keeps_report_active_and_retry_succeeds(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text(REPLY_TEXT, user_id=SUPERADMIN_ID)

        h.bot.fail_types.add("message")
        try:
            await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        finally:
            h.bot.fail_types.discard("message")

        self.assertEqual(last_alert(h), cat.text("admin.reports.reply_failed"))
        report = _report(h, report_id)
        self.assertTrue(report.is_active, "репорт закрыт без доставки")
        self.assertIsNone(report.developer_reply)
        self.assertFalse(_replies_to_author(h))
        # Экран остаётся предпросмотром — повтор возможен.
        self.assertIn("rpts_reply_send", _callbacks(h))

        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)

        delivered = _replies_to_author(h)
        self.assertEqual(len(delivered), 1, "повторная отправка не сработала")
        self.assertEqual(
            delivered[0]["text"], cat.text("admin.reports.user_reply", reply=REPLY_TEXT)
        )
        archived = _report(h, report_id)
        self.assertTrue(archived.is_archived)
        self.assertEqual(archived.developer_reply, REPLY_TEXT)

    @scenario_test
    async def test_repeat_send_after_success_does_not_duplicate(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text(REPLY_TEXT, user_id=SUPERADMIN_ID)

        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        self.assertEqual(len(_replies_to_author(h)), 1)

        # Двойной клик/повтор update после успеха: ответа номер два быть не
        # должно, статус и сохранённый ответ не меняются.
        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)

        self.assertEqual(len(_replies_to_author(h)), 1, "ответ продублирован")
        report = _report(h, report_id)
        self.assertTrue(report.is_archived)
        self.assertEqual(report.developer_reply, REPLY_TEXT)


# --- Е. Два суперадмина ----------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsSecondSuperadminTests(ReportsAcceptanceBase):
    """§11: закрытый в другом окне репорт нельзя закрыть повторно."""

    @scenario_test
    async def test_second_superadmin_resolved_after_external_close(self, h) -> None:
        report_id = _seed_report(h)
        # Второй суперадмин открыл карточку в своём чате.
        await _open_card(
            h, report_id, user_id=SECOND_SUPERADMIN_ID, chat_id=SECOND_ADMIN_CHAT_ID
        )
        self.assertEqual(_callbacks(h), [f"rpts_reply:{report_id}", "rpts_back"])

        # Первый закрыл репорт из другого окна.
        with h.session_factory() as session:
            report_store.archive_report(session, report_id, reply="Закрыто первым")

        await h.callback(
            f"rpts_reply:{report_id}",
            user_id=SECOND_SUPERADMIN_ID,
            chat_id=SECOND_ADMIN_CHAT_ID,
        )

        self.assertEqual(last_alert(h), cat.text("admin.reports.already_closed"))
        screen = h.screen_text()
        cat.assert_shows(
            self, screen, "admin.reports.reply_label", reply="Закрыто первым"
        )
        self.assertNotIn(
            "rpts_reply_send",
            _callbacks(h),
            "второй суперадмин получил управление ответом",
        )
        self.assertFalse(
            [d for d in _callbacks(h) if d and d.startswith("rpts_reply:")]
        )
        self.assertFalse(_replies_to_author(h))

    @scenario_test
    async def test_send_after_external_close_is_refused(self, h) -> None:
        report_id = _seed_report(h)
        # Второй дошёл до предпросмотра в своём чате.
        await _start_reply(
            h, report_id, user_id=SECOND_SUPERADMIN_ID, chat_id=SECOND_ADMIN_CHAT_ID
        )
        await h.text(
            REPLY_TEXT, user_id=SECOND_SUPERADMIN_ID, chat_id=SECOND_ADMIN_CHAT_ID
        )

        with h.session_factory() as session:
            report_store.archive_report(session, report_id, reply="Ответ первого")

        await h.callback(
            "rpts_reply_send",
            user_id=SECOND_SUPERADMIN_ID,
            chat_id=SECOND_ADMIN_CHAT_ID,
        )

        self.assertEqual(last_alert(h), cat.text("admin.reports.already_closed"))
        self.assertFalse(_replies_to_author(h), "ответ ушёл в закрытый репорт")
        report = _report(h, report_id)
        self.assertEqual(report.developer_reply, "Ответ первого")
        self.assertTrue(report.is_archived)

        # Повторное нажатие тоже ничего не отправляет.
        await h.callback(
            "rpts_reply_send",
            user_id=SECOND_SUPERADMIN_ID,
            chat_id=SECOND_ADMIN_CHAT_ID,
        )
        self.assertFalse(_replies_to_author(h))

    @scenario_test
    async def test_refusal_leaves_admin_on_archive_card(self, h) -> None:
        """
        §11: после отказа «уже закрыт» второй суперадмин видит архивную
        карточку с ответом первого и датой закрытия, а не приглашение писать
        ответ в закрытый репорт.
        """
        report_id = _seed_report(h)
        await _start_reply(
            h, report_id, user_id=SECOND_SUPERADMIN_ID, chat_id=SECOND_ADMIN_CHAT_ID
        )
        await h.text(
            REPLY_TEXT, user_id=SECOND_SUPERADMIN_ID, chat_id=SECOND_ADMIN_CHAT_ID
        )
        with h.session_factory() as session:
            report_store.archive_report(session, report_id, reply="Ответ первого")

        await h.callback(
            "rpts_reply_send",
            user_id=SECOND_SUPERADMIN_ID,
            chat_id=SECOND_ADMIN_CHAT_ID,
        )

        self.assertEqual(last_alert(h), cat.text("admin.reports.already_closed"))
        screen = h.screen_text()
        cat.assert_shows(
            self, screen, "admin.reports.reply_label", reply="Ответ первого"
        )
        cat.assert_not_shows(self, screen, "admin.reports.reply_prompt")
        self.assertNotIn("rpts_reply_send", _callbacks(h))
        self.assertNotIn("rpts_reply_change", _callbacks(h))


# --- Ж. Архив --------------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsArchiveTests(ReportsAcceptanceBase):
    """§12: архив — те же названия и пагинация, карточка только для чтения."""

    @scenario_test
    async def test_archive_list_and_card(self, h) -> None:
        closed_id = _seed_report(h, screen_label="места", username="closed_author")
        active_id = _seed_report(h, body="Ещё действующий")
        with h.session_factory() as session:
            report_store.archive_report(session, closed_id, reply="Исправлено")

        await _open_reports(h)
        await h.callback("rpts_archive", user_id=SUPERADMIN_ID)

        cat.assert_shows(self, h.screen_text(), "admin.reports.archive_title")
        cat.assert_shows(self, h.screen_text(), "admin.reports.archive_total", total=1)
        self.assertEqual(
            _keyboard_rows(h),
            [
                [
                    (
                        cat.text(
                            "buttons.reports.item",
                            screen_label="места",
                            author_label="@closed_author",
                        ),
                        f"rpts_acard:{closed_id}",
                    )
                ],
                [
                    (cat.text("buttons.nav.back"), "rpts_back"),
                    (cat.text("buttons.reports.cabinet"), "rpts_cabinet"),
                ],
            ],
        )

        await h.callback(f"rpts_acard:{closed_id}", user_id=SUPERADMIN_ID)

        screen = h.screen_text()
        cat.assert_shows(self, screen, "admin.reports.card_title", report_id=closed_id)
        cat.assert_shows(self, screen, "admin.reports.reply_label", reply="Исправлено")
        cat.assert_shows(self, screen, "admin.reports.closed")
        cat.assert_shows(self, screen, "admin.reports.body", body=REPORT_TEXT)
        self.assertRegex(screen, DATE_RE, "в архивной карточке нет даты закрытия")
        # Управления ответом нет: только возврат в архив.
        self.assertEqual(_keyboard_rows(h)[0][0][1], "rpts_archive")
        self.assertEqual(_callbacks(h), ["rpts_archive"])
        self.assertEqual(len(_documents(h)), 1, "нет .txt-снимка архивного репорта")

        # Действующий репорт из архива не открывается.
        await h.callback("rpts_archive", user_id=SUPERADMIN_ID)
        await h.callback(f"rpts_acard:{active_id}", user_id=SUPERADMIN_ID)
        self.assertEqual(last_alert(h), cat.text("admin.reports.missing"))
        await h.callback("rpts_acard:999999", user_id=SUPERADMIN_ID)
        self.assertEqual(last_alert(h), cat.text("admin.reports.missing"))

    @scenario_test
    async def test_archived_reports_are_not_counted_as_active(self, h) -> None:
        for _ in range(3):
            _seed_report(h, body="Действующий")
        closed = _seed_report(h, body="Закрытый")
        with h.session_factory() as session:
            report_store.archive_report(session, closed, reply="Готово")

        await h.text("/admin", user_id=SUPERADMIN_ID)
        self.assertIn(cat.text("buttons.reports.counter", count=3), _labels(h))

        await h.callback("rpts_list", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.active_total", total=3)
        self.assertEqual(
            len([d for d in _callbacks(h) if d and d.startswith("rpts_card:")]), 3
        )
        self.assertNotIn(f"rpts_card:{closed}", _callbacks(h))

    @scenario_test
    async def test_archive_pagination(self, h) -> None:
        with h.session_factory() as session:
            for index in range(7):
                report_id = _seed_report(h, body=f"Закрытая {index}")
                report_store.archive_report(session, report_id, reply="Готово")

        await _open_reports(h)
        await h.callback("rpts_archive", user_id=SUPERADMIN_ID)

        rows = [
            data for data in _callbacks(h) if data and data.startswith("rpts_acard:")
        ]
        self.assertEqual(len(rows), 5)
        self.assertIn("1/2", _labels(h))
        self.assertIn("rpts_archive_page:5", _callbacks(h))

        await h.callback("rpts_archive_page:5", user_id=SUPERADMIN_ID)
        rows = [
            data for data in _callbacks(h) if data and data.startswith("rpts_acard:")
        ]
        self.assertEqual(len(rows), 2)
        self.assertIn("2/2", _labels(h))
        self.assertIn("rpts_archive_page:0", _callbacks(h))
