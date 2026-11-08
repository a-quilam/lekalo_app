# tests/test_poster2_u1a_template_v4.py
"""
U1a — встроенный шаблон v5 (этап A1): два текста, структура и ссылка.

Эталон v5 записан здесь ЛИТЕРАЛЬНО (не импортом из продукта), а продукт
сверяется с ним побайтово. Проверяется:

- константы `BUILTIN_TEMPLATE_*`: версия 5, простой и расширенный тексты;
- рендер заполненного простого анонса побайтово: заголовок, сразу под ним
  «дата, время» (§1.1 — запятая вместо `//`), одна пустая строка перед
  площадкой, адрес отдельной нежирной строкой, одна пустая строка перед
  описанием и одна перед «Мероприятие ведёт: …»;
- рендер расширенного анонса побайтово (§1.2): `[условие_входа]` под
  заголовком, дата и время через `–` (U+2013) без пробелов, «город, адрес»,
  куратор и блок подвала;
- рендер пустого анонса: строки с пустыми кирпичиками скрываются целиком,
  поэтому шаблон пуст, а единый рендер для отправки даёт техническую «.»;
- шапка простого текста содержит ровно пять кодов-подписей (название, дата,
  время_начала, место, адрес), `ссылка_места` — только цель ссылки
  `[место](...)`; тело — только `[описание]`, подвал — контакт с подписью;
- `[город]` есть только в расширенном тексте и не появляется в простом
  рендере, даже если значение города передано;
- `LinkNode` без URL выводит подпись без `<a>` (жирность сохраняется), с
  URL — жирную кликабельную ссылку;
- стандартный шаблон, созданный для места, действительно v5 с 13 привязками.

Только временная SQLite и чистый компилятор: без сети, Telegram и
реальной `post_bot.db`.
"""

from __future__ import annotations

import os
import shutil
import unittest
from typing import Any

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
from migrations import run_migrations
from place_store import create_place
from template_compiler import render_announcement, render_template
from template_parser import BrickNode, FormatNode, LinkNode, parse_template
from template_store import get_standard_template, render_template_version
from template_types import (
    BUILTIN_TEMPLATE_CONTENT,
    BUILTIN_TEMPLATE_EXTENDED_CONTENT,
    BUILTIN_TEMPLATE_VERSION,
    TEMPLATE_CONTENT,
    TEMPLATE_EXTENDED_CONTENT,
)
from tests.stage19a_helpers import make_engine, temp_db_path
from user_store import get_or_create_user
from utils.tg_html import entities_to_html

# --- Эталон v5 (только литералы, без импорта из продукта) -------------------

REFERENCE_VERSION = 5
REFERENCE_HEADER = (
    "**[название]**\n"
    "\n"
    "**[дата], [время_начала]**\n"
    "**[место]([ссылка_места])**\n"
    "[адрес]"
)
REFERENCE_BODY = "\n[описание]"
REFERENCE_FOOTER = "\nМероприятие ведёт: [контакт]"

# Poster 2.3 H2: шаблон хранит один канонический текст. Эталон v5 побайтово
# совпадает с прежней склейкой трёх блоков через "\n" (миграция 24).
REFERENCE_CONTENT = REFERENCE_HEADER + "\n" + REFERENCE_BODY + "\n" + REFERENCE_FOOTER

# Этап A1 (§1.2): расширенный вариант того же шаблона. Время окончания идёт
# через символ `–` (U+2013) без пробелов, город и адрес — через запятую,
# куратор — отдельной строкой с подписью, подвал — отдельным блоком.
REFERENCE_EXTENDED_HEADER = (
    "**[название]**\n"
    "[условие_входа]\n"
    "\n"
    "**[дата], [время_начала]–[время_окончания]**\n"
    "**[место]([ссылка_места])**\n"
    "[город], [адрес]"
)
REFERENCE_EXTENDED_BODY = "\n[описание]"
REFERENCE_EXTENDED_FOOTER = (
    "\nМероприятие ведёт: [контакт]\n"
    "Куратор от площадки: [куратор]\n"
    "\n"
    "[подвал]"
)
REFERENCE_EXTENDED_CONTENT = (
    REFERENCE_EXTENDED_HEADER
    + "\n"
    + REFERENCE_EXTENDED_BODY
    + "\n"
    + REFERENCE_EXTENDED_FOOTER
)
# Эталон расширенного анонса (§2.2): все значения заполнены.
EXTENDED_REFERENCE = (
    "<b>Летний концерт</b>\n"
    "Вход свободный\n"
    "\n"
    "<b>12.07 (пн), 19:00–21:00</b>\n"
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
# Расширенный анонс без необязательных значений (§2.2): строки «Условие
# входа», «Куратор от площадки» и блок подвала скрываются целиком.
EXTENDED_MINIMAL_REFERENCE = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07 (пн), 19:00–21:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "Москва, ул. Ленина, 1\n"
    "\n"
    "Большой зал, вход свободный.\n"
    "\n"
    "Мероприятие ведёт: @contact"
)

# Побайтовый ожидаемый HTML: ровно 9 строк, как их увидит Telegram.
FILLED_REFERENCE = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07 (пн), 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1\n"
    "\n"
    "Большой зал, вход свободный.\n"
    "\n"
    "Мероприятие ведёт: @contact"
)

# H2: каждая строка эталона содержит кирпичик, поэтому при пустых значениях
# все строки скрываются целиком (вместе с литеральной запятой и разметкой) и
# шаблон рендерится в пустую строку. Заглушка «.» — решение уровня сообщения
# (`render_announcement`), а не шаблона.
EMPTY_REFERENCE = ""

FILLED_VALUES = {
    "название": "Летний концерт",
    "дата": "12.07.2027",
    "время_начала": "19:00",
    "место": "Дом культуры",
    "ссылка_места": "https://example.com/place",
    "адрес": "ул. Ленина, 1",
    "описание": "Большой зал, вход свободный.",
    "контакт": "@contact",
    # Этап A1 (§1.2): поля расширенного варианта. Простой текст их не
    # содержит, поэтому его рендер от них не зависит.
    "время_окончания": "21:00",
    "город": "Москва",
    "условие_входа": "Вход свободный",
    "куратор": "@curator",
    "подвал": "До встречи!",
}

OWNER_TG = 9_843_001


def _walk_codes(tokens: Any) -> list[str]:
    """Все коды кирпичиков секции, включая подпись и цель ссылки."""
    codes: list[str] = []
    for token in tokens:
        if isinstance(token, BrickNode):
            codes.append(token.code)
        elif isinstance(token, LinkNode):
            codes.extend(_walk_codes(token.label))
            if isinstance(token.url, BrickNode):
                codes.append(token.url.code)
        elif isinstance(token, FormatNode):
            codes.extend(_walk_codes(token.children))
    return codes


def _label_codes(tokens: Any) -> list[str]:
    """Коды-подписи: всё, что печатается, кроме цели ссылки."""
    codes: list[str] = []
    for token in tokens:
        if isinstance(token, BrickNode):
            codes.append(token.code)
        elif isinstance(token, LinkNode):
            codes.extend(_label_codes(token.label))
        elif isinstance(token, FormatNode):
            codes.extend(_label_codes(token.children))
    return codes


class _TemplateCase(unittest.TestCase):
    """Временная база и стандартный шаблон места."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-u1a-v4-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)
        self.user = get_or_create_user(self.session, OWNER_TG, username="v4-owner")
        self.place = create_place(
            self.session,
            "Дом культуры",
            city="Москва",
            created_by_user_id=self.user.id,
            address="ул. Ленина, 1",
            url="https://example.com/place",
        )
        self.standard = get_standard_template(self.session, OWNER_TG, self.place.id)

    # --- помощники --------------------------------------------------------

    def values(self, **overrides: str) -> dict[int, str]:
        result: dict[int, str] = {}
        for code, value in {**FILLED_VALUES, **overrides}.items():
            brick = get_system_brick(self.session, code)
            if brick is None:  # pragma: no cover — реестр системных обязателен
                raise AssertionError(f"нет системного кирпичика {code!r}")
            result[brick.id] = value
        return result

    def brick_id(self, code: str) -> int:
        brick = get_system_brick(self.session, code)
        if brick is None:  # pragma: no cover — реестр системных обязателен
            raise AssertionError(f"нет системного кирпичика {code!r}")
        return int(brick.id)

    def render(self, values: dict[int, str]) -> str:
        return render_template_version(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.standard.template_id,
            values_by_id=values,
        )

    def render_announcement_variant(
        self,
        values: dict[int, str],
        *,
        extended: bool,
        place_id: int | None = None,
        template_id: int | None = None,
    ) -> str:
        """
        Живой рендер черновика: вариант выбирает флаг шаблона (§1–3).

        Единственный штатный путь, которым расширенный текст вообще попадает
        в анонс: флаг копируется в снимок, и рендер идёт по снимку.
        """
        place_id = place_id if place_id is not None else self.place.id
        standard = get_standard_template(self.session, OWNER_TG, place_id)
        template_id = (
            template_id if template_id is not None else standard.template_id
        )
        self.session.execute(
            sql_text("UPDATE templates SET extended_active = :flag WHERE id = :id"),
            {"flag": int(extended), "id": template_id},
        )
        self.session.commit()
        self.session.expire_all()

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
            template_id=template_id,
            expected_revision=confirmed.revision,
        )
        revision = chosen.revision
        for brick_id, value in values.items():
            saved = save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=chosen.draft_id,
                brick_id=brick_id,
                value=value,
                expected_revision=revision,
            )
            revision = saved.revision
        payload = build_announcement_render_payload(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=chosen.draft_id,
        )
        self.assertEqual(payload.use_extended, extended)
        return build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=chosen.draft_id,
        ).html

    def tokens(self, part: str) -> list[Any]:
        """Токены эталонного блока (литерал теста, не константа продукта)."""
        text = {
            "content": REFERENCE_CONTENT,
            "header": REFERENCE_HEADER,
            "body": REFERENCE_BODY,
            "footer": REFERENCE_FOOTER,
            "extended": REFERENCE_EXTENDED_CONTENT,
            "extended_header": REFERENCE_EXTENDED_HEADER,
            "extended_body": REFERENCE_EXTENDED_BODY,
            "extended_footer": REFERENCE_EXTENDED_FOOTER,
        }[part]
        return parse_template(text, section=TEMPLATE_CONTENT)

    def bound_tokens(self, part: str) -> list[Any]:
        """Токены блока с brick_id из сохранённых привязок стандарта."""
        bindings = {
            binding.code: binding.brick_id
            for binding in self.standard.current_version.bindings
        }

        def _bind(tokens: list[Any]) -> list[Any]:
            result: list[Any] = []
            for token in tokens:
                if isinstance(token, BrickNode):
                    result.append(
                        BrickNode(code=token.code, brick_id=bindings[token.code])
                    )
                elif isinstance(token, LinkNode):
                    url = token.url
                    if isinstance(url, BrickNode):
                        url = BrickNode(code=url.code, brick_id=bindings[url.code])
                    result.append(
                        LinkNode(label=tuple(_bind(list(token.label))), url=url)
                    )
                elif isinstance(token, FormatNode):
                    result.append(
                        FormatNode(
                            marker=token.marker,
                            children=tuple(_bind(list(token.children))),
                        )
                    )
                else:
                    result.append(token)
            return result

        return _bind(self.tokens(part))

    def definitions(self) -> dict[int, Any]:
        definitions: dict[int, Any] = {}
        for binding in self.standard.current_version.bindings:
            brick = get_system_brick(self.session, binding.code)
            if brick is None:  # pragma: no cover
                raise AssertionError(f"нет системного кирпичика {binding.code!r}")
            definitions[brick.id] = brick
        return definitions


class BuiltinTemplateV5ConstantsTests(unittest.TestCase):
    """Константы продукта совпадают с эталоном v5 побайтово."""

    def test_version_is_five(self) -> None:
        self.assertEqual(BUILTIN_TEMPLATE_VERSION, REFERENCE_VERSION)

    def test_content_matches_reference_byte_for_byte(self) -> None:
        self.assertEqual(BUILTIN_TEMPLATE_CONTENT, REFERENCE_CONTENT)

    def test_extended_content_matches_reference_byte_for_byte(self) -> None:
        # Этап A1 (§1): у стандарта ДВА сохранённых текста; расширенный
        # создаётся сразу вместе с простым и не удаляется при выключении.
        self.assertEqual(
            BUILTIN_TEMPLATE_EXTENDED_CONTENT, REFERENCE_EXTENDED_CONTENT
        )


class TemplateV5RenderTests(_TemplateCase):
    """Побайтовый рендер заполненного и пустого простого анонса."""

    def test_filled_announcement_is_byte_exact(self) -> None:
        self.assertEqual(self.render(self.values()), FILLED_REFERENCE)

    def test_empty_announcement_is_byte_exact(self) -> None:
        rendered = self.render({})
        self.assertEqual(rendered, EMPTY_REFERENCE)
        # Единый рендер для отправки подставляет техническую «.».
        payload = {TEMPLATE_CONTENT: self.bound_tokens("content")}
        definitions = self.definitions()
        self.assertEqual(render_template(payload, {}, definitions), EMPTY_REFERENCE)
        # Пустой видимый текст и нет фото: `render_announcement` отдаёт
        # техническую «.».
        self.assertEqual(render_announcement(payload, {}, definitions), ".")

    def test_blank_lines_only_between_blocks(self) -> None:
        lines = self.render(self.values()).split("\n")
        self.assertEqual(len(lines), 9, lines)
        # Ровно одна пустая строка сразу после заголовка.
        self.assertEqual(lines[0], "<b>Летний концерт</b>")
        self.assertEqual(lines[1], "", "между заголовком и датой нет пустой")
        # §1.1: между датой и временем начала — запятая и пробел.
        self.assertEqual(lines[2], "<b>12.07 (пн), 19:00</b>")
        # Площадка — соседняя с датой строка, пустой между ними нет.
        self.assertTrue(lines[3].startswith("<b>"))
        # Адрес — сразу под площадкой, нежирный.
        self.assertEqual(lines[4], "ул. Ленина, 1")
        # Ровно одна пустая строка перед описанием и перед контактом.
        self.assertEqual(lines[5], "")
        self.assertEqual(lines[6], "Большой зал, вход свободный.")
        self.assertEqual(lines[7], "")
        self.assertEqual(lines[8], "Мероприятие ведёт: @contact")
        self.assertEqual(
            [index for index, line in enumerate(lines) if line == ""], [1, 5, 7]
        )

    def test_place_link_is_bold_and_clickable(self) -> None:
        rendered = self.render(self.values())
        self.assertIn(
            '<b><a href="https://example.com/place">Дом культуры</a></b>', rendered
        )
        # Проверка на уровне entities: text_link на площадке + жирность
        # вокруг него. HTML → entities → HTML не теряет разметку.
        text = (
            "Летний концерт\n"
            "\n"
            "12.07 (пн), 19:00\n"
            "Дом культуры\n"
            "ул. Ленина, 1\n"
            "\n"
            "Большой зал, вход свободный.\n"
            "\n"
            "Мероприятие ведёт: @contact"
        )
        entities = self._entities(text)
        rebuilt = entities_to_html(text, entities)
        self.assertEqual(rebuilt, rendered)
        links = [item for item in entities if item.type == "text_link"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].url, "https://example.com/place")

    def _entities(self, text: str) -> list[Any]:
        """Entities эквивалентного Telegram-сообщения эталона v5."""
        from telegram import MessageEntity

        def _span(label: str, *, start_at: int = 0) -> tuple[int, int]:
            index = text.index(label, start_at)
            offset = len(text[:index].encode("utf-16-le")) // 2
            return offset, len(label.encode("utf-16-le")) // 2

        title_offset, title_length = _span("Летний концерт")
        date_offset, date_length = _span("12.07 (пн), 19:00")
        place_offset, place_length = _span("Дом культуры")
        return [
            MessageEntity(
                type=MessageEntity.BOLD, offset=title_offset, length=title_length
            ),
            MessageEntity(
                type=MessageEntity.BOLD, offset=date_offset, length=date_length
            ),
            MessageEntity(
                type=MessageEntity.BOLD, offset=place_offset, length=place_length
            ),
            MessageEntity(
                type=MessageEntity.TEXT_LINK,
                offset=place_offset,
                length=place_length,
                url="https://example.com/place",
            ),
        ]

    def test_place_without_url_keeps_bold_name_without_anchor(self) -> None:
        values = self.values()
        values.pop(self.brick_id("ссылка_места"))
        lines = self.render(values).split("\n")
        self.assertEqual(lines[3], "<b>Дом культуры</b>")
        self.assertNotIn("<a ", lines[3])
        self.assertNotIn("</a>", lines[3])
        self.assertTrue(lines[3].startswith("<b>") and lines[3].endswith("</b>"))
        self.assertEqual(lines[4], "ул. Ленина, 1")

    def test_blank_url_is_treated_as_absent(self) -> None:
        values = self.values()
        values[self.brick_id("ссылка_места")] = ""
        rendered = self.render(values)
        self.assertIn("<b>Дом культуры</b>", rendered)
        self.assertNotIn("<a ", rendered)


class TemplateV5BrickSetTests(_TemplateCase):
    """Набор кодов по секциям: эталон v5, город только в расширенном тексте."""

    def test_header_has_exactly_five_label_codes(self) -> None:
        labels = _label_codes(self.tokens("header"))
        self.assertEqual(labels, ["название", "дата", "время_начала", "место", "адрес"])
        self.assertEqual(len(labels), 5)

    def test_link_target_is_only_refmesta(self) -> None:
        tokens = self.tokens("header")
        links = [token for token in tokens if isinstance(token, LinkNode)] + [
            child
            for token in tokens
            if isinstance(token, FormatNode)
            for child in token.children
            if isinstance(child, LinkNode)
        ]
        self.assertEqual(len(links), 1, "в шапке ровно одна гиперссылка")
        link = links[0]
        target = link.url
        self.assertIsInstance(target, BrickNode)
        assert isinstance(target, BrickNode)
        self.assertEqual(target.code, "ссылка_места")
        self.assertEqual(_label_codes(link.label), ["место"])

    def test_body_has_only_description_and_footer_only_contact(self) -> None:
        self.assertEqual(_walk_codes(self.tokens("body")), ["описание"])
        # §1.1: контакт в подвале подписан, подпись — литерал шаблона.
        self.assertEqual(_walk_codes(self.tokens("footer")), ["контакт"])

    def test_all_header_codes_are_the_reference_set(self) -> None:
        self.assertEqual(
            set(_walk_codes(self.tokens("header"))),
            {"название", "дата", "время_начала", "место", "ссылка_места", "адрес"},
        )

    def test_extended_text_has_its_own_brick_set(self) -> None:
        # §1.2: расширенный текст добавляет условие входа, время окончания,
        # город, куратора и подвал; базовые коды те же.
        self.assertEqual(
            _label_codes(self.tokens("extended_header")),
            [
                "название",
                "условие_входа",
                "дата",
                "время_начала",
                "время_окончания",
                "место",
                "город",
                "адрес",
            ],
        )
        self.assertEqual(_walk_codes(self.tokens("extended_body")), ["описание"])
        self.assertEqual(
            _walk_codes(self.tokens("extended_footer")),
            ["контакт", "куратор", "подвал"],
        )

    def test_city_is_only_in_the_extended_text(self) -> None:
        simple_codes = set(_walk_codes(self.tokens("content")))
        self.assertNotIn("город", simple_codes)
        extended_codes = set(_walk_codes(self.tokens("extended")))
        self.assertIn("город", extended_codes)
        self.assertIn("куратор", extended_codes)
        self.assertIn("подвал", extended_codes)

    def test_city_value_does_not_change_the_simple_render(self) -> None:
        values = self.values()
        values[self.brick_id("город")] = "Москва"
        self.assertEqual(self.render(values), FILLED_REFERENCE)
        self.assertNotIn("Москва", self.render(values))

    def test_extended_render_is_byte_exact(self) -> None:
        # Расширенный текст рендерится через штатный путь анонса: флаг
        # шаблона копируется в снимок черновика (§1, §3).
        rendered = self.render_announcement_variant(self.values(), extended=True)
        self.assertEqual(rendered, EXTENDED_REFERENCE)

    def test_simple_render_does_not_use_extended_fields(self) -> None:
        # §3: при выключенном флаге дополнительные поля не появляются в
        # анонсе, даже если их значения сохранены.
        rendered = self.render_announcement_variant(self.values(), extended=False)
        self.assertEqual(rendered, FILLED_REFERENCE)
        for fragment in ("Вход свободный", "@curator", "До встречи!", "–"):
            self.assertNotIn(fragment, rendered)

    def test_extended_render_hides_optional_lines_when_empty(self) -> None:
        values = self.values()
        for code in ("условие_входа", "куратор", "подвал"):
            values.pop(self.brick_id(code))
        self.assertEqual(
            self.render_announcement_variant(values, extended=True),
            EXTENDED_MINIMAL_REFERENCE,
        )

    def test_extended_render_without_city_and_address(self) -> None:
        # §2.2: «ни города, ни адреса» — это состояние карточки площадки,
        # поэтому нужна отдельная площадка без адреса и с пустым городом.
        bare_place = create_place(
            self.session, "Дом культуры", city="Москва", created_by_user_id=self.user.id
        )
        self.session.execute(
            sql_text("UPDATE places SET city = NULL WHERE id = :id"),
            {"id": bare_place.id},
        )
        self.session.commit()
        values = self.values()
        values.pop(self.brick_id("город"))
        values.pop(self.brick_id("адрес"))
        # У этой площадки нет и ссылки: подпись остаётся жирной без `<a>`.
        values.pop(self.brick_id("ссылка_места"))
        # §2.2: при пустых городе и адресе строка скрывается целиком, поэтому
        # описание примыкает к площадке без пустой строки.
        self.assertEqual(
            self.render_announcement_variant(
                values, extended=True, place_id=bare_place.id
            ),
            (
                "<b>Летний концерт</b>\n"
                "Вход свободный\n"
                "\n"
                "<b>12.07 (пн), 19:00–21:00</b>\n"
                "<b>Дом культуры</b>\n"
                "Большой зал, вход свободный.\n"
                "\n"
                "Мероприятие ведёт: @contact\n"
                "Куратор от площадки: @curator\n"
                "\n"
                "До встречи!"
            ),
        )

    def test_extended_render_without_end_time_drops_dash(self) -> None:
        values = self.values()
        values.pop(self.brick_id("время_окончания"))
        rendered = self.render_announcement_variant(values, extended=True)
        lines = rendered.split("\n")
        # §2.2: тире уходит вместе со временем окончания.
        self.assertEqual(lines[3], "<b>12.07 (пн), 19:00</b>")
        self.assertNotIn("–", rendered)

    def test_standard_template_for_place_is_v5(self) -> None:
        version = self.standard.current_version
        self.assertEqual(version.version, 1)
        self.assertEqual(version.builtin_version, REFERENCE_VERSION)
        self.assertEqual(version.content, REFERENCE_CONTENT)
        self.assertEqual(version.extended_content, REFERENCE_EXTENDED_CONTENT)
        # Этап A1: 13 привязок — базовые 8 плюс условие входа, время
        # окончания, город, куратор и подвал. Сохранённый порядок — по
        # brick_id, поэтому сверяем состав, а не последовательность.
        self.assertEqual(len(version.bindings), 13)
        self.assertEqual(
            {binding.code for binding in version.bindings},
            {
                "название",
                "условие_входа",
                "дата",
                "время_начала",
                "время_окончания",
                "место",
                "ссылка_места",
                "город",
                "адрес",
                "описание",
                "контакт",
                "куратор",
                "подвал",
            },
        )
        self.assertEqual(
            [binding.brick_id for binding in version.bindings],
            [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14],
        )
        # По умолчанию стандарт простой (§1.3): флаг выключен, оба текста
        # сохранены.
        self.assertFalse(self.standard.extended_active)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
