# tests/test_stage261_f_acceptance.py
"""
Poster 2.6.1, этап F — независимая интеграционная приёмка (§11).

Модуль проверяет ТРЕБОВАНИЯ ВЛАДЕЛЬЦА (`docs/agent_team/task_261_20261110.md`,
§11.1–§11.10), а не устройство реализации. Сквозные сценарии идут через
настоящее приложение (`bot.build_application`), `Application.process_update`,
FakeBot и временную SQLite (`tests/integration_harness.py`): рабочая
`post_bot.db`, `.env` и сеть Telegram не используются. Ожидания видимых строк
берутся из действующего каталога `config/ui_texts.yaml` (`tests/ui_texts_expect`),
формулировки разделов Rich-страниц — из `config/rich_content.yaml` через
публичные сборщики.

Что закрепляется по пунктам приёмки.

§11.1 — «Помощь» и «О проекте» остаются Rich Messages: 9 и 6 свёрнутых
разделов с утверждёнными названиями, каждый смысловой абзац — отдельный блок
(ни одного «слипшегося» текста с переносами внутри абзаца), есть жирные
выделения, список, таблица, две отдельные цитаты, разделители и слайд-шоу;
два PNG уходят В ОДНОМ `sendRichMessage` в порядке `help_forward.png` →
`help_schedule.png`, отдельных сообщений с картинками нет; навигация
«Помощь» → «О проекте» → «К помощи» → «Назад» работает и держит ровно один
живой Rich Message.

§11.2 — выбор готового анонса в «Мои анонсы» сразу отправляет копию (полный
текст + сохранённые фотографии), промежуточного «Обзор анонса» и кнопки
«Обновить» нет; второе открытие — ещё одна копия, первая не изменилась;
правка поля не меняет старые копии; сохранённый анонс не даёт ошибку
«Сначала завершите обзор анонса»; устаревший callback не оставляет пустого
экрана.

§11.3 — экран вопроса: шапка `🧩 🏷 Название · 1/5`, ровно одна пустая строка,
затем ровно одна подсказка (без универсального дубля); счётчик совпадает с
фактической последовательностью вопросов (4 и 6), не растёт при «Пропустить»
и корректен при «Назад».

§11.4 — главный редактор: нет «Готово» и «Обновить», кнопка фото показывает
фактическое количество, «Дополнительных полей» нет при выключенном
расширенном варианте и они появляются второй строкой при включённом.

§11.5–§11.6 — карточка шаблона: выключенный вариант не показывает свой текст,
технических полей «Тип»/«Версия» и «Условных частей» нет; включённый —
строка «Расширенный шаблон включён.», кнопка выключения справа от
«Восстановить стандарт»; переключение правит текущее сообщение, не создавая
нового. Редактор: «Текущий текст:», короткая инструкция,
`[📋 Список кирпичиков] [❔ Как редактировать]` / `[◀️ К шаблону]`; «Очистить»
и её сценария нет; возврат ведёт в тот же вариант.

§11.7–§11.8 — «Мои кирпичики» и «Ссылка для создания анонса» без «🏠 В меню»,
экран ссылки без «Создать новую ссылку»; «📋 Скопировать» — настоящая
`CopyTextButton` с полным URL текущего экземпляра бота (≤256 символов) без
callback (нажатие копирования — клиентское, сообщений не порождает); переход
по ссылке сразу открывает первый вопрос и не выдаёт административных прав.

§11.9 — в рабочем коде нет тестовых часов и замороженных дат, рабочий
`app_today()` не подменён; один согласованный день операции доходит до
рендера: сохранённое «сегодня» рендерится и на следующий день, границы
месяца и года проходят.

§11.10 — антирегрессии: анонс площадки (живой блок приходит раньше вопроса),
личный шаблон, условные части, календарь, сохранение черновика, `/report`
посреди мастера.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage261_f_acceptance -v
"""

from __future__ import annotations

import re
import unittest
from datetime import date
from pathlib import Path

from sqlalchemy import select

from tests import test_stage261_c_bricks_invite as invite
from tests import test_stage261_c_templates as tpl
from tests import ui_texts_expect as cat

try:
    from telegram import InputMediaPhoto

    from about_rich import about_section_titles, build_about_rich_message
    from announcement_store import list_announcement_drafts
    from handlers.announcement_wizard import CONVERSATION_NAME
    from help_rich import build_help_rich_message, help_section_titles
    from models import PlaceAdmin, User
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        assert_delivered,
        is_live_record,
        scenario_test,
    )
    from tests.test_stage26_a2_extended_toggle import (
        _callbacks,
        _extended_text,
        _flag,
        _message_calls,
        _open_card,
        _rules,
        _seed_place,
    )
    from tests.test_stage27b_announcement_ui import _seed_personal_template
    from tests.test_stage261_a_wizard_editor import (
        DATA_LABEL,
        EXTRAS_LABEL,
        MENU_LABEL,
        TemplateSpec,
        _blocks,
        _control_rows,
        _deliver,
        _details,
        _finish_without_photos,
        _latest_draft,
        _open_personal_questions,
        _open_place_questions,
        _place,
        _seed_standard,
        _step_total,
        _walk_questions,
    )
    from tests.test_stage261_b_my_announcements import (
        _assert_no_preview_screen,
        _assert_no_stage_error,
        _reopen_from_list,
        _saved_preview_draft,
    )
    from tests.test_stage261_e_dates import DATE_CODE, _DatesCase
    from utils.parsers import app_today

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# --- утверждённые контрактом значения (§1.2, §1.4, §1.5) ---------------------

HELP_TITLE = "Помощь"
ABOUT_TITLE = "О проекте"
HELP_SECTIONS = 9
ABOUT_SECTIONS = 6

ABOUT_URLS = (
    "https://t.me/random_culture",
    "https://github.com/a-quilam/lekalo_app",
    "https://t.me/a_quilam",
    "https://kommuverse.ru/@mush",
    "https://yoomoney.ru/fundraise/1LFG7EPRP2C.261110",
)

# §1.5: порядок оригинальных PNG в одном Rich Message.
SLIDE_FILES = ("help_forward.png", "help_schedule.png")

# §11.2: удалённый сценарий повторного открытия.
REMOVED_PREVIEW_TITLE = "Обзор анонса"
REMOVED_REFRESH = "Обновить"

# §11.8: сценарий перевыпуска ссылки удалён владельцем.
REMOVED_REGENERATE = "Создать новую ссылку"

INVITED_TG = 26_011_102
APP_DIR = Path(__file__).resolve().parent.parent
SKIP_REASON = f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}"


# --- помощники ---------------------------------------------------------------


def _rich_blocks(message: dict) -> list[dict]:
    return list(message.get("blocks") or [])


def _details_blocks(message: dict) -> list[dict]:
    """Свёрнутые разделы в порядке экрана (сериализованный details — плоский)."""
    return [block for block in _rich_blocks(message) if block.get("type") == "details"]


def _nested_blocks(block: dict) -> list[dict]:
    return list(block.get("blocks") or [])


def _types(blocks: list[dict]) -> list[str]:
    return [str(block.get("type")) for block in blocks]


def _text_parts(block: dict) -> list[str]:
    """Строковые части `RichText` абзаца/пункта списка."""
    value = block.get("text")
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(value.get("text") or "")]
    if not isinstance(value, list):
        return []
    parts: list[str] = []
    for part in value:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            parts.append(str(part.get("text") or ""))
    return parts


def _rich_parts(block: dict) -> list[dict]:
    """Вложенные оформленные части `RichText` (жирный, ссылка)."""
    value = block.get("text")
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [part for part in value if isinstance(part, dict)]


def _instance_id(h) -> int:
    """id экземпляра доставленного анонса — источник `anp_*` callback'ов."""
    data = h.control_callback("data")
    if data is None:
        raise AssertionError(f"главный редактор не открыт: {h.live_buttons()}")
    return int(data.split(":")[-1])


def _photo_calls(h) -> list[dict]:
    return [call for call in h.calls if call["type"] in {"photo", "media_group"}]


def _rich_calls(h) -> list[dict]:
    return [call for call in h.calls if call["type"] == "rich_message"]


def _assert_no_image_messages(case, h, *, where: str) -> None:
    """§11.1: картинки не уходят отдельными сообщениями."""
    images = [
        call for call in h.calls if call["type"] in {"photo", "media_group", "document"}
    ]
    case.assertEqual(images, [], f"{where}: отдельные сообщения с картинками")


async def _deliver_with_photo(h, *, file_id: str = "photo-261") -> int:
    """Проходит мастер и отправляет анонс с ОДНОЙ фотографией."""
    await _walk_questions(h)
    await h.photo(file_id)
    draft = _latest_draft(h)
    assert_delivered(h, draft.id)
    return int(draft.id)


# --- §11.1: структура Rich Messages -----------------------------------------


@unittest.skipIf(INTEGRATION_IMPORT_ERROR is not None, SKIP_REASON)
class RichStructureTests(unittest.TestCase):
    """
    §1.2–§1.4: содержание Rich-страниц без запуска бота.

    Проверяется именно СТРУКТУРА полученного сообщения (блоки Bot API), а не
    синтаксис HTML: владелец требует визуально различимые смысловые блоки.
    """

    @staticmethod
    def _section(message: dict, title: str) -> list[dict]:
        for block in _details_blocks(message):
            if block.get("summary") == title:
                return _nested_blocks(block)
        raise AssertionError(f"раздела «{title}» нет: {message}")

    def test_help_has_nine_collapsed_sections_with_approved_titles(self) -> None:
        message = build_help_rich_message().to_dict()
        sections = _details_blocks(message)
        self.assertEqual(len(sections), HELP_SECTIONS, _types(_rich_blocks(message)))
        self.assertEqual(
            [section.get("summary") for section in sections],
            list(help_section_titles()),
            "названия/порядок разделов справки",
        )
        self.assertEqual(_rich_blocks(message)[0].get("text"), HELP_TITLE)
        for section in sections:
            self.assertNotIn("is_open", section, "раздел обязан быть свёрнутым (§1.2)")
            self.assertTrue(
                _nested_blocks(section), f"пустой раздел: {section.get('summary')}"
            )

    def test_help_paragraphs_are_separate_blocks(self) -> None:
        """§1.2: каждый смысловой абзац — отдельный блок, без «стены текста»."""
        message = build_help_rich_message().to_dict()
        for section in _details_blocks(message):
            for child in _nested_blocks(section):
                for text in _text_parts(child):
                    self.assertNotIn(
                        "\n\n",
                        text,
                        "абзац склеен переносами внутри блока "
                        f"«{section.get('summary')}»",
                    )

        creation = self._section(message, "Создание анонса")
        self.assertEqual(
            _types(creation),
            ["paragraph", "list", "divider", "paragraph", "paragraph"],
            "§1.3.1: вступление, список способов, разделитель и два абзаца",
        )
        methods = creation[1].get("items") or []
        self.assertEqual(len(methods), 2, "два способа создания анонса")
        for item in methods:
            paragraph = item["blocks"][0]
            parts = _rich_parts(paragraph)
            self.assertEqual(
                parts[0].get("type"), "bold", "пункт списка начинается выделением"
            )
        for child, expected_bold in zip(creation[3:], ["Дальше бот задаст вопросы"]):
            bold = [part for part in _rich_parts(child) if part.get("type") == "bold"]
            self.assertEqual(len(bold), 1, "у абзаца одно смысловое выделение")
            self.assertEqual(str(bold[0].get("text")), expected_bold)
        self.assertEqual(
            [part for part in _rich_parts(creation[4]) if part.get("type") == "bold"],
            [],
            "финальный абзац — без выделений",
        )

    def test_help_has_list_table_quotes_dividers_and_slideshow(self) -> None:
        message = build_help_rich_message().to_dict()

        photos = self._section(message, "Фотографии")
        self.assertEqual(_types(photos), ["list"], "компактный список (§1.3.5)")
        items = photos[0].get("items") or []
        self.assertEqual(len(items), 3, f"пунктов списка: {len(items)}")
        self.assertTrue(all(item.get("blocks") for item in items))

        templates = self._section(message, "Шаблоны и кирпичики")
        tables = [child for child in templates if child.get("type") == "table"]
        self.assertEqual(len(tables), 1, "ровно одна таблица (§1.3.3)")
        cells = tables[0].get("cells") or []
        self.assertEqual(len(cells), 4, "строка заголовка + три примера")
        for row in cells:
            self.assertEqual(len(row), 2, "две колонки «В шаблоне»/«В анонсе»")
        self.assertTrue(all(cell.get("is_header") for cell in cells[0]))
        self.assertIs(tables[0].get("is_bordered"), True)
        self.assertEqual(
            _types(templates),
            ["list", "table", "divider", "paragraph"],
            "§1.3.3: список определений, таблица, разделитель и абзац",
        )

        good = self._section(message, "Как написать хороший анонс")
        self.assertEqual(
            _types(good).count("blockquote"), 2, "две отдельные цитаты (§1.3.7)"
        )
        self.assertGreaterEqual(_types(good).count("divider"), 2, "разделители")
        for quote in [child for child in good if child.get("type") == "blockquote"]:
            self.assertEqual(
                _types(_nested_blocks(quote)),
                ["paragraph"],
                "цитата — настоящий Rich-блок, а не строка текста",
            )

        editing = self._section(message, "Редактирование и публикация")
        slideshows = [child for child in editing if child.get("type") == "slideshow"]
        self.assertEqual(len(slideshows), 1, "слайд-шоу в разделе (§1.3.6)")
        slides = _nested_blocks(slideshows[0])
        self.assertEqual(len(slides), 2, f"кадров: {len(slides)}")
        for slide in slides:
            self.assertEqual(slide.get("type"), "photo")
            self.assertIsInstance(
                slide.get("photo"), (InputMediaPhoto, dict), "кадр — не фото"
            )
        # Файлы настоящие: байты PNG поднимаются в вызов псевдо-вложением.
        media = [slide["photo"] for slide in slides]
        files = [item.get("media") for item in media if isinstance(item, dict)]
        self.assertTrue(files, f"кадры без файла: {media}")
        for item in files:
            content = getattr(item, "input_file_content", None)
            self.assertIsNotNone(content, "у кадра нет байтов файла")
            self.assertTrue(content, "кадр слайд-шоу пуст")

    def test_about_has_six_sections_urls_and_bold_marks(self) -> None:
        message = build_about_rich_message().to_dict()
        sections = _details_blocks(message)
        self.assertEqual(len(sections), ABOUT_SECTIONS, _types(_rich_blocks(message)))
        self.assertEqual(
            [section.get("summary") for section in sections],
            list(about_section_titles()),
            "названия/порядок разделов «О проекте»",
        )
        self.assertEqual(_rich_blocks(message)[0].get("text"), ABOUT_TITLE)
        for section in sections:
            self.assertNotIn("is_open", section, "раздел обязан быть свёрнутым")

        urls: list[str] = []

        def collect(value: object) -> None:
            if isinstance(value, dict):
                if value.get("url"):
                    urls.append(str(value["url"]))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(sections)
        self.assertEqual(
            sorted(set(urls)), sorted(ABOUT_URLS), "все ссылки владельца сохранены"
        )

        def bold_texts(title: str) -> list[str]:
            out: list[str] = []
            for child in self._section(message, title):
                if child.get("type") != "paragraph":
                    continue
                out.extend(
                    str(part.get("text"))
                    for part in _rich_parts(child)
                    if part.get("type") == "bold"
                )
            return out

        self.assertTrue(bold_texts("Открытый код"), "опенсорс выделен жирным")
        self.assertEqual(
            bold_texts("Как помочь и связаться"),
            ["Поддержать разработку:"],
            "призыв к поддержке остался жирным",
        )
        self.assertEqual(bold_texts("На что я надеюсь"), [], "лишних выделений нет")


@unittest.skipIf(INTEGRATION_IMPORT_ERROR is not None, SKIP_REASON)
class RichNavigationTests(unittest.TestCase):
    """§11.1: одна отправка на открытие, порядок PNG и рабочая навигация."""

    @scenario_test
    async def test_help_sends_one_rich_message_with_two_slides_in_order(
        self, h
    ) -> None:
        await h.text("/start")
        await h.callback("help")

        self.assertEqual(len(h.bot.rich_records), 1, "ровно один Rich Message")
        self.assertEqual(len(_rich_calls(h)), 1, "второй отправки нет")

        call = _rich_calls(h)[0]
        media = [
            (name, getattr(value, "filename", None))
            for name, value in call.items()
            if name.startswith("rich_media")
        ]
        self.assertEqual(
            media,
            [("rich_media_0", SLIDE_FILES[0]), ("rich_media_1", SLIDE_FILES[1])],
            f"файлы ушли не в том порядке: {media}",
        )

        payload = h.bot.rich_records[-1]["rich_message"]
        self.assertEqual(
            len(_details_blocks(payload)), HELP_SECTIONS, "разделов в отправке"
        )
        serialized = repr(payload)
        self.assertLess(
            serialized.index("attach://rich_media_0"),
            serialized.index("attach://rich_media_1"),
            "порядок кадров в payload",
        )
        _assert_no_image_messages(self, h, where="справка")
        self.assertEqual(
            [label for label, _data in h.live_buttons()],
            [cat.text("buttons.help_back"), cat.text("buttons.help_about")],
            "клавиатура справки",
        )

    @scenario_test
    async def test_about_navigation_keeps_single_live_rich_message(self, h) -> None:
        await h.text("/start")
        await h.callback("help")
        help_id = int(h.bot.rich_records[-1]["message_id"])

        await h.callback("help_about")
        self.assertEqual(len(h.bot.rich_records), 2, "«О проекте» — одна отправка")
        about_id = int(h.bot.rich_records[-1]["message_id"])
        self.assertIn(help_id, h.deleted_ids(), "справка закрыта перед «О проекте»")
        self.assertEqual(
            len(_details_blocks(h.bot.rich_records[-1]["rich_message"])),
            ABOUT_SECTIONS,
            "разделов в «О проекте»",
        )
        self.assertEqual(
            [data for _label, data in h.live_buttons()],
            ["about_help"],
            "кнопка возврата к помощи",
        )
        _assert_no_image_messages(self, h, where="«О проекте»")

        await h.callback("about_help")
        self.assertEqual(len(h.bot.rich_records), 3, "возврат — новая справка")
        self.assertIn(about_id, h.deleted_ids(), "«О проекте» закрыт")
        restored = h.bot.rich_records[-1]["rich_message"]
        self.assertEqual(_rich_blocks(restored)[0].get("text"), HELP_TITLE)
        self.assertEqual(
            [data for _label, data in h.live_buttons()],
            ["help_back", "help_about"],
            "навигация снова полная",
        )
        deleted = set(h.deleted_ids())
        alive = [
            int(record["message_id"])
            for record in h.bot.rich_records
            if int(record["message_id"]) not in deleted
        ]
        self.assertEqual(len(alive), 1, "в чате один живой Rich Message")

        await h.callback("help_back")
        self.assertEqual(
            h.screen_text(), cat.text("menu.main"), "«Назад» вернул в главное меню"
        )
        self.assertIn(
            int(h.bot.rich_records[-1]["message_id"]),
            h.deleted_ids(),
            "справка закрыта при возврате",
        )


# --- §11.2: «Мои анонсы» -----------------------------------------------------


@unittest.skipIf(INTEGRATION_IMPORT_ERROR is not None, SKIP_REASON)
class MyAnnouncementsImmediateCopyTests(unittest.TestCase):
    """§2.2–2.4: готовый анонс отправляется копией сразу, прежние неизменны."""

    @staticmethod
    async def _delivered_with_photo(h) -> int:
        await h.text("/start")
        place_id = await _place(h)
        _seed_standard(h, place_id, content="**[название]**\n\n[описание]")
        await _open_place_questions(h, place_id)
        return await _deliver_with_photo(h)

    @scenario_test
    async def test_selection_sends_full_copy_with_photos_no_preview(self, h) -> None:
        draft_id = await self._delivered_with_photo(h)
        details = _details(h, draft_id)
        self.assertEqual(details.publication_photos, ("photo-261",), "фото черновика")
        photos_before = len(_photo_calls(h))
        posts_before = len(h.posts())
        first_text = h.posts()[0]
        first_ids = tuple(details.delivered_message_ids)
        self.assertTrue(first_text, "текст первого анонса пуст")

        await _reopen_from_list(h, draft_id)

        self.assertEqual(
            len(_photo_calls(h)), photos_before + 1, "фотография не отправлена"
        )
        self.assertEqual(len(h.posts()), posts_before + 1, "копия не отправлена")
        self.assertEqual(h.posts()[-1], first_text, "копия содержит полный текст")
        self.assertEqual(
            _photo_calls(h)[-1].get("caption"),
            first_text,
            "подпись копии — текст анонса",
        )

        _assert_no_preview_screen(self, h, where="открытие готового анонса")
        _assert_no_stage_error(self, h, where="открытие готового анонса")
        self.assertNotIn(
            REMOVED_PREVIEW_TITLE, h.screen_text(), "промежуточный экран обзора"
        )
        labels = [label for label, _data in h.live_buttons()]
        self.assertNotIn(REMOVED_REFRESH, labels, "кнопка «Обновить» осталась")

        rows = _control_rows(h)
        row_labels = [label for row in rows for label, _data in row]
        self.assertNotIn("Готово", row_labels, "кнопка «Готово» запрещена")
        instance_id = _instance_id(h)
        self.assertEqual(
            rows[-1],
            [(MENU_LABEL, f"anp_menu:{instance_id}")],
            "выход в меню последним",
        )
        self.assertTrue(
            [label for label, _data in h.live_buttons() if label == DATA_LABEL],
            "кнопка списка текстовых полей отсутствует",
        )
        self.assertNotIn(first_ids[0], h.deleted_ids(), "первая копия удалена")
        self.assertEqual(
            h.bot.content_text(h.chat_id, first_ids[0]),
            first_text,
            "содержимое первой копии изменилось",
        )

    @scenario_test
    async def test_each_selection_adds_copy_and_edit_keeps_old_ones(self, h) -> None:
        draft_id = await self._delivered_with_photo(h)
        first_ids = tuple(_details(h, draft_id).delivered_message_ids)
        first_text = h.posts()[0]
        posts_before = len(h.posts())

        await _reopen_from_list(h, draft_id)
        second_ids = tuple(_details(h, draft_id).delivered_message_ids)
        second_text = h.posts()[-1]
        self.assertNotEqual(first_ids, second_ids, "копия переиспользована")
        self.assertEqual(len(h.posts()), posts_before + 1)

        await _reopen_from_list(h, draft_id)
        third_ids = tuple(_details(h, draft_id).delivered_message_ids)
        self.assertEqual(len(h.posts()), posts_before + 2, "третьей копии нет")
        self.assertEqual(len({first_ids, second_ids, third_ids}), 3)

        # §2.4: правка поля меняет данные и НЕ трогает старые копии.
        await h.callback(h.control_callback("data"))
        await h.callback(h.live_callbacks("anf_sel:")[0])
        await h.text("Новое название")
        updated = _details(h, draft_id).publication_text
        self.assertIn("Новое название", updated or "", "правка не применена")

        await _reopen_from_list(h, draft_id)
        self.assertEqual(
            h.posts()[-1], updated, f"копия собрана из старых данных: {h.posts()[-3:]}"
        )
        for message_id, expected in (
            (first_ids[0], first_text),
            (second_ids[0], second_text),
        ):
            self.assertEqual(
                h.bot.content_text(h.chat_id, message_id),
                expected,
                "старая копия изменилась при правке",
            )
        self.assertNotIn(first_ids[0], h.deleted_ids(), "старая копия удалена")
        _assert_no_preview_screen(self, h, where="копия после правки")

    @scenario_test
    async def test_saved_draft_copy_and_stale_preview_is_safe(self, h) -> None:
        """§2.2/§2.6: сохранённый черновик — копия; старый callback безопасен."""
        await h.text("/start")
        await _place(h)
        draft_id = await _saved_preview_draft(h)
        details = _details(h, draft_id)
        self.assertIsNone(details.publication_status, "черновик уже доставлен")

        await _reopen_from_list(h, draft_id)
        self.assertTrue(h.posts(), "копия не отправлена")
        _assert_no_stage_error(self, h, where="сохранённый черновик")
        self.assertIsNotNone(h.control_callback("data"), "главный редактор не открылся")

        await h.callback("main_menu")
        await h.callback("my_announcements")
        deletes_before = len(h.deleted_ids())
        await h.callback(f"aw_preview:{draft_id}:{details.revision - 1}")

        self.assertTrue(h.screen_text(), "пользователь остался на пустом экране")
        self.assertEqual(
            len(h.deleted_ids()), deletes_before, "устаревший callback удалил сообщения"
        )
        self.assertTrue(
            cat.is_stale(h.answer_texts()[-1])
            or REMOVED_REFRESH not in h.screen_text(),
            f"нет безопасного ответа: {h.answer_texts()}",
        )
        _assert_no_stage_error(self, h, where="устаревший aw_preview")


# --- §11.3: мастер -----------------------------------------------------------


@unittest.skipIf(INTEGRATION_IMPORT_ERROR is not None, SKIP_REASON)
class WizardQuestionScreenTests(unittest.TestCase):
    """§3.1/§3.3/§3.4: два блока, одна подсказка, честный счётчик."""

    @scenario_test
    async def test_question_is_header_and_single_hint(self, h) -> None:
        place_id = await _place(h)
        await _open_place_questions(h, place_id)

        screen = h.screen_text()
        blocks = _blocks(screen)
        self.assertEqual(len(blocks), 2, f"экран не из двух блоков: {blocks!r}")
        self.assertEqual(
            screen.count("\n\n"), 1, "после шапки ровно одна пустая строка"
        )
        self.assertRegex(
            blocks[0], cat.question_re(1), f"шапка не по формату: {blocks[0]!r}"
        )
        self.assertRegex(
            blocks[0], re.compile(r"^🧩 .+ · 1/\d+$"), "формат `🧩 … · 1/N`"
        )
        self.assertTrue(blocks[1].strip(), "подсказка пустая")

        # §3.3: универсального дубля под индивидуальной подсказкой нет.
        self.assertNotIn(
            cat.text("wizard.hint.short_text"),
            screen,
            "дубль «Напишите короткий текст»",
        )
        self.assertNotIn(
            cat.fragment("wizard.hint.fallback"),
            screen,
            "универсальный fallback рядом с индивидуальной подсказкой",
        )
        for service_key in ("wizard.question.current_value", "wizard.question.skipped"):
            self.assertNotIn(
                cat.fragment(service_key), screen, "служебная строка состояния"
            )

    @scenario_test
    async def test_counter_matches_four_and_six_question_templates(self, h) -> None:
        place_id = await _place(h)
        for total in (4, 6):
            specs: list[TemplateSpec] = [
                (f"qx{total}_{index}", f"Поле {index}", "short_text", {})
                for index in range(1, total + 1)
            ]
            template_id, _revision, _bricks = _seed_personal_template(
                h,
                name=f"Сценарий {total}",
                specs=specs,
                content=" ".join(f"[{code}]" for code, _t, _v, _r in specs),
            )
            await _open_personal_questions(h, place_id, template_id)
            screens = await _walk_questions(h)
            self.assertEqual(
                [_step_total(screen) for screen in screens],
                [(step, total) for step in range(1, total + 1)],
                f"счётчик для {total} вопросов: {screens!r}",
            )
            await _finish_without_photos(h)

    @scenario_test
    async def test_skip_keeps_total_and_back_shows_step(self, h) -> None:
        place_id = await _place(h)
        specs: list[TemplateSpec] = [
            (f"qs{index}", f"Поле {index}", "short_text", {}) for index in range(1, 5)
        ]
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Пропуски и возврат",
            specs=specs,
            content=" ".join(f"[{code}]" for code, _t, _v, _r in specs),
        )
        await _open_personal_questions(h, place_id, template_id)

        await h.text("Первое")
        await h.text("Второе")
        self.assertEqual(_step_total(h.screen_text()), (3, 4), h.screen_text())

        await h.callback(
            next(
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("aw_qback")
            )
        )
        self.assertEqual(_step_total(h.screen_text()), (2, 4), "шаг после «Назад»")

        skip = next(
            (
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("aw_qskip")
            ),
            None,
        )
        self.assertIsNotNone(skip, f"нет «Пропустить»: {h.screen_buttons()}")
        await h.callback(skip)
        self.assertEqual(
            _step_total(h.screen_text()), (3, 4), "«Пропустить» сбил счётчик"
        )

        screens = await _walk_questions(h, prefer="skip")
        self.assertEqual(
            {total for _step, total in map(_step_total, screens)},
            {4},
            f"«Пропустить» изменил общее количество: {screens!r}",
        )


# --- §11.4: главный редактор -------------------------------------------------


@unittest.skipIf(INTEGRATION_IMPORT_ERROR is not None, SKIP_REASON)
class MainEditorContractTests(unittest.TestCase):
    """§2.5/§4: строки редактора, фактическое N фото и гейт доп. полей."""

    @scenario_test
    async def test_editor_rows_without_done_and_with_actual_photos(self, h) -> None:
        await h.text("/start")
        place_id = await _place(h)
        _seed_standard(h, place_id, content="**[название]**")
        await _open_place_questions(h, place_id)
        draft_id = await _deliver_with_photo(h)
        self.assertEqual(_details(h, draft_id).publication_photos, ("photo-261",))

        rows = _control_rows(h)
        photos_label = cat.text("buttons.finished.photos", count=1)
        instance_id = _instance_id(h)
        self.assertEqual(
            rows,
            [
                [
                    (DATA_LABEL, f"anp_data:{instance_id}"),
                    (photos_label, f"anp_photos:{instance_id}"),
                ],
                [(MENU_LABEL, f"anp_menu:{instance_id}")],
            ],
            f"строки редактора не по контракту: {rows}",
        )
        labels = [label for row in rows for label, _data in row]
        self.assertNotIn("Готово", labels, "кнопка «Готово» запрещена")
        self.assertNotIn(REMOVED_REFRESH, labels, "кнопка «Обновить» запрещена")
        callbacks = [data for _label, data in h.live_buttons() if data]
        self.assertFalse(
            [
                data
                for data in callbacks
                if data.startswith(("anp_ready", "aw_preview"))
            ],
            f"удалённые callback остались в чате: {callbacks}",
        )
        self.assertNotIn(EXTRAS_LABEL, labels, "расширенный вариант выключен")

    @scenario_test
    async def test_extras_appear_only_for_enabled_extended_template(self, h) -> None:
        await h.text("/start")
        place_id = await _place(h)
        _seed_standard(
            h,
            place_id,
            content="**[название]**",
            extended_content="**[название]**\n\nКуратор: [куратор]",
            extended_active=True,
        )
        await _open_place_questions(h, place_id)
        await _deliver_with_photo(h)

        rows = _control_rows(h)
        instance_id = _instance_id(h)
        self.assertEqual(
            rows[1],
            [(EXTRAS_LABEL, f"anp_extras:{instance_id}")],
            f"вторая строка редактора: {rows}",
        )
        self.assertEqual(rows[0][0][0], DATA_LABEL)
        self.assertEqual(rows[-1], [(MENU_LABEL, f"anp_menu:{instance_id}")])


# --- §11.5–§11.6: карточка и редактор шаблона --------------------------------


@unittest.skipIf(INTEGRATION_IMPORT_ERROR is not None, SKIP_REASON)
class TemplateCardAcceptanceTests(unittest.TestCase):
    """§6.2–6.4: выключенный/включённый вариант и переключение на месте."""

    @scenario_test
    async def test_off_card_hides_extended_text_and_technical_fields(self, h) -> None:
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)

        text = h.screen_text()
        cat.assert_shows(self, text, tpl.CARD_TITLE_KEY, place_name="Дом культуры")
        self.assertIn(tpl.SIMPLE_TITLE, text, "жирный подзаголовок «Простой шаблон»")
        for fragment in tpl.SIMPLE_ONLY:
            self.assertIn(fragment, text, "текст простого шаблона")
        for fragment in tpl.EXTENDED_ONLY:
            self.assertNotIn(fragment, text, "расширенный текст скрыт")
        for technical in ("Тип:", "Версия:"):
            self.assertNotIn(technical, text, "технические поля убраны")
        self.assertNotIn(tpl.EXTENDED_ON, text, "строки включения ещё нет")

        self.assertEqual(
            tpl._rows(h),
            [
                [
                    (tpl.BTN_EDIT, f"tp_edit:{template_id}"),
                    (tpl.BTN_ON, f"tp_ext_on:{template_id}"),
                ],
                [(tpl.BTN_BRICKS, f"tp_bricks:{template_id}")],
                [(tpl.BTN_RESET, f"tp_reset:{template_id}")],
                [(tpl.BTN_TO_PLACE, f"tp_back_place:{place_id}")],
            ],
            "кнопки выключенной карточки",
        )
        self.assertFalse(_callbacks(h, "tp_cond:"), "условные части у выключенного")
        self.assertNotIn(
            tpl.BTN_COND, [label for row in tpl._rows(h) for label, _ in row]
        )

    @scenario_test
    async def test_on_card_shows_status_and_toggle_edits_in_place(self, h) -> None:
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        extended_before = _extended_text(h, template_id)
        rules_before = {(r.section, r.code, r.fragment) for r in _rules(h, template_id)}
        self.assertTrue(rules_before, "у стандарта есть условные части")

        messages_before = _message_calls(h)
        screen_id_before = h.screen_message_id()
        await h.callback(f"tp_ext_on:{template_id}")

        self.assertTrue(_flag(h, template_id), "флаг не включён")
        self.assertEqual(_message_calls(h), messages_before, "создано новое сообщение")
        self.assertEqual(h.screen_message_id(), screen_id_before, "экран пересоздан")
        self.assertEqual(
            h.screen()["type"], "edit_message", "карточка не отредактирована"
        )

        text = h.screen_text()
        self.assertIn(tpl.EXTENDED_ON, text, "строка «Расширенный шаблон включён.»")
        for fragment in tpl.EXTENDED_ONLY:
            self.assertNotIn(fragment, text, "полный расширенный текст в карточке")
        for technical in ("Тип:", "Версия:"):
            self.assertNotIn(technical, text)

        self.assertEqual(
            tpl._rows(h),
            [
                [
                    (tpl.BTN_SIMPLE, f"tp_edit:{template_id}"),
                    (tpl.BTN_EXT, f"tp_edit_ext:{template_id}"),
                ],
                [(tpl.BTN_COND, f"tp_cond:{template_id}")],
                [(tpl.BTN_BRICKS, f"tp_bricks:{template_id}")],
                [
                    (tpl.BTN_RESET, f"tp_reset:{template_id}"),
                    (tpl.BTN_OFF, f"tp_ext_off:{template_id}"),
                ],
                [(tpl.BTN_TO_PLACE, f"tp_back_place:{place_id}")],
            ],
            "кнопки включённой карточки: выключение справа от восстановления",
        )

        # §6.4: вкл → выкл → вкл не теряет текст и условные части.
        for prefix in ("tp_ext_off", "tp_ext_on"):
            await h.callback(f"{prefix}:{template_id}")
        self.assertTrue(_flag(h, template_id), "повторное включение сбросило флаг")
        self.assertEqual(
            _extended_text(h, template_id),
            extended_before,
            "расширенный текст потерян при переключении",
        )
        self.assertEqual(
            {(r.section, r.code, r.fragment) for r in _rules(h, template_id)},
            rules_before,
            "условные части потеряны при переключении",
        )

    @scenario_test
    async def test_editor_has_explanation_help_and_list_without_clear(self, h) -> None:
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        await h.callback(f"tp_edit:{template_id}")

        text = h.screen_text()
        self.assertTrue(text.startswith(tpl.EDIT_TITLE), text[:80])
        self.assertNotIn(tpl.EDIT_TITLE_EXT, text, "заголовок другого варианта")
        self.assertIn(tpl.EDIT_CURRENT, text, "«Текущий текст:»")
        self.assertIn(tpl.EDIT_INSTRUCTION, text, "короткая инструкция")
        for fragment in tpl.SIMPLE_ONLY:
            self.assertIn(fragment, text, "сохранённый текст с переносами")
        self.assertEqual(
            tpl._rows(h),
            [
                [
                    (tpl.BTN_LIST, f"tp_bricks:{template_id}"),
                    (tpl.BTN_HELP, f"tp_help:{template_id}"),
                ],
                [(tpl.BTN_TO_TEMPLATE, f"tp_open:{template_id}")],
            ],
            "кнопки редактора",
        )
        labels = [label for row in tpl._rows(h) for label, _data in row]
        self.assertFalse([label for label in labels if "Очистить" in label])
        self.assertFalse(_callbacks(h, "tp_clear"), "кнопка очистки зарегистрирована")

        # §7.4: «Список кирпичиков» ведёт в редактор того же варианта.
        await h.callback(f"tp_bricks:{template_id}")
        self.assertTrue(h.screen_text(), "экран списка пуст")
        await h.callback("tp_back")
        self.assertTrue(
            h.screen_text().startswith(tpl.EDIT_TITLE),
            "«Назад» из списка не в тот же вариант",
        )

        # §7.5: справка «Как редактировать» и возврат в тот же вариант.
        await h.callback(f"tp_help:{template_id}")
        cat.assert_shows(self, h.screen_text(), "templates.help.title")
        cat.assert_shows(self, h.screen_text(), "templates.help.example")
        self.assertEqual(tpl._rows(h), [[(tpl.BTN_TO_EDITOR, "tp_back")]])
        await h.callback("tp_back")
        self.assertTrue(
            h.screen_text().startswith(tpl.EDIT_TITLE),
            "возврат не в тот же вариант шаблона",
        )

        # Устаревшая кнопка очистки безопасна и ничего не меняет.
        screen_before = h.screen_text()
        answers_before = len(h.answers())
        await h.callback("tp_clear")
        self.assertEqual(len(h.answers()), answers_before + 1, "ровно один ответ")
        cat.assert_stale(self, h.answer_texts()[-1])
        self.assertEqual(h.screen_text(), screen_before, "экран изменился")
        cat.assert_not_shows(self, h.screen_text(), "errors.internal")


# --- §11.7–§11.8: навигация и ссылка ----------------------------------------


@unittest.skipIf(INTEGRATION_IMPORT_ERROR is not None, SKIP_REASON)
class NavigationAndInviteTests(unittest.TestCase):
    """§5/§8: два экрана без «В меню» и настоящая ссылка организатору."""

    @scenario_test
    async def test_bricks_hub_has_single_back(self, h) -> None:
        invite._seed_bricks(h)
        await invite._open_hub(h)

        datas = invite._datas(h)
        self.assertNotIn("main_menu", datas, "кнопка «🏠 В меню» осталась")
        self.assertNotIn(("🏠 В меню", "main_menu"), invite._buttons(h))
        self.assertIn((invite.BACK_LABEL, "bk_back"), invite._buttons(h))

        await h.callback("bk_back")
        self.assertIn(
            cat.text("menu.manage"), h.screen_text(), "возврат в «Управление»"
        )

    @scenario_test
    async def test_invite_screen_has_no_menu_or_reissue(self, h) -> None:
        place_id = invite._seed_place(h)
        await invite._open_invite_screen(h, place_id)

        labels = invite._labels(h)
        self.assertNotIn("🏠 В меню", labels, "кнопка «В меню» на экране ссылки")
        self.assertFalse(
            [label for label in labels if REMOVED_REGENERATE in label],
            f"кнопка перевыпуска осталась: {labels}",
        )
        self.assertNotIn("adm_invite_regen:", " ".join(invite._datas(h)))
        self.assertEqual(
            labels,
            [cat.text("buttons.places.invite_copy"), invite.TO_CARD_LABEL],
            "ровно две кнопки: копирование и возврат к карточке",
        )

    @scenario_test
    async def test_copy_button_is_real_copy_text_with_bot_url(self, h) -> None:
        place_id = invite._seed_place(h)
        await invite._open_invite_screen(h, place_id)
        token = invite._token_from_screen(h)
        expected = f"https://t.me/{h.bot.username}?start={token}"

        self.assertIn(expected, h.screen_text(), "ссылка в тексте экрана")
        self.assertEqual(invite._copy_text(h), expected, "копируется не та ссылка")
        self.assertLessEqual(len(expected), 256, "лимит CopyTextButton 1..256")
        self.assertNotIn("samplePost_bot", h.screen_text())
        self.assertNotIn("lekalo_app", h.screen_text(), "хардкод username")

        record = h.screen()
        assert record is not None
        buttons = [
            button for row in record["reply_markup"].inline_keyboard for button in row
        ]
        copy_buttons = [button for button in buttons if button.copy_text is not None]
        self.assertEqual(len(copy_buttons), 1, "ровно одна кнопка копирования")
        # Копирование — клиентское действие: callback нет, поэтому нажатие не
        # может породить сообщение (проверяется структурно).
        self.assertIsNone(copy_buttons[0].callback_data, "у копирования есть callback")
        self.assertEqual(invite._token_from_screen(h), token, "ссылка перевыпущена")
        self.assertEqual(
            [data for _label, data in h.screen_buttons() if data],
            ["adm_invite_back"],
            "callback'и экрана ссылки изменились",
        )

    @scenario_test
    async def test_deep_link_opens_first_question_without_admin_rights(self, h) -> None:
        place_id = invite._seed_place(h)
        await invite._open_invite_screen(h, place_id)
        token = invite._token_from_screen(h)

        await h.text(f"/start {token}", user_id=INVITED_TG)

        text = h.screen_text()
        self.assertRegex(text, cat.question_re(1), f"первый вопрос не открыт: {text!r}")
        self.assertNotIn("Выберите площадку", text, "остался выбор площадки")
        self.assertIsNotNone(
            h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME),
            "конструктор не запущен",
        )

        with h.session_factory() as session:
            user = session.scalar(
                select(User).where(User.telegram_user_id == INVITED_TG)
            )
            self.assertIsNotNone(user, "приглашённый пользователь не создан")
            assert user is not None
            admin_rows = session.scalars(
                select(PlaceAdmin).where(PlaceAdmin.user_id == user.id)
            ).all()
            drafts = [
                draft.place_id
                for draft in list_announcement_drafts(
                    session, actor_telegram_user_id=INVITED_TG
                )
            ]
        self.assertEqual(admin_rows, [], "приглашённому выданы права администратора")
        self.assertEqual(drafts, [place_id], "анонс создан не для той площадки")


# --- §11.9: даты -------------------------------------------------------------


PRODUCTION_FORBIDDEN = ("frozen_today", "2026-11-02", "02.11.2026")


@unittest.skipIf(INTEGRATION_IMPORT_ERROR is not None, SKIP_REASON)
class WorkingClockTests(unittest.TestCase):
    """§9.1/§11.9: тестовых часов и фиксированных дат в рабочем коде нет."""

    def test_production_sources_have_no_frozen_dates(self) -> None:
        offenders: list[str] = []
        for path in sorted(APP_DIR.rglob("*.py")):
            relative = path.relative_to(APP_DIR).as_posix()
            if relative.startswith(("tests/", ".venv/", "logs/", "backups/")):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            offenders.extend(
                f"{relative}: {marker}"
                for marker in PRODUCTION_FORBIDDEN
                if marker in text
            )
        self.assertEqual(
            offenders, [], f"рабочий код содержит тестовые часы/даты: {offenders}"
        )

    def test_app_today_is_the_real_clock(self) -> None:
        """§11.9: `frozen_today` не влияет на рабочий бот — часы настоящие."""
        self.assertLessEqual(
            abs((app_today() - date.today()).days),
            1,
            f"рабочие часы подменены: app_today()={app_today()}",
        )


@unittest.skipIf(INTEGRATION_IMPORT_ERROR is not None, SKIP_REASON)
class CrossDayRenderAcceptance(_DatesCase):
    """
    §9.2/§9.4: один день операции на сохранение и рендер.

    Проверяется сквозной путь анонса (сохранение → рендер) с замороженными
    часами: смена суток, месяца и года не должна ломать сохранённые данные.
    """

    def test_saved_today_renders_after_midnight(self) -> None:
        from tests.integration_harness import frozen_today

        op_today = date(2026, 11, 10)
        with frozen_today(op_today.year, op_today.month, op_today.day):
            self._save(DATE_CODE, "10.11.2026", today=op_today)
            expected = self._preview(today=op_today)
        self.assertIn("10.11", expected, "рендер не показывает сохранённую дату")
        with frozen_today(2026, 11, 11):
            self.assertEqual(
                self._preview(today=op_today), expected, "дата операции потеряна"
            )
        self.assertEqual(self._stored_date(), "10.11.2026")

    def test_month_boundary_and_year_boundary(self) -> None:
        from tests.integration_harness import frozen_today

        cases = (
            (date(2027, 1, 31), "31.01.2027", date(2027, 2, 1)),
            (date(2026, 12, 31), "31.12.2026", date(2027, 1, 1)),
        )
        for save_day, value, next_day in cases:
            with frozen_today(save_day.year, save_day.month, save_day.day):
                self._save(DATE_CODE, value, today=save_day)
                expected = self._preview(today=save_day)
            day_month = ".".join(value.split(".")[:2])
            self.assertIn(day_month, expected, f"рендер границы {value}")
            with frozen_today(next_day.year, next_day.month, next_day.day):
                self.assertEqual(
                    self._preview(today=save_day),
                    expected,
                    f"переход {save_day} → {next_day} сломал рендер",
                )


# --- §11.10: антирегрессии ---------------------------------------------------


@unittest.skipIf(INTEGRATION_IMPORT_ERROR is not None, SKIP_REASON)
class AntiRegressionAcceptanceTests(unittest.TestCase):
    """§11.10: ранее работавшие функции не сломаны этапами A–E."""

    @scenario_test
    async def test_place_announcement_live_block_before_question(self, h) -> None:
        """Живой анонс приходит раньше вопроса, фото доводит до доставки."""
        await h.text("/start")
        place_id = await _place(h)
        _seed_standard(h, place_id, content="**[название]**")
        await _open_place_questions(h, place_id)

        live = [index for index, call in enumerate(h.calls) if is_live_record(call)]
        self.assertTrue(live, "живой блок анонса не создан")
        header_prefix = cat.fragment("wizard.question.header").strip()
        question_index = next(
            index
            for index, call in enumerate(h.calls)
            if header_prefix in str(call.get("text") or "")
        )
        self.assertLess(live[0], question_index, "вопрос пришёл раньше живого анонса")

        draft_id = await _deliver_with_photo(h, file_id="live-photo")
        details = _details(h, draft_id)
        self.assertEqual(details.publication_photos, ("live-photo",))
        self.assertTrue(details.publication_text, "текст анонса пуст")

    @scenario_test
    async def test_personal_template_and_conditional_parts_work(self, h) -> None:
        await h.text("/start")
        place_id = await _place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Личный",
            specs=[("pmain", "Основное", "short_text", {})],
            content="[pmain]",
        )
        await _open_personal_questions(h, place_id, template_id)
        self.assertRegex(h.screen_text(), cat.question_re(1, 1), "личный шаблон")
        draft_id, _screens = await _deliver(h)
        self.assertTrue(_details(h, draft_id).publication_text, "анонс пуст")

        # §6.3: условные части доступны включённому расширенному варианту.
        standard_place = _seed_place(h)
        await h.text("/start")
        standard_id = await _open_card(h, standard_place)
        await h.callback(f"tp_ext_on:{standard_id}")
        cond = _callbacks(h, "tp_cond:")
        self.assertTrue(cond, f"нет входа в условные части: {tpl._rows(h)}")
        await h.callback(cond[0])
        self.assertTrue(h.screen_text(), "экран условных частей пуст")
        cat.assert_not_shows(self, h.screen_text(), "errors.internal")

    @scenario_test
    async def test_calendar_quick_day_advances_question(self, h) -> None:
        """§11.10: быстрый день календаря отвечает на вопрос и ведёт дальше."""
        place_id = await _place(h)
        _seed_standard(h, place_id, content="[название]\n\n[дата]\n\n[описание]")
        await _open_place_questions(h, place_id)
        await h.text("Название")
        self.assertRegex(h.screen_text(), cat.question_re(2, 3), "не вопрос о дате")

        day = next(
            (
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("aw_calday:")
            ),
            None,
        )
        self.assertIsNotNone(day, f"календаря нет: {h.screen_buttons()}")
        await h.callback(day)
        self.assertRegex(
            h.screen_text(), cat.question_re(3, 3), "календарь не перевёл дальше"
        )

    @scenario_test
    async def test_report_inside_wizard_returns_same_question(self, h) -> None:
        place_id = await _place(h)
        await _open_place_questions(h, place_id)
        question = h.screen_text()

        await h.text("/report")
        cat.assert_shows(self, h.screen_text(), "report.screen.start")
        await h.callback("rp_cancel")
        self.assertEqual(h.screen_text(), question, "вопрос не восстановлен")

    @scenario_test
    async def test_saved_draft_keeps_values_and_becomes_copy(self, h) -> None:
        """Сохранение черновика не теряет значения; выбор даёт готовую копию."""
        from tests.test_poster2_delivery import _to_photo_step

        await h.text("/start")
        draft = await _to_photo_step(h)
        # Текст на шаге фото переводит к первичному обзору (он же завершает мастер).
        await h.text("Текст сохранённого анонса")
        save = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("aw_save")
        ]
        self.assertTrue(save, f"нет сохранения черновика: {h.screen_buttons()}")
        await h.callback(save[0])

        details = _details(h, draft.id)
        self.assertIsNone(
            details.publication_status, "сохранение превратилось в доставку"
        )
        values_before = [
            (value.brick_id, value.status, value.payload) for value in details.values
        ]
        self.assertTrue(values_before, "значения полей потеряны")

        # §2.2: выбор этого черновика из списка даёт готовую копию.
        await _reopen_from_list(h, draft.id)
        self.assertTrue(h.posts(), "копия сохранённого черновика не отправлена")
        _assert_no_stage_error(self, h, where="сохранённый черновик")
        self.assertEqual(
            [
                (value.brick_id, value.status, value.payload)
                for value in _details(h, draft.id).values
            ],
            values_before,
            "значения черновика изменились после отправки копии",
        )
