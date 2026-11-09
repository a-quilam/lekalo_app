# tests/test_poster2_humor.py
"""
Poster 2.0 S5 п.7 (§5.7): редкие юмористические вариации интерфейса.

Две группы проверок, как требует план S5:

* **Обычные варианты** — юмор выключен, не выпал по частоте или экран не
  входит в белый список: интерфейс ведёт себя как без юмора.
* **Шуточные варианты** — вариация выпала: меняется только текст, а
  `callback_data`, порядок кнопок и состояние черновика совпадают.

Модуль `utils/ui_humor` чистый: без Telegram-типов, без сети, без `random()`.
Интеграционная часть идёт через настоящее приложение, FakeBot и временную
SQLite (не реальная `post_bot.db`, не сеть Telegram).
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from models import Brick
from tests import ui_texts_expect as cat

try:
    from announcement_store import (
        get_announcement_draft,
        list_announcement_drafts,
    )
    from brick_store import create_personal_brick
    from template_store import create_personal_template
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        is_live_record,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import ui_humor, ui_texts

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


JOKE_PROBE = "ШУТКА-ПРОБА-7f3a: сегодня без пафоса."

# Каталог фраз юмора (Poster 2.4 P4): отдельный файл, не ui_texts.yaml.
CATALOG_PATH = Path(__file__).resolve().parent.parent / "config" / "humor_phrases.yaml"


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


def _without_notice(text: str) -> str:
    """
    Убирает одноразовую плашку «значение пропущено» — она не часть экрана.

    Подпись плашки задаёт каталог (`wizard.skip_question.znachenie_ostavleno`),
    поэтому сравнение идёт по ключу, а не по литералу. Плашки «✓ {title}»
    после сохранения в интерфейсе больше нет (§16 финального задания).
    """
    notice_keys = ("wizard.skip_question.znachenie_ostavleno",)
    blocks = text.split("\n\n")
    return "\n\n".join(
        block
        for block in blocks
        if not any(cat.shows(block.strip(), key) for key in notice_keys)
    )


def _callback(h, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(f"нет callback {prefix!r}: {h.screen_buttons()}")
    return found[0]


def _seed_template(h, *, bricks: int = 2) -> int:
    """Личный шаблон из `bricks` кирпичиков с описаниями для автора."""
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="humor")
        codes = ["ttl", "dsc", "ext"][:bricks]
        idiom = {
            "ttl": (
                "Заголовок",
                "short_text",
                "Имя и фамилия ведущего, без должности.",
            ),
            "dsc": ("Описание", "long_text", "Что будет & чего не будет."),
            "ext": ("Дополнительно", "short_text", "Любая короткая ремарка."),
        }
        for code in codes:
            title, value_type, description = idiom[code]
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code=code,
                title=title,
                value_type=value_type,
                rules={},
                description=description,
            )
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name="Юмор",
            content="\n".join(
                p for p in ("", "\n".join(f"[{code}]" for code in codes), "") if p
            ),
        )
        session.commit()
        return template.template_id


def _latest_draft(h):
    with h.session_factory() as session:
        drafts = list_announcement_drafts(
            session, actor_telegram_user_id=DEFAULT_USER_ID
        )
    if not drafts:
        raise AssertionError("черновик не создан")
    return drafts[0]


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session, actor_telegram_user_id=DEFAULT_USER_ID, draft_id=draft_id
        )


def _live_records(h) -> list[dict]:
    """Сообщения/правки живого блока: HTML-предпросмотр без клавиатуры (U1b)."""
    return [
        call
        for call in h.calls
        if call.get("reply_markup") is None and is_live_record(call)
    ]


def _live_text(h) -> str:
    texts = [
        str(call.get("text") or "")
        for call in _live_records(h)
        if call.get("text") is not None
    ]
    if not texts:
        raise AssertionError("живой блок ещё не отправлен")
    return texts[-1]


class HumorCoreTests(unittest.TestCase):
    """Обычные варианты: детерминизм, частота, белый список, флаг."""

    def test_jokes_are_a_fixed_constant_without_llm(self) -> None:
        self.assertGreaterEqual(len(ui_humor.HUMOR_JOKES), 3)
        self.assertTrue(
            all(isinstance(item, str) and item for item in ui_humor.HUMOR_JOKES)
        )
        # Шутка пустого анонса берётся из того же набора (S5 п.7).
        self.assertEqual(
            set(ui_humor.EMPTY_ANNOUNCEMENT_JOKES), set(ui_humor.HUMOR_JOKES)
        )

    def test_pick_variant_is_deterministic_for_one_key(self) -> None:
        keys = ["7:1:1:5", "7:1:1:6", "7:2:3:11", "8:1:1:1"]
        first = {key: ui_humor.pick_variant(key) for key in keys}
        for _ in range(50):
            self.assertEqual({key: ui_humor.pick_variant(key) for key in keys}, first)
        # Результат всегда либо None, либо фраза из набора.
        for value in first.values():
            self.assertTrue(value is None or value in ui_humor.HUMOR_JOKES)

    def test_pick_variant_uses_hash_not_process_hash(self) -> None:
        """Ключ не зависит от PYTHONHASHSEED: sha256 стабилен между процессами."""
        key = "7:1:1:5"
        expected = ui_humor.pick_variant(key)
        with patch.dict("os.environ", {"PYTHONHASHSEED": "12345"}):
            self.assertEqual(ui_humor.pick_variant(key), expected)

    def test_variant_rate_is_about_one_percent(self) -> None:
        keys = [f"{7 + index}:1:1:{index}" for index in range(20_000)]
        hits = [key for key in keys if ui_humor.pick_variant(key) is not None]
        share = len(hits) / len(keys)
        self.assertGreaterEqual(share, 0.004, f"юмор выпадает слишком редко: {share}")
        self.assertLessEqual(share, 0.016, f"юмор выпадает слишком часто: {share}")

    def test_hits_use_the_whole_joke_set(self) -> None:
        keys = [f"{7 + index}:1:1:{index}" for index in range(20_000)]
        found = {ui_humor.pick_variant(key) for key in keys}
        found.discard(None)
        self.assertEqual(found, set(ui_humor.HUMOR_JOKES))

    def test_whitelist_allows_only_question_and_button(self) -> None:
        self.assertTrue(ui_humor.humor_allowed(ui_humor.SCREEN_QUESTION))
        self.assertTrue(ui_humor.humor_allowed(ui_humor.SCREEN_BUTTON))
        for kind in (
            ui_humor.SCREEN_ERROR,
            ui_humor.SCREEN_RIGHTS,
            ui_humor.SCREEN_LIMITS,
            ui_humor.SCREEN_DATA_LOSS,
            "unknown",
            "",
        ):
            self.assertFalse(ui_humor.humor_allowed(kind), kind)

    def test_forbidden_screens_never_joke_even_forced(self) -> None:
        keys = [f"{7 + index}:1:1:{index}" for index in range(5_000)]
        for kind in (
            ui_humor.SCREEN_ERROR,
            ui_humor.SCREEN_RIGHTS,
            ui_humor.SCREEN_LIMITS,
            ui_humor.SCREEN_DATA_LOSS,
        ):
            for key in keys:
                self.assertIsNone(
                    ui_humor.pick_variant(key, screen_kind=kind, forced=True),
                    f"{kind} не должен шутить ({key})",
                )

    def test_forced_show_ignores_rate_but_keeps_the_set(self) -> None:
        jokes = {
            ui_humor.pick_variant(
                f"empty:{index}",
                screen_kind=ui_humor.SCREEN_QUESTION,
                forced=True,
            )
            for index in range(500)
        }
        self.assertNotIn(None, jokes, "обязательный показ не должен пропадать")
        self.assertTrue(jokes <= set(ui_humor.HUMOR_JOKES))

    def test_flag_off_disables_everything_including_empty_announce(self) -> None:
        with patch("config.UI_HUMOR_ENABLED", False):
            self.assertFalse(ui_humor.humor_enabled())
            for index in range(2_000):
                self.assertIsNone(ui_humor.pick_variant(f"{7 + index}:1:1:{index}"))
            self.assertIsNone(
                ui_humor.pick_variant("empty:1", forced=True),
                "флаг главнее обязательного показа",
            )
            self.assertIsNone(ui_humor.empty_announcement_joke())
            self.assertIsNone(
                ui_humor.get_humor_line(announcement_is_empty=True),
                "пустой анонс без шутки при выключенном юморе",
            )

    def test_flag_on_shows_empty_announce_joke_and_hides_it_when_filled(self) -> None:
        with patch("config.UI_HUMOR_ENABLED", True):
            self.assertTrue(ui_humor.humor_enabled())
            joke = ui_humor.get_humor_line(announcement_is_empty=True)
            self.assertIn(joke, ui_humor.HUMOR_JOKES)
            self.assertEqual(ui_humor.get_humor_line(announcement_is_empty=True), joke)
            self.assertIsNone(ui_humor.get_humor_line(announcement_is_empty=False))

    def test_config_parses_env_flag_with_default_on(self) -> None:
        import config

        self.assertTrue(config.parse_humor_enabled(None))
        self.assertTrue(config.parse_humor_enabled("1"))
        self.assertTrue(config.parse_humor_enabled("да"))
        for raw in ("0", "false", "NO", " off ", "нет", "выкл"):
            self.assertFalse(config.parse_humor_enabled(raw), raw)


class HumorPhrasesCatalogTests(unittest.TestCase):
    """
    Poster 2.4 (P4): фразы живут в редактируемом YAML, механика прежняя.

    Каталог `config/humor_phrases.yaml` — отдельный файл (не ui_texts.yaml),
    ключ `humor.phrases` со списком строк. Любая проблема файла не ломает
    импорт: действует вшитый набор `DEFAULT_JOKES`.
    """

    def tearDown(self) -> None:
        # Возвращаем рабочий каталог: остальные тесты модуля смотрят на набор.
        ui_humor.reload_phrases()

    def _write(self, tmp: str, text: str):
        path = Path(tmp) / "humor_phrases.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_catalog_file_is_valid_and_used(self) -> None:
        report = ui_humor.reload_phrases(CATALOG_PATH)
        self.assertTrue(report.ok, report.errors)
        self.assertFalse(report.used_defaults)
        self.assertGreaterEqual(report.phrases, 3)
        self.assertEqual(len(ui_humor.HUMOR_JOKES), report.phrases)
        # Отдельный каталог: юмор не добавлен в ui_texts.yaml.
        self.assertNotIn("humor.phrases", ui_texts.DEFAULTS)

    def test_builtin_defaults_are_the_historical_set(self) -> None:
        """
        Вшитая страховка не должна «уезжать» вслед за правками файла.

        Владелец правит `humor_phrases.yaml` свободно; `DEFAULT_JOKES`
        остаётся прежним набором S5 — это последний рубеж при битом файле.
        """
        self.assertEqual(len(ui_humor.DEFAULT_JOKES), 8)
        self.assertTrue(
            all(isinstance(item, str) and item for item in ui_humor.DEFAULT_JOKES)
        )
        self.assertEqual(len(set(ui_humor.DEFAULT_JOKES)), 8)

    def test_edited_catalog_is_used_not_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                'humor:\n  phrases:\n    - "Своя фраза раз"\n'
                '    - "Своя фраза два"\n',
            )
            report = ui_humor.reload_phrases(path)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(ui_humor.HUMOR_JOKES, ("Своя фраза раз", "Своя фраза два"))
        # Механика не изменилась: шутка выбирается только из набора файла.
        value = ui_humor.pick_variant("empty:1", forced=True)
        self.assertIn(value, ui_humor.HUMOR_JOKES)

    def test_flat_key_form_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, '"humor.phrases": ["Фраза из плоского ключа"]\n')
            report = ui_humor.reload_phrases(path)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(ui_humor.HUMOR_JOKES, ("Фраза из плоского ключа",))

    def test_broken_yaml_falls_back_to_defaults_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "humor: [не список фраз\n")
            with self.assertLogs("utils.ui_humor", level=logging.WARNING):
                report = ui_humor.reload_phrases(path)
        self.assertFalse(report.ok)
        self.assertTrue(report.used_defaults)
        self.assertEqual(ui_humor.HUMOR_JOKES, ui_humor.DEFAULT_JOKES)
        self.assertIsNotNone(ui_humor.empty_announcement_joke())

    def test_missing_file_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "no_such.yaml"
            with self.assertLogs("utils.ui_humor", level=logging.WARNING):
                report = ui_humor.reload_phrases(path)
        self.assertFalse(report.ok)
        self.assertEqual(ui_humor.HUMOR_JOKES, ui_humor.DEFAULT_JOKES)

    def test_empty_list_and_missing_key_fall_back_to_defaults(self) -> None:
        cases = {
            "пустой список": "humor:\n  phrases: []\n",
            "нет ключа": "humor:\n  other: 1\n",
            "не список": 'humor:\n  phrases: "одна фраза"\n',
            "пустая строка": 'humor:\n  phrases: ["   "]\n',
            "не строка": "humor:\n  phrases: [42]\n",
        }
        for name, text in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                path = self._write(tmp, text)
                with self.assertLogs("utils.ui_humor", level=logging.WARNING):
                    report = ui_humor.reload_phrases(path)
                self.assertFalse(report.ok, name)
                self.assertEqual(ui_humor.HUMOR_JOKES, ui_humor.DEFAULT_JOKES, name)

    def test_joke_with_placeholder_is_rejected(self) -> None:
        """Шутка — не шаблон: подстановка в ней не предусмотрена."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, 'humor:\n  phrases: ["Привет, {name}!"]\n')
            with self.assertLogs("utils.ui_humor", level=logging.WARNING):
                report = ui_humor.reload_phrases(path)
        self.assertFalse(report.ok)
        self.assertEqual(ui_humor.HUMOR_JOKES, ui_humor.DEFAULT_JOKES)

    def test_joke_with_markup_chars_is_rejected(self) -> None:
        """
        D1: символы `<`, `>` и `&` в фразе запрещены каталогом.

        Экран уходит с `parse_mode=HTML`, поэтому даже при экранировании на
        стороне рендера такой файл не применяется: бот работает на вшитых
        фразах, а владелец видит причину в логе.
        """
        for char in ("<", ">", "&"):
            with self.subTest(char), tempfile.TemporaryDirectory() as tmp:
                path = self._write(
                    tmp, f'humor:\n  phrases:\n    - "Многоточие {char} и точка"\n'
                )
                with self.assertLogs("utils.ui_humor", level=logging.WARNING) as logs:
                    report = ui_humor.reload_phrases(path)
                self.assertFalse(report.ok, char)
                self.assertTrue(report.used_defaults, char)
                self.assertEqual(ui_humor.HUMOR_JOKES, ui_humor.DEFAULT_JOKES, char)
                self.assertTrue(
                    any("разметки" in error for error in report.errors),
                    f"нет понятной причины: {report.errors}",
                )
                self.assertTrue(
                    any("разметки" in line for line in logs.output),
                    "причина должна попадать в WARNING",
                )
                # Бот жив: шутка пустого анонса есть и берётся из дефолтов.
                self.assertIn(
                    ui_humor.get_humor_line(announcement_is_empty=True),
                    ui_humor.DEFAULT_JOKES,
                )

    def test_yaml_jokes_still_never_reach_forbidden_screens(self) -> None:
        """Смена источника фраз не открывает запрещённые экраны."""
        for kind in ui_humor.FORBIDDEN_SCREEN_KINDS:
            for index in range(500):
                self.assertIsNone(
                    ui_humor.pick_variant(f"k{index}", screen_kind=kind, forced=True),
                    kind,
                )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HumorQuestionScreenTests(unittest.TestCase):
    """Шуточные варианты: меняется только текст экрана вопроса."""

    @scenario_test
    async def test_joke_changes_text_only_not_keyboard_or_draft(self, h):
        template_id = _seed_template(h)
        # Ключ шутки содержит brick_id второго вопроса. Он зависит от числа
        # системных кирпичиков (U1a добавила `[город]`), поэтому берём его из
        # базы, а не хардкодом.
        with h.session_factory() as session:
            second_id = session.scalar(
                select(Brick.id).where(Brick.code == "dsc", Brick.scope == "personal")
            )
        assert second_id is not None, "личный кирпичик второго вопроса не создан"

        await h.text("/start")

        def fake_pick(key, **kwargs):
            # Шутим только на втором вопросе, чтобы сравнить один и тот же
            # экран в одном и том же состоянии.
            return JOKE_PROBE if f":{second_id}" in key else None

        with patch("utils.ui_humor.pick_variant", side_effect=fake_pick):
            await h.callback(f"tp_pick:{template_id}")
            self.assertNotIn(JOKE_PROBE, h.screen_text())
            await h.text("Заголовок анонса")
            self.assertRegex(h.screen_text(), cat.question_re(2, 2))
            joked_text = h.screen_text()
            joked_buttons = h.screen_buttons()
            self.assertIn(JOKE_PROBE, joked_text)

            # Тот же второй вопрос без шутки: назад и снова вперёд.
            # Poster 2.4 (P2): кнопки «➡️ Далее» больше нет — заполненное
            # поле перелистывается «⏭ Пропустить», значение сохраняется.
            with patch("utils.ui_humor.pick_variant", return_value=None):
                await h.callback(_callback(h, "aw_qback"))
                await h.callback(_callback(h, "aw_qskip"))
                plain_text = h.screen_text()
                plain_buttons = h.screen_buttons()
        after_plain = _details(h, _latest_draft(h).draft_id)
        self.assertNotIn(JOKE_PROBE, plain_text)
        self.assertRegex(plain_text, cat.question_re(2, 2))
        # Описание кирпичика видно на экране вопроса (S5 п.5).
        self.assertIn("Что будет &amp; чего не будет", plain_text)

        self.assertEqual(
            [data for _label, data in joked_buttons],
            [data for _label, data in plain_buttons],
            "шутка не меняет callback_data и порядок кнопок",
        )
        self.assertEqual(
            [label for label, _data in joked_buttons],
            [label for label, _data in plain_buttons],
            "шутка не меняет подписи кнопок",
        )
        # Текст отличается только добавленной шуткой: служебная строка
        # «сохранено» живёт один показ и сравнивается отдельно.
        self.assertEqual(
            _without_notice(joked_text.replace(f"\n\n{JOKE_PROBE}", "")),
            _without_notice(plain_text),
        )

        # Состояние черновика шуткой не затронуто: до и после показа с
        # шуткой ревизия и значения совпадают.
        draft = _latest_draft(h)
        details = _details(h, draft.draft_id)
        self.assertEqual(
            [value.payload for value in details.values],
            [{"text": "Заголовок анонса"}],
        )
        self.assertEqual(details.revision, after_plain.revision)
        self.assertEqual(
            [(value.brick_id, value.status) for value in details.values],
            [(value.brick_id, value.status) for value in after_plain.values],
        )

    @scenario_test
    async def test_same_screen_keeps_the_same_text_after_redraw(self, h):
        template_id = _seed_template(h)
        keys: list[str] = []

        def fake_pick(key, **kwargs):
            keys.append(key)
            return JOKE_PROBE

        await h.text("/start")
        with patch("utils.ui_humor.pick_variant", side_effect=fake_pick):
            await h.callback(f"tp_pick:{template_id}")
            await h.text("Заголовок анонса")
            second_first = h.screen_text()
            # «◀️ Назад» и «⏭ Пропустить» (P2: вместо «➡️ Далее»)
            # перерисовывают тот же вопрос без изменения данных: ключ и
            # текст обязаны совпасть.
            await h.callback(_callback(h, "aw_qback"))
            self.assertRegex(h.screen_text(), cat.question_re(1, 2))
            await h.callback(_callback(h, "aw_qskip"))
            second_again = h.screen_text()

        self.assertGreaterEqual(len(keys), 4)
        self.assertEqual(keys[1], keys[3], "ключ одного экрана стабилен")
        self.assertEqual(
            _without_notice(second_again),
            _without_notice(second_first),
            "текст не «прыгает» при перерисовке",
        )

    @scenario_test
    async def test_error_screen_has_no_joke_ever(self, h):
        """Белый список: экран ошибки не шутит, даже когда юмор «выпал»."""
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="humor")
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="dt",
                title="Дата",
                value_type="date",
                rules={},
            )
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="ttl",
                title="Заголовок",
                value_type="short_text",
                rules={},
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Ошибка",
                content="\n".join(p for p in ("", "[ttl]\n[dt]", "") if p),
            )
            session.commit()
            template_id = template.template_id

        def fake_pick(key, **kwargs):
            if kwargs.get("screen_kind") == ui_humor.SCREEN_ERROR:
                return None
            return JOKE_PROBE

        await h.text("/start")
        with patch("utils.ui_humor.pick_variant", side_effect=fake_pick):
            await h.callback(f"tp_pick:{template_id}")
            self.assertIn(JOKE_PROBE, h.screen_text())
            await h.text("Заголовок анонса")
            self.assertRegex(h.screen_text(), cat.question_re(2, 2))
            # Невалидная дата: экран диагностики, юмор запрещён.
            await h.text("не дата вовсе")
            error_text = h.screen_text()
        self.assertIn("⚠️", error_text)
        self.assertNotIn(JOKE_PROBE, error_text)

    @scenario_test
    async def test_joke_never_reaches_live_block_or_publication(self, h):
        template_id = _seed_template(h, bricks=1)
        await h.text("/start")
        with patch("utils.ui_humor.pick_variant", return_value=JOKE_PROBE):
            await h.callback(f"tp_pick:{template_id}")
            self.assertIn(JOKE_PROBE, h.screen_text())
            await h.callback(_callback(h, "aw_qskip"))
            await h.callback(_callback(h, "aw_photo_none"))

        live = _live_text(h)
        self.assertNotIn(JOKE_PROBE, live)
        draft = _details(h, _latest_draft(h).draft_id)
        self.assertNotIn(JOKE_PROBE, draft.publication_text or "")
        for text in h.posts():
            self.assertNotIn(JOKE_PROBE, text or "")

    @scenario_test
    async def test_flag_off_removes_joke_and_empty_announce_line(self, h):
        template_id = _seed_template(h, bricks=1)
        await h.text("/start")
        with patch("config.UI_HUMOR_ENABLED", False):
            await h.callback(f"tp_pick:{template_id}")
            question_screen = h.screen_text()
            await h.callback(_callback(h, "aw_qskip"))
            empty_screen = h.screen_text()
        for joke in ui_humor.HUMOR_JOKES:
            self.assertNotIn(joke, question_screen, "флаг выключает и вопросы")
            self.assertNotIn(joke, empty_screen, "флаг главнее пустого анонса")
        # Экран вопроса остаётся рабочим: юмор не влияет на смысл.
        self.assertRegex(question_screen, cat.question_re(1, 1))
        self.assertIn("Имя и фамилия ведущего", question_screen)
        self.assertIn(cat.text("wizard.photos.title"), empty_screen)
        for joke in ui_humor.HUMOR_JOKES:
            self.assertNotIn(joke, empty_screen, "при выключенном флаге шуток нет")

    @scenario_test
    async def test_description_is_escaped_on_the_question_screen(self, h):
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="humor")
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="ttl",
                title="Ведущий",
                value_type="short_text",
                rules={},
                description='<b>Имя</b> & "без должности"',
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Экранирование",
                content="\n".join(p for p in ("", "[ttl]", "") if p),
            )
            session.commit()
            template_id = template.template_id
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        text = h.screen_text()
        self.assertIn("&lt;b&gt;Имя&lt;/b&gt; &amp;", text)
        self.assertNotIn("<b>Имя</b>", text)


# «Злая» фраза для D1: незакрытый тег и амперсанд. В сыром виде такая
# строка ломает parse_mode=HTML, поэтому на экране она обязана быть
# экранирована. Валидатор каталога её тоже отвергает (см. выше), но здесь
# набор подставляется напрямую — проверяется второй слой защиты.
EVIL_JOKE = "Шутка & <b>без закрытия"
ESCAPED_EVIL_JOKE = "Шутка &amp; &lt;b&gt;без закрытия"
INTERNAL_ERROR_FRAGMENT = "Что-то пошло не так"


class _BalanceChecker(HTMLParser):
    """Собирает незакрытые/лишние теги — именно это Telegram и отвергает."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001, ANN201
        self.stack.append(tag)

    def handle_endtag(self, tag):  # noqa: ANN001, ANN201
        if not self.stack:
            self.problems.append(f"лишний </{tag}>")
            return
        if self.stack[-1] != tag:
            self.problems.append(f"нарушен порядок: <{self.stack[-1]}> и </{tag}>")
            return
        self.stack.pop()


def _assert_html_balanced(case: unittest.TestCase, html: str) -> None:
    checker = _BalanceChecker()
    checker.feed(html)
    checker.close()
    case.assertEqual(checker.problems, [], f"HTML сломан: {html!r}")
    case.assertEqual(checker.stack, [], f"незакрытые теги: {html!r}")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HumorEscapingTests(unittest.TestCase):
    """
    D1: фраза юмора — обычный текст, даже если она попала в набор в обход
    валидатора каталога.

    Сценарий дефекта: `/start` → `tp_pick` → «⏭ Пропустить» на вопросе без
    значения → шаг фото. Экран уходит с `parse_mode=HTML`, и неэкранированная
    фраза ломала разбор: Telegram не отправлял экран, пользователь видел
    «Что-то пошло не так» без кнопок шага.
    """

    @scenario_test
    async def test_evil_joke_is_escaped_on_photo_step_and_preview(self, h):
        template_id = _seed_template(h, bricks=1)
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        # Пустое значение + «Пропустить»: анонс без текста, дальше шаг фото.
        with patch.object(ui_humor, "HUMOR_JOKES", (EVIL_JOKE,)):
            await h.callback(_callback(h, "aw_qskip"))
            photo_text = h.screen_text()
            photo_buttons = h.screen_buttons()

            self.assertIn(ESCAPED_EVIL_JOKE, photo_text, photo_text)
            self.assertNotIn(EVIL_JOKE, photo_text, "фраза не экранирована")
            cat.assert_not_shows(self, photo_text, "errors.internal")
            _assert_html_balanced(self, photo_text)
            callbacks = [data for _label, data in photo_buttons if data]
            self.assertTrue(
                any(data.startswith("aw_photo_none") for data in callbacks),
                f"кнопка «Пропустить» шага фото потеряна: {photo_buttons}",
            )
            self.assertTrue(
                any(data.startswith("aw_photo_back") for data in callbacks),
                f"кнопка «Назад» шага фото потеряна: {photo_buttons}",
            )
            self.assertIn(cat.text("wizard.photos.title"), photo_text)

            # Тот же путь дальше: служебный экран обзора пустого анонса
            # (второй потребитель `get_humor_line`) тоже экранирует фразу.
            draft = _latest_draft(h)
            await h.callback(f"aw_preview:{draft.draft_id}:{draft.revision}")
            preview_text = h.screen_text()

        self.assertIn(ESCAPED_EVIL_JOKE, preview_text, preview_text)
        self.assertNotIn(EVIL_JOKE, preview_text, "фраза не экранирована")
        cat.assert_not_shows(self, preview_text, "errors.internal")
        _assert_html_balanced(self, preview_text)
        self.assertTrue(h.screen_buttons(), "экран обзора остался без кнопок")

    @scenario_test
    async def test_catalog_with_evil_phrase_keeps_photo_step_alive(self, h):
        """
        Ровно репро D1: в каталоге фраза с `&`/`<b>`; валидатор её
        отклоняет, бот работает на встроенных фразах, и экран шага фото
        доходит до пользователя с шуткой и кнопками.
        """
        template_id = _seed_template(h, bricks=1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "humor_phrases.yaml"
            path.write_text(
                'humor:\n  phrases:\n    - "Шутка & <b>без закрытия"\n',
                encoding="utf-8",
            )
            with (
                patch.dict("os.environ", {ui_humor.PATH_ENV_VARIABLE: str(path)}),
                self.assertLogs("utils.ui_humor", level=logging.WARNING),
            ):
                report = ui_humor.reload_phrases()
                self.assertFalse(report.ok)
                self.assertEqual(ui_humor.HUMOR_JOKES, ui_humor.DEFAULT_JOKES)

                await h.text("/start")
                await h.callback(f"tp_pick:{template_id}")
                await h.callback(_callback(h, "aw_qskip"))
                photo_text = h.screen_text()
                photo_buttons = h.screen_buttons()

        cat.assert_not_shows(self, photo_text, "errors.internal")
        self.assertNotIn(EVIL_JOKE, photo_text, "злая фраза не должна дойти")
        _assert_html_balanced(self, photo_text)
        self.assertIn(cat.text("wizard.photos.title"), photo_text)
        default_joke = next(
            joke for joke in ui_humor.DEFAULT_JOKES if joke in photo_text
        )
        self.assertTrue(default_joke)
        callbacks = [data for _label, data in photo_buttons if data]
        self.assertTrue(
            any(data.startswith("aw_photo_none") for data in callbacks),
            f"кнопка «Пропустить» шага фото потеряна: {photo_buttons}",
        )


if __name__ == "__main__":
    unittest.main()
