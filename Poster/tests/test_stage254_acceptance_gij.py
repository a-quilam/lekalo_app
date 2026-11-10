# tests/test_stage254_acceptance_gij.py
r"""
Poster 2.5.4 — НЕЗАВИСИМАЯ приёмка наборов G, I, J.

Проверяется пользовательский контракт, а не реализация. Ожидания сняты с
требований владельца (спецификация 2.5.4 §4, §6, §8) и записаны здесь до
сверки с кодом:

G. «Мои анонсы» (§6) и клавиатура площадок (§4):
   - подпись записи — `Заголовок — Место (состояние)`: на первом месте
     название мероприятия, затем место и простое состояние;
   - состояние только `готов` (`publication_status=sent`) или `черновик`
     (любое другое, включая технические `sending`/`partial`/`failed`);
   - пустой/пробельный заголовок — `Без названия`; анонс по личному шаблону
     без площадки показывает имя личного шаблона;
   - заголовок нормализуется (пробелы и переводы строк схлопываются), текст
     экрана экранируется как HTML, подпись кнопки — обычный текст;
   - длинный заголовок сокращается, место и состояние остаются видны;
   - в кнопках списка нет номера и эмодзи «▶️» (номер остаётся в тексте);
   - корзина 🗑 работает только с подтверждением, «отмена» сохраняет запись,
     чужой анонс не открывается и не удаляется;
   - пагинация 5 записей на страницу, возврат из подтверждения сохраняет
     страницу, недостижимая страница клампится, удаление последней записи
     страницы возвращает на предыдущую, а не в пустоту;
   - устаревшая кнопка `aw_open:` удалённого анонса даёт понятный ответ;
   - в хабе «📍 Места» и в выборе площадки при создании анонса «поиск» и
     «добавить» стоят ОДНОЙ строкой, навигация не сломана.

I. Polling и VPN-прокси (§8):
   - обычные HTTP-вызовы и `getUpdates` имеют ЯВНЫЕ таймауты 15/15/15/5 с
     (дефолт PTB 22.8 — 5/5/5/1 с тесен для TLS через прокси);
   - таймауты действительно попадают в собранные `HTTPXRequest` (разные
     объекты для обычных вызовов и long-polling);
   - приложение собирается без сети, `Updater` не запущен, обработчики на
     месте;
   - `run_polling` получает `bootstrap_retries=-1` (дефолт 0 убивал процесс
     при первом сбое TLS на старте) и явный `allowed_updates` (регрессия P0).

J. Сквозной маршрут автора:
   создание → публикация → «Мои анонсы» с новым форматом → правка поля
   in-place (без новых сообщений) → слишком длинный текст → отмена возвращает
   значение → повторное открытие даёт НОВЫЙ экземпляр, прежний остаётся
   архивным без кнопок → `/help` из конструктора не теряет шаг (регрессия
   исправленного F1) → рестарт службы сохраняет resume и управление.
   Негативные: текст из чужого чата не меняет анонс, устаревшие callback'и
   старого экземпляра отвечают отказом и ничего не пишут.

Сценарии идут через настоящее приложение (`bot.build_application`), FakeBot и
временную SQLite (`tests/integration_harness`): реальная `post_bot.db`,
`.env`, секреты и сеть Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage254_acceptance_gij -v
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

# Пакет tests подставляет фиктивный токен в окружение (tests/__init__.py)
# до импорта bot/config — импорт стоит первым намеренно.
import tests  # noqa: F401
from tests import ui_texts_expect as cat
from utils.ui_texts import tr

try:
    import httpx
    from telegram import Update
    from telegram.ext import Application, ConversationHandler

    import bot as bot_module
    from announcement_store import start_announcement_wizard
    from announcement_wizard import snapshot_brick_ids_by_code
    from models import AnnouncementDraft, utcnow
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from tests.test_poster2_24_p3_announcements_tester import (
        _seed_draft_rows,
        _seed_sent,
    )
    from tests.test_stage25_delivery_instances import (
        _final_markup,
        _only_active,
        _stored,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from tests.test_stage254_lists import (
        _ensure_user,
        _label_by_place,
        _mark_sent,
        _open_list,
        _personal_with_title,
        _place_with_title,
        _rows,
    )
    from utils.inline_buttons import is_disabled

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    IMPORT_ERROR = str(exc)


# —— ожидания контракта (свои, не из реализации) ———————————————————————
LIST_HEAD = "📂 Мои анонсы"
SEARCH_PLACE = "🔎 Найти место"
ADD_PLACE = "➕ Добавить место"
ALL_PLACES = "📚 Все места"
PICK_SEARCH = "🔎 Найти площадку"
PICK_ADD = "➕ Новая площадка"
BACK = "◀️ Назад"
MENU_TEXT = tr("menu.main")
PHOTOS_TITLE = tr("wizard.photos.title")
GONE_TEXT = "больше не хранится"
HINT_LABEL = "⏳ Ожидаю текст короче"
CANCEL_LABEL = "Отменить"
TEXT_LIMIT = 4096
LONG_TEXT = "а" * 4200

# Виды вызовов, создающих НОВОЕ сообщение бота в чате.
CREATE_TYPES = frozenset({"message", "photo", "media_group", "rich_message"})


# --- помощники -------------------------------------------------------------


def _created(h, since: int = 0) -> int:
    """Сколько вызовов создали сообщения бота начиная с индекса `since`."""
    return sum(1 for call in h.calls[since:] if call["type"] in CREATE_TYPES)


def _data(h, prefix: str) -> list[str]:
    return [
        data
        for row in _rows(h)
        for _label, data in row
        if data and data.startswith(prefix)
    ]


def _row_with(h, data: str) -> list[tuple[str, str | None]] | None:
    for row in _rows(h):
        if any(cell[1] == data for cell in row):
            return row
    return None


def _open_buttons(h) -> list[str]:
    """`callback_data` кнопок-записей списка (не корзины)."""
    return [
        data
        for _label, data in h.live_buttons()
        if data and data.startswith("aw_open:")
    ]


def _markup_for(h, message_id: int):
    """Последняя клавиатура КОНКРЕТНОГО сообщения в чате."""
    latest = None
    for call in h.calls:
        if call.get("message_id") is None:
            continue
        if int(call["message_id"]) != int(message_id):
            continue
        if call.get("reply_markup") is not None:
            latest = call.get("reply_markup")
    return latest


def _markup_rows(h, message_id: int) -> list[list]:
    markup = _markup_for(h, message_id)
    if markup is None or not hasattr(markup, "inline_keyboard"):
        raise AssertionError("клавиатура сообщения не обновлена")
    return [list(row) for row in markup.inline_keyboard or []]


def _standard_template_callback(h) -> str:
    """Кнопка стандартного шаблона места (в списке она идёт первой)."""
    for label, data in h.screen_buttons():
        if data and data.startswith("aw_template:") and "Стандартный" in label:
            return data
    raise AssertionError(f"нет стандартного шаблона: {h.screen_buttons()}")


async def _publish_standard(h, *, title: str = "Концерт", place: str = "РанДом"):
    """
    Полный путь автора по стандартному шаблону: место → шаблон → название →
    пропуски остальных вопросов → «Без фотографий».

    Название — единственное непустое поле: у анонса без фото подпись поста
    должна быть непустой, а формат «Мои анонсы» проверяет именно заголовок.
    """
    place_id, _alias = _seed_place(h, name=place)
    await _start_to_template(h, place_id)
    await h.callback(_standard_template_callback(h))
    await h.text(title)
    for _ in range(20):
        if PHOTOS_TITLE in h.screen_text():
            break
        skips = [d for _l, d in h.screen_buttons() if d and d.startswith("aw_qskip")]
        if not skips:
            raise AssertionError(f"не дошли до шага фото: {h.screen_text()[:200]!r}")
        await h.callback(skips[0])
    if PHOTOS_TITLE not in h.screen_text():
        raise AssertionError(f"не шаг фото: {h.screen_text()[:200]!r}")
    await h.callback(_callback(h, "aw_photo_none"))
    return _latest_draft(h)


# =========================================================================
# G. «Мои анонсы» (§6) — понятные названия записей
# =========================================================================


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class EntryLabelTests(unittest.TestCase):
    """§6: `Заголовок — Место (состояние)` — пользовательский формат записи."""

    @scenario_test
    async def test_sent_entry_is_title_place_and_gotov(self, h: Harness) -> None:
        draft_id = _place_with_title(h, place_name="РанДом", title="Концерт")
        _mark_sent(h, draft_id)
        await h.text("/start")
        await _open_list(h)

        self.assertEqual(
            _label_by_place(h, "РанДом"),
            "Концерт — РанДом (готов)",
            "подпись готового анонса: заголовок — место (состояние)",
        )
        self.assertIn(
            "1. Концерт — РанДом (готов)",
            h.screen_text(),
            "номер остаётся только в тексте экрана",
        )

    @scenario_test
    async def test_draft_entry_is_chernovik(self, h: Harness) -> None:
        _place_with_title(h, place_name="Красный Маяк", title="Киноклуб")
        await h.text("/start")
        await _open_list(h)

        self.assertEqual(
            _label_by_place(h, "Красный Маяк"),
            "Киноклуб — Красный Маяк (черновик)",
        )

    @scenario_test
    async def test_blank_title_shows_bez_nazvaniya(self, h: Harness) -> None:
        _place_with_title(h, place_name="РанДом", title="   ")
        await h.text("/start")
        await _open_list(h)

        self.assertEqual(
            _label_by_place(h, "РанДом"),
            "Без названия — РанДом (черновик)",
            "пробельный заголовок — это отсутствие названия",
        )

    @scenario_test
    async def test_personal_template_shows_its_name(self, h: Harness) -> None:
        draft_id = _personal_with_title(h, title="Лекция")
        _mark_sent(h, draft_id)
        await h.text("/start")
        await _open_list(h)

        self.assertEqual(
            _label_by_place(h, "Личный «Вечер»"),
            "Лекция — Личный «Вечер» (готов)",
            "у личного анонса площадки нет — показывается имя шаблона",
        )

    @scenario_test
    async def test_technical_statuses_are_just_chernovik(self, h: Harness) -> None:
        draft_id = _place_with_title(h, place_name="РанДом", title="Концерт")
        await h.text("/start")
        for status in ("sending", "partial", "failed"):
            with h.session_factory() as session:
                row = session.get(AnnouncementDraft, draft_id)
                self.assertIsNotNone(row)
                assert row is not None
                row.publication_status = status
                row.publication_text = "Текст"
                row.publication_photos = "[]"
                row.publication_started_at = utcnow()
                # Технический статус не опубликован: триггер схемы запрещает
                # published_at у sending/partial/failed.
                row.published_at = None
                session.commit()
            await _open_list(h)
            label = _label_by_place(h, "РанДом")
            self.assertEqual(label, "Концерт — РанДом (черновик)", status)
            for technical in ("sending", "partial", "failed", "ошибка", "частично"):
                self.assertNotIn(technical, label.lower(), f"статус {status}: {label}")

    @scenario_test
    async def test_buttons_have_no_number_or_play_emoji(self, h: Harness) -> None:
        _place_with_title(h, place_name="РанДом", title="Концерт")
        _place_with_title(h, place_name="Красный Маяк", title="Киноклуб")
        await h.text("/start")
        await _open_list(h)

        opens = [(label, data) for label, data in h.screen_buttons() if data]
        self.assertTrue(opens, "список записей пуст")
        for label, _data in opens:
            self.assertNotIn("▶️", label, f"эмодзи нумерации в кнопке: {label!r}")
            self.assertNotRegex(
                label, r"^\s*\d+\.", f"номер в кнопке списка: {label!r}"
            )
        self.assertIn("1. ", h.screen_text(), "номер пропал из текста экрана")

    @scenario_test
    async def test_long_title_is_shortened_and_keeps_place(self, h: Harness) -> None:
        _place_with_title(
            h,
            place_name="Красный Маяк",
            title="Очень длинный заголовок мероприятия " * 4,
        )
        await h.text("/start")
        await _open_list(h)

        label = _label_by_place(h, "Красный Маяк")
        self.assertTrue(label.endswith("(черновик)"), label)
        self.assertIn("Красный Маяк", label)
        self.assertIn("…", label, "длинный заголовок должен сокращаться")
        self.assertLessEqual(len(label), 62, f"подпись слишком длинная: {label!r}")

    @scenario_test
    async def test_title_is_normalized_and_escaped_per_surface(
        self, h: Harness
    ) -> None:
        """Заголовок схлопывается; текст экрана — HTML, подпись — как есть."""
        _place_with_title(h, place_name="РанДом", title="  Концерт\n  вечер  ")
        _place_with_title(h, place_name="Маяк", title="<b>Концерт & Ко</b>")
        await h.text("/start")
        await _open_list(h)

        self.assertEqual(
            _label_by_place(h, "РанДом"),
            "Концерт вечер — РанДом (черновик)",
            "пробелы и переводы строк схлопываются в один пробел",
        )
        self.assertEqual(
            _label_by_place(h, "Маяк"),
            "<b>Концерт & Ко</b> — Маяк (черновик)",
            "подпись кнопки — обычный текст, без HTML-экранирования",
        )
        shown = h.screen_text()
        self.assertIn(
            "&lt;b&gt;Концерт &amp; Ко&lt;/b&gt;", shown, "экран не экранирован"
        )
        self.assertNotIn("<b>Концерт & Ко</b>", shown, "сырой HTML попал на экран")


# =========================================================================
# G. Механика списка: корзина, пагинация, чужие записи, устаревшие кнопки
# =========================================================================


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class ListMechanicsTests(unittest.TestCase):
    """§6: механика «Мои анонсы» не изменилась, дефекты не появляются."""

    @scenario_test
    async def test_trash_row_needs_confirmation(self, h: Harness) -> None:
        draft_id = _place_with_title(h, place_name="РанДом", title="Концерт")
        await h.text("/start")
        await _open_list(h)

        opens = _data(h, "aw_open:")
        self.assertEqual(len(opens), 1, h.screen_buttons())
        row = _row_with(h, opens[0])
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(len(row), 2, f"запись не «название | 🗑»: {row}")
        self.assertEqual(row[1][0], "🗑", f"нет кнопки удаления: {row}")

        await h.callback(f"aw_del:{draft_id}")
        self.assertIn("Удалить анонс", h.screen_text())
        await h.callback(f"aw_del_no:{draft_id}")
        with h.session_factory() as session:
            self.assertIsNotNone(
                session.get(AnnouncementDraft, draft_id), "«отмена» удалила запись"
            )
        self.assertIn("Концерт — РанДом (черновик)", h.screen_text())
        self.assertTrue(
            any("Удаление отменено" in text for text in h.answer_texts()[-1:]),
            f"нет подтверждения отмены: {h.answer_texts()[-2:]}",
        )

        await h.callback(f"aw_del:{draft_id}")
        await h.callback(f"aw_del_yes:{draft_id}")
        with h.session_factory() as session:
            self.assertIsNone(
                session.get(AnnouncementDraft, draft_id), "подтверждение не удалило"
            )
        self.assertIn(LIST_HEAD, h.screen_text())

    @scenario_test
    async def test_pagination_keeps_page_and_clamps_edges(self, h: Harness) -> None:
        # 4 черновика + 2 готовых = 6 записей: 5 на первой странице, 1 на второй.
        await h.text("/start")
        _seed_draft_rows(h, count=4, start_at=utcnow())
        for _index in range(2):
            _seed_sent(h, utcnow())
        await _open_list(h)

        self.assertEqual(len(_open_buttons(h)), 5, "пять записей на странице")
        self.assertEqual(
            [label for label, data in h.screen_buttons() if data == "noop:1:2"],
            ["1/2"],
            "нет номера страницы",
        )

        next_page = _data(h, "aw_home_page:")
        self.assertTrue(next_page, f"нет стрелки вперёд: {h.screen_buttons()}")
        await h.callback(next_page[0])
        self.assertEqual(len(_open_buttons(h)), 1, "на второй странице одна запись")
        self.assertIn("2/2", [label for label, _d in h.screen_buttons()])

        # Возврат из подтверждения не сбрасывает страницу.
        victim = _open_buttons(h)[0].split(":")[1]
        await h.callback(f"aw_del:{victim}")
        await h.callback(f"aw_del_no:{victim}")
        self.assertEqual(len(_open_buttons(h)), 1, "страница потеряна")
        self.assertIn("2/2", [label for label, _d in h.screen_buttons()])

        # Недостижимая страница приводится к последней, а не к пустому экрану.
        await h.callback("aw_home_page:999")
        self.assertIn("2/2", [label for label, _d in h.screen_buttons()])
        self.assertEqual(len(_open_buttons(h)), 1)
        await h.callback("aw_home_page:0")
        self.assertIn("1/2", [label for label, _d in h.screen_buttons()])
        await h.callback("aw_home_page:1")

        # Удаление последней записи страницы 2 возвращает на страницу 1.
        await h.callback(f"aw_del:{victim}")
        await h.callback(f"aw_del_yes:{victim}")
        self.assertEqual(len(_open_buttons(h)), 5, "показана пустая страница")
        self.assertFalse(
            [d for _l, d in h.screen_buttons() if d and d.startswith("aw_home_page:")],
            "одна страница — пагинации быть не должно",
        )
        self.assertIn("📂 Мои анонсы", h.screen_text())

    @scenario_test
    async def test_foreign_entry_is_invisible_and_untouched(self, h: Harness) -> None:
        mine = _place_with_title(h, place_name="МойЗал", title="Мой концерт")
        template_id, _rev, _bricks = _seed_personal_template(
            h, name="Чужой шаблон", actor_tg=55
        )
        with h.session_factory() as session:
            foreign = start_announcement_wizard(
                session,
                actor_telegram_user_id=55,
                template_id=template_id,
                origin="personal",
            )
            session.commit()
            foreign_id = foreign.draft_id

        await _open_list(h)
        self.assertNotIn(
            f"aw_open:{foreign_id}",
            [data for _l, data in h.screen_buttons()],
            "чужой анонс виден в списке",
        )

        # Чужой анонс нельзя ни открыть, ни удалить по угаданному id.
        for data in (f"aw_open:{foreign_id}", f"aw_del_yes:{foreign_id}"):
            await h.callback(data)
            self.assertTrue(
                any(GONE_TEXT in text for text in h.answer_texts()[-1:]),
                f"нет понятного ответа на {data}: {h.answer_texts()[-2:]}",
            )
            with h.session_factory() as session:
                self.assertIsNotNone(
                    session.get(AnnouncementDraft, foreign_id),
                    f"{data} затронул чужой анонс",
                )
        # Мой список цел.
        await _open_list(h)
        self.assertEqual(_data(h, "aw_open:"), [f"aw_open:{mine}"])

    @scenario_test
    async def test_stale_open_button_gives_notice_and_list(self, h: Harness) -> None:
        await h.text("/start")
        await _open_list(h)

        await h.callback("aw_open:9999")

        self.assertTrue(
            any(GONE_TEXT in text for text in h.answer_texts()),
            f"нет ответа об исчезнувшем анонсе: {h.answer_texts()[-2:]}",
        )
        self.assertIn(LIST_HEAD, h.screen_text(), "список не показан вместо ошибки")
        self.assertIn(("🏠 В меню", "main_menu"), h.screen_buttons())
        self.assertIn("Анонсов пока нет", h.screen_text())


# =========================================================================
# G. Клавиатура площадок (§4)
# =========================================================================


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class PlacesKeyboardTests(unittest.TestCase):
    """§4: «поиск» и «добавить» — одной строкой в хабе и в выборе площадки."""

    @scenario_test
    async def test_hub_has_search_and_add_in_one_row(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("places")
        self.assertIn("📍 Места", h.screen_text())

        self.assertEqual(
            _row_with(h, "pl_search"),
            [(SEARCH_PLACE, "pl_search"), (ADD_PLACE, "pl_add")],
            f"поиск и добавление не в одной строке: {h.screen_buttons()}",
        )
        self.assertEqual(_row_with(h, "pl_all"), [(ALL_PLACES, "pl_all")])
        back_row = [row for row in _rows(h) if (BACK, "main_menu") in row]
        self.assertEqual(back_row, [[(BACK, "main_menu")]])

        # Навигация не сломана.
        await h.callback("pl_search")
        cat.assert_shows(self, h.screen_text(), "places.search_input")
        # Из поиска — назад в хаб, там доступна кнопка добавления.
        await h.callback("pl_back")
        await h.callback("pl_add")
        cat.assert_shows(self, h.screen_text(), "places.create_input")
        await h.callback("pl_back")
        self.assertIn("📍 Места", h.screen_text())
        await h.callback("pl_all")
        cat.assert_shows(self, h.screen_text(), "places.all.empty")
        await h.callback("main_menu")
        self.assertIn(MENU_TEXT, h.screen_text())

    @scenario_test
    async def test_picker_has_search_and_add_in_one_row(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("pick_place")

        self.assertEqual(
            _row_with(h, "pl_pick_search"),
            [(PICK_SEARCH, "pl_pick_search"), (PICK_ADD, "pl_pick_add")],
            f"создание анонса: поиск и новая площадка не одной строкой: "
            f"{h.screen_buttons()}",
        )
        self.assertTrue(
            _row_with(h, "pick_template"), f"нет личного шаблона: {h.screen_buttons()}"
        )
        self.assertEqual(_row_with(h, "main_menu"), [(BACK, "main_menu")])

        await h.callback("pl_pick_search")
        cat.assert_shows(self, h.screen_text(), "places.pick_search_input")
        await h.callback("pl_pick_back")
        self.assertIn("Создать анонс", h.screen_text())
        await h.callback("pl_pick_add")
        cat.assert_shows(self, h.screen_text(), "places.create_input")
        await h.callback("pl_back")
        self.assertIn(
            (PICK_SEARCH, "pl_pick_search"),
            h.screen_buttons(),
            "возврат из добавления потерял выбор площадки",
        )

    @scenario_test
    async def test_picker_with_existing_place_keeps_one_row(self, h: Harness) -> None:
        """Ветка экрана с существующими площадками — та же компоновка §4."""
        _ensure_user(h)
        _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("pick_place")
        self.assertIn("Дом культуры", h.screen_text())

        self.assertEqual(
            _row_with(h, "pl_pick_search"),
            [(PICK_SEARCH, "pl_pick_search"), (PICK_ADD, "pl_pick_add")],
            f"список площадок: действия не одной строкой: {h.screen_buttons()}",
        )
        # Тап по площадке по-прежнему сразу начинает анонс именно с неё
        # (стандартный шаблон места, первый вопрос).
        picks = _data(h, "pl_pick:")
        self.assertTrue(picks, f"нет кнопки площадки: {h.screen_buttons()}")
        await h.callback(picks[0])
        cat.assert_question(self, h.screen_text(), 1, msg="площадка не начала анонс")


# =========================================================================
# I. Polling и VPN-прокси (§8)
# =========================================================================


@unittest.skipIf(IMPORT_ERROR is not None, f"импорт недоступен: {IMPORT_ERROR}")
class PollingTimeoutsTests(unittest.TestCase):
    """§8: явные таймауты и бесконечный retry bootstrap."""

    def test_constants_are_larger_than_ptb_defaults(self) -> None:
        self.assertEqual(bot_module.PROXY_CONNECT_TIMEOUT, 15.0)
        self.assertEqual(bot_module.PROXY_READ_TIMEOUT, 15.0)
        self.assertEqual(bot_module.PROXY_WRITE_TIMEOUT, 15.0)
        self.assertEqual(bot_module.PROXY_POOL_TIMEOUT, 5.0)
        self.assertEqual(bot_module.POLLING_GET_UPDATES_CONNECT_TIMEOUT, 15.0)
        self.assertEqual(bot_module.POLLING_GET_UPDATES_READ_TIMEOUT, 15.0)
        self.assertEqual(bot_module.POLLING_GET_UPDATES_WRITE_TIMEOUT, 15.0)
        self.assertEqual(bot_module.POLLING_GET_UPDATES_POOL_TIMEOUT, 5.0)
        for value in (
            bot_module.PROXY_CONNECT_TIMEOUT,
            bot_module.PROXY_READ_TIMEOUT,
            bot_module.PROXY_WRITE_TIMEOUT,
            bot_module.POLLING_GET_UPDATES_CONNECT_TIMEOUT,
            bot_module.POLLING_GET_UPDATES_READ_TIMEOUT,
            bot_module.POLLING_GET_UPDATES_WRITE_TIMEOUT,
        ):
            self.assertGreater(value, 5.0, "таймаут не больше дефолта PTB (5 с)")
        self.assertEqual(bot_module.POLLING_BOOTSTRAP_RETRIES, -1)

    def test_ptb_default_bootstrap_is_zero(self) -> None:
        """Контракт §8 имеет смысл только если дефолт PTB правда 0."""
        parameters = Application.run_polling.__code__.co_varnames[
            : Application.run_polling.__code__.co_argcount
        ]
        self.assertIn("bootstrap_retries", parameters, "PTB сменил сигнатуру")
        default = (Application.run_polling.__kwdefaults__ or {}).get(
            "bootstrap_retries", 0
        )
        self.assertEqual(default, 0, "дефолт PTB изменился — пересмотреть §8")


@unittest.skipIf(IMPORT_ERROR is not None, f"импорт недоступен: {IMPORT_ERROR}")
class PollingApplicationTests(unittest.TestCase):
    """§8: собранное приложение реально применяет таймауты (без сети)."""

    @staticmethod
    def _timeout(request) -> httpx.Timeout:
        return request._client_kwargs["timeout"]

    def test_build_without_network_and_without_updater_loop(self) -> None:
        def boom(*_args, **_kwargs):
            raise AssertionError("сеть запрещена в тестах")

        with (
            patch("socket.socket", boom),
            patch.object(httpx.AsyncClient, "send", boom),
        ):
            app = bot_module.build_application()

        self.assertIsNotNone(app.updater, "Updater не создан")
        assert app.updater is not None
        self.assertFalse(app.updater.running, "polling запущен при сборке")
        self.assertTrue(app.error_handlers, "error_handler не зарегистрирован")
        handlers = [handler for group in app.handlers.values() for handler in group]
        self.assertTrue(handlers, "обработчики не зарегистрированы")
        self.assertTrue(
            any(isinstance(handler, ConversationHandler) for handler in handlers),
            "ConversationHandler конструктора не зарегистрирован",
        )

    def test_timeouts_reach_both_requests(self) -> None:
        app = bot_module.build_application()
        updates_request, regular_request = app.bot._request
        self.assertIsNot(
            updates_request, regular_request, "getUpdates без своих таймаутов"
        )

        regular = self._timeout(regular_request)
        self.assertEqual(regular.connect, bot_module.PROXY_CONNECT_TIMEOUT)
        self.assertEqual(regular.read, bot_module.PROXY_READ_TIMEOUT)
        self.assertEqual(regular.write, bot_module.PROXY_WRITE_TIMEOUT)
        self.assertEqual(regular.pool, bot_module.PROXY_POOL_TIMEOUT)

        updates = self._timeout(updates_request)
        self.assertEqual(
            updates.connect, bot_module.POLLING_GET_UPDATES_CONNECT_TIMEOUT
        )
        self.assertEqual(updates.read, bot_module.POLLING_GET_UPDATES_READ_TIMEOUT)
        self.assertEqual(updates.write, bot_module.POLLING_GET_UPDATES_WRITE_TIMEOUT)
        self.assertEqual(updates.pool, bot_module.POLLING_GET_UPDATES_POOL_TIMEOUT)

        self.assertEqual(
            updates_request.read_timeout,
            bot_module.POLLING_GET_UPDATES_READ_TIMEOUT,
        )
        self.assertEqual(regular_request.read_timeout, bot_module.PROXY_READ_TIMEOUT)
        self.assertNotEqual(
            (regular.connect, regular.read, regular.pool),
            (5.0, 5.0, 1.0),
            "применены дефолты PTB, а не запас под прокси",
        )

    def test_main_passes_bootstrap_retries_and_allowed_updates(self) -> None:
        app = MagicMock()
        with (
            patch.object(bot_module, "setup_logging"),
            patch.object(bot_module, "logger"),
            patch.object(bot_module, "init_db"),
            patch.object(bot_module, "build_application", return_value=app),
        ):
            bot_module.main()

        app.run_polling.assert_called_once()
        kwargs = app.run_polling.call_args.kwargs
        self.assertEqual(kwargs.get("bootstrap_retries"), -1)
        self.assertEqual(kwargs.get("allowed_updates"), Update.ALL_TYPES)


# =========================================================================
# J. Сквозной маршрут автора
# =========================================================================


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class EndToEndRouteTests(unittest.TestCase):
    """J: создание → правка → отмена → переиздание → справка → рестарт."""

    @scenario_test
    async def test_publish_then_list_shows_new_format(self, h: Harness) -> None:
        await h.text("/start")
        draft = await _publish_standard(h, title="Концерт")

        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(h.screen_text(), "", "служебный экран остался в чате")
        self.assertEqual(len(h.posts()), 1, "анонс доставлен не одним сообщением")
        active = _only_active(h, draft.id)
        self.assertEqual(
            h.live_control_callbacks(),
            [
                f"anp_data:{active.id}",
                f"anp_photos:{active.id}",
                # 2.6.1 §4: у стандартного шаблона расширенный вариант по
                # умолчанию выключен, поэтому кнопки доп. полей нет даже при
                # сохранённом расширенном тексте снимка.
                f"anp_menu:{active.id}",
            ],
            "управление должно жить на самом анонсе",
        )

        await h.callback("main_menu")
        await h.callback("my_announcements")
        self.assertEqual(_label_by_place(h, "РанДом"), "Концерт — РанДом (готов)")

    @scenario_test
    async def test_field_edit_is_in_place_and_updates_caption(self, h: Harness) -> None:
        await h.text("/start")
        draft = await _publish_standard(h, title="Концерт")
        active = _only_active(h, draft.id)
        message_id = int(active.message_ids[0])
        bricks = snapshot_brick_ids_by_code(_stored(h, draft.id).template_snapshot)
        before = _stored(h, draft.id).publication_text

        since = len(h.calls)
        await h.callback(f"anp_data:{active.id}")
        await h.callback(f"anf_sel:{active.id}:{bricks['описание']}")
        await h.text("Новое описание")

        self.assertEqual(_created(h, since), 0, "правка создала новые сообщения")
        stored = _stored(h, draft.id)
        self.assertIn("Новое описание", stored.publication_text or "")
        self.assertNotEqual(stored.publication_text, before, "значение не сохранено")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            stored.publication_text,
            "сообщение анонса не обновилось на месте",
        )
        self.assertEqual(
            _only_active(h, draft.id).message_ids, (message_id,), "анонс переиздан"
        )

    @scenario_test
    async def test_long_text_then_cancel_restores_value(self, h: Harness) -> None:
        await h.text("/start")
        draft = await _publish_standard(h, title="Концерт")
        active = _only_active(h, draft.id)
        message_id = int(active.message_ids[0])
        bricks = snapshot_brick_ids_by_code(_stored(h, draft.id).template_snapshot)
        published = _stored(h, draft.id).publication_text
        posts_before = len(h.posts())

        await h.callback(f"anp_data:{active.id}")
        await h.callback(f"anf_sel:{active.id}:{bricks['описание']}")
        since = len(h.calls)
        await h.text(LONG_TEXT)

        # Отказ по длине — строка каталога `wizard.length_refusal.*` с лимитом.
        refusals = [text for text in h.reply_texts() if cat.length_refusal(text)]
        self.assertTrue(refusals, f"нет причины отказа: {h.reply_texts()[-2:]}")
        cat.assert_length_refusal(self, refusals[-1], TEXT_LIMIT)
        self.assertEqual(_created(h, since), 1, "лишние сообщения интерфейса")

        rows = _markup_rows(h, message_id)
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(len(rows[0]), 1, rows[0])
        self.assertTrue(is_disabled(rows[0][0]), "подсказка должна быть неактивной")
        self.assertEqual(rows[0][0].text, HINT_LABEL)
        self.assertIsNone(rows[0][0].callback_data)
        self.assertEqual(rows[1][0].text, CANCEL_LABEL)
        cancel = rows[1][0].callback_data
        self.assertTrue(cancel and cancel.startswith("anq_cancel:"), rows[1])

        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_text, published, "анонс испорчен отказом")
        self.assertEqual(h.bot.content_text(h.chat_id, message_id), published)
        self.assertEqual(len(h.posts()), posts_before, "лишняя публикация")

        since = len(h.calls)
        await h.callback(cancel)
        self.assertEqual(_created(h, since), 0, "отмена создала сообщения")
        self.assertEqual(
            _stored(h, draft.id).publication_text, published, "отмена не вернула текст"
        )
        self.assertTrue(
            h.live_callbacks("anf_sel:"),
            f"список полей не вернулся: {h.live_buttons()}",
        )
        self.assertEqual(h.live_callbacks("anq_cancel"), [], "экран ошибки остался")

        # Повторное нажатие старой «Отменить» безопасно.
        await h.callback(cancel)
        cat.assert_stale(
            self,
            " ".join(h.answer_texts()[-1:]),
            f"нет stale-ответа: {h.answer_texts()[-2:]}",
        )
        self.assertEqual(_stored(h, draft.id).publication_text, published)

    @scenario_test
    async def test_reopen_creates_new_instance_and_archives_old(
        self, h: Harness
    ) -> None:
        await h.text("/start")
        draft = await _publish_standard(h, title="Концерт")
        first = _only_active(h, draft.id)
        old_message_id = int(first.message_ids[0])
        bricks = snapshot_brick_ids_by_code(_stored(h, draft.id).template_snapshot)

        await h.callback("main_menu")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")

        second = _only_active(h, draft.id)
        self.assertNotEqual(second.id, first.id, "открыт прежний экземпляр")
        self.assertEqual(second.generation, 2, "экземпляр не переиздан")
        self.assertNotEqual(
            tuple(second.message_ids),
            tuple(first.message_ids),
            "адрес сообщения прежний",
        )
        self.assertEqual(
            _final_markup(h, old_message_id), None, "у старого остались кнопки"
        )
        self.assertNotIn(
            old_message_id, h.deleted_ids(), "архивный анонс удалён из чата"
        )
        self.assertEqual(len(h.posts()), 2, "переиздание не создало второй пост")
        controls = h.live_control_callbacks()
        self.assertTrue(controls, "у нового анонса нет управления")
        self.assertTrue(
            all(callback.endswith(f":{second.id}") for callback in controls),
            f"управление осталось у старого экземпляра: {controls}",
        )

        # Правка идёт в новый экземпляр, старый текст в чате не меняется.
        old_text = h.bot.content_text(h.chat_id, old_message_id)
        await h.callback(f"anp_data:{second.id}")
        await h.callback(f"anf_sel:{second.id}:{bricks['описание']}")
        await h.text("Описание новой версии")
        self.assertIn(
            "Описание новой версии", _stored(h, draft.id).publication_text or ""
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, old_message_id),
            old_text,
            "правка задела архивный анонс",
        )
        self.assertIn(
            "Описание новой версии",
            h.bot.content_text(h.chat_id, int(second.message_ids[0])) or "",
        )

        # Устаревшие кнопки старого экземпляра ничего не пишут.
        since = len(h.calls)
        await h.callback(f"anf_sel:{first.id}:{bricks['название']}")
        self.assertEqual(_created(h, since), 0)
        cat.assert_stale(
            self,
            " ".join(h.answer_texts()[-1:]),
            f"нет stale-ответа: {h.answer_texts()[-2:]}",
        )
        self.assertIn(
            "Описание новой версии", _stored(h, draft.id).publication_text or ""
        )

    @scenario_test
    async def test_help_from_constructor_keeps_step(self, h: Harness) -> None:
        """Регрессия F1 (исправлен): справка — не выход из конструктора."""
        await h.text("/start")
        place_id, _alias = _seed_place(h, name="РанДом")
        await _start_to_template(h, place_id)
        await h.callback(_standard_template_callback(h))
        question_text = h.screen_text()
        question_buttons = h.screen_buttons()
        draft = _latest_draft(h)

        await h.text("/help")
        await h.callback("help_back")

        self.assertEqual(h.screen_text(), question_text, "шаг черновика потерян")
        self.assertEqual(h.screen_buttons(), question_buttons, "кнопки шага другие")
        self.assertEqual(_latest_draft(h).id, draft.id, "черновик подменён")

    @scenario_test
    async def test_restart_keeps_resume_and_controls(self, h: Harness) -> None:
        await h.text("/start")
        draft = await _publish_standard(h, title="Концерт")
        active = _only_active(h, draft.id)
        message_id = int(active.message_ids[0])
        bricks = snapshot_brick_ids_by_code(_stored(h, draft.id).template_snapshot)
        await h.callback(f"anp_data:{active.id}")
        self.assertTrue(h.live_callbacks("anf_sel:"), "список полей не открылся")

        await h.restart()

        self.assertIsNone(h.dialog_state(), "состояние диалога пережило рестарт")
        self.assertEqual(h.user_data(), {}, "user_data пережил рестарт")
        self.assertTrue(
            h.live_callbacks("anf_sel:"), "клавиатура управления потеряна в чате"
        )

        since = len(h.calls)
        await h.callback(f"anf_sel:{active.id}:{bricks['название']}")
        await h.text("Концерт-2")

        self.assertEqual(_created(h, since), 0, "после рестарта созданы сообщения")
        stored = _stored(h, draft.id)
        self.assertIn(
            "Концерт-2", stored.publication_text or "", "правка не применилась"
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            stored.publication_text,
            "сообщение анонса не обновилось",
        )
        self.assertTrue(h.live_callbacks("anf_sel:"), "управление не вернулось")

    @scenario_test
    async def test_text_from_foreign_chat_changes_nothing(self, h: Harness) -> None:
        await h.text("/start")
        draft = await _publish_standard(h, title="Концерт")
        active = _only_active(h, draft.id)
        bricks = snapshot_brick_ids_by_code(_stored(h, draft.id).template_snapshot)
        published = _stored(h, draft.id).publication_text

        await h.callback(f"anf_sel:{active.id}:{bricks['описание']}", chat_id=999001)
        await h.text("Текст из чужого чата", chat_id=999001)

        self.assertEqual(
            _stored(h, draft.id).publication_text,
            published,
            "текст из чужого чата записался в анонс",
        )
        cat.assert_stale(
            self,
            " ".join(h.answer_texts()[-1:]),
            f"нет отказа на чужой чат: {h.answer_texts()[-2:]}",
        )


if __name__ == "__main__":
    unittest.main()
