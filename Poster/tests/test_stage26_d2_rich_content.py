# tests/test_stage26_d2_rich_content.py
"""
«Лекало» 2.6, этап D2: приёмка Rich-контента «Помощи» и «О проекте».

Финальное задание 2026-11-08 §13–§17. Проверяется утверждённое содержимое
и поведение загрузчика, а не внутреннее устройство модулей:

1. «Помощь» — ровно девять разделов, «О проекте» — ровно шесть; все
   раскрываемые блоки свёрнуты (в сериализации нет `is_open`);
2. ключевые фразы каждого раздела §13.1–13.9 и §14.1–14.6 совпадают
   дословно с редакцией каталога 2.6.1 (1–5 фраз на раздел);
3. раздел §13.3 содержит настоящую Rich-таблицу: две колонки, заголовочная
   строка «В шаблоне»/«В анонсе» и три строки-примера;
4. §13.7 содержит ДВЕ отдельные Rich-цитаты сравнения;
5. «О проекте» содержит ровно пять кликабельных ссылок с короткими
   подписями: РанДом Культуры, GitHub, ЮMoney, Telegram автора, Мастодон;
6. просьба о поддержке §14.6: сбор «0 из 15 000 ₽» и прямая ссылка ЮMoney,
   жирный призыв «Поддержать разработку:»;
7. в «Помощи» нет отдельной инструкции по условным частям (§17): она живёт
   только в `templates.cond.hint` каталога `ui_texts`;
8. отсутствие PNG `assets/rich/*.png` — не ошибка: раздел §13.6 собирается
   без медиа, в лог идёт WARNING, текстовые блоки раздела остаются;
9. битый Rich-каталог (неизвестный блок, синтаксис YAML, нет страницы) —
   `RichContentError` при загрузке и сборке, НЕ тихий откат на старый текст;
10. в `ui_texts.yaml`/`DEFAULTS` нет дублей длинного содержимого
    `help.rich.*`/`about.rich.*`, кроме служебных `*.rich.unavailable`;
11. миграция 32 даёт системным кирпичикам дословные подсказки §16;
12. `templates.cond.hint` равен дословному тексту §17.

Реальная сеть, Telegram, `.env` и `post_bot.db` не используются: только
временные файлы в /tmp и обычная сборка/сериализация Rich-сообщений.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml
from telegram import InputFile

from about_rich import build_about_rich_message
from help_rich import build_help_rich_message
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from rich_message import (
    InputRichBlockDetails,
    InputRichBlockSlideshow,
    validate_rich_message,
)
from rich_pages import page_message
from tests.stage19a_helpers import make_engine, temp_db_path
from utils import ui_texts
from utils.rich_content import (
    Block,
    BoldPart,
    DetailsBlock,
    DividerBlock,
    LinkPart,
    ListBlock,
    ParagraphBlock,
    QuoteBlock,
    RichContentError,
    RichPage,
    Section,
    SlideshowBlock,
    TableBlock,
    TextPart,
    load_catalog,
)

# --- утверждённое задание §13–§17 (дословно) --------------------------------

HELP_TITLE = "Помощь"
ABOUT_TITLE = "О проекте"

HELP_SECTION_TITLES = (
    "Создание анонса",
    "Редактирование и публикация",
    "Площадки",
    "Шаблоны и кирпичики",
    "Дополнительные поля",
    "Фотографии",
    "Как написать хороший анонс",
    "Управление площадкой",
    "Ошибки и предложения",
)

ABOUT_SECTION_TITLES = (
    "Зачем я это сделал",
    "Как я делал «Лекало»",
    "Что получилось",
    "На что я надеюсь",
    "Открытый код",
    "Как помочь и связаться",
)

# По 1–2 дословные фразы на каждый раздел §13/§14. Ключ — название раздела
# из утверждённого задания, а не `id` каталога: владелец правит YAML, порядок
# и названия закреплены отдельной проверкой.
HELP_PHRASES: dict[str, tuple[str, ...]] = {
    "Создание анонса": (
        "Начать можно двумя способами:",
        "📍 Создать для площадки — выберите площадку из справочника. "
        "Бот возьмёт её шаблон, адрес и ссылки.",
        "📝 Создать по личному шаблону — для анонса без привязки к площадке. "
        "Сначала доступен стандартный шаблон, позже его можно изменить.",
        "Дальше бот задаст вопросы о названии, дате, времени, описании и других "
        "полях шаблона. Необязательное можно пропустить.",
        "В конце получится готовый пост. Его можно проверить, поправить и позже "
        "найти в «Моих анонсах».",
    ),
    "Редактирование и публикация": (
        "Исправить ответ. Откройте готовый анонс и измените нужное поле. "
        "Фотографии тоже можно заменить.",
        "Дописать текст перед публикацией. Перешлите готовый пост, а не "
        "копируйте его: так сохраняются оформление и фотографии.",
        "Не отправляйте недоделанный пост сразу в канал. Сначала поместите его "
        "в отложенные сообщения: там можно выбрать «Изменить» и внести "
        "последние правки.",
    ),
    "Площадки": (
        "В общем справочнике можно выбрать площадку для анонса или найти новое "
        "пространство в другом городе.",
        "У площадки сохраняются адрес, ссылки и собственный шаблон — их не "
        "нужно вводить для каждого мероприятия.",
        "Если площадки ещё нет, её можно добавить в справочник.",
        "Кто может менять сведения? Администраторы площадки. Организаторы "
        "используют её шаблон при создании анонса.",
    ),
    "Шаблоны и кирпичики": (
        "Шаблон — заготовка поста: порядок текста и оформление.",
        "Кирпичики — поля, которые бот заполняет вашими ответами.",
        "Можно создавать свои кирпичики, менять порядок полей и оформление. "
        "Личный шаблон настраиваете вы, шаблон площадки — её администраторы.",
    ),
    "Дополнительные поля": (
        "Не каждому мероприятию нужны время окончания, условия входа или "
        "дополнительные контакты. Для этого у площадки может быть расширенный "
        "вариант шаблона: его включает администратор.",
        "Куратор — выбирается из списка площадки или указывается вручную.",
        "Подвал — готовое напоминание в конце анонса: например, как найти вход "
        "или где оставить донат.",
        "Ненужные дополнительные поля можно не заполнять — в готовом посте они "
        "не появятся.",
    ),
    # §13.5: компактный маркированный список из трёх пунктов. Текст
    # склеивается из частей пункта (строка + жирное выделение).
    "Фотографии": (
        "Можно прикрепить до десяти изображений.",
        "Советую одну квадратную картинку (1:1), но можно отправить несколько.",
        "Фотографии можно поменять и после создания анонса.",
    ),
    "Как написать хороший анонс": (
        "«Лекало» оформит пост, но о самом мероприятии лучше рассказать "
        "конкретно: что будет происходить, можно ли участвовать и нужна ли "
        "подготовка.",
        "Приходите на читку пьесы. Будет интересно!",
        "Читаем пьесу по ролям. Можно послушать или самому взять роль. "
        "Готовиться заранее не нужно.",
        "Подумайте о тех, кто придёт впервые. Если нужна запись, сменная обувь "
        "или знание правил участия, напишите об этом. Повторяющиеся пояснения "
        "можно добавить в шаблон.",
    ),
    "Управление площадкой": (
        "Администраторы площадки могут:",
        "редактировать адрес, ссылки и другие сведения;",
        "настраивать шаблон, кирпичики и дополнительные поля;",
        "вести списки кураторов и подвалов;",
        "назначать других администраторов.",
    ),
    "Ошибки и предложения": (
        "Нашли баг или неудобство — отправьте /report. Команда работает и во "
        "время создания анонса.",
        "Разработчик получит ваше описание и журнал действий за последний час, "
        "но не содержимое сообщений.",
    ),
}

ABOUT_PHRASES: dict[str, tuple[str, ...]] = {
    "Зачем я это сделал": (
        "Я занимаюсь анонсами с 2021 года — с тех пор, как попал в "
        "коммунарское сообщество Москвы и начал делать площадки для "
        "мероприятий.",
        "За эти годы я научился сходу ваять анонс почти в любом формате.",
        "Хороший анонс должен сразу объяснять, что, когда и где происходит,",
        "Нам самим ещё есть куда расти. Чтобы прыгнуть выше, нужна новая "
        "опора — так и появилась идея «Лекала».",
    ),
    "Как я делал «Лекало»": (
        "Начало 2025 года. Я уже пытался навайбкодить бота для анонсов.",
        "Осень 2026-го. Я вернулся к проекту, освоил OpenCode и собрал команду "
        "нейроагентов.",
        "Больше двух недель каждый день добавлял функции, искал ошибки и "
        "переделывал неудачные решения.",
        "На запросы к нейросетям потратил около 15 тысяч рублей",
    ),
    "Что получилось": (
        "Мне было важно сделать бота не только для РанДома.",
        "Адрес, метро, ссылки и другие постоянные сведения не нужно каждый "
        "раз копировать вручную.",
    ),
    "На что я надеюсь": (
        "Мне хотелось сделать бота не только для РанДома.",
        "Надеюсь, «Лекало» пригодится низовым сообществам",
        "Если РанДому за эту работу достанется немного славы, " "я буду только рад.",
    ),
    "Открытый код": (
        "Опенсорс — моя принципиальная позиция.",
        "Исходный код на GitHub",
    ),
    "Как помочь и связаться": (
        "Мне нужны отзывы, предложения и советы, чтобы окончательно "
        "отполировать бота.",
        "Если «Лекало» пригодилось, поделитесь им с товарищами.",
        "Через СБП — написать мне в Telegram",
        "Сейчас собрано 0 из 15 000 ₽, потраченных на разработку бота.",
    ),
}

# §16 дословно: пять описаний, которые пишет миграция 32.
MIGRATION_32_HINTS: dict[str, str] = {
    "название": "Напишите название мероприятия.",
    "дата": "Выберите дату мероприятия или напишите её.",
    "время_начала": "Выберите время начала мероприятия или напишите.",
    "описание": "Пришлите описание мероприятия.",
    "контакт": (
        "Напишите контакты для гостей в формате вашей площадки. Например: "
        "«Ведущий мероприятия: t.me/iddeleted» или «По вопросам площадки: "
        "t.me/vozdvizhenka». Можно в несколько строк."
    ),
}

# §16 дословно: три описания, которые уже были получены миграцией 30.
MIGRATION_30_HINTS: dict[str, str] = {
    "условие_входа": (
        "Напишите условия входа. Например: «Вход бесплатный, без "
        "регистрации», «Вход — 100 ₽» или «Вход по записи»."
    ),
    "куратор": (
        "Выберите куратора или пришлите его контакт: @ник, ссылку или текст "
        "с гиперссылкой."
    ),
    "подвал": "Выберите напоминание, которое появится внизу анонса.",
}

COND_HINT_KEY = "templates.cond.hint"
COND_HINT = (
    "Если кирпичик не заполнен, можно скрыть связанную с ним строку или "
    "часть текста. Выберите кирпичик и укажите, что убирать."
)

LINK_RANDOM_CULTURE = "https://t.me/random_culture"
LINK_GITHUB = "https://github.com/a-quilam/lekalo_app"
LINK_AUTHOR = "https://t.me/a_quilam"
LINK_MASTODON = "https://kommuverse.ru/@mush"
LINK_YOOMONEY = "https://yoomoney.ru/fundraise/1LFG7EPRP2C.261110"
ABOUT_LINKS = frozenset(
    {LINK_RANDOM_CULTURE, LINK_GITHUB, LINK_AUTHOR, LINK_MASTODON, LINK_YOOMONEY}
)

DONATE_PHRASE = "Сейчас собрано 0 из 15 000 ₽, потраченных на разработку бота."

# Битый каталог не должен молча подменяться старым текстом (§15.1).
BROKEN_CATALOG = '"help": [\n'


# --- помощники --------------------------------------------------------------


def _part_text(part: TextPart) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, (LinkPart, BoldPart)):
        return part.text
    raise AssertionError(f"неизвестная часть абзаца: {part!r}")


def _rich_text_plain(value: Any) -> str:
    """Плоский текст готового `RichText` из словаря Bot API."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_rich_text_plain(item) for item in value)
    if isinstance(value, dict):
        return _rich_text_plain(value.get("text", ""))
    return ""


def _bold_texts(value: Any) -> list[str]:
    """Тексты жирных частей готового `RichText` (в порядке появления)."""
    if isinstance(value, str):
        return []
    if isinstance(value, list):
        return [text for item in value for text in _bold_texts(item)]
    if isinstance(value, dict):
        return [str(value.get("text"))] if value.get("type") == "bold" else []
    return []


def _block_texts(block: Block) -> list[str]:
    """Весь читаемый текст блока каталога (без маркеров разметки)."""
    if isinstance(block, ParagraphBlock):
        # Части абзаца склеиваются: жирное выделение внутри фразы не должно
        # разрывать проверяемую дословную формулировку.
        return ["".join(_part_text(part) for part in block.parts)]
    if isinstance(block, ListBlock):
        return ["".join(_part_text(part) for part in item) for item in block.items]
    if isinstance(block, TableBlock):
        return [*block.header, *(cell for row in block.rows for cell in row)]
    if isinstance(block, QuoteBlock):
        return [block.text, *([block.credit] if block.credit else [])]
    if isinstance(block, DividerBlock):
        return []
    if isinstance(block, DetailsBlock):
        return [
            block.summary,
            *(text for child in block.blocks for text in _block_texts(child)),
        ]
    if isinstance(block, SlideshowBlock):
        return [slide.id for slide in block.slides]
    raise AssertionError(f"неизвестный блок каталога: {type(block).__name__}")


def _section_text(section: Section) -> str:
    return "\n".join(
        [section.title, *(t for block in section.blocks for t in _block_texts(block))]
    )


def _page_text(page: RichPage) -> str:
    parts = [page.title, page.intro, page.hint]
    parts.extend(_section_text(section) for section in page.sections)
    return "\n".join(parts)


def _section_links(section: Section) -> list[LinkPart]:
    """Кликабельные ссылки раздела каталога (в любом порядке)."""
    links: list[LinkPart] = []

    def walk(block: Block) -> None:
        if isinstance(block, ParagraphBlock):
            links.extend(part for part in block.parts if isinstance(part, LinkPart))
        elif isinstance(block, ListBlock):
            for item in block.items:
                links.extend(part for part in item if isinstance(part, LinkPart))
        elif isinstance(block, DetailsBlock):
            for child in block.blocks:
                walk(child)

    for block in section.blocks:
        walk(block)
    return links


def _page_links(page: RichPage) -> list[LinkPart]:
    return [link for section in page.sections for link in _section_links(section)]


def _sections_by_title(page: RichPage) -> dict[str, Section]:
    return {section.title: section for section in page.sections}


def _payload_details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    return [block for block in blocks if block.get("type") == "details"]


def _walk_payload(block: dict[str, Any]):
    yield block
    for child in block.get("blocks") or ():
        yield from _walk_payload(child)
    for item in block.get("items") or ():
        for child in item.get("blocks") or ():
            yield from _walk_payload(child)


def _payload_urls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    def collect(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [value] if value.get("type") == "url" else []
        if isinstance(value, list):
            return [url for item in value for url in collect(item)]
        return []

    found: list[dict[str, Any]] = []
    for block in payload["blocks"]:
        for node in _walk_payload(block):
            found.extend(collect(node.get("text", "")))
            found.extend(collect(node.get("summary", "")))
    return found


def _details_child(payload: dict[str, Any], summary: str) -> list[dict[str, Any]]:
    details = next(
        block for block in _payload_details(payload) if block["summary"] == summary
    )
    children = details["blocks"]
    assert isinstance(children, list)
    return children


def _write_temp_catalog(directory: str, text: str) -> Path:
    path = Path(directory) / "rich_content.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _catalog_mapping() -> dict[str, Any]:
    data = yaml.safe_load(
        (
            Path(__file__).resolve().parent.parent / "config" / "rich_content.yaml"
        ).read_text(encoding="utf-8")
    )
    assert isinstance(data, dict)
    return data


# --- 1–4. Структура и дословные тексты «Помощи» ------------------------------


class HelpContentTests(unittest.TestCase):
    """§13: ровно девять свёрнутых разделов с утверждёнными текстами."""

    page: RichPage
    payload: dict[str, Any]
    blocks: list[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.page = load_catalog().help
        cls.payload = build_help_rich_message().to_dict()
        cls.blocks = list(cls.payload["blocks"])

    def test_heading_is_help(self) -> None:
        heading = self.blocks[0]
        self.assertEqual(heading["type"], "heading")
        self.assertEqual(heading["text"], HELP_TITLE)
        self.assertEqual(heading["size"], 1)

    def test_exactly_nine_collapsed_sections(self) -> None:
        details = _payload_details(self.payload)
        self.assertEqual(len(details), 9, "§13: ровно девять разделов")
        self.assertEqual(
            [block["summary"] for block in details], list(HELP_SECTION_TITLES)
        )

    def test_no_details_is_open_at_any_depth(self) -> None:
        states = [
            node.get("is_open")
            for block in self.payload["blocks"]
            for node in _walk_payload(block)
            if node.get("type") == "details"
        ]
        self.assertTrue(states, "в справке нет раскрываемых блоков")
        self.assertTrue(
            all(value is None for value in states),
            "§15.2: разделы изначально свёрнуты, `is_open` не передаётся",
        )

    def test_message_is_valid_rich_message(self) -> None:
        validate_rich_message(build_help_rich_message())

    def test_section_phrases_are_verbatim(self) -> None:
        by_title = _sections_by_title(self.page)
        self.assertEqual(set(by_title), set(HELP_SECTION_TITLES))
        for title, phrases in HELP_PHRASES.items():
            text = _section_text(by_title[title])
            for phrase in phrases:
                with self.subTest(section=title, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_no_intro_and_short_hint_only(self) -> None:
        self.assertEqual(self.page.intro, "", "§13: без вступлений")
        self.assertTrue(self.page.hint.strip())

    def test_section_13_3_has_real_rich_table(self) -> None:
        children = _details_child(self.payload, "Шаблоны и кирпичики")
        tables = [child for child in children if child["type"] == "table"]
        self.assertEqual(len(tables), 1, "§13.3: ровно одна Rich-таблица")
        self.assertTrue(tables[0]["is_bordered"], "таблица с рамкой")
        cells = tables[0]["cells"]
        self.assertEqual(len(cells), 4, "заголовок + три строки-примера")
        self.assertEqual([len(row) for row in cells], [2, 2, 2, 2])

        header, *rows = cells
        self.assertEqual([cell["text"] for cell in header], ["В шаблоне", "В анонсе"])
        self.assertTrue(all(cell.get("is_header") is True for cell in header))
        self.assertEqual(
            [[cell["text"] for cell in row] for row in rows],
            [
                ["[название]", "Читка пьесы"],
                ["[дата]", "9 ноября"],
                ["[время_начала]", "19:00"],
            ],
        )
        self.assertTrue(
            all("is_header" not in cell for row in rows for cell in row),
            "ячейки данных не должны быть заголовочными",
        )

    def test_section_13_7_has_two_separate_quotes(self) -> None:
        children = _details_child(self.payload, "Как написать хороший анонс")
        quotes = [child for child in children if child["type"] == "blockquote"]
        self.assertEqual(len(quotes), 2, "§13.7: две отдельные цитаты")
        texts = []
        for quote in quotes:
            paragraph = quote["blocks"][0]
            self.assertEqual(paragraph["type"], "paragraph")
            texts.append(paragraph["text"])
        self.assertEqual(
            texts,
            [
                "Приходите на читку пьесы. Будет интересно!",
                "Читаем пьесу по ролям. Можно послушать или самому "
                "взять роль. Готовиться заранее не нужно.",
            ],
        )

    def test_section_13_7_dividers_frame_the_comparison(self) -> None:
        """§13.7: разделители до и после блока с примерами «плохо/хорошо»."""
        children = _details_child(self.payload, "Как написать хороший анонс")
        kinds = [child["type"] for child in children]
        self.assertEqual(
            kinds.count("divider"),
            2,
            "§13.7: ровно два разделителя — до и после блока цитат",
        )
        # Первый divider — перед подписью «Слишком расплывчато:» и её цитатой.
        self.assertEqual(kinds[0], "paragraph")
        self.assertEqual(kinds[1], "divider")
        self.assertEqual(kinds[2], "paragraph")
        self.assertEqual(kinds[3], "blockquote")
        # Второй divider — сразу после второй цитаты.
        last_quote = len(kinds) - 1 - kinds[::-1].index("blockquote")
        self.assertEqual(kinds[last_quote + 1], "divider", "divider ПОСЛЕ цитат")
        self.assertEqual(
            kinds,
            [
                "paragraph",
                "divider",
                "paragraph",
                "blockquote",
                "paragraph",
                "blockquote",
                "divider",
                "paragraph",
            ],
        )
        for child in children:
            if child["type"] == "divider":
                self.assertEqual(set(child), {"type"}, "у divider нет полей")

    def test_section_13_1_has_bold_led_list_and_paragraphs(self) -> None:
        """§13.1: вступление, список из двух способов и два абзаца с выделением."""
        children = _details_child(self.payload, "Создание анонса")
        self.assertEqual(
            [child["type"] for child in children],
            ["paragraph", "list", "divider", "paragraph", "paragraph"],
            "§13.1: вступление, список способов, divider и два абзаца",
        )
        self.assertEqual(
            _rich_text_plain(children[0]["text"]),
            "Начать можно двумя способами:",
            "первый абзац — без выделений",
        )
        self.assertEqual(_bold_texts(children[0]["text"]), [])

        items = children[1]["items"]
        self.assertEqual(len(items), 2, "§13.1: ровно два способа создания")
        paragraphs = [item["blocks"][0] for item in items]
        self.assertTrue(all(item["type"] == "paragraph" for item in paragraphs))
        texts = [_rich_text_plain(paragraph["text"]) for paragraph in paragraphs]
        self.assertEqual(
            [text.split(" — ")[0] for text in texts],
            ["📍 Создать для площадки", "📝 Создать по личному шаблону"],
            "каждый пункт начинается с названия способа",
        )
        for paragraph, bold_text in zip(
            paragraphs,
            ["📍 Создать для площадки", "📝 Создать по личному шаблону"],
        ):
            parts = paragraph["text"]
            self.assertIsInstance(parts, list, "пункт состоит из частей")
            first = parts[0]
            self.assertEqual(first.get("type"), "bold", "пункт начинается жирным")
            self.assertEqual(first.get("text"), bold_text)

        for child, bold_text in zip(
            children[3:],
            ["Дальше бот задаст вопросы"],
        ):
            text = child["text"]
            self.assertIsInstance(text, list, "абзац должен состоять из частей")
            first = text[0]
            self.assertEqual(first.get("type"), "bold", "абзац начинается жирным")
            self.assertEqual(first.get("text"), bold_text)

        last = children[4]
        self.assertEqual(_bold_texts(last["text"]), [], "финальный абзац без выделений")
        self.assertEqual(
            _rich_text_plain(last["text"]),
            "В конце получится готовый пост. Его можно проверить, поправить и "
            "позже найти в «Моих анонсах».",
        )

    def test_section_13_2_has_list_and_bold_led_paragraph(self) -> None:
        """§13.2: вступление, список из двух пунктов и абзац с выделением."""
        children = _details_child(self.payload, "Площадки")
        self.assertEqual(
            [child["type"] for child in children],
            ["paragraph", "list", "divider", "paragraph"],
            "§13.2: абзац, список, divider и абзац с выделением",
        )
        self.assertEqual(
            _rich_text_plain(children[0]["text"]),
            "В общем справочнике можно выбрать площадку для анонса или найти "
            "новое пространство в другом городе.",
        )
        items = children[1]["items"]
        self.assertEqual(len(items), 2, "§13.2: ровно два пункта списка")
        self.assertEqual(
            [
                "".join(
                    _rich_text_plain(paragraph["text"]) for paragraph in item["blocks"]
                )
                for item in items
            ],
            [
                "У площадки сохраняются адрес, ссылки и собственный шаблон — их "
                "не нужно вводить для каждого мероприятия.",
                "Если площадки ещё нет, её можно добавить в справочник.",
            ],
        )
        self.assertEqual(
            _bold_texts(children[3]["text"]),
            ["Кто может менять сведения?"],
            "§13.2: у последнего абзаца выделено только начало",
        )

    def test_section_13_5_is_marked_list_of_three_items(self) -> None:
        """§13.5: компактный Rich-список из трёх пунктов с выделениями."""
        children = _details_child(self.payload, "Фотографии")
        self.assertEqual(
            [child["type"] for child in children],
            ["list"],
            "§13.5: раздел — список, а не слипшийся абзац",
        )
        items = children[0]["items"]
        self.assertEqual(len(items), 3, "§13.5: ровно три пункта")
        paragraphs = [item["blocks"][0] for item in items]
        self.assertTrue(all(item["type"] == "paragraph" for item in paragraphs))

        texts = ["".join(_rich_text_plain(part["text"])) for part in paragraphs]
        self.assertEqual(
            texts,
            [
                "Можно прикрепить до десяти изображений.",
                "Советую одну квадратную картинку (1:1), но можно отправить "
                "несколько.",
                "Фотографии можно поменять и после создания анонса.",
            ],
        )
        bold_texts = [_bold_texts(item["text"]) for item in paragraphs]
        self.assertEqual(bold_texts[0], ["до десяти изображений"])
        self.assertEqual(bold_texts[1], ["одну квадратную картинку (1:1)"])
        self.assertEqual(bold_texts[2], [], "третий пункт без выделения")

    def test_section_13_5_list_item_parts_are_rich_text(self) -> None:
        """Пункт списка — массив `RichText`, как и абзац (§13.5)."""
        children = _details_child(self.payload, "Фотографии")
        for item in children[0]["items"]:
            self.assertEqual(set(item), {"blocks"})
            for paragraph in item["blocks"]:
                self.assertEqual(set(paragraph), {"type", "text"})

    def test_section_13_6_highlights_sentence_and_section_name(self) -> None:
        """§13.6: выделены советы-начала абзацев, «Мои анонсы» — нет."""
        children = _details_child(self.payload, "Редактирование и публикация")
        bold_texts: list[str] = []
        for child in children:
            if child["type"] == "paragraph":
                bold_texts.extend(_bold_texts(child["text"]))
        self.assertEqual(
            bold_texts,
            [
                "Исправить ответ.",
                "Дописать текст перед публикацией.",
                "Не отправляйте недоделанный пост сразу в канал.",
            ],
            "§13.6: выделены только советы в начале абзацев",
        )

    def test_help_has_no_clickable_links(self) -> None:
        self.assertEqual(_page_links(self.page), [])


# --- 5–6. «О проекте»: ссылки и донат ---------------------------------------


class AboutContentTests(unittest.TestCase):
    """§14: ровно шесть свёрнутых разделов, пять ссылок, сбор через ЮMoney."""

    page: RichPage
    payload: dict[str, Any]
    blocks: list[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.page = load_catalog().about
        cls.payload = build_about_rich_message().to_dict()
        cls.blocks = list(cls.payload["blocks"])

    def test_heading_is_about(self) -> None:
        heading = self.blocks[0]
        self.assertEqual(heading["type"], "heading")
        self.assertEqual(heading["text"], ABOUT_TITLE)
        self.assertEqual(heading["size"], 1)

    def test_exactly_six_collapsed_sections(self) -> None:
        details = _payload_details(self.payload)
        self.assertEqual(len(details), 6, "§14: ровно шесть разделов")
        self.assertEqual(
            [block["summary"] for block in details], list(ABOUT_SECTION_TITLES)
        )
        states = [
            node.get("is_open")
            for block in self.payload["blocks"]
            for node in _walk_payload(block)
            if node.get("type") == "details"
        ]
        self.assertTrue(states)
        self.assertTrue(
            all(value is None for value in states),
            "§14: разделы изначально свёрнуты",
        )

    def test_message_is_valid_rich_message(self) -> None:
        validate_rich_message(build_about_rich_message())

    def test_section_phrases_are_verbatim(self) -> None:
        by_title = _sections_by_title(self.page)
        self.assertEqual(set(by_title), set(ABOUT_SECTION_TITLES))
        for title, phrases in ABOUT_PHRASES.items():
            text = _section_text(by_title[title])
            for phrase in phrases:
                with self.subTest(section=title, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_rare_bold_highlights_per_section(self) -> None:
        """§1.4: редкие смысловые выделения, «На что я надеюсь» — без них."""
        expected = {
            "Зачем я это сделал": [
                "Хороший анонс должен сразу объяснять, что, когда и где " "происходит,"
            ],
            "Как я делал «Лекало»": [
                "Начало 2025 года.",
                "Осень 2026-го.",
                "15 тысяч рублей",
            ],
            "Что получилось": ["собирать собственные шаблоны из кирпичиков"],
            "На что я надеюсь": [],
            "Открытый код": ["Опенсорс — моя принципиальная позиция."],
            # Просьбу о поддержке владелец утвердил жирной — она остаётся.
            "Как помочь и связаться": ["Поддержать разработку:"],
        }
        children = _payload_details(self.payload)
        by_title = {child["summary"]: child["blocks"] for child in children}
        for title, bold_texts in expected.items():
            found = [
                text
                for block in by_title[title]
                if block["type"] == "paragraph"
                for text in _bold_texts(block["text"])
            ]
            with self.subTest(section=title):
                self.assertEqual(found, bold_texts)

    def test_five_clickable_links_in_expected_sections(self) -> None:
        urls = _payload_urls(self.payload)
        self.assertEqual(len(urls), 5, "§14: ровно пять ссылок, включая ЮMoney")
        self.assertEqual({item["url"] for item in urls}, ABOUT_LINKS)
        for item in urls:
            self.assertEqual(set(item), {"type", "text", "url"})
            self.assertTrue(str(item["text"]).strip(), "пустая подпись ссылки")

        by_title = _sections_by_title(self.page)
        self.assertEqual(
            [link.url for link in _section_links(by_title["Зачем я это сделал"])],
            [LINK_RANDOM_CULTURE],
        )
        self.assertEqual(
            [link.url for link in _section_links(by_title["Открытый код"])],
            [LINK_GITHUB],
        )
        self.assertEqual(
            sorted(
                link.url for link in _section_links(by_title["Как помочь и связаться"])
            ),
            sorted([LINK_YOOMONEY, LINK_AUTHOR, LINK_MASTODON]),
        )

    def test_donate_phrase_is_present_and_bold(self) -> None:
        section = _sections_by_title(self.page)["Как помочь и связаться"]
        text = _section_text(section)
        self.assertIn(DONATE_PHRASE, text)
        self.assertIn("СБП", text)
        bold_parts = [
            part.text
            for block in section.blocks
            if isinstance(block, ParagraphBlock)
            for part in block.parts
            if isinstance(part, BoldPart)
        ]
        self.assertEqual(
            bold_parts,
            ["Поддержать разработку:"],
            "§14.6: призыв к поддержке — жирный абзац",
        )


# --- 7. §17: условные части вне «Помощи» ------------------------------------


class ConditionalHintScopeTests(unittest.TestCase):
    """§17: инструкция по условным частям — только в редакторе шаблона."""

    def test_help_has_no_conditional_parts_instruction(self) -> None:
        help_text = _page_text(load_catalog().help)
        self.assertNotIn("условн", help_text.lower())
        self.assertNotIn(COND_HINT, help_text)
        titles = [section.title for section in load_catalog().help.sections]
        self.assertNotIn("Условные части", titles)

    def test_conditional_hint_lives_in_ui_texts(self) -> None:
        self.assertEqual(ui_texts.tr(COND_HINT_KEY), COND_HINT)
        self.assertEqual(ui_texts.texts().get(COND_HINT_KEY), COND_HINT)
        self.assertEqual(ui_texts.DEFAULTS[COND_HINT_KEY], COND_HINT)


# --- 8. Отсутствие PNG: раздел без медиа и WARNING ---------------------------


class MissingMediaTests(unittest.TestCase):
    """§15.3: каталог может появиться раньше оригиналов владельца."""

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="poster-26-d2-media-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _catalog_without_media(self) -> Path:
        source = (
            Path(__file__).resolve().parent.parent / "config" / "rich_content.yaml"
        ).read_text(encoding="utf-8")
        edited = source.replace(
            "assets/rich/help_forward.png",
            "assets/rich/__d2_missing_forward.png",
        ).replace(
            "assets/rich/help_schedule.png",
            "assets/rich/__d2_missing_schedule.png",
        )
        self.assertNotEqual(edited, source, "не удалось подменить пути медиа")
        return _write_temp_catalog(self.directory, edited)

    def test_catalog_declares_two_slides_in_order(self) -> None:
        section = _sections_by_title(load_catalog().help)["Редактирование и публикация"]
        slideshows = [
            block for block in section.blocks if isinstance(block, SlideshowBlock)
        ]
        self.assertEqual(len(slideshows), 1, "§15.3: настоящее слайд-шоу")
        self.assertEqual(
            [slide.file for slide in slideshows[0].slides],
            [
                "assets/rich/help_forward.png",
                "assets/rich/help_schedule.png",
            ],
        )

    def test_real_project_media_state_matches_build(self) -> None:
        """
        Реальное состояние `assets/rich/`: сборка честно отражает наличие PNG.

        Оригиналы владельца лежат в проекте и отслеживаются Git, поэтому
        рабочая ветка — «файлы есть»: слайд-шоу собирается без WARNING.
        Ветка «файлов нет» остаётся: каталог сам по себе не требует медиа.
        """
        page = load_catalog().help
        root = Path(__file__).resolve().parent.parent
        section = _sections_by_title(page)["Редактирование и публикация"]
        slideshow = next(
            block for block in section.blocks if isinstance(block, SlideshowBlock)
        )
        missing = [
            slide for slide in slideshow.slides if not (root / slide.file).is_file()
        ]

        if missing:
            with self.assertLogs("rich_pages", level="WARNING") as logs:
                payload = page_message(page).to_dict()
            for slide in missing:
                self.assertIn(slide.id, "\n".join(logs.output))
            children = _details_child(payload, "Редактирование и публикация")
            self.assertFalse(
                [child for child in children if child["type"] == "slideshow"]
            )
        else:
            with self.assertNoLogs("rich_pages", level="WARNING"):
                payload = page_message(page).to_dict()
            children = _details_child(payload, "Редактирование и публикация")
            slideshows = [child for child in children if child["type"] == "slideshow"]
            self.assertEqual(len(slideshows), 1)
            self.assertEqual(
                len(slideshows[0]["blocks"]),
                2,
                "§1.5: в слайд-шоу оба утверждённых кадра",
            )

    def test_slideshow_uploads_png_bytes_not_a_path(self) -> None:
        """
        §1.5: в Telegram едут настоящие PNG, а не текст пути к ним.

        `telegram.InputFile(str)` трактует строку как СОДЕРЖИМОЕ файла и
        отправляет её UTF-8-байты. Поэтому сборка обязана передавать байты
        картинки и её имя: проверяется и размер, и PNG-сигнатура, и то, что
        имя/MIME не превратились в «application.octet-stream».
        """
        page = load_catalog().help
        root = Path(__file__).resolve().parent.parent
        section = _sections_by_title(page)["Редактирование и публикация"]
        slideshow = next(
            block for block in section.blocks if isinstance(block, SlideshowBlock)
        )
        if any(not (root / slide.file).is_file() for slide in slideshow.slides):
            self.skipTest("оригиналы PNG отсутствуют в проекте")

        built = page_message(page)
        details = [
            block
            for block in built.blocks or ()
            if isinstance(block, InputRichBlockDetails)
            and block.summary == "Редактирование и публикация"
        ]
        self.assertEqual(len(details), 1)
        slides = [
            child
            for child in details[0].blocks
            if isinstance(child, InputRichBlockSlideshow)
        ]
        self.assertEqual(len(slides), 1)

        uploaded = [photo.photo.media for photo in slides[0].blocks]
        self.assertEqual(len(uploaded), 2, "§1.5: два кадра в утверждённом порядке")
        for media, slide in zip(uploaded, slideshow.slides):
            with self.subTest(slide=slide.id):
                self.assertIsInstance(
                    media,
                    InputFile,
                    "слайд должен грузиться файлом, а не строкой file_id",
                )
                assert isinstance(media, InputFile)  # для mypy
                content = media.input_file_content
                self.assertIsInstance(content, bytes)
                assert isinstance(content, bytes)  # для mypy
                self.assertEqual(
                    content,
                    (root / slide.file).read_bytes(),
                    "отправляемое содержимое расходится с PNG на диске",
                )
                self.assertTrue(content.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(media.filename, (root / slide.file).name)
                self.assertEqual(media.mimetype, "image/png")

    def test_missing_png_builds_section_without_media(self) -> None:
        path = self._catalog_without_media()
        with mock.patch.dict(os.environ, {"RICH_CONTENT_PATH": str(path)}):
            page = load_catalog().help
            with self.assertLogs("rich_pages", level="WARNING") as logs:
                payload = page_message(page).to_dict()

        warnings = "\n".join(logs.output)
        self.assertIn("нет файла слайд-шоу", warnings)
        self.assertIn("help-forward", warnings)
        self.assertIn("help-schedule", warnings)

        children = _details_child(payload, "Редактирование и публикация")
        self.assertFalse(
            [child for child in children if child["type"] == "slideshow"],
            "без файлов блок слайд-шоу не отправляется",
        )
        texts = [
            _rich_text_plain(child["text"])
            for child in children
            if child["type"] == "paragraph"
        ]
        for expected in (
            "Исправить ответ. Откройте готовый анонс и измените нужное поле. "
            "Фотографии тоже можно заменить.",
            "Дописать текст перед публикацией. Перешлите готовый пост, а не "
            "копируйте его: так сохраняются оформление и фотографии.",
            "Не отправляйте недоделанный пост сразу в канал. Сначала поместите "
            "его в отложенные сообщения: там можно выбрать «Изменить» и внести "
            "последние правки.",
        ):
            self.assertIn(expected, texts)


# --- 9. Битый каталог: fail-fast, без старого текста ------------------------


class RichCatalogFailFastTests(unittest.TestCase):
    """§15.1: ошибка каталога — исключение, а не прежняя редакция справки."""

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="poster-26-d2-broken-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _build_with(self, text: str) -> None:
        path = _write_temp_catalog(self.directory, text)
        with mock.patch.dict(os.environ, {"RICH_CONTENT_PATH": str(path)}):
            with self.assertRaises(RichContentError):
                build_help_rich_message()

    def test_broken_yaml_syntax_raises(self) -> None:
        self._build_with(BROKEN_CATALOG)

    def test_unknown_block_type_raises(self) -> None:
        data = _catalog_mapping()
        data["help"]["sections"][0]["blocks"] = [{"marquee": "текст"}]
        self._build_with(yaml.safe_dump(data, allow_unicode=True))

    def test_divider_with_payload_raises(self) -> None:
        """Разделитель не имеет содержимого: текст в `divider` — ошибка схемы."""
        data = _catalog_mapping()
        data["help"]["sections"][0]["blocks"] = [{"divider": "текст"}]
        self._build_with(yaml.safe_dump(data, allow_unicode=True))

    def test_missing_page_raises(self) -> None:
        data = _catalog_mapping()
        data.pop("about")
        self._build_with(yaml.safe_dump(data, allow_unicode=True))

    def test_broken_catalog_is_not_replaced_by_previous_help(self) -> None:
        path = _write_temp_catalog(self.directory, BROKEN_CATALOG)
        with mock.patch.dict(os.environ, {"RICH_CONTENT_PATH": str(path)}):
            with self.assertRaises(RichContentError):
                build_about_rich_message()


# --- 10. Нет дублей длинного содержимого в ui_texts -------------------------


class UiTextsDuplicationTests(unittest.TestCase):
    """§15.1: единственный источник содержимого — `rich_content.yaml`."""

    ALLOWED = frozenset({"help.rich.unavailable", "about.rich.unavailable"})

    def _forbidden(self, keys: Any) -> list[str]:
        return sorted(
            key
            for key in keys
            if key.startswith(("help.rich.", "about.rich.")) and key not in self.ALLOWED
        )

    def test_yaml_catalog_has_no_rich_content_duplicates(self) -> None:
        path = Path(__file__).resolve().parent.parent / "config" / "ui_texts.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertEqual(self._forbidden(data), [])

    def test_defaults_have_no_rich_content_duplicates(self) -> None:
        self.assertEqual(self._forbidden(ui_texts.DEFAULTS), [])

    def test_only_service_unavailable_texts_remain(self) -> None:
        path = Path(__file__).resolve().parent.parent / "config" / "ui_texts.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        remaining = {
            key for key in data if key.startswith(("help.rich.", "about.rich."))
        }
        self.assertEqual(remaining, self.ALLOWED)


# --- 11. Миграция 32: подсказки §16 ----------------------------------------


def _sql(path: str, statement: str, params: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(statement, params).fetchall()
    finally:
        connection.close()


class SystemBrickHintsMigrationTests(unittest.TestCase):
    """§16: реальные описания системных кирпичиков, а не универсальные."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-26-d2-migr-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)

    def _descriptions(self) -> dict[str, str | None]:
        rows = _sql(
            self.path,
            "SELECT code, description FROM bricks "
            "WHERE scope = 'system' ORDER BY id",
        )
        return {str(code): description for code, description in rows}

    def test_migration_32_is_last(self) -> None:
        self.assertEqual(LATEST_VERSION, 32)
        self.assertEqual(
            (DEFAULT_MIGRATIONS[-1].version, DEFAULT_MIGRATIONS[-1].name),
            (32, "system_brick_hints"),
        )
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)

    def test_migration_32_sets_verbatim_hints(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        descriptions = self._descriptions()
        for code, expected in MIGRATION_32_HINTS.items():
            with self.subTest(code=code):
                self.assertEqual(descriptions.get(code), expected)

    def test_other_system_hints_stay_filled(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        descriptions = self._descriptions()
        for code, expected in MIGRATION_30_HINTS.items():
            with self.subTest(code=code):
                self.assertEqual(descriptions.get(code), expected)
        self.assertTrue(
            all(
                descriptions.get(code) not in (None, "")
                for code in (
                    "название",
                    "дата",
                    "время_начала",
                    "описание",
                    "контакт",
                    "условие_входа",
                    "куратор",
                    "подвал",
                )
            ),
            "обязательные подсказки §16 не должны быть пустыми",
        )

    def test_repeat_run_is_idempotent(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        before = self._descriptions()
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(self._descriptions(), before)

    def test_owner_edited_description_is_not_overwritten(self) -> None:
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:31]), 31)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE bricks SET description = 'Своя подсказка' "
                "WHERE code = 'название' AND scope = 'system'"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        descriptions = self._descriptions()
        self.assertEqual(descriptions.get("название"), "Своя подсказка")
        self.assertEqual(
            descriptions.get("время_начала"), MIGRATION_32_HINTS["время_начала"]
        )


if __name__ == "__main__":
    unittest.main()
