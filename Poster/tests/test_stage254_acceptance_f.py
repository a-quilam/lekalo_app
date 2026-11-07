# tests/test_stage254_acceptance_f.py
r"""
Poster 2.5.4 §5 + §9 (тест F) — НЕЗАВИСИМАЯ приёмка «Разбор ссылок в шаблонах».

Проверяется пользовательский контракт, а не реализация. Автор набирает текст
в клиенте Telegram и выделяет жирным кнопкой клиента: в сообщении приходит
ТЕКСТ без звёзд + bold-entity на нужном диапазоне. Литеральные `**` в тексте
без entities — не контракт (это авторская запись грамматики, её поведение
закреплено отдельно в тестах разработчика).

Сценарии (все ожидания — собственные, снятые до чтения реализации):

1. A `[место]([ссылка_места])` + bold на всю конструкцию:
   markup `**[место]([ссылка_места])**`, без `\(`/`\)`,
   HTML `<b><a href="[ссылка_места]">[место]</a></b>`.
2. A без entities (и с пустым списком) — текст не меняется.
3. A + italic, A + bold+italic — ссылка внутри оформления.
4. Б `[место] ([ссылка_места])` (пробел) + bold: это НЕ гиперссылка —
   видимый текст «значение место (значение ссылка)», без href.
5. В bold только на подписи (и только на её внутренности) — ссылка цела,
   стиль расширяется до конструкции.
6. Г bold только на адресе (и только на его внутренности) — стиль адреса
   отбрасывается, ссылка цела, жирности нет.
7. Настоящий `text_link` на слове: `слово(URL)`; URL с `&`/параметрами и
   Unicode-подписью; `javascript:` деградирует в текст без потерь.
8. code/pre вокруг конструкции — буквальный спан: ссылка НЕ трактуется.
9. Ссылка с параметрами `…?a=1&b=2` — `&` уходит в `&amp;` только в href,
   в видимом тексте URL нет.
10. `javascript:` внутри скобок после кирпичика — текст, не ссылка, без
    падения и без потери текста.
11. Эмодзи перед конструкцией: entity-смещения в UTF-16, ссылка не съезжает.
12. Битые entities (за концом текста, нулевой длины, отрицательное смещение)
    не роняют конвертер и не теряют текст.

Полный жизненный цикл на настоящем приложении (`bot.build_application`,
FakeBot, временная SQLite): стандартный шаблон места — сохранение через
редактор (`tp_save`) → новая версия в БД → повторное открытие редактора →
рендер сохранённой версии с реальными значениями; расширенная секция личного
шаблона; личный шаблон со своими кирпичиками; правка уже существующего
шаблона; идемпотентная повторная отправка черновика; устаревшая кнопка
`tp_save`.

Реальная `post_bot.db`, `.env` и сеть Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage254_acceptance_f -v
"""

from __future__ import annotations

import unittest

from telegram import MessageEntity

from template_parser import BrickNode, LinkNode, parse_template, render_to_html
from tests import ui_texts_expect as cat
from utils.tg_html import entities_to_template_markup

LINE = "[место]([ссылка_места])"
LINE_SPACED = "[место] ([ссылка_места])"


def ent(kind: str, offset: int, length: int, **kwargs) -> MessageEntity:
    """MessageEntity с явным типом (как приходит от клиента Telegram)."""
    return MessageEntity(type=kind, offset=offset, length=length, **kwargs)


def u16(text: str) -> int:
    """Длина строки в 16-битных код-юнитах — как смещения Telegram."""
    return len(text.encode("utf-16-le")) // 2


def _links(tokens: object) -> list[LinkNode]:
    """Все гиперссылки в дереве токенов (рекурсивно)."""
    if isinstance(tokens, LinkNode):
        return [tokens]
    found: list[LinkNode] = []
    for child in getattr(tokens, "children", None) or ():
        found.extend(_links(child))
    if isinstance(tokens, (list, tuple)):
        for token in tokens:
            found.extend(_links(token))
    return found


class ConvertGoldenTests(unittest.TestCase):
    """entities → авторская запись: золотые ожидания из контракта §5."""

    def convert(self, text: str, entities: list[MessageEntity] | None) -> str:
        return entities_to_template_markup(text, entities)

    # --- 1-3. A: конструкция ссылки + оформление --------------------------

    def test_a_bold_whole_is_hyperlink_without_backslashes(self) -> None:
        """A + bold на всю конструкцию: `**…**`, слешей нет, ссылка цела."""
        markup = self.convert(LINE, [ent("bold", 0, u16(LINE))])

        self.assertEqual(markup, f"**{LINE}**")
        self.assertNotIn("\\(", markup, "скобки ссылки стали литералами")
        self.assertNotIn("\\)", markup, "скобки ссылки стали литералами")

        tokens = parse_template(markup)
        links = _links(tokens)
        self.assertEqual(len(links), 1, f"ожидалась одна ссылка: {markup!r}")
        self.assertEqual(
            [t.code for t in links[0].label if isinstance(t, BrickNode)], ["место"]
        )
        self.assertIsInstance(links[0].url, BrickNode)
        assert isinstance(links[0].url, BrickNode)
        self.assertEqual(links[0].url.code, "ссылка_места")
        self.assertEqual(
            render_to_html(tokens), '<b><a href="[ссылка_места]">[место]</a></b>'
        )

    def test_a_without_entities_is_unchanged(self) -> None:
        """Без форматирования текст шаблона не переписывается."""
        self.assertEqual(self.convert(LINE, None), LINE)
        self.assertEqual(self.convert(LINE, []), LINE)

    def test_a_italic_whole_keeps_link(self) -> None:
        markup = self.convert(LINE, [ent("italic", 0, u16(LINE))])
        self.assertEqual(markup, f"*{LINE}*")
        self.assertEqual(
            render_to_html(parse_template(markup)),
            '<i><a href="[ссылка_места]">[место]</a></i>',
        )

    def test_a_bold_and_italic_whole_nest_link_inside(self) -> None:
        markup = self.convert(
            LINE, [ent("bold", 0, u16(LINE)), ent("italic", 0, u16(LINE))]
        )
        self.assertEqual(markup, f"***{LINE}***")
        html = render_to_html(parse_template(markup))
        self.assertEqual(html, '<b><i><a href="[ссылка_места]">[место]</a></i></b>')
        self.assertEqual(html.count("<a "), 1, "ссылка размножилась")

    # --- 4. Б: пробел перед скобкой — проза, не ссылка --------------------

    def test_b_spaced_construction_is_not_a_link(self) -> None:
        """Б: `[место] ([ссылка_места])` — значения рядом, гиперссылки нет."""
        markup = self.convert(LINE_SPACED, [ent("bold", 0, u16(LINE_SPACED))])
        self.assertEqual(markup, f"**{LINE_SPACED}**")
        self.assertNotIn("\\)", markup, "лишние слеши у прозы")
        self.assertNotIn("\\(", markup, "лишние слеши у прозы")

        tokens = parse_template(markup)
        self.assertEqual(_links(tokens), [], "проза со пробелом стала ссылкой")
        html = render_to_html(tokens)
        self.assertEqual(html, f"<b>{LINE_SPACED}</b>")
        self.assertNotIn("<a ", html)

    # --- 5-6. В/Г: частичное форматирование -------------------------------

    def test_c_bold_label_only_keeps_link(self) -> None:
        """В: bold только на `[место]` — стиль расширяется, ссылка цела."""
        for offset, length in ((0, u16("[место]")), (1, u16("место"))):
            with self.subTest(offset=offset, length=length):
                markup = self.convert(LINE, [ent("bold", offset, length)])
                self.assertNotIn("\\(", markup)
                self.assertEqual(markup, f"**{LINE}**")
                self.assertEqual(
                    render_to_html(parse_template(markup)),
                    '<b><a href="[ссылка_места]">[место]</a></b>',
                )

    def test_d_bold_address_only_is_dropped_and_link_survives(self) -> None:
        """Г: bold только на адресе — адрес невидим, стиль отбрасывается."""
        url_start = u16(LINE[: LINE.index("[ссылка_места]")])
        for offset, length in (
            (url_start, u16("[ссылка_места]")),
            (url_start + 1, u16("ссылка_места")),
        ):
            with self.subTest(offset=offset, length=length):
                markup = self.convert(LINE, [ent("bold", offset, length)])
                self.assertNotIn("\\(", markup)
                self.assertEqual(markup, LINE, "стиль адреса попал в конструкцию")
                tokens = parse_template(markup)
                self.assertEqual(len(_links(tokens)), 1)
                self.assertEqual(
                    render_to_html(tokens),
                    '<a href="[ссылка_места]">[место]</a>',
                )

    # --- 7. Настоящие text_link -------------------------------------------

    def test_text_link_word_with_query_and_fragment(self) -> None:
        """text_link: подпись в скобках, URL с `&` уходит в href как `&amp;`."""
        url = "https://e.example/x?a=1&b=2#h"
        markup = self.convert("сайт", [ent("text_link", 0, u16("сайт"), url=url)])
        self.assertEqual(markup, f"сайт({url})")
        self.assertEqual(
            render_to_html(parse_template(markup)),
            '<a href="https://e.example/x?a=1&amp;b=2#h">сайт</a>',
        )

    def test_text_link_unicode_label_and_url(self) -> None:
        label = "Дом 🎭"
        url = "https://e.example/театр"
        markup = self.convert(label, [ent("text_link", 0, u16(label), url=url)])
        self.assertEqual(markup, f"{label}({url})")
        self.assertEqual(
            render_to_html(parse_template(markup)),
            f'<a href="{url}">{label}</a>',
        )

    def test_text_link_dangerous_scheme_keeps_text_without_link(self) -> None:
        """`javascript:` в text_link: текст остаётся, ссылки нет, без падения."""
        markup = self.convert(
            "клик", [ent("text_link", 0, u16("клик"), url="javascript:alert(1)")]
        )
        self.assertEqual(markup, "клик")
        html = render_to_html(parse_template(markup))
        self.assertEqual(html, "клик")
        self.assertNotIn("<a ", html)

    # --- 8. code/pre — буквальный спан ------------------------------------

    def test_code_entity_makes_construction_literal(self) -> None:
        markup = self.convert(LINE, [ent("code", 0, u16(LINE))])
        self.assertEqual(markup, f"`{LINE}`")
        tokens = parse_template(markup)
        self.assertEqual(_links(tokens), [], "code-спан стал ссылкой")
        self.assertEqual(render_to_html(tokens), f"<code>{LINE}</code>")

    def test_pre_entity_makes_construction_literal(self) -> None:
        markup = self.convert(LINE, [ent("pre", 0, u16(LINE))])
        self.assertEqual(markup, f"```{LINE}```")
        tokens = parse_template(markup)
        self.assertEqual(_links(tokens), [], "pre-спан стал ссылкой")
        self.assertEqual(render_to_html(tokens), f"<pre>{LINE}</pre>")

    # --- 9-10. Литеральный URL и опасная схема ----------------------------

    def test_literal_url_with_params_is_escaped_only_in_href(self) -> None:
        text = "[место](https://e.example/x?a=1&b=2)"
        markup = self.convert(text, [ent("bold", 0, u16(text))])
        self.assertEqual(markup, f"**{text}**")
        html = render_to_html(parse_template(markup))
        self.assertEqual(
            html, '<b><a href="https://e.example/x?a=1&amp;b=2">[место]</a></b>'
        )
        visible_part = html.split('">', 1)[1]
        self.assertNotIn("https://", visible_part, "URL попал в видимый текст")
        self.assertEqual(visible_part, "[место]</a></b>")

    def test_dangerous_scheme_after_brick_is_text_not_link(self) -> None:
        text = "[место](javascript:alert(1))"
        markup = self.convert(text, [ent("bold", 0, u16(text))])
        html = render_to_html(parse_template(markup))
        self.assertNotIn("<a ", html, "опасная схема стала ссылкой")
        self.assertEqual(html, "<b>[место](javascript:alert(1))</b>", "текст потерян")

    # --- 11-12. UTF-16 и битые entities -----------------------------------

    def test_emoji_before_construction_does_not_shift_span(self) -> None:
        text = "😀 " + LINE
        offset = u16("😀 ")
        markup = self.convert(text, [ent("bold", offset, u16(LINE))])
        self.assertEqual(markup, f"😀 **{LINE}**")
        self.assertEqual(
            render_to_html(parse_template(markup)),
            '😀 <b><a href="[ссылка_места]">[место]</a></b>',
        )

    def test_malformed_entities_do_not_lose_text(self) -> None:
        cases = {
            "за концом текста": [ent("bold", 0, u16(LINE) + 70)],
            "нулевая длина": [ent("bold", 0, 0)],
            "отрицательное смещение": [ent("bold", -1, 3)],
        }
        for label, entities in cases.items():
            with self.subTest(case=label):
                markup = self.convert(LINE, entities)
                self.assertEqual(markup, LINE, "текст изменён или потерян")
                self.assertEqual(len(_links(parse_template(markup))), 1)

    def test_second_serialization_is_idempotent(self) -> None:
        """Повторная сериализация уже сохранённой записи ничего не меняет."""
        once = self.convert(LINE, [ent("bold", 0, u16(LINE))])
        self.assertEqual(self.convert(once, None), once)
        self.assertEqual(self.convert(once, []), once)


# --- Интеграция: настоящее приложение и временная SQLite ---------------------

try:
    from sqlalchemy import select

    from announcement_store import (
        build_announcement_render_payload,
        choose_announcement_template,
        confirm_announcement_place,
        save_announcement_value,
        start_announcement_wizard,
    )
    from announcement_wizard import visible_announcement_text
    from brick_store import get_system_brick
    from models import TemplateVersion, TemplateVersionBrick
    from template_compiler import render_template
    from template_store import render_template_version
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


def _draft_block(screen: str) -> str:
    """Текст между первой парой разделителей — то, что видит автор."""
    parts = screen.split("────────────")
    return parts[1].strip("\n") if len(parts) > 1 else ""


def _version_content(h, template_id: int, revision: int) -> str:
    with h.session_factory() as session:
        row = session.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.version == revision,
            )
        )
        if row is None:
            raise AssertionError(f"Версия {revision} шаблона {template_id} не найдена")
        return row.content


def _version_extended(h, template_id: int, revision: int) -> str | None:
    with h.session_factory() as session:
        row = session.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.version == revision,
            )
        )
        if row is None:
            raise AssertionError(f"Версия {revision} шаблона {template_id} не найдена")
        return row.extended_content


def _version_bricks(h, template_id: int, revision: int) -> dict[str, int]:
    with h.session_factory() as session:
        row = session.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.version == revision,
            )
        )
        if row is None:
            raise AssertionError(f"Версия {revision} шаблона {template_id} не найдена")
        rows = session.scalars(
            select(TemplateVersionBrick).where(
                TemplateVersionBrick.template_version_id == row.id
            )
        ).all()
        return {binding.code: binding.brick_id for binding in rows}


def _version_count(h, template_id: int) -> int:
    with h.session_factory() as session:
        rows = session.scalars(
            select(TemplateVersion).where(TemplateVersion.template_id == template_id)
        ).all()
        return len(rows)


def _render_version(
    h, template_id: int, values_by_id: dict[int, str], *, version: int | None = None
) -> str:
    with h.session_factory() as session:
        return render_template_version(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            template_id=template_id,
            values_by_id=values_by_id,
            version=version,
        )


async def _open_standard_editor(h, place_id: int) -> int:
    """Маршрут автора: карточка места → «Стандартный шаблон» → правка."""
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


async def _open_personal_editor(h, template_id: int, *, extended: bool = False) -> None:
    await h.text("/templates")
    await h.callback(f"tp_open:{template_id}")
    prefix = "tp_edit_ext" if extended else "tp_edit"
    await h.callback(f"{prefix}:{template_id}")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StandardTemplateLifecycleTests(unittest.TestCase):
    """Стандартный шаблон места: весь путь §5.2 на настоящем приложении."""

    @scenario_test
    async def test_bold_link_full_lifecycle(self, h) -> None:
        await h.text("/start")
        place_id, _alias = _seed_place(h, url="https://example.com/place?a=1&b=2")
        template_id = await _open_standard_editor(h, place_id)

        await h.text(LINE, entities=[ent("bold", 0, u16(LINE))])

        # 1. Экран подтверждения: канонический текст без обратных слешей.
        confirm = h.screen_text()
        self.assertIn(f"**{LINE}**", confirm)
        self.assertNotIn("\\(", confirm, "в подтверждении появились слеши")
        self.assertNotIn("\\)", confirm, "в подтверждении появились слеши")
        await h.callback("tp_save")

        # 2. Сохранённая версия и привязки.
        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, f"**{LINE}**")
        self.assertNotIn("\\", stored, "в сохранённом тексте появились слеши")
        bindings = _version_bricks(h, template_id, 2)
        with h.session_factory() as session:
            place_brick = get_system_brick(session, "место")
            url_brick = get_system_brick(session, "ссылка_места")
        assert place_brick is not None and url_brick is not None
        self.assertEqual(bindings.get("место"), place_brick.id)
        self.assertEqual(bindings.get("ссылка_места"), url_brick.id)

        # 3. Повторное открытие редактора: черновик без слешей.
        await h.callback(f"tp_edit:{template_id}")
        draft = _draft_block(h.screen_text())
        self.assertEqual(draft, f"**{LINE}**")

        # 4. Рендер сохранённой версии: href = значение ссылки, текст = значение
        #    места, URL в видимом тексте нет, скобок нет.
        rendered = _render_version(
            h,
            template_id,
            {
                place_brick.id: "Дом культуры",
                url_brick.id: "https://example.com/place?a=1&b=2",
            },
        )
        self.assertEqual(
            rendered,
            '<b><a href="https://example.com/place?a=1&amp;b=2">Дом культуры</a></b>',
        )
        visible = visible_announcement_text(rendered)
        self.assertEqual(visible, "Дом культуры")
        self.assertNotIn("http", visible)
        self.assertNotIn("(", visible)
        self.assertNotIn("[", visible)

    @scenario_test
    async def test_spaced_construction_renders_as_prose(self, h) -> None:
        """Б: пробел перед `(` — значения рядом, гиперссылки в анонсе нет."""
        await h.text("/start")
        place_id, _alias = _seed_place(h, url="https://example.com/place?a=1&b=2")
        template_id = await _open_standard_editor(h, place_id)

        await h.text(LINE_SPACED, entities=[ent("bold", 0, u16(LINE_SPACED))])
        await h.callback("tp_save")

        self.assertEqual(_version_content(h, template_id, 2), f"**{LINE_SPACED}**")
        bindings = _version_bricks(h, template_id, 2)
        rendered = _render_version(
            h,
            template_id,
            {
                bindings["место"]: "Дом культуры",
                bindings["ссылка_места"]: "https://example.com/place?a=1&b=2",
            },
        )
        self.assertNotIn("<a ", rendered, "проза стала гиперссылкой")
        self.assertIn("Дом культуры", rendered)
        self.assertIn("(https://example.com/place?a=1&amp;b=2)", rendered)
        visible = visible_announcement_text(rendered)
        self.assertEqual(visible, "Дом культуры (https://example.com/place?a=1&b=2)")

    @scenario_test
    async def test_bold_only_address_keeps_plain_link(self, h) -> None:
        """Г: bold только на адресе — ссылка сохраняется, жирности нет."""
        await h.text("/start")
        place_id, _alias = _seed_place(h, url="https://example.com/place")
        template_id = await _open_standard_editor(h, place_id)

        start = u16(LINE[: LINE.index("[ссылка_места]")])
        await h.text(LINE, entities=[ent("bold", start, u16("[ссылка_места]"))])
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, LINE, "стиль адреса попал в конструкцию")
        self.assertNotIn("\\", stored)
        bindings = _version_bricks(h, template_id, 2)
        rendered = _render_version(
            h,
            template_id,
            {
                bindings["место"]: "Дом культуры",
                bindings["ссылка_места"]: "https://example.com/place",
            },
        )
        self.assertEqual(
            rendered, '<a href="https://example.com/place">Дом культуры</a>'
        )

    @scenario_test
    async def test_code_span_in_template_is_literal(self, h) -> None:
        """code вокруг конструкции: сохранено буквально, ссылки в анонсе нет."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id = await _open_standard_editor(h, place_id)

        await h.text(LINE, entities=[ent("code", 0, u16(LINE))])
        self.assertIn(f"`{LINE}`", h.screen_text())
        await h.callback("tp_save")

        self.assertEqual(_version_content(h, template_id, 2), f"`{LINE}`")
        # В буквальном span кирпичиков нет — привязок у версии тоже нет.
        self.assertEqual(_version_bricks(h, template_id, 2), {})
        rendered = _render_version(h, template_id, {})
        self.assertEqual(rendered, f"<code>{LINE}</code>")
        self.assertNotIn("<a ", rendered)

    @scenario_test
    async def test_dangerous_scheme_in_template_is_safe_text(self, h) -> None:
        """`javascript:` в скобках: сохранение не падает, текст цел, ссылки нет."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id = await _open_standard_editor(h, place_id)

        text = "[место](javascript:alert(1))"
        await h.text(text, entities=[ent("bold", 0, u16(text))])
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertIn("[место]", stored)
        self.assertIn("javascript", stored, "текст адреса потерян")
        bindings = _version_bricks(h, template_id, 2)
        rendered = _render_version(h, template_id, {bindings["место"]: "Дом культуры"})
        self.assertNotIn("<a ", rendered, "опасная схема стала ссылкой")
        self.assertEqual(rendered, "<b>Дом культуры(javascript:alert(1))</b>")

    @scenario_test
    async def test_resending_unchanged_draft_is_idempotent(self, h) -> None:
        """Повторная отправка показанного черновика не создаёт новую версию."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id = await _open_standard_editor(h, place_id)

        await h.text(LINE, entities=[ent("bold", 0, u16(LINE))])
        await h.callback("tp_save")
        self.assertEqual(_version_count(h, template_id), 2)

        await h.callback(f"tp_edit:{template_id}")
        draft = _draft_block(h.screen_text())
        await h.text(draft)
        cat.assert_shows(
            self, h.screen_text(), "templates.build.predprosmotr_podtverzhdenie"
        )
        await h.callback("tp_save")

        self.assertIn("Текст остался прежним", h.screen_text())
        self.assertEqual(_version_count(h, template_id), 2, "создана лишняя версия")
        self.assertEqual(_version_content(h, template_id, 2), f"**{LINE}**")

    @scenario_test
    async def test_stale_save_callback_does_not_create_version(self, h) -> None:
        """Устаревшая кнопка `tp_save` после сохранения ничего не создаёт."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id = await _open_standard_editor(h, place_id)

        await h.text(LINE, entities=[ent("bold", 0, u16(LINE))])
        await h.callback("tp_save")
        self.assertEqual(_version_count(h, template_id), 2)

        await h.callback("tp_save")  # повторное нажатие уже отработавшей кнопки

        self.assertEqual(_version_count(h, template_id), 2, "создана лишняя версия")
        self.assertEqual(_version_content(h, template_id, 2), f"**{LINE}**")

    @scenario_test
    async def test_editing_existing_template_updates_it(self, h) -> None:
        """Правка уже существующего шаблона: новая версия по тому же пути."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id = await _open_standard_editor(h, place_id)

        await h.text(LINE, entities=[ent("bold", 0, u16(LINE))])
        await h.callback("tp_save")
        self.assertEqual(_version_content(h, template_id, 2), f"**{LINE}**")

        await h.callback(f"tp_edit:{template_id}")
        await h.text(LINE_SPACED, entities=[ent("bold", 0, u16(LINE_SPACED))])
        self.assertIn(f"**{LINE_SPACED}**", h.screen_text())
        await h.callback("tp_save")

        self.assertEqual(_version_count(h, template_id), 3)
        self.assertEqual(_version_content(h, template_id, 3), f"**{LINE_SPACED}**")

        bindings = _version_bricks(h, template_id, 3)
        rendered = _render_version(
            h,
            template_id,
            {
                bindings["место"]: "Дом культуры",
                bindings["ссылка_места"]: "https://example.com/place",
            },
        )
        self.assertNotIn("<a ", rendered, "проза со пробелом стала ссылкой")
        self.assertEqual(rendered, "<b>Дом культуры (https://example.com/place)</b>")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PersonalTemplateLifecycleTests(unittest.TestCase):
    """Личный шаблон: свои кирпичики, расширенная секция, рендер."""

    @scenario_test
    async def test_own_bricks_link_saved_and_rendered(self, h) -> None:
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="F own bricks",
            specs=[
                ("nazv", "Название", "short_text", {}),
                ("ssylka", "Ссылка", "url", {}),
            ],
            content="Старый [nazv]",
        )
        await _open_personal_editor(h, template_id)

        text = "[nazv]([ssylka])"
        await h.text(text, entities=[ent("bold", 0, u16(text))])
        self.assertIn(f"**{text}**", h.screen_text())
        self.assertNotIn("\\(", h.screen_text())
        await h.callback("tp_save")

        self.assertEqual(_version_content(h, template_id, 2), f"**{text}**")
        bindings = _version_bricks(h, template_id, 2)
        self.assertEqual(bindings.get("nazv"), bricks["nazv"])
        self.assertEqual(bindings.get("ssylka"), bricks["ssylka"])

        rendered = _render_version(
            h,
            template_id,
            {
                bricks["nazv"]: "Дом культуры",
                bricks["ssylka"]: "https://example.com/place?a=1&b=2",
            },
        )
        self.assertEqual(
            rendered,
            '<b><a href="https://example.com/place?a=1&amp;b=2">Дом культуры</a></b>',
        )
        self.assertEqual(visible_announcement_text(rendered), "Дом культуры")

    @scenario_test
    async def test_extended_section_link_saved_and_rendered(self, h) -> None:
        """Расширенная секция хранит ссылку и рендерится как гиперссылка."""
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="F extended",
            specs=[
                ("nazv", "Название", "short_text", {}),
                ("ssylka", "Ссылка", "url", {}),
            ],
            content="Начало [nazv]",
            extended_content="Расширено [nazv]",
        )
        place_id, _alias = _seed_place(h)
        await _open_personal_editor(h, template_id, extended=True)

        text = "[nazv]([ssylka])"
        await h.text(text, entities=[ent("bold", 0, u16(text))])
        self.assertIn(f"**{text}**", h.screen_text())
        await h.callback("tp_save")

        self.assertEqual(_version_extended(h, template_id, 2), f"**{text}**")
        self.assertNotIn("\\(", _version_extended(h, template_id, 2) or "")
        self.assertEqual(
            _version_content(h, template_id, 2), "Начало [nazv]", "канон затёрт"
        )

        # Публичный путь рендера анонса: расширенный вариант.
        with h.session_factory() as session:
            draft = start_announcement_wizard(
                session, actor_telegram_user_id=DEFAULT_USER_ID, place_id=place_id
            )
            confirmed = confirm_announcement_place(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.draft_id,
                expected_revision=draft.revision,
            )
            details = choose_announcement_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=confirmed.draft_id,
                template_id=template_id,
                expected_revision=confirmed.revision,
            )
            details = save_announcement_value(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=details.draft_id,
                brick_id=bricks["nazv"],
                value="Дом культуры",
                expected_revision=details.revision,
            )
            details = save_announcement_value(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=details.draft_id,
                brick_id=bricks["ssylka"],
                value="https://example.com/place?a=1&b=2",
                expected_revision=details.revision,
            )
            payload = build_announcement_render_payload(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=details.draft_id,
            )
            extended_html = render_template(
                payload.tokens_by_text,
                payload.values_by_id,
                payload.definitions_by_id,
                use_extended=True,
            )
        self.assertEqual(
            extended_html,
            '<b><a href="https://example.com/place?a=1&amp;b=2">Дом культуры</a></b>',
        )
        self.assertEqual(visible_announcement_text(extended_html), "Дом культуры")


if __name__ == "__main__":
    unittest.main()
