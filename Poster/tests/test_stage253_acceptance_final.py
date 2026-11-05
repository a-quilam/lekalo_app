# tests/test_stage253_acceptance_final.py
"""
Poster 2.5.3 — финальная приёмка J–L и подтверждение E4 (§7).

Сквозные пользовательские сценарии через настоящее приложение, FakeBot и
временную SQLite (`tests/integration_harness.py`): реальная `post_bot.db`,
`.env` и сеть Telegram не используются.

Что закрепляется (требования владельца целиком):

J. «🏠 В меню» и повторное открытие анонса
   J1. Доставленный анонс → «🏠 В меню» (`anp_menu`): управление и клавиатуры
       прежнего экземпляра сняты, само сообщение анонса остаётся в чате,
       черновик не меняется.
   J2. Старые callback (`anp_`/`anf_` прежнего экземпляра) отвечают
       «устарело» и не правят БД.
   J3. «📂 Мои анонсы» → повторное открытие создаёт РОВНО ОДИН новый
       управляемый экземпляр; прежний остаётся архивным снимком.

K. Защита данных площадки
   K1. Название площадки, город, адрес и ссылка отсутствуют в списке
       текстовых полей, хотя присутствуют в анонсе.
   K2. Поддельные `aw_qgoto` с brick_id адреса и города отклоняются и не
       меняют ни значения анонса, ни строку площадки в БД.
   K3. Права площадки не ослаблены: посторонний не меняет чужое место,
       администратор — меняет (`tests/test_stage20b_edit_flow.py`,
       `EditSecurityTests` покрывает админ-маршрут в интерфейсе).

E4-CONFIRM. 1500 символов в готовом анонсе (§7)
   1500 символов сохраняются в черновике, но НЕ публикуются: анонс и его
   фото целы, ввод не удалён, «Обзора анонса» и старого маршрута «✏️ … — 1500»
   нет. Правка переживает рестарт и применяется ОДИН раз после исправления.

L. Сквозной путь автора: создание → фото → публикация → правка текста →
   фото-режим (2-е фото, удаление первого) → длинное описание → исправление →
   «В меню» → повторное открытие → безопасность старых кнопок → рестарт.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage253_acceptance_final -v
"""

from __future__ import annotations

import unittest

try:
    from sqlalchemy import text

    import announcement_controls as controls
    from announcement_instances import list_instances
    from announcement_store import (
        FIELD_STATUS_KNOWN,
        build_announcement_fields,
    )
    from brick_types import SYSTEM_BRICKS_BY_CODE
    from place_edit_store import (
        PlaceEditPermissionError,
        set_place_address,
    )
    from template_store import get_standard_template, update_template_content
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage25_acceptance_c import _assert_field_list
    from tests.test_stage25_delivery_instances import (
        _final_markup,
        _only_active,
        _stored,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from utils.ui_texts import tr

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# Подписи — из каталога текстов, а не литералами.
FIELDS_TITLE = tr("buttons.finished.data")
PHOTOS_TITLE = tr("wizard.photos.title")
STALE_INSTANCE = controls.STALE_INSTANCE_TEXT
STALE_WIZARD = tr("errors.stale.wizard")

# 2.5.3 §7: короткая ошибка превышения лимита.
LENGTH_REFUSAL = "Не отправлено: текст не помещается"

# Формулировки старого маршрута восстановления: их быть не должно.
REMOVED_MARKERS = (
    "Обзор анонса",
    "Самые длинные поля",
    "Изменить другое поле",
    "Без фотографий",
)

# Прежний экран/кнопка правки: удалён в §5.
REMOVED_EDIT = "✏️ Изменить анонс"

# Поля площадки: их значения приходят из справочника, в списке их нет.
PLACE_CODES = ("место", "адрес", "ссылка_места", "город")
PLACE_TITLES = tuple(SYSTEM_BRICKS_BY_CODE[code].title for code in PLACE_CODES)

# §7: 1500 символов — больше подписи к фото (1024) и меньше лимита без фото
# (4096), поэтому одно и то же значение проходит без снимка и не проходит
# с ним.
LONG_VALUE = "а" * 1500

CREATE_TYPES = frozenset({"message", "photo", "media_group", "rich_message"})


# --- помощники -------------------------------------------------------------


def _created_count(h) -> int:
    """Сколько вызовов создали сообщения бота (без правок и удалений)."""
    return sum(1 for call in h.calls if call["type"] in CREATE_TYPES)


def _answers_since(h, index: int) -> list[dict]:
    return [call for call in h.calls[index:] if call["type"] == "callback_answer"]


def _texts_since(h, index: int) -> list[str]:
    """Тексты сообщений/правок, созданных начиная с вызова `index`."""
    out: list[str] = []
    for call in h.calls[index:]:
        if call["type"] not in {
            "message",
            "edit_message",
            "photo",
            "media_group",
            "rich_message",
        }:
            continue
        text = call.get("text")
        if isinstance(text, str) and text:
            out.append(text)
    return out


def _labels(h) -> str:
    return " | ".join(label for label, _data in h.live_buttons())


def _field_buttons(h) -> list[tuple[str, str]]:
    """Живые кнопки полей списка (подпись, `anf_sel:` callback)."""
    return [
        (label, data)
        for label, data in h.live_buttons()
        if data and data.startswith("anf_sel:")
    ]


async def _open_field_list(h, instance_id: int) -> None:
    await h.callback(controls.panel_callback(controls.ACTION_DATA, instance_id))


def _control(h, action: str) -> str:
    data = h.control_callback(action)
    if data is None:
        raise AssertionError(f"нет кнопки {action!r}: {h.live_buttons()}")
    return data


def _live(h, prefix: str) -> str:
    found = h.live_callbacks(prefix)
    if not found:
        raise AssertionError(f"нет живой кнопки {prefix!r}: {h.live_buttons()}")
    return found[0]


def _draft_row(h, draft_id: int):
    """(stage, revision, publication_status, publication_text) из БД."""
    with h.session_factory() as session:
        return session.execute(
            text(
                "SELECT stage, revision, publication_status, publication_text "
                "FROM announcement_drafts WHERE id = :draft_id"
            ),
            {"draft_id": int(draft_id)},
        ).first()


def _value_rows(h, draft_id: int) -> list[tuple[int, str, str | None]]:
    """Все строки `announcement_values` черновика (brick_id, state, value_json)."""
    with h.session_factory() as session:
        rows = session.execute(
            text(
                "SELECT brick_id, state, value_json FROM announcement_values "
                "WHERE draft_id = :draft_id ORDER BY brick_id"
            ),
            {"draft_id": int(draft_id)},
        ).all()
    return [(int(row[0]), str(row[1]), row[2]) for row in rows]


def _place_row(h, place_id: int) -> tuple:
    """(каноническое название, город, адрес, ссылка) площадки из БД."""
    with h.session_factory() as session:
        place = session.execute(
            text("SELECT city, address, url FROM places WHERE id = :place_id"),
            {"place_id": int(place_id)},
        ).first()
        name = session.execute(
            text(
                "SELECT name FROM place_names "
                "WHERE place_id = :place_id AND is_canonical = 1"
            ),
            {"place_id": int(place_id)},
        ).first()
    return (name[0] if name else None, place[0], place[1], place[2])


def _instances(h, draft_id: int):
    with h.session_factory() as session:
        return list_instances(session, draft_id)


async def _delivered_personal(
    h, *, fields: int, photos: int, titles: list[str] | None = None
):
    """Личный шаблон `fields` полей → вопросы → шаг фото → доставка."""
    place_id, _alias = _seed_place(h)
    titles = titles or [f"Поле {index}" for index in range(fields)]
    specs: list[tuple[str, str, str, dict[str, object]]] = [
        (f"f{index}", titles[index], "short_text" if index else "long_text", {})
        for index in range(fields)
    ]
    content = "\n".join(f"Строка {index}: [f{index}]" for index in range(fields))
    template_id, _revision, bricks = _seed_personal_template(
        h,
        name=f"Шаблон на {fields} полей",
        specs=specs,
        content=content,
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    for _ in range(fields + 5):
        if PHOTOS_TITLE in h.screen_text():
            break
        await h.text("Значение")
    if PHOTOS_TITLE not in h.screen_text():
        raise AssertionError(f"Не шаг фото: {h.screen_text()[:200]!r}")
    if photos == 0:
        await h.callback(_callback(h, "aw_photo_none"))
    else:
        await h.photo("photo-0")
    draft = _latest_draft(h)
    if _stored(h, draft.id).publication_status != "sent":
        raise AssertionError(f"анонс не доставлен: {h.screen_text()[:200]!r}")
    return draft, bricks


async def _delivered_standard(h, *, address: str | None = "ул. Тестовая, 1"):
    """Стандарт площадки с добавленным `[город]`; все вопросы пропущены."""
    place_id, _alias = _seed_place(h, address=address)
    with h.session_factory() as session:
        standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
        updated = update_template_content(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            template_id=standard.template_id,
            expected_revision=standard.revision,
            content=standard.current_version.content + "\n[город]",
        )
        if not updated.ok:
            raise AssertionError(f"стандарт не обновлён: {updated.message}")
        standard_id = standard.template_id
    await _start_to_template(h, place_id)
    await h.callback(_callback(h, "aw_template", f":{standard_id}"))
    for _ in range(20):
        skips = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("aw_qskip")
        ]
        if not skips:
            break
        await h.callback(skips[0])
    if PHOTOS_TITLE in h.screen_text():
        await h.callback(_callback(h, "aw_photo_none"))
    draft = _latest_draft(h)
    if _stored(h, draft.id).publication_status != "sent":
        raise AssertionError(f"анонс не доставлен: {h.screen_text()[:200]!r}")
    return draft, place_id


# --- J. Выход в меню и повторное открытие ---------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MenuExitAndReopenTests(unittest.TestCase):
    """«🏠 В меню» снимает управление; повторное открытие даёт один экземпляр."""

    @scenario_test
    async def test_j1_menu_removes_controls_and_keeps_message(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered_personal(h, fields=1, photos=1)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]
        published = _stored(h, draft.id).publication_text
        row_before = _draft_row(h, draft.id)
        values_before = _value_rows(h, draft.id)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), published, "анонс в чате"
        )
        self.assertTrue(h.live_control_callbacks(), "управление на анонсе")

        await h.callback(controls.panel_callback(controls.ACTION_MENU, active.id))

        # Клавиатуры прежнего экземпляра сняты, само сообщение осталось.
        self.assertEqual(h.live_control_callbacks(), [], "управление не снято с анонса")
        self.assertEqual(
            [data for _label, data in h.live_buttons() if data.startswith("anp_")],
            [],
            "кнопки управления остались живыми",
        )
        self.assertNotIn(message_id, h.deleted_ids(), "сообщение анонса удалено")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            published,
            "текст анонса переписан выходом в меню",
        )
        # Черновик и значения не изменились: «В меню» — только навигация.
        self.assertEqual(_draft_row(h, draft.id), row_before, "черновик изменён")
        self.assertEqual(_value_rows(h, draft.id), values_before)
        self.assertFalse(
            [item for item in _instances(h, draft.id) if item.is_active],
            "экземпляр остался действующим после выхода в меню",
        )
        archived = [item for item in _instances(h, draft.id) if item.id == active.id]
        self.assertEqual(len(archived), 1)
        self.assertTrue(archived[0].is_archived, "экземпляр не архивирован")

    @scenario_test
    async def test_j2_old_callbacks_are_stale_and_change_nothing(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered_personal(h, fields=1, photos=1)
        active = _only_active(h, draft.id)
        await h.callback(controls.panel_callback(controls.ACTION_MENU, active.id))

        row_before = _draft_row(h, draft.id)
        values_before = _value_rows(h, draft.id)
        created_before = _created_count(h)

        for forged in (
            controls.panel_callback(controls.ACTION_DATA, active.id),
            controls.panel_callback(controls.ACTION_PHOTOS, active.id),
            controls.field_callback(
                controls.FIELD_ACTION_SELECT, active.id, bricks["f0"]
            ),
            controls.field_callback(controls.FIELD_ACTION_BACK, active.id),
        ):
            calls_before = len(h.calls)
            await h.callback(forged)
            answers = _answers_since(h, calls_before)
            self.assertTrue(
                any(
                    a.get("text") == STALE_INSTANCE and a.get("show_alert")
                    for a in answers
                ),
                f"нет ответа «устарело» на {forged!r}: {answers}",
            )

        self.assertEqual(
            _created_count(h), created_before, "старые кнопки создали сообщения"
        )
        self.assertEqual(_draft_row(h, draft.id), row_before, "старые кнопки правят БД")
        self.assertEqual(_value_rows(h, draft.id), values_before)
        self.assertEqual(h.live_control_callbacks(), [], "управление вернулось")

    @scenario_test
    async def test_j3_reopen_creates_exactly_one_controlled_instance(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered_personal(h, fields=1, photos=1)
        first = _only_active(h, draft.id)
        published = _stored(h, draft.id).publication_text
        posts_before = len(h.posts())
        await h.callback(controls.panel_callback(controls.ACTION_MENU, first.id))

        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")

        active = [item for item in _instances(h, draft.id) if item.is_active]
        self.assertEqual(len(active), 1, "повторное открытие создало не один экземпляр")
        reopened = _only_active(h, draft.id)
        self.assertNotEqual(reopened.id, first.id, "экземпляр не новый")
        self.assertEqual(reopened.generation, first.generation + 1)
        self.assertNotEqual(reopened.message_ids, first.message_ids, "то же сообщение")
        self.assertEqual(
            len(h.posts()), posts_before + 1, "новая версия отправлена ровно один раз"
        )
        # Прежний экземпляр остался архивным снимком, не удалён.
        self.assertNotIn(first.message_ids[0], h.deleted_ids())
        self.assertIsNone(_final_markup(h, first.message_ids[0]), "кнопки не сняты")
        # Управление нового экземпляра — живое и указывает на новый id.
        self.assertEqual(
            sorted({data.split(":")[-1] for data in h.live_control_callbacks()}),
            [str(reopened.id)],
            f"управление не на новом экземпляре: {h.live_control_callbacks()}",
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, reopened.message_ids[0]),
            published,
            "новая версия показывает другой текст",
        )


# --- K. Защита данных площадки --------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceDataProtectionTests(unittest.TestCase):
    """Данные площадки не переписываются редактором анонса."""

    @scenario_test
    async def test_k1_place_fields_are_absent_from_field_list(self, h) -> None:
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h)
        active = _only_active(h, draft.id)

        with h.session_factory() as session:
            fields = build_announcement_fields(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        known = {field.code for field in fields if field.status == FIELD_STATUS_KNOWN}
        # Все значения площадки приходят из справочника, адрес теперь заполнен:
        # в анонсе они есть, но в списке правки их быть не должно.
        self.assertLessEqual(
            set(PLACE_CODES), known, "поля площадки должны быть в анонсе"
        )

        await _open_field_list(h, active.id)
        _assert_field_list(h)
        labels = _labels(h)
        for title in PLACE_TITLES:
            self.assertNotIn(title, labels, f"поле площадки {title!r} в списке")

    @scenario_test
    async def test_k2_forged_field_navigation_keeps_place_data(self, h) -> None:
        await h.text("/start")
        draft, place_id = await _delivered_standard(h)
        active = _only_active(h, draft.id)
        await _open_field_list(h, active.id)
        await h.callback(_field_buttons(h)[0][1])
        # 2.5.4 §1: ввод идёт на самом анонсе, отдельного вопроса нет.
        self.assertTrue(h.live_callbacks("anq_"), "ввод не открылся")

        row_before = _draft_row(h, draft.id)
        values_before = _value_rows(h, draft.id)
        place_before = _place_row(h, place_id)
        revision = row_before[1]
        created_before = _created_count(h)
        answers_before = len(h.answer_texts())

        for code in ("адрес", "город"):
            brick_id = SYSTEM_BRICKS_BY_CODE[code].id
            await h.callback(f"aw_qgoto:{draft.id}:{revision}:{brick_id}")
            answers = h.answer_texts()[answers_before:]
            answers_before = len(h.answer_texts())
            self.assertTrue(
                any(STALE_WIZARD in answer for answer in answers),
                f"подделка на {code!r} не отклонена: {answers}",
            )
            # Отказ не создаёт ни вопросов, ни экранов.
            self.assertEqual(
                _created_count(h),
                created_before,
                f"подделка на {code!r} создала сообщения",
            )

        self.assertEqual(_draft_row(h, draft.id), row_before, "черновик изменён")
        self.assertEqual(_value_rows(h, draft.id), values_before, "значения изменены")
        self.assertEqual(_place_row(h, place_id), place_before, "площадка изменена")
        self.assertEqual(_created_count(h), created_before, "появились сообщения")
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(
            stored.publication_text,
            h.bot.content_text(h.chat_id, active.message_ids[0]),
            "анонс в чате разошёлся с БД",
        )

    @scenario_test
    async def test_k3_place_edit_permissions_still_enforced(self, h) -> None:
        """Админ-маршрут площадки по-прежнему проверяет права."""
        await h.text("/start")
        _draft, place_id = await _delivered_standard(h)
        place_before = _place_row(h, place_id)

        with h.session_factory() as session:
            with self.assertRaises(
                PlaceEditPermissionError, msg="посторонний правит чужую площадку"
            ):
                set_place_address(
                    session,
                    actor_telegram_user_id=987654,
                    place_id=place_id,
                    address="Подмена",
                    expected_address=place_before[2],
                )
        self.assertEqual(_place_row(h, place_id), place_before, "площадка изменена")

        with h.session_factory() as session:
            result = set_place_address(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                place_id=place_id,
                address="Новый адрес",
                expected_address=place_before[2],
            )
        self.assertEqual(result.status, "ok", f"админ не смог править: {result}")
        self.assertEqual(_place_row(h, place_id)[2], "Новый адрес")


# --- E4-CONFIRM. 1500 символов в готовом анонсе ---------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LongTextInDeliveredAnnouncementTests(unittest.TestCase):
    """§7: длинный текст — неопубликованная правка, пост и фото целы."""

    @scenario_test
    async def test_e4_confirm_long_value_is_draft_and_applies_once(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered_personal(
            h, fields=1, photos=1, titles=["Описание"]
        )
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]
        published = _stored(h, draft.id).publication_text
        self.assertEqual(published, "Строка 0: Значение", published)
        posts_before = len(h.posts())

        # «📝 Текстовые поля»: поле «Описание» помечено заполненным.
        await _open_field_list(h, active.id)
        _assert_field_list(h)
        self.assertIn(
            "1. Описание — заполнено",
            " | ".join(label for label, _data in _field_buttons(h)),
            h.live_buttons(),
        )
        calls_before = len(h.calls)

        # «Описание» → 1500 символов.
        await h.callback(_field_buttons(h)[-1][1])
        long_message = await h.text(LONG_VALUE)

        # 2.5.4 §1: ошибка ввода приходит ответом на сообщение автора
        # (ввод живёт на анонсе, отдельного экрана/вопроса нет).
        shown = "\n".join([h.screen_text(), *h.answer_texts(), *h.reply_texts()])
        self.assertIn(LENGTH_REFUSAL, shown, f"нет короткой ошибки: {shown!r}")
        # Лимит считается по итоговому тексту анонса: значение заменяет прежнее
        # в шаблоне «Строка 0: [f0]», то есть 10 + 1500 символов.
        self.assertIn("1510 символов при лимите 1024", shown)
        # Ввод не потерян: сообщение с текстом осталось в чате.
        self.assertNotIn(long_message.message.message_id, h.deleted_ids())
        # Значение сохранено как неопубликованная правка.
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_text, published, "пост переиздан")
        self.assertEqual(stored.delivered_message_ids, (message_id,))
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            published,
            "сообщение анонса изменилось",
        )
        self.assertEqual(len(h.posts()), posts_before, "лишняя публикация")
        long_rows = [row for row in _value_rows(h, draft.id) if row[0] == bricks["f0"]]
        self.assertEqual(len(long_rows), 1, "значение поля не сохранено")
        self.assertEqual(long_rows[0][1], "valid")
        self.assertIn(LONG_VALUE, long_rows[0][2] or "", "сохранён не весь текст")

        # Старого маршрута нет: ни «Обзора анонса», ни кнопок «✏️ … — 1500».
        scripts = "\n".join([*_texts_since(h, calls_before), _labels(h)])
        for marker in REMOVED_MARKERS:
            self.assertNotIn(marker, scripts, f"старый маршрут: {marker!r}")
        self.assertNotIn(REMOVED_EDIT, scripts, "экран правки вернулся")
        self.assertEqual(h.live_callbacks("aw_edit"), [], "старая кнопка правки")
        for label, data in h.live_buttons():
            self.assertNotIn(
                "✏️",
                label,
                f"кнопка старого маршрута: {label!r} ({data!r})",
            )
        self.assertFalse(
            [
                answer
                for answer in h.answer_texts()
                if STALE_WIZARD in answer or STALE_INSTANCE in answer
            ],
            "цикл устаревших кнопок",
        )

        # «◀️ Назад» из ввода и рестарт: правка остаётся в БД.
        backs = [
            data
            for data in h.live_callbacks("anq_back")
            if data.endswith(f":{bricks['f0']}")
        ]
        self.assertEqual(len(backs), 1, f"нет «Назад» ввода: {h.live_buttons()}")
        await h.callback(backs[0])
        self.assertIsNone(h.screen_message_id(), "лишний экран ввода остался")
        self.assertTrue(_field_buttons(h), "список полей не вернулся")

        await h.restart()
        self.assertIsNone(
            h.user_data().get("announcement_ui"), "состояние диалога должно потеряться"
        )
        long_rows = [row for row in _value_rows(h, draft.id) if row[0] == bricks["f0"]]
        self.assertIn(LONG_VALUE, long_rows[0][2] or "", "правка не пережила рестарт")
        after_restart = _stored(h, draft.id)
        self.assertEqual(after_restart.publication_text, published, "пост изменился")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            published,
            "анонс в чате изменился",
        )

        # Исправление на допустимый текст: ровно одна новая версия.
        await _open_field_list(h, active.id)
        _assert_field_list(h)
        field_data = next(
            data for label, data in _field_buttons(h) if "Описание" in label
        )
        await h.callback(field_data)
        created_at_question = _created_count(h)
        await h.text("Исправленное описание")

        self.assertEqual(
            _created_count(h), created_at_question, "лишние сообщения при исправлении"
        )
        fixed = _stored(h, draft.id)
        self.assertEqual(fixed.publication_status, "sent")
        self.assertIn("Исправленное описание", fixed.publication_text or "")
        self.assertNotIn(LONG_VALUE, fixed.publication_text or "")
        self.assertEqual(fixed.delivered_message_ids, (message_id,), "анонс переиздан")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            fixed.publication_text,
            "обновление не применено к тому же сообщению",
        )
        self.assertEqual(len(h.posts()), posts_before, "лишняя публикация")
        self.assertEqual(_only_active(h, draft.id).id, active.id, "экземпляр заменён")
        self.assertTrue(_field_buttons(h), f"список полей не вернулся: {_labels(h)}")


# --- L. Сквозной путь ------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FullAuthorPathTests(unittest.TestCase):
    """Сквозной пользовательский путь автора от создания до восстановления."""

    @scenario_test
    async def test_l_full_path(self, h) -> None:
        # 1. /start → создание анонса для площадки → поля → 1 фото → публикация.
        await h.text("/start")
        draft, bricks = await _delivered_personal(h, fields=2, photos=1)
        first = _only_active(h, draft.id)
        message_id = first.message_ids[0]
        posts_before = len(h.posts())
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, ("photo-0",))
        self.assertEqual(message_id, stored.delivered_message_ids[0])
        self.assertIn("Строка 0: Значение", stored.publication_text)

        # 2. Изменение текста: правка на месте, новый пост не появляется.
        await _open_field_list(h, first.id)
        await h.callback(f"anf_sel:{first.id}:{bricks['f0']}")
        created_at_question = _created_count(h)
        await h.text("Правка текста")

        self.assertEqual(_created_count(h), created_at_question, "лишние сообщения")
        edited = _stored(h, draft.id)
        self.assertIn("Правка текста", edited.publication_text)
        self.assertEqual(edited.delivered_message_ids, (message_id,))
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), edited.publication_text
        )
        self.assertEqual(len(h.posts()), posts_before, "текст опубликован заново")
        self.assertTrue(_field_buttons(h), f"список полей не вернулся: {_labels(h)}")

        # 3. Фото-режим → 2-е фото: 1 → 2 = одна контролируемая замена.
        await h.callback(_control(h, controls.ACTION_PHOTOS))
        self.assertTrue(h.live_callbacks("aw_photo_"), "фото-режим не открылся")
        await h.photo("photo-1")

        album = _only_active(h, draft.id)
        self.assertNotEqual(album.id, first.id, "структура 1→2 не заменена")
        self.assertEqual(
            _stored(h, draft.id).publication_photos, ("photo-0", "photo-1")
        )
        self.assertIsNotNone(album.panel_message_id, "у альбома нет панели")
        self.assertEqual(len(h.posts()), posts_before + 1, "замена ушла не один раз")

        # 4. Удаление первого снимка: 2 → 1, снова один снимок.
        await h.callback(_live(h, "aw_photo_del:"))
        single = _only_active(h, draft.id)
        self.assertNotEqual(single.id, album.id, "структура 2→1 не заменена")
        self.assertEqual(_stored(h, draft.id).publication_photos, ("photo-1",))
        self.assertIsNone(single.panel_message_id, "у одного фото осталась панель")
        posts_after_photos = len(h.posts())
        self.assertEqual(posts_after_photos, posts_before + 2)

        # 5. Длинное описание (> 1024 при фото): правка не публикуется, пост цел.
        await h.callback(_live(h, "anf_back:"))
        await _open_field_list(h, single.id)
        await h.callback(f"anf_sel:{single.id}:{bricks['f1']}")
        published = _stored(h, draft.id).publication_text
        long_message = await h.text(LONG_VALUE)

        # 2.5.4 §1: ошибка ввода приходит ответом на сообщение автора
        # (ввод живёт на анонсе, отдельного экрана/вопроса нет).
        shown = "\n".join([h.screen_text(), *h.answer_texts(), *h.reply_texts()])
        self.assertIn(LENGTH_REFUSAL, shown, f"нет короткой ошибки: {shown!r}")
        self.assertNotIn(long_message.message.message_id, h.deleted_ids())
        refused = _stored(h, draft.id)
        self.assertEqual(refused.publication_status, "sent")
        self.assertEqual(refused.publication_text, published, "пост повреждён")
        self.assertEqual(refused.publication_photos, ("photo-1",), "фото потеряны")
        self.assertEqual(refused.delivered_message_ids, single.message_ids)
        self.assertEqual(
            h.bot.content_text(h.chat_id, single.message_ids[0]), published
        )
        self.assertEqual(len(h.posts()), posts_after_photos, "лишняя публикация")
        long_rows = [row for row in _value_rows(h, draft.id) if row[0] == bricks["f1"]]
        self.assertIn(LONG_VALUE, long_rows[0][2] or "", "ввод не сохранён")

        # 6. Исправление текста: ровно одна новая версия на месте.
        await h.text("Исправленное описание")

        fixed = _stored(h, draft.id)
        self.assertIn("Исправленное описание", fixed.publication_text or "")
        self.assertEqual(fixed.delivered_message_ids, single.message_ids)
        self.assertEqual(
            h.bot.content_text(h.chat_id, single.message_ids[0]),
            fixed.publication_text,
        )
        self.assertEqual(len(h.posts()), posts_after_photos, "лишняя публикация")
        self.assertEqual(_only_active(h, draft.id).id, single.id, "экземпляр заменён")

        # 7. «🏠 В меню»: управление снято, сообщение осталось.
        published_text = fixed.publication_text
        await h.callback(controls.panel_callback(controls.ACTION_MENU, single.id))
        self.assertEqual(h.live_control_callbacks(), [], "управление не снято")
        self.assertNotIn(single.message_ids[0], h.deleted_ids())
        self.assertEqual(
            h.bot.content_text(h.chat_id, single.message_ids[0]), published_text
        )

        # 8. «📂 Мои анонсы» → повторное открытие: один новый экземпляр.
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")
        reopened = _only_active(h, draft.id)
        self.assertNotEqual(reopened.id, single.id)
        self.assertEqual(reopened.generation, single.generation + 1)
        self.assertEqual(
            len([item for item in _instances(h, draft.id) if item.is_active]), 1
        )
        self.assertEqual(
            len(h.posts()), posts_after_photos + 1, "повторное открытие не одно"
        )

        # 9. Старые кнопки безопасны: «устарело», без правок и сообщений.
        row_before = _draft_row(h, draft.id)
        created_before = _created_count(h)
        calls_before = len(h.calls)
        await h.callback(controls.panel_callback(controls.ACTION_DATA, single.id))
        answers = _answers_since(h, calls_before)
        self.assertTrue(
            any(
                a.get("text") == STALE_INSTANCE and a.get("show_alert") for a in answers
            ),
            f"старая кнопка не отклонена: {answers}",
        )
        self.assertEqual(_draft_row(h, draft.id), row_before)
        self.assertEqual(
            _created_count(h), created_before, "старая кнопка создала экран"
        )

        # 10. Рестарт: экземпляр и содержимое восстанавливаются из БД.
        await h.restart()
        self.assertIsNone(h.user_data().get("announcement_ui"))
        restored = _only_active(h, draft.id)
        self.assertEqual(restored.id, reopened.id, "экземпляр потерян")
        self.assertEqual(_stored(h, draft.id).publication_text, published_text)
        self.assertEqual(
            h.bot.content_text(h.chat_id, restored.message_ids[0]), published_text
        )
        await h.callback(controls.panel_callback(controls.ACTION_DATA, restored.id))
        _assert_field_list(h)
        self.assertTrue(_field_buttons(h), "правка недоступна после рестарта")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
