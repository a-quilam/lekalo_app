# tests/test_stage25_panel.py
"""
Poster 2.5, этап C3: управление готовым анонсом (`anp_`, §30–33, §58 Е/Ж/З).

Проверяется пользовательский результат через настоящее приложение, FakeBot и
временную SQLite (реальная `post_bot.db`, `.env` и сеть не используются):

- **Е. Маршрутизация панели** — после доставки (0, 1 и 3 фото) каждая кнопка
  `anp_{action}:{id}` открывает свой экран: «Данные» — компактный редактор,
  «Фото» — коллекция вложений, «Доп. поля» — список доп. полей, «В меню» —
  главное меню. На каждое нажатие приходит ровно один `answer_callback_query`
  без alert, без лишних сообщений, удалений и повторной публикации. Набор
  действий отдельной панели медиагруппы совпадает с клавиатурой сообщения.
- **Ж. Старый callback** — после переиздания прежний экземпляр архивный:
  нажатие получает alert `errors.stale.instance`, не правит экраны, не пишет
  в БД и не меняет статусы экземпляров. Несуществующий id ведёт себя так же.
- **З. Рестарт службы** — кнопки активного экземпляра работают без состояния
  диалога: экземпляр восстанавливается из БД. Удаление черновика отключает
  управление: в Harness без `PRAGMA foreign_keys` строка экземпляра
  переживает удаление, и защиту даёт проверка доступности черновика
  (`_details`); в production каскад уносит строку (проверяется отдельно).
"""

from __future__ import annotations

import re
import unittest
from typing import TYPE_CHECKING

from sqlalchemy import event

try:
    import announcement_controls as controls
    from announcement_instances import get_instance
    from announcement_store import (
        AnnouncementNotFoundError,
        delete_announcement,
        get_announcement_draft,
    )
    from handlers.announcement_wizard import AW_EXTRAS, AW_PREVIEW, CONVERSATION_NAME
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        inline_buttons,
        is_panel_record,
        is_post_message,
        is_screen_record,
        scenario_test,
    )
    from tests.test_stage25_acceptance_c import _assert_field_list
    from tests.test_stage25_delivery_instances import (
        ANNOUNCEMENT_TEXT,
        _delivered_with_photos,
        _delivered_without_photos,
        _final_markup,
        _instances,
        _only_active,
        _stored,
    )
    from tests.test_stage27b_announcement_ui import _callback

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# Заголовки экранов, к которым ведут действия панели (§18/§26–27/§34).
# 2.5.3 §5: экрана «✏️ Изменить анонс» больше нет — «📝 Текстовые поля»
# открывают список полей на самом управлении анонса.
FIELDS_TITLE = "📝 Текстовые поля"
PHOTOS_TITLE = "📷 Фотографии анонса"
EXTRAS_TITLE = "🧩 Дополнительные поля"
MAIN_MENU_TITLE = "Здравствуйте! 👋"

# §27: набор действий ОДИН и тот же у клавиатуры сообщения и у панели
# медиагруппы. Счётчик фото в подписи кнопки — единственное отличие.
EXPECTED_PANEL_ACTIONS = (
    ("📝 Текстовые поля", "anp_data"),
    ("🖼 Фото (n)", "anp_photos"),
    ("🧩 Дополнительные поля", "anp_extras"),
    ("🏠 В меню", "anp_menu"),
)

# Экран и состояние диалога после действия панели (§24/§30–33).
# `None` — отдельного экрана нет: список полей живёт на управлении анонса
# (2.5.3 §5), поэтому проверяется живая клавиатура чата.
PANEL_EXPECTATIONS = {
    "data": (None, AW_PREVIEW),
    "photos": (PHOTOS_TITLE, AW_PREVIEW),
    "extras": (EXTRAS_TITLE, AW_EXTRAS),
    "menu": (MAIN_MENU_TITLE, None),
}

# Типы вызовов, которых не должно быть у устаревшей кнопки (§32): никаких
# правок экранов, новых сообщений и удалений.
FORBIDDEN_STALE_CALLS = frozenset(
    {
        "message",
        "photo",
        "media_group",
        "edit_message",
        "edit_caption",
        "edit_media",
        "edit_markup",
        "delete_message",
    }
)


# --- помощники -------------------------------------------------------------


def _instance_buttons(h, instance) -> list[tuple[str, str | None]]:
    """Кнопки управления именно этого экземпляра: панель или само сообщение."""
    if instance.panel_message_id is not None:
        for record in h.calls:
            if not is_panel_record(record):
                continue
            ids = h.bot.call_message_ids.get(id(record), ())
            if instance.panel_message_id in ids:
                return inline_buttons(record.get("reply_markup"))
        raise AssertionError(
            f"панель {instance.panel_message_id} экземпляра #{instance.id} не найдена"
        )
    return inline_buttons(_final_markup(h, instance.message_ids[0]))


def _panel_layout(buttons) -> list[tuple[str, str, str]]:
    """
    (подпись, действие, id экземпляра) каждой кнопки управления.

    Счётчик фото в подписи нормализуется: у 0/1/3 фото различается только он.
    """
    layout: list[tuple[str, str, str]] = []
    for label, data in buttons:
        action, _colon, raw_id = (data or "").partition(":")
        layout.append((re.sub(r"\(\d+\)", "(n)", label), action, raw_id))
    return layout


def _button_data(buttons, action: str) -> str:
    prefix = f"{controls.PANEL_CALLBACK_PREFIX}{action}:"
    for _label, data in buttons:
        if data and data.startswith(prefix):
            return data
    raise AssertionError(f"нет кнопки {prefix!r}; есть: {buttons}")


if TYPE_CHECKING:
    # Миксин вызывается только вместе с unittest.TestCase (см. классы ниже);
    # для mypy база даёт методы assert*, в рантайме базой остаётся object.
    _PanelTestBase = unittest.TestCase
else:
    _PanelTestBase = object


class PanelTestMixin(_PanelTestBase):
    """Общие проверки ответа на callback и состава чата."""

    async def _assert_stale(
        self,
        h,
        instance_id: int,
        draft_id: int,
        *,
        draft_present: bool = True,
    ) -> None:
        """
        Устаревшая кнопка: только alert, без правок экранов и БД (§32).

        Проверяется фактический чат (FakeBot), состояние экземпляров и текст
        черновика: старая кнопка не воскрешает прежнюю версию и не открывает
        редактор. `draft_present=False` — запись уже удалена (§33): тогда
        сохраняется только «данных анонса больше нет».
        """
        for action in controls.PANEL_ACTIONS:
            await self._assert_stale_action(
                h,
                action,
                instance_id,
                draft_id,
                draft_present=draft_present,
            )

    async def _assert_stale_action(
        self,
        h,
        action: str,
        instance_id: int,
        draft_id: int,
        *,
        draft_present: bool,
    ) -> None:
        statuses_before = [
            (item.id, item.status, item.generation, item.message_ids)
            for item in _instances(h, draft_id)
        ]
        publication_before = (
            _stored(h, draft_id).publication_text if draft_present else None
        )
        screen_before = h.screen_text()
        posts_before = h.posts()
        calls_before = len(h.calls)
        answers_before = len(h.answers())

        await h.callback(controls.panel_callback(action, instance_id))

        new_calls = h.calls[calls_before:]
        forbidden = [
            call["type"] for call in new_calls if call["type"] in FORBIDDEN_STALE_CALLS
        ]
        self.assertEqual(
            forbidden,
            [],
            f"{action}: устаревшая кнопка что-то сделала: {new_calls}",
        )
        new_answers = h.answers()[answers_before:]
        self.assertEqual(
            len(new_answers), 1, f"{action}: на нажатие нужен ровно один ответ"
        )
        self.assertTrue(
            new_answers[0].get("show_alert"), f"{action}: нужен alert, а не тост"
        )
        self.assertEqual(
            new_answers[0].get("text"),
            controls.STALE_INSTANCE_TEXT,
            f"{action}: текст пояснения — errors.stale.instance",
        )
        self.assertEqual(
            h.screen_text(), screen_before, f"{action}: экран не должен меняться"
        )
        self.assertEqual(h.posts(), posts_before, f"{action}: посты не меняются")
        self.assertEqual(
            [
                (item.id, item.status, item.generation, item.message_ids)
                for item in _instances(h, draft_id)
            ],
            statuses_before,
            f"{action}: статусы экземпляров не меняются",
        )
        if draft_present:
            self.assertEqual(
                _stored(h, draft_id).publication_text,
                publication_before,
                f"{action}: данные анонса не меняются",
            )
        else:
            with h.session_factory() as session:
                with self.assertRaises(AnnouncementNotFoundError):
                    get_announcement_draft(
                        session,
                        actor_telegram_user_id=DEFAULT_USER_ID,
                        draft_id=draft_id,
                    )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PanelRoutingTests(PanelTestMixin, unittest.TestCase):
    """§18/§27/§58 Е: каждая кнопка готового анонса ведёт на свой экран."""

    async def _assert_panel_routing(self, h, draft) -> None:
        active = _only_active(h, draft.id)
        buttons = _instance_buttons(h, active)
        layout = _panel_layout(buttons)
        # Набор действий одинаков у клавиатуры сообщения и у панели; id —
        # действующего экземпляра (§32: привязка к конкретной версии).
        self.assertEqual(
            [(label, action) for label, action, _raw in layout],
            list(EXPECTED_PANEL_ACTIONS),
            f"набор действий панели: {buttons}",
        )
        self.assertEqual(
            {raw for _label, _action, raw in layout},
            {str(active.id)},
            "все кнопки привязаны к активному экземпляру",
        )
        # §27: панель медиагруппы и клавиатура на сообщении — одна и та же
        # раскладка общего компонента; различается только счётчик фото.
        self.assertEqual(
            layout,
            _panel_layout(
                inline_buttons(
                    controls.panel_keyboard(
                        active.id, photos_count=len(_stored(h, draft.id).photos)
                    )
                )
            ),
            "панель и клавиатура сообщения используют одну раскладку",
        )

        # «Данные» → «Фото» → «Данные» → «Доп. поля» → «Данные»:
        # проверяется и прямая навигация, и возврат из каждого экрана.
        # «🏠 В меню» здесь не нажимается: выход архивирует экземпляр (§2),
        # и после него эти же кнопки обязаны стать no-op — это отдельный
        # тест `tests/test_stage252_single_control.py`.
        for action in ("data", "photos", "data", "extras", "data"):
            await self._press(h, buttons, action)

        self.assertEqual(_only_active(h, draft.id).id, active.id, "экземпляр тот же")

    async def _press(self, h, buttons, action: str) -> None:
        """
        Нажатие одной кнопки панели с проверкой ответа и состава чата.

        2.5.2 §2: служебных экранов у анонса нет, поэтому первый вход в
        редактор («Фото»/«Доп. поля») создаёт ВРЕМЕННЫЙ экран — это
        допустимо. Новых постов и лишних удалений быть не должно. 2.5.3 §5:
        «📝 Текстовые поля» вообще не создают сообщений — список живёт на
        клавиатуре анонса или в тексте его единственной панели.
        """
        data = _button_data(buttons, action)
        calls_before = len(h.calls)
        answers_before = len(h.answers())
        deleted_before = list(h.deleted_ids())
        posts_before = list(h.posts())

        await h.callback(data)

        new_calls = h.calls[calls_before:]
        created = [
            call
            for call in new_calls
            if call["type"] in {"message", "photo", "media_group"}
            and not is_screen_record(call)
            and not is_panel_record(call)
        ]
        self.assertEqual(created, [], f"{action}: лишние сообщения в чате")
        # 2.5.3 §5: возврат к списку полей закрывает временный экран
        # («Фото»/«Доп. поля»), иначе в чате осталась бы вторая клавиатура.
        # Удаляться могут только служебные экраны — не анонс и не панель.
        newly_deleted = set(h.deleted_ids()) - set(deleted_before)
        for message_id in newly_deleted:
            creator = next(
                (
                    call
                    for call in h.calls
                    if int(message_id)
                    in {int(mid) for mid in h.bot.call_message_ids.get(id(call), ())}
                ),
                None,
            )
            self.assertIsNotNone(
                creator, f"{action}: удалено чужое сообщение {message_id}"
            )
            self.assertTrue(
                is_screen_record(creator),
                f"{action}: удалено не служебное сообщение ({message_id})",
            )
        self.assertEqual(h.posts(), posts_before, f"{action}: анонс переиздан")

        new_answers = h.answers()[answers_before:]
        self.assertEqual(len(new_answers), 1, f"{action}: нужен ровно один ответ")
        self.assertFalse(new_answers[0].get("show_alert"), f"{action}: alert не нужен")
        self.assertFalse(new_answers[0].get("text"), f"{action}: текст не нужен")

        title, state = PANEL_EXPECTATIONS[action]
        if title is None:
            # 2.5.3 §5: список текстовых полей — на самом управлении анонса.
            _assert_field_list(h)
            self.assertEqual(h.screen_text(), "", "служебный экран не создаётся")
        else:
            self.assertTrue(
                h.screen_text().startswith(title),
                f"{action}: ожидался экран {title!r}, "
                f"получено {h.screen_text()[:80]!r}",
            )
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), state)

    @scenario_test
    async def test_zero_photos_panel_routing(self, h) -> None:
        """§20: без фото управление живёт на самом сообщении анонса."""
        await h.text("/start")
        await self._assert_panel_routing(h, await _delivered_without_photos(h))

    @scenario_test
    async def test_one_photo_panel_routing(self, h) -> None:
        """§21: одно фото — кнопки на том же сообщении с подписью."""
        await h.text("/start")
        await self._assert_panel_routing(h, await _delivered_with_photos(h, 1))

    @scenario_test
    async def test_album_panel_routing(self, h) -> None:
        """§22: у медиагруппы — отдельная панель, действия те же."""
        await h.text("/start")
        await self._assert_panel_routing(h, await _delivered_with_photos(h, 3))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StalePanelCallbackTests(PanelTestMixin, unittest.TestCase):
    """§30–32/§58 Ж: старая кнопка не открывает редактор и не меняет данные."""

    @scenario_test
    async def test_archived_instance_button_only_alerts(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        first = _only_active(h, draft.id)

        # §29–30 (2.5): правка значения идёт НА МЕСТЕ и экземпляр не меняет;
        # архивный снимок образуется только при переиздании — повторным
        # открытием из «Мои анонсы» (`aw_open`).
        await h.callback("main_menu")
        await h.callback("my_announcements")
        await h.callback(_callback(h, "aw_open"))

        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id, "переиздание выпустило версию")
        self.assertEqual(active.generation, first.generation + 1)
        self.assertEqual(
            [item for item in _instances(h, draft.id) if item.id == first.id][0].status,
            "archived",
        )

        await self._assert_stale(h, first.id, draft.id)

    @scenario_test
    async def test_unknown_instance_button_only_alerts(self, h) -> None:
        """§33: записи нет — то же пояснение, никаких действий."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        await self._assert_stale(h, 999_999, draft.id)

    @scenario_test
    async def test_button_from_foreign_chat_only_alerts(self, h) -> None:
        """§32: кнопка пересланного анонса в другом чате редактор не открывает."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)

        screen_before = h.screen_text()
        calls_before = len(h.calls)
        answers_before = len(h.answers())
        await h.callback(
            controls.panel_callback("data", active.id),
            chat_id=h.chat_id + 1,
        )

        new_calls = h.calls[calls_before:]
        self.assertEqual(
            [call["type"] for call in new_calls],
            ["callback_answer"],
            f"чужой чат получил действия: {new_calls}",
        )
        new_answers = h.answers()[answers_before:]
        self.assertEqual(len(new_answers), 1)
        self.assertTrue(new_answers[0].get("show_alert"))
        self.assertEqual(new_answers[0].get("text"), controls.STALE_INSTANCE_TEXT)
        self.assertEqual(h.screen_text(), screen_before)
        self.assertEqual(_only_active(h, draft.id).id, active.id)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RestartPanelTests(PanelTestMixin, unittest.TestCase):
    """§32/§58 З: управление экземпляром выживает рестарт и удаление записи."""

    @scenario_test
    async def test_panel_works_after_restart(self, h) -> None:
        """Состояния диалога нет, экземпляр и его адреса — в БД."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 3)
        active = _only_active(h, draft.id)

        await h.restart()
        self.assertIsNone(
            h.dialog_state(name=CONVERSATION_NAME),
            "после рестарта состояния диалога нет",
        )
        with h.session_factory() as session:
            restored = get_instance(session, active.id)
        self.assertIsNotNone(restored, "экземпляр читается из БД после рестарта")
        self.assertEqual(restored, active, "адреса сообщений и панели не изменились")

        buttons = _instance_buttons(h, restored)
        self.assertEqual(
            [(label, action) for label, action, _raw in _panel_layout(buttons)],
            list(EXPECTED_PANEL_ACTIONS),
        )

        for action in ("data", "photos", "extras", "menu"):
            await self._press_after_restart(h, buttons, action)

    async def _press_after_restart(self, h, buttons, action: str) -> None:
        """
        Нажатие после рестарта: действие работает, лишнего контента нет.

        Первый вход в UI после рестарта снимает легаси reply-клавиатуру
        отдельным служебным сообщением (оно сразу удаляется), поэтому
        проверяется «нет постов», а не «нет сообщений вообще».
        """
        data = _button_data(buttons, action)
        calls_before = len(h.calls)
        answers_before = len(h.answers())
        posts_before = list(h.posts())

        await h.callback(data)

        new_calls = h.calls[calls_before:]
        new_answers = h.answers()[answers_before:]
        self.assertEqual(len(new_answers), 1, f"{action}: нужен ровно один ответ")
        self.assertFalse(new_answers[0].get("show_alert"), f"{action}: alert не нужен")
        self.assertEqual(h.posts(), posts_before, f"{action}: анонс переиздан")

        deleted = set(h.deleted_ids())
        for call in new_calls:
            if call["type"] not in {"message", "photo", "media_group"}:
                continue
            if is_screen_record(call):
                # 2.5.2 §2: экран редактора — временный и живёт до
                # успешного сохранения/выхода в меню. Это не второй анонс.
                continue
            self.assertFalse(
                is_post_message(call), f"{action}: в чате появился новый пост"
            )
            ids = set(h.bot.call_message_ids.get(id(call), ()))
            self.assertTrue(
                ids <= deleted,
                f"{action}: служебное сообщение осталось в чате (ids={ids})",
            )

        title, state = PANEL_EXPECTATIONS[action]
        if title is None:
            # 2.5.3 §5: список текстовых полей — на самом управлении анонса,
            # отдельного экрана после рестарта тоже не создаётся.
            _assert_field_list(h)
            self.assertEqual(h.screen_text(), "", "служебный экран не создаётся")
        else:
            self.assertTrue(
                h.screen_text().startswith(title),
                f"{action}: ожидался экран {title!r}, "
                f"получено {h.screen_text()[:80]!r}",
            )
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), state)

    @scenario_test
    async def test_deleted_draft_button_only_alerts(self, h) -> None:
        """
        §33: запись удалена — управление отключено, сообщения остались.

        В Harness `PRAGMA foreign_keys` не включён (как и в старой локальной
        сборке), поэтому строка экземпляра переживает удаление черновика.
        Пользователь всё равно получает alert: обработчик проверяет
        доступность черновика (`_details`) перед открытием редактора, а не
        доверяет одной строке `announcement_instances`.
        """
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)

        with h.session_factory() as session:
            delete_announcement(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        with h.session_factory() as session:
            with self.assertRaises(AnnouncementNotFoundError):
                get_announcement_draft(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    draft_id=draft.id,
                )
        with h.session_factory() as session:
            survivor = get_instance(session, active.id)
        self.assertIsNotNone(
            survivor,
            "без PRAGMA foreign_keys строка экземпляра переживает удаление",
        )
        assert survivor is not None
        self.assertEqual(survivor.status, "active")
        # Фактическое поведение Harness: статус не менялся, но кнопка уже
        # не работает — защиту даёт проверка доступности черновика.
        await self._assert_stale(h, active.id, draft.id, draft_present=False)
        self.assertEqual(
            h.posts(),
            [ANNOUNCEMENT_TEXT],
            "историческое сообщение анонса остаётся в чате",
        )

    @scenario_test
    async def test_foreign_keys_cascade_removes_instance(self, h) -> None:
        """
        Production-схема: `PRAGMA foreign_keys=ON` уносит экземпляр каскадом.

        Отдельный тест фиксирует, что расхождение с Harness — только режим
        SQLite: сама схема объявляет `ON DELETE CASCADE`, и после включения
        PRAGMA (как в `database.py`) старая кнопка находит уже не строку, а
        пустоту — тот же alert.
        """
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)

        # Пул соединений создаёт новые подключения — только на них
        # действует PRAGMA foreign_keys (он не транзакционный).
        h.engine.dispose()

        def _enable_foreign_keys(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        event.listen(h.engine, "connect", _enable_foreign_keys)
        try:
            with h.session_factory() as session:
                delete_announcement(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    draft_id=draft.id,
                )
            with h.session_factory() as session:
                self.assertIsNone(
                    get_instance(session, active.id),
                    "каскад FK удаляет строку экземпляра вместе с черновиком",
                )
        finally:
            event.remove(h.engine, "connect", _enable_foreign_keys)

        screen_before = h.screen_text()
        answers_before = len(h.answers())
        await h.callback(controls.panel_callback("data", active.id))
        new_answers = h.answers()[answers_before:]
        self.assertEqual(len(new_answers), 1)
        self.assertTrue(new_answers[0].get("show_alert"))
        self.assertEqual(new_answers[0].get("text"), controls.STALE_INSTANCE_TEXT)
        self.assertEqual(h.screen_text(), screen_before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
