# tests/test_stage262_place_emoji.py
"""
Poster 2.6.2 §5.1–5.2: случайный эмодзи новых мест и редактор эмодзи.

Контракт:

1. НОВОЕ место получает эмодзи из набора `PLACE_EMOJI_CHOICES` (23 символа) и
   сохраняет его в `places.emoji`;
2. эмодзи хранится, а не генерируется при рендере: два показа карточки дают
   одно и то же значение;
3. существующие места свои эмодзи сохраняют (бэкфилл `🚀` и всё, что записано
   ранее, не переписывается);
4. пустое/NULL значение не ломает рендер — подставляется `🚀`;
5. кнопка «🎨 Изменить эмодзи» видна управляющему местом и не видна
   постороннему;
6. пикер: 5 эмодзи на страницу, 23 → 5 страниц, навигация листает набор;
7. выбор эмодзи сохраняет его в БД и сразу виден в карточке;
8. «🎲 Случайный эмодзи» сохраняет значение из набора;
9. «◀️ Назад» из пикера возвращает карточку без изменений;
10. подделанный callback с чужим `place_id` и не-админ получают отказ;
11. повторное открытие и повторный выбор безвредны;
12. выбранный эмодзи виден в публичной карточке места.

Все сценарии идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (`tests.integration_harness`); рабочая база, `.env` и сеть
Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage262_place_emoji -v
"""

from __future__ import annotations

import unittest

from sqlalchemy import text

from place_store import (
    PLACE_EMOJI_CHOICES,
    PLACE_EMOJI_DEFAULT,
    PLACE_EMOJI_PAGE_SIZE,
    place_emoji,
)
from tests import ui_texts_expect as cat
from tests.integration_harness import (
    HARNESS_IMPORT_ERROR,
    Harness,
    scenario_test,
)

ADMIN_TG = 2_622_201
STRANGER_TG = 2_622_202
OTHER_ADMIN_TG = 2_622_203


# --- помощники ---------------------------------------------------------------


def _rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _callbacks(h: Harness) -> list[str]:
    return [data for _label, data in h.screen_buttons() if data is not None]


def _labels(h: Harness) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


INTERNAL_FRAGMENT = "Что-то пошло не так"


def _internal_errors(h: Harness) -> list[str]:
    """Экраны с внутренней ошибкой (после шага их быть не должно)."""
    return [text for text in h.screen_texts() if INTERNAL_FRAGMENT in text]


def _last_alert(h: Harness) -> str:
    answers = h.answers()
    if not answers:
        return ""
    record = answers[-1]
    args = tuple(record.get("args") or ())
    if args and isinstance(args[0], str):
        return args[0]
    return str(record.get("text") or "")


def _make_place(
    h: Harness,
    name: str = "Дом культуры",
    *,
    admin_tg: int | None = ADMIN_TG,
) -> int:
    """Место со стандартным шаблоном; `admin_tg=None` — без администратора."""
    from models import PlaceAdmin, utcnow
    from place_store import create_place
    from user_store import get_or_create_user

    with h.session_factory() as session:
        user = None
        if admin_tg is not None:
            user = get_or_create_user(
                session, admin_tg, username=f"pe{admin_tg}", full_name="Админ места"
            )
        place = create_place(
            session,
            name,
            city="Город эмодзи",
            created_by_user_id=user.id if user is not None else None,
        )
        if user is not None:
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=user.id,
                    assigned_at=utcnow(),
                )
            )
        session.commit()
        return int(place.id)


def _emoji_in_db(h: Harness, place_id: int) -> str | None:
    from models import Place

    with h.session_factory() as session:
        place = session.get(Place, place_id)
        assert place is not None, "место исчезло"
        return place.emoji


def _set_emoji_raw(h: Harness, place_id: int, value: str | None) -> None:
    """Сырая запись значения: имитация старых/повреждённых строк."""
    with h.engine.begin() as connection:
        connection.execute(
            text("UPDATE places SET emoji = :value WHERE id = :id"),
            {"value": value, "id": place_id},
        )


async def _open_admin_card(h: Harness, place_id: int, *, tg: int = ADMIN_TG) -> None:
    await h.text("/admin", user_id=tg)
    await h.callback(f"adm_place:{place_id}", user_id=tg)


def _emoji_callback(h: Harness) -> str:
    for data in _callbacks(h):
        if data.startswith("adm_pemoji:"):
            return data
    raise AssertionError(f"нет кнопки эмодзи: {h.screen_buttons()}")


async def _open_picker(h: Harness, place_id: int, *, tg: int = ADMIN_TG) -> None:
    await _open_admin_card(h, place_id, tg=tg)
    await h.callback(_emoji_callback(h), user_id=tg)


# --- §5.1: эмодзи при создании -----------------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlaceEmojiCreationTests(unittest.TestCase):
    """Пункты 1–4: присвоение при создании, стабильность и запасное значение."""

    @scenario_test
    async def test_new_place_gets_emoji_from_set(self, h: Harness) -> None:
        """Пункт 1: эмодзи нового места — из набора и сохранён в БД."""
        place_id = _make_place(h)

        stored = _emoji_in_db(h, place_id)
        self.assertIn(stored, PLACE_EMOJI_CHOICES, f"эмодзи вне набора: {stored!r}")
        self.assertEqual(len(PLACE_EMOJI_CHOICES), 23, "набор должен быть из 23")
        self.assertEqual(
            len(set(PLACE_EMOJI_CHOICES)), 23, "символы в наборе уникальны"
        )

    @scenario_test
    async def test_emoji_is_not_regenerated_on_render(self, h: Harness) -> None:
        """Пункт 2: повторный рендер показывает сохранённое значение."""
        place_id = _make_place(h)
        stored = _emoji_in_db(h, place_id)

        await _open_admin_card(h, place_id)
        first = h.screen_text()
        await h.callback(f"adm_place:{place_id}")
        second = h.screen_text()

        self.assertEqual(_emoji_in_db(h, place_id), stored, "эмодзи перезаписан")
        self.assertIn(stored, first)
        self.assertIn(stored, second)

    @scenario_test
    async def test_existing_place_keeps_its_emoji(self, h: Harness) -> None:
        """Пункт 3: записанное ранее значение не меняется при чтении карточки."""
        place_id = _make_place(h)
        _set_emoji_raw(h, place_id, "🎪")

        await _open_admin_card(h, place_id)

        self.assertEqual(_emoji_in_db(h, place_id), "🎪")
        self.assertIn("🎪", h.screen_text())

    @scenario_test
    async def test_null_emoji_renders_with_fallback(self, h: Harness) -> None:
        """Пункт 4: NULL/пустое значение не ломает рендер (подставляется `🚀`)."""
        place_id = _make_place(h)
        _set_emoji_raw(h, place_id, None)
        self.assertEqual(place_emoji(None), PLACE_EMOJI_DEFAULT)
        self.assertEqual(place_emoji("   "), PLACE_EMOJI_DEFAULT)
        self.assertEqual(place_emoji(42), PLACE_EMOJI_DEFAULT)

        await _open_admin_card(h, place_id)

        self.assertIn(PLACE_EMOJI_DEFAULT, h.screen_text())
        self.assertEqual(_internal_errors(h), [], "рендер упал")


# --- §5.2: редактор эмодзи ---------------------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlaceEmojiEditorTests(unittest.TestCase):
    """Пункты 5–12: кнопка, пикер, выбор, случайный, «Назад», права."""

    @scenario_test
    async def test_edit_button_visible_to_place_admin_only(self, h: Harness) -> None:
        """Пункт 5: кнопка есть у управляющего и нет у постороннего."""
        place_id = _make_place(h)

        await _open_admin_card(h, place_id)
        self.assertIn(f"adm_pemoji:{place_id}", _callbacks(h))
        self.assertIn(cat.text("buttons.admin.izmenit_emodzi"), _labels(h))

        # Посторонний в кабинет не попадает вовсе: экран отказа, без кнопки.
        await h.text("/admin", user_id=STRANGER_TG)
        self.assertNotIn(f"adm_pemoji:{place_id}", _callbacks(h))
        self.assertNotIn(cat.text("buttons.admin.izmenit_emodzi"), _labels(h))

    @scenario_test
    async def test_picker_has_five_per_page_and_navigation(self, h: Harness) -> None:
        """Пункт 6: 5 на странице, 23 → 5 страниц, навигация листает набор."""
        place_id = _make_place(h)
        await _open_picker(h, place_id)

        first_page = _emoji_page_ids(h)
        self.assertEqual(len(first_page), PLACE_EMOJI_PAGE_SIZE)
        self.assertEqual(first_page, list(PLACE_EMOJI_CHOICES[:5]))
        self.assertIn(
            "1/5", " ".join(_labels(h)), f"нет счётчика страниц: {_labels(h)}"
        )

        # Последняя (пятая) страница: 23 = 5*4 + 3.
        for expected in range(1, 5):
            callbacks = [
                data for data in _callbacks(h) if data.startswith("adm_pemoji_page:")
            ]
            target = f"adm_pemoji_page:{expected}"
            self.assertIn(target, callbacks, f"нет перехода на {expected}: {callbacks}")
            await h.callback(target, user_id=ADMIN_TG)
        last_page = _emoji_page_ids(h)
        self.assertEqual(last_page, list(PLACE_EMOJI_CHOICES[20:]))
        self.assertIn("5/5", " ".join(_labels(h)))
        # Стрелки «вперёд» на последней странице нет: осталась только «◀️».
        self.assertEqual(
            [data for data in _callbacks(h) if data.startswith("adm_pemoji_page:")],
            ["adm_pemoji_page:3"],
        )

    @scenario_test
    async def test_choice_saves_and_shows_in_card(self, h: Harness) -> None:
        """Пункт 7: выбор сохраняет эмодзи и сразу виден в карточке."""
        place_id = _make_place(h)
        await _open_picker(h, place_id)

        chosen = PLACE_EMOJI_CHOICES[2]
        await h.callback(f"adm_pemoji_set:{chosen}", user_id=ADMIN_TG)

        self.assertEqual(_emoji_in_db(h, place_id), chosen)
        self.assertNotIn("adm_pemoji_page:", _callbacks(h), "пикер не закрылся")
        self.assertIn(f"adm_pemoji:{place_id}", _callbacks(h), "это не карточка места")
        self.assertIn(chosen, h.screen_text())

    @scenario_test
    async def test_random_button_saves_value_from_set(self, h: Harness) -> None:
        """Пункт 8: «🎲 Случайный эмодзи» сохраняет значение из набора."""
        place_id = _make_place(h)
        _set_emoji_raw(h, place_id, "🦊")
        await _open_picker(h, place_id)

        await h.callback("adm_pemoji_random", user_id=ADMIN_TG)

        stored = _emoji_in_db(h, place_id)
        self.assertIn(stored, PLACE_EMOJI_CHOICES, f"вне набора: {stored!r}")
        self.assertIn(stored, h.screen_text(), "карточка показывает старое значение")

    @scenario_test
    async def test_back_from_picker_keeps_emoji(self, h: Harness) -> None:
        """Пункт 9: «◀️ Назад» ничего не меняет."""
        place_id = _make_place(h)
        _set_emoji_raw(h, place_id, "🌿")
        await _open_picker(h, place_id)

        await h.callback("adm_pemoji_back", user_id=ADMIN_TG)

        self.assertEqual(_emoji_in_db(h, place_id), "🌿")
        self.assertIn("🌿", h.screen_text())
        self.assertIn(f"adm_pemoji:{place_id}", _callbacks(h))

    @scenario_test
    async def test_stranger_and_forged_callback_are_refused(self, h: Harness) -> None:
        """Пункт 10: не-админ и подделанный `place_id` получают отказ."""
        own = _make_place(h, "Своё место")
        foreign = _make_place(h, "Чужое место", admin_tg=OTHER_ADMIN_TG)
        _set_emoji_raw(h, foreign, "🦊")

        await _open_picker(h, own)
        # Подделка: открыт пикер своего места, в callback — чужое.
        await h.callback(f"adm_pemoji:{foreign}", user_id=ADMIN_TG)
        self.assertTrue(_last_alert(h), "подделанная кнопка не получила отказ")
        cat.assert_stale(self, _last_alert(h))
        await h.callback("adm_pemoji_set:🎭", user_id=ADMIN_TG)
        self.assertEqual(_emoji_in_db(h, foreign), "🦊", "чужое место изменено")

        # Посторонний не может ни открыть пикер, ни выбрать эмодзи.
        await h.callback(f"adm_pemoji:{own}", user_id=STRANGER_TG)
        self.assertEqual(_internal_errors(h), [], "отказ упал внутренней ошибкой")
        await h.callback("adm_pemoji_set:🎬", user_id=STRANGER_TG)
        self.assertNotEqual(_emoji_in_db(h, own), "🎬", "посторонний сменил эмодзи")

    @scenario_test
    async def test_reopening_and_repeating_choice_are_harmless(
        self, h: Harness
    ) -> None:
        """Пункт 11: повторный вход и повторный выбор ничего не ломают."""
        place_id = _make_place(h)
        chosen = PLACE_EMOJI_CHOICES[7]

        await _open_picker(h, place_id)
        await h.callback(f"adm_pemoji_set:{chosen}", user_id=ADMIN_TG)
        await _open_picker(h, place_id)
        await h.callback(f"adm_pemoji_set:{chosen}", user_id=ADMIN_TG)

        self.assertEqual(_emoji_in_db(h, place_id), chosen)
        self.assertEqual(_internal_errors(h), [])
        self.assertIn(chosen, h.screen_text())

    @scenario_test
    async def test_chosen_emoji_shows_in_public_card(self, h: Harness) -> None:
        """Пункт 12: выбранный эмодзи виден в публичной карточке места."""
        place_id = _make_place(h)
        chosen = PLACE_EMOJI_CHOICES[12]

        await _open_picker(h, place_id)
        await h.callback(f"adm_pemoji_set:{chosen}", user_id=ADMIN_TG)

        # Публичный просмотр: «Управление → Площадки → место» у не-админа.
        await h.text("/start", user_id=STRANGER_TG)
        await h.callback("manage_places", user_id=STRANGER_TG)
        await h.callback(f"plm_open:{place_id}", user_id=STRANGER_TG)

        self.assertIn(chosen, h.screen_text(), "публичная карточка без эмодзи")

        # Эмодзи виден и в списке мест кабинета (кнопка строки списка).
        await h.callback("adm_places", user_id=ADMIN_TG)
        self.assertIn(
            chosen,
            " ".join(_labels(h)),
            f"в списке мест нет эмодзи: {_labels(h)}",
        )


def _emoji_page_ids(h: Harness) -> list[str]:
    """Эмодзи, показанные на текущей странице пикера (в порядке кнопок)."""
    values: list[str] = []
    for data in _callbacks(h):
        if data.startswith("adm_pemoji_set:"):
            values.append(data[len("adm_pemoji_set:") :])
    return values
