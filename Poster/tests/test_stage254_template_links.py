# tests/test_stage254_template_links.py
r"""
Poster 2.5.4 §5: ссылки в шаблонах при вводе сообщением с форматированием.

Дефект: автор присылает текст шаблона с bold-обёрткой —

    **[место]([ссылка_места])**

— а бот сохраняет `**[место]\([ссылка_места]\)**`: в тексте появляются
обратные слеши, конструкция ссылки разрушается, и вместо кликабельного
названия площадки в анонсе виден буквальный `[место]([ссылка_места])`.

Проверяется весь жизненный цикл §5.2 на настоящем приложении:

    сообщение с entities → `entities_to_template_markup` → сохранение
    секции (template_store, как в редакторе `handlers/templates.py`) →
    повторное чтение → `parse_template` → `render_to_html`

Случаи:

A. `[место]([ссылка_места])` — гиперссылка: видимый текст = значение
   `[место]`, адрес = значение `[ссылка_места]`; при bold название жирное,
   URL уходит в `href`, а не в видимый текст.
Б. `[место] ([ссылка_места])` — НЕ гиперссылка: значение `[место]`, пробел,
   значение `[ссылка_места]` в круглых скобках. Пробел перед `(` — проза.
В. Форматирование части конструкции (§5.3–5.4): стиль только на подписи,
   только на адресе или на всей ссылке даёт одинаковый семантический
   результат — жирную (курсивную, подчёркнутую, зачёркнутую, скрытую)
   кликабельную подпись; адрес всегда уходит в href.

Регрессии, которые нельзя ослабить:

- литеральные скобки в тексте с entities продолжают экранироваться и
  остаются текстом (никаких случайных ссылок);
- `{подпись}(URL)` и настоящие Telegram `text_link` entities работают как
  раньше;
- опасные схемы внутри скобок после кирпичика ссылкой не становятся и
  текст не теряют.

Реальная `post_bot.db`, `.env` и сеть Telegram не используются: настоящий
`Application.process_update` поверх временной SQLite (`Harness`).

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage254_template_links -v
"""

from __future__ import annotations

import unittest

from telegram import MessageEntity

from template_parser import BrickNode, LinkNode, parse_template, render_to_html
from utils.tg_html import entities_to_html, entities_to_template_markup


def ent(kind: str, offset: int, length: int, **kwargs) -> MessageEntity:
    """MessageEntity с явным типом."""
    return MessageEntity(type=kind, offset=offset, length=length, **kwargs)


PLACE_LINE = "[место]([ссылка_места])"
PLACE_LINE_SPACED = "[место] ([ссылка_места])"


def _find_link(tokens: object) -> LinkNode | None:
    """Первая гиперссылка в дереве токенов (рекурсивно) либо None."""
    if isinstance(tokens, LinkNode):
        return tokens
    children = getattr(tokens, "children", None)
    if children is not None:
        return _find_link_from(children)
    if isinstance(tokens, (list, tuple)):
        return _find_link_from(tokens)
    return None


def _find_link_from(tokens) -> LinkNode | None:
    for token in tokens:
        found = _find_link(token)
        if found is not None:
            return found
    return None


class ConverterLinkTests(unittest.TestCase):
    """entities → грамматика: скобки ссылки не должны становиться литералами."""

    def convert(self, text: str, entities: list[MessageEntity] | None) -> str:
        return entities_to_template_markup(text, entities)

    def test_case_a_bold_link_is_kept(self) -> None:
        """A: `**[место]([ссылка_места])**` — ссылка, а не экранированный текст."""
        markup = self.convert(PLACE_LINE, [ent("bold", 0, len(PLACE_LINE))])
        self.assertEqual(markup, f"**{PLACE_LINE}**")
        self.assertNotIn("\\(", markup, "скобки ссылки экранированы")

        tokens = parse_template(markup)
        self.assertEqual(len(tokens), 1)
        link = tokens[0].children[0]
        self.assertIsInstance(link, LinkNode)
        assert isinstance(link, LinkNode)
        self.assertEqual(
            [t.code for t in link.label if isinstance(t, BrickNode)], ["место"]
        )
        self.assertIsInstance(link.url, BrickNode)
        assert isinstance(link.url, BrickNode)
        self.assertEqual(link.url.code, "ссылка_места")
        self.assertEqual(
            render_to_html(tokens),
            '<b><a href="[ссылка_места]">[место]</a></b>',
        )

    def test_case_a_without_entities_is_unchanged(self) -> None:
        """A без форматирования: текст сохраняется как есть (и так ссылка)."""
        self.assertEqual(self.convert(PLACE_LINE, None), PLACE_LINE)

    def test_case_b_space_keeps_prose(self) -> None:
        """Б: пробел перед `(` — проза, а не ссылка (пробел и так разделяет)."""
        markup = self.convert(
            PLACE_LINE_SPACED, [ent("bold", 0, len(PLACE_LINE_SPACED))]
        )
        self.assertIn("**", markup)
        self.assertNotIn("\\(", markup, "литеральная скобка без ссылки")
        tokens = parse_template(markup)
        self.assertFalse(
            any(isinstance(t, LinkNode) for t in tokens),
            f"проза стала ссылкой: {tokens!r}",
        )
        self.assertEqual(render_to_html(tokens), f"<b>{PLACE_LINE_SPACED}</b>")

    def test_bold_label_only_keeps_link_shape(self) -> None:
        """Bold на всей строке — штатный вид стандартного шаблона."""
        markup = self.convert(PLACE_LINE, [ent("bold", 0, len(PLACE_LINE))])
        self.assertEqual(render_to_html(parse_template(markup)).count("<a "), 1)

    def test_partial_formatting_keeps_link(self) -> None:
        """
        §5.3–5.4: форматирование части конструкции ссылку не разрушает.

        Владелец выделяет жирным (или курсивом, подчёркиванием, зачёркиванием,
        спойлером) только подпись, только адрес или всю конструкцию. Во всех
        случаях обязан получиться один и тот же осмысленный результат: ссылка
        с подписью-значением `[место]` и адресом-значением `[ссылка_места]`,
        без обратных слешей в авторском тексте.
        """
        label_end = len("[место]")
        for kind, tag in (
            ("bold", "b"),
            ("italic", "i"),
            ("underline", "u"),
            ("strikethrough", "s"),
            ("spoiler", "tg-spoiler"),
        ):
            with self.subTest(kind=kind):
                # 1. Форматирование только на подписи.
                label_markup = self.convert(PLACE_LINE, [ent(kind, 0, label_end)])
                self.assertNotIn("\\(", label_markup)
                self.assertEqual(
                    render_to_html(parse_template(label_markup)),
                    f'<{tag}><a href="[ссылка_места]">[место]</a></{tag}>',
                )
                # 2. Форматирование только на адресе: в href его нет,
                #    поэтому оно игнорируется, а ссылка собирается.
                address_markup = self.convert(
                    PLACE_LINE,
                    [ent(kind, label_end + 1, len(PLACE_LINE) - label_end - 1)],
                )
                self.assertNotIn("\\(", address_markup)
                self.assertEqual(
                    render_to_html(parse_template(address_markup)),
                    '<a href="[ссылка_места]">[место]</a>',
                )
                # 3. Форматирование на всей конструкции — прежний путь.
                whole_markup = self.convert(PLACE_LINE, [ent(kind, 0, len(PLACE_LINE))])
                self.assertEqual(
                    render_to_html(parse_template(whole_markup)),
                    f'<{tag}><a href="[ссылка_места]">[место]</a></{tag}>',
                )
                # Инвариант: во всех трёх вариантах ссылка одна и та же.
                for markup in (label_markup, address_markup, whole_markup):
                    link = _find_link(parse_template(markup))
                    self.assertIsNotNone(link, markup)
                    assert link is not None
                    self.assertEqual(
                        [t.code for t in link.label if isinstance(t, BrickNode)],
                        ["место"],
                    )
                    self.assertIsInstance(link.url, BrickNode)
                    assert isinstance(link.url, BrickNode)
                    self.assertEqual(link.url.code, "ссылка_места")

    def test_partial_formatting_on_literal_url_keeps_link(self) -> None:
        """Та же логика для литерального адреса: bold на подписи не ломает."""
        text = "[место](https://e.example)"
        markup = self.convert(text, [ent("bold", 0, len("[место]"))])
        self.assertNotIn("\\(", markup)
        self.assertEqual(
            render_to_html(parse_template(markup)),
            '<b><a href="https://e.example">[место]</a></b>',
        )

    def test_partial_formatting_does_not_turn_prose_into_link(self) -> None:
        """Случай Б не затронут: пробел перед `(` остаётся прозой."""
        for kind in ("bold", "italic", "underline"):
            with self.subTest(kind=kind):
                markup = self.convert(PLACE_LINE_SPACED, [ent(kind, 0, len("[место]"))])
                tokens = parse_template(markup)
                self.assertFalse(
                    _find_link(tokens),
                    f"проза стала ссылкой: {markup!r}",
                )

    def test_formatting_spanning_two_links_keeps_both(self) -> None:
        """Диапазон через две ссылки расширяется до целых конструкций."""
        text = f"{PLACE_LINE} и {PLACE_LINE}"
        markup = self.convert(text, [ent("bold", 3, len(text) - 10)])
        rendered = render_to_html(parse_template(markup))
        self.assertEqual(rendered.count("<a "), 2, rendered)
        self.assertNotIn("\\(", markup)
        self.assertTrue(rendered.startswith("<b>"), rendered)
        self.assertTrue(rendered.endswith("</b>"), rendered)

    def test_text_link_with_partial_formatting_is_untouched(self) -> None:
        """Настоящая Telegram-ссылка сохраняет прежнее поведение."""
        text = "[место]([ссылка_места])"
        entities = [
            ent("text_link", 0, len(text), url="https://e.example"),
            ent("bold", 0, len("[место]")),
        ]
        markup = self.convert(text, entities)
        self.assertEqual(
            render_to_html(parse_template(markup)),
            '<a href="https://e.example"><b>[место]</b>([ссылка_места])</a>',
        )

    def test_literal_parens_with_entities_still_escaped(self) -> None:
        """Литеральные скобки в тексте с entities — по-прежнему текст."""
        text = "Смотри (тут) и (там)"
        markup = self.convert(text, [ent("bold", 0, len("Смотри"))])
        self.assertEqual(markup, "**Смотри** \\(тут\\) и \\(там\\)")
        tokens = parse_template(markup)
        self.assertFalse(any(isinstance(t, LinkNode) for t in tokens))
        self.assertEqual(
            render_to_html(tokens), entities_to_html(text, [ent("bold", 0, 6)])
        )

    def test_literal_parens_after_brick_are_escaped(self) -> None:
        """Скобка вплотную к кирпичику, но без адреса внутри — литерал."""
        text = "[место](тут)"
        markup = self.convert(text, [ent("bold", 0, len(text))])
        tokens = parse_template(markup)
        self.assertFalse(
            any(isinstance(t, LinkNode) for t in tokens),
            f"проза стала ссылкой: {tokens!r}",
        )
        self.assertEqual(render_to_html(tokens), "<b>[место](тут)</b>")

    def test_explicit_link_form_still_works(self) -> None:
        """`{подпись}(URL)` — рабочая грамматика, entities её не ломают."""
        self.assertEqual(
            render_to_html(parse_template("{**жирно**}(https://e.example)")),
            '<a href="https://e.example"><b>жирно</b></a>',
        )
        text = "Статья"
        markup = self.convert(
            text, [ent("text_link", 0, len(text), url="https://e.example")]
        )
        self.assertEqual(markup, "Статья(https://e.example)")
        self.assertIn("<a href=", render_to_html(parse_template(markup)))

    def test_url_inside_link_group_is_kept_verbatim(self) -> None:
        """Адрес с `&` и параметрами остаётся адресом, а не литералом."""
        text = "[место](https://e.example/x?a=1&b=2)"
        markup = self.convert(text, [ent("bold", 0, len(text))])
        tokens = parse_template(markup)
        self.assertEqual(len(tokens), 1)
        html = render_to_html(tokens)
        self.assertIn('<a href="https://e.example/x?a=1&amp;b=2">', html)
        self.assertIn("[место]</a>", html)

    def test_plain_number_in_parens_after_brick_is_literal(self) -> None:
        """`[цена](100.50)` — проза, а не домен: слеши остаются, ссылки нет."""
        text = "[цена](100.50)"
        markup = self.convert(text, [ent("bold", 0, len(text))])
        self.assertIn("\\(", markup)
        tokens = parse_template(markup)
        self.assertFalse(any(isinstance(t, LinkNode) for t in tokens))
        self.assertEqual(render_to_html(tokens), f"<b>{text}</b>")

    def test_link_label_does_not_get_nested_link(self) -> None:
        """Подпись text_link с `](…)` не даёт вложенной ссылки в href."""
        text = "[место]([ссылка_места])"
        markup = self.convert(
            text,
            [
                ent("text_link", 0, len(text), url="https://e.example"),
                ent("bold", 0, len(text)),
            ],
        )
        html = render_to_html(parse_template(markup))
        self.assertEqual(html.count("<a "), 1, f"вложенная ссылка: {html}")
        self.assertIn('href="https://e.example"', html)
        self.assertIn("[место]([ссылка_места])", html)

    def test_dangerous_scheme_degrades_without_losing_text(self) -> None:
        """`javascript:` в скобках после кирпичика — текст, не ссылка, без падения."""
        text = "[место](javascript:alert(1))"
        markup = self.convert(text, [ent("bold", 0, len(text))])
        html = render_to_html(parse_template(markup))
        self.assertNotIn("<a ", html)
        self.assertIn("[место]", html)
        self.assertIn("javascript", html)


# --- Интеграция: редактор шаблонов на настоящем приложении -------------------

try:
    from sqlalchemy import select

    from models import TemplateVersion
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _seed_personal_template,
        _seed_place,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def _version_content(h, template_id: int, revision: int) -> str:
    with h.session_factory() as session:
        version = session.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.version == revision,
            )
        )
        if version is None:
            raise AssertionError(f"Версия {revision} шаблона {template_id} не найдена")
        return version.content


def _version_extended(h, template_id: int, revision: int) -> str | None:
    with h.session_factory() as session:
        version = session.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.version == revision,
            )
        )
        if version is None:
            raise AssertionError(f"Версия {revision} шаблона {template_id} не найдена")
        return version.extended_content


def _version_bricks(h, template_id: int, revision: int) -> dict[str, int]:
    from models import TemplateVersionBrick

    with h.session_factory() as session:
        version = session.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.version == revision,
            )
        )
        if version is None:
            raise AssertionError(f"Версия {revision} шаблона {template_id} не найдена")
        rows = session.scalars(
            select(TemplateVersionBrick).where(
                TemplateVersionBrick.template_version_id == version.id
            )
        ).all()
        return {row.code: row.brick_id for row in rows}


def _render_version(h, template_id: int, values_by_id: dict[int, str]) -> str:
    from template_store import render_template_version

    with h.session_factory() as session:
        return render_template_version(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            template_id=template_id,
            values_by_id=values_by_id,
        )


async def _open_personal_editor(h, template_id: int) -> None:
    await h.text("/templates")
    await h.callback(f"tp_open:{template_id}")
    await h.callback(f"tp_edit:{template_id}")


async def _open_standard_editor(h, place_id: int) -> int:
    """
    Открывает редактор стандартного шаблона места через кабинет площадки.

    Прямой `tp_open` показывает только личные шаблоны, поэтому маршрут тот
    же, что у автора: карточка места → «Стандартный шаблон» → правка.
    """
    await h.callback("manage")
    await h.callback("manage_places")
    await h.callback(f"plm_open:{place_id}")
    await h.callback(f"adm_edit_place:{place_id}")
    await h.callback("adm_cancel")
    await h.callback(f"tp_std:{place_id}")
    template_id = int(
        next(
            data.split(":", 1)[1]
            for _label, data in h.screen_buttons()
            if data and data.startswith("tp_edit:")
        )
    )
    await h.callback(f"tp_edit:{template_id}")
    return template_id


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PersonalTemplateLinkTests(unittest.TestCase):
    """Личный шаблон: ссылка из entities переживает сохранение и рендер."""

    @scenario_test
    async def test_bold_link_saved_and_rendered(self, h) -> None:
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Шаблон §5",
            specs=[
                ("nazvanie", "Название", "short_text", {}),
                ("ssylka", "Ссылка", "url", {}),
            ],
            content="Старый [nazvanie]",
        )
        await _open_personal_editor(h, template_id)

        text = "[nazvanie]([ssylka])"
        await h.text(text, entities=[ent("bold", 0, len(text))])
        # Подтверждение показывает канонический текст без обратных слешей.
        self.assertIn(f"**{text}**", h.screen_text())
        self.assertNotIn("\\(", h.screen_text())
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, f"**{text}**")
        self.assertNotIn("\\(", stored, "в сохранённом тексте появились слеши")
        bindings = _version_bricks(h, template_id, 2)
        self.assertEqual(
            {code: bindings[code] for code in ("nazvanie", "ssylka")},
            {"nazvanie": bricks["nazvanie"], "ssylka": bricks["ssylka"]},
        )

        rendered = _render_version(
            h,
            template_id,
            {
                bricks["nazvanie"]: "Дом культуры",
                bricks["ssylka"]: "https://example.com/place",
            },
        )
        self.assertEqual(
            rendered, '<b><a href="https://example.com/place">Дом культуры</a></b>'
        )

    @scenario_test
    async def test_partial_bold_on_label_saved_and_rendered(self, h) -> None:
        """§5.3: жирная только подпись — в версии одна ссылка, без слешей."""
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Шаблон §5-label-bold",
            specs=[
                ("nazvanie", "Название", "short_text", {}),
                ("ssylka", "Ссылка", "url", {}),
            ],
            content="Старый [nazvanie]",
        )
        await _open_personal_editor(h, template_id)

        text = "[nazvanie]([ssylka])"
        await h.text(text, entities=[ent("bold", 0, len("[nazvanie]"))])
        shown = h.screen_text()
        self.assertNotIn("\\(", shown, "в подтверждении появились слеши")
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertNotIn("\\(", stored, "ссылка разрушена экранированием")
        link = _find_link(parse_template(stored))
        self.assertIsNotNone(link, stored)
        assert link is not None
        self.assertEqual(
            [t.code for t in link.label if isinstance(t, BrickNode)], ["nazvanie"]
        )
        self.assertIsInstance(link.url, BrickNode)
        assert isinstance(link.url, BrickNode)
        self.assertEqual(link.url.code, "ssylka")
        bindings = _version_bricks(h, template_id, 2)
        self.assertEqual(bindings.get("nazvanie"), bricks["nazvanie"])
        self.assertEqual(bindings.get("ssylka"), bricks["ssylka"])

        rendered = _render_version(
            h,
            template_id,
            {
                bricks["nazvanie"]: "Дом культуры",
                bricks["ssylka"]: "https://example.com/place",
            },
        )
        self.assertEqual(
            rendered,
            '<b><a href="https://example.com/place">Дом культуры</a></b>',
        )

    @scenario_test
    async def test_spaced_link_stays_prose_after_save(self, h) -> None:
        """Б: пробел перед `(` — значения стоят рядом, гиперссылки нет."""
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Шаблон §5-Б",
            specs=[
                ("nazvanie", "Название", "short_text", {}),
                ("ssylka", "Ссылка", "url", {}),
            ],
            content="Старый [nazvanie]",
        )
        await _open_personal_editor(h, template_id)

        text = "[nazvanie] ([ssylka])"
        await h.text(text, entities=[ent("bold", 0, len(text))])
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, f"**{text}**")
        rendered = _render_version(
            h,
            template_id,
            {
                bricks["nazvanie"]: "Дом культуры",
                bricks["ssylka"]: "https://example.com/place",
            },
        )
        self.assertNotIn("<a ", rendered, "проза стала ссылкой")
        self.assertIn("Дом культуры", rendered)
        self.assertIn("(https://example.com/place)", rendered)

    @scenario_test
    async def test_extended_section_link_is_saved(self, h) -> None:
        """Расширенная секция хранит ссылку тем же путём."""
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Шаблон §5-ext",
            specs=[
                ("nazvanie", "Название", "short_text", {}),
                ("ssylka", "Ссылка", "url", {}),
            ],
            content="[nazvanie]",
            extended_content="[nazvanie]",
        )
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit_ext:{template_id}")

        text = "[nazvanie]([ssylka])"
        await h.text(text, entities=[ent("bold", 0, len(text))])
        await h.callback("tp_save")

        extended = _version_extended(h, template_id, 2)
        self.assertEqual(extended, f"**{text}**")
        # Расширенная секция рендерится только при выбранных доп. кирпичиках,
        # поэтому здесь проверяем её собственный текст: конструкция ссылки
        # сохранена, грамматика читает её как ссылку, а не как литерал.
        self.assertNotIn("\\(", extended or "")
        tokens = parse_template(extended or "")
        self.assertEqual(len(tokens), 1)
        link = tokens[0].children[0]
        self.assertIsInstance(link, LinkNode)
        assert isinstance(link, LinkNode)
        self.assertIsInstance(link.url, BrickNode)
        assert isinstance(link.url, BrickNode)
        self.assertEqual(link.url.code, "ssylka")
        html = render_to_html(tokens)
        self.assertIn('<a href="[ssylka]">', html)
        self.assertIn("<b>", html)

    @scenario_test
    async def test_reopened_editor_shows_link_without_backslashes(self, h) -> None:
        """Повторное открытие редактора: в черновике нет `\\(`."""
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Шаблон §5-reopen",
            specs=[
                ("nazvanie", "Название", "short_text", {}),
                ("ssylka", "Ссылка", "url", {}),
            ],
            content="Старый [nazvanie]",
        )
        await _open_personal_editor(h, template_id)
        text = "[nazvanie]([ssylka])"
        await h.text(text, entities=[ent("bold", 0, len(text))])
        await h.callback("tp_save")

        await h.callback(f"tp_edit:{template_id}")
        shown = h.screen_text()
        self.assertIn(f"**{text}**", shown)
        self.assertNotIn("\\(", shown, "черновик показывает слеши")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StandardTemplateLinkTests(unittest.TestCase):
    """Стандартный шаблон места: `**[место]([ссылка_места])**` из entities."""

    @scenario_test
    async def test_standard_template_place_link_from_entities(self, h) -> None:
        from brick_store import get_system_brick

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id = await _open_standard_editor(h, place_id)

        text = PLACE_LINE
        await h.text(text, entities=[ent("bold", 0, len(text))])
        # Подтверждение показывает авторскую запись без обратных слешей.
        self.assertIn(f"**{text}**", h.screen_text())
        self.assertNotIn("\\(", h.screen_text())
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, f"**{text}**", "ссылка разрушена экранированием")
        bindings = _version_bricks(h, template_id, 2)
        with h.session_factory() as session:
            place_brick = get_system_brick(session, "место")
            url_brick = get_system_brick(session, "ссылка_места")
        assert place_brick is not None and url_brick is not None
        self.assertEqual(bindings.get("место"), place_brick.id)
        self.assertEqual(bindings.get("ссылка_места"), url_brick.id)

        rendered = _render_version(
            h,
            template_id,
            {
                place_brick.id: "Дом культуры",
                url_brick.id: "https://example.com/place",
            },
        )
        self.assertEqual(
            rendered, '<b><a href="https://example.com/place">Дом культуры</a></b>'
        )

    @scenario_test
    async def test_standard_template_unsafe_url_keeps_text(self, h) -> None:
        """Опасная схема в скобках: сохранение не падает, текст цел."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id = await _open_standard_editor(h, place_id)

        text = "[место](javascript:alert(1))"
        await h.text(text, entities=[ent("bold", 0, len(text))])
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertIn("[место]", stored)
        self.assertIn("javascript", stored)
        rendered = _render_version(h, template_id, {})
        self.assertNotIn("<a ", rendered)
