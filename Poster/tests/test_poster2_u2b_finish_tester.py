# tests/test_poster2_u2b_finish_tester.py
"""
Poster 2.0 U2b / 2.5, независимая проверка управления доставленным анонсом
(poster-tester).

Сценарии написаны заново (не опираются на `tests/test_poster2_u2b_finish.py`)
и проверяют только негативные/граничные случаи:

- подделанный callback правки (`aw_edit` прежней версии или
  `anp_data:{instance_id}`) с чужим/несуществующим идентификатором
  отклоняется;
- чужой пользователь не открывает и не применяет правку чужого анонса (IDOR);
- двойное нажатие «📝 Данные» не создаёт второй экран и не отправляет копию;
- H3: чужая кнопка не может заменить версию (применения по кнопке нет,
  поэтому кнопки «Применить изменения» не существует);
- повторное открытие экранов правки не даёт второй пост;
- устаревшая кнопка прежнего экземпляра не меняет готовый анонс.

Poster 2.5 (§18, коммиты 010a80f/f2091da) убрал экран «✅ Анонс готов»:
после доставки управление живёт у самого анонса (`anp_`: 📝 Данные, 🖼 Фото,
🧩 Доп. поля, 🏠 В меню), а служебный экран конструктора — компактный
редактор (`aw_qmanage`/`aw_photos`/`main_menu`). Поэтому «кнопка правки
доставленного» — это `anp_data:{instance_id}`; callback прежних версий
(`aw_edit:{draft_id}:{revision}`) обязан вести себя как устаревший.

Реальная `post_bot.db`, `.env` и сеть Telegram не используются.
"""

from __future__ import annotations

import unittest

try:
    from sqlalchemy import select

    import announcement_controls as controls
    from announcement_store import get_announcement_draft
    from handlers.announcement_wizard import DELIVERED_EDIT_TEXT
    from models import User
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        inline_buttons,
        is_panel_record,
        scenario_test,
    )
    from tests.test_stage25_delivery_instances import (
        _final_markup,
        _only_active,
    )
    from tests.test_stage27b_announcement_ui import (
        OTHER_TG,
        _callback,
        _callbacks,
        _choose_personal,
        _latest_draft,
        _no_photos,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _ensure_other_user(h) -> None:
    """Второй пользователь: его callback'и не должны управлять чужим анонсом."""
    with h.session_factory() as session:
        if session.scalar(select(User).where(User.telegram_user_id == OTHER_TG)):
            return
        session.add(
            User(
                telegram_user_id=OTHER_TG,
                username="foreign-u2b-finish",
                full_name="Чужой",
            )
        )
        session.commit()


def _instance_controls(h, instance) -> list[tuple[str, str | None]]:
    """Кнопки управления экземпляром: у поста (0–1 фото) или у панели альбома."""
    if instance.panel_message_id is not None:
        for record in h.calls:
            if not is_panel_record(record):
                continue
            if instance.panel_message_id in h.bot.call_message_ids.get(id(record), ()):
                return inline_buttons(record.get("reply_markup"))
        raise AssertionError(
            f"панель {instance.panel_message_id} экземпляра #{instance.id} не найдена"
        )
    return inline_buttons(_final_markup(h, instance.message_ids[0]))


def _instance_button(h, instance, action: str) -> str:
    """
    Реальная кнопка `anp_{action}:{instance_id}` с доставленного анонса.

    Берётся из фактической клавиатуры, а не собирается в тесте: проверяется,
    что продукт отдаёт именно её и привязывает к нужному экземпляру (§32).
    """
    expected = controls.panel_callback(action, instance.id)
    buttons = _instance_controls(h, instance)
    if expected not in {data for _label, data in buttons}:
        raise AssertionError(f"нет кнопки {expected!r}; есть: {buttons}")
    return expected


async def _delivered(h, *, value: str = "Значение"):
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(h)
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text(value)
    await _no_photos(h)
    draft = _latest_draft(h)
    return draft, bricks


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ForeignDeliveredTests(unittest.TestCase):
    """IDOR: чужой пользователь не управляет готовым анонсом."""

    @scenario_test
    async def test_foreign_edit_callback_is_rejected(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        active = _only_active(h, draft.id)
        # §18 (2.5): правка доставленного — кнопка самого анонса, а не
        # прежний экран завершения с `aw_edit`.
        edit = _instance_button(h, active, controls.ACTION_DATA)
        # §18/§32: у самого анонса только действия экземпляра `anp_`, все —
        # на действующий экземпляр; кнопок прежнего экрана завершения нет.
        buttons = _instance_controls(h, active)
        self.assertTrue(buttons)
        for label, data in buttons:
            self.assertTrue(
                data and data.startswith(controls.PANEL_CALLBACK_PREFIX), buttons
            )
            self.assertTrue(data.endswith(f":{active.id}"), f"{label}: {buttons}")
        self.assertEqual(buttons[0][0], "📝 Данные", buttons)
        self.assertFalse(_callbacks(h, "aw_edit"), "экрана завершения нет")
        _ensure_other_user(h)

        screen_before = h.screen_text()
        buttons_before = h.screen_buttons()
        posts_before = h.posts()
        answers_before = len(h.answers())

        await h.callback(edit, user_id=OTHER_TG)

        new_answers = h.answers()[answers_before:]
        self.assertEqual(len(new_answers), 1, "на нажатие нужен ровно один ответ")
        self.assertTrue(new_answers[0].get("show_alert"))
        self.assertEqual(new_answers[0].get("text"), controls.STALE_INSTANCE_TEXT)
        # Черновик не изменился, экран автора не подменён чужой правкой.
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.revision, draft.revision)
        self.assertEqual(h.screen_text(), screen_before, "экран автора не подменён")
        self.assertEqual(h.screen_buttons(), buttons_before)
        self.assertEqual(h.posts(), posts_before)

    @scenario_test
    async def test_foreign_edit_callback_cannot_change_the_version(self, h) -> None:
        """H3: версия меняется сохранением, поэтому чужой ввод — главный риск."""
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        first = _only_active(h, draft.id)
        first_id = first.message_ids[0]
        posts_before = h.posts()
        self.assertEqual(len(posts_before), 1)

        # Автор меняет значение: правка применяется сразу и обновляет
        # активный экземпляр НА МЕСТЕ (§28) — второго сообщения и архивного
        # снимка при том же составе медиа не появляется.
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        # Poster 2.4 (P2): «✏️ Изменить ответ» удалено — экран вопроса сам
        # принимает новый текст.
        await h.text("Своё изменение")
        stored = _details(h, draft.id)
        self.assertIn("Своё изменение", stored.publication_text or "")
        self.assertEqual(
            tuple(stored.delivered_message_ids),
            (first_id,),
            "правка на месте не меняет адрес доставки",
        )
        posts_sent_by_author = h.posts()
        self.assertEqual(len(posts_sent_by_author), 1, "второй копии нет")
        self.assertEqual(h.bot.content_text(h.chat_id, first_id), "Своё изменение")

        active = _only_active(h, draft.id)
        self.assertEqual(active.id, first.id, "активен тот же экземпляр")
        self.assertNotIn(first_id, h.deleted_ids(), "анонс остаётся в чате")
        self.assertIsNotNone(_final_markup(h, first_id), "кнопки живы (§28)")

        # Чужой пользователь пытается открыть правку чужого анонса кнопкой
        # действующего экземпляра и записать своё значение.
        edit = _instance_button(h, active, controls.ACTION_DATA)
        _ensure_other_user(h)
        screen_before = h.screen_text()
        await h.callback(edit, user_id=OTHER_TG)
        self.assertEqual(h.answer_texts()[-1], controls.STALE_INSTANCE_TEXT)
        self.assertEqual(h.screen_text(), screen_before, "чужая правка не открылась")
        await h.text("Чужое изменение", user_id=OTHER_TG)

        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertNotIn("Чужое изменение", stored.publication_text or "")
        self.assertIn("Своё изменение", stored.publication_text or "")
        self.assertEqual(h.posts(), posts_sent_by_author)
        self.assertEqual(_only_active(h, draft.id).id, active.id)

        # Негатив: без чужого вмешательства в чате ровно одна версия.
        self.assertEqual(len(h.posts()), 1)
        self.assertEqual(h.bot.content_text(h.chat_id, first_id), "Своё изменение")

    @scenario_test
    async def test_foreign_open_of_draft_does_not_deliver(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        posts_before = h.posts()
        _ensure_other_user(h)

        await h.callback(f"aw_open:{draft.id}", user_id=OTHER_TG)

        self.assertEqual(h.posts(), posts_before, "чужих постов не появилось")
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ForgedDeliveredCallbackTests(unittest.TestCase):
    """Подделанные и повторные callback'и управления доставленным анонсом."""

    @scenario_test
    async def test_forged_edit_with_unknown_draft_is_rejected(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        posts_before = h.posts()

        await h.callback(f"aw_edit:{draft.id + 500}:{draft.revision}")

        self.assertIn("Данные изменились", h.answer_texts()[-1])
        self.assertEqual(h.posts(), posts_before)
        # Экран остаётся рабочим редактором доставленного анонса.
        self.assertIn(DELIVERED_EDIT_TEXT, h.screen_text())
        self.assertEqual(_details(h, draft.id).revision, draft.revision)

        # Подделанный `anp_data` с несуществующим экземпляром — тот же отказ.
        await h.callback(controls.panel_callback("data", 999_999))
        self.assertEqual(h.answer_texts()[-1], controls.STALE_INSTANCE_TEXT)
        self.assertEqual(h.posts(), posts_before)
        self.assertIn(DELIVERED_EDIT_TEXT, h.screen_text())

    @scenario_test
    async def test_forged_edit_with_wrong_revision_is_rejected(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        posts_before = h.posts()

        await h.callback(f"aw_edit:{draft.id}:{draft.revision + 7}")

        self.assertIn("Данные изменились", h.answer_texts()[-1])
        self.assertEqual(h.posts(), posts_before)
        self.assertIn(DELIVERED_EDIT_TEXT, h.screen_text())
        self.assertEqual(_details(h, draft.id).revision, draft.revision)

    @scenario_test
    async def test_double_edit_keeps_one_screen_and_no_new_post(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        active = _only_active(h, draft.id)
        posts_before = h.posts()
        sends_before = len([c for c in h.screens() if c["type"] == "message"])
        edit = _instance_button(h, active, controls.ACTION_DATA)

        await h.callback(edit)
        first_text = h.screen_text()
        # «📝 Данные» ведёт в компактный редактор (§26): там свои кнопки
        # `aw_qmanage`/`aw_photos`/`main_menu`, а `aw_edit` не появляется.
        self.assertEqual(first_text, DELIVERED_EDIT_TEXT)
        self.assertTrue(_callbacks(h, "aw_qmanage"))
        self.assertTrue(_callbacks(h, "aw_photos"))
        self.assertTrue(_callbacks(h, "main_menu"))
        self.assertFalse(_callbacks(h, "aw_edit"))
        await h.callback(edit)

        self.assertEqual(h.screen_text(), first_text, "экран не задвоился")
        self.assertEqual(
            len([c for c in h.screens() if c["type"] == "message"]),
            sends_before,
            "второго сообщения экрана нет",
        )
        self.assertEqual(h.posts(), posts_before)
        # Порядок сообщений не изменился: служебный экран ниже анонса.
        self.assertGreater(h.screen_message_id(), max(active.message_ids))

    @scenario_test
    async def test_reopening_edit_screens_creates_no_second_post(self, h) -> None:
        """H3: применения по кнопке нет — и повторное открытие не плодит пост."""
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        active = _only_active(h, draft.id)
        version_ids = tuple(_details(h, draft.id).delivered_message_ids)
        posts_before = h.posts()

        for _attempt in range(2):
            await h.callback(_instance_button(h, active, controls.ACTION_DATA))
            await h.callback(_instance_button(h, active, controls.ACTION_PHOTOS))
            await h.callback(_instance_button(h, active, controls.ACTION_DATA))
            await h.callback(_instance_button(h, active, controls.ACTION_MENU))
            self.assertIn("Что хотите сделать?", h.screen_text())

        self.assertEqual(h.posts(), posts_before)
        stored = _details(h, draft.id)
        self.assertEqual(tuple(stored.delivered_message_ids), version_ids)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(_only_active(h, draft.id).id, active.id, "экземпляр тот же")
        self.assertFalse(_callbacks(h, "aw_apply"))

    @scenario_test
    async def test_stale_edit_from_old_screen_changes_nothing(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        first = _only_active(h, draft.id)
        first_id = first.message_ids[0]

        # Кнопка «📝 Данные» прежнего экземпляра и callback прошлой версии
        # экрана запоминаются до переиздания — как их помнит клиент Telegram.
        stale_instance = _instance_button(h, first, controls.ACTION_DATA)
        stale_legacy_edit = f"aw_edit:{draft.id}:{draft.revision}"

        # §28: правка текста идёт на месте — экземпляр и его кнопка остаются
        # действующими, вторая копия в чате не появляется.
        await h.callback(stale_instance)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        # Poster 2.4 (P2): «✏️ Изменить ответ» удалено — экран вопроса сам
        # принимает новый текст.
        await h.text("Новое значение")
        self.assertEqual(h.bot.content_text(h.chat_id, first_id), "Новое значение")
        self.assertEqual(len(h.posts()), 1, "правка на месте не создаёт копию")

        # §29–30: теперь анонс переоткрыт из «Мои анонсы» — отправлен новый
        # цельный экземпляр, прежний стал архивным снимком.
        await h.callback("main_menu")
        await h.callback("aw_start")
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))
        posts_before = h.posts()
        self.assertEqual(len(posts_before), 2)
        # §30 п.4: у прежнего экземпляра клавиатура снята.
        self.assertIsNone(_final_markup(h, first_id))

        await h.callback(stale_instance)
        self.assertEqual(h.answer_texts()[-1], controls.STALE_INSTANCE_TEXT)
        await h.callback(stale_legacy_edit)
        self.assertIn("Данные изменились", h.answer_texts()[-1])

        self.assertEqual(h.posts(), posts_before)
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        # Устаревшая кнопка не откатила и не удвоила сохранённую версию.
        self.assertEqual(stored.publication_text, "Новое значение")
        self.assertEqual(len(h.posts()), 2)
        self.assertEqual(
            _only_active(h, draft.id).message_ids[0],
            tuple(stored.delivered_message_ids)[0],
        )
