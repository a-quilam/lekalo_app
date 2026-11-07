# tests/test_stage251_acceptance.py
"""
Poster 2.5.1 — независимая приёмка релиза (12 сквозных проверок).

Файл закрепляет пользовательский контракт корректирующего релиза, а не
реализацию: Rich-справку и её фолбэк, возврат «◀️ Назад» на экран-источник
(раздел, страницу, шаг конструктора, рестарт), единую пагинацию списков,
неизменную фото-сетку и независимый снимок условных частей анонса.

1. Rich JSON официальной схемы (`blocks`, heading size=1, items-объекты,
   details без `is_open`, divider);
2. отказ `sendRichMessage` — обычная плашка `help.rich.unavailable` с
   `main_menu_keyboard()` и без временных ключей;
3. возврат в «Места» на ту же страницу списка;
4. возврат в конструктор на следующий шаг с живым resume-снимком;
5. возврат после рестарта: шаг восстановлен, введённое значение в БД;
6. возврат удаляет временный Rich Message из памяти и `user_states`;
7. на справке ровно одна кнопка «◀️ Назад» (`help_back`);
8. пагинация списка мест конструктора: 5/страницу, 6-е место даёт `1/2`;
9. пагинация личных шаблонов: 7 шаблонов → 5 и индикатор;
10. пагинация доп. полей (`aw_exbpage`) и названий места (`aw_place_names`);
11. фото-сетка не изменилась: `PHOTO_PAGE_SIZE == 8`, 8 фото — одна страница;
12. снимок условных частей: заморозка при создании; признак
   `conditional_rules_frozen` больше не влияет на рендер (2.5.2 §8).

Реальные Telegram, `.env` и `post_bot.db` не используются: настоящий
`Application.process_update` поверх временной SQLite (`Harness`) и отдельная
временная БД для доменного сценария снимка.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \\
        -m unittest tests.test_stage251_acceptance -v
"""

from __future__ import annotations

import asyncio
import json
import unittest

from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker
from telegram.error import BadRequest

from announcement_store import (
    ORIGIN_PERSONAL,
    build_announcement_preview,
    save_announcement_value,
    start_announcement_wizard,
)
from conditional_parts import create_conditional_part
from models import AnnouncementValue, Template
from template_store import create_personal_template, update_template_content
from tests import ui_texts_expect as cat
from tests.stage19a_helpers import make_engine
from tests.stage23_helpers import OWNER_TG, TemplateDatabaseTestCase
from user_store import get_or_create_user
from utils.ui_texts import tr

try:
    from handlers.announcement_wizard import PHOTO_PAGE_SIZE, RESUME_PAYLOAD_KEY
    from handlers.main_menu import main_menu_keyboard
    from place_store import add_alternative_name
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        inline_buttons,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _callbacks,
        _choose_personal,
        _latest_draft,
        _no_photos,
        _open_control,
        _open_edit,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from utils import screen as screen_utils

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# --- общие константы --------------------------------------------------------

BACK_LABEL = "◀️ Назад"
BACK_DATA = "help_back"
# Poster 2.5.4 §7: вторая кнопка в той же строке — «ℹ️ О проекте».
ABOUT_LABEL = "ℹ️ О проекте"
ABOUT_DATA = "help_about"
RICH_KEY = "help_rich_message_id"
MENU_TEXT = tr("menu.main")

CONTENT_V1 = "**[main] – [cost]**\nКонец"
HIDDEN_FRAGMENT = " – [cost]"
EMPTY_COST_V1 = "<b>Концерт</b>\nКонец"
FILLED_COST_V1 = "<b>Концерт – 500 ₽</b>\nКонец"
CONTENT_V2 = "**[main] – [cost]**\nНовый текст\nНовый конец"


# --- помощники интеграционных сцен ------------------------------------------


def _nav_buttons(h: Harness, *prefixes: str) -> list[tuple[str, str | None]]:
    """Кнопки строки навигации выбранных маршрутов (в порядке клавиатуры)."""
    return [
        (label, data)
        for label, data in h.screen_buttons()
        if data is not None and data.startswith(prefixes)
    ]


def _stored_value(h: Harness, draft_id: int, brick_id: int) -> dict | None:
    """Сохранённое значение кирпичика (None — значение ещё не принято)."""
    with h.session_factory() as session:
        row = session.scalar(
            select(AnnouncementValue).where(
                AnnouncementValue.draft_id == draft_id,
                AnnouncementValue.brick_id == brick_id,
            )
        )
    if row is None or not row.value_json:
        return None
    return json.loads(row.value_json)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RichHelpAcceptanceTests(unittest.TestCase):
    """П.1, 2, 7: структура Rich-справки, фолбэк и единственная кнопка."""

    @scenario_test
    async def test_rich_schema_official_bot_api(self, h: Harness) -> None:
        await h.text("/start")
        await h.text("/help")
        record = h.bot.rich_records[-1]
        payload = record["rich_message"]
        self.assertEqual(list(payload), ["blocks"], "ровно один способ — blocks")

        blocks = payload["blocks"]
        self.assertEqual(
            blocks[0],
            {
                "type": "heading",
                "text": tr("help.rich.title"),
                "size": 1,
            },
            "первый блок — heading с максимальным размером",
        )

        dividers = [block for block in blocks if block["type"] == "divider"]
        self.assertEqual(
            dividers,
            [{"type": "divider"}],
            "divider передаётся без лишних полей",
        )

        details = [block for block in blocks if block["type"] == "details"]
        self.assertEqual(len(details), 10, "в справке десять разделов")
        for block in details:
            self.assertNotIn("is_open", block, "свёрнутый раздел без is_open")
            self.assertTrue(block.get("summary"))
            self.assertTrue(block.get("blocks"))

        lists = [
            child
            for block in details
            for child in block["blocks"]
            if child["type"] == "list"
        ]
        self.assertTrue(lists, "в разделах есть списки")
        for block in lists:
            self.assertTrue(block["items"])
            for item in block["items"]:
                self.assertEqual(
                    list(item), ["blocks"], "items — объекты со своими blocks"
                )
                nested = item["blocks"]
                self.assertTrue(nested)
                self.assertEqual(nested[0]["type"], "paragraph")
                self.assertIsInstance(nested[0]["text"], str)

    @scenario_test
    async def test_rich_failure_fallback(self, h: Harness) -> None:
        h.bot.errors_by_type["rich_message"] = BadRequest("can't parse entities")
        await h.text("/start")
        await h.callback("help")

        self.assertEqual(
            list(h.bot.rich_records),
            [],
            "Rich Message не должен считаться отправленным",
        )
        notice = tr("help.rich.unavailable").split(".")[0]
        self.assertIn(notice, h.screen_text(), "нет понятной плашки сбоя")
        self.assertEqual(
            h.screen_buttons(),
            inline_buttons(main_menu_keyboard()),
            "фолбэк показывает обычное меню",
        )
        self.assertNotIn(RICH_KEY, h.user_data())
        self.assertNotIn("help_return", h.user_data())
        self.assertIsNone(screen_utils.load_rich_temp(h.user_id))
        self.assertNotIn("rich_temp", screen_utils.load_payload(h.user_id) or {})

    @scenario_test
    async def test_help_keyboard_has_back_and_about_buttons(self, h: Harness) -> None:
        """Poster 2.5.4 §7: возврат и «ℹ️ О проекте» — в одной строке."""
        await h.text("/start")
        await h.callback("help")
        record = h.bot.rich_records[-1]
        buttons = inline_buttons(record["reply_markup"])

        self.assertEqual(buttons, [(BACK_LABEL, BACK_DATA), (ABOUT_LABEL, ABOUT_DATA)])
        self.assertEqual(len(buttons), 2, "на справке две кнопки")
        rows = record["reply_markup"].inline_keyboard
        self.assertEqual(len(rows), 1, "кнопки должны стоять в одной строке")
        self.assertNotIn(
            "main_menu", [data for _label, data in buttons], "старая кнопка меню"
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HelpBackAcceptanceTests(unittest.TestCase):
    """П.3–6: возврат в раздел, в конструктор, после рестарта и очистка."""

    @scenario_test
    async def test_help_back_restores_section(self, h: Harness) -> None:
        await h.text("/start")
        for number in range(7):
            _seed_place(h, name=f"Площадка страниц {number + 1}")
        await h.callback("places")
        await h.callback("pl_all")
        next_pages = [
            data
            for _label, data in h.screen_buttons()
            if data is not None and data.startswith("pl_page:")
        ]
        self.assertTrue(next_pages, "нет навигации по страницам списка мест")
        await h.callback(next_pages[0])
        page_text = h.screen_text()
        self.assertIn("Все места", page_text)

        await h.callback("help")
        await h.callback(BACK_DATA)

        self.assertEqual(h.screen_text(), page_text, "страница списка не восстановлена")
        self.assertNotIn(MENU_TEXT, h.screen_text(), "вернулись в меню, а не в раздел")

    @scenario_test
    async def test_help_back_restores_wizard_step(self, h: Harness) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Два поля",
            specs=[
                ("main", "Основное", "short_text", {}),
                ("second", "Второе", "short_text", {}),
            ],
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        self.assertRegex(h.screen_text(), cat.question_re(1, 2))

        await h.text("Первый ответ")
        self.assertRegex(h.screen_text(), cat.question_re(2, 2))

        await h.callback("help")
        payload = screen_utils.load_payload(h.user_id) or {}
        self.assertIn(RESUME_PAYLOAD_KEY, payload, "справка сняла resume-снимок")

        await h.callback(BACK_DATA)
        self.assertRegex(
            h.screen_text(), cat.question_re(2, 2), "не вернулись на текущий шаг"
        )
        self.assertIn(
            RESUME_PAYLOAD_KEY,
            screen_utils.load_payload(h.user_id) or {},
            "возврат снял resume-снимок",
        )

    @scenario_test
    async def test_help_back_after_restart(self, h: Harness) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Два поля",
            specs=[
                ("main", "Основное", "short_text", {}),
                ("second", "Второе", "short_text", {}),
            ],
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        draft_id = _latest_draft(h).id

        await h.text("Ответ до рестарта")
        self.assertRegex(h.screen_text(), cat.question_re(2, 2))

        await h.restart()
        await h.text("/help")
        await h.callback(BACK_DATA)

        self.assertRegex(h.screen_text(), cat.question_re(2, 2), "шаг не восстановлен")
        self.assertEqual(
            (_stored_value(h, draft_id, bricks["main"]) or {}).get("text"),
            "Ответ до рестарта",
            "введённое значение потеряно",
        )
        self.assertEqual(_latest_draft(h).id, draft_id)

    @scenario_test
    async def test_help_back_deletes_rich_temp(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        rich_id = h.bot.rich_records[0]["message_id"]
        self.assertEqual(h.user_data()[RICH_KEY], rich_id)

        await h.callback(BACK_DATA)

        self.assertIn(rich_id, h.deleted_ids(), "временная справка не удалена")
        self.assertNotIn(RICH_KEY, h.user_data(), "id справки остался в user_data")
        self.assertIsNone(
            screen_utils.load_rich_temp(h.user_id), "id справки остался в БД"
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class WizardPaginationAcceptanceTests(unittest.TestCase):
    """П.8–10: единая пагинация списков, доп. полей и названий места."""

    @scenario_test
    async def test_place_list_pagination(self, h: Harness) -> None:
        await h.text("/start")
        for number in range(5):
            _seed_place(h, name=f"Зал страниц {number + 1}")
        await h.text("/new_wizard")
        await h.callback("aw_start")
        await h.callback("aw_new")
        await h.callback("aw_place_all:0")

        self.assertEqual(
            len(_callbacks(h, "aw_place_open:")),
            5,
            "на страницу пять мест",
        )
        self.assertEqual(
            [
                (label, data)
                for label, data in _nav_buttons(h, "noop:", "aw_place_all:")
                if data != "aw_place_all:0"
            ],
            [],
            "на пяти местах пагинации нет",
        )

        _seed_place(h, name="Зал страниц 6")
        await h.callback("aw_place_all:0")
        self.assertEqual(len(_callbacks(h, "aw_place_open:")), 5)
        self.assertEqual(
            _nav_buttons(h, "noop:", "aw_place_all:"),
            [("1/2", "noop:1:2"), ("▶️", "aw_place_all:1")],
            "первая страница: индикатор и правая стрелка",
        )

        await h.callback("aw_place_all:1")
        self.assertEqual(
            _nav_buttons(h, "noop:", "aw_place_all:"),
            [("◀️", "aw_place_all:0"), ("2/2", "noop:2:2")],
            "последняя страница: левая стрелка и индикатор",
        )

    @scenario_test
    async def test_template_list_pagination(self, h: Harness) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        with h.session_factory() as session:
            get_or_create_user(session, h.user_id, username="tpl-owner")
            for index in range(7):
                create_personal_template(
                    session,
                    actor_telegram_user_id=h.user_id,
                    name=f"Личный шаблон {index + 1}",
                    content="Шапка\n[название]\nПодвал",
                )
            session.commit()
        await _start_to_template(h, place_id)

        personal = [
            (label, data)
            for label, data in h.screen_buttons()
            if (data or "").startswith("aw_template:") and not label.startswith("✅")
        ]
        self.assertEqual(len(personal), 5, "пять личных шаблонов на страницу")
        self.assertEqual(
            [label for label, _data in _nav_buttons(h, "aw_tplpage:", "noop:")],
            ["1/2", "▶️"],
            "первая страница шаблонов",
        )

    @scenario_test
    async def test_extras_and_names_pagination(self, h: Harness) -> None:
        # --- названия места: 6 названий — две страницы --------------------
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        with h.session_factory() as session:
            user = get_or_create_user(session, h.user_id, username="names-owner")
            for index in range(5):
                add_alternative_name(
                    session,
                    place_id,
                    f"Вариант названия {index + 1}",
                    created_by_user_id=user.id,
                )
            session.commit()

        await h.text("/new_wizard")
        await h.callback("aw_start")
        await h.callback("aw_new")
        await h.callback("aw_place_all:0")
        await h.callback(f"aw_place_open:{place_id}")
        await h.callback(_callback(h, "aw_place_names"))

        self.assertEqual(len(_callbacks(h, "aw_place_name_pick")), 5)
        self.assertEqual(
            _nav_buttons(h, "noop:", "aw_place_names:"),
            [("1/2", "noop:1:2"), ("▶️", f"aw_place_names:{place_id}:1")],
            "первая страница названий",
        )
        await h.callback(f"aw_place_names:{place_id}:1")
        self.assertEqual(len(_callbacks(h, "aw_place_name_pick")), 1)
        self.assertEqual(
            _nav_buttons(h, "noop:", "aw_place_names:"),
            [("◀️", f"aw_place_names:{place_id}:0"), ("2/2", "noop:2:2")],
            "последняя страница названий",
        )

        # --- доп. поля: 8 кирпичиков в расширенном тексте — две страницы --
        await h.text("/cancel")
        await h.text("/new_wizard")
        await h.text("/start")
        place_id, _alias_id = _seed_place(h, name="Дом с допами")
        specs: list[tuple[str, str, str, dict]] = [
            ("main", "Основное", "short_text", {})
        ]
        specs.extend(
            (f"extra{number}", f"Доп {number}", "short_text", {}) for number in range(8)
        )
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="С допами",
            specs=specs,
            content="[main]",
            extended_content="[main]\n"
            + "\n".join(f"[extra{number}]" for number in range(8)),
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Главное")
        await _no_photos(h)
        await _open_edit(h)
        # 2.5.3 §5: «📝 Текстовые поля» заменяют клавиатуру анонса, поэтому
        # доп. поля открываются после возврата управления «◀️ Назад».
        await _open_control(h, "extras")

        self.assertEqual(
            [label for label, _data in _nav_buttons(h, "noop:", "aw_exbpage:")],
            ["1/2", "▶️"],
            "первая страница доп. полей",
        )
        next_page = _nav_buttons(h, "aw_exbpage:")[0][1]
        assert next_page is not None
        self.assertEqual(next_page.split(":")[-1], "1", "callback хранит номер")
        await h.callback(next_page)
        self.assertEqual(
            [label for label, _data in _nav_buttons(h, "noop:", "aw_exbpage:")],
            ["◀️", "2/2"],
            "последняя страница доп. полей",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoGridAcceptanceTests(unittest.TestCase):
    """П.11: фото-сетка не менялась — 8 снимков на одну страницу."""

    @scenario_test
    async def test_photo_grid_unchanged(self, h: Harness) -> None:
        self.assertEqual(PHOTO_PAGE_SIZE, 8, "размер фото-страницы не менялся")

        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h, content="[main]")
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Текст анонса")
        self.assertIn(cat.text("wizard.photos.title"), h.screen_text())

        for index in range(8):
            await h.photo(f"grid-{index}", media_group_id="mg-grid")
        await asyncio.sleep(0.6)

        # 2.5.2 §2: после публикации служебного экрана нет — коллекция фото
        # открывается управлением самого анонса (`anp_photos`).
        # 2.5.3 §6: фото-режим — клавиатура панели альбома, отдельного экрана
        # больше нет, поэтому раскладка читается из живых кнопок чата.
        photos_control = h.control_callback("photos")
        self.assertIsNotNone(photos_control, "у анонса нет кнопки «Фото»")
        assert photos_control is not None
        await h.callback(photos_control)
        self.assertIsNone(h.screen_message_id(), "служебного экрана нет")
        self.assertEqual(
            len(h.live_callbacks("aw_photo_del:")),
            8,
            "все восемь фото на одной странице",
        )
        self.assertEqual(
            h.live_callbacks("aw_photos:"),
            [],
            "на восьми фото навигации по страницам нет",
        )
        labels = [label for label, _data in h.live_buttons()]
        self.assertNotIn("▶️", labels)
        self.assertTrue(
            not any(label == "◀️" for label in labels),
            f"стрелки страниц нет: {labels}",
        )
        self.assertIn("◀️ Назад", labels, "выход из фото-режима на месте")


class ConditionalSnapshotAcceptanceTests(TemplateDatabaseTestCase):
    """П.12: заморозка правил в снимке после миграции 28."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="snapshot-acceptance")
        self.main_brick = self.make_personal_brick(self.owner, "main")
        self.cost_brick = self.make_personal_brick(self.owner, "cost")
        self.template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="С ценой",
            content=CONTENT_V1,
        )

    # --- помощники ---------------------------------------------------------

    def _revision_of(self, template_id: int) -> int:
        return int(
            self.session.execute(
                select(Template.revision).where(Template.id == template_id)
            ).scalar_one()
        )

    def _add_rule(self):
        return create_conditional_part(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
            expected_revision=self._revision_of(self.template.template_id),
            section="content",
            brick_id=self.cost_brick,
            fragment=HIDDEN_FRAGMENT,
        )

    def _start_draft(self, template_id: int):
        return start_announcement_wizard(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            origin=ORIGIN_PERSONAL,
            template_id=template_id,
        )

    def _save(self, draft, brick_id: int, value: object):
        return save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=brick_id,
            value=value,
            expected_revision=draft.revision,
        )

    def _preview_by_id(self, draft_id: int) -> str:
        return build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft_id,
        ).html

    def _restart(self) -> None:
        self.session.close()
        self.engine.dispose()
        self.engine = make_engine(self.path)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.Session()

    def _frozen_flag(self, snapshot_id: int) -> int:
        return int(
            self.session.execute(
                text(
                    "SELECT conditional_rules_frozen FROM template_snapshots "
                    "WHERE id = :snapshot_id"
                ),
                {"snapshot_id": snapshot_id},
            ).scalar_one()
        )

    # --- сценарий ----------------------------------------------------------

    def test_conditional_snapshot_e2e_and_frozen_flag(self) -> None:
        template_id = self.template.template_id
        created = self._add_rule()
        self.assertEqual(created.status, "created", created.message)

        draft = self._start_draft(template_id)
        self.assertIsNotNone(draft.template_snapshot, "снимок зафиксирован")
        self.assertEqual(draft.template_version, 1)
        draft_id = draft.draft_id

        draft = self._save(draft, self.main_brick, "Концерт")
        frozen = self._preview_by_id(draft_id)
        self.assertEqual(
            frozen,
            EMPTY_COST_V1,
            "правило снимка скрывает фрагмент в строке с литералом",
        )
        self.assertNotIn("[cost]", frozen)

        updated = update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=template_id,
            expected_revision=self._revision_of(template_id),
            content=CONTENT_V2,
        )
        self.assertTrue(updated.ok, updated.message)
        self.assertEqual(self._revision_of(template_id), 2)
        self.session.expire_all()
        self.assertEqual(
            self._preview_by_id(draft_id),
            frozen,
            "перенос правила в новую версию не меняет старый черновик",
        )

        self._restart()
        self.assertEqual(
            self._preview_by_id(draft_id),
            frozen,
            "после рестарта снимок рендерится идентично",
        )

        # --- снимок остаётся источником правил при любом значении признака --
        # 2.5.2 §8: легаси-путь «правила по версии черновика» удалён, признак
        # `conditional_rules_frozen` больше не влияет на рендер. Снимок без
        # строк правил не начинает применять правило, добавленное позже —
        # даже если в базе остался `frozen = 0` от прежних версий.
        legacy_template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Без правила",
            content=CONTENT_V1,
        )
        legacy_draft = self._start_draft(legacy_template.template_id)
        legacy_id = legacy_draft.draft_id
        legacy_draft = self._save(legacy_draft, self.main_brick, "Концерт")
        before = self._preview_by_id(legacy_id)
        self.assertEqual(
            before,
            "<b>Концерт – </b>\nКонец",
            "предусловие: правила на момент создания не было",
        )

        snapshot_id = legacy_draft.template_snapshot.snapshot_id
        self.assertEqual(self._frozen_flag(snapshot_id), 1)
        self.session.execute(text("DROP TRIGGER trg_template_snapshots_no_update"))
        self.session.execute(
            text(
                "UPDATE template_snapshots SET conditional_rules_frozen = 0 "
                "WHERE id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
        self.session.commit()
        self.assertEqual(self._frozen_flag(snapshot_id), 0, "признак сброшен")

        added = create_conditional_part(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=legacy_template.template_id,
            expected_revision=self._revision_of(legacy_template.template_id),
            section="content",
            brick_id=self.cost_brick,
            fragment=HIDDEN_FRAGMENT,
        )
        self.assertEqual(added.status, "created", added.message)
        self.session.expire_all()
        self.assertEqual(
            self._preview_by_id(legacy_id),
            before,
            "снимок рендерится по своей копии правил: новое правило версии "
            "и признак frozen=0 на рендер не влияют",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
