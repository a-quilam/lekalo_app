# tests/test_stage25_conditional_ui.py
"""
Poster 2.5, этап D2 — Telegram-интерфейс условных частей шаблона (§39–45).

Обновления проходят через настоящее приложение `build_application`, как в
Stage 9: проверяются экраны шагов, callback-кнопки, тексты каталога,
оптимистическая блокировка и записи в SQLite. Telegram и реальная
`post_bot.db` не используются.

Сценарии задания D2:

1. полный маршрут «кирпичик → фрагмент → предпросмотр → сохранение»;
2. сквозной §42-А: правило, созданное через интерфейс, убирает пустое
   время окончания из готового анонса;
3. свой фрагмент: ошибка «не найден» оставляет на вводе, корректный
   фрагмент ведёт к предпросмотру и сохранению;
4. целая строка цены исчезает без лишних пустых строк;
5. повторяющийся кирпичик — автор выбирает конкретное вхождение;
6. список правил: изменение и удаление с подтверждением;
7. `stale`: после правки текста карточка предупреждает, правило не
   применяется к случайному месту;
8. права: чужой личный шаблон не открывает раздел правил;
9. «Назад» на каждом шаге не теряет выбранный кирпичик.
"""

from __future__ import annotations

import unittest

from announcement_store import (
    build_announcement_preview,
    choose_announcement_template,
    confirm_announcement_place,
    save_announcement_value,
    start_announcement_wizard,
)
from brick_store import get_system_brick
from conditional_parts import (
    STATUS_ACTIVE,
    STATUS_STALE,
    list_conditional_parts,
)
from models import PlaceAdmin, utcnow
from place_store import create_place
from template_store import create_personal_template, update_template_content
from tests.integration_harness import (
    HARNESS_IMPORT_ERROR,
    scenario_test,
)
from tests.stage23_helpers import OTHER_TG
from user_store import get_or_create_user

try:
    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001
    INTEGRATION_ERROR = str(e)

# §42-А: шаблон без заполненного времени окончания превращается в «19:00».
TIME_TEMPLATE = "[время_начала]–[время_окончания]"


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


def _callback(h, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(f"Нет callback {prefix!r}; есть: {h.screen_buttons()}")
    return found[0]


def _user(session, telegram_user_id: int):
    user = get_or_create_user(
        session, telegram_user_id, username=f"u{telegram_user_id}"
    )
    session.commit()
    return user


def _template_with_system_time_bricks(
    h, *, name: str = "Время", content: str = TIME_TEMPLATE
):
    """Личный шаблон на системных кирпичиках времени (их уже создали миграции)."""
    with h.session_factory() as session:
        user = _user(session, h.user_id)
        template = create_personal_template(
            session,
            actor_telegram_user_id=user.telegram_user_id,
            name=name,
            content=content,
        )
        session.commit()
        return template.template_id, template.revision


def _parts(h, template_id: int) -> list:
    with h.session_factory() as session:
        return list(
            list_conditional_parts(session, h.user_id, template_id, include_stale=True)
        )


async def _open_card(h, template_id: int) -> None:
    await h.text("/templates")
    await h.callback(f"tp_open:{template_id}")


def _place_with_admin(h, telegram_user_id: int) -> tuple[int, int]:
    """Место, его администратор и автоматически созданный стандарт."""
    from sqlalchemy import select

    from models import Template

    with h.session_factory() as session:
        user = _user(session, telegram_user_id)
        place = create_place(
            session,
            "Дом культуры",
            city="Тестовый город",
            created_by_user_id=user.id,
        )
        session.add(
            PlaceAdmin(
                place_id=place.id,
                user_id=user.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
            )
        )
        session.commit()
        template = session.scalar(
            select(Template).where(
                Template.place_id == place.id, Template.kind == "standard"
            )
        )
        assert template is not None
        return place.id, template.id


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class ConditionalUiRouteTests(unittest.TestCase):
    """Маршрут автора §40: карточка → кирпичик → фрагмент → предпросмотр."""

    @scenario_test
    async def test_open_shows_only_bricks_from_text(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        self.assertIn(
            ("🧩 Условные части", f"tp_cond:{template_id}"), h.screen_buttons()
        )

        await h.callback(f"tp_cond:{template_id}")
        # Шаг 1: только кирпичики из текста шаблона, с контекстом.
        self.assertIn("время_окончания", h.screen_text())
        self.assertIn("вхождение 1", h.screen_text())
        self.assertNotIn("дата", h.screen_text())
        entries = _callbacks(h, f"tp_condb:{template_id}:c:")
        self.assertEqual(len(entries), 2)

    @scenario_test
    async def test_route_preview_and_save_via_ui(self, h) -> None:
        template_id, revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")

        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:c:{end_brick.id}:1")
        # Шаг 2: варианты фрагмента, предложение НЕ сохраняется сразу.
        labels = [label for label, _data in h.screen_buttons()]
        self.assertIn("Скрыть с соседним разделителем", labels)
        self.assertIn("Скрыть целую строку", labels)
        self.assertIn("✏️ Свой фрагмент", labels)
        self.assertIn(f"tp_condf:{template_id}:sep", _callbacks(h, "tp_condf"))

        await h.callback(f"tp_condf:{template_id}:sep")
        self.assertIn("–[время_окончания]", h.screen_text())
        self.assertIn("не сохранён", h.screen_text())
        self.assertEqual(_parts(h, template_id), [])

        await h.callback(f"tp_conduse:{template_id}")
        # Шаг 4: предпросмотр на реальном тексте секции.
        self.assertIn("Кирпичик заполнен", h.screen_text())
        self.assertIn("[время_начала]–[время_окончания]", h.screen_text())
        self.assertIn("[время_начала]", h.screen_text().split("Кирпичик пуст:")[-1])
        self.assertEqual(_parts(h, template_id), [], "без подтверждения не сохраняем")

        await h.callback(f"tp_conds:{template_id}")
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].fragment, "–[время_окончания]")
        self.assertEqual(parts[0].status, STATUS_ACTIVE)
        self.assertIn("действует", h.screen_text())
        # Правило не создаёт новую версию шаблона.
        with h.session_factory() as session:
            from models import Template

            row = session.get(Template, template_id)
            assert row is not None
            self.assertEqual(row.revision, revision)

    @scenario_test
    async def test_custom_fragment_error_keeps_author_on_input(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:c:{end_brick.id}:1")
        await h.callback(f"tp_condf:{template_id}:custom")

        await h.text("Нет такого фрагмента")
        self.assertIn("нет кирпичика", h.screen_text())
        self.assertIn("Свой фрагмент", h.screen_text())
        self.assertEqual(_parts(h, template_id), [])

        await h.text("–[время_окончания]")
        self.assertIn("Предпросмотр", h.screen_text())
        await h.callback(f"tp_conds:{template_id}")
        self.assertEqual(len(_parts(h, template_id)), 1)

    @scenario_test
    async def test_repeated_brick_chooses_occurrence(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(
            h,
            name="Повтор",
            content="[время_начала] • [время_окончания] • [время_окончания]",
        )
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        first = f"tp_condb:{template_id}:c:"
        entries = [data for data in _callbacks(h, first) if data.endswith(":2")]
        self.assertEqual(len(entries), 1, "у кирпичика есть второе вхождение")
        self.assertIn("(вхождение 2)", h.screen_text())

        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:c:{end_brick.id}:2")
        await h.callback(f"tp_condf:{template_id}:custom")
        # Одинаковый фрагмент есть у двух вхождений — правило должно лечь
        # именно на выбранное второе, а не на первое.
        await h.text(" • [время_окончания]")
        self.assertIn("вхождение 2", h.screen_text())
        await h.callback(f"tp_conds:{template_id}")
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].occurrence, 2)

    @scenario_test
    async def test_list_rule_edit_and_delete(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:c:{end_brick.id}:1")
        await h.callback(f"tp_condf:{template_id}:sep")
        await h.callback(f"tp_conduse:{template_id}")
        await h.callback(f"tp_conds:{template_id}")
        part_id = _parts(h, template_id)[0].id

        # Список правил: открыть конкретное.
        await h.callback(f"tp_condo:{template_id}:{part_id}")
        self.assertIn("–[время_окончания]", h.screen_text())
        self.assertIn("✏️ Изменить", [label for label, _d in h.screen_buttons()])

        await h.callback(f"tp_conde:{template_id}:{part_id}")
        self.assertIn("Выберите, какую часть текста скрывать", h.screen_text())
        await h.callback(f"tp_condback:{template_id}:bricks")
        await h.callback(f"tp_condb:{template_id}:c:{end_brick.id}:1")
        await h.callback(f"tp_condf:{template_id}:custom")
        await h.text("–[время_окончания]")
        await h.callback(f"tp_conds:{template_id}")
        self.assertEqual(len(_parts(h, template_id)), 1, "правка не создаёт дубль")

        # Удаление — только через подтверждение.
        await h.callback(f"tp_condo:{template_id}:{part_id}")
        await h.callback(f"tp_condd:{template_id}:{part_id}")
        self.assertIn("Удалить условную часть?", h.screen_text())
        self.assertEqual(len(_parts(h, template_id)), 1)
        await h.callback(f"tp_conddy:{template_id}:{part_id}")
        self.assertEqual(_parts(h, template_id), [])

    @scenario_test
    async def test_back_keeps_context_on_every_step(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:c:{end_brick.id}:1")
        await h.callback(f"tp_condf:{template_id}:sep")
        await h.callback(f"tp_conduse:{template_id}")

        await h.callback(f"tp_condback:{template_id}:proposal")
        self.assertIn("Будет скрываться фрагмент", h.screen_text())
        await h.callback(f"tp_condback:{template_id}:fragment")
        self.assertIn("время_окончания", h.screen_text())
        self.assertIn(
            "Скрыть целую строку", [label for label, _d in h.screen_buttons()]
        )
        await h.callback(f"tp_condback:{template_id}:bricks")
        self.assertIn("Выберите кирпичик", h.screen_text())
        await h.callback(f"tp_condback:{template_id}:card")
        self.assertIn("Версия", h.screen_text())
        self.assertIn(
            ("🧩 Условные части", f"tp_cond:{template_id}"), h.screen_buttons()
        )
        self.assertEqual(_parts(h, template_id), [])

    @scenario_test
    async def test_admin_of_place_edits_standard_template_rules(self, h) -> None:
        """MINOR-1: админ места проходит маршрут в стандартном шаблоне."""
        place_id, template_id = _place_with_admin(h, h.user_id)
        await h.callback(f"tp_std:{place_id}")
        self.assertIn("Стандартный шаблон места", h.screen_text())

        await h.callback(f"tp_cond:{template_id}")
        self.assertIn("Выберите кирпичик", h.screen_text())
        with h.session_factory() as session:
            address = get_system_brick(session, "адрес")
            assert address is not None
        # Раньше здесь был ложный алерт «вхождение исчезло»: `origin` не
        # переносился из карточки в состояние маршрута.
        await h.callback(f"tp_condb:{template_id}:c:{address.id}:1")
        self.assertIn("Выберите, какую часть текста скрывать", h.screen_text())
        await h.callback(f"tp_condf:{template_id}:custom")
        await h.text("[адрес]")
        await h.callback(f"tp_conduse:{template_id}")
        await h.callback(f"tp_conds:{template_id}")
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].fragment, "[адрес]")

    @scenario_test
    async def test_custom_fragment_keeps_leading_space(self, h) -> None:
        """MINOR-2: ведущий пробел фрагмента значим и сохраняется как введён."""
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            template = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="С адресом",
                content="📍 [место] ([адрес])",
            )
            session.commit()
            template_id = template.template_id
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        with h.session_factory() as session:
            address = get_system_brick(session, "адрес")
            assert address is not None
        await h.callback(f"tp_condb:{template_id}:c:{address.id}:1")
        await h.callback(f"tp_condf:{template_id}:custom")
        await h.text(" ([адрес])")
        # Предпросмотр показывает ровно введённый фрагмент.
        self.assertIn(" ([адрес])", h.screen_text())
        self.assertIn("📍 [место]", h.screen_text().split("Кирпичик пуст:")[-1])
        await h.callback(f"tp_conds:{template_id}")
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].fragment, " ([адрес])")


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class ConditionalAnnouncementTests(unittest.TestCase):
    """§42-А/Б: правило, созданное через интерфейс, работает в анонсе."""

    def _seed_place(self, h):
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            place = create_place(
                session,
                "Дом культуры",
                city="Тестовый город",
                created_by_user_id=user.id,
            )
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=user.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
            return place.id

    def _draft(self, h, template_id: int):
        place_id = self._seed_place(h)
        with h.session_factory() as session:
            draft = start_announcement_wizard(
                session, actor_telegram_user_id=h.user_id, place_id=place_id
            )
            confirmed = confirm_announcement_place(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=draft.draft_id,
                expected_revision=draft.revision,
            )
            return choose_announcement_template(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=confirmed.draft_id,
                template_id=template_id,
                expected_revision=confirmed.revision,
            )

    @scenario_test
    async def test_end_time_hidden_in_announcement(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
            start_brick = get_system_brick(session, "время_начала")
            assert start_brick is not None
        await h.callback(f"tp_condb:{template_id}:c:{end_brick.id}:1")
        await h.callback(f"tp_condf:{template_id}:sep")
        await h.callback(f"tp_conduse:{template_id}")
        await h.callback(f"tp_conds:{template_id}")
        self.assertEqual(len(_parts(h, template_id)), 1)

        chosen = self._draft(h, template_id)
        with h.session_factory() as session:
            saved = save_announcement_value(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=chosen.draft_id,
                brick_id=start_brick.id,
                value="19:00",
                expected_revision=chosen.revision,
            )
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=saved.draft_id,
            )
            self.assertEqual(preview.html, "19:00", "§42-А: пустое время скрыто")

            filled = save_announcement_value(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=saved.draft_id,
                brick_id=end_brick.id,
                value="22:00",
                expected_revision=saved.revision,
            )
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=filled.draft_id,
            )
            self.assertEqual(preview.html, "19:00–22:00")

    @scenario_test
    async def test_price_line_disappears_without_empty_line(self, h) -> None:
        content = "Афиша\n\n💰 Цена: [цена]\n\nКонтакты"
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            from brick_store import create_personal_brick

            create_personal_brick(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                code="цена",
                title="Цена",
                value_type="short_text",
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="С ценой",
                content=content,
            )
            session.commit()
            template_id = template.template_id
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        await h.callback(_callback(h, f"tp_condb:{template_id}:c:"))
        # «Целая строка» — один из предложенных вариантов.
        self.assertIn(
            "Скрыть целую строку", [label for label, _d in h.screen_buttons()]
        )
        await h.callback(f"tp_condf:{template_id}:line")
        self.assertIn("💰 Цена: [цена]", h.screen_text())
        await h.callback(f"tp_conduse:{template_id}")
        await h.callback(f"tp_conds:{template_id}")

        chosen = self._draft(h, template_id)
        with h.session_factory() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=chosen.draft_id,
            )
        self.assertEqual(preview.html, "Афиша\n\nКонтакты", "без лишней пустой строки")


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class ConditionalStaleAndAccessTests(unittest.TestCase):
    """§44 и права: stale-правило видно, но не применяется; чужое не открыть."""

    @scenario_test
    async def test_stale_rule_is_shown_and_not_applied(self, h) -> None:
        template_id, revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:c:{end_brick.id}:1")
        await h.callback(f"tp_condf:{template_id}:sep")
        await h.callback(f"tp_conduse:{template_id}")
        await h.callback(f"tp_conds:{template_id}")

        # Текст шаблона меняется мимо интерфейса правил: фрагмент исчез.
        with h.session_factory() as session:
            result = update_template_content(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                content="Совсем другой текст",
            )
            self.assertTrue(result.ok, result.message)
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1, "правило не удалено молча")
        self.assertEqual(parts[0].status, STATUS_STALE)

        # Карточка честно предупреждает (и не меняет правила при отрисовке).
        await h.callback("tp_hub")
        await h.callback(f"tp_open:{template_id}")
        self.assertIn("требуют перенастройки", h.screen_text())
        self.assertEqual(_parts(h, template_id)[0].status, STATUS_STALE)

        # Stale-правило видно в списке и помечено.
        await h.callback(f"tp_cond:{template_id}")
        await h.callback(f"tp_condl:{template_id}")
        self.assertIn("требует перенастройки", h.screen_text())
        self.assertEqual(
            [part.status for part in _parts(h, template_id)], [STATUS_STALE]
        )

    @scenario_test
    async def test_reset_shows_rule_as_stale_and_keeps_list_button(self, h) -> None:
        """MAJOR-2/MINOR-3: сброс делает правило видимым stale, кнопка списка жива."""
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:c:{end_brick.id}:1")
        await h.callback(f"tp_condf:{template_id}:sep")
        await h.callback(f"tp_conduse:{template_id}")
        await h.callback(f"tp_conds:{template_id}")
        self.assertEqual(len(_parts(h, template_id)), 1)

        # Сброс к стандарту создаёт новую версию: правило обязано стать
        # видимым «требует перенастройки», а не исчезнуть из редактора.
        await h.callback(f"tp_condback:{template_id}:card")
        await h.callback(f"tp_reset:{template_id}")
        await h.callback(f"tp_reset_yes:{template_id}")
        self.assertIn("требуют перенастройки", h.screen_text())
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1, "правило не удалено молча (§44)")
        self.assertEqual(parts[0].status, STATUS_STALE)

        await h.callback(f"tp_cond:{template_id}")
        await h.callback(f"tp_condl:{template_id}")
        self.assertIn("требует перенастройки", h.screen_text())

    @scenario_test
    async def test_missing_entry_keeps_list_button(self, h) -> None:
        """MINOR-3: без вхождений кирпичиков список правил остаётся доступен."""
        template_id, revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:c:{end_brick.id}:1")
        await h.callback(f"tp_condf:{template_id}:sep")
        await h.callback(f"tp_conduse:{template_id}")
        await h.callback(f"tp_conds:{template_id}")

        # Возвращаемся на шаг выбора фрагмента и меняем текст в другом окне:
        # выбранного вхождения больше нет, но правило осталось.
        await h.callback(f"tp_condback:{template_id}:fragment")
        with h.session_factory() as session:
            result = update_template_content(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                content="Совсем другой текст",
            )
            self.assertTrue(result.ok, result.message)
        await h.callback(f"tp_condback:{template_id}:fragment")

        self.assertIn("вхождение кирпичика исчезло", h.screen_text())
        labels = [label for label, _data in h.screen_buttons()]
        self.assertIn("📋 Все условные части", labels)
        self.assertIn(f"tp_condl:{template_id}", _callbacks(h, "tp_condl:"))

    @scenario_test
    async def test_foreign_personal_template_is_refused(self, h) -> None:
        with h.session_factory() as session:
            _user(session, OTHER_TG)
            template = create_personal_template(
                session,
                actor_telegram_user_id=OTHER_TG,
                name="Чужой",
                content=TIME_TEMPLATE,
            )
            session.commit()
            foreign_id = template.template_id
        await h.text("/templates")
        answers_before = len(h.answer_texts())
        await h.callback(f"tp_cond:{foreign_id}")
        # Ни раздела правил, ни изменений в БД: чужой шаблон неотличим от
        # несуществующего.
        self.assertGreater(len(h.answer_texts()), answers_before)
        self.assertEqual(_parts(h, foreign_id), [])
        self.assertNotIn("Выберите кирпичик", h.screen_text())
