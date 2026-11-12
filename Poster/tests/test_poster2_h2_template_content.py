# tests/test_poster2_h2_template_content.py
"""
Poster 2.3 H2 (часть 1): единый `content` шаблона и необязательный
`extended_content`.

Четыре независимые проверки контракта H2 на временных SQLite (реальная
`post_bot.db`, сеть и Telegram не используются):

1. **Golden байт-в-байт.** Прежний эталон v4 состоял из трёх частей
   (`header`/`body`/`footer`); их склейка по прежней формуле побайтово
   совпадает с `BUILTIN_TEMPLATE_CONTENT`, а рендер этой строки совпадает с
   литеральным golden-HTML для матрицы: все значения заполнены / пустое
   описание / пустой контакт / оба пустых. То есть перенос секций в одну
   строку не меняет ни одного символа готового анонса.

2. **Единый `content` в базовой схеме.** Свежая база 1.0.0 сразу содержит
   `content` и `extended_content`, колонок секций и `section`/`position` у
   extras нет; схема совпадает со свежим `create_all`, история неизменяема.

3. **Скрытие пустых brick-строк.** Строка, в которой есть только токены
   кирпичиков, отрендерившиеся пустыми, исчезает целиком (вместе с висячим
   `//` и разметкой `<b></b>`). Литеральные строки и пустые строки вне
   brick-строк сохраняются буквально.

4. **`extended_content` при выбранных extras.** Анонс без дополнительных
   кирпичиков рендерит `content`, с дополнительными — `extended_content`
   (extras дописываются строками в его конец). Удаление extras возвращает
   канонический текст; значения кирпичиков самого шаблона при этом целы.
"""

from __future__ import annotations

import unittest
from typing import Any

from sqlalchemy import text as sql_text

from announcement_store import (
    add_announcement_extra,
    build_announcement_preview,
    build_announcement_render_payload,
    remove_announcement_extra,
    save_announcement_value,
)
from brick_store import get_system_brick
from brick_types import SYSTEM_BRICKS
from template_compiler import render_announcement, render_template
from template_parser import BrickNode, FormatNode, LinkNode, parse_template
from template_store import create_personal_template
from template_types import (
    BUILTIN_TEMPLATE_CONTENT,
    BUILTIN_TEMPLATE_EXTENDED_CONTENT,
    TEMPLATE_CONTENT,
)
from tests.stage27_helpers import OWNER_TG, AnnouncementDatabaseTestCase

# --- Эталон v5 (литералы, без импорта констант продукта) --------------------
#
# §1.1: между датой и временем — запятая и пробел; контакт — с подписью
# «Мероприятие ведёт:».

REFERENCE_HEADER = (
    "**[название]**\n"
    "\n"
    "**[дата], [время_начала]**\n"
    "**[место]([ссылка_места])**\n"
    "[адрес]"
)
REFERENCE_BODY = "\n[описание]"
REFERENCE_FOOTER = "\nМероприятие ведёт: [контакт]"

# §1.2: расширенный текст того же стандарта. Хранится рядом с простым.
REFERENCE_EXTENDED_CONTENT = (
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


# Формула склейки прежних секций: разделитель только между непустыми (по
# `.strip()`) соседними частями. Для эталона v4 тело и подвал уже начинаются
# с перевода строки, поэтому склейка равна прежней трёхчастной записи.
def _join(header: str, body: str, footer: str) -> str:
    """Склейка прежних секций в единый `content` по исторической формуле."""
    if not (header.strip() or body.strip() or footer.strip()):
        return header + body + footer
    text = header.strip()
    if text and (body.strip() or footer.strip()):
        text += "\n"
    text += body
    if body.strip() and footer.strip():
        text += "\n"
    return text + footer


# --- Golden-HTML: ровно то, что увидит Telegram -----------------------------

GOLDEN_ALL_FILLED = (
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
# Пустое описание: его строка скрывается вместе с соседней пустой, поэтому
# между адресом и контактом остаётся ровно один пустой разделитель шаблона.
GOLDEN_EMPTY_DESCRIPTION = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07 (пн), 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1\n"
    "\n"
    "Мероприятие ведёт: @contact"
)
# Пустой контакт: скрыта вся строка «Мероприятие ведёт: …» (§2.1), текст
# заканчивается описанием.
GOLDEN_EMPTY_CONTACT = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07 (пн), 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1\n"
    "\n"
    "Большой зал, вход свободный."
)
GOLDEN_EMPTY_BOTH = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07 (пн), 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1"
)

FILLED_VALUES = {
    "название": "Летний концерт",
    "дата": "12.07.2027",
    "время_начала": "19:00",
    "место": "Дом культуры",
    "ссылка_места": "https://example.com/place",
    "адрес": "ул. Ленина, 1",
    "описание": "Большой зал, вход свободный.",
    "контакт": "@contact",
    # §1.2: поля расширенного текста. Простой текст их не содержит, поэтому
    # на golden простого варианта они не влияют.
    "время_окончания": "21:00",
    "город": "Москва",
    "условие_входа": "Вход свободный",
    "куратор": "@curator",
    "подвал": "До встречи!",
}

# Golden расширенного текста при всех заполненных значениях (§1.2, §2.2).
GOLDEN_EXTENDED_ALL_FILLED = (
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

SYSTEM_BY_CODE = {item.code: item for item in SYSTEM_BRICKS}
BINDINGS = {code: SYSTEM_BY_CODE[code].id for code in FILLED_VALUES}


def _definitions() -> dict[int, Any]:
    """Определения системных кирпичиков: ID → описание для компилятора."""
    return {definition.id: definition for definition in SYSTEM_BRICKS}


def _bound_tokens(text: str) -> list[Any]:
    """Токены текста с brick_id из постоянных системных привязок."""
    result: list[Any] = []
    for token in parse_template(text):
        if isinstance(token, BrickNode):
            result.append(BrickNode(code=token.code, brick_id=BINDINGS[token.code]))
        elif isinstance(token, LinkNode):
            url = token.url
            if isinstance(url, BrickNode):
                url = BrickNode(code=url.code, brick_id=BINDINGS[url.code])
            result.append(
                LinkNode(label=tuple(_bound_tokens_children(token.label)), url=url)
            )
        elif isinstance(token, FormatNode):
            result.append(
                FormatNode(
                    marker=token.marker,
                    children=tuple(_bound_tokens_children(token.children)),
                )
            )
        else:
            result.append(token)
    return result


def _bound_tokens_children(children: Any) -> list[Any]:
    """Дети разметки/подписи ссылки с сохранением структуры узлов."""
    result: list[Any] = []
    for token in children:
        if isinstance(token, BrickNode):
            result.append(BrickNode(code=token.code, brick_id=BINDINGS[token.code]))
        elif isinstance(token, LinkNode):
            url = token.url
            if isinstance(url, BrickNode):
                url = BrickNode(code=url.code, brick_id=BINDINGS[url.code])
            result.append(
                LinkNode(label=tuple(_bound_tokens_children(token.label)), url=url)
            )
        elif isinstance(token, FormatNode):
            result.append(
                FormatNode(
                    marker=token.marker,
                    children=tuple(_bound_tokens_children(token.children)),
                )
            )
        else:
            result.append(token)
    return result


def _values(**overrides: str) -> dict[int, Any]:
    """Значения по brick_id; пустые строки не передаются (это «не задано»)."""
    return {
        BINDINGS[code]: value
        for code, value in {**FILLED_VALUES, **overrides}.items()
        if value
    }


class TemplateContentGoldenTests(unittest.TestCase):
    """Склейка прежних секций и рендер единого `content` — байт-в-байт."""

    def test_sections_join_equals_builtin_content(self) -> None:
        joined = _join(REFERENCE_HEADER, REFERENCE_BODY, REFERENCE_FOOTER)
        self.assertEqual(joined, BUILTIN_TEMPLATE_CONTENT)
        # Прежний рендер склеивал непустые секции через "\n" — результат
        # совпадает с единым текстом H2 побайтово.
        self.assertEqual(
            joined,
            "\n".join((REFERENCE_HEADER, REFERENCE_BODY, REFERENCE_FOOTER)),
        )

    def test_extended_content_present_in_builtin(self) -> None:
        # §1: у стандарта ДВА сохранённых текста; расширенный создаётся сразу.
        self.assertEqual(BUILTIN_TEMPLATE_EXTENDED_CONTENT, REFERENCE_EXTENDED_CONTENT)

    def _render(self, text: str, values: dict[int, Any]) -> str:
        return render_template(
            {TEMPLATE_CONTENT: _bound_tokens(text)},
            values,
            _definitions(),
        )

    def _render_extended(self, values: dict[int, Any]) -> str:
        """Рендер сохранённого расширенного текста стандарта (§1.2)."""
        from template_types import TEMPLATE_EXTENDED_CONTENT

        return render_template(
            {
                TEMPLATE_CONTENT: _bound_tokens(BUILTIN_TEMPLATE_CONTENT),
                TEMPLATE_EXTENDED_CONTENT: _bound_tokens(REFERENCE_EXTENDED_CONTENT),
            },
            values,
            _definitions(),
            use_extended=True,
        )

    def test_golden_matrix_matches_joined_content_byte_for_byte(self) -> None:
        """
        Матрица H2: рендер склейки секций = рендер литерала = golden-эталон.

        Случаи — все значения заполнены, пустое описание, пустой контакт,
        оба пустых. Ожидаемый HTML записан здесь литерально: смена поведения
        «пустой рендер» → «скрытие пустых brick-строк» в H2 осознанная и
        утверждена, поэтому пустая строка не оставляет висячих разделителей.
        """
        joined = _join(REFERENCE_HEADER, REFERENCE_BODY, REFERENCE_FOOTER)
        matrix: tuple[tuple[str, dict[str, str], str], ...] = (
            ("all filled", {}, GOLDEN_ALL_FILLED),
            ("empty description", {"описание": ""}, GOLDEN_EMPTY_DESCRIPTION),
            ("empty contact", {"контакт": ""}, GOLDEN_EMPTY_CONTACT),
            ("both empty", {"описание": "", "контакт": ""}, GOLDEN_EMPTY_BOTH),
        )
        for name, overrides, golden in matrix:
            with self.subTest(case=name):
                values = _values(**overrides)
                from_join = self._render(joined, values)
                from_literal = self._render(BUILTIN_TEMPLATE_CONTENT, values)
                self.assertEqual(from_join, from_literal, "склейка = литерал")
                self.assertEqual(from_join, golden, "golden байт-в-байт")

    def test_extended_golden_all_filled(self) -> None:
        """
        §1.2/§2.2: рендер сохранённого расширенного текста байт-в-байт.

        `use_extended=True` берёт `extended_content` шаблона: диапазон времени
        через U+2013, «город, адрес» одной строкой, куратор и подвал.
        """
        self.assertEqual(
            self._render_extended(_values()),
            GOLDEN_EXTENDED_ALL_FILLED,
            "расширенный golden байт-в-байт",
        )

    def test_extended_optional_lines_hidden_when_empty(self) -> None:
        # §2.2: условные строки расширенного текста скрываются вместе с
        # разделителями: пустые «условие_входа», «куратор» и «подвал» не
        # оставляют висячих переводов строк.
        values = _values(условие_входа="", куратор="", подвал="")
        rendered = self._render_extended(values)
        self.assertNotIn("Вход свободный", rendered)
        self.assertNotIn("Куратор", rendered)
        self.assertNotIn("До встречи!", rendered)
        self.assertNotIn("\n\n\n", rendered, "нет двойных разделителей")
        self.assertFalse(rendered.endswith("\n"), "нет хвостового перевода строки")
        self.assertIn("Мероприятие ведёт: @contact", rendered)
        self.assertIn("Москва, ул. Ленина, 1", rendered)

    def test_extended_flag_falls_back_to_simple_text(self) -> None:
        # Флаг без сохранённого extended_content деградирует к простому тексту,
        # а не к пустому сообщению (контракт render_template).
        tokens = {TEMPLATE_CONTENT: _bound_tokens(BUILTIN_TEMPLATE_CONTENT)}
        self.assertEqual(
            render_template(tokens, _values(), _definitions(), use_extended=True),
            GOLDEN_ALL_FILLED,
        )

    def test_empty_builtin_render_is_empty_and_gets_placeholder(self) -> None:
        tokens = {TEMPLATE_CONTENT: _bound_tokens(BUILTIN_TEMPLATE_CONTENT)}
        self.assertEqual(render_template(tokens, {}, _definitions()), "")
        self.assertEqual(
            render_announcement(tokens, {}, _definitions()),
            ".",
            "пустой видимый текст без фото → техническая «.»",
        )


# --- Скрытие пустых brick-строк --------------------------------------------

HIDING_TEXT = (
    "Афиша\n"
    "\n"
    "[название]\n"
    "\n"
    "литерал // без токенов\n"
    "\n"
    "\n"
    "**[дата] // [время_начала]**\n"
    "\n"
    "Подпись"
)


class EmptyBrickLineHidingTests(unittest.TestCase):
    """
    Строка с brick-токенами, отрендерившимися пустыми, скрывается целиком.

    H2 (утверждённый контракт): висячий литерал `//` и пустая разметка
    `<b></b>` не должны попадать в готовый анонс. Строки без токенов
    выводятся буквально, включая литеральные `//` и пустые строки.
    """

    def _render(self, text: str, values: dict[int, Any]) -> str:
        return render_template(
            {TEMPLATE_CONTENT: _bound_tokens(text)},
            values,
            _definitions(),
        )

    def test_brick_only_lines_disappear_without_dangling_literals(self) -> None:
        rendered = self._render(HIDING_TEXT, {})
        self.assertNotIn("[название]", rendered)
        self.assertNotIn("//", rendered.replace("литерал // без токенов", ""))
        self.assertNotIn("<b></b>", rendered)
        # Пустая brick-строка не оставляет «дыру»: вместе с ней уходит одна
        # соседняя пустая строка, остальные пустые строки сохраняются.
        self.assertEqual(rendered, "Афиша\n\nлитерал // без токенов\n\n\nПодпись")

    def test_literal_line_keeps_its_own_separator(self) -> None:
        """Литеральный `//` в строке без кирпичиков — не «сирота»."""
        rendered = self._render("а\n//\n\nб", {})
        self.assertEqual(rendered, "а\n//\n\nб")
        # Видимый текст непустой → заглушка «.» не подставляется.
        tokens = {TEMPLATE_CONTENT: _bound_tokens("//")}
        self.assertEqual(
            render_announcement(tokens, {}, _definitions()),
            "//",
            "литеральный `//` остаётся текстом анонса",
        )

    def test_filled_brick_line_is_kept_with_its_literal(self) -> None:
        rendered = self._render(
            HIDING_TEXT,
            {
                BINDINGS["название"]: "Название",
                BINDINGS["дата"]: "01.01.2027",
                BINDINGS["время_начала"]: "19:00",
            },
        )
        self.assertEqual(
            rendered,
            "Афиша\n\nНазвание\n\nлитерал // без токенов\n\n\n"
            "<b>01.01 (пт) // 19:00</b>\n\nПодпись",
        )


# --- extras → extended_content ---------------------------------------------


class ExtendedContentRenderTests(AnnouncementDatabaseTestCase):
    """Анонс рендерит `extended_content` только при выбранных extras."""

    def setUp(self) -> None:
        super().setUp()
        # Poster 2.3.1 (E1): доп. кирпичик стоит на своей позиции в
        # расширенном тексте шаблона. Личный шаблон не видит кирпичики места,
        # поэтому кандидат — личный кирпичик владельца.
        self.extra_brick = self.make_personal_brick(self.owner, "доп_места")
        self.template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Расширяемый",
            content="Основной текст: [название]",
            extended_content="Расширенный текст: [название]\n[доп_места]",
        )
        self.draft = self.start_at_questions(template_id=self.template.template_id)

    def _preview(self) -> str:
        return build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=self.draft.draft_id,
        ).html

    def _title_brick_id(self) -> int:
        """ID системного кирпичика «название» (обязателен в каталоге)."""
        definition = get_system_brick(self.session, "название")
        self.assertIsNotNone(definition)
        assert definition is not None
        return int(definition.id)

    def test_extras_switch_render_between_content_and_extended(self) -> None:
        draft = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=self.draft.draft_id,
            brick_id=self._title_brick_id(),
            value="Концерт",
            expected_revision=self.draft.revision,
        )
        # Без extras: расширение не выбрано, рендерится канонический текст
        # (расширенный текст снимка остаётся доступным, но не используется).
        payload = build_announcement_render_payload(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertFalse(payload.use_extended)
        self.assertEqual(self._preview(), "Основной текст: Концерт")

        added = add_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=self.extra_brick,
            value="Дополнительно",
            expected_revision=draft.revision,
        )
        self.assertEqual([extra.code for extra in added.extras], ["доп_места"])
        payload = build_announcement_render_payload(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertTrue(payload.use_extended)
        # E1: значение extras стоит на позиции токена в расширенном тексте.
        self.assertEqual(self._preview(), "Расширенный текст: Концерт\nДополнительно")

        # Удаление extras возвращает канонический текст; значение самого
        # кирпичика extras удалено вместе с ним (документированное поведение
        # remove), а значения кирпичиков шаблона не затронуты.
        removed = remove_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            extra_id=added.extras[0].extra_id,
            expected_revision=added.revision,
        )
        self.assertEqual(removed.extras, ())
        self.assertIsNone(removed.value_for(self.extra_brick))
        self.assertEqual(
            self.session.execute(
                sql_text(
                    "SELECT brick_id FROM announcement_values "
                    "WHERE draft_id = :draft ORDER BY brick_id"
                ),
                {"draft": draft.draft_id},
            ).fetchall(),
            [(self._title_brick_id(),)],
            "значение канонического кирпичика сохранено",
        )
        self.assertEqual(self._preview(), "Основной текст: Концерт")
