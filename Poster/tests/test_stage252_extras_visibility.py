# tests/test_stage252_extras_visibility.py
"""
Poster 2.5.2, T3: видимость кнопки «🧩 Дополнительные поля» (§4, сц. 8–10).

Кнопка появляется ТОЛЬКО когда экран добавления не окажется пустым:

- у снимка шаблона есть расширенная версия (`extended_content`);
- в ней есть кирпичики, которых нет в простой версии (кандидаты);
- хотя бы один из них доступен автору для выбора;
- либо у анонса уже выбраны доп. поля (ими надо управлять).

Проверяется пользовательский результат через настоящее приложение, FakeBot
и временную SQLite: временный экран редактора, клавиатура на самом анонсе,
панель медиагруппы и `panel_keyboard`. Прямой вызов `anp_extras` на скрытую
кнопку остаётся допустимым (старая клавиатура) — это не видимость.
"""

from __future__ import annotations

import asyncio
import unittest

from tests import ui_texts_expect as cat

try:
    import announcement_controls as controls
    from announcement_store import (
        add_announcement_extra,
        extras_available,
        extras_available_for_draft,
        get_announcement_draft,
    )
    from announcement_wizard import extra_candidate_brick_ids
    from brick_store import archive_personal_brick
    from handlers import announcement_wizard as wizard
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        assert_delivered,
        inline_buttons,
        is_panel_record,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
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


EXTRAS_LABEL = "🧩 Дополнительные поля"
TemplateSpec = tuple[str, str, str, dict[str, object]]
PLAIN_SPEC: TemplateSpec = ("main", "Основное поле", "short_text", {})
EXTRA_SPEC: TemplateSpec = ("extra", "Доп. поле", "short_text", {})


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _screen_labels(h) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _control_buttons(h) -> list[tuple[str, str | None]]:
    """Кнопки управления активным анонсом (клавиатура поста или панель)."""
    for record in reversed(h.calls):
        buttons = inline_buttons(record.get("reply_markup"))
        if buttons and all(data and data.startswith("anp_") for _l, data in buttons):
            return buttons
    raise AssertionError("в чате нет клавиатуры управления анонсом")


def _control_labels(h) -> list[str]:
    return [label for label, _data in _control_buttons(h)]


def _panel_buttons(h) -> list[tuple[str, str | None]]:
    records = [call for call in h.calls if is_panel_record(call)]
    if not records:
        raise AssertionError("отдельной панели управления нет")
    return inline_buttons(records[-1].get("reply_markup"))


async def _deliver(
    h,
    *,
    specs,
    content: str,
    extended: str | None,
    photos: int = 0,
):
    """Сев личного шаблона → ответ на вопрос → шаг фото → доставка."""
    place_id, _alias = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(
        h,
        specs=specs,
        content=content,
        extended_content=extended,
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    # Вопросов может быть несколько (content со всеми кирпичиками): отвечаем,
    # пока мастер не перейдёт к шагу фотографий.
    for _attempt in range(5):
        if cat.text("wizard.photos.title") in h.screen_text():
            break
        await h.text("Значение")
    if photos == 0:
        await _no_photos(h)
    elif photos == 1:
        await h.photo("photo-0")
    else:
        for index in range(photos):
            await h.photo(f"photo-{index}", media_group_id="album")
        await asyncio.sleep(0.4)
    draft = _latest_draft(h)
    assert_delivered(h, draft.id)
    return draft, bricks


async def _open_fields(h) -> None:
    """
    Список текстовых полей доставленного анонса (2.5.3 §5).

    Отдельного экрана «✏️ Изменить анонс» больше нет: кнопка «📝 Текстовые
    поля» заменяет клавиатуру самого анонса (0–1 фото) или текст его
    единственной панели.
    """
    data = h.control_callback("data")
    if data is None:
        raise AssertionError(f"нет управления анонсом: {h.live_buttons()}")
    await h.callback(data)


def _extras_datas(h) -> list[str]:
    """`callback_data` кнопки доп. полей во ВСЕХ живых клавиатурах чата."""
    return [
        data
        for _label, data in h.live_buttons()
        if data and (data.startswith("anp_extras") or data.startswith("aw_extras"))
    ]


async def _back_to_controls(h) -> None:
    """«◀️ Назад» списка полей возвращает управление анонсу (§5)."""
    backs = h.live_callbacks("anf_back:")
    if not backs:
        raise AssertionError(f"нет «Назад» списка полей: {h.live_buttons()}")
    await h.callback(backs[0])


async def _ensure_controls(h) -> None:
    """
    Управление анонсом доступно автору: список полей закрывается «◀️ Назад».

    Список может не открыться (например, поле в снимке шаблона ушло в
    архив) — тогда управление уже на месте, и закрывать нечего.
    """
    if h.live_callbacks("anf_back:"):
        await _back_to_controls(h)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ExtrasHiddenTests(unittest.TestCase):
    """8–9: кнопки нет, когда экран добавления был бы пуст."""

    @scenario_test
    async def test_simple_only_template_has_no_extras_button(self, h) -> None:
        """Только простая версия шаблона — кнопки доп. полей нет нигде."""
        await h.text("/start")
        draft, _bricks = await _deliver(
            h,
            specs=[PLAIN_SPEC],
            content="[main]",
            extended=None,
        )

        self.assertNotIn(EXTRAS_LABEL, _control_labels(h), "кнопка на самом анонсе")
        await _open_fields(h)
        self.assertNotIn(EXTRAS_LABEL, [label for label, _d in h.live_buttons()])
        self.assertFalse(
            _extras_datas(h),
            "у simple-only шаблона нет перехода в доп. поля",
        )

        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
            self.assertFalse(
                extras_available(
                    session,
                    details,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                ),
                "предикат должен быть False",
            )
            self.assertFalse(
                extras_available_for_draft(session, draft.id),
                "предикат панели должен быть False",
            )

    @scenario_test
    async def test_extended_without_new_bricks_has_no_extras_button(self, h) -> None:
        """Расширенная версия повторяет простую — кандидатов нет."""
        await h.text("/start")
        draft, _bricks = await _deliver(
            h,
            specs=[
                PLAIN_SPEC,
                ("second", "Второе поле", "short_text", {}),
            ],
            content="[main] [second]",
            extended="[main] [second]",
        )

        self.assertNotIn(EXTRAS_LABEL, _control_labels(h))
        await _open_fields(h)
        self.assertNotIn(EXTRAS_LABEL, [label for label, _d in h.live_buttons()])
        self.assertFalse(_extras_datas(h))

        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
            snapshot = details.template_snapshot
            assert snapshot is not None
            self.assertEqual(extra_candidate_brick_ids(snapshot), ())

    @scenario_test
    async def test_unselectable_candidate_hides_extras_button(self, h) -> None:
        """Кандидат есть, но определение архивно — выбирать нечего."""
        await h.text("/start")
        draft, bricks = await _deliver(
            h,
            specs=[PLAIN_SPEC, EXTRA_SPEC],
            content="[main]",
            extended="[main]\n[extra]",
        )
        # До архивации кирпичика кнопка есть: сценарий (в) уже доступен.
        self.assertIn(EXTRAS_LABEL, _control_labels(h))

        with h.session_factory() as session:
            archive_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                brick_id=bricks["extra"],
                expected_revision=1,
            )

        await _open_fields(h)
        await _ensure_controls(h)
        self.assertNotIn(
            EXTRAS_LABEL,
            [label for label, _data in h.live_buttons()],
            "архивный кандидат не должен открывать экран доп. полей",
        )
        self.assertFalse(_extras_datas(h))
        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
            self.assertFalse(
                extras_available(
                    session,
                    details,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                )
            )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ExtrasVisibleTests(unittest.TestCase):
    """10: кнопка есть и ведёт в непустой экран, пока есть чем управлять."""

    @scenario_test
    async def test_extended_with_extra_brick_shows_button(self, h) -> None:
        await h.text("/start")
        _draft, _bricks = await _deliver(
            h,
            specs=[PLAIN_SPEC, EXTRA_SPEC],
            content="[main]",
            extended="[main]\n[extra]",
        )

        self.assertIn(EXTRAS_LABEL, _control_labels(h), "кнопка на самом анонсе")
        await _open_fields(h)
        # 2.5.3 §5: список полей заменяет клавиатуру анонса, поэтому кнопка
        # доп. полей появляется после «◀️ Назад».
        await _back_to_controls(h)
        self.assertIn(
            EXTRAS_LABEL,
            [label for label, _data in h.live_buttons()],
            "кнопка в управлении анонсом",
        )

        extras_data = _extras_datas(h)
        self.assertEqual(len(extras_data), 1, "ровно один переход в доп. поля")
        await h.callback(extras_data[0])
        # Экран не пуст: в списке есть кирпичик расширенного текста.
        self.assertTrue(
            [
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("aw_exedit")
            ],
            f"пустой экран доп. полей: {h.screen_buttons()}",
        )

    @scenario_test
    async def test_button_stays_after_all_candidates_chosen(self, h) -> None:
        """После выбора всех кандидатов экран не пуст: управление значениями."""
        await h.text("/start")
        draft, _bricks = await _deliver(
            h,
            specs=[PLAIN_SPEC, EXTRA_SPEC],
            content="[main]",
            extended="[main]\n[extra]",
        )
        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
            snapshot = details.template_snapshot
            assert snapshot is not None
            candidates = extra_candidate_brick_ids(snapshot)
            self.assertEqual(len(candidates), 1)
            for brick_id in candidates:
                add_announcement_extra(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    draft_id=draft.id,
                    brick_id=brick_id,
                    value="Доп. значение",
                )

        await _open_fields(h)
        await _back_to_controls(h)
        self.assertIn(
            EXTRAS_LABEL,
            [label for label, _data in h.live_buttons()],
            "выбранным полем надо управлять — кнопка обязана остаться",
        )
        extras_data = _extras_datas(h)
        await h.callback(extras_data[0])
        self.assertTrue(
            [
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("aw_exdel")
            ],
            f"выбранное поле не показано на экране: {h.screen_buttons()}",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PanelExtrasVisibilityTests(unittest.TestCase):
    """Панель опубликованного анонса подчиняется тому же предикату."""

    @scenario_test
    async def test_album_panel_hides_extras_for_simple_template(self, h) -> None:
        await h.text("/start")
        await _deliver(
            h,
            specs=[PLAIN_SPEC],
            content="[main]",
            extended=None,
            photos=3,
        )
        labels = [label for label, _data in _panel_buttons(h)]
        self.assertNotIn(EXTRAS_LABEL, labels, f"панель: {labels}")
        self.assertFalse(
            [
                data
                for _label, data in _panel_buttons(h)
                if data and data.startswith("anp_extras")
            ]
        )

    @scenario_test
    async def test_album_panel_shows_extras_for_extended_template(self, h) -> None:
        await h.text("/start")
        await _deliver(
            h,
            specs=[PLAIN_SPEC, EXTRA_SPEC],
            content="[main]",
            extended="[main]\n[extra]",
            photos=3,
        )
        labels = [label for label, _data in _panel_buttons(h)]
        self.assertIn(EXTRAS_LABEL, labels, f"панель: {labels}")

    def test_panel_keyboard_flag_controls_button(self) -> None:
        """`panel_keyboard` скрывает и показывает кнопку по флагу предиката."""
        with_extras = [
            data
            for _label, data in inline_buttons(
                controls.panel_keyboard(1, extras_available=True)
            )
        ]
        without_extras = [
            data
            for _label, data in inline_buttons(
                controls.panel_keyboard(1, extras_available=False)
            )
        ]
        self.assertIn(controls.panel_callback(controls.ACTION_EXTRAS, 1), with_extras)
        self.assertNotIn(
            controls.panel_callback(controls.ACTION_EXTRAS, 1), without_extras
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PreviewAndDeliveredKeyboardTests(unittest.TestCase):
    """Те же условия — на обзоре и в компактном редакторе доставленного."""

    @scenario_test
    async def test_preview_keyboard_respects_flag(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _deliver(
            h,
            specs=[PLAIN_SPEC, EXTRA_SPEC],
            content="[main]",
            extended="[main]\n[extra]",
        )
        details = _details(h, draft.id)

        shown = [
            data
            for _label, data in inline_buttons(
                wizard._preview_keyboard(
                    details,
                    extras_available=wizard._extras_available(details, DEFAULT_USER_ID),
                )
            )
        ]
        hidden = [
            data
            for _label, data in inline_buttons(
                wizard._preview_keyboard(details, extras_available=False)
            )
        ]
        self.assertTrue(
            [data for data in shown if data and data.startswith("aw_extras")]
        )
        self.assertFalse(
            [data for data in hidden if data and data.startswith("aw_extras")]
        )

        # 2.5.3 §5: у доставленного анонса управление — `panel_keyboard`
        # (клавиатура сообщения 0–1 фото или панель альбома). Фабрика
        # подчиняется тому же флагу предиката.
        with_extras = [
            data
            for _label, data in inline_buttons(
                controls.panel_keyboard(1, extras_available=True)
            )
        ]
        without_extras = [
            data
            for _label, data in inline_buttons(
                controls.panel_keyboard(1, extras_available=False)
            )
        ]
        self.assertIn(controls.panel_callback(controls.ACTION_EXTRAS, 1), with_extras)
        self.assertNotIn(
            controls.panel_callback(controls.ACTION_EXTRAS, 1), without_extras
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
