# tests/test_stage26_c_wizard_catalogs.py
"""
«Лекало» 2.6, этап C (§9, §10.1–10.3, §19.5–19.7): справочники кураторов и
подвалов внутри конструктора анонса.

Проверяется пользовательский контракт, а не внутренние функции: сценарии
идут через настоящее приложение (`bot.build_application`), FakeBot и
временную SQLite. Рабочая `post_bot.db`, `.env` и сеть Telegram не
используются.

Что закрепляется:

- куратор в мастере: несколько — список с пагинацией и ручной ввод; один —
  кнопка выбора без автоподстановки и ручной ввод; ноль — две НАСТОЯЩИЕ
  неактивные кнопки в одной строке (без callback_data), ручной ввод и
  отсутствие уведомлений администраторам; личный вариант не упоминает
  администратора площадки;
- подвал в первоначальном мастере: несколько — выбор; один — подстановка
  текста без вопроса; ноль — неактивный ряд при АКТИВНЫХ «Назад» и
  «Пропустить», создание продолжается; ручного ввода текста нет;
- подвал в «Дополнительных полях» — все пять сценариев §10.2, красные
  «Отмена»/«Удалить», пагинация;
- §10.3: единственный подвал подставляется ровно один раз, удалённое
  значение не возвращается после перерисовки/перезапуска, правка
  справочника не меняет сохранённый анонс;
- значение справочника принадлежит конкретному анонсу.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage26_c_wizard_catalogs -v
"""

from __future__ import annotations

import unittest

from tests import ui_texts_expect as cat

try:

    from announcement_store import get_announcement_draft
    from brick_types import BrickValueStatus
    from catalogs_store import (
        create_curator,
        create_footer,
        selectable_footers,
        update_footer_text,
    )
    from models import PlaceAdmin, utcnow
    from place_store import create_place
    from template_store import (
        create_personal_template,
        get_standard_template,
        set_standard_extended_active,
        update_template_content,
    )
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        assert_delivered,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import inline_buttons

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)

# Администратор площадки — НЕ автор анонса: так проверяется, что справочники
# только читаются автором, а уведомлений администратору не уходит. Чат автора
# в FakeBot — DEFAULT_CHAT_ID (777), id администратора в чат не попадает.
ADMIN_TG = 26_070_001
AUTHOR_TG = 26_070_002
FOREIGN_TG = 26_070_003

CURATOR_CODE = "куратор"
FOOTER_CODE = "подвал"

FOOTER_CHOOSE = cat.text("wizard.catalog.footer_choose")
FOOTER_CHOOSE_OTHER = cat.text("wizard.catalog.footer_choose_other")
DELETE_CONFIRM = cat.text("wizard.catalog.footer_delete_confirm")
DELETE_LABEL = cat.text("buttons.wizard.catalog_delete")
CANCEL_LABEL = cat.text("buttons.wizard.catalog_cancel")
SKIPPED_TEXT = cat.text("wizard.status.skipped")


# --- Помощники ---------------------------------------------------------------


def _rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _raw_rows(h: Harness) -> list[list]:
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return list(markup.inline_keyboard)


def _labels(h: Harness) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _callbacks(h: Harness, prefix: str) -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


def _callback(h: Harness, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(
            f"нет callback {prefix!r}: {h.screen_buttons()}; экран: "
            f"{h.screen_text()[:300]!r}"
        )
    return found[0]


async def _press(h: Harness, prefix: str, *, user_id: int = AUTHOR_TG) -> None:
    await h.callback(_callback(h, prefix), user_id=user_id)


def _ui(h: Harness, *, user_id: int = AUTHOR_TG) -> dict:
    return h.user_data(user_id=user_id).get("announcement_ui") or {}


def _draft_id(h: Harness, *, user_id: int = AUTHOR_TG) -> int:
    return int(_ui(h, user_id=user_id)["draft_id"])


def _details(h: Harness, draft_id: int, *, user_id: int = AUTHOR_TG):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=user_id,
            draft_id=draft_id,
        )


def _brick_id(details, code: str) -> int:
    snapshot = details.template_snapshot
    assert snapshot is not None, "у черновика нет снимка шаблона"
    for binding in snapshot.bindings:
        if binding.code == code:
            return binding.brick_id
    raise AssertionError(f"в снимке нет кирпичика [{code}]")


def _value(details, code: str):
    return details.value_for(_brick_id(details, code))


def _value_text(details, code: str) -> str | None:
    value = _value(details, code)
    if value is None or value.status is not BrickValueStatus.VALID:
        return None
    payload = value.payload or {}
    raw = payload.get("text")
    return raw if isinstance(raw, str) else None


def _value_status(details, code: str):
    value = _value(details, code)
    return None if value is None else value.status


def _seed_place(h: Harness, name: str = "Дом культуры") -> int:
    """Площадка администратора ADMIN_TG; автор анонса — AUTHOR_TG."""
    with h.session_factory() as session:
        admin = get_or_create_user(session, ADMIN_TG, username="admin")
        get_or_create_user(session, AUTHOR_TG, username="author")
        get_or_create_user(session, FOREIGN_TG, username="foreign")
        place = create_place(
            session,
            name,
            city="Москва",
            created_by_user_id=admin.id,
            address="ул. Ленина, 1",
            url="https://example.com/place",
        )
        session.add(
            PlaceAdmin(
                place_id=place.id,
                user_id=admin.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
            )
        )
        session.commit()
        return int(place.id)


def _set_standard(h: Harness, place_id: int, extended_body: str | None) -> None:
    """
    Стандарт площадки: простой текст — одно название; расширенный — название
    плюс `extended_body`; флаг включён.
    """
    with h.session_factory() as session:
        standard = get_standard_template(session, ADMIN_TG, place_id)
        extended = None
        if extended_body is not None:
            extended = f"**[название]**\n\n{extended_body}"
        result = update_template_content(
            session,
            actor_telegram_user_id=ADMIN_TG,
            template_id=standard.template_id,
            expected_revision=standard.revision,
            content="**[название]**",
            extended_content=extended,
        )
        assert result.ok, result.message
        assert result.template is not None
        set_standard_extended_active(
            session,
            actor_telegram_user_id=ADMIN_TG,
            template_id=standard.template_id,
            active=True,
            expected_revision=result.template.revision,
        )
        session.commit()


def _seed_curators(h: Harness, place_id: int | None, values) -> None:
    with h.session_factory() as session:
        for value in values:
            create_curator(
                session,
                actor_telegram_user_id=ADMIN_TG if place_id else AUTHOR_TG,
                place_id=place_id,
                value=value,
            )
        session.commit()


def _seed_footers(h: Harness, place_id: int | None, entries) -> None:
    with h.session_factory() as session:
        for name, text in entries:
            create_footer(
                session,
                actor_telegram_user_id=ADMIN_TG if place_id else AUTHOR_TG,
                place_id=place_id,
                name=name,
                text=text,
            )
        session.commit()


def _personal_template(
    h: Harness,
    *,
    content: str = "**[название]**\n\n[куратор]\n\n[подвал]",
    extended: str | None = None,
) -> int:
    with h.session_factory() as session:
        get_or_create_user(session, AUTHOR_TG, username="author")
        template = create_personal_template(
            session,
            actor_telegram_user_id=AUTHOR_TG,
            name="Личный шаблон",
            content=content,
            extended_content=extended,
        )
        session.commit()
        return int(template.template_id)


async def _open_place_questions(h: Harness, place_id: int) -> None:
    """Старт анонса по площадке сразу на вопросах (шаблон — стандарт)."""
    await h.text("/start", user_id=AUTHOR_TG)
    await h.callback("pick_place", user_id=AUTHOR_TG)
    await h.callback(f"pl_pick:{place_id}", user_id=AUTHOR_TG)


async def _open_personal_questions(h: Harness, template_id: int) -> None:
    await h.text("/start", user_id=AUTHOR_TG)
    await h.callback("pick_place", user_id=AUTHOR_TG)
    await h.callback("pick_template", user_id=AUTHOR_TG)
    await h.callback(f"tp_pick:{template_id}", user_id=AUTHOR_TG)


async def _answer_name(h: Harness, value: str = "Летний концерт") -> None:
    await h.text(value, user_id=AUTHOR_TG)


async def _open_extras(h: Harness) -> None:
    """
    Открывает «Дополнительные поля» текущего черновика.

    На шаге фото кнопки доп. полей нет; штатный вход из обзора требует
    отдельного перехода, поэтому используется тот же callback, что и на
    экране обзора (`aw_extras:<draft>:<revision>`), с актуальной ревизией.
    """
    ui = _ui(h)
    await h.callback(f"aw_extras:{ui['draft_id']}:{ui['revision']}", user_id=AUTHOR_TG)


def _admin_chat_ids(h: Harness) -> set[int]:
    """chat_id всех исходящих сообщений бота (для проверки «нет уведомлений»)."""
    out: set[int] = set()
    for call in h.calls:
        if call["type"] not in {
            "message",
            "photo",
            "edit_message",
            "edit_caption",
            "media_group",
            "rich_message",
            "document",
            "edit_markup",
        }:
            continue
        chat_id = call.get("chat_id")
        if chat_id is None:
            args = tuple(call.get("args") or ())
            chat_id = args[0] if args else None
        if isinstance(chat_id, int):
            out.add(chat_id)
    return out


def _assert_real_disabled_row(
    self: unittest.TestCase, h: Harness, first: str, second: str
) -> None:
    """
    Ряд §11: две кнопки в ОДНОЙ строке, обе настоящие неактивные.

    `callback_data` отсутствует (иначе Bot API сделал бы кнопку обычной), в
    сериализации есть `disabled`, а нажатие физически не может породить
    callback.
    """
    found = None
    for index, row in enumerate(_raw_rows(h)):
        texts = [button.text for button in row]
        if texts == [first, second]:
            found = (index, row)
            break
    self.assertIsNotNone(found, f"нет ряда [{first}] [{second}]: {_rows(h)}")
    assert found is not None
    index, row = found
    self.assertEqual(len(row), 2, "обе подсказки должны быть в одной строке")
    for button in row:
        self.assertIsNone(
            button.callback_data, "у неактивной кнопки не должно быть callback"
        )
        self.assertTrue(
            inline_buttons.is_disabled(button), "кнопка должна быть disabled"
        )
        self.assertIn("disabled", button.to_dict())
    self.assertEqual(_rows(h)[index][0][1], None)
    self.assertEqual(_rows(h)[index][1][1], None)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CuratorInWizardTests(unittest.TestCase):
    """§9, §19.5: куратор в первоначальном мастере (площадка и личный)."""

    @scenario_test
    async def test_several_curators_choice_with_pagination(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]")
        _seed_curators(h, place_id, [f"@c{number}" for number in range(1, 8)])
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        # §3.3 (2.6.1): у системного куратора есть собственное описание,
        # поэтому это описание и есть единственная подсказка — строка
        # `wizard.catalog.curator_choose` второй подсказкой не добавляется.
        self.assertEqual(
            h.screen_text().split("\n\n")[1:],
            [
                cat.text(
                    "wizard.question.description_html",
                    description=(
                        "Выберите куратора или пришлите его контакт: @ник, "
                        "ссылку или текст с гиперссылкой."
                    ),
                )
            ],
            f"у вопроса куратора не одна подсказка: {h.screen_text()!r}",
        )
        cat.assert_not_shows(self, h.screen_text(), "wizard.catalog.curator_choose")
        picks = _callbacks(h, "aw_curpick:")
        self.assertEqual(len(picks), 5, "пять записей на страницу")
        self.assertIn(("1/2", "noop:1:2"), h.screen_buttons())
        self.assertTrue(_callbacks(h, "aw_curpage:"))

        await _press(h, "aw_curpage:")
        self.assertIn("@c6", _labels(h))
        self.assertIn("@c7", _labels(h))
        self.assertNotIn("@c1", _labels(h))

        await _press(h, "aw_curpick:")
        draft_id = _draft_id(h)
        details = _details(h, draft_id)
        self.assertEqual(_value_text(details, CURATOR_CODE), "@c6")
        # После выбора мастер ушёл дальше (куратор последний в шаблоне).
        self.assertTrue(
            _callbacks(h, "aw_photo_none:"),
            f"мастер не продвинулся: {h.screen_buttons()}",
        )

    @scenario_test
    async def test_several_curators_manual_text_is_saved(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]")
        _seed_curators(h, place_id, ["@c1", "@c2", "@c3"])
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        # Кнопки выбора не мешают ручному контакту.
        await h.text("Иван @manual", user_id=AUTHOR_TG)
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, CURATOR_CODE), "Иван @manual")
        self.assertTrue(_callbacks(h, "aw_photo_none:"))

    @scenario_test
    async def test_single_curator_is_not_autofilled_and_manual_works(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]")
        _seed_curators(h, place_id, ["@only"])
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        # Один куратор — кнопка выбора есть, значения у анонса ещё нет.
        self.assertIn(("@only", _callback(h, "aw_curpick:")), h.screen_buttons())
        details = _details(h, _draft_id(h))
        self.assertIsNone(
            _value(details, CURATOR_CODE), "не должно быть автоподстановки"
        )

        # Кнопка выбора сохраняет контакт.
        await _press(h, "aw_curpick:")
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, CURATOR_CODE), "@only")

    @scenario_test
    async def test_single_curator_manual_text_works_without_pressing_button(
        self, h
    ) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]")
        _seed_curators(h, place_id, ["@only"])
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        await h.text("@other", user_id=AUTHOR_TG)
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, CURATOR_CODE), "@other")

    @scenario_test
    async def test_empty_curator_list_real_disabled_row_and_manual_input(
        self, h
    ) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]")
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        first = cat.text("wizard.catalog.no_curators_place")
        second = cat.text("wizard.catalog.no_curators_place_hint")
        _assert_real_disabled_row(self, h, first, second)
        # Штатная навигация вопроса остаётся рабочей.
        self.assertTrue(_callbacks(h, "aw_qback:"))
        self.assertTrue(_callbacks(h, "aw_qskip:"))

        await h.text("@free", user_id=AUTHOR_TG)
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, CURATOR_CODE), "@free")
        # Никаких сообщений администратору площадки: весь чат — автора.
        admin_ids = _admin_chat_ids(h)
        self.assertNotIn(ADMIN_TG, admin_ids)
        self.assertTrue(admin_ids <= {h.chat_id}, admin_ids)

    @scenario_test
    async def test_foreign_author_sees_same_disabled_row_without_admin_ping(
        self, h
    ) -> None:
        """Автор без прав на справочник: выбор доступен, уведомлений нет."""
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]")
        await h.text("/start", user_id=FOREIGN_TG)
        await h.callback("pick_place", user_id=FOREIGN_TG)
        await h.callback(f"pl_pick:{place_id}", user_id=FOREIGN_TG)
        await h.text("Концерт", user_id=FOREIGN_TG)

        first = cat.text("wizard.catalog.no_curators_place")
        second = cat.text("wizard.catalog.no_curators_place_hint")
        _assert_real_disabled_row(self, h, first, second)
        self.assertNotIn(ADMIN_TG, _admin_chat_ids(h))
        # Ручной ввод чужому автору тоже доступен.
        await h.text("@guest", user_id=FOREIGN_TG)
        details = _details(h, _draft_id(h, user_id=FOREIGN_TG), user_id=FOREIGN_TG)
        self.assertEqual(_value_text(details, CURATOR_CODE), "@guest")

    @scenario_test
    async def test_curator_value_belongs_to_specific_announcement(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]")
        _seed_curators(h, place_id, ["@c1", "@c2"])
        await _open_place_questions(h, place_id)
        await _answer_name(h)
        await h.callback(
            next(data for label, data in h.screen_buttons() if label == "@c1" and data),
            user_id=AUTHOR_TG,
        )
        first_draft = _draft_id(h)

        await _open_place_questions(h, place_id)
        await _answer_name(h, "Второй концерт")
        await h.callback(
            next(data for label, data in h.screen_buttons() if label == "@c2" and data),
            user_id=AUTHOR_TG,
        )
        second_draft = _draft_id(h)

        self.assertNotEqual(first_draft, second_draft)
        self.assertEqual(_value_text(_details(h, first_draft), CURATOR_CODE), "@c1")
        self.assertEqual(_value_text(_details(h, second_draft), CURATOR_CODE), "@c2")

    @scenario_test
    async def test_personal_several_curators_choice_and_pagination(self, h) -> None:
        template_id = _personal_template(h, content="**[название]**\n\n[куратор]")
        _seed_curators(h, None, [f"@p{number}" for number in range(1, 7)])
        await _open_personal_questions(h, template_id)
        await _answer_name(h)

        self.assertEqual(len(_callbacks(h, "aw_curpick:")), 5)
        self.assertIn(("1/2", "noop:1:2"), h.screen_buttons())
        await _press(h, "aw_curpage:")
        self.assertIn("@p6", _labels(h))
        await _press(h, "aw_curpick:")
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, CURATOR_CODE), "@p6")

    @scenario_test
    async def test_personal_single_curator_is_not_autofilled(self, h) -> None:
        template_id = _personal_template(h, content="**[название]**\n\n[куратор]")
        _seed_curators(h, None, ["@only-personal"])
        await _open_personal_questions(h, template_id)
        await _answer_name(h)

        self.assertIn(
            ("@only-personal", _callback(h, "aw_curpick:")), h.screen_buttons()
        )
        self.assertIsNone(_value(_details(h, _draft_id(h)), CURATOR_CODE))

    @scenario_test
    async def test_personal_empty_curator_row_does_not_mention_place_admin(
        self, h
    ) -> None:
        template_id = _personal_template(h, content="**[название]**\n\n[куратор]")
        await _open_personal_questions(h, template_id)
        await _answer_name(h)

        first = cat.text("wizard.catalog.no_curators_personal")
        second = cat.text("wizard.catalog.no_curators_personal_hint")
        _assert_real_disabled_row(self, h, first, second)
        place_hint = cat.text("wizard.catalog.no_curators_place_hint")
        self.assertNotIn(place_hint, _labels(h))
        self.assertNotIn("админ", h.screen_text().lower())
        self.assertFalse(_callbacks(h, "adm_"))

        await h.text("@me", user_id=AUTHOR_TG)
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, CURATOR_CODE), "@me")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FooterInWizardTests(unittest.TestCase):
    """§10.1, §19.6: подвал в первоначальном мастере."""

    @scenario_test
    async def test_several_footers_choice_in_wizard(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "[подвал]")
        _seed_footers(h, place_id, [("F1", "Текст 1"), ("F2", "Текст 2")])
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        self.assertIn(FOOTER_CHOOSE, h.screen_text())
        self.assertEqual(len(_callbacks(h, "aw_footpick:")), 2)
        self.assertIn("F1", _labels(h))
        self.assertIn("F2", _labels(h))
        await h.callback(
            next(data for label, data in h.screen_buttons() if label == "F2" and data),
            user_id=AUTHOR_TG,
        )
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, FOOTER_CODE), "Текст 2")
        self.assertTrue(_callbacks(h, "aw_photo_none:"))

    @scenario_test
    async def test_single_footer_is_autofilled_without_question(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "[подвал]")
        _seed_footers(h, place_id, [("Донат", "Карта рядом")])
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        # Вопроса про подвал не было: сразу шаг фотографий.
        self.assertNotIn(FOOTER_CHOOSE, h.screen_text())
        self.assertTrue(_callbacks(h, "aw_photo_none:"))
        self.assertFalse(
            any(FOOTER_CHOOSE in text for text in h.screen_texts()),
            "экран выбора единственного подвала не должен показываться",
        )
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, FOOTER_CODE), "Карта рядом")
        self.assertEqual(_value_status(details, FOOTER_CODE), BrickValueStatus.VALID)

    @scenario_test
    async def test_empty_footer_list_keeps_back_and_skip_active(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "[подвал]")
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        first = cat.text("wizard.catalog.no_footers_place")
        second = cat.text("wizard.catalog.no_footers_place_hint")
        _assert_real_disabled_row(self, h, first, second)
        # В отличие от информационного ряда, навигация мастера активна.
        back = _callbacks(h, "aw_qback:")
        skip = _callbacks(h, "aw_qskip:")
        self.assertEqual(len(back), 1)
        self.assertEqual(len(skip), 1)

        # Ручного ввода подвала нет: текст остаётся на том же вопросе.
        await h.text("Свой подвал", user_id=AUTHOR_TG)
        self.assertIn(cat.text("wizard.catalog.footer_choose"), h.screen_text())
        details = _details(h, _draft_id(h))
        self.assertIsNone(_value(details, FOOTER_CODE))

        # «Пропустить» не блокирует создание: анонс доходит до доставки.
        await h.callback(skip[0], user_id=AUTHOR_TG)
        assert_delivered_buttons = _callbacks(h, "aw_photo_none:")
        self.assertTrue(assert_delivered_buttons)
        await h.callback(assert_delivered_buttons[0], user_id=AUTHOR_TG)
        draft_id = _draft_id(h)
        assert_delivered(h, draft_id)
        self.assertEqual(h.posts()[-1], "<b>Летний концерт</b>")
        self.assertNotIn("подвал", (h.posts()[-1] or "").lower())

    @scenario_test
    async def test_empty_catalogs_do_not_block_creation(self, h) -> None:
        """§19.6: пустые справочники не блокируют создание анонса (§10.1)."""
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]\n\n[подвал]")
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        # Куратор: пустой ряд, но ручной ввод доступен — создание идёт дальше.
        _assert_real_disabled_row(
            self,
            h,
            cat.text("wizard.catalog.no_curators_place"),
            cat.text("wizard.catalog.no_curators_place_hint"),
        )
        await h.text("@manual", user_id=AUTHOR_TG)

        # Подвал: пустой ряд с активными «Назад»/«Пропустить».
        _assert_real_disabled_row(
            self,
            h,
            cat.text("wizard.catalog.no_footers_place"),
            cat.text("wizard.catalog.no_footers_place_hint"),
        )
        skip = _callbacks(h, "aw_qskip:")
        self.assertEqual(len(skip), 1)
        await h.callback(skip[0], user_id=AUTHOR_TG)

        await h.callback(_callback(h, "aw_photo_none:"), user_id=AUTHOR_TG)
        draft_id = _draft_id(h)
        assert_delivered(h, draft_id)
        self.assertEqual(h.posts()[-1], "<b>Летний концерт</b>\n\nКуратор: @manual")
        # «Пропустить» оставляет «надгробие»: пустой подвал не дописан в пост.
        self.assertEqual(
            _value_status(_details(h, draft_id), FOOTER_CODE),
            BrickValueStatus.EMPTY_OPTIONAL,
        )

    @scenario_test
    async def test_footer_manual_text_rejected_on_choice_question(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "[подвал]")
        _seed_footers(h, place_id, [("F1", "Текст 1"), ("F2", "Текст 2")])
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        before = _details(h, _draft_id(h)).revision
        await h.text("Свой текст", user_id=AUTHOR_TG)
        self.assertIn(cat.text("wizard.catalog.footer_choose"), h.screen_text())
        details = _details(h, _draft_id(h))
        self.assertEqual(details.revision, before, "отказ не должен менять черновик")
        self.assertIsNone(_value(details, FOOTER_CODE))
        # Вопрос остался тем же — навигация и выбор на месте.
        self.assertTrue(_callbacks(h, "aw_footpick:"))

    @scenario_test
    async def test_autofill_happens_once_and_not_on_reopen(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "[подвал]")
        _seed_footers(h, place_id, [("Донат", "Карта рядом")])
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        after_autofill = _details(h, _draft_id(h)).revision
        # «Назад» со шага фото возвращает к вопросу подвала с уже выбранным
        # значением — повторной подстановки нет. §3.3 (2.6.1) убрал строку
        # «Сейчас: …» с экрана вопроса, поэтому значение проверяем по данным.
        await _press(h, "aw_photo_back:")
        self.assertEqual(
            _value_text(_details(h, _draft_id(h)), FOOTER_CODE), "Карта рядом"
        )
        self.assertEqual(_details(h, _draft_id(h)).revision, after_autofill)

        # «Пропустить» не перезаписывает значение и не увеличивает ревизию.
        await _press(h, "aw_qskip:")
        self.assertEqual(_details(h, _draft_id(h)).revision, after_autofill)

        # Рестарт бота тоже не возвращает подстановку: значение уже есть.
        await h.restart()
        await h.text("/start", user_id=AUTHOR_TG)
        await h.callback("my_announcements", user_id=AUTHOR_TG)
        await h.callback(_callback(h, "aw_open:"), user_id=AUTHOR_TG)
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, FOOTER_CODE), "Карта рядом")
        self.assertEqual(_details(h, _draft_id(h)).revision, after_autofill)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FooterExtrasTests(unittest.TestCase):
    """§10.2, §19.7: подвал в «Дополнительных полях» (площадка)."""

    async def _prepare_extras(self, h: Harness) -> int:
        """Черновик на шаге фото без выбранного подвала."""
        place_id = _seed_place(h)
        _set_standard(h, place_id, "[подвал]")
        await _open_place_questions(h, place_id)
        await _answer_name(h)
        # Справочник пуст: вопрос подвала пропускается, создание идёт дальше.
        if _callbacks(h, "aw_qskip:"):
            await _press(h, "aw_qskip:")
        await _open_extras(h)
        return place_id

    async def _add_footer(
        self, h: Harness, place_id: int, name: str, text: str
    ) -> None:
        _seed_footers(h, place_id, [(name, text)])
        # Экран доп. полей перерисовываем, чтобы запись появилась в списке.
        await _open_extras(h)

    @scenario_test
    async def test_no_active_footers_hides_entry_completely(self, h) -> None:
        await self._prepare_extras(h)
        self.assertTrue(_callbacks(h, "aw_exback:"))
        self.assertFalse(_callbacks(h, "aw_footch:"), "кнопки [подвал] быть не должно")
        self.assertIn(cat.text("wizard.extras.empty"), h.screen_text())

    @scenario_test
    async def test_single_footer_unselected_inserts_immediately(self, h) -> None:
        place_id = await self._prepare_extras(h)
        await self._add_footer(h, place_id, "Донат", "Карта рядом")
        self.assertTrue(_callbacks(h, "aw_footch:"))

        await _press(h, "aw_footch:")
        # Сразу обновлённый список: без экрана выбора и без подтверждения.
        self.assertFalse(_callbacks(h, "aw_footpick:"))
        self.assertFalse(_callbacks(h, "aw_footpage:"))
        self.assertFalse(_callbacks(h, "aw_footdel:"))
        self.assertIn("Карта рядом", h.screen_text())
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, FOOTER_CODE), "Карта рядом")

    @scenario_test
    async def test_single_footer_selected_asks_delete_with_danger_buttons(
        self, h
    ) -> None:
        place_id = await self._prepare_extras(h)
        await self._add_footer(h, place_id, "Донат", "Карта рядом")
        await _press(h, "aw_footch:")

        # Повторное нажатие — «Удалить подвал?» с красными «Отмена»/«Удалить».
        await _press(h, "aw_footch:")
        self.assertIn(DELETE_CONFIRM, h.screen_text())
        self.assertIn("Донат", h.screen_text())
        rows = _raw_rows(h)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(
            [button.text for button in rows[0]], [CANCEL_LABEL, DELETE_LABEL]
        )
        for button in rows[0]:
            self.assertEqual(button.to_dict().get("style"), "danger")
        self.assertTrue(rows[0][0].callback_data.startswith("aw_footcancel:"))
        self.assertTrue(rows[0][1].callback_data.startswith("aw_footdel:"))

        # «Отмена» ничего не меняет.
        before = _details(h, _draft_id(h))
        await _press(h, "aw_footcancel:")
        after = _details(h, _draft_id(h))
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(_value_text(after, FOOTER_CODE), "Карта рядом")
        self.assertIn("Карта рядом", h.screen_text())

        # «Удалить» убирает значение из анонса, но не из справочника.
        await _press(h, "aw_footch:")
        await _press(h, "aw_footdel:")
        details = _details(h, _draft_id(h))
        self.assertEqual(
            _value_status(details, FOOTER_CODE), BrickValueStatus.EMPTY_OPTIONAL
        )
        self.assertNotIn("Карта рядом", h.screen_text())
        self.assertIn(SKIPPED_TEXT, h.screen_text())
        with h.session_factory() as session:
            footers = selectable_footers(
                session, actor_telegram_user_id=AUTHOR_TG, place_id=place_id
            )
        self.assertEqual([footer.name for footer in footers], ["Донат"])

        # Повторное открытие редактора удалённое НЕ подставляет.
        await _open_extras(h)
        details = _details(h, _draft_id(h))
        self.assertEqual(
            _value_status(details, FOOTER_CODE), BrickValueStatus.EMPTY_OPTIONAL
        )
        self.assertNotIn("Карта рядом", h.screen_text())

    @scenario_test
    async def test_several_footers_unselected_list_and_cancel(self, h) -> None:
        place_id = await self._prepare_extras(h)
        _seed_footers(
            h,
            place_id,
            [("F1", "Текст 1"), ("F2", "Текст 2"), ("F3", "Текст 3")],
        )
        await _open_extras(h)
        await _press(h, "aw_footch:")

        self.assertIn(FOOTER_CHOOSE, h.screen_text())
        self.assertEqual(len(_callbacks(h, "aw_footpick:")), 3)
        self.assertEqual(_labels(h)[-1], CANCEL_LABEL)
        self.assertFalse(_callbacks(h, "aw_footdel:"))

        before = _details(h, _draft_id(h)).revision
        await _press(h, "aw_footcancel:")
        after = _details(h, _draft_id(h))
        self.assertEqual(after.revision, before)
        # Значения нет: «Пропустить» мастера оставило «надгробие», а отмена
        # на экране выбора ничего не меняет.
        self.assertIsNone(_value_text(after, FOOTER_CODE))

        # Выбор сохраняет текст (а не название) и возвращает в доп. поля.
        await _press(h, "aw_footch:")
        await h.callback(
            next(data for label, data in h.screen_buttons() if label == "F3" and data),
            user_id=AUTHOR_TG,
        )
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, FOOTER_CODE), "Текст 3")
        self.assertIn(cat.text("wizard.extras.title"), h.screen_text())
        self.assertIn("Текст 3", h.screen_text())

    @scenario_test
    async def test_several_footers_selected_offers_other_variants_and_delete(
        self, h
    ) -> None:
        place_id = await self._prepare_extras(h)
        _seed_footers(
            h,
            place_id,
            [("F1", "Текст 1"), ("F2", "Текст 2"), ("F3", "Текст 3")],
        )
        await _open_extras(h)
        await _press(h, "aw_footch:")
        await h.callback(
            next(data for label, data in h.screen_buttons() if label == "F1" and data),
            user_id=AUTHOR_TG,
        )

        # Несколько активных и выбран: заголовок замены, БЕЗ «Удалить подвал?».
        await _press(h, "aw_footch:")
        self.assertIn(FOOTER_CHOOSE_OTHER, h.screen_text())
        self.assertNotIn(DELETE_CONFIRM, h.screen_text())
        labels = _labels(h)
        self.assertNotIn("F1", labels, "текущий подвал в списке не повторяется")
        self.assertIn("F2", labels)
        self.assertIn("F3", labels)
        self.assertEqual(labels[-1], CANCEL_LABEL)
        self.assertIn(DELETE_LABEL, labels)
        self.assertTrue(_callbacks(h, "aw_footdel:"))

        # Замена другим подвалом перезаписывает значение анонса.
        await h.callback(
            next(data for label, data in h.screen_buttons() if label == "F2" and data),
            user_id=AUTHOR_TG,
        )
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, FOOTER_CODE), "Текст 2")

    @scenario_test
    async def test_manual_text_on_extras_footer_screen_changes_nothing(self, h) -> None:
        place_id = await self._prepare_extras(h)
        _seed_footers(h, place_id, [("F1", "Текст 1"), ("F2", "Текст 2")])
        await _open_extras(h)
        await _press(h, "aw_footch:")

        before = _details(h, _draft_id(h)).revision
        await h.text("Свой подвал", user_id=AUTHOR_TG)
        after = _details(h, _draft_id(h))
        self.assertEqual(after.revision, before, "ручной ввод подвала запрещён")
        self.assertIsNone(_value_text(after, FOOTER_CODE))

    @scenario_test
    async def test_several_footers_in_extras_have_pagination(self, h) -> None:
        place_id = await self._prepare_extras(h)
        _seed_footers(
            h,
            place_id,
            [(f"F{number}", f"Текст {number}") for number in range(1, 8)],
        )
        await _open_extras(h)
        await _press(h, "aw_footch:")

        self.assertEqual(len(_callbacks(h, "aw_footpick:")), 5)
        self.assertIn(("1/2", "noop:1:2"), h.screen_buttons())
        await _press(h, "aw_footpage:")
        labels = _labels(h)
        self.assertIn("F6", labels)
        self.assertIn("F7", labels)
        self.assertNotIn("F1", labels)
        # «Отмена» есть на любой странице.
        self.assertEqual(labels[-1], CANCEL_LABEL)

    @scenario_test
    async def test_catalog_edit_does_not_change_saved_announcement(self, h) -> None:
        place_id = await self._prepare_extras(h)
        await self._add_footer(h, place_id, "Донат", "Старый текст")
        await _press(h, "aw_footch:")

        with h.session_factory() as session:
            footer = selectable_footers(
                session, actor_telegram_user_id=AUTHOR_TG, place_id=place_id
            )[0]
            update_footer_text(
                session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=place_id,
                entry_id=footer.id,
                text="Новый текст",
            )
            session.commit()

        details = _details(h, _draft_id(h))
        self.assertEqual(
            _value_text(details, FOOTER_CODE),
            "Старый текст",
            "правка справочника не должна переписывать сохранённый анонс",
        )

    @scenario_test
    async def test_stale_footer_pick_does_not_replace_value(self, h) -> None:
        place_id = await self._prepare_extras(h)
        _seed_footers(h, place_id, [("F1", "Текст 1"), ("F2", "Текст 2")])
        await _open_extras(h)
        await _press(h, "aw_footch:")
        # Запоминаем callback второй страницы выбора ДО сохранения значения.
        stale_pick = next(
            data for label, data in h.screen_buttons() if label == "F2" and data
        )
        await h.callback(
            next(data for label, data in h.screen_buttons() if label == "F1" and data),
            user_id=AUTHOR_TG,
        )
        self.assertEqual(_value_text(_details(h, _draft_id(h)), FOOTER_CODE), "Текст 1")

        # Устаревший callback (другая ревизия) значение не переписывает.
        await h.callback(stale_pick, user_id=AUTHOR_TG)
        self.assertTrue(h.answer_texts())
        cat.assert_shows(self, h.answer_texts()[-1], "errors.stale.wizard")
        self.assertEqual(_value_text(_details(h, _draft_id(h)), FOOTER_CODE), "Текст 1")

    @scenario_test
    async def test_footer_value_belongs_to_specific_announcement(self, h) -> None:
        place_id = await self._prepare_extras(h)
        _seed_footers(h, place_id, [("F1", "Текст 1"), ("F2", "Текст 2")])
        await _open_extras(h)
        await _press(h, "aw_footch:")
        await h.callback(
            next(data for label, data in h.screen_buttons() if label == "F1" and data),
            user_id=AUTHOR_TG,
        )
        first_draft = _draft_id(h)

        # Второй анонс по тому же шаблону — независимое значение.
        await _open_place_questions(h, place_id)
        await _answer_name(h, "Второй концерт")
        await _press(h, "aw_qskip:")
        second_draft = _draft_id(h)
        await _open_extras(h)
        await _press(h, "aw_footch:")
        await h.callback(
            next(data for label, data in h.screen_buttons() if label == "F2" and data),
            user_id=AUTHOR_TG,
        )

        self.assertNotEqual(first_draft, second_draft)
        self.assertEqual(_value_text(_details(h, first_draft), FOOTER_CODE), "Текст 1")
        self.assertEqual(_value_text(_details(h, second_draft), FOOTER_CODE), "Текст 2")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PersonalFooterTests(unittest.TestCase):
    """§19.5–19.7 для ЛИЧНОГО шаблона: свои справочники, без админа места."""

    @scenario_test
    async def test_personal_single_footer_autofilled_in_wizard(self, h) -> None:
        template_id = _personal_template(h, content="**[название]**\n\n[подвал]")
        _seed_footers(h, None, [("Личный", "Свой текст")])
        await _open_personal_questions(h, template_id)
        await _answer_name(h)

        self.assertNotIn(FOOTER_CHOOSE, h.screen_text())
        self.assertTrue(_callbacks(h, "aw_photo_none:"))
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, FOOTER_CODE), "Свой текст")

    @scenario_test
    async def test_personal_empty_footer_row_has_no_place_admin(self, h) -> None:
        template_id = _personal_template(h, content="**[название]**\n\n[подвал]")
        await _open_personal_questions(h, template_id)
        await _answer_name(h)

        _assert_real_disabled_row(
            self,
            h,
            cat.text("wizard.catalog.no_footers_personal"),
            cat.text("wizard.catalog.no_footers_personal_hint"),
        )
        self.assertNotIn("админ", h.screen_text().lower())
        self.assertFalse(_callbacks(h, "adm_"))
        self.assertTrue(_callbacks(h, "aw_qskip:"))
        await _press(h, "aw_qskip:")
        await h.callback(_callback(h, "aw_photo_none:"), user_id=AUTHOR_TG)
        assert_delivered(h, _draft_id(h))

    @scenario_test
    async def test_personal_footer_extras_insert_and_delete(self, h) -> None:
        # У личного шаблона [подвал] приходит из расширенного текста — это
        # доп. поле, а не вопрос мастера (этап C не меняет личный контракт).
        template_id = _personal_template(
            h,
            content="**[название]**",
            extended="**[название]**\n\n[подвал]",
        )
        await _open_personal_questions(h, template_id)
        await _answer_name(h)
        await _open_extras(h)
        self.assertFalse(_callbacks(h, "aw_footch:"), "справочник пуст — кнопки нет")

        _seed_footers(h, None, [("Личный", "Свой текст")])
        await _open_extras(h)
        await _press(h, "aw_footch:")
        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, FOOTER_CODE), "Свой текст")

        # Один активный и выбран — «Удалить подвал?», затем удаление.
        await _press(h, "aw_footch:")
        self.assertIn(DELETE_CONFIRM, h.screen_text())
        await _press(h, "aw_footdel:")
        details = _details(h, _draft_id(h))
        self.assertEqual(
            _value_status(details, FOOTER_CODE), BrickValueStatus.EMPTY_OPTIONAL
        )
        with h.session_factory() as session:
            footers = selectable_footers(
                session, actor_telegram_user_id=AUTHOR_TG, place_id=None
            )
        self.assertEqual(len(footers), 1, "запись личного справочника не удаляется")

        # Повторный вход не подставляет удалённое значение.
        await _open_extras(h)
        details = _details(h, _draft_id(h))
        self.assertEqual(
            _value_status(details, FOOTER_CODE), BrickValueStatus.EMPTY_OPTIONAL
        )
        self.assertNotIn("Свой текст", h.screen_text())

    @scenario_test
    async def test_personal_several_footers_replace_and_delete(self, h) -> None:
        template_id = _personal_template(
            h,
            content="**[название]**",
            extended="**[название]**\n\n[подвал]",
        )
        _seed_footers(h, None, [("Личный 1", "Текст 1"), ("Личный 2", "Текст 2")])
        await _open_personal_questions(h, template_id)
        await _answer_name(h)
        await _open_extras(h)
        await _press(h, "aw_footch:")

        # Несколько и не выбран — список с «Отменой».
        self.assertIn(FOOTER_CHOOSE, h.screen_text())
        self.assertEqual(len(_callbacks(h, "aw_footpick:")), 2)
        self.assertEqual(_labels(h)[-1], CANCEL_LABEL)
        await h.callback(
            next(
                data
                for label, data in h.screen_buttons()
                if label == "Личный 2" and data
            ),
            user_id=AUTHOR_TG,
        )
        self.assertEqual(_value_text(_details(h, _draft_id(h)), FOOTER_CODE), "Текст 2")

        # Несколько и выбран — другие варианты + «Удалить»/«Отмена»,
        # без вопроса «Удалить подвал?».
        await _press(h, "aw_footch:")
        self.assertIn(FOOTER_CHOOSE_OTHER, h.screen_text())
        self.assertNotIn(DELETE_CONFIRM, h.screen_text())
        self.assertIn("Личный 1", _labels(h))
        self.assertNotIn("Личный 2", _labels(h))
        self.assertTrue(_callbacks(h, "aw_footdel:"))
        self.assertEqual(_labels(h)[-2:], [DELETE_LABEL, CANCEL_LABEL])

        # «Удалить» убирает значение из анонса, запись остаётся в личном
        # справочнике.
        await _press(h, "aw_footdel:")
        self.assertEqual(
            _value_status(_details(h, _draft_id(h)), FOOTER_CODE),
            BrickValueStatus.EMPTY_OPTIONAL,
        )
        with h.session_factory() as session:
            footers = selectable_footers(
                session, actor_telegram_user_id=AUTHOR_TG, place_id=None
            )
        self.assertEqual(
            sorted(footer.name for footer in footers), ["Личный 1", "Личный 2"]
        )


if __name__ == "__main__":  # pragma: no cover — ручной запуск
    unittest.main()
