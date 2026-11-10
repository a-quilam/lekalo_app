# tests/test_stage261_b_my_announcements.py
"""
Poster 2.6.1, этап B — независимая приёмка «Мои анонсы» (§2).

Проверяются ТРЕБОВАНИЯ ВЛАДЕЛЬЦА (`docs/agent_team/task_261_20261110.md`,
§2.1–2.6, приёмка §11.2), а не устройство реализации. Все сценарии идут
через настоящее приложение (`bot.build_application`),
`Application.process_update`, FakeBot и временную SQLite
(`tests/integration_harness.py`): рабочая `post_bot.db`, `.env` и сеть
Telegram не используются.

Что закрепляется.

§2.2–2.3 — выбор ГОТОВОГО анонса в «Мои анонсы» сразу отправляет новую
копию (полный текст + актуальные значения кирпичиков) и открывает главный
редактор. Промежуточного экрана «Обзор анонса» и кнопки «Обновить» нет;
каждое открытие — ещё одна копия, прежние не удаляются и не правятся.

§2.6 — дефект «Сначала завершите обзор анонса» воспроизводится не на
«чистом» анонсе, а после работы с его полями: правка доп. поля возвращает
черновик на промежуточный этап (`extras`/`questions`), и повторная сборка
падала на проверке этапа `begin_announcement_publication`. Сохранённый
НЕдоставленный черновик продолжает свой этап мастера — первичный обзор при
создании не удалён (антирегресс §2.6, последний абзац).

§2.6 — устаревшие callback удалённого экрана «Обновить» обрабатываются
безопасно: без падения, без удаления сообщений, без пустого экрана.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage261_b_my_announcements -v
"""

from __future__ import annotations

import unittest

from tests import ui_texts_expect as cat

try:
    from announcement_store import WizardStage, get_announcement_draft
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        assert_delivered,
        scenario_test,
    )
    from tests.test_stage261_a_wizard_editor import (
        DATA_LABEL,
        EXTRAS_LABEL,
        MENU_LABEL,
        PHOTOS_LABEL,
        _control_rows,
        _deliver,
        _latest_draft,
        _open_place_questions,
        _place,
        _seed_standard,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# Формулировки удалённого сценария повторного открытия (§2.2, §11.2).
REMOVED_PREVIEW_TITLE = "Обзор анонса"
REMOVED_REFRESH = "Обновить"
STAGE_ERROR_FRAGMENT = "Сначала завершите обзор анонса"

# Приёмочные пункты §11.2, которые проверяются на экране и в чате.
STALE_KEY = "errors.stale.wizard"


def _details(h, draft_id: int):
    """Свежие детали черновика из БД."""
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _stage(h, draft_id: int) -> str:
    return _details(h, draft_id).stage.value


def _screen_texts(h) -> list[str]:
    return h.screen_texts()


def _assert_no_preview_screen(case, h, *, where: str, since: int = 0) -> None:
    """
    §2.2/§11.2: «Обзор анонса» и «Обновить» не появляются.

    `since` — индекс, с которого смотреть историю экранов: прежние экраны
    (например, обзор, с которого ушёл пользователь) в чате остаются, и
    проверять их как «новый экран» нельзя.
    """
    texts = _screen_texts(h)[since:]
    for text in texts:
        case.assertNotIn(
            REMOVED_PREVIEW_TITLE,
            text or "",
            f"{where}: промежуточный экран «Обзор анонса»: {text!r}",
        )
    labels = [label for label, _data in h.live_buttons()]
    case.assertNotIn(
        REMOVED_REFRESH,
        labels,
        f"{where}: кнопка «Обновить» осталась: {labels}",
    )
    callbacks = [data for _label, data in h.live_buttons() if data]
    case.assertFalse(
        [data for data in callbacks if data.startswith("aw_preview:")],
        f"{where}: callback удалённого экрана обзора: {callbacks}",
    )


def _assert_no_stage_error(case, h, *, where: str) -> None:
    """§2.6/§11.2: ошибки «Сначала завершите обзор анонса» больше нет."""
    for text in _screen_texts(h):
        case.assertNotIn(
            STAGE_ERROR_FRAGMENT,
            text or "",
            f"{where}: дефект повторного открытия не исправлен: {text!r}",
        )
    for answer in h.answer_texts():
        case.assertNotIn(
            STAGE_ERROR_FRAGMENT,
            answer or "",
            f"{where}: ошибка этапа в коротком ответе: {answer!r}",
        )


async def _deliver_standard_with_extras(h) -> int:
    """
    Готовый стандартный анонс с доступными доп. полями.

    Именно такой анонс воспроизводит дефект владельца: после правки доп.
    поля черновик остаётся на промежуточном этапе, а данные уже собраны и
    доставлены.
    """
    await h.text("/start")
    place_id = await _place(h)
    _seed_standard(
        h,
        place_id,
        content="Название: [название]",
        extended_content="Название: [название]\nКуратор: [куратор]",
        extended_active=True,
    )
    await _open_place_questions(h, place_id)
    draft_id, _screens = await _deliver(h)
    return draft_id


async def _open_extras_from_editor(h) -> None:
    """Открывает доп. поля с главного редактора готового анонса."""
    data = [
        data
        for _label, data in h.live_buttons()
        if data and data.startswith("anp_extras:")
    ]
    if not data:
        raise AssertionError(f"нет кнопки доп. полей: {h.live_buttons()}")
    await h.callback(data[0])


async def _back_to_editor(h) -> None:
    """Возврат из доп. полей в главный редактор (если кнопка есть)."""
    data = [
        data
        for _label, data in h.live_buttons()
        if data and data.startswith(("anf_back:", "aw_exback:"))
    ]
    if not data:
        raise AssertionError(f"нет возврата из доп. полей: {h.live_buttons()}")
    await h.callback(data[0])


async def _reopen_from_list(h, draft_id: int) -> None:
    """«Мои анонсы» → выбор сохранённого анонса (путь владельца)."""
    await h.callback("main_menu")
    await h.callback("my_announcements")
    await h.callback(f"aw_open:{draft_id}")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MyAnnouncementsSelectionTests(unittest.TestCase):
    """§2.2–2.3: выбор готового анонса сразу отправляет новую копию."""

    @scenario_test
    async def test_delivered_reopen_after_extras_sends_copy_without_preview(
        self, h
    ) -> None:
        """
        Дефект владельца: доставленный анонс → доп. поля → назад → выбор.

        Ожидание: сразу новая копия и главный редактор, без «Обзора анонса»,
        «Обновить» и без «Сначала завершите обзор анонса».
        """
        draft_id = await _deliver_standard_with_extras(h)
        posts_before = len(h.posts())
        self.assertEqual(posts_before, 1, f"первая доставка: {h.posts()}")
        delivery_ids = tuple(_details(h, draft_id).delivered_message_ids)

        # Правка полей уводит этап с `completed` — так дефект и возникает.
        await _open_extras_from_editor(h)
        self.assertNotEqual(
            _stage(h, draft_id),
            WizardStage.COMPLETED.value,
            "этап не сдвинулся: сценарий дефекта не воспроизведён",
        )
        await _back_to_editor(h)

        await _reopen_from_list(h, draft_id)

        _assert_no_preview_screen(self, h, where="повторное открытие")
        _assert_no_stage_error(self, h, where="повторное открытие")
        self.assertEqual(len(h.posts()), posts_before + 1, "новая копия не отправлена")
        self.assertEqual(
            h.posts()[-1], h.posts()[0], "копия отличается от доставленного текста"
        )
        details = _details(h, draft_id)
        self.assertTrue(details.publication_sent)
        self.assertNotEqual(
            tuple(details.delivered_message_ids),
            delivery_ids,
            "активной осталась прежняя версия",
        )
        self.assertNotIn(
            delivery_ids[0],
            h.deleted_ids(),
            "прежняя копия удалена",
        )
        # §2.5: главный редактор открыт — кнопки живут у НОВОГО экземпляра.
        rows = _control_rows(h)
        instance = h.control_callback("data")
        self.assertIsNotNone(instance, "главный редактор не открыт")
        instance_id = int((instance or "").split(":")[-1])
        self.assertEqual(
            rows,
            [
                [
                    (DATA_LABEL, f"anp_data:{instance_id}"),
                    (PHOTOS_LABEL, f"anp_photos:{instance_id}"),
                ],
                [(EXTRAS_LABEL, f"anp_extras:{instance_id}")],
                [(MENU_LABEL, f"anp_menu:{instance_id}")],
            ],
            f"главный редактор не по контракту §2.5: {rows}",
        )

    @scenario_test
    async def test_each_selection_sends_another_copy_and_keeps_old_ones(
        self, h
    ) -> None:
        """§2.3: каждое открытие — новая копия, прежние неизменны."""
        draft_id = await _deliver_standard_with_extras(h)
        first_ids = tuple(_details(h, draft_id).delivered_message_ids)
        first_text = h.posts()[0]

        await _reopen_from_list(h, draft_id)
        second_ids = tuple(_details(h, draft_id).delivered_message_ids)
        await _reopen_from_list(h, draft_id)
        third_ids = tuple(_details(h, draft_id).delivered_message_ids)

        self.assertEqual(len(h.posts()), 3, f"копий: {h.posts()}")
        self.assertEqual(
            len({first_ids, second_ids, third_ids}), 3, "версии не различаются"
        )
        for message_id in (*first_ids, *second_ids, *third_ids):
            self.assertNotIn(message_id, h.deleted_ids(), "копия удалена")

        # Первая копия не изменилась и не была переиспользована (§2.3).
        self.assertEqual(
            h.bot.content_text(h.chat_id, first_ids[0]),
            first_text,
            "содержимое первой копии изменилось",
        )
        _assert_no_preview_screen(self, h, where="третье открытие")
        _assert_no_stage_error(self, h, where="третье открытие")

    @scenario_test
    async def test_copy_contains_current_brick_values(self, h) -> None:
        """§2.2: копия собрана из АКТУАЛЬНЫХ сохранённых значений полей."""
        draft_id = await _deliver_standard_with_extras(h)
        before = _details(h, draft_id)
        self.assertTrue(before.publication_text)

        # Меняем значение поля через редактор доставленного анонса.
        await _reopen_from_list(h, draft_id)
        await h.callback(_control(h, "data"))
        field = h.live_callbacks("anf_sel:")
        if not field:
            raise AssertionError(f"список полей не открыт: {h.live_buttons()}")
        await h.callback(field[0])
        await h.text("Новое название")
        current = _details(h, draft_id)
        self.assertIn(
            "Новое название", current.publication_text or "", "правка не применена"
        )

        # Следующее открытие отправляет копию с новым значением.
        await _reopen_from_list(h, draft_id)
        self.assertIn(
            "Новое название",
            h.posts()[-1] or "",
            f"копия собрана из старых данных: {h.posts()}",
        )
        self.assertNotEqual(current.publication_text, before.publication_text)
        _assert_no_preview_screen(self, h, where="открытие после правки")


def _control(h, action: str) -> str:
    data = h.control_callback(action)
    if data is None:
        raise AssertionError(f"нет кнопки {action!r}: {h.live_buttons()}")
    return data


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SavedDraftTests(unittest.TestCase):
    """§2.2: сохранённый готовый черновик при выборе отправляется копией."""

    @scenario_test
    async def test_saved_draft_selection_delivers_copy_without_preview(self, h) -> None:
        """
        §2.2/§2.6: выбор сохранённого черновика — копия и главный редактор.

        Черновик сохранён «✅ Сохранить черновик» (обзор уже пройден): при
        выборе из «Мои анонсы» бот сразу отправляет копию с текстом анонса и
        не показывает «Обзор анонса» / «Обновить» / ошибку этапа.
        """
        await h.text("/start")
        await _place(h)
        draft_id = await _saved_preview_draft(h)
        values_before = tuple(
            (value.brick_id, value.status, value.payload)
            for value in _details(h, draft_id).values
        )

        await _reopen_from_list(h, draft_id)

        _assert_no_preview_screen(self, h, where="сохранённый черновик")
        _assert_no_stage_error(self, h, where="сохранённый черновик")
        details = _details(h, draft_id)
        self.assertTrue(details.publication_sent, "копия не отправлена")
        self.assertEqual(
            h.posts(), [details.publication_text], f"текст копии: {h.posts()}"
        )
        # §2.6: данные не пересобраны и не потеряны.
        self.assertEqual(
            tuple(
                (value.brick_id, value.status, value.payload)
                for value in details.values
            ),
            values_before,
            "значения черновика изменились",
        )
        # §2.5: главный редактор простого анонса без доп. полей.
        rows = _control_rows(h)
        instance_id = int((h.control_callback("data") or "").split(":")[-1])
        self.assertEqual(
            rows,
            [
                [
                    (DATA_LABEL, f"anp_data:{instance_id}"),
                    (PHOTOS_LABEL, f"anp_photos:{instance_id}"),
                ],
                [(MENU_LABEL, f"anp_menu:{instance_id}")],
            ],
            f"главный редактор не по контракту §2.5: {rows}",
        )

    @scenario_test
    async def test_each_selection_of_saved_draft_sends_another_copy(self, h) -> None:
        """§2.3: повторный выбор сохранённого черновика — вторая копия."""
        await h.text("/start")
        await _place(h)
        draft_id = await _saved_preview_draft(h)

        await _reopen_from_list(h, draft_id)
        first_ids = tuple(_details(h, draft_id).delivered_message_ids)
        first_text = h.posts()[0]

        await _reopen_from_list(h, draft_id)

        second_ids = tuple(_details(h, draft_id).delivered_message_ids)
        self.assertEqual(len(h.posts()), 2, f"копий: {h.posts()}")
        self.assertNotEqual(second_ids, first_ids, "версия не сменилась")
        self.assertNotIn(first_ids[0], h.deleted_ids(), "первая копия удалена")
        self.assertEqual(
            h.bot.content_text(h.chat_id, first_ids[0]),
            first_text,
            "содержимое первой копии изменилось",
        )
        _assert_no_preview_screen(self, h, where="повторный выбор черновика")

    @scenario_test
    async def test_saved_draft_stays_saved_after_restart(self, h) -> None:
        """
        Отметка «сохранён автором» переживает рестарт службы (§2.2).

        `stage=preview` + `None` не отличает готовый черновик от шага фото,
        поэтому признак хранится рядом с resume-снимком в `user_states`, а не
        только в памяти диалога.
        """
        await h.text("/start")
        await _place(h)
        draft_id = await _saved_preview_draft(h)

        await h.restart()

        await h.text("/start")
        await _reopen_from_list(h, draft_id)

        _assert_no_preview_screen(self, h, where="после рестарта")
        details = _details(h, draft_id)
        self.assertTrue(details.publication_sent, "после рестарта копия не ушла")
        self.assertEqual(
            h.posts(), [details.publication_text], f"текст копии: {h.posts()}"
        )
        self.assertIsNotNone(h.control_callback("data"), "главный редактор не открыт")

    @scenario_test
    async def test_question_stage_continues_with_saved_answer(self, h) -> None:
        """Сохранённый этап вопросов продолжается с сохранённым ответом."""
        await h.text("/start")
        place_id = await _place(h)
        _seed_standard(h, place_id, content="**[название]**")
        await _open_place_questions(h, place_id)
        draft = _latest_draft(h)
        with h.session_factory() as session:
            from announcement_store import set_announcement_stage

            set_announcement_stage(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                stage=WizardStage.QUESTIONS,
                expected_revision=draft.revision,
            )
            session.commit()

        await _reopen_from_list(h, draft.id)

        _assert_no_stage_error(self, h, where="этап вопросов")
        self.assertRegex(
            h.screen_text(), cat.question_re(1), "сценарий не продолжен с вопроса"
        )


async def _saved_preview_draft(h) -> int:
    """
    Черновик, который автор СОЗНАТЕЛЬНО сохранил на обзоре (§2.2).

    Мастер доведён до обзора и сохранён кнопкой «✅ Сохранить черновик»: это
    и есть «сохранённый готовый черновик», при выборе которого бот обязан
    сразу прислать копию. Голый шаг фото сюда не подходит: там тот же этап
    БД означает «приём фотографий ещё не завершён» (§2.6).
    """
    from tests.test_poster2_delivery import _to_photo_step

    await h.text("/start")
    draft = await _to_photo_step(h)
    assert _stage(h, draft.id) == WizardStage.PREVIEW.value, _stage(h, draft.id)
    # Обзор доступен после ответа текстом: «Сохранить черновик» на нём.
    await h.text("Текст сохранённого анонса")
    save = [
        data
        for _label, data in h.screen_buttons()
        if data and data.startswith("aw_save")
    ]
    if not save:
        raise AssertionError(f"нет кнопки сохранения черновика: {h.screen_buttons()}")
    await h.callback(save[0])
    assert _stage(h, draft.id) == WizardStage.PREVIEW.value, _stage(h, draft.id)
    return int(draft.id)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StalePreviewCallbackTests(unittest.TestCase):
    """§2.6: устаревшие callback удалённого экрана безопасны."""

    @scenario_test
    async def test_stale_preview_callback_keeps_screen_and_messages(self, h) -> None:
        """Устаревший `aw_preview` не падает, не удаляет сообщения и не пуст."""
        await h.text("/start")
        place_id = await _place(h)
        _seed_standard(
            h,
            place_id,
            content="Название: [название]",
            extended_content="Название: [название]\nКуратор: [куратор]",
            extended_active=True,
        )
        await _open_place_questions(h, place_id)
        draft_id, _screens = await _deliver(h)
        details = _details(h, draft_id)

        # Уходим в «Мои анонсы»: текущий экран диалога — список, а callback
        # обзора остался в чате от прошлого экрана (ревизия устарела).
        await h.callback("main_menu")
        await h.callback("my_announcements")
        before_deletes = len(h.deleted_ids())

        await h.callback(f"aw_preview:{draft_id}:{details.revision}")

        self.assertEqual(
            len(h.deleted_ids()), before_deletes, "устаревший callback удалил сообщения"
        )
        self.assertTrue(h.screen_text(), "пользователь остался на пустом экране")
        self.assertTrue(
            cat.is_stale(h.answer_texts()[-1])
            or REMOVED_REFRESH not in h.screen_text(),
            f"нет безопасного ответа: {h.answer_texts()}",
        )
        _assert_no_stage_error(self, h, where="устаревший aw_preview")

    @scenario_test
    async def test_stale_preview_of_delivered_opens_main_editor(self, h) -> None:
        """`aw_preview` старого сообщения доставленного → главный редактор."""
        await h.text("/start")
        place_id = await _place(h)
        _seed_standard(
            h,
            place_id,
            content="Название: [название]",
            extended_content="Название: [название]\nКуратор: [куратор]",
            extended_active=True,
        )
        await _open_place_questions(h, place_id)
        draft_id, _screens = await _deliver(h)
        details = _details(h, draft_id)
        posts_before = len(h.posts())
        instance_before = h.control_callback("data")

        before_deletes = len(h.deleted_ids())

        # Устаревшая кнопка «Обновить»: ревизия ниже текущей, поэтому
        # срабатывает именно путь устаревшего callback, а не обзор.
        await h.callback(f"aw_preview:{draft_id}:{details.revision - 1}")

        # Ни новой копии, ни удалений: анонс уже в чате.
        self.assertEqual(len(h.posts()), posts_before, "копия отправлена не по §2.2")
        self.assertEqual(
            len(h.deleted_ids()), before_deletes, "callback удалил сообщения"
        )
        # Главный редактор: управление у анонса и список его полей.
        self.assertIsNotNone(h.control_callback("data"), "нет управления анонсом")
        self.assertEqual(
            h.control_callback("data"),
            instance_before,
            "экземпляр анонса сменился без отправки",
        )
        self.assertTrue(h.live_callbacks("anf_sel:"), "список полей не открыт")
        _assert_no_stage_error(self, h, where="aw_preview доставленного")

    @scenario_test
    async def test_stale_preview_of_saved_draft_sends_copy(self, h) -> None:
        """`aw_preview` сохранённого недоставленного обзора → копия и редактор."""
        await h.text("/start")
        await _place(h)
        draft_id = await _saved_preview_draft(h)
        details = _details(h, draft_id)
        self.assertIsNone(details.publication_status)

        await h.callback("main_menu")
        await h.callback("my_announcements")

        screens_before = len(h.screen_texts())
        await h.callback(f"aw_preview:{draft_id}:{details.revision}")

        # Сравниваются экраны ПОСЛЕ нажатия: прежний экран обзора остаётся
        # в истории чата, но новых экранов с «Обзором анонса»/«Обновить» нет.
        _assert_no_preview_screen(
            self, h, where="aw_preview сохранённого", since=screens_before
        )
        _assert_no_stage_error(self, h, where="aw_preview сохранённого")
        fresh = _details(h, draft_id)
        self.assertTrue(fresh.publication_sent, "копия не отправлена")
        self.assertEqual(
            h.posts(), [fresh.publication_text], f"текст копии: {h.posts()}"
        )
        self.assertIsNotNone(h.control_callback("data"), "главный редактор не открыт")

    @scenario_test
    async def test_stale_apply_callback_does_not_send_anything(self, h) -> None:
        """Устаревший `aw_apply` (кнопка «Обновить») ничего не отправляет."""
        await h.text("/start")
        await _place(h)
        draft_id = await _saved_preview_draft(h)
        details = _details(h, draft_id)
        await h.callback("main_menu")
        await h.callback("my_announcements")
        posts_before = len(h.posts())
        before_deletes = len(h.deleted_ids())

        await h.callback(f"aw_apply:{draft_id}:{details.revision}")

        self.assertEqual(
            len(h.posts()), posts_before, "устаревшая кнопка отправила пост"
        )
        self.assertEqual(
            len(h.deleted_ids()), before_deletes, "устаревший callback удалил сообщения"
        )
        self.assertTrue(h.screen_text(), "пользователь остался на пустом экране")
        _assert_no_stage_error(self, h, where="устаревший aw_apply")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class WizardPreviewAntiRegressionTests(unittest.TestCase):
    """§2.6: обзор при СОЗДАНИИ анонса продолжает работать."""

    @scenario_test
    async def test_creation_flow_keeps_preview_and_send_button(self, h) -> None:
        """
        Первичный обзор не удалён: шаг фото → обзор → «Отправить» доставляет.

        Антирегресс требования «не удалять логику первоначального обзора,
        если она используется для завершения мастера».
        """
        from tests.test_poster2_delivery import _to_photo_step

        await h.text("/start")
        draft = await _to_photo_step(h)
        self.assertEqual(_stage(h, draft.id), WizardStage.PREVIEW.value)
        self.assertIn(
            cat.text("buttons.wizard.skip"),
            [label for label, _data in h.screen_buttons()],
            "шаг фото не показан",
        )

        await h.callback(_callback(h, "aw_photo_none"))

        assert_delivered(h, draft.id)
        self.assertEqual(len(h.posts()), 1, f"копий: {h.posts()}")
        self.assertIsNotNone(h.control_callback("data"), "главный редактор не открыт")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class WizardStepDraftTests(unittest.TestCase):
    """
    §2.6: НЕзавершённый мастер и «Сохранённый черновик» — одно состояние БД.

    Мастер идёт последним шагом (фотографии) → `stage=preview`,
    `publication_status=None`. Кнопка «✅ Сохранить черновик» на обзоре НЕ
    меняет это состояние. Значит по одной строке БД «готовый сохранённый
    черновик» неотличим от «приём фото ещё не завершён», и немедленная
    отправка по одному лишь `stage` завершала бы мастер за пользователя.

    Поэтому отличием служит отметка «автор сохранил сам» в `user_states`
    (`announcement_saved_drafts`): без неё выбор анонса продолжает шаг
    мастера, с ней — сразу отправляет копию (§2.2).
    """

    @scenario_test
    async def test_unfinished_photo_step_continues_wizard_without_sending(
        self, h
    ) -> None:
        """
        Черновик, остановленный на шаге фото, копию НЕ отправляет (§2.6).

        Иначе выбор такого анонса из «Мои анонсы» завершал бы мастер за
        автора: фото ещё не собраны, человек не нажимал «Сохранить черновик».
        """
        from tests.test_poster2_delivery import _to_photo_step

        await h.text("/start")
        draft = await _to_photo_step(h)
        self.assertEqual(
            (_stage(h, draft.id), _details(h, draft.id).publication_status),
            (WizardStage.PREVIEW.value, None),
            "состояние БД шага фото изменилось",
        )

        await _reopen_from_list(h, draft.id)

        details = _details(h, draft.id)
        self.assertFalse(details.publication_sent, "копия отправлена без запроса")
        self.assertEqual(h.posts(), [], f"в чате появился пост: {h.posts()}")
        self.assertIsNone(details.publication_status)
        # Мастер продолжается со своего этапа (§2.6: обзор при СОЗДАНИИ не
        # меняется), поэтому здесь штатный экран шага, а не пустота и не
        # ошибка. Копия не отправляется: «Сохранить черновик» не нажимали.
        self.assertTrue(h.screen_text(), "пользователь остался на пустом экране")
        self.assertIn(
            cat.text("buttons.wizard.otpravit_etot_chat"),
            [label for label, _data in h.screen_buttons()],
            f"шаг мастера не продолжен: {h.screen_buttons()}",
        )
        _assert_no_stage_error(self, h, where="шаг фото")

    @scenario_test
    async def test_explicit_save_and_photo_step_share_db_state(self, h) -> None:
        """«Сохранить черновик» не меняет строку БД — отсюда и нужна отметка."""
        from tests.test_poster2_delivery import _to_photo_step

        await h.text("/start")
        draft = await _to_photo_step(h)
        step_state = (
            _stage(h, draft.id),
            _details(h, draft.id).publication_status,
        )
        self.assertEqual(step_state, (WizardStage.PREVIEW.value, None))

        # «Сохранить черновик» с обзора: тот же этап, тот же статус.
        await h.text("Ещё текст")
        await h.callback(_callback(h, "aw_save"))
        self.assertEqual(
            (_stage(h, draft.id), _details(h, draft.id).publication_status),
            step_state,
            "сохранение не должно выдавать шаг фото за готовый анонс",
        )


def _callback(h, prefix: str) -> str:
    found = [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]
    if not found:
        raise AssertionError(f"нет callback {prefix!r}: {h.screen_buttons()}")
    return found[0]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
