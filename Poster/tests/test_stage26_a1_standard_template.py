# tests/test_stage26_a1_standard_template.py
"""
Независимая приёмка этапа A1 (§19.1 а–ж): стандартный шаблон площадки.

Проверяется пользовательский контракт, а не реализация Developer:

- (а) у новой площадки ровно один стандартный шаблон, в нём оба текста,
  по 3 + 8 автоматических условных частей, расширенный вариант выключен;
- (б) простой вариант со всеми полями даёт побайтово HTML §1.1: настоящие
  `<b>`/`<a href>`, запятая между датой и временем, «Мероприятие ведёт:»;
- (в) пустые необязательные значения не оставляют подписей, запятых,
  висячих строк, пустых абзацев и Markdown-разметки (по одному и вместе);
- (г) расширенный вариант (§1.2): строка условия, `–` (U+2013), «город,
  адрес» в четырёх комбинациях, «Куратор от площадки:», подвал; пустое
  время окончания убирает тире, пустые необязательные — свои строки;
- (д) `extended_active` заморожен в снимке: переключение флага не меняет
  начатый черновик, новый черновик берёт текущий флаг;
- (е) миграции 1–32 на чистой временной базе; повторный запуск миграций,
  `backfill_standard_templates` и `ensure_standard_template` идемпотентны —
  ни кирпичики 12–14, ни правила, ни шаблоны не дублируются;
- (ж) сброс стандарта сохраняет расширенный текст и 11 правил, выключает
  флаг; включение флага снова даёт расширенный вариант.

Итоговый HTML — единственный эталон для (б)–(г): строки ниже записаны
ЛИТЕРАЛЬНО, без импорта констант продукта. Сценарии идут через штатный
сервисный путь анонса (место → подтверждение → выбор шаблона → значения →
предпросмотр), потому что вариант стандарта выбирает флаг снимка; плюс
сквозные проверки через настоящее приложение и FakeBot (без сети).

Реальная `post_bot.db`, `.env` и Telegram не используются: только временная
SQLite в /tmp.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage26_a1_standard_template -v
"""

from __future__ import annotations

import os
import shutil
import unittest
from datetime import date
from typing import cast

from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session, sessionmaker

from announcement_store import (
    build_announcement_preview,
    build_announcement_render_payload,
    choose_announcement_template,
    confirm_announcement_place,
    save_announcement_value,
    start_announcement_wizard,
)
from brick_store import get_system_brick
from conditional_parts import list_conditional_parts
from migrations import LATEST_VERSION, run_migrations
from models import Brick, PlaceAdmin, Template, TemplateSnapshot, utcnow
from place_store import create_place
from template_store import (
    TemplateConfirmationRequired,
    TemplatePermissionError,
    backfill_standard_templates,
    ensure_standard_template,
    get_standard_template,
    reset_standard_template,
)
from tests.integration_harness import (
    DEFAULT_USER_ID,
    Harness,
    frozen_today,
    scenario_test,
)
from tests.stage19a_helpers import make_engine, temp_db_path
from user_store import get_or_create_user

OWNER_TG = 26_011_001
OTHER_TG = 26_011_002
TODAY = date(2026, 11, 2)

# --- Эталонные тексты стандарта (литералы, не импорт из продукта) -----------

SIMPLE_TEXT = (
    "**[название]**\n"
    "\n"
    "**[дата], [время_начала]**\n"
    "**[место]([ссылка_места])**\n"
    "[адрес]\n"
    "\n"
    "[описание]\n"
    "\n"
    "Мероприятие ведёт: [контакт]"
)

EXTENDED_TEXT = (
    "**[название]**\n"
    "[условие_входа]\n"
    "\n"
    "**[дата], [время_начала]–[время_окончания]**\n"
    "**[место]([ссылка_места])**\n"
    "[город], [адрес]\n"
    "\n"
    "[описание]\n"
    "\n"
    "Мероприятие ведёт: [контакт]\n"
    "Куратор от площадки: [куратор]\n"
    "\n"
    "[подвал]"
)

# §2: автоматические условные части — 3 в простом тексте и 8 в расширенном.
SIMPLE_RULES = (
    ("адрес", "\n[адрес]", 1),
    ("описание", "[описание]", 1),
    ("контакт", "Мероприятие ведёт: [контакт]", 1),
)
EXTENDED_RULES = (
    ("условие_входа", "\n[условие_входа]", 1),
    ("время_окончания", "–[время_окончания]", 1),
    ("город", "[город], ", 1),
    ("адрес", ", [адрес]", 1),
    ("описание", "[описание]", 1),
    ("контакт", "Мероприятие ведёт: [контакт]", 1),
    ("куратор", "Куратор от площадки: [куратор]", 1),
    ("подвал", "[подвал]", 1),
)

# 13 привязок в порядке brick_id: имена/дата/время/место/ссылка/описание/
# контакт/адрес/город и три новых кирпичика этапа A1.
EXPECTED_BINDINGS = (
    (1, "название"),
    (2, "дата"),
    (3, "время_начала"),
    (4, "время_окончания"),
    (5, "место"),
    (7, "ссылка_места"),
    (8, "описание"),
    (9, "контакт"),
    (10, "адрес"),
    (11, "город"),
    (12, "условие_входа"),
    (13, "куратор"),
    (14, "подвал"),
)

PLACE_VALUES = {
    "name": "Дом культуры",
    "city": "Москва",
    "address": "ул. Ленина, 1",
    "url": "https://example.com/place",
}

SIMPLE_VALUES = {
    "название": "Летний концерт",
    "дата": "09.11",
    "время_начала": "19:00",
    "описание": "Большой зал, вход свободный.",
    "контакт": "@contact",
}

EXTENDED_VALUES = {
    **SIMPLE_VALUES,
    "время_окончания": "21:00",
    "условие_входа": "Вход свободный",
    "куратор": "@curator",
    "подвал": "До встречи!",
}

SIMPLE_FULL_HTML = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>09.11 (пн), 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1\n"
    "\n"
    "Большой зал, вход свободный.\n"
    "\n"
    "Мероприятие ведёт: @contact"
)

EXTENDED_FULL_HTML = (
    "<b>Летний концерт</b>\n"
    "Вход свободный\n"
    "\n"
    "<b>09.11 (пн), 19:00–21:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "Москва, ул. Ленина, 1\n"
    "\n"
    "Большой зал, вход свободный.\n"
    "\n"
    "Мероприятие ведёт: @contact\n"
    "Куратор от площадки: @curator\n"
    "\n"
    "До встречи!"
)


class _StandardTemplateCase(unittest.TestCase):
    """Временная база: миграции 1–32, чистая Session и место с админом."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-a1-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.migrations_applied = run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)
        self.user = get_or_create_user(self.session, OWNER_TG, username="owner")

    # --- сущности ---------------------------------------------------------

    def make_place(
        self,
        *,
        name: str = PLACE_VALUES["name"],
        city: str | None = PLACE_VALUES["city"],
        address: str | None = PLACE_VALUES["address"],
        url: str | None = PLACE_VALUES["url"],
    ) -> int:
        """Площадка с админом владельца; `city=None` — историческая запись."""
        place = create_place(
            self.session,
            name,
            city=city or "Москва",
            created_by_user_id=self.user.id,
            address=address,
            url=url,
        )
        self.session.add(
            PlaceAdmin(
                place_id=place.id,
                user_id=self.user.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
            )
        )
        if city is None:
            # `create_place` требует непустой город: «сведений нет» — только
            # старая запись, поэтому колонка обнуляется напрямую.
            self.session.execute(
                sql_text("UPDATE places SET city = NULL WHERE id = :id"),
                {"id": place.id},
            )
        self.session.commit()
        self.session.expire_all()
        return place.id

    def standard(self, place_id: int):
        return get_standard_template(self.session, OWNER_TG, place_id)

    def set_flag(self, template_id: int, value: bool) -> None:
        """Прямая установка флага в обход UI (этап A2 добавил сеттер с правами)."""
        self.session.execute(
            sql_text("UPDATE templates SET extended_active = :flag WHERE id = :id"),
            {"flag": int(value), "id": template_id},
        )
        self.session.commit()
        self.session.expire_all()

    # --- штатный путь рендера --------------------------------------------

    def render(self, place_id: int, values: dict[str, str], *, extended: bool) -> str:
        """
        Предпросмотр анонса по стандарту места.

        Вариант выбирает флаг, зафиксированный в снимке (§3.1), поэтому
        рендер идёт через мастер анонса, а не прямым вызовом компилятора.
        """
        standard = self.standard(place_id)
        self.set_flag(standard.template_id, extended)
        draft = start_announcement_wizard(
            self.session, actor_telegram_user_id=OWNER_TG, place_id=place_id
        )
        confirmed = confirm_announcement_place(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )
        chosen = choose_announcement_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=confirmed.draft_id,
            template_id=standard.template_id,
            expected_revision=confirmed.revision,
        )
        snapshot = chosen.template_snapshot
        self.assertIsNotNone(snapshot, "черновик обязан получить снимок шаблона")
        assert snapshot is not None
        self.assertEqual(
            snapshot.extended_active,
            extended,
            "снимок обязан зафиксировать текущий флаг шаблона",
        )
        revision = chosen.revision
        for code, value in values.items():
            brick = get_system_brick(self.session, code)
            self.assertIsNotNone(brick, code)
            assert brick is not None
            saved = save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=chosen.draft_id,
                brick_id=brick.id,
                value=value,
                expected_revision=revision,
                today=TODAY,
            )
            revision = saved.revision
        payload = build_announcement_render_payload(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=chosen.draft_id
        )
        self.assertEqual(payload.use_extended, extended)
        # Компилятор проверяет относительную дату («09.11») по `app_today()`,
        # а не по переданному `today`: без заморозки год выбирается от реальной
        # даты и «09.11» со временем уходит в прошлое (horizon_months ≥ 0).
        with frozen_today(TODAY.year, TODAY.month, TODAY.day):
            return build_announcement_preview(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=chosen.draft_id,
                today=TODAY,
            ).html

    # --- инварианты сообщения --------------------------------------------

    def assert_clean_html(self, html: str) -> None:
        """Ни мусора разметки, ни висячей пунктуации, ни лишних пустых строк."""
        self.assertNotIn("**", html, "Markdown не должен попадать в Telegram")
        self.assertNotIn("[", html)
        self.assertNotIn("]", html)
        self.assertNotIn("None", html)
        self.assertNotIn(" // ", html, "старый разделитель `//` недопустим")
        self.assertNotIn("\n\n\n", html, "двойной пустой абзац")
        self.assertFalse(html.startswith("\n") or html.endswith("\n"))
        self.assertFalse(html.startswith(" ") or html.endswith(" "))
        self.assertNotIn("  ", html, "двойной пробел/пустая подпись")
        self.assertNotIn(", ,", html)
        self.assertNotIn("()", html, "пустые скобки ссылки")
        self.assertNotIn('href=""', html)
        self.assertNotIn("<b></b>", html, "пустая жирная подпись")
        self.assertEqual(html.count("<b>"), html.count("</b>"))
        self.assertEqual(html.count("<a "), html.count("</a>"))
        for line in html.split("\n"):
            self.assertEqual(line, line.strip())
            self.assertFalse(line.startswith(","), f"висячая запятая: {line!r}")
            self.assertFalse(line.endswith(","), f"висячая запятая: {line!r}")

    # --- правила ----------------------------------------------------------

    def rules(self, template_id: int, section: str) -> list[tuple]:
        return [
            (part.code, part.fragment, part.occurrence)
            for part in list_conditional_parts(
                self.session, OWNER_TG, template_id, section=section
            )
        ]


class StandardTemplateInitialisationTests(_StandardTemplateCase):
    """§19.1 (а): новая площадка получает один готовый стандарт."""

    def test_new_place_has_single_standard_with_both_texts(self) -> None:
        place_id = self.make_place()
        standard = self.standard(place_id)

        self.assertEqual(
            self.session.scalar(
                select(func.count(Template.id)).where(
                    Template.place_id == place_id,
                    Template.kind == "standard",
                )
            ),
            1,
            "у площадки должен быть ровно один стандартный шаблон",
        )
        self.assertEqual(standard.kind, "standard")
        self.assertEqual(standard.place_id, place_id)
        self.assertEqual(standard.current_version.version, 1)
        self.assertEqual(standard.current_version.content, SIMPLE_TEXT)
        self.assertEqual(standard.current_version.extended_content, EXTENDED_TEXT)
        self.assertFalse(
            standard.extended_active,
            "расширенный вариант изначально выключен (§1–3)",
        )

    def test_standard_has_13_bindings_in_brick_id_order(self) -> None:
        place_id = self.make_place()
        standard = self.standard(place_id)
        self.assertEqual(
            tuple(
                (binding.brick_id, binding.code)
                for binding in standard.current_version.bindings
            ),
            EXPECTED_BINDINGS,
        )

    def test_standard_conditional_parts_frozen_in_both_sections(self) -> None:
        place_id = self.make_place()
        standard = self.standard(place_id)

        self.assertEqual(
            self.rules(standard.template_id, "content"), list(SIMPLE_RULES)
        )
        self.assertEqual(
            self.rules(standard.template_id, "extended_content"),
            list(EXTENDED_RULES),
        )
        current_total = len(
            list_conditional_parts(self.session, OWNER_TG, standard.template_id)
        )
        self.assertEqual(current_total, 11, "3 простых + 8 расширенных правил")

    def test_standard_repair_and_reset_require_place_admin(self) -> None:
        place_id = self.make_place()
        get_or_create_user(self.session, OTHER_TG, username="other")
        standard = self.standard(place_id)
        self.set_flag(standard.template_id, True)

        with self.assertRaises(TemplatePermissionError):
            ensure_standard_template(self.session, OTHER_TG, place_id)

        current = self.standard(place_id)
        result = reset_standard_template(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=current.template_id,
            expected_revision=current.revision,
            confirm=True,
        )
        self.assertEqual(result.status, "forbidden")
        self.assertTrue(
            self.standard(place_id).extended_active,
            "чужой сброс не должен выключать расширенный вариант",
        )


class SimpleVariantTests(_StandardTemplateCase):
    """§19.1 (б, в): точный HTML простого варианта и пустые поля."""

    def test_full_simple_announcement_matches_section_1_1_byte_for_byte(self) -> None:
        place_id = self.make_place()
        html = self.render(place_id, SIMPLE_VALUES, extended=False)

        self.assertEqual(html, SIMPLE_FULL_HTML)
        self.assert_clean_html(html)
        # Запятая и пробел между датой и временем, а не `//`.
        self.assertIn("<b>09.11 (пн), 19:00</b>", html)
        self.assertIn('href="https://example.com/place"', html)
        self.assertIn("\nМероприятие ведёт: @contact", html)
        self.assertNotIn("город", html.lower())

    def test_empty_optionals_do_not_leave_labels_commas_or_blank_lines(self) -> None:
        cases = {
            "без контакта": (
                {},
                {k: v for k, v in SIMPLE_VALUES.items() if k != "контакт"},
                (
                    "<b>Летний концерт</b>\n"
                    "\n"
                    "<b>09.11 (пн), 19:00</b>\n"
                    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                    "ул. Ленина, 1\n"
                    "\n"
                    "Большой зал, вход свободный."
                ),
            ),
            "без адреса": (
                {"address": None},
                SIMPLE_VALUES,
                (
                    "<b>Летний концерт</b>\n"
                    "\n"
                    "<b>09.11 (пн), 19:00</b>\n"
                    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                    "\n"
                    "Большой зал, вход свободный.\n"
                    "\n"
                    "Мероприятие ведёт: @contact"
                ),
            ),
            "без ссылки": (
                {"url": None},
                SIMPLE_VALUES,
                (
                    "<b>Летний концерт</b>\n"
                    "\n"
                    "<b>09.11 (пн), 19:00</b>\n"
                    "<b>Дом культуры</b>\n"
                    "ул. Ленина, 1\n"
                    "\n"
                    "Большой зал, вход свободный.\n"
                    "\n"
                    "Мероприятие ведёт: @contact"
                ),
            ),
            "без описания": (
                {},
                {k: v for k, v in SIMPLE_VALUES.items() if k != "описание"},
                (
                    "<b>Летний концерт</b>\n"
                    "\n"
                    "<b>09.11 (пн), 19:00</b>\n"
                    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                    "ул. Ленина, 1\n"
                    "\n"
                    "Мероприятие ведёт: @contact"
                ),
            ),
            "без контакта и адреса": (
                {"address": None},
                {k: v for k, v in SIMPLE_VALUES.items() if k != "контакт"},
                (
                    "<b>Летний концерт</b>\n"
                    "\n"
                    "<b>09.11 (пн), 19:00</b>\n"
                    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                    "\n"
                    "Большой зал, вход свободный."
                ),
            ),
            "только обязательные поля": (
                {"address": None, "url": None},
                {
                    "название": SIMPLE_VALUES["название"],
                    "дата": SIMPLE_VALUES["дата"],
                    "время_начала": SIMPLE_VALUES["время_начала"],
                },
                (
                    "<b>Летний концерт</b>\n"
                    "\n"
                    "<b>09.11 (пн), 19:00</b>\n"
                    "<b>Дом культуры</b>"
                ),
            ),
            "только описание": (
                {"address": None, "url": None},
                {
                    "название": SIMPLE_VALUES["название"],
                    "дата": SIMPLE_VALUES["дата"],
                    "время_начала": SIMPLE_VALUES["время_начала"],
                    "описание": SIMPLE_VALUES["описание"],
                },
                (
                    "<b>Летний концерт</b>\n"
                    "\n"
                    "<b>09.11 (пн), 19:00</b>\n"
                    "<b>Дом культуры</b>\n"
                    "\n"
                    "Большой зал, вход свободный."
                ),
            ),
            "контакт без описания": (
                {},
                {
                    "название": SIMPLE_VALUES["название"],
                    "дата": SIMPLE_VALUES["дата"],
                    "время_начала": SIMPLE_VALUES["время_начала"],
                    "контакт": SIMPLE_VALUES["контакт"],
                },
                (
                    "<b>Летний концерт</b>\n"
                    "\n"
                    "<b>09.11 (пн), 19:00</b>\n"
                    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                    "ул. Ленина, 1\n"
                    "\n"
                    "Мероприятие ведёт: @contact"
                ),
            ),
        }

        for label, (place_overrides, values, expected) in cases.items():
            with self.subTest(label):
                place_id = self.make_place(
                    address=place_overrides.get("address", PLACE_VALUES["address"]),
                    url=place_overrides.get("url", PLACE_VALUES["url"]),
                )
                html = self.render(place_id, values, extended=False)
                self.assertEqual(html, expected)
                self.assert_clean_html(html)
                if "контакт" not in values:
                    self.assertNotIn("Мероприятие ведёт", html)
                else:
                    self.assertIn("Мероприятие ведёт: @contact", html)
                if place_overrides.get("url", PLACE_VALUES["url"]) is None:
                    self.assertNotIn("<a ", html)
                    self.assertNotIn("(https://example.com/place)", html)

    def test_simple_variant_ignores_extended_only_values(self) -> None:
        place_id = self.make_place()
        html = self.render(place_id, EXTENDED_VALUES, extended=False)

        self.assertEqual(html, SIMPLE_FULL_HTML)
        self.assertNotIn("Вход свободный", html)
        self.assertNotIn("21:00", html)
        self.assertNotIn("Куратор от площадки", html)
        self.assertNotIn("До встречи!", html)
        self.assertNotIn("Москва", html)


class ExtendedVariantTests(_StandardTemplateCase):
    """§19.1 (г): точный HTML расширенного варианта (§1.2)."""

    def test_full_extended_announcement_matches_section_1_2(self) -> None:
        place_id = self.make_place()
        html = self.render(place_id, EXTENDED_VALUES, extended=True)

        self.assertEqual(html, EXTENDED_FULL_HTML)
        self.assert_clean_html(html)
        self.assertIn("\nВход свободный\n", html)
        self.assertIn("<b>09.11 (пн), 19:00\u201321:00</b>", html)
        self.assertIn("\u2013", html, "между временем должна быть U+2013")
        self.assertNotIn(" - ", html)
        self.assertIn("\nМосква, ул. Ленина, 1\n", html)
        self.assertIn("\nКуратор от площадки: @curator\n", html)
        self.assertTrue(html.endswith("\n\nДо встречи!"))

    def test_missing_condition_contact_curator_and_footer_hide_their_lines(
        self,
    ) -> None:
        place_id = self.make_place()
        cases = {
            "без условия входа": (
                {k: v for k, v in EXTENDED_VALUES.items() if k != "условие_входа"},
                (
                    "<b>Летний концерт</b>\n"
                    "\n"
                    "<b>09.11 (пн), 19:00–21:00</b>\n"
                    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                    "Москва, ул. Ленина, 1\n"
                    "\n"
                    "Большой зал, вход свободный.\n"
                    "\n"
                    "Мероприятие ведёт: @contact\n"
                    "Куратор от площадки: @curator\n"
                    "\n"
                    "До встречи!"
                ),
            ),
            "без куратора": (
                {k: v for k, v in EXTENDED_VALUES.items() if k != "куратор"},
                (
                    "<b>Летний концерт</b>\n"
                    "Вход свободный\n"
                    "\n"
                    "<b>09.11 (пн), 19:00–21:00</b>\n"
                    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                    "Москва, ул. Ленина, 1\n"
                    "\n"
                    "Большой зал, вход свободный.\n"
                    "\n"
                    "Мероприятие ведёт: @contact\n"
                    "До встречи!"
                ),
            ),
            "без подвала": (
                {k: v for k, v in EXTENDED_VALUES.items() if k != "подвал"},
                (
                    "<b>Летний концерт</b>\n"
                    "Вход свободный\n"
                    "\n"
                    "<b>09.11 (пн), 19:00–21:00</b>\n"
                    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                    "Москва, ул. Ленина, 1\n"
                    "\n"
                    "Большой зал, вход свободный.\n"
                    "\n"
                    "Мероприятие ведёт: @contact\n"
                    "Куратор от площадки: @curator"
                ),
            ),
            "без контакта": (
                {k: v for k, v in EXTENDED_VALUES.items() if k != "контакт"},
                (
                    "<b>Летний концерт</b>\n"
                    "Вход свободный\n"
                    "\n"
                    "<b>09.11 (пн), 19:00–21:00</b>\n"
                    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                    "Москва, ул. Ленина, 1\n"
                    "\n"
                    "Большой зал, вход свободный.\n"
                    "\n"
                    "Куратор от площадки: @curator\n"
                    "\n"
                    "До встречи!"
                ),
            ),
            "без описания": (
                {k: v for k, v in EXTENDED_VALUES.items() if k != "описание"},
                (
                    "<b>Летний концерт</b>\n"
                    "Вход свободный\n"
                    "\n"
                    "<b>09.11 (пн), 19:00–21:00</b>\n"
                    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                    "Москва, ул. Ленина, 1\n"
                    "\n"
                    "Мероприятие ведёт: @contact\n"
                    "Куратор от площадки: @curator\n"
                    "\n"
                    "До встречи!"
                ),
            ),
            "только обязательные поля": (
                {
                    "название": EXTENDED_VALUES["название"],
                    "дата": EXTENDED_VALUES["дата"],
                    "время_начала": EXTENDED_VALUES["время_начала"],
                },
                (
                    "<b>Летний концерт</b>\n"
                    "\n"
                    "<b>09.11 (пн), 19:00</b>\n"
                    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                    "Москва, ул. Ленина, 1"
                ),
            ),
        }

        for label, (values, expected) in cases.items():
            with self.subTest(label):
                html = self.render(place_id, values, extended=True)
                self.assertEqual(html, expected)
                self.assert_clean_html(html)
                if "условие_входа" not in values:
                    self.assertNotIn("Вход свободный", html)
                if "куратор" not in values:
                    self.assertNotIn("Куратор от площадки", html)
                if "подвал" not in values:
                    self.assertNotIn("До встречи!", html)
                if "контакт" not in values:
                    self.assertNotIn("Мероприятие ведёт", html)

    def test_missing_end_time_removes_dash_not_the_whole_line(self) -> None:
        place_id = self.make_place()
        values = {k: v for k, v in EXTENDED_VALUES.items() if k != "время_окончания"}
        html = self.render(place_id, values, extended=True)

        self.assertIn("<b>09.11 (пн), 19:00</b>", html)
        self.assertNotIn("\u2013", html, "без времени окончания тире не остаётся")
        self.assertNotIn("-", html)
        self.assertNotIn("21:00", html)
        self.assertIn("Большой зал, вход свободный.", html)
        self.assert_clean_html(html)

    def test_city_and_address_combinations(self) -> None:
        expected_city_line = {
            "город и адрес": "Москва, ул. Ленина, 1",
            "только город": "Москва",
            "только адрес": "ул. Ленина, 1",
        }
        place_kwargs: dict[str, dict[str, str | None]] = {
            "город и адрес": {},
            "только город": {"address": None},
            "только адрес": {"city": None},
        }
        for label, city_line in expected_city_line.items():
            with self.subTest(label):
                overrides = place_kwargs[label]
                place_id = self.make_place(
                    city=overrides.get("city", PLACE_VALUES["city"]),
                    address=overrides.get("address", PLACE_VALUES["address"]),
                )
                html = self.render(place_id, EXTENDED_VALUES, extended=True)
                self.assertIn(f"\n{city_line}\n", html)
                self.assertNotIn(", ,", html)
                self.assertNotIn("\n,", html)
                self.assert_clean_html(html)
                if label == "только город":
                    self.assertNotIn("ул. Ленина", html)
                if label == "только адрес":
                    self.assertNotIn("Москва", html)

    def test_no_city_and_no_address_hide_the_whole_line(self) -> None:
        place_id = self.make_place(city=None, address=None)
        html = self.render(place_id, EXTENDED_VALUES, extended=True)

        self.assertEqual(
            html,
            (
                "<b>Летний концерт</b>\n"
                "Вход свободный\n"
                "\n"
                "<b>09.11 (пн), 19:00–21:00</b>\n"
                '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                "Большой зал, вход свободный.\n"
                "\n"
                "Мероприятие ведёт: @contact\n"
                "Куратор от площадки: @curator\n"
                "\n"
                "До встречи!"
            ),
        )
        self.assertNotIn("Москва", html)
        for line in html.split("\n"):
            self.assertNotEqual(line, ",", "висячая запятая адреса")
            self.assertFalse(
                line.endswith(", ул. Ленина, 1"), "строка адреса не должна остаться"
            )
        self.assert_clean_html(html)

    def test_extended_without_link_keeps_bold_place_without_anchor(self) -> None:
        place_id = self.make_place(url=None)
        html = self.render(place_id, EXTENDED_VALUES, extended=True)
        self.assertIn("\n<b>Дом культуры</b>\n", html)
        self.assertNotIn("<a ", html)
        self.assert_clean_html(html)


class FlagFreezeTests(_StandardTemplateCase):
    """§19.1 (д, ж): флаг в снимке, начатый черновик и сброс стандарта."""

    def test_started_draft_keeps_its_variant_after_flag_switch(self) -> None:
        place_id = self.make_place()
        standard = self.standard(place_id)
        self.set_flag(standard.template_id, True)

        draft = start_announcement_wizard(
            self.session, actor_telegram_user_id=OWNER_TG, place_id=place_id
        )
        confirmed = confirm_announcement_place(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )
        chosen = choose_announcement_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=confirmed.draft_id,
            template_id=standard.template_id,
            expected_revision=confirmed.revision,
        )
        snapshot = chosen.template_snapshot
        self.assertIsNotNone(snapshot, "черновик обязан получить снимок шаблона")
        assert snapshot is not None
        self.assertTrue(snapshot.extended_active)
        self.assertEqual(
            self.session.scalar(
                select(TemplateSnapshot.extended_active).where(
                    TemplateSnapshot.id == snapshot.snapshot_id
                )
            ),
            1,
            "флаг обязан попасть в строку снимка",
        )

        # Флаг выключают посреди работы над анонсом.
        self.set_flag(standard.template_id, False)
        revision = chosen.revision
        for code, value in EXTENDED_VALUES.items():
            brick = get_system_brick(self.session, code)
            self.assertIsNotNone(brick, code)
            assert brick is not None
            revision = save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=chosen.draft_id,
                brick_id=brick.id,
                value=value,
                expected_revision=revision,
                today=TODAY,
            ).revision
        payload = build_announcement_render_payload(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=chosen.draft_id
        )
        with frozen_today(TODAY.year, TODAY.month, TODAY.day):
            html = build_announcement_preview(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=chosen.draft_id,
                today=TODAY,
            ).html
        self.assertTrue(payload.use_extended, "начатый черновик продолжает вариант")
        self.assertEqual(html, EXTENDED_FULL_HTML)

        # Новый черновик берёт уже текущий (выключенный) флаг.
        html_new = self.render(place_id, SIMPLE_VALUES, extended=False)
        self.assertEqual(html_new, SIMPLE_FULL_HTML)

        # И снова включённый флаг возвращает расширенный вариант.
        html_extended = self.render(place_id, EXTENDED_VALUES, extended=True)
        self.assertEqual(html_extended, EXTENDED_FULL_HTML)

    def test_flag_switch_keeps_extended_text_and_rules(self) -> None:
        place_id = self.make_place()
        standard = self.standard(place_id)
        self.set_flag(standard.template_id, True)

        enabled = self.standard(place_id)
        self.assertTrue(enabled.extended_active)
        self.assertEqual(self.rules(enabled.template_id, "content"), list(SIMPLE_RULES))
        self.assertEqual(
            self.rules(enabled.template_id, "extended_content"), list(EXTENDED_RULES)
        )

        self.set_flag(standard.template_id, False)
        disabled = self.standard(place_id)
        self.assertFalse(disabled.extended_active)
        self.assertEqual(disabled.current_version.content, SIMPLE_TEXT)
        self.assertEqual(
            disabled.current_version.extended_content,
            EXTENDED_TEXT,
            "выключение флага не удаляет расширенный текст (§3)",
        )
        self.assertEqual(
            self.rules(disabled.template_id, "content"), list(SIMPLE_RULES)
        )
        self.assertEqual(
            self.rules(disabled.template_id, "extended_content"),
            list(EXTENDED_RULES),
            "выключение флага не удаляет условные части",
        )

    def test_reset_turns_flag_off_and_keeps_extended_text_and_rules(self) -> None:
        place_id = self.make_place()
        standard = self.standard(place_id)
        self.set_flag(standard.template_id, True)
        current = self.standard(place_id)
        self.assertTrue(current.extended_active)

        result = reset_standard_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=current.template_id,
            expected_revision=current.revision,
            confirm=True,
        )
        self.assertEqual(result.status, "updated")
        self.assertEqual(result.message, "Расширенный вариант стандарта выключен.")

        after = self.standard(place_id)
        self.assertFalse(after.extended_active)
        self.assertEqual(after.current_version.content, SIMPLE_TEXT)
        self.assertEqual(after.current_version.extended_content, EXTENDED_TEXT)
        self.assertEqual(self.rules(after.template_id, "content"), list(SIMPLE_RULES))
        self.assertEqual(
            self.rules(after.template_id, "extended_content"), list(EXTENDED_RULES)
        )

        # Повторный сброс без изменений ничего не ломает и не включает флаг.
        again = reset_standard_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=after.template_id,
            expected_revision=after.revision,
            confirm=True,
        )
        self.assertIn(again.status, {"unchanged", "updated"})
        self.assertFalse(self.standard(place_id).extended_active)

        # Включение флага возвращает расширенный вариант.
        self.assertEqual(
            self.render(place_id, EXTENDED_VALUES, extended=True),
            EXTENDED_FULL_HTML,
        )

    def test_reset_without_confirmation_is_refused(self) -> None:
        place_id = self.make_place()
        standard = self.standard(place_id)
        self.set_flag(standard.template_id, True)
        current = self.standard(place_id)
        with self.assertRaises(TemplateConfirmationRequired) as ctx:
            reset_standard_template(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=current.template_id,
                expected_revision=current.revision,
                confirm=False,
            )
        self.assertIn("подтверж", str(ctx.exception).lower())
        self.assertTrue(self.standard(place_id).extended_active)


class MigrationIdempotencyTests(_StandardTemplateCase):
    """§19.1 (е): миграции 1–32 и повторная инициализация стандарта."""

    def _journal(self) -> list[int]:
        rows = self.session.execute(
            sql_text("SELECT version FROM schema_migrations ORDER BY version")
        ).all()
        return [cast(int, row[0]) for row in rows]

    def _brick_count(self, code: str) -> int:
        return int(
            self.session.scalar(select(func.count(Brick.id)).where(Brick.code == code))
            or 0
        )

    def test_clean_database_applies_migrations_1_to_33_once(self) -> None:
        self.assertEqual(self.migrations_applied, 33)
        self.assertEqual(LATEST_VERSION, 33, "последняя версия схемы — 33")
        self.assertEqual(self._journal(), list(range(1, 34)))

        # Повторный запуск на уже мигрированной базе ничего не добавляет.
        self.assertEqual(run_migrations(self.engine), 33)
        self.assertEqual(self._journal(), list(range(1, 34)))

    def _count(self, model: type) -> int:
        return int(self.session.scalar(select(func.count()).select_from(model)) or 0)

    def test_standard_reinitialisation_does_not_duplicate_anything(self) -> None:
        place_id = self.make_place()
        standard = self.standard(place_id)
        self.assertEqual(self._count(Template), 1)
        self.assertFalse(standard.extended_active)

        self.assertEqual(backfill_standard_templates(self.session), 0)
        repaired = ensure_standard_template(self.session, OWNER_TG, place_id)
        self.assertEqual(repaired.template_id, standard.template_id)
        self.assertEqual(repaired.revision, standard.revision)

        # Повторная миграция на «живой» тестовой базе: ни шаблонов, ни
        # кирпичиков, ни правил не прибавилось.
        self.assertEqual(run_migrations(self.engine), 33)
        self.session.expire_all()
        self.assertEqual(self._count(Template), 1)
        self.assertEqual(
            int(
                self.session.scalar(
                    select(func.count())
                    .select_from(Brick)
                    .where(Brick.scope == "system")
                )
                or 0
            ),
            13,
        )
        for code in ("условие_входа", "куратор", "подвал", "адрес", "город"):
            self.assertEqual(self._brick_count(code), 1, code)
        self.assertEqual(backfill_standard_templates(self.session), 0)
        self.assertEqual(
            len(list_conditional_parts(self.session, OWNER_TG, standard.template_id)),
            11,
        )
        self.assertEqual(
            self.rules(standard.template_id, "content"), list(SIMPLE_RULES)
        )
        self.assertEqual(
            self.rules(standard.template_id, "extended_content"), list(EXTENDED_RULES)
        )


# --- Сквозной сценарий через настоящее приложение и FakeBot -----------------


def _callback(h: Harness, prefix: str, suffix: str | None = None) -> str:
    for _label, data in h.screen_buttons():
        if (
            data
            and data.startswith(prefix)
            and (suffix is None or data.endswith(suffix))
        ):
            return data
    raise AssertionError(f"нет callback {prefix!r}: {h.screen_buttons()}")


async def _seed_place_with_flag(h: Harness, *, extended: bool) -> tuple[int, int]:
    with h.session_factory() as session:
        user = get_or_create_user(session, DEFAULT_USER_ID, username="owner")
        place = create_place(
            session,
            PLACE_VALUES["name"],
            city=PLACE_VALUES["city"],
            created_by_user_id=user.id,
            address=PLACE_VALUES["address"],
            url=PLACE_VALUES["url"],
        )
        session.add(
            PlaceAdmin(
                place_id=place.id,
                user_id=user.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
            )
        )
        session.commit()
        standard = get_standard_template(session, DEFAULT_USER_ID, place.id)
        session.execute(
            sql_text("UPDATE templates SET extended_active = :flag WHERE id = :id"),
            {"flag": int(extended), "id": standard.template_id},
        )
        session.commit()
        return place.id, standard.template_id


async def _fill_and_publish(h: Harness, place_id: int, template_id: int) -> int:
    await h.callback("aw_start")
    await h.callback("aw_new")
    await h.callback("aw_place_search")
    await h.text("Дом")
    await h.callback(f"aw_place_open:{place_id}")
    await h.callback(_callback(h, "aw_place_confirm"))
    with frozen_today(2026, 11, 2):
        await h.callback(_callback(h, "aw_template", f":{template_id}"))
        answers = [
            SIMPLE_VALUES["название"],
            "09.11.2026",
            SIMPLE_VALUES["время_начала"],
            SIMPLE_VALUES["описание"],
            SIMPLE_VALUES["контакт"],
        ]
        for _ in range(8):
            buttons = h.screen_buttons()
            if not any((data or "").startswith("aw_q") for _l, data in buttons):
                break
            value = answers.pop(0) if answers else None
            if value is None:
                await h.callback(
                    next(
                        data
                        for _l, data in buttons
                        if data and data.startswith("aw_qskip")
                    )
                )
            else:
                await h.text(value)
        await h.callback(_callback(h, "aw_photo_none"))
    with h.session_factory() as session:
        draft_id = int(
            session.execute(
                sql_text("SELECT MAX(id) FROM announcement_drafts")
            ).scalar_one()
        )
    return draft_id


class DeliveredPostTests(unittest.TestCase):
    """§19.1: итоговый HTML — это то, что реально уходит в Telegram."""

    @scenario_test
    async def test_delivered_simple_post_matches_expected_html(
        self, h: Harness
    ) -> None:
        await h.text("/start")
        place_id, template_id = await _seed_place_with_flag(h, extended=False)
        draft_id = await _fill_and_publish(h, place_id, template_id)

        self.assertEqual(h.draft_status(draft_id), "sent")
        self.assertEqual(h.posts()[-1], SIMPLE_FULL_HTML)
        self.assertNotIn("Москва", h.posts()[-1] or "")

    @scenario_test
    async def test_delivered_extended_post_matches_expected_html(
        self, h: Harness
    ) -> None:
        await h.text("/start")
        place_id, template_id = await _seed_place_with_flag(h, extended=True)
        draft_id = await _fill_and_publish(h, place_id, template_id)

        self.assertEqual(h.draft_status(draft_id), "sent")
        # Дополнительные поля (условие, время окончания, куратор, подвал) в
        # первоначальном мастере не спрашиваются: строки скрыты, но город и
        # адрес расширенного текста уже присутствуют.
        self.assertEqual(
            h.posts()[-1],
            (
                "<b>Летний концерт</b>\n"
                "\n"
                "<b>09.11 (пн), 19:00</b>\n"
                '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
                "Москва, ул. Ленина, 1\n"
                "\n"
                "Большой зал, вход свободный.\n"
                "\n"
                "Мероприятие ведёт: @contact"
            ),
        )
        with h.session_factory() as session:
            flag: int = int(
                session.execute(
                    sql_text(
                        "SELECT extended_active FROM template_snapshots "
                        "WHERE id = (SELECT template_snapshot_id "
                        "FROM announcement_drafts WHERE id = :draft_id)"
                    ),
                    {"draft_id": draft_id},
                ).scalar_one()
            )
        self.assertEqual(flag, 1, "снимок доставленного анонса помнит флаг")


if __name__ == "__main__":
    unittest.main()
