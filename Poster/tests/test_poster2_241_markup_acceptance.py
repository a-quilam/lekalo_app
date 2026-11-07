# tests/test_poster2_241_markup_acceptance.py
r"""
Poster 2.4.1 — независимая приёмка T1 (poster-tester): реальные MessageEntity
через обработчики, цепочка input → save → read → edit → render.

Файл написан тестировщиком независимо от тестов разработчика и проверяет
ПОЛЬЗОВАТЕЛЬСКИЕ требования релиза 2.4.1: форматирование, набранное в
клиенте Telegram, доходит до версии шаблона и до HTML анонса.

Группы сценариев (все интеграционные идут через настоящий
`Application.process_update`, FakeBot и временную SQLite; рабочая
`post_bot.db`, `.env` и сеть Telegram не используются):

1. Жирный заголовок личного шаблона: подтверждение показывает канонический
   текст, версия 2 хранит `**…**`, рендер отдаёт `<b>…</b>`.
2. Курсив внутри предложения.
3. Скрытая ссылка: URL приходит из entity, а не из видимого текста.
4. Порядок entities на одном диапазоне не важен (два отдельных шаблона:
   второй порядок в том же шаблоне не создаёт версию).
5. Эмодзи и смещения UTF-16.
6. Несколько ссылок в разных частях текста.
7. Литеральные `* _ ~ < &` экранируются ровно один раз.
8. История версий 1→2→3 и карточка с маркерами.
9. Привязка кирпичика рядом с форматированным текстом.
10. `content` и `extended_content` в одном шаблоне.
11. Живой блок конструктора анонса.
12. Валидность сохранённого HTML (парность тегов, идемпотентность
    `sanitize_html`) — юнит- и интегральные проверки.
13. Стандартный шаблон места через кабинет площадки.

Негативные проверки: повторное нажатие «Сохранить», чужой пользователь,
текст из одних пробелов, `javascript:` в ссылке, URL с пробелом (деградация
в подпись без ссылки, сохранение не падает).
"""

from __future__ import annotations

import re
import unittest

from telegram import MessageEntity

from template_parser import parse_template, render_to_html
from tests import ui_texts_expect as cat
from utils.tg_html import entities_to_template_markup, sanitize_html


def ent(kind: str, offset: int, length: int, **kwargs) -> MessageEntity:
    """MessageEntity с явным типом (минимальный конструктор для тестов)."""
    return MessageEntity(type=kind, offset=offset, length=length, **kwargs)


def span(text: str, label: str, start: int = 0) -> tuple[int, int]:
    """
    Смещение и длина подстроки в код-юнитах UTF-16.

    Telegram считает смещения в UTF-16: эмодзи занимает две единицы, и
    индекс Python дал бы сдвиг форматирования.
    """
    index = text.index(label, start)
    offset = len(text[:index].encode("utf-16-le")) // 2
    return offset, len(label.encode("utf-16-le")) // 2


_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)(?:\s+[^<>]*)?>")


def assert_balanced(test: unittest.TestCase, html: str) -> None:
    """Парность и порядок HTML-тегов; сырых `<` вне разметки нет."""
    stack: list[str] = []
    matches = list(_TAG_RE.finditer(html))
    for match in matches:
        name = match.group(1).lower()
        if match.group(0).startswith("</"):
            test.assertTrue(stack, f"закрывающий </{name}> без открывающего: {html!r}")
            test.assertEqual(stack.pop(), name, f"нарушен порядок тегов: {html!r}")
        else:
            stack.append(name)
    test.assertEqual(stack, [], f"незакрытые теги: {html!r}")
    test.assertEqual(
        html.count("<"),
        len(matches),
        f"сырой '<' вне разметки: {html!r}",
    )


def assert_safe_html(test: unittest.TestCase, html: str) -> None:
    """HTML рендера безопасен: парность тегов и идемпотентная очистка."""
    assert_balanced(test, html)
    cleaned = str(sanitize_html(html))
    test.assertEqual(cleaned, html, f"sanitize_html изменил корректный HTML: {html!r}")
    test.assertNotIn("&amp;amp;", html, "двойное экранирование амперсанда")


class HtmlValidityUnitTests(unittest.TestCase):
    """Требование 12 без приложения: конвертер → парсер → рендер → sanitize."""

    CASES: list[tuple[str, list[MessageEntity]]] = [
        ("Летний концерт [main]", [ent("bold", 0, 14)]),
        (
            "Событие 🎉 Билеты [main]",
            [ent("bold", 0, 7), ent("italic", 10, 6)],
        ),
        (
            "Зал Афиша и Сцена [main]",
            [
                ent("text_link", 4, 5, url="https://a.example"),
                ent("text_link", 13, 5, url="https://b.example"),
            ],
        ),
        (
            "Дом культуры [main]",
            [
                ent("bold", 0, 12),
                ent("text_link", 0, 12, url="https://e.example/hall"),
            ],
        ),
        ("a < b & c *x* _y_ ~z~ [main]", [ent("bold", 0, 1)]),
        ("Наше-место [main]", [ent("text_link", 0, 10, url="https://e.example/x")]),
    ]

    def test_rendered_markup_is_valid_html(self) -> None:
        for text, entities in self.CASES:
            with self.subTest(text=text):
                markup = entities_to_template_markup(text, entities)
                html = render_to_html(parse_template(markup))
                assert_safe_html(self, html)

    def test_literal_escaping_is_exact(self) -> None:
        """Требование 7: ровно одно экранирование, маркеры остаются текстом."""
        text = "a < b & c *x* _y_ ~z~ [main]"
        markup = entities_to_template_markup(text, [ent("bold", 0, 1)])
        self.assertEqual(markup, "**a** < b & c \\*x\\* \\_y\\_ \\~z\\~ [main]")
        html = render_to_html(parse_template(markup))
        self.assertEqual(html, "<b>a</b> &lt; b &amp; c *x* _y_ ~z~ [main]")
        assert_safe_html(self, html)

    def test_sanitize_is_idempotent_on_rendered_and_broken_html(self) -> None:
        for html in (
            "<b>жирный</b> <i>курсив</i>",
            '<b><a href="https://e.example">Дом</a></b> [main]',
            "<b>a</b> &lt; b &amp; c",
            "просто текст < и & без тегов",
            "<b>не закрыт <i>и ещё</i>",
        ):
            with self.subTest(html=html):
                once = str(sanitize_html(html))
                self.assertEqual(str(sanitize_html(once)), once)


# --- Интеграция: настоящее приложение ---------------------------------------
try:
    from sqlalchemy import select

    from models import TemplateVersion, TemplateVersionBrick
    from template_store import render_template_version
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        is_live_record,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _choose_personal,
        _no_photos,
        _open_control,
        _open_edit,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


async def _open_personal_editor(h, template_id: int) -> None:
    """`/templates` → карточка → правка канонического текста."""
    await h.text("/templates")
    await h.callback(f"tp_open:{template_id}")
    await h.callback(f"tp_edit:{template_id}")


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


def _all_versions(h, template_id: int) -> list[tuple[int, str, str | None]]:
    with h.session_factory() as session:
        rows = session.scalars(
            select(TemplateVersion)
            .where(TemplateVersion.template_id == template_id)
            .order_by(TemplateVersion.version)
        ).all()
        return [(row.version, row.content, row.extended_content) for row in rows]


def _version_bricks(h, template_id: int, revision: int) -> dict[str, int]:
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
    with h.session_factory() as session:
        return render_template_version(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            template_id=template_id,
            values_by_id=values_by_id,
        )


def _live_records(h) -> list[dict]:
    """Живой блок: HTML-предпросмотр анонса без клавиатуры."""
    return [
        call
        for call in h.calls
        if call.get("reply_markup") is None and is_live_record(call)
    ]


def _live_text(h) -> str:
    records = _live_records(h)
    if not records:
        raise AssertionError("живой блок не отправлен")
    return records[-1].get("text") or ""


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class BoldHeaderFlowTests(unittest.TestCase):
    """Требование 1: жирный заголовок личного шаблона."""

    @scenario_test
    async def test_bold_header_survives_save_and_render(self, h) -> None:
        template_id, _revision, bricks = _seed_personal_template(
            h, content="Старый текст [main]", specs=[("main", "Поле", "short_text", {})]
        )
        await _open_personal_editor(h, template_id)
        self.assertIn("Старый текст [main]", h.screen_text())

        text = "Летний концерт [main]"
        await h.text(text, entities=[ent("bold", *span(text, "Летний концерт"))])

        # Подтверждение показывает канонический текст грамматики, а не HTML.
        confirm = h.screen_text()
        self.assertIn("**Летний концерт** [main]", confirm)
        self.assertNotIn("<b>", confirm)
        self.assertEqual(
            [data for _label, data in h.screen_buttons() if data == "tp_save"],
            ["tp_save"],
        )

        await h.callback("tp_save")
        self.assertIn("Сохранено в версии 2.", h.screen_text())

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, "**Летний концерт** [main]")

        html = render_to_html(parse_template(stored))
        self.assertEqual(html, "<b>Летний концерт</b> [main]")
        assert_safe_html(self, html)

        # Кирпичик не потерян: привязка существует, анонс подставляет значение.
        self.assertEqual(_version_bricks(h, template_id, 2).get("main"), bricks["main"])
        rendered = _render_version(h, template_id, {bricks["main"]: "Летний концерт"})
        self.assertEqual(rendered, "<b>Летний концерт</b> Летний концерт")
        assert_safe_html(self, rendered)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ItalicInsideSentenceTests(unittest.TestCase):
    """Требование 2: курсив на слове внутри предложения."""

    @scenario_test
    async def test_italic_word_in_sentence(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)
        text = "Сегодня очень весело [main]"
        await h.text(text, entities=[ent("italic", *span(text, "очень"))])

        self.assertIn("Сегодня *очень* весело [main]", h.screen_text())
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, "Сегодня *очень* весело [main]")
        html = render_to_html(parse_template(stored))
        self.assertEqual(html, "Сегодня <i>очень</i> весело [main]")
        assert_safe_html(self, html)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HiddenLinkTests(unittest.TestCase):
    """Требование 3: URL есть только в entity, не в видимом тексте."""

    @scenario_test
    async def test_hidden_text_link_keeps_url(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)
        text = "Наше-место [main]"
        url = "https://example.com/place?utm=1"
        await h.text(
            text,
            entities=[ent("text_link", *span(text, "Наше-место"), url=url)],
        )
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, "Наше-место(https://example.com/place?utm=1) [main]")
        html = render_to_html(parse_template(stored))
        self.assertEqual(
            html,
            '<a href="https://example.com/place?utm=1">Наше-место</a> [main]',
        )
        assert_safe_html(self, html)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MultipleLinksTests(unittest.TestCase):
    """Требование 6: две ссылки в разных частях текста."""

    @scenario_test
    async def test_two_links_in_one_section(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)
        text = "Зал Афиша и Сцена [main]"
        await h.text(
            text,
            entities=[
                ent("text_link", *span(text, "Афиша"), url="https://a.example"),
                ent("text_link", *span(text, "Сцена"), url="https://b.example"),
            ],
        )
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(
            stored,
            "Зал {Афиша}(https://a.example) и {Сцена}(https://b.example) [main]",
        )
        html = render_to_html(parse_template(stored))
        self.assertEqual(
            html,
            'Зал <a href="https://a.example">Афиша</a> и '
            '<a href="https://b.example">Сцена</a> [main]',
        )
        assert_safe_html(self, html)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EntityOrderTests(unittest.TestCase):
    """
    Требование 4: порядок entities на одном диапазоне не важен.

    Второй порядок в ТОМ ЖЕ шаблоне не создаёт новую версию («Текст остался
    прежним»), поэтому каждый порядок проверяется на своём шаблоне.
    """

    URL = "https://example.com/hall"

    def _entities(self, text: str, *, link_first: bool) -> list[MessageEntity]:
        offset, length = span(text, "Дом культуры")
        bold = ent("bold", offset, length)
        link = ent("text_link", offset, length, url=self.URL)
        return [link, bold] if link_first else [bold, link]

    @scenario_test
    async def test_link_then_bold(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)
        text = "Дом культуры [main]"
        await h.text(text, entities=self._entities(text, link_first=True))
        await h.callback("tp_save")
        self._assert_stored(h, template_id)

    @scenario_test
    async def test_bold_then_link(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)
        text = "Дом культуры [main]"
        await h.text(text, entities=self._entities(text, link_first=False))
        await h.callback("tp_save")
        self._assert_stored(h, template_id)

    def _assert_stored(self, h, template_id: int) -> None:
        stored = _version_content(h, template_id, 2)
        self.assertEqual(
            stored,
            "**{Дом культуры}(https://example.com/hall)** [main]",
        )
        html = render_to_html(parse_template(stored))
        self.assertEqual(
            html,
            '<b><a href="https://example.com/hall">Дом культуры</a></b> [main]',
        )
        assert_safe_html(self, html)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class Utf16EmojiOffsetTests(unittest.TestCase):
    """Требование 5: эмодзи (2 код-юнита UTF-16) не сдвигает форматирование."""

    @scenario_test
    async def test_emoji_keeps_entity_boundaries(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)
        text = "Событие 🎉 Билеты [main]"
        await h.text(
            text,
            entities=[
                ent("bold", *span(text, "Событие")),
                ent("italic", *span(text, "Билеты")),
            ],
        )
        self.assertIn("**Событие** 🎉 *Билеты* [main]", h.screen_text())
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, "**Событие** 🎉 *Билеты* [main]")
        html = render_to_html(parse_template(stored))
        self.assertEqual(html, "<b>Событие</b> 🎉 <i>Билеты</i> [main]")
        assert_safe_html(self, html)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LiteralEscapingTests(unittest.TestCase):
    """Требование 7: литералы `* _ ~ < &` не превращаются в оформление."""

    @scenario_test
    async def test_literals_escaped_once(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)
        text = "a < b & c *x* _y_ ~z~ [main]"
        await h.text(text, entities=[ent("bold", *span(text, "a"))])

        # Экран подтверждения показывает тот же текст, что попадёт в версию,
        # в HTML-экранированном виде (одинарное экранирование).
        self.assertIn(
            "**a** &lt; b &amp; c \\*x\\* \\_y\\_ \\~z\\~ [main]",
            h.screen_text(),
        )
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, "**a** < b & c \\*x\\* \\_y\\_ \\~z\\~ [main]")
        html = render_to_html(parse_template(stored))
        self.assertEqual(html, "<b>a</b> &lt; b &amp; c *x* _y_ ~z~ [main]")
        assert_safe_html(self, html)
        self.assertEqual(str(sanitize_html(html)), html)
        self.assertNotIn("&amp;amp;", html)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class VersionHistoryTests(unittest.TestCase):
    """Требование 8: версии 1→2→3, карточка и повторная правка с маркерами."""

    @scenario_test
    async def test_three_versions_and_card_markers(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)

        first = "Первый вариант [main]"
        await h.text(first, entities=[ent("bold", *span(first, "Первый"))])
        await h.callback("tp_save")
        self.assertEqual(
            _version_content(h, template_id, 2), "**Первый** вариант [main]"
        )

        # Вторая правка открывает сохранённый текст с маркерами.
        await h.callback(f"tp_edit:{template_id}")
        self.assertIn("**Первый** вариант [main]", h.screen_text())
        second = "Второй вариант [main]"
        await h.text(second, entities=[ent("italic", *span(second, "Второй"))])
        await h.callback("tp_save")

        versions = _all_versions(h, template_id)
        self.assertEqual([version for version, _c, _e in versions], [1, 2, 3])
        self.assertNotEqual(versions[0][1], versions[1][1])
        self.assertNotEqual(versions[1][1], versions[2][1])
        self.assertEqual(versions[2][1], "*Второй* вариант [main]")

        # Карточка показывает сохранённые маркеры как текст, а не как HTML.
        await h.callback("tp_hub")
        await h.callback(f"tp_open:{template_id}")
        card = h.screen_text()
        self.assertIn("*Второй* вариант [main]", card)
        self.assertNotIn("<i>Второй</i>", card)
        self.assertNotIn("&lt;i&gt;", card)
        self.assertIn("Версия: 3", card)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class BrickBindingTests(unittest.TestCase):
    """Требование 9: кирпичик рядом с форматированием остаётся кирпичиком."""

    @scenario_test
    async def test_brick_next_to_bold_is_bound_and_substituted(self, h) -> None:
        template_id, _revision, bricks = _seed_personal_template(
            h,
            specs=[("main", "Основное поле", "short_text", {})],
            content="Старый [main]",
        )
        await _open_personal_editor(h, template_id)
        text = "Итог: [main]"
        await h.text(text, entities=[ent("bold", *span(text, "Итог"))])
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, "**Итог**: [main]")
        self.assertNotIn("[[main]]", stored)
        self.assertEqual(_version_bricks(h, template_id, 2).get("main"), bricks["main"])

        rendered = _render_version(h, template_id, {bricks["main"]: "Летний концерт"})
        self.assertEqual(rendered, "<b>Итог</b>: Летний концерт")
        assert_safe_html(self, rendered)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ContentAndExtendedTests(unittest.TestCase):
    """Требование 10: обе секции шаблона хранят форматирование."""

    @scenario_test
    async def test_both_sections_keep_markup(self, h) -> None:
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Обе секции",
            specs=[
                ("main", "Основное", "short_text", {}),
                ("extra", "Доп", "short_text", {}),
            ],
            content="[main]",
            extended_content="[main]\n[extra]",
        )
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")

        await h.callback(f"tp_edit:{template_id}")
        content_text = "Основной текст [main]"
        await h.text(
            content_text,
            entities=[ent("bold", *span(content_text, "Основной"))],
        )
        await h.callback("tp_save")
        self.assertEqual(
            _version_content(h, template_id, 2), "**Основной** текст [main]"
        )

        await h.callback(f"tp_edit_ext:{template_id}")
        extended_text = "Расширенно: [main] [extra]"
        await h.text(
            extended_text,
            entities=[ent("italic", *span(extended_text, "Расширенно"))],
        )
        await h.callback("tp_save")

        self.assertEqual(
            _version_content(h, template_id, 3), "**Основной** текст [main]"
        )
        self.assertEqual(
            _version_extended(h, template_id, 3),
            "*Расширенно*: [main] [extra]",
        )
        # Карточка показывает обе секции с маркерами.
        card = h.screen_text()
        self.assertIn("**Основной** текст [main]", card)
        self.assertIn("*Расширенно*: [main] [extra]", card)

        # Анонс: без доп. поля — content, с доп. полем — extended.
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Главное значение")
        await _no_photos(h)
        self.assertEqual(h.posts()[-1], "<b>Основной</b> текст Главное значение")

        # 2.5.3 §5: «📝 Текстовые поля» заменяют клавиатуру анонса, поэтому
        # доп. поля открываются после возврата управления «◀️ Назад».
        await _open_edit(h)
        await _open_control(h, "extras")
        await h.callback(_callback(h, "aw_exedit", f":{bricks['extra']}"))
        await h.text("Доп значение")
        self.assertEqual(
            h.posts()[-1],
            "<i>Расширенно</i>: Главное значение Доп значение",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LiveBlockFormattingTests(unittest.TestCase):
    """Требование 11: ответ с entities сразу виден в живом блоке."""

    @scenario_test
    async def test_live_block_renders_bold_and_link(self, h) -> None:
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Форматирование",
            specs=[
                ("title", "Название", "short_text", {}),
                ("desc", "Описание", "long_text", {}),
            ],
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        text = "Летний концерт и сайт"
        await h.text(
            text,
            entities=[
                ent("bold", *span(text, "Летний концерт")),
                ent(
                    "text_link", *span(text, "сайт"), url="https://example.com/tickets"
                ),
            ],
        )

        live = _live_text(h)
        self.assertIn("<b>Летний концерт</b>", live)
        self.assertIn('<a href="https://example.com/tickets">сайт</a>', live)
        self.assertNotIn("&lt;b&gt;", live)
        assert_safe_html(self, live)
        # Следующий вопрос — экран, а не новый пост.
        self.assertRegex(h.screen_text(), cat.question_re(2, 2))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StoredHtmlValidityIntegrationTests(unittest.TestCase):
    """Требование 12: HTML каждой сохранённой версии валиден и безопасен."""

    @scenario_test
    async def test_every_stored_version_renders_valid_html(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)

        saves = [
            ("Летний концерт [main]", [ent("bold", 0, 14)]),
            (
                "Событие 🎉 Билеты [main]",
                [ent("bold", 0, 7), ent("italic", 10, 6)],
            ),
            (
                "Наше-место [main]",
                [ent("text_link", 0, 10, url="https://example.com/place?utm=1")],
            ),
            ("a < b & c *x* _y_ ~z~ [main]", [ent("bold", 0, 1)]),
        ]
        for index, (text, entities) in enumerate(saves):
            if index:
                await h.callback(f"tp_edit:{template_id}")
            await h.text(text, entities=entities)
            await h.callback("tp_save")

        versions = _all_versions(h, template_id)
        self.assertEqual([version for version, _c, _e in versions], [1, 2, 3, 4, 5])
        self.assertEqual(len({content for _v, content, _e in versions}), 5)

        for revision, content, extended in versions:
            with self.subTest(revision=revision):
                html = render_to_html(parse_template(content))
                assert_safe_html(self, html)
                if extended is not None:
                    assert_safe_html(self, render_to_html(parse_template(extended)))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StandardPlaceTemplateTests(unittest.TestCase):
    """Требование 13: стандартный шаблон места из кабинета площадки."""

    @scenario_test
    async def test_standard_template_bold_header_and_place_values(self, h) -> None:
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        await h.callback("manage")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")
        # Poster 2.4.1 T2: тап по площадке сразу открывает полную карточку;
        # редактор — её кнопка `adm_edit_place`.
        await h.callback(f"adm_edit_place:{place_id}")
        await h.callback("adm_cancel")

        # Возврат из редактора ведёт в карточку места с кнопкой стандарта.
        callbacks = [data for _label, data in h.screen_buttons()]
        self.assertIn(f"tp_std:{place_id}", callbacks)
        await h.callback(f"tp_std:{place_id}")
        self.assertIn("Стандартный шаблон места", h.screen_text())

        template_id = int(
            next(
                data.split(":", 1)[1]
                for _label, data in h.screen_buttons()
                if data and data.startswith("tp_edit:")
            )
        )
        await h.callback(f"tp_edit:{template_id}")
        text = "Гала-концерт [название]\n[адрес]"
        await h.text(text, entities=[ent("bold", *span(text, "Гала-концерт"))])
        self.assertIn("**Гала-концерт** [название]", h.screen_text())
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, "**Гала-концерт** [название]\n[адрес]")
        bindings = _version_bricks(h, template_id, 2)
        self.assertIn("название", bindings)
        self.assertIn("адрес", bindings)

        rendered = _render_version(
            h,
            template_id,
            {
                bindings["название"]: "Дом культуры",
                bindings["адрес"]: "ул. Тестовая, 1",
            },
        )
        self.assertEqual(rendered, "<b>Гала-концерт</b> Дом культуры\nул. Тестовая, 1")
        assert_safe_html(self, rendered)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NegativeAndStaleFlowsTests(unittest.TestCase):
    """Негативные сценарии: повторы, чужие права, пустой текст, плохие URL."""

    @scenario_test
    async def test_identical_text_does_not_create_version(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)
        text = "Летний концерт [main]"
        await h.text(text, entities=[ent("bold", *span(text, "Летний концерт"))])
        await h.callback("tp_save")

        # Та же самая авторская запись второй раз версию не создаёт.
        await h.callback(f"tp_edit:{template_id}")
        await h.text(text, entities=[ent("bold", *span(text, "Летний концерт"))])
        await h.callback("tp_save")
        self.assertIn(
            "Текст остался прежним. Новая версия не создана.", h.screen_text()
        )
        self.assertEqual(
            [version for version, _c, _e in _all_versions(h, template_id)], [1, 2]
        )

    @scenario_test
    async def test_repeated_save_is_stale_and_adds_no_version(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)
        text = "Итог [main]"
        await h.text(text, entities=[ent("bold", *span(text, "Итог"))])
        await h.callback("tp_save")
        await h.callback("tp_save")

        self.assertIn(
            cat.text("errors.stale.templates"),
            [answer.get("text") for answer in h.answers()],
        )
        self.assertEqual(
            [version for version, _c, _e in _all_versions(h, template_id)], [1, 2]
        )

    @scenario_test
    async def test_foreign_user_cannot_open_or_save_template(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await h.callback(f"tp_open:{template_id}", user_id=55)
        self.assertIn(
            cat.text("errors.stale.templates"),
            [answer.get("text") for answer in h.answers()],
        )
        self.assertEqual(
            [version for version, _c, _e in _all_versions(h, template_id)], [1]
        )

    @scenario_test
    async def test_whitespace_only_text_is_rejected(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)
        await h.text("   ")

        screen = h.screen_text()
        self.assertIn("Текст не сохранён", screen)
        self.assertIn("Шаблон не должен состоять только из пустых строк.", screen)
        self.assertEqual(
            [version for version, _c, _e in _all_versions(h, template_id)], [1]
        )

    @scenario_test
    async def test_dangerous_and_unexpressible_urls_degrade_without_failure(
        self, h
    ) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Старый [main]"
        )
        await _open_personal_editor(h, template_id)

        # javascript: в entity — подпись без ссылки, сохранение проходит.
        dangerous = "Купить [main]"
        await h.text(
            dangerous,
            entities=[
                ent(
                    "text_link",
                    *span(dangerous, "Купить"),
                    url="javascript:alert(1)",
                )
            ],
        )
        confirm = h.screen_text()
        self.assertIn("Купить [main]", confirm)
        self.assertNotIn("javascript:", confirm)
        self.assertNotIn("Ошибка в записи шаблона", confirm)
        await h.callback("tp_save")
        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, "Купить [main]")
        self.assertNotIn("javascript:", stored)

        # URL с пробелом грамматика не умеет: тоже подпись без ссылки.
        await h.callback(f"tp_edit:{template_id}")
        spaced = "Сайт [main]"
        await h.text(
            spaced,
            entities=[
                ent(
                    "text_link",
                    *span(spaced, "Сайт"),
                    url="https://e.example/a b",
                )
            ],
        )
        self.assertNotIn("Ошибка в записи шаблона", h.screen_text())
        await h.callback("tp_save")
        stored = _version_content(h, template_id, 3)
        self.assertEqual(stored, "Сайт [main]")
        assert_safe_html(self, render_to_html(parse_template(stored)))
