# tests/test_stage26_a2_extended_toggle.py
"""
Этап A2 (§3 задания от 2026-11-08): переключатель расширенного варианта
стандартного шаблона площадки.

Проверяется пользовательский контракт на настоящем приложении
(`bot.build_application`), FakeBot и временной SQLite — реальные
`post_bot.db`, `.env` и Telegram не используются:

1. флаг выключен → в карточке стандарта есть «Включить дополнительный
   шаблон», доступа к расширенному редактору нет;
2. одно нажатие включает флаг: он сразу меняется в БД, экран перерисовывается
   тем же сообщением, появляются «Расширенная версия» и «Выключить
   дополнительный шаблон», отдельного сообщения об успехе нет;
3. одно нажатие выключает флаг: кнопка включения возвращается, а расширенный
   текст и условные части остаются целыми;
4. любое число переключений не теряет текст, привязки и правила;
5. не-админ места получает отказ, флаг не меняется;
6. §3.1: начатый черновик продолжает свой снимок, новый черновик берёт
   текущий флаг (флаг не инвалидирует существующие снимки).

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage26_a2_extended_toggle -v
"""

from __future__ import annotations

import unittest
from datetime import date

from sqlalchemy import func, select

from announcement_store import (
    build_announcement_preview,
    build_announcement_render_payload,
    choose_announcement_template,
    confirm_announcement_place,
    save_announcement_value,
    start_announcement_wizard,
)
from brick_store import get_system_brick
from conditional_parts import list_conditional_parts
from models import PlaceAdmin, Template, TemplateSnapshot, utcnow
from permissions import can_manage_place
from place_store import create_place
from template_state import load_template_session, save_template_session
from template_store import get_standard_template, set_standard_extended_active
from tests import ui_texts_expect as cat
from tests.integration_harness import (
    DEFAULT_USER_ID,
    HARNESS_IMPORT_ERROR,
    frozen_today,
    scenario_test,
)
from tests.stage23_helpers import OTHER_TG
from user_store import get_or_create_user

try:
    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)

TODAY = date(2026, 11, 2)

TITLE_SIMPLE = "✏️ Правка шаблона"
TITLE_EXTENDED = cat.text("buttons.templates.rasshirennaya_versiya")
BUTTON_ON = cat.text("buttons.templates.vklyuchit_dopolnitelnyy_shablon")
BUTTON_OFF = cat.text("buttons.templates.vyklyuchit_dopolnitelnyy_shablon")


def _seed_place(h, *, actor_tg: int = DEFAULT_USER_ID, admin: bool = True) -> int:
    """Площадка со стандартным шаблоном; `admin=True` выдаёт роль админа."""
    with h.session_factory() as session:
        user = get_or_create_user(session, actor_tg, username=f"user-{actor_tg}")
        place = create_place(
            session,
            "Дом культуры",
            city="Тестовый город",
            created_by_user_id=user.id,
            address="ул. Тестовая, 1",
            url="https://example.com/place",
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


def _standard(h, place_id: int):
    with h.session_factory() as session:
        return get_standard_template(session, DEFAULT_USER_ID, place_id)


def _flag(h, template_id: int) -> bool:
    with h.session_factory() as session:
        row = session.get(Template, template_id)
        assert row is not None
        return bool(row.extended_active)


def _extended_text(h, template_id: int) -> str | None:
    with h.session_factory() as session:
        return get_standard_template(
            session, DEFAULT_USER_ID, int(session.get(Template, template_id).place_id)
        ).current_version.extended_content


def _rules(h, template_id: int):
    with h.session_factory() as session:
        return list(
            list_conditional_parts(
                session, DEFAULT_USER_ID, template_id, include_stale=True
            )
        )


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


def _labels(h) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _last_alert(h) -> str:
    answers = h.answers()
    if not answers:
        return ""
    args = answers[-1].get("args") or ()
    return str(args[0]) if args else str(answers[-1].get("text") or "")


def _message_calls(h) -> int:
    """Все отправленные сообщения (не правки экрана)."""
    return sum(1 for call in h.calls if call["type"] in {"message", "photo"})


async def _open_card(h, place_id: int) -> int:
    await h.callback(f"tp_std:{place_id}")
    buttons = [data for _label, data in h.screen_buttons()]
    edit = next(data for data in buttons if data and data.startswith("tp_edit:"))
    return int(edit.split(":", 1)[1])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StandardExtendedToggleTests(unittest.TestCase):
    """§3: включение/выключение расширенного варианта в карточке стандарта."""

    @scenario_test
    async def test_flag_off_shows_only_enable_button(self, h) -> None:
        """Флаг выключен: «Включить…» есть, расширенного доступа нет."""
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)

        self.assertFalse(_flag(h, template_id), "новая площадка — флаг выключен")
        labels = _labels(h)
        self.assertIn(BUTTON_ON, labels)
        self.assertNotIn(BUTTON_OFF, labels)
        self.assertNotIn(TITLE_EXTENDED, labels, "расширенный редактор скрыт")
        self.assertEqual(_callbacks(h, "tp_edit_ext:"), [])
        self.assertEqual(_callbacks(h, "tp_mkext:"), [])
        self.assertIn(f"tp_ext_on:{template_id}", _callbacks(h, "tp_ext_on:"))
        # Расширенный текст в базе есть, просто не показан как доступный.
        self.assertIsNotNone(_extended_text(h, template_id))

    @scenario_test
    async def test_one_tap_enables_flag_in_place_without_success_message(
        self, h
    ) -> None:
        """Одно нажатие: флаг=1, экран перерисован, отдельного сообщения нет."""
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)

        messages_before = _message_calls(h)
        screen_id_before = h.screen_message_id()
        answers_before = len(h.answers())
        await h.callback(f"tp_ext_on:{template_id}")

        self.assertTrue(_flag(h, template_id), "флаг включён сразу")
        labels = _labels(h)
        self.assertIn(TITLE_EXTENDED, labels)
        self.assertIn(BUTTON_OFF, labels)
        self.assertNotIn(BUTTON_ON, labels)
        self.assertIn(f"tp_edit_ext:{template_id}", _callbacks(h, "tp_edit_ext:"))

        # Экран обновлён тем же сообщением: новых сообщений и нового экрана нет.
        self.assertEqual(_message_calls(h), messages_before, "новое сообщение")
        self.assertEqual(h.screen_message_id(), screen_id_before, "экран пересоздан")
        self.assertEqual(h.screen()["type"], "edit_message")
        self.assertIn(f"tp_ext_off:{template_id}", _callbacks(h, "tp_ext_off:"))
        # Подтверждения и текста успеха нет: единственный ответ — пустой ack.
        self.assertEqual(len(h.answers()), answers_before + 1)
        self.assertEqual(_last_alert(h), "")

    @scenario_test
    async def test_one_tap_disables_and_keeps_extended_content(self, h) -> None:
        """Выключение: флаг=0, кнопка включения вернулась, текст цел."""
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        extended_before = _extended_text(h, template_id)
        rules_before = _rules(h, template_id)
        self.assertTrue(rules_before)

        await h.callback(f"tp_ext_on:{template_id}")
        messages_before = _message_calls(h)
        await h.callback(f"tp_ext_off:{template_id}")

        self.assertFalse(_flag(h, template_id))
        self.assertIn(BUTTON_ON, _labels(h))
        self.assertNotIn(BUTTON_OFF, _labels(h))
        self.assertEqual(_callbacks(h, "tp_edit_ext:"), [])
        self.assertEqual(_extended_text(h, template_id), extended_before)
        self.assertEqual(len(_rules(h, template_id)), len(rules_before))
        self.assertEqual(_message_calls(h), messages_before, "новое сообщение")

    @scenario_test
    async def test_many_toggles_keep_text_rules_and_bindings(self, h) -> None:
        """Любое число переключений не удаляет расширенный текст и правила."""
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        with h.session_factory() as session:
            standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
            content = standard.current_version.content
            extended = standard.current_version.extended_content
            bindings = standard.current_version.brick_ids
            revision = standard.revision
        rules_before = {
            (part.section, part.code, part.fragment) for part in _rules(h, template_id)
        }

        for index in range(4):
            prefix = "tp_ext_on" if index % 2 == 0 else "tp_ext_off"
            await h.callback(f"{prefix}:{template_id}")

        with h.session_factory() as session:
            standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
            self.assertEqual(standard.current_version.content, content)
            self.assertEqual(standard.current_version.extended_content, extended)
            self.assertEqual(standard.current_version.brick_ids, bindings)
            self.assertEqual(
                standard.revision, revision, "флаг не создаёт новых версий"
            )
        self.assertFalse(_flag(h, template_id), "4 переключения → снова выключен")
        self.assertEqual(
            {
                (part.section, part.code, part.fragment)
                for part in _rules(h, template_id)
            },
            rules_before,
        )

    @scenario_test
    async def test_non_admin_callback_is_rejected(self, h) -> None:
        """Не-админ места не меняет флаг даже при подделанном callback."""
        place_id = _seed_place(h, actor_tg=OTHER_TG)
        with h.session_factory() as session:
            standard = get_standard_template(session, OTHER_TG, place_id)
            template_id = standard.template_id
            revision = standard.revision
        # Пользователь харнесса не админ этого места; состояние карточки
        # подделано напрямую, как будто экран остался с прошлого раза.
        await h.text("/start")
        save_template_session(
            DEFAULT_USER_ID,
            {
                "phase": "card",
                "template_id": template_id,
                "revision": revision,
                "origin": "admin",
                "place_id": place_id,
                "page": 0,
            },
        )

        await h.callback(f"tp_ext_on:{template_id}")
        self.assertTrue(_last_alert(h), "нужен явный отказ")
        self.assertFalse(_flag(h, template_id), "флаг не изменён не-админом")

        await h.callback(f"tp_ext_off:{template_id}")
        self.assertFalse(_flag(h, template_id))

    @scenario_test
    async def test_repeat_tap_is_not_an_error(self, h) -> None:
        """Повторное нажатие той же кнопки: «message is not modified» — не ошибка."""
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)

        await h.callback(f"tp_ext_on:{template_id}")
        messages_before = _message_calls(h)
        await h.callback(f"tp_ext_on:{template_id}")

        self.assertTrue(_flag(h, template_id))
        self.assertEqual(_message_calls(h), messages_before, "лишнее сообщение")
        cat.assert_not_shows(self, h.screen_text(), "errors.internal")
        self.assertIn(BUTTON_OFF, _labels(h))

    @scenario_test
    async def test_extended_entry_opens_editor_only_when_enabled(self, h) -> None:
        """«Расширенная версия» открывает редактор расширенного текста."""
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        await h.callback(f"tp_ext_on:{template_id}")
        await h.callback(f"tp_edit_ext:{template_id}")
        state = load_template_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "section_edit")
        cat.assert_shows(
            self,
            h.screen_text(),
            "templates.build.redaktirovanie_otpravte_novym",
            "экран редактора расширенного текста не открылся",
        )

    @scenario_test
    async def test_toggle_on_personal_template_is_refused(self, h) -> None:
        """Переключатель — только у стандарта места: личный шаблон отклонён."""
        with h.session_factory() as session:
            user = get_or_create_user(session, DEFAULT_USER_ID, username="user-default")
            from template_store import create_personal_template

            template = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="Личный",
                content="[название]",
            )
            session.commit()
            personal_id = template.template_id
        await h.text("/start")
        await h.callback(f"tp_open:{personal_id}")
        self.assertNotIn(f"tp_ext_on:{personal_id}", _callbacks(h, "tp_ext_on:"))
        await h.callback(f"tp_ext_on:{personal_id}")
        self.assertTrue(_last_alert(h), "нужен явный отказ")
        with h.session_factory() as session:
            row = session.get(Template, personal_id)
            assert row is not None
            self.assertFalse(bool(row.extended_active))

    @scenario_test
    async def test_store_setter_enforces_permissions_and_revision(self, h) -> None:
        """Сервисный сеттер сам проверяет права и оптимистичную ревизию."""
        place_id = _seed_place(h, actor_tg=OTHER_TG)
        with h.session_factory() as session:
            # Пользователь харнесса существует, но местом не управляет.
            get_or_create_user(session, DEFAULT_USER_ID, username="user-default")
            session.commit()
            standard = get_standard_template(session, OTHER_TG, place_id)
            self.assertFalse(
                can_manage_place(session, DEFAULT_USER_ID, place_id),
                "подготовка: пользователь харнесса не управляет местом",
            )
            denied = set_standard_extended_active(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                template_id=standard.template_id,
                active=True,
            )
            self.assertEqual(denied.status, "forbidden")

            stale = set_standard_extended_active(
                session,
                actor_telegram_user_id=OTHER_TG,
                template_id=standard.template_id,
                active=True,
                expected_revision=standard.revision + 1,
            )
            self.assertEqual(stale.status, "stale")

            enabled = set_standard_extended_active(
                session,
                actor_telegram_user_id=OTHER_TG,
                template_id=standard.template_id,
                active=True,
                expected_revision=standard.revision,
            )
            self.assertEqual(enabled.status, "updated")
            assert enabled.template is not None
            self.assertTrue(enabled.template.extended_active)

            again = set_standard_extended_active(
                session,
                actor_telegram_user_id=OTHER_TG,
                template_id=standard.template_id,
                active=True,
            )
            self.assertEqual(again.status, "unchanged")

    @scenario_test
    async def test_started_draft_keeps_its_snapshot_after_toggle(self, h) -> None:
        """§3.1: начатый черновик рендерится по своему снимку после переключения."""
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        self.assertFalse(_flag(h, template_id))

        with h.session_factory() as session:
            draft = start_announcement_wizard(
                session, actor_telegram_user_id=DEFAULT_USER_ID, place_id=place_id
            )
            confirmed = confirm_announcement_place(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.draft_id,
                expected_revision=draft.revision,
            )
            chosen = choose_announcement_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=confirmed.draft_id,
                template_id=template_id,
                expected_revision=confirmed.revision,
            )
            snapshot = chosen.template_snapshot
            assert snapshot is not None
            self.assertFalse(snapshot.extended_active)
            draft_id = chosen.draft_id
            revision = chosen.revision
            for code, value in {
                "название": "Летний концерт",
                "дата": "09.11",
                "время_начала": "19:00",
                "описание": "Большой зал.",
                "контакт": "@contact",
            }.items():
                brick = get_system_brick(session, code)
                assert brick is not None
                saved = save_announcement_value(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    draft_id=draft_id,
                    brick_id=brick.id,
                    value=value,
                    expected_revision=revision,
                    today=TODAY,
                )
                revision = saved.revision
            payload = build_announcement_render_payload(
                session, actor_telegram_user_id=DEFAULT_USER_ID, draft_id=draft_id
            )
            self.assertFalse(payload.use_extended, "черновик начат простым вариантом")
            # Компилятор проверяет относительную дату («09.11») по `app_today()`,
            # а не по переданному `today`: нужна заморозка часов.
            with frozen_today(TODAY.year, TODAY.month, TODAY.day):
                before = build_announcement_preview(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    draft_id=draft_id,
                    today=TODAY,
                ).html
            snapshots_before = int(
                session.scalar(
                    select(func.count(TemplateSnapshot.id)).where(
                        TemplateSnapshot.source_template_id == template_id
                    )
                )
                or 0
            )

        await h.callback(f"tp_ext_on:{template_id}")
        self.assertTrue(_flag(h, template_id))

        with h.session_factory() as session:
            payload = build_announcement_render_payload(
                session, actor_telegram_user_id=DEFAULT_USER_ID, draft_id=draft_id
            )
            self.assertFalse(
                payload.use_extended, "снимок не переключился задним числом"
            )
            with frozen_today(TODAY.year, TODAY.month, TODAY.day):
                after = build_announcement_preview(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    draft_id=draft_id,
                    today=TODAY,
                ).html
            self.assertEqual(after, before, "начатый черновик не изменился")
            snapshots_after = int(
                session.scalar(
                    select(func.count(TemplateSnapshot.id)).where(
                        TemplateSnapshot.source_template_id == template_id
                    )
                )
                or 0
            )
            self.assertEqual(
                snapshots_after, snapshots_before, "переключение не плодит снимки"
            )

        # Новый черновик, начатый после включения, берёт уже расширенный вариант.
        with h.session_factory() as session:
            new_draft = start_announcement_wizard(
                session, actor_telegram_user_id=DEFAULT_USER_ID, place_id=place_id
            )
            new_confirmed = confirm_announcement_place(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=new_draft.draft_id,
                expected_revision=new_draft.revision,
            )
            new_chosen = choose_announcement_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=new_confirmed.draft_id,
                template_id=template_id,
                expected_revision=new_confirmed.revision,
            )
            new_snapshot = new_chosen.template_snapshot
            assert new_snapshot is not None
            self.assertTrue(
                new_snapshot.extended_active,
                "новый черновик фиксирует включённый флаг",
            )
