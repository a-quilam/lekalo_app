# tests/test_stage261_c_bricks_invite.py
"""
Poster 2.6.1, этап C — два независимых экрана (§5 и §8 задания владельца).

Проверяются ТРЕБОВАНИЯ ВЛАДЕЛЬЦА (`docs/agent_team/task_261_20261110.md`),
а не устройство реализации. Все сценарии идут через настоящее приложение
(`bot.build_application`), `Application.process_update`, FakeBot и временную
SQLite (`tests/integration_harness.py`): рабочая `post_bot.db`, `.env` и сеть
Telegram не используются.

Что закрепляется.

§5 — хаб «Мои кирпичики»: кнопки «🏠 В меню» больше нет, остался ОДИН возврат
«◀️ Назад» (`bk_back`), который возвращает в экран-источник (`hub_origin`):
из конструктора — на тот же шаг, из «Управления» — в «Управление». Кнопки
раздела (создать/предложения/список/архив) не изменились.

§8.1 — точный текст экрана ссылки: четыре абзаца (заголовок, `📍 {название}`,
инструкция организатору, ссылка). Ссылка — отдельным абзацем обычным текстом.

§8.2 — кнопки строго: `[📋 Скопировать]` / `[◀️ К карточке]`. Кнопок
«Создать новую ссылку» и «🏠 В меню» нет; старая кнопка `adm_invite_regen`
получает безопасный STALE и ничего не меняет.

§8.3 — «📋 Скопировать» — настоящая `CopyTextButton` с точным URL текущей
ссылки (тот же, что в тексте), без callback и без перевыпуска.

§8.4 — ссылка ведёт на фактический username работающего бота
(`Bot.username`), хардкода `samplePost_bot`/`lekalo_app` нет.

§8.5 — переход по ссылке сразу открывает вопрос о названии мероприятия
(антирегрессия существующего механизма).

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage261_c_bricks_invite -v
"""

from __future__ import annotations

import unittest

from sqlalchemy import select

from tests import ui_texts_expect as cat

try:
    from brick_state import load_brick_session
    from brick_store import archive_personal_brick, create_personal_brick
    from handlers.announcement_wizard import CONVERSATION_NAME
    from models import PlaceAdmin, PlaceInviteLink, utcnow
    from place_store import create_place
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


INVITED_TG = 8_961_101
MANAGE_TITLE = "⚙️ Управление"
HUB_TITLE = "Мои кирпичики"
BACK_LABEL = "◀️ Назад"

# §8.1: ровно эти четыре абзаца, в этом порядке.
INVITE_TITLE = cat.text("places.invite.title", place="").split("\n")[0]
INVITE_INSTRUCTION = cat.text("places.invite.instruction")
COPY_LABEL = cat.text("buttons.places.invite_copy")
TO_CARD_LABEL = cat.text("buttons.bricks.k_kartochke")


# --- помощники ---------------------------------------------------------------


def _rows(h) -> list[list[tuple[str, str | None]]]:
    """Клавиатура экрана как строки — так её видит автор."""
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _buttons(h) -> list[tuple[str, str | None]]:
    return h.screen_buttons()


def _datas(h) -> list[str]:
    return [data or "" for _label, data in _buttons(h)]


def _labels(h) -> list[str]:
    return [label for label, _data in _buttons(h)]


def _copy_text(h) -> str | None:
    """`CopyTextButton.text` единственной кнопки копирования (None — её нет)."""
    record = h.screen()
    assert record is not None
    markup = record.get("reply_markup")
    for row in markup.inline_keyboard:
        for button in row:
            if button.copy_text is not None:
                return str(button.copy_text.text)
    return None


def _token_from_screen(h) -> str:
    text = h.screen_text()
    marker = "?start="
    if marker not in text:
        raise AssertionError(f"В экране нет ссылки: {text!r}")
    return text.split(marker, 1)[1].split()[0].strip()


def _invites(h) -> list:
    with h.session_factory() as session:
        return list(
            session.scalars(select(PlaceInviteLink).order_by(PlaceInviteLink.id)).all()
        )


def _active_invites(h) -> list:
    return [row for row in _invites(h) if row.revoked_at is None]


def _seed_place(
    h,
    *,
    name: str = "Дом культуры",
    admin_tg: int = DEFAULT_USER_ID,
    admin: bool = True,
) -> int:
    """Площадка со стандартным шаблоном; `admin=True` выдаёт роль админа."""
    with h.session_factory() as session:
        user = get_or_create_user(session, admin_tg, username=f"u{admin_tg}")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
        )
        if admin:
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=user.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
        session.commit()
        return int(place.id)


async def _open_invite_screen(h, place_id: int) -> None:
    """Полная карточка места → «🔗 Ссылка для создания анонса»."""
    await h.text("/start")
    await h.callback("manage_places")
    await h.callback(f"plm_open:{place_id}")
    await h.callback(f"adm_invite:{place_id}")


async def _open_hub(h) -> None:
    """Штатный вход «⚙️ Управление → 🧩 Кирпичики»."""
    await h.text("/start")
    await h.callback("manage")
    await h.callback("manage_bricks")


def _seed_bricks(h) -> tuple[int, int]:
    """Один активный и один архивный кирпичик; возвращает (активный, архивный)."""
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID)
        active = create_personal_brick(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            code="активный_261",
            title="Активный кирпичик",
            value_type="short_text",
        )
        archived = create_personal_brick(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            code="архивный_261",
            title="Архивный кирпичик",
            value_type="short_text",
        )
        archive_personal_brick(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            brick_id=archived.id,
            expected_revision=archived.revision,
        )
        return int(active.id), int(archived.id)


# --- §5: навигация хаба «Мои кирпичики» --------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HubNavigationTests(unittest.TestCase):
    """§5: в хабе один возврат — «◀️ Назад»; кнопки «🏠 В меню» нет."""

    @scenario_test
    async def test_hub_has_no_main_menu_button(self, h) -> None:
        _seed_bricks(h)
        await _open_hub(h)

        self.assertIn(HUB_TITLE, h.screen_text())
        self.assertNotIn("main_menu", _datas(h), "кнопка «🏠 В меню» осталась")
        self.assertNotIn(("🏠 В меню", "main_menu"), _buttons(h))
        self.assertIn((BACK_LABEL, "bk_back"), _buttons(h))

    @scenario_test
    async def test_hub_keeps_other_buttons(self, h) -> None:
        active_id, _archived_id = _seed_bricks(h)
        await _open_hub(h)

        self.assertIn(("➕ Создать кирпичик", "bk_create"), _buttons(h))
        self.assertIn(("📤 Мои предложения", "bp_mine"), _buttons(h))
        self.assertIn(f"bk_open:{active_id}", _datas(h), "строки списка пропали")
        self.assertIn(f"bp_start:{active_id}", _datas(h), "кнопка 📨 пропала")
        self.assertTrue(
            [data for data in _datas(h) if data.startswith("bk_archlist")],
            f"кнопки архива нет: {_buttons(h)}",
        )

    @scenario_test
    async def test_back_from_hub_returns_to_manage(self, h) -> None:
        _seed_bricks(h)
        await _open_hub(h)

        state = load_brick_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state.get("hub_origin"), "manage")

        await h.callback("bk_back")
        self.assertIn(MANAGE_TITLE, h.screen_text())
        self.assertNotIn(HUB_TITLE, h.screen_text())

    @scenario_test
    async def test_back_from_hub_opens_in_wizard_returns_to_step(self, h) -> None:
        """Вход из конструктора: «Назад» из хаба — на тот же шаг, не в меню."""
        from tests.test_stage251_help_return import (
            _choose_personal,
            _seed_personal_template,
            _start_to_template,
        )
        from tests.test_stage251_help_return import (
            _seed_place as _seed_wizard_place,
        )

        await h.text("/start")
        place_id, _alias_id = _seed_wizard_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        self.assertRegex(h.screen_text(), cat.question_re(1))

        await h.callback("bricks")
        self.assertIn(HUB_TITLE, h.screen_text())
        self.assertNotIn("main_menu", _datas(h))
        state = load_brick_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state.get("hub_origin"), "wizard")

        await h.callback("bk_back")
        self.assertRegex(h.screen_text(), cat.question_re(1), "не вернулись на шаг")
        self.assertNotIn(MANAGE_TITLE, h.screen_text())
        self.assertNotIn(HUB_TITLE, h.screen_text())


# --- §8: экран «Ссылка для создания анонса» ----------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InviteScreenTests(unittest.TestCase):
    """§8.1–§8.4: точный текст, кнопки, копирование и username бота."""

    @scenario_test
    async def test_screen_text_is_exact_contract(self, h) -> None:
        place_id = _seed_place(h, name="Дом культуры")
        await _open_invite_screen(h, place_id)

        text = h.screen_text()
        blocks = text.split("\n\n")
        self.assertEqual(len(blocks), 4, f"абзацы §8.1: {blocks!r}")
        self.assertEqual(blocks[0], INVITE_TITLE)
        self.assertEqual(blocks[1], "📍 Дом культуры")
        self.assertEqual(blocks[2], INVITE_INSTRUCTION)
        token = _token_from_screen(h)
        self.assertEqual(blocks[3], f"https://t.me/{h.bot.username}?start={token}")

        # Превью ссылки отключается самим экраном (§8.1).
        record = h.screen()
        assert record is not None
        preview = record.get("link_preview_options")
        self.assertIsNotNone(preview, "экран обязан отключать превью ссылки")
        self.assertTrue(getattr(preview, "is_disabled", False))

    @scenario_test
    async def test_buttons_are_copy_and_back_only(self, h) -> None:
        place_id = _seed_place(h)
        await _open_invite_screen(h, place_id)

        rows = _rows(h)
        self.assertEqual(len(rows), 2, f"кнопки §8.2: {rows!r}")
        self.assertEqual([label for label, _data in rows[0]], [COPY_LABEL])
        self.assertEqual(rows[1], [(TO_CARD_LABEL, "adm_invite_back")])

        # У кнопки копирования нет callback: нажатие не редактирует экран.
        self.assertIsNone(rows[0][0][1])
        self.assertEqual(_labels(h).count(COPY_LABEL), 1)

        # Убранные кнопки отсутствуют полностью.
        self.assertNotIn("adm_invite_regen:", " ".join(_datas(h)))
        self.assertNotIn(("🏠 В меню", "main_menu"), _buttons(h))
        self.assertNotIn("main_menu", _datas(h))

    @scenario_test
    async def test_copy_button_copies_exact_current_link(self, h) -> None:
        place_id = _seed_place(h)
        await _open_invite_screen(h, place_id)

        link = f"https://t.me/{h.bot.username}?start={_token_from_screen(h)}"
        self.assertEqual(_copy_text(h), link, "копируется не та ссылка")

        # §8.3: лимит CopyTextButton — 1..256 символов.
        self.assertGreater(len(link), 0)
        self.assertLessEqual(len(link), 256)

        # Копирование — действие клиента: экран и ссылка не меняются.
        screen_before = h.screen_message_id()
        self.assertEqual(_active_invites(h)[0].token, _token_from_screen(h))
        self.assertEqual(screen_before, h.screen_message_id())

    @scenario_test
    async def test_link_uses_bot_username_not_hardcoded(self, h) -> None:
        """§8.4: username берётся у работающего бота, а не из хардкода."""
        place_id = _seed_place(h)
        await _open_invite_screen(h, place_id)

        text = h.screen_text()
        self.assertIn(f"https://t.me/{h.bot.username}?start=", text)
        self.assertNotIn("samplePost_bot", text)
        self.assertNotIn("lekalo_app", text)
        self.assertEqual(
            _copy_text(h),
            f"https://t.me/{h.bot.username}?start=" + _token_from_screen(h),
        )

    @scenario_test
    async def test_old_regenerate_button_is_stale_and_changes_nothing(self, h) -> None:
        """§8.2: обработчик перевыпуска удалён — старая кнопка безопасна."""
        place_id = _seed_place(h)
        await _open_invite_screen(h, place_id)
        token = _token_from_screen(h)
        before = h.screen_text()
        answers_before = len(h.answers())

        await h.callback(f"adm_invite_regen:{place_id}")

        # Callback обработан (query не «висит») и получил STALE-объяснение.
        self.assertGreater(len(h.answers()), answers_before)
        cat.assert_stale(self, h.answer_texts()[-1])
        # Экран, ссылка и БД не изменились.
        self.assertEqual(h.screen_text(), before)
        self.assertEqual(_token_from_screen(h), token)
        self.assertEqual([row.token for row in _active_invites(h)], [token])
        self.assertEqual(len(_invites(h)), 1, "ссылка не перевыпущена")


# --- §8.5: антирегрессия перехода по ссылке ----------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InviteDeepLinkTests(unittest.TestCase):
    """§8.5: по ссылке сразу вопрос о названии, без меню и выбора площадки."""

    @scenario_test
    async def test_deep_link_opens_first_question_for_exact_place(self, h) -> None:
        place_id = _seed_place(h, name="Дом культуры")
        await _open_invite_screen(h, place_id)
        token = _token_from_screen(h)

        await h.text(f"/start {token}", user_id=INVITED_TG)

        text = h.screen_text()
        self.assertRegex(text, cat.question_re(1))
        self.assertNotIn("Выберите площадку", text)
        self.assertNotIn(INVITE_TITLE, text, "остались на экране ссылки")
        self.assertIsNotNone(
            h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME),
            "конструктор не запущен",
        )

        from announcement_store import list_announcement_drafts

        with h.session_factory() as session:
            drafts = list(
                list_announcement_drafts(session, actor_telegram_user_id=INVITED_TG)
            )
        self.assertEqual([draft.place_id for draft in drafts], [place_id])

    @scenario_test
    async def test_repeat_visit_reuses_same_draft(self, h) -> None:
        place_id = _seed_place(h)
        await _open_invite_screen(h, place_id)
        token = _token_from_screen(h)

        await h.text(f"/start {token}", user_id=INVITED_TG)
        first = h.screen_text()
        await h.text(f"/start {token}", user_id=INVITED_TG)

        self.assertEqual(h.screen_text(), first, "повторный переход сбросил вопрос")

        from announcement_store import list_announcement_drafts

        with h.session_factory() as session:
            drafts = list(
                list_announcement_drafts(session, actor_telegram_user_id=INVITED_TG)
            )
        self.assertEqual([draft.place_id for draft in drafts], [place_id])
        self.assertEqual(len(_active_invites(h)), 1)


if __name__ == "__main__":
    unittest.main()
