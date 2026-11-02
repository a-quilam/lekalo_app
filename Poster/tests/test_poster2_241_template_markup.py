# tests/test_poster2_241_template_markup.py
r"""
Poster 2.4.1 T1: Telegram-форматирование в редакторе шаблонов.

Проверяется вся цепочка «ввод → сохранение → чтение → редактирование →
рендер»: сообщение с entities из клиента Telegram превращается в
канонический текст грамматики Stage 24 (`entities_to_template_markup`),
переживает сохранение версии шаблона и даёт тот же HTML анонса, что и
прямой `entities_to_html`.

1. **Конвертер** (юнит): жирный/курсив/подчёркивание/зачёркивание/спойлер,
   code/pre, text_link с произвольной подписью, URL-entity, вложенность,
   кириллица и эмодзи (смещения UTF-16), несколько ссылок, литеральные
   спецсимволы, отсутствие entities.
2. **Раунд-трип**: `render_to_html(parse_template(конвертер))` ==
   `entities_to_html` для поддерживаемых типов.
3. **Грамматика Stage 24**: маркеры `||`, `` ` ``, ```` ``` ````, `\|`,
   `` \` ``, `tg://` в ссылке, дефисы в подписи.
4. **Интеграция** через настоящее приложение: редактор личного шаблона,
   сохранение секции с форматированием, повторное открытие, рендер анонса
   и подстановка `[название]`/`[адрес]` рядом с форматированным текстом.
5. **Дефекты T1 (найдены Tester'ом)**:
   D1 — `[код]` в сообщении с entities остаётся кирпичиком (привязки
   сохраняются, анонс подставляет значение, а не литерал `[[main]]`);
   D2 — порядок entities при равных смещениях не важен (ссылка внутри
   форматирования); D3 — text_link на часть текста с вложенным
   форматированием пишется явной формой `{подпись}(URL)`;
   D4/D5 — URL со скобками и схемы вне грамматики деградируют в подпись
   без ссылки и не роняют сохранение.
"""

from __future__ import annotations

import unittest

from telegram import MessageEntity

from template_parser import (
    BrickNode,
    LinkNode,
    TemplateParseError,
    parse_template,
    render_to_html,
)
from utils.tg_html import entities_to_html, entities_to_template_markup


def ent(kind: str, offset: int, length: int, **kwargs) -> MessageEntity:
    """Shortcut для MessageEntity с явным типом."""
    return MessageEntity(type=kind, offset=offset, length=length, **kwargs)


def span(text: str, label: str, start: int = 0) -> tuple[int, int]:
    """
    Смещение и длина подстроки в код-юнитах UTF-16.

    Telegram считает смещения в UTF-16, поэтому «обычный» `text.index` дал бы
    другую длину: эмодзи занимает два код-юнита.
    """
    index = text.index(label, start)
    offset = len(text[:index].encode("utf-16-le")) // 2
    return offset, len(label.encode("utf-16-le")) // 2


class MarkupConverterTests(unittest.TestCase):
    """entities → текст грамматики Stage 24."""

    def convert(self, text: str, entities: list[MessageEntity]) -> str:
        return entities_to_template_markup(text, entities)

    def test_bold_header(self) -> None:
        text = "Летний концерт"
        self.assertEqual(
            self.convert(text, [ent("bold", 0, len(text))]), "**Летний концерт**"
        )

    def test_italic_inside_sentence(self) -> None:
        text = "Сегодня очень весело"
        start, length = span(text, "очень")
        self.assertEqual(
            self.convert(text, [ent("italic", start, length)]),
            "Сегодня *очень* весело",
        )

    def test_underline_and_strikethrough_and_spoiler(self) -> None:
        self.assertEqual(
            self.convert("подчёркнутый", [ent("underline", 0, 12)]),
            "__подчёркнутый__",
        )
        self.assertEqual(self.convert("да", [ent("underline", 0, 2)]), "__да__")
        self.assertEqual(self.convert("нет", [ent("strikethrough", 0, 3)]), "~~нет~~")
        self.assertEqual(self.convert("тайна", [ent("spoiler", 0, 5)]), "||тайна||")

    def test_code_and_pre(self) -> None:
        self.assertEqual(self.convert("print()", [ent("code", 0, 7)]), "`print()`")
        self.assertEqual(self.convert("блок", [ent("pre", 0, 4)]), "```блок```")

    def test_hidden_text_link_with_arbitrary_label(self) -> None:
        # URL отсутствует в видимом тексте: адрес обязан прийти из entity.
        text = "Наше-место"
        markup = self.convert(
            text, [ent("text_link", 0, len(text), url="https://example.com/place")]
        )
        self.assertEqual(markup, "Наше-место(https://example.com/place)")

    def test_bold_with_link(self) -> None:
        text = "Дом культуры"
        entities = [
            ent("bold", 0, len(text)),
            ent("text_link", 0, len(text), url="https://example.com"),
        ]
        markup = self.convert(text, entities)
        self.assertIn("<b>", render_to_html(parse_template(markup)))
        self.assertIn("<a href=", render_to_html(parse_template(markup)))
        self.assertEqual(
            render_to_html(parse_template(markup)), entities_to_html(text, entities)
        )

    def test_tg_scheme_link_is_kept(self) -> None:
        text = "Канал"
        markup = self.convert(
            text, [ent("text_link", 0, len(text), url="tg://resolve?domain=poster")]
        )
        self.assertIn("tg://resolve?domain=poster", markup)
        self.assertIn(
            'href="tg://resolve?domain=poster"', render_to_html(parse_template(markup))
        )

    def test_cyrillic_and_emoji_before_entity(self) -> None:
        # Эмодзи занимает два код-юнита UTF-16: смещение в индексах Python
        # дало бы сдвиг форматирования на один символ.
        text = "🎉 Привет мир"
        start, length = span(text, "Привет")
        markup = self.convert(text, [ent("bold", start, length)])
        self.assertEqual(markup, "🎉 **Привет** мир")

    def test_multiple_links(self) -> None:
        text = "Первая и вторая"
        entities = [
            ent("text_link", 0, len("Первая"), url="https://a.example"),
            ent("text_link", 9, len("вторая"), url="https://b.example"),
        ]
        html = render_to_html(parse_template(self.convert(text, entities)))
        self.assertIn('href="https://a.example"', html)
        self.assertIn('href="https://b.example"', html)

    def test_literal_html_characters_are_not_broken(self) -> None:
        text = "a<b>&c"
        markup = self.convert(text, [ent("bold", 0, 3)])
        self.assertEqual(
            render_to_html(parse_template(markup)),
            entities_to_html(text, [ent("bold", 0, 3)]),
        )

    def test_literal_formatting_markers_are_escaped_with_entities(self) -> None:
        text = "жирный **тут** _и_ ~всё~ `код` |труба| (скобки) [квадрат]"
        markup = self.convert(text, [ent("bold", 0, len("жирный"))])
        html = render_to_html(parse_template(markup))
        # Ни один литеральный маркер не стал оформлением.
        self.assertEqual(html.count("<b>"), 1)
        self.assertNotIn("<i>", html)
        self.assertNotIn("<s>", html)
        self.assertNotIn("<code>", html)
        self.assertNotIn("<tg-spoiler>", html)
        self.assertIn("**тут**", html)
        self.assertIn("[квадрат]", html)

    def test_no_entities_returns_text_unchanged(self) -> None:
        text = "обычный **текст** с [квадратными] скобками"
        self.assertEqual(entities_to_template_markup(text, None), text)
        self.assertEqual(entities_to_template_markup(text, []), text)

    def test_nested_bold_italic(self) -> None:
        text = "оба стиля"
        entities = [
            ent("bold", 0, len(text)),
            ent("italic", 4, 5),
        ]
        html = render_to_html(parse_template(self.convert(text, entities)))
        self.assertEqual(html, entities_to_html(text, entities))

    def test_url_entity_stays_plain_text(self) -> None:
        text = "https://t.me/poster"
        markup = self.convert(text, [ent("url", 0, len(text))])
        self.assertEqual(markup, text)

    def test_unsupported_entities_degrade_to_text(self) -> None:
        self.assertEqual(
            entities_to_template_markup("упомянутый", [ent("text_mention", 0, 10)]),
            "упомянутый",
        )
        self.assertEqual(
            entities_to_template_markup("emoji", [ent("custom_emoji", 0, 5)]),
            "emoji",
        )


class BrickTokensTests(unittest.TestCase):
    """D1: `[код]` в сообщении с entities остаётся кирпичиком, а не литералом."""

    def convert(self, text: str, entities: list[MessageEntity]) -> str:
        return entities_to_template_markup(text, entities)

    def test_brick_next_to_bold_survives(self) -> None:
        # Редактор шаблона: правка с entities не должна превращать `[main]`
        # в литеральную скобку `[[main]]` — иначе привязка теряется.
        text = "Итог: [main]"
        self.assertEqual(
            self.convert(text, [ent("bold", 0, len("Итог"))]),
            "**Итог**: [main]",
        )

    def test_brick_before_and_after_entities(self) -> None:
        text = "[main] и [адрес]"
        start, length = span(text, "и")
        self.assertEqual(
            self.convert(text, [ent("italic", start, length)]),
            "[main] и [адрес]".replace("и", "*и*", 1),
        )

    def test_cyrillic_system_brick_codes_are_kept(self) -> None:
        text = "[название] — [дата]"
        start, length = span(text, "—")
        markup = self.convert(text, [ent("bold", start, length)])
        self.assertIn("[название]", markup)
        self.assertIn("[дата]", markup)
        # Кириллические коды — валидные кирпичики: парсер их видит.
        bricks = [t for t in parse_template(markup) if isinstance(t, BrickNode)]
        self.assertEqual([t.code for t in bricks], ["название", "дата"])

    def test_user_brick_code_with_underscore_and_digits(self) -> None:
        text = "Цена: [цена_2]"
        markup = self.convert(text, [ent("bold", 0, len("Цена"))])
        self.assertEqual(markup, "**Цена**: [цена_2]")

    def test_literal_brackets_are_still_escaped(self) -> None:
        # Не кирпичик (внутри скобка/вложенная скобка/пусто) — литеральные
        # скобки; настоящий `[код]` в тех же сообщениях остаётся кирпичиком.
        for literal in ("[a(b)]", "[]", "[a[b]]", "["):
            with self.subTest(text=literal):
                markup = self.convert(f"a {literal}", [ent("bold", 0, 1)])
                html = render_to_html(parse_template(markup))
                self.assertIn("**a**", markup)
                # Литерал не стал кирпичиком и не потерялся.
                self.assertFalse(
                    any(isinstance(t, BrickNode) for t in parse_template(markup)),
                    f"{literal!r}: литерал стал кирпичиком",
                )
                self.assertIn("[", html)

    def test_brick_inside_code_span_stays_text(self) -> None:
        # Внутри code/pre кирпичиков нет — `[main]` там буквальный текст.
        markup = self.convert("[main]", [ent("code", 0, 6)])
        self.assertEqual(markup, "`[main]`")
        self.assertFalse(any(isinstance(t, BrickNode) for t in parse_template(markup)))

    def test_brick_inside_link_label_stays_brick(self) -> None:
        # Ссылка, подпись которой содержит кирпичик: обычная запись
        # `подпись(URL)` неоднозначна (кирпичик «съел» бы скобку), поэтому
        # выбирается явная форма.
        text = "Цена [main]"
        markup = self.convert(
            text, [ent("text_link", 0, len(text), url="https://e.example")]
        )
        self.assertEqual(markup, "{Цена [main]}(https://e.example)")
        tokens = parse_template(markup)
        self.assertEqual(len(tokens), 1)
        link = tokens[0]
        assert isinstance(link, LinkNode)
        self.assertEqual(
            [t.code for t in link.label if isinstance(t, BrickNode)], ["main"]
        )
        self.assertEqual(
            render_to_html(tokens), '<a href="https://e.example">Цена [main]</a>'
        )

    def test_brick_before_literal_closing_bracket(self) -> None:
        # `[main]]` — по грамматике это кирпичик + литеральная `]`: одиночная
        # закрывающая скобка после токена допустима, экранированием (`]]`)
        # является только пара. Иначе с entities кирпичик терялся.
        markup = self.convert("a [main]] b", [ent("bold", 0, 1)])
        self.assertEqual(markup, "**a** [main]]] b")
        tokens = parse_template(markup)
        self.assertEqual([t.code for t in tokens if isinstance(t, BrickNode)], ["main"])
        self.assertEqual(render_to_html(tokens), "<b>a</b> [main]] b")
        # `[[…]]` остаётся экранированной литеральной скобкой.
        self.assertFalse(
            any(
                isinstance(t, BrickNode)
                for t in parse_template(
                    self.convert("a [[main]] b", [ent("bold", 0, 1)])
                )
            )
        )

    def test_literal_double_brackets_are_escaped(self) -> None:
        # `[[…]]` — уже литеральная скобка автора: удваивается, а не
        # превращается в кирпичик.
        markup = self.convert("a [[main]] b", [ent("bold", 0, 1)])
        self.assertEqual(markup, "**a** [[[[main]]]] b")
        self.assertFalse(any(isinstance(t, BrickNode) for t in parse_template(markup)))

    def test_brick_pattern_matches_parser(self) -> None:
        # `utils.tg_html` не импортирует прикладной парсер: шаблон кирпичика
        # продублирован, и это единственная точка проверки их согласованности.
        from template_parser import BRICK_PATTERN
        from utils.tg_html import _BRICK_TOKEN_RE

        samples = (
            "[main]",
            "[цена_2]",
            "[название]",
            "[]",
            "[a(b)]",
            "[a[b]]",
            "[a b]",
            "[a]хвост",
            "[a]]",
            "нет токена",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                ours = _BRICK_TOKEN_RE.match(sample)
                theirs = BRICK_PATTERN.match(sample)
                self.assertEqual(
                    ours.group(0) if ours else None,
                    theirs.group(0) if theirs else None,
                )


class EntityOrderTests(unittest.TestCase):
    """D2: порядок entities при равных смещениях не влияет на результат."""

    TEXT = "Дом культуры"
    URL = "https://e.example"

    def _entities(self) -> list[list[MessageEntity]]:
        return [
            [
                ent("text_link", 0, 12, url=self.URL),
                ent("bold", 0, 12),
            ],
            [
                ent("bold", 0, 12),
                ent("text_link", 0, 12, url=self.URL),
            ],
        ]

    def test_both_orders_render_same_html(self) -> None:
        expected = '<b><a href="https://e.example">Дом культуры</a></b>'
        for entities in self._entities():
            with self.subTest(order=[e.type for e in entities]):
                markup = entities_to_template_markup(self.TEXT, entities)
                self.assertEqual(render_to_html(parse_template(markup)), expected)

    def test_link_is_inner_layer(self) -> None:
        for entities in self._entities():
            with self.subTest(order=[e.type for e in entities]):
                markup = entities_to_template_markup(self.TEXT, entities)
                # Ссылка внутри жирного, а не наоборот.
                self.assertFalse(markup.startswith("{"))
                self.assertIn("{Дом культуры}(https://e.example)", markup)
                self.assertEqual(markup, "**{Дом культуры}(https://e.example)**")

    def test_url_entity_order_is_same(self) -> None:
        # `url` тоже не должен перекрывать форматирование.
        for entities in (
            [ent("url", 0, 12), ent("bold", 0, 12)],
            [ent("bold", 0, 12), ent("url", 0, 12)],
        ):
            with self.subTest(order=[e.type for e in entities]):
                markup = entities_to_template_markup(self.TEXT, entities)
                self.assertEqual(
                    render_to_html(parse_template(markup)), "<b>Дом культуры</b>"
                )


class PartialLinkTests(unittest.TestCase):
    """D3: text_link на часть текста с вложенным форматированием."""

    def test_link_over_tail_with_italic(self) -> None:
        text = "Купить билеты на сайте"
        entities = [
            ent("text_link", 0, 22, url="https://e.example"),
            ent("italic", 14, 8),
        ]
        markup = entities_to_template_markup(text, entities)
        self.assertEqual(markup, "{Купить билеты *на сайте*}(https://e.example)")
        self.assertEqual(
            render_to_html(parse_template(markup)),
            '<a href="https://e.example">Купить билеты <i>на сайте</i></a>',
        )

    def test_link_over_prefix_with_bold_suffix_stays(self) -> None:
        text = "Дом культуры сегодня"
        entities = [
            ent("text_link", 0, 19, url="https://e.example"),
            ent("bold", 0, 12),
        ]
        markup = entities_to_template_markup(text, entities)
        self.assertEqual(
            render_to_html(parse_template(markup)),
            '<a href="https://e.example"><b>Дом культуры</b> сегодн</a>я',
        )
        self.assertIn("{**Дом культуры** сегодн}(https://e.example)", markup)

    def test_link_in_middle_of_sentence(self) -> None:
        text = "До ссылка после"
        entities = [ent("text_link", 3, 6, url="https://e.example")]
        markup = entities_to_template_markup(text, entities)
        self.assertEqual(
            render_to_html(parse_template(markup)),
            'До <a href="https://e.example">ссылка</a> после',
        )


class UnexpressibleLinkTests(unittest.TestCase):
    """D4/D5: URL, который грамматика не умеет записать, — деградация в текст."""

    CASES = (
        "https://e.example/a[b]",  # D4: квадратные скобки
        "https://e.example/Кино_(фильм)",  # D5: круглые скобки
        "mailto:poster@example.com",  # схема вне грамматики
        "ftp://e.example/file",
        "https://e.example/a b",  # пробел
        "https://e.example/a\\b",  # обратный слэш — экранирование в грамматике
    )

    def test_degradation_keeps_label_and_does_not_break_saving(self) -> None:
        for url in self.CASES:
            with self.subTest(url=url):
                markup = entities_to_template_markup(
                    "Статья", [ent("text_link", 0, 6, url=url)]
                )
                self.assertEqual(markup, "Статья")
                # Главное: сохранение не падает с TemplateParseError.
                self.assertEqual(render_to_html(parse_template(markup)), "Статья")

    def test_expressible_urls_are_still_links(self) -> None:
        for url in (
            "https://e.example/a",
            "http://e.example/a?b=c#d",
            "tg://resolve?domain=poster",
            "t.me/poster",
        ):
            with self.subTest(url=url):
                markup = entities_to_template_markup(
                    "Статья", [ent("text_link", 0, 6, url=url)]
                )
                self.assertTrue(markup.startswith("Статья("), markup)
                self.assertIn("<a href=", render_to_html(parse_template(markup)))


class RoundTripTests(unittest.TestCase):
    """entities → грамматика → parse → render_to_html == entities_to_html."""

    CASES: list[tuple[str, list[MessageEntity]]] = [
        ("Жирный заголовок", [ent("bold", 0, 15)]),
        ("Жирный заголовок", [ent("bold", 0, 6), ent("italic", 7, 9)]),
        ("подчёркнутый", [ent("underline", 0, 12)]),
        ("зачёркнутый", [ent("strikethrough", 0, 11)]),
        ("секрет", [ent("spoiler", 0, 6)]),
        ("print()", [ent("code", 0, 7)]),
        ("блок", [ent("pre", 0, 4)]),
        (
            "Ссылка",
            [ent("text_link", 0, 6, url="https://example.com/x")],
        ),
        (
            "🎉 Ссылка на место",
            [ent("text_link", 3, 14, url="tg://resolve?domain=poster")],
        ),
        (
            "Жирная ссылка",
            [
                ent("bold", 0, 13),
                ent("text_link", 0, 13, url="https://example.com"),
            ],
        ),
        (
            "До ссылка после",
            [ent("text_link", 3, 6, url="https://example.com")],
        ),
        ("a < b & c", [ent("bold", 0, 1)]),
    ]

    def test_round_trip_matches_entities_to_html(self) -> None:
        # URL-entity сюда не входит: по контракту задачи голый URL остаётся
        # текстом (клиент Telegram сам делает его кликабельным), поэтому
        # `entities_to_html` с явным `<a>` — не эталон для этого типа.
        for text, entities in self.CASES:
            with self.subTest(text=text):
                markup = entities_to_template_markup(text, entities)
                self.assertEqual(
                    render_to_html(parse_template(markup)),
                    entities_to_html(text, entities),
                )


class GrammarExtensionsTests(unittest.TestCase):
    """Новые маркеры грамматики Stage 24: спойлер, code, pre, tg://."""

    def _html(self, text: str) -> str:
        return render_to_html(parse_template(text, section="body"))

    def test_spoiler_marker(self) -> None:
        self.assertEqual(self._html("||секрет||"), "<tg-spoiler>секрет</tg-spoiler>")

    def test_spoiler_supports_nesting(self) -> None:
        self.assertEqual(
            self._html("||секрет с **жирным**||"),
            "<tg-spoiler>секрет с <b>жирным</b></tg-spoiler>",
        )

    def test_code_marker(self) -> None:
        self.assertEqual(self._html("`print()`"), "<code>print()</code>")

    def test_pre_marker(self) -> None:
        self.assertEqual(self._html("```блок\nкода```"), "<pre>блок\nкода</pre>")

    def test_code_is_literal_span(self) -> None:
        # Внутри code нет вложенного форматирования и кирпичиков.
        self.assertEqual(self._html("`**не жирный**`"), "<code>**не жирный**</code>")
        self.assertEqual(self._html("`[название]`"), "<code>[название]</code>")

    def test_escaped_spoiler_and_backtick(self) -> None:
        self.assertEqual(self._html("\\|\\|не спойлер\\|\\|"), "||не спойлер||")
        self.assertEqual(self._html("\\`не код\\`"), "`не код`")

    def test_unclosed_code_marker_stays_text(self) -> None:
        self.assertEqual(self._html("`не закрыто"), "`не закрыто")

    def test_tg_scheme_allowed_in_link(self) -> None:
        self.assertEqual(
            self._html("Канал(tg://resolve?domain=poster)"),
            '<a href="tg://resolve?domain=poster">Канал</a>',
        )

    def test_dangerous_schemes_still_rejected(self) -> None:
        for url in ("javascript:alert(1)", "data:text/html,x", "file:/etc/passwd"):
            with self.assertRaises(TemplateParseError):
                parse_template(f"Текст({url})", section="body")

    def test_hyphens_in_label_are_literal(self) -> None:
        tokens = parse_template("Наше-место(https://example.com)", section="body")
        self.assertEqual(len(tokens), 1)
        assert isinstance(tokens[0], LinkNode)
        self.assertEqual(
            self._html("Наше-место(https://example.com)"),
            '<a href="https://example.com">Наше-место</a>',
        )

    def test_explicit_label_form(self) -> None:
        # `{подпись}(URL)` — ссылка в середине предложения: подпись не
        # захватывает предшествующий текст.
        self.assertEqual(
            self._html("До {ссылка}(https://example.com) после"),
            'До <a href="https://example.com">ссылка</a> после',
        )

    def test_explicit_label_keeps_nested_formatting(self) -> None:
        self.assertEqual(
            self._html("{**жирно**}(https://example.com)"),
            '<a href="https://example.com"><b>жирно</b></a>',
        )

    def test_empty_code_span_stays_text(self) -> None:
        # Пустая пара кавычек — не пустой тег, а литеральный текст.
        self.assertEqual(self._html("``"), "``")
        self.assertEqual(self._html("Не `` и всё"), "Не `` и всё")


# --- Интеграция: редактор шаблонов на настоящем приложении -------------------

try:
    from sqlalchemy import select

    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import _seed_personal_template

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def _version_content(h, template_id: int, revision: int) -> str:
    """Канонический текст сохранённой версии шаблона."""
    from models import TemplateVersion

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


def _version_bricks(h, template_id: int, revision: int) -> dict[str, int]:
    """Сохранённые привязки версии: код кирпичика → brick_id."""
    from models import TemplateVersion, TemplateVersionBrick

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
    """HTML анонса из СОХРАНЁННОЙ версии (привязки восстанавливаются из БД)."""
    from template_store import render_template_version

    with h.session_factory() as session:
        return render_template_version(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            template_id=template_id,
            values_by_id=values_by_id,
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TemplateFormattingFlowTests(unittest.TestCase):
    """
    Форматирование из клиента Telegram проходит весь путь редактора.

    Открыт личный шаблон в разделе «Мои шаблоны», секция приходит сообщением
    с entities, сохраняется, шаблон открывается заново — и разметка на месте.
    """

    async def _open_personal_editor(self, h, template_id: int) -> None:
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit:{template_id}")

    @scenario_test
    async def test_formatted_section_is_saved_as_markup(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            content="Старый текст [main]",
        )
        await self._open_personal_editor(h, template_id)

        # Литеральные маркеры в тексте тоже присутствуют: при наличии
        # entities они экранируются и не превращаются в оформление.
        text = "Заголовок и курсив [main]"
        bold_offset, bold_length = span(text, "Заголовок")
        italic_offset, italic_length = span(text, "курсив")
        await h.text(
            text,
            entities=[
                ent("bold", bold_offset, bold_length),
                ent("italic", italic_offset, italic_length),
            ],
        )

        # Подтверждение показывает канонический текст с маркерами.
        self.assertIn("**Заголовок**", h.screen_text())
        self.assertIn("*курсив*", h.screen_text())

        await h.callback("tp_save")
        stored = _version_content(h, template_id, 2)
        self.assertIn("**Заголовок**", stored)
        self.assertIn("*курсив*", stored)
        self.assertIn("[main]", stored)

    @scenario_test
    async def test_reopened_template_keeps_formatting(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            content="Старый текст [main]",
        )
        await self._open_personal_editor(h, template_id)
        text = "Жирный заголовок [main]"
        await h.text(text, entities=[ent("bold", 0, len("Жирный заголовок"))])
        await h.callback("tp_save")

        # Карточка и повторная правка читают сохранённую версию из БД.
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        self.assertIn("**Жирный заголовок**", h.screen_text())
        await h.callback(f"tp_edit:{template_id}")
        self.assertIn("**Жирный заголовок**", h.screen_text())

    @scenario_test
    async def test_repeated_edit_does_not_lose_formatting(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            content="Старый текст [main]",
        )
        await self._open_personal_editor(h, template_id)
        await h.text(
            "Первый [main]",
            entities=[ent("bold", 0, len("Первый"))],
        )
        await h.callback("tp_save")
        self.assertIn("**Первый**", _version_content(h, template_id, 2))

        # Вторая правка открывает сохранённую версию с маркерами.
        await h.callback(f"tp_edit:{template_id}")
        self.assertIn("**Первый**", h.screen_text(), "черновик — с маркерами")
        # Пользователь присылает новый текст без entities — прежняя разметка
        # не должна «протечь» в новый черновик.
        await h.text("Второй [main]")
        await h.callback("tp_save")
        stored = _version_content(h, template_id, 3)
        self.assertEqual(stored, "Второй [main]")

    @scenario_test
    async def test_announcement_render_has_bold_and_link(self, h) -> None:
        template_id, _revision, bricks = _seed_personal_template(
            h,
            content="Старый [main]",
        )
        await self._open_personal_editor(h, template_id)
        text = "Ссылка на сайт [main]"
        await h.text(
            text,
            entities=[
                ent(
                    "text_link",
                    *span(text, "Ссылка на сайт"),
                    url="https://example.com/place",
                )
            ],
        )
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertIn("Ссылка на сайт(https://example.com/place)", stored)
        html = render_to_html(parse_template(stored))
        self.assertIn('<a href="https://example.com/place">', html)
        self.assertIn("[main]", html)
        self.assertEqual(bricks["main"] > 0, True)

    @scenario_test
    async def test_bricks_next_to_formatted_text_are_substituted(self, h) -> None:
        """
        `[main]` рядом с форматированным текстом остаётся кирпичиком.

        Секция приходит с entities, сохраняется, затем читается как версия
        шаблона: маркеры на месте, `[main]` не «съеден» разметкой.
        """
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            specs=[("main", "Основное поле", "short_text", {})],
            content="Старый [main]",
        )
        await self._open_personal_editor(h, template_id)
        text = "Итог: значение [main]"
        await h.text(
            text,
            entities=[ent("bold", *span(text, "Итог"))],
        )
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertIn("**Итог**", stored)
        self.assertIn("[main]", stored)
        html = render_to_html(parse_template(stored))
        self.assertIn("<b>Итог</b>", html)
        self.assertIn("[main]", html)

    @scenario_test
    async def test_brick_binding_survives_edit_with_entities(self, h) -> None:
        """
        D1: правка сообщением с entities не уничтожает привязку кирпичика.

        До исправления `[main]` экранировался в `[[main]]`: кирпичик
        становился литеральной скобкой, `template_version_bricks` оставалась
        пустой, а анонс показывал текст вместо значения.
        """
        template_id, _revision, bricks = _seed_personal_template(
            h,
            specs=[("main", "Основное поле", "short_text", {})],
            content="Старый [main]",
        )
        await self._open_personal_editor(h, template_id)
        text = "Итог: [main]"
        await h.text(text, entities=[ent("bold", *span(text, "Итог"))])
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        # Кирпичик остался кирпичиком, а не литеральной скобкой.
        self.assertEqual(stored, "**Итог**: [main]")
        self.assertNotIn("[[main]]", stored)

        bindings = _version_bricks(h, template_id, 2)
        self.assertEqual(bindings.get("main"), bricks["main"])

        # Анонс подставляет значение кирпичика вместо токена.
        rendered = _render_version(h, template_id, {bricks["main"]: "Летний концерт"})
        self.assertIn("<b>Итог</b>", rendered)
        self.assertIn("Летний концерт", rendered)
        self.assertNotIn("[main]", rendered)

    @scenario_test
    async def test_tg_link_is_saved_and_rendered(self, h) -> None:
        """
        BLOCKER T1: `tg://`-ссылка сохраняется И рендерится анонсом.

        Парсер принимает схему `tg://` наравне с http(s), а компилятор
        отклонял её как «некорректный адрес» — сохранение проходило, а
        рендер версии падал. Обе стороны должны согласованно разрешать
        `tg://`.
        """
        template_id, _revision, bricks = _seed_personal_template(
            h,
            specs=[("main", "Основное поле", "short_text", {})],
            content="Старый [main]",
        )
        await self._open_personal_editor(h, template_id)
        text = "Скрытая ссылка [main]"
        await h.text(
            text,
            entities=[
                ent(
                    "text_link",
                    *span(text, "Скрытая ссылка"),
                    url="tg://resolve?domain=poster",
                )
            ],
        )
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertIn("Скрытая ссылка(tg://resolve?domain=poster)", stored)

        # Рендер сохранённой версии не падает и отдаёт ту же ссылку.
        rendered = _render_version(h, template_id, {bricks["main"]: "Дом культуры"})
        self.assertIn(
            '<a href="tg://resolve?domain=poster">Скрытая ссылка</a>', rendered
        )
        self.assertIn("Дом культуры", rendered)

    @scenario_test
    async def test_link_over_partial_text_with_bold_is_saved(self, h) -> None:
        """D3: text_link на часть текста с вложенным форматированием."""
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            content="Старый [main]",
        )
        await self._open_personal_editor(h, template_id)
        text = "Купить билеты на сайте"
        await h.text(
            text,
            entities=[
                ent("text_link", 0, 22, url="https://example.com/tickets"),
                ent("italic", *span(text, "на сайте")),
            ],
        )
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertIn("{Купить билеты *на сайте*}(https://example.com/tickets)", stored)
        html = render_to_html(parse_template(stored))
        self.assertIn(
            '<a href="https://example.com/tickets">Купить билеты '
            "<i>на сайте</i></a>",
            html,
        )

    @scenario_test
    async def test_link_with_brackets_in_url_does_not_break_saving(self, h) -> None:
        """D4/D5: URL со скобками деградирует в подпись, сохранение проходит."""
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            content="Старый [main]",
        )
        await self._open_personal_editor(h, template_id)
        text = "Статья [main]"
        await h.text(
            text,
            entities=[
                ent(
                    "text_link",
                    *span(text, "Статья"),
                    url="https://example.com/a[b]",
                )
            ],
        )
        # Ошибки в записи нет: подтверждение, а не экран ошибки.
        self.assertNotIn("Ошибка в записи шаблона", h.screen_text())
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertIn("[main]", stored)
        self.assertIn("Статья", stored)
        # Деградация документирована: подпись без ссылки.
        self.assertNotIn("example.com/a[b]", stored)
