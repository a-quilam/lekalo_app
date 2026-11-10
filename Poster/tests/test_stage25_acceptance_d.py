# tests/test_stage25_acceptance_d.py
"""
Poster 2.5, независимая приёмка этапа D (условные части шаблонов) — Tester.

Проверяется утверждённое пользовательское поведение (spec §38–45, §59), а
не реализация Developer. Сценарии идут от того, что видит автор в чате и что
получается в готовом анонсе: экраны маршрута, точный текст кнопок,
callback_data, сохранение только после предпросмотра, поведение при
изменении шаблона, права и перезапуск.

Покрытие не дублирует соседние наборы, а дополняет их приёмочными
проверками:

1. §40 (2.5.2 §6–7) — маршрут `карточка → «Условные части» → список →
   кирпичик → способ скрытия → сохранение → список` и «Назад» на каждом
   шаге; выбранный способ сохраняется сразу.
2. §42 — обязательные примеры А–Е сквозным рендером анонса (не только
   `apply_conditional_parts`): тире, цена, адрес, абзац, повтор.
3. §42/§45 — фрагмент внутри форматирования не оставляет пустых тегов.
4. §43 — простой и расширенный шаблоны независимы: правило расширенной
   секции срабатывает внутри расширенного и не влияет на простой.
5. §44 — правка текста: однозначный фрагмент переносится, неоднозначный
   становится `stale` и не применяется к случайному месту; сброс к
   стандарту не удаляет правило молча.
6. §44 — права: чужой личный шаблон и стандарт без админских прав
   недоступны, админ места и суперадмин — доступны.
7. §44 — правила переживают перезапуск (новое `Application`, тот же файл БД).
8. §39 — в тексте шаблона нет синтаксиса условий: `{{…}}`, `{%…%}`,
   `<if>` остаются обычным текстом.

Регрессия приёмки: §42-В проверяется в ОБЕИХ сторонах (` ([адрес])`:
заполнен → адрес в скобках, пуст → только место). Раньше парсер принимал
`([адрес])` за гиперссылку с пустой подписью и терял значение.

Реальная `post_bot.db`, `.env` и сеть Telegram не используются: настоящий
`Application.process_update` поверх временной SQLite (`Harness`).

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage25_acceptance_d -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import select

from tests import ui_texts_expect as cat

try:
    from announcement_store import (
        add_announcement_extra,
        build_announcement_preview,
        build_announcement_render_payload,
        choose_announcement_template,
        confirm_announcement_place,
        save_announcement_value,
        start_announcement_wizard,
    )
    from brick_store import get_system_brick
    from conditional_parts import (
        STATUS_ACTIVE,
        STATUS_STALE,
        active_rules_for_version,
        create_conditional_part,
    )
    from models import PlaceAdmin, Template, utcnow
    from place_store import create_place
    from template_store import (
        create_personal_template,
        get_standard_template,
        render_template_version,
        update_template_content,
    )
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.stage23_helpers import OTHER_TG, SUPERADMIN_TG
    from tests.test_stage25_conditional_ui import (
        _authored_parts,
        _open_card,
        _parts,
        _template_with_system_time_bricks,
    )
    from tests.test_stage27b_announcement_ui import (
        _seed_personal_template,
        _seed_place,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# --- помощники -------------------------------------------------------------


def _callbacks(h, prefix: str, suffix: str | None = None) -> list[str]:
    return [
        data
        for _label, data in h.screen_buttons()
        if data
        and data.startswith(prefix)
        and (suffix is None or data.endswith(suffix))
    ]


def _labels(h) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _draft_preview(h, template_id: int, values: dict[int, str], place_id: int) -> str:
    """
    Сквозной путь §59: черновик → шаблон → значения → готовый анонс.

    Возвращает HTML готового анонса — то, что уйдёт в Telegram.
    """
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
        details = choose_announcement_template(
            session,
            actor_telegram_user_id=h.user_id,
            draft_id=confirmed.draft_id,
            template_id=template_id,
            expected_revision=confirmed.revision,
        )
    for brick_id, value in values.items():
        with h.session_factory() as session:
            details = save_announcement_value(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=details.draft_id,
                brick_id=brick_id,
                value=value,
                expected_revision=details.revision,
            )
    with h.session_factory() as session:
        return build_announcement_preview(
            session, actor_telegram_user_id=h.user_id, draft_id=details.draft_id
        ).html


def _rule(
    h,
    template_id: int,
    revision: int,
    brick_id: int,
    fragment: str,
    *,
    section: str = "content",
    occurrence: int = 1,
):
    with h.session_factory() as session:
        return create_conditional_part(
            session,
            actor_telegram_user_id=h.user_id,
            template_id=template_id,
            expected_revision=revision,
            section=section,
            brick_id=brick_id,
            fragment=fragment,
            occurrence=occurrence,
        )


def _active_rules(h, version_id: int):
    """Активные правила версии: устаревшее правило не применяется (§44)."""
    with h.session_factory() as session:
        return active_rules_for_version(session, version_id, "extended_content")


def _template_revision(h, template_id: int) -> int:
    with h.session_factory() as session:
        row = session.get(Template, template_id)
        assert row is not None
        return int(row.revision)


def _place_with_admin(h, admin_tg: int) -> tuple[int, int]:
    """Место, его действующий админ и автоматически созданный стандарт."""
    with h.session_factory() as session:
        admin = get_or_create_user(session, admin_tg, username=f"admin{admin_tg}")
        place = create_place(
            session,
            f"Место {admin_tg}",
            city="Тестовый город",
            created_by_user_id=admin.id,
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
        template = session.scalar(
            select(Template).where(
                Template.place_id == place.id, Template.kind == "standard"
            )
        )
        assert template is not None
        template_id = template.id
        # Poster 2.5.2 §6: раздел правил есть только у расширенного шаблона,
        # поэтому стандарт получает расширенную версию явно.
        details = get_standard_template(session, admin_tg, place.id)
        result = update_template_content(
            session,
            actor_telegram_user_id=admin_tg,
            template_id=template_id,
            expected_revision=details.revision,
            content=details.current_version.content,
            extended_content=details.current_version.content,
        )
        assert result.ok, result.message
        return place.id, template_id


# --- 1. маршрут §40 --------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RouteTests(unittest.TestCase):
    """§40: карточка → список → кирпичик → способ скрытия → сохранение."""

    @scenario_test
    async def test_route_saves_rule_immediately(self, h) -> None:
        template_id, revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        self.assertIn(
            (cat.text("buttons.templates.uslovnye_chasti"), f"tp_cond:{template_id}"),
            h.screen_buttons(),
            "в редакторе шаблона есть раздел условий (§3)",
        )

        await h.callback(f"tp_cond:{template_id}")
        self.assertIn(
            cat.text("templates.cond.title"), h.screen_text(), "единый экран условий"
        )
        await h.callback(f"tp_condadd:{template_id}")

        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        self.assertIn("Время окончания", _labels(h), "кирпичик из текста")
        self.assertEqual(
            len(_callbacks(h, f"tp_condb:{template_id}:e:")),
            2,
            "по кнопке на кирпичик, без номеров вхождений",
        )

        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        self.assertEqual(
            _labels(h)[:3],
            [
                "Скрыть всю строку",
                "Скрыть с разделителем",
                "Скрыть свой фрагмент",
            ],
            "§40 шаг 2: понятные варианты фрагмента",
        )
        await h.callback(f"tp_condf:{template_id}:sep")

        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1, "правило появилось сразу после выбора")
        self.assertEqual(parts[0].fragment, "–[время_окончания]")
        self.assertEqual(parts[0].status, STATUS_ACTIVE)
        self.assertIn(
            "Время окончания ✓", _labels(h), "§3: настроенный кирпичик помечен"
        )
        self.assertEqual(
            _template_revision(h, template_id), revision, "правило не создаёт версию"
        )

    @scenario_test
    async def test_back_keeps_context_on_every_step(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        await h.callback(f"tp_condadd:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:custom")

        await h.callback(f"tp_condback:{template_id}:mode")
        cat.assert_shows(self, h.screen_text(), "templates.cond.mode_title")
        await h.callback(f"tp_condback:{template_id}:bricks")
        self.assertIn("Время окончания", _labels(h), "кирпичик не потерян")
        await h.callback(f"tp_condback:{template_id}:list")
        self.assertIn(cat.text("templates.cond.title"), h.screen_text())
        await h.callback(f"tp_condback:{template_id}:card")
        cat.assert_shows(
            self,
            h.screen_text(),
            "templates.card.title",
            "вернулись в редактор шаблона",
        )
        self.assertEqual(_parts(h, template_id), [], "«Назад» ничего не сохранил")

    @scenario_test
    async def test_custom_fragment_validation(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        await h.callback(f"tp_condadd:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:custom")

        # §40 шаг 3: фрагмент обязан существовать и включать кирпичик.
        await h.text("–[совсем_другой]")
        self.assertIn("Свой фрагмент", h.screen_text(), "автор остался на вводе")
        cat.assert_shows(
            self, h.screen_text(), "templates.cond.custom_not_all", "причина названа"
        )
        self.assertEqual(_parts(h, template_id), [])

        await h.text("–[время_окончания]")
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1, "правило сохранено")
        self.assertEqual(parts[0].fragment, "–[время_окончания]")
        # Фрагмент совпал с разделителем: экран показывает кирпичик с «✓»,
        # а не сырой текст правила (§3, §7).
        self.assertIn("Время окончания ✓", _labels(h))
        self.assertNotIn("–[время_окончания]", "".join(_labels(h)))


# --- 2. рендер §42 А–Е -----------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RenderExampleTests(unittest.TestCase):
    """§42: обязательные примеры сквозным рендером готового анонса."""

    @scenario_test
    async def test_example_a_end_time(self, h) -> None:
        template_id, revision, bricks = _seed_personal_template(
            h,
            name="А",
            specs=[
                ("st", "Старт", "short_text", {}),
                ("en", "Конец", "short_text", {}),
            ],
            content="[st]–[en]",
        )
        self.assertTrue(_rule(h, template_id, revision, bricks["en"], "–[en]").ok)
        place_id, _alias = _seed_place(h)
        self.assertEqual(
            _draft_preview(h, template_id, {bricks["st"]: "19:00"}, place_id),
            "19:00",
            "§42-А: пустое время окончания скрывает тире",
        )
        self.assertEqual(
            _draft_preview(
                h, template_id, {bricks["st"]: "19:00", bricks["en"]: "22:00"}, place_id
            ),
            "19:00–22:00",
            "§42-А: заполненное время остаётся",
        )

    @scenario_test
    async def test_example_b_price_line(self, h) -> None:
        template_id, revision, bricks = _seed_personal_template(
            h,
            name="Б",
            specs=[
                ("main", "Основное", "short_text", {}),
                ("cost", "Цена", "short_text", {}),
            ],
            content="[main]\n\n💰 Цена: [cost]\n\nКонтакты",
        )
        self.assertTrue(
            _rule(h, template_id, revision, bricks["cost"], "💰 Цена: [cost]").ok
        )
        place_id, _alias = _seed_place(h)
        self.assertEqual(
            _draft_preview(h, template_id, {bricks["main"]: "Афиша"}, place_id),
            "Афиша\n\nКонтакты",
            "§42-Б: строка исчезает без лишней пустой строки",
        )
        self.assertEqual(
            _draft_preview(
                h,
                template_id,
                {bricks["main"]: "Афиша", bricks["cost"]: "500 ₽"},
                place_id,
            ),
            "Афиша\n\n💰 Цена: 500 ₽\n\nКонтакты",
        )

    @scenario_test
    async def test_example_v_address_both_sides(self, h) -> None:
        """§42-В в обе стороны — регресс MAJOR-A (пробел перед «(»)."""
        template_id, revision, bricks = _seed_personal_template(
            h,
            name="В",
            specs=[
                ("mesto", "Место", "short_text", {}),
                ("adres", "Адрес", "short_text", {}),
            ],
            content="📍 [mesto] ([adres])",
        )
        self.assertTrue(
            _rule(h, template_id, revision, bricks["adres"], " ([adres])").ok
        )
        place_id, _alias = _seed_place(h)
        self.assertEqual(
            _draft_preview(
                h, template_id, {bricks["mesto"]: "РанДом Культуры"}, place_id
            ),
            "📍 РанДом Культуры",
            "§42-В: пустой адрес скрывает скобки целиком",
        )
        self.assertEqual(
            _draft_preview(
                h,
                template_id,
                {
                    bricks["mesto"]: "РанДом Культуры",
                    bricks["adres"]: "Большой Николопесковский, 13",
                },
                place_id,
            ),
            "📍 РанДом Культуры (Большой Николопесковский, 13)",
            "§42-В: заполненный адрес остаётся в скобках (не теряется как ссылка)",
        )

    @scenario_test
    async def test_example_d_paragraph(self, h) -> None:
        template_id, revision, bricks = _seed_personal_template(
            h,
            name="Д",
            specs=[
                ("who", "Кто", "short_text", {}),
                ("cost", "Цена", "short_text", {}),
            ],
            content="[who]\n\nЦена: [cost]\nпродолжение\n\nФинал",
        )
        self.assertTrue(
            _rule(
                h, template_id, revision, bricks["cost"], "Цена: [cost]\nпродолжение"
            ).ok
        )
        place_id, _alias = _seed_place(h)
        self.assertEqual(
            _draft_preview(h, template_id, {bricks["who"]: "Все"}, place_id),
            "Все\n\nФинал",
            "§42-Д: после скрытия абзаца нет лишнего пустого абзаца",
        )

    @scenario_test
    async def test_example_e_repeated_fragment(self, h) -> None:
        template_id, revision, bricks = _seed_personal_template(
            h,
            name="Е",
            specs=[("who", "Кто", "short_text", {})],
            content="[who] — основное\n\n[who] — гость",
        )
        self.assertTrue(
            _rule(h, template_id, revision, bricks["who"], "[who]", occurrence=2).ok
        )
        place_id, _alias = _seed_place(h)
        self.assertEqual(
            _draft_preview(h, template_id, {bricks["who"]: ""}, place_id),
            " — гость",
            "§42-Е: скрыто именно выбранное второе вхождение",
        )
        self.assertEqual(
            _draft_preview(h, template_id, {bricks["who"]: "Все"}, place_id),
            "Все — основное\n\nВсе — гость",
            "§42-Е: без разрешения ничего не удаляется",
        )

    @scenario_test
    async def test_formatting_fragment_leaves_no_empty_tags(self, h) -> None:
        """Регресс MAJOR-1: фрагмент внутри `**…**` не оставляет `<b></b>`."""
        template_id, revision, bricks = _seed_personal_template(
            h,
            name="Формат",
            specs=[
                ("st", "Старт", "short_text", {}),
                ("cost", "Цена", "short_text", {}),
                ("who", "Кто", "short_text", {}),
            ],
            content="🕒 [st]\n**💰 Цена: [cost]**\nКто: [who]",
        )
        self.assertTrue(
            _rule(h, template_id, revision, bricks["cost"], "💰 Цена: [cost]").ok
        )
        place_id, _alias = _seed_place(h)
        html = _draft_preview(
            h, template_id, {bricks["st"]: "19:00", bricks["who"]: "Все"}, place_id
        )
        self.assertEqual(html, "🕒 19:00\nКто: Все", "§45: пустых тегов не осталось")
        self.assertNotIn("<b>", html, "нет пустой жирной строки")
        self.assertEqual(
            _draft_preview(
                h,
                template_id,
                {bricks["st"]: "19:00", bricks["cost"]: "500 ₽", bricks["who"]: "Все"},
                place_id,
            ),
            "🕒 19:00\n<b>💰 Цена: 500 ₽</b>\nКто: Все",
            "§45: заполненный фрагмент сохраняет форматирование",
        )


# --- 3. простой и расширенный шаблоны §43 ----------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ExtendedIndependenceTests(unittest.TestCase):
    """§43: видимость фрагмента и выбор версии шаблона независимы."""

    @scenario_test
    async def test_extended_rule_works_inside_extended_only(self, h) -> None:
        template_id, revision, bricks = _seed_personal_template(
            h,
            name="Расширенный",
            specs=[
                ("main", "Основное", "short_text", {}),
                ("cost", "Цена", "short_text", {}),
                ("end", "Конец", "short_text", {}),
            ],
            content="[main]",
            extended_content="[main]\n💰 Цена: [cost]\n🕒 [end]",
        )
        for code, fragment in (("cost", "💰 Цена: [cost]"), ("end", "🕒 [end]")):
            self.assertTrue(
                _rule(
                    h,
                    template_id,
                    revision,
                    bricks[code],
                    fragment,
                    section="extended_content",
                ).ok
            )
        place_id, _alias = _seed_place(h)
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
            details = choose_announcement_template(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=confirmed.draft_id,
                template_id=template_id,
                expected_revision=confirmed.revision,
            )
            details = save_announcement_value(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=details.draft_id,
                brick_id=bricks["main"],
                value="Главное",
                expected_revision=details.revision,
            )
            payload = build_announcement_render_payload(
                session, actor_telegram_user_id=h.user_id, draft_id=details.draft_id
            )
            self.assertFalse(payload.use_extended, "§43: без доп. полей — простой")
            self.assertEqual(
                build_announcement_preview(
                    session, actor_telegram_user_id=h.user_id, draft_id=details.draft_id
                ).html,
                "Главное",
            )
            # §42-Г: цена заполнена, время окончания пусто → расширенный включён,
            # но пустая строка со временем не отображается.
            details = add_announcement_extra(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=details.draft_id,
                brick_id=bricks["cost"],
                value="500 ₽",
            )
            details = add_announcement_extra(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=details.draft_id,
                brick_id=bricks["end"],
                value=None,
            )
            payload = build_announcement_render_payload(
                session, actor_telegram_user_id=h.user_id, draft_id=details.draft_id
            )
            self.assertTrue(
                payload.use_extended, "хотя бы одно доп. поле → расширенный"
            )
            self.assertEqual(
                build_announcement_preview(
                    session, actor_telegram_user_id=h.user_id, draft_id=details.draft_id
                ).html,
                "Главное\n💰 Цена: 500 ₽",
                "§42-Г: пустое время скрыто, цена показана",
            )

    @scenario_test
    async def test_extended_section_rule_saved_through_ui(self, h) -> None:
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Секция",
            specs=[
                ("main", "Основное", "short_text", {}),
                ("cost", "Цена", "short_text", {}),
            ],
            content="[main]",
            extended_content="[main]\n💰 Цена: [cost]",
        )
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        await h.callback(f"tp_condadd:{template_id}")
        # §3: условия только у расширенного шаблона, секция не подписывается.
        self.assertIn("Цена", _labels(h), "кирпичик расширенного текста доступен")
        self.assertNotIn("основной текст", h.screen_text().lower())
        await h.callback(f"tp_condb:{template_id}:e:{bricks['cost']}")
        await h.callback(f"tp_condf:{template_id}:line")
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].section, "extended_content", "правило в расширенной")
        self.assertEqual(parts[0].fragment, "💰 Цена: [cost]")

        place_id, _alias = _seed_place(h)
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
            details = choose_announcement_template(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=confirmed.draft_id,
                template_id=template_id,
                expected_revision=confirmed.revision,
            )
            details = save_announcement_value(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=details.draft_id,
                brick_id=bricks["main"],
                value="Главное",
                expected_revision=details.revision,
            )
            details = add_announcement_extra(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=details.draft_id,
                brick_id=bricks["cost"],
                value=None,
            )
            payload = build_announcement_render_payload(
                session, actor_telegram_user_id=h.user_id, draft_id=details.draft_id
            )
            self.assertTrue(payload.use_extended, "доп. поле выбрано — расширенный")
            self.assertEqual(
                build_announcement_preview(
                    session, actor_telegram_user_id=h.user_id, draft_id=details.draft_id
                ).html,
                "Главное",
                "§43: правило расширенной секции скрывает строку в ней самой",
            )


# --- 4. правка шаблона и сброс §44 -----------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RuleEditTests(unittest.TestCase):
    """§44: однозначный перенос, stale вместо случайного места, сброс."""

    @scenario_test
    async def test_unique_fragment_transfers_to_new_version(self, h) -> None:
        template_id, revision = _template_with_system_time_bricks(h)
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_cond:{template_id}")
        await h.callback(f"tp_condadd:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:sep")

        with h.session_factory() as session:
            result = update_template_content(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                content="Добавка\n[время_начала]–[время_окончания]",
            )
            self.assertTrue(result.ok, result.message)
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1, "правило не удалено молча")
        self.assertEqual(
            parts[0].status, STATUS_ACTIVE, "уникальный фрагмент перенесён"
        )
        self.assertEqual(parts[0].version, 2, "правило живёт в новой версии")

    @scenario_test
    async def test_ambiguous_fragment_becomes_stale(self, h) -> None:
        template_id, revision, bricks = _seed_personal_template(
            h,
            name="Неоднозначный",
            specs=[
                ("st", "Старт", "short_text", {}),
                ("en", "Конец", "short_text", {}),
            ],
            content="[st]–[en]",
            extended_content="[st]–[en]",
        )
        # §3: условие принадлежит расширенному тексту.
        self.assertTrue(
            _rule(
                h,
                template_id,
                revision,
                bricks["en"],
                "–[en]",
                section="extended_content",
            ).ok
        )

        with h.session_factory() as session:
            result = update_template_content(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                content="[st]–[en] (резерв: –[en])",
                extended_content="[st]–[en] (резерв: –[en])",
            )
            self.assertTrue(result.ok, result.message)
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1, "правило не удалено молча")
        self.assertEqual(parts[0].status, STATUS_STALE, "два вхождения → перенастройка")
        self.assertEqual(
            _active_rules(h, parts[0].version_id),
            (),
            "§44: устаревшее правило не применяется к случайному месту",
        )

    @scenario_test
    async def test_reset_keeps_rule_that_transfers_to_standard(self, h) -> None:
        """
        Этап A1, §2.3/§3: сброс переносит авторское правило, пока кирпичик есть.

        Сброс возвращает встроенный стандарт v5, у которого есть расширенный
        текст с `[время_окончания]`, поэтому правило `–[время_окончания]`
        переезжает в новую версию активным. Удалять правило и скрывать раздел
        условий больше нельзя: текст расширения никуда не исчез.
        """
        template_id, _revision = _template_with_system_time_bricks(h)
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_cond:{template_id}")
        await h.callback(f"tp_condadd:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:sep")
        self.assertEqual(len(_parts(h, template_id)), 1)

        await h.callback(f"tp_condback:{template_id}:card")
        await h.callback(f"tp_reset:{template_id}")
        await h.callback(f"tp_reset_yes:{template_id}")
        self.assertNotIn("требуют перенастройки", h.screen_text(), "плашки нет")
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1, "правило не потеряно при сбросе")
        rule = parts[0]
        self.assertEqual(rule.fragment, "–[время_окончания]")
        self.assertEqual(rule.status, STATUS_ACTIVE, "кирпичик есть — правило активно")
        self.assertEqual(rule.version, 2, "правило живёт в новой версии")
        # Раздел условий остаётся доступным: у стандарта v5 есть расширенный
        # текст, правила которого видны автору.
        self.assertIn(
            f"tp_cond:{template_id}",
            _callbacks(h, f"tp_cond:{template_id}"),
            "расширенный текст стандарта есть — раздел условий доступен",
        )


# --- 5. права §44 ----------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AccessTests(unittest.TestCase):
    """§44: личный шаблон — владельцу, стандарт — админу места/суперадмину."""

    @scenario_test
    async def test_foreign_personal_template_is_refused(self, h) -> None:
        with h.session_factory() as session:
            get_or_create_user(session, OTHER_TG, username="other")
            template = create_personal_template(
                session,
                actor_telegram_user_id=OTHER_TG,
                name="Чужой",
                content="[время_начала]–[время_окончания]",
            )
            session.commit()
            foreign_id = template.template_id
        await h.text("/templates")
        answers_before = len(h.answer_texts())
        await h.callback(f"tp_cond:{foreign_id}")
        self.assertGreater(len(h.answer_texts()), answers_before, "автор получил отказ")
        self.assertTrue(h.answer_texts()[-1])
        self.assertEqual(_parts(h, foreign_id), [], "правил не создано")
        self.assertNotIn("Выберите кирпичик", h.screen_text())

    @scenario_test
    async def test_standard_template_admin_and_non_admin(self, h) -> None:
        """
        §44: админ места проходит маршрут, пользователь без прав — нет.

        Этап A1, §2: стандарт создаётся с 11 встроенными правилами, поэтому
        «ничего не изменилось» проверяется по авторским правилам и точному
        составу встроенных. `_place_with_admin` заменяет расширенный текст на
        простой (`extended_content=content`); после этого правила расширенной
        секции, чьих кирпичиков в новом тексте нет, удаляются, а правило
        `', [адрес]'` (кирпичик есть, но фрагмент неоднозначен в двух местах)
        становится `stale`.
        """
        place_id, standard_id = _place_with_admin(h, OTHER_TG)
        rules_before = _parts(h, standard_id, OTHER_TG)
        self.assertEqual(len(rules_before), 6, "состав встроенных правил после правки")
        self.assertEqual(
            sorted((part.section, part.code, part.status) for part in rules_before),
            sorted(
                [
                    ("content", "адрес", STATUS_ACTIVE),
                    ("content", "описание", STATUS_ACTIVE),
                    ("content", "контакт", STATUS_ACTIVE),
                    ("extended_content", "описание", STATUS_ACTIVE),
                    ("extended_content", "контакт", STATUS_ACTIVE),
                    ("extended_content", "адрес", STATUS_STALE),
                ]
            ),
            "3 простых правила + 2 перенесённых расширенных + stale адрес",
        )
        self.assertEqual(
            _authored_parts(h, standard_id, OTHER_TG), [], "авторских правил нет"
        )

        # Действующий админ места проходит маршрут (регресс MINOR-1).
        await h.callback(f"tp_std:{place_id}", user_id=OTHER_TG)
        await h.callback(f"tp_cond:{standard_id}", user_id=OTHER_TG)
        self.assertIn(
            cat.text("templates.cond.title"),
            h.screen_text(),
            "экран условий открылся",
        )
        await h.callback(f"tp_condadd:{standard_id}", user_id=OTHER_TG)
        self.assertIn(cat.text("templates.cond.title"), h.screen_text())
        brick_buttons = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith(f"tp_condb:{standard_id}:")
        ]
        self.assertTrue(brick_buttons, "кирпичики стандарта доступны админу")
        answers_before = len(h.answer_texts())
        await h.callback(brick_buttons[0], user_id=OTHER_TG)
        self.assertEqual(
            h.answer_texts()[answers_before:], [], "нет ложного «кирпичик исчез»"
        )
        cat.assert_shows(self, h.screen_text(), "templates.cond.mode_title")

        # Пользователь без прав на место — отказ, ничего не меняется.
        answers_before = len(h.answer_texts())
        await h.callback(f"tp_cond:{standard_id}")
        self.assertGreater(len(h.answer_texts()), answers_before, "нужен отказ")
        self.assertEqual(
            _parts(h, standard_id, OTHER_TG), rules_before, "правила не изменились"
        )
        self.assertEqual(
            _authored_parts(h, standard_id, OTHER_TG), [], "авторских правил нет"
        )

    @scenario_test
    async def test_superadmin_can_open_standard_rules(self, h) -> None:
        place_id, standard_id = _place_with_admin(h, OTHER_TG)
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
            await h.text("/start", user_id=SUPERADMIN_TG)
            await h.callback(f"tp_std:{place_id}", user_id=SUPERADMIN_TG)
            cat.assert_shows(self, h.screen_text(), "templates.card.title")
            answers_before = len(h.answer_texts())
            await h.callback(f"tp_cond:{standard_id}", user_id=SUPERADMIN_TG)
            self.assertEqual(h.answer_texts()[answers_before:], [], "без отказа")
            self.assertIn(
                cat.text("templates.cond.title"),
                h.screen_text(),
                "экран открылся",
            )
            await h.callback(f"tp_condadd:{standard_id}", user_id=SUPERADMIN_TG)
            self.assertIn(cat.text("templates.cond.title"), h.screen_text())


# --- 6. перезапуск §44 -----------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PersistenceTests(unittest.TestCase):
    """§44: правила хранятся в БД и переживают перезапуск."""

    @scenario_test
    async def test_rule_survives_restart(self, h) -> None:
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Рестарт",
            specs=[
                ("mesto", "Место", "short_text", {}),
                ("adres", "Адрес", "short_text", {}),
            ],
            content="📍 [mesto]",
            extended_content="📍 [mesto] ([adres])",
        )
        await _open_card(h, template_id)
        await h.callback(f"tp_cond:{template_id}")
        await h.callback(f"tp_condadd:{template_id}")
        await h.callback(f"tp_condb:{template_id}:e:{bricks['adres']}")
        await h.callback(f"tp_condf:{template_id}:custom")
        await h.text(" ([adres])")
        self.assertEqual(_parts(h, template_id)[0].fragment, " ([adres])")

        await h.restart()
        self.assertEqual(h.user_data(), {}, "состояние диалога потеряно, как в жизни")

        await h.text("/start")
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_cond:{template_id}")
        self.assertIn("Адрес ✓", _labels(h), "правило видно после рестарта")
        self.assertEqual(len(_parts(h, template_id)), 1)

        place_id, _alias = _seed_place(h)
        self.assertEqual(
            _draft_preview(h, template_id, {bricks["mesto"]: "РанДом"}, place_id),
            "📍 РанДом",
            "§43: основной текст без правил расширенной секции",
        )
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
            details = choose_announcement_template(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=confirmed.draft_id,
                template_id=template_id,
                expected_revision=confirmed.revision,
            )
            details = save_announcement_value(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=details.draft_id,
                brick_id=bricks["mesto"],
                value="РанДом",
                expected_revision=details.revision,
            )
            details = add_announcement_extra(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=details.draft_id,
                brick_id=bricks["adres"],
                value=None,
            )
            self.assertEqual(
                build_announcement_preview(
                    session, actor_telegram_user_id=h.user_id, draft_id=details.draft_id
                ).html,
                "📍 РанДом",
                "правило расширенной секции применяется после рестарта",
            )


# --- 7. нет синтаксиса условий §39 ----------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NoConditionSyntaxTests(unittest.TestCase):
    """§39: шаблон остаётся обычным текстом, условия живут отдельно."""

    @scenario_test
    async def test_condition_like_text_is_literal(self, h) -> None:
        template_id, revision, bricks = _seed_personal_template(
            h,
            name="Синтаксис",
            specs=[("cost", "Цена", "short_text", {})],
            content="Цена: {{cost}} или {% if cost %} [cost]",
        )
        raw = "Цена: {{cost}} или {% if cost %} [cost]"
        with h.session_factory() as session:
            from models import TemplateVersion

            stored = session.scalar(
                select(TemplateVersion.content)
                .where(TemplateVersion.template_id == template_id)
                .order_by(TemplateVersion.version.desc())
            )
        self.assertEqual(stored, raw, "§39: текст шаблона хранится дословно")
        self.assertEqual(
            _template_revision(h, template_id), revision, "текст сохранён как есть"
        )
        self.assertIn("{{cost}}", raw)
        self.assertIn("{% if cost %}", raw)
        with h.session_factory() as session:
            self.assertEqual(
                render_template_version(
                    session,
                    actor_telegram_user_id=h.user_id,
                    template_id=template_id,
                    values_by_id={bricks["cost"]: "500 ₽"},
                ),
                "Цена: {{cost}} или {% if cost %} 500 ₽",
                "§39: псевдосинтаксис не исполняется, кирпичик подставлен",
            )
        # Правило создаётся отдельно и не требует синтаксиса в тексте.
        self.assertTrue(_rule(h, template_id, revision, bricks["cost"], "[cost]").ok)
        place_id, _alias = _seed_place(h)
        self.assertEqual(
            _draft_preview(h, template_id, {}, place_id),
            "Цена: {{cost}} или {% if cost %} ",
            "скрытие работает над обычным текстом",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
