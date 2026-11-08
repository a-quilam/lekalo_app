# tests/test_stage25_conditional_ui.py
"""
Poster 2.5.3 §3 — Telegram-интерфейс условий расширенного шаблона.

Обновления проходят через настоящее приложение `build_application`, как в
Stage 9: проверяются экраны, callback-кнопки, тексты каталога,
оптимистическая блокировка и записи в SQLite. Telegram и реальная
`post_bot.db` не используются.

Что закрепляется (Poster 2.5.3 §3):

1. условия существуют ТОЛЬКО у расширенного шаблона: у простого нет кнопки,
   а прямой callback получает отказ и ничего не меняет;
2. путь автора — ОДИН экран «🧩 Условия расширенного шаблона»: подсказка и
   кнопки кирпичиков расширенного текста, без перечня полей в теле;
3. кирпичик в обоих текстах показывается ОДИН раз; у настроенного условия в
   подписи стоит «✓»;
4. не больше пяти кирпичиков на страницу; при одной странице навигации нет,
   средняя кнопка инертна, по краям нет лишних стрелок;
5. три способа скрытия: вся строка, строка с разделителем, свой фрагмент;
   выбор сохраняется сразу, без предпросмотра;
6. повторяющийся кирпичик — ОДНО условие на все вхождения;
7. удаление условия — только через подтверждение;
8. права: чужой личный шаблон не открывает раздел.
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
    SECTION_EXTENDED_CONTENT,
    STATUS_ACTIVE,
    create_conditional_part,
    list_conditional_parts,
)
from models import PlaceAdmin, utcnow
from place_store import create_place
from template_store import (
    create_personal_template,
    update_template_content,
)
from template_types import (
    BUILTIN_TEMPLATE_CONDITIONAL_RULES,
    TEMPLATE_CONTENT,
    TEMPLATE_EXTENDED_CONTENT,
)
from tests import ui_texts_expect as cat
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

# Заголовок и подсказка — из каталога (владелец правит YAML).
TITLE = cat.text("templates.cond.title")
HINT = cat.text("templates.cond.hint")


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


def _callback(h, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(f"Нет callback {prefix!r}; есть: {h.screen_buttons()}")
    return found[0]


def _labels(h) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _user(session, telegram_user_id: int):
    user = get_or_create_user(
        session, telegram_user_id, username=f"u{telegram_user_id}"
    )
    session.commit()
    return user


def _template_with_system_time_bricks(
    h,
    *,
    name: str = "Время",
    content: str = TIME_TEMPLATE,
    extended_content: str | None = None,
):
    """
    Личный шаблон на системных кирпичиках времени.

    Poster 2.5.3 §3: условия принадлежат только расширенному тексту, поэтому
    по умолчанию расширение совпадает с текстом — раздел доступен.
    """
    with h.session_factory() as session:
        user = _user(session, h.user_id)
        template = create_personal_template(
            session,
            actor_telegram_user_id=user.telegram_user_id,
            name=name,
            content=content,
            extended_content=(
                content if extended_content is None else extended_content
            ),
        )
        session.commit()
        return template.template_id, template.revision


def _parts(h, template_id: int, actor_tg: int | None = None) -> list:
    """Правила шаблона глазами автора (по умолчанию — пользователь харнесса)."""
    with h.session_factory() as session:
        return list(
            list_conditional_parts(
                session,
                h.user_id if actor_tg is None else actor_tg,
                template_id,
                include_stale=True,
            )
        )


# Этап A1, §2: у стандартного шаблона v5 и у любого шаблона, сброшенного к
# встроенному стандарту, автоматически появляются 11 правил (3 простых + 8
# расширенных). Поэтому проверка «правил не создано/не осталось» должна
# смотреть на АВТОРСКИЕ правила, а не на пустой список.
BUILTIN_RULE_KEYS = {
    (rule.section, rule.code, rule.fragment)
    for rule in BUILTIN_TEMPLATE_CONDITIONAL_RULES
}


def _authored_parts(h, template_id: int, actor_tg: int | None = None) -> list:
    """Правила, которых нет среди встроенных правил стандарта."""
    return [
        part
        for part in _parts(h, template_id, actor_tg)
        if (part.section, part.code, part.fragment) not in BUILTIN_RULE_KEYS
    ]


async def _open_card(h, template_id: int) -> None:
    await h.text("/templates")
    await h.callback(f"tp_open:{template_id}")


async def _open_rules(h, template_id: int) -> None:
    await _open_card(h, template_id)
    await h.callback(f"tp_cond:{template_id}")


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
class ConditionalAvailabilityTests(unittest.TestCase):
    """§3: раздел условий есть только у расширенного шаблона."""

    @scenario_test
    async def test_simple_template_has_no_rules_entry(self, h) -> None:
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            template = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="Простой",
                content=TIME_TEMPLATE,
            )
            session.commit()
            template_id = template.template_id
        await _open_card(h, template_id)
        self.assertNotIn(
            f"tp_cond:{template_id}",
            _callbacks(h, "tp_cond"),
            "у простого шаблона нет кнопки условий",
        )
        self.assertIn("Версия", h.screen_text(), "карточка простого шаблона на месте")

    @scenario_test
    async def test_simple_template_callback_is_refused(self, h) -> None:
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            template = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="Простой",
                content=TIME_TEMPLATE,
            )
            session.commit()
            template_id = template.template_id
        await _open_card(h, template_id)
        answers_before = len(h.answer_texts())
        await h.callback(f"tp_cond:{template_id}")
        self.assertGreater(
            len(h.answer_texts()), answers_before, "прямой callback получил отказ"
        )
        self.assertTrue(h.answer_texts()[-1])
        self.assertNotIn(TITLE, h.screen_text())
        self.assertEqual(_parts(h, template_id), [], "правил не создано")

    @scenario_test
    async def test_extended_template_shows_rules_entry(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_card(h, template_id)
        self.assertIn((TITLE, f"tp_cond:{template_id}"), h.screen_buttons())

    @scenario_test
    async def test_content_section_callback_is_refused(self, h) -> None:
        """2.5.3 §3: у простого текста условий нет — отказ без записи."""
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_rules(h, template_id)
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        answers_before = len(h.answer_texts())
        await h.callback(f"tp_condb:{template_id}:c:{end_brick.id}")
        self.assertGreater(
            len(h.answer_texts()), answers_before, "старая кнопка отклонена"
        )
        self.assertEqual(_parts(h, template_id), [], "в БД ничего не записано")
        # Секция расширенного текста того же кирпичика работает.
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        cat.assert_shows(self, h.screen_text(), "templates.cond.mode_title")


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class ConditionalUiRouteTests(unittest.TestCase):
    """§3: один экран → кирпичик → способ скрытия → сохранение."""

    @scenario_test
    async def test_open_shows_single_screen_with_hint(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_rules(h, template_id)
        text = h.screen_text()
        self.assertIn(TITLE, text)
        cat.assert_shows(self, text, "templates.cond.hint")
        # В теле только заголовок и подсказка: перечня полей нет.
        self.assertNotIn("Время начала", text)
        self.assertNotIn("Время окончания", text)
        self.assertEqual(
            [label for label, _data in h.screen_buttons()],
            ["Время начала", "Время окончания", "◀️ Назад"],
        )
        # Одна страница — навигации нет вовсе.
        self.assertEqual(_callbacks(h, "tp_condp:"), [])
        self.assertEqual(_callbacks(h, "noop:"), [])
        self.assertEqual(
            _callbacks(h, f"tp_condback:{template_id}:card"),
            [f"tp_condback:{template_id}:card"],
        )

    @scenario_test
    async def test_bricks_have_no_occurrences_or_fragments(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(
            h,
            content="[время_начала] • [время_окончания] • [время_окончания]",
        )
        await _open_rules(h, template_id)
        text = h.screen_text()
        # §3: в интерфейсе нет номеров вхождений и технических диапазонов.
        self.assertNotIn("вхождение", text.lower())
        self.assertNotIn("⏎", text)
        self.assertNotIn("диапазон", text.lower())
        # Один кирпичик — одна кнопка, сколько бы раз он ни встречался.
        self.assertEqual(len(_callbacks(h, f"tp_condb:{template_id}:e:")), 2)

    @scenario_test
    async def test_separator_mode_saves_immediately(self, h) -> None:
        template_id, revision = _template_with_system_time_bricks(h)
        await _open_rules(h, template_id)
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")

        labels = _labels(h)
        self.assertIn("Скрыть с разделителем", labels)
        self.assertIn("Скрыть всю строку", labels)
        self.assertIn("Скрыть свой фрагмент", labels)
        self.assertIn(f"tp_condf:{template_id}:sep", _callbacks(h, "tp_condf:"))

        answers_before = len(h.answer_texts())
        await h.callback(f"tp_condf:{template_id}:sep")
        self.assertEqual(
            h.answer_texts()[answers_before:],
            ["Условие сохранено."],
            "выбор способа сохраняется сразу, без предпросмотра",
        )
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].fragment, "–[время_окончания]")
        self.assertEqual(parts[0].section, SECTION_EXTENDED_CONTENT)
        self.assertEqual(parts[0].status, STATUS_ACTIVE)
        # Вернулись на единый экран: настроенный кирпичик помечен «✓».
        text = h.screen_text()
        self.assertIn(TITLE, text)
        self.assertIn("Время окончания ✓", _labels(h))
        self.assertIn("Время начала", _labels(h))
        # Условие не создаёт новую версию шаблона.
        with h.session_factory() as session:
            from models import Template

            row = session.get(Template, template_id)
            assert row is not None
            self.assertEqual(row.revision, revision)

    @scenario_test
    async def test_line_mode_hides_whole_line(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(
            h,
            content="🕒 [время_начала]\n💰 Цена: [время_окончания]\nКонтакты",
        )
        await _open_rules(h, template_id)
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:line")

        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].fragment, "💰 Цена: [время_окончания]")

    @scenario_test
    async def test_custom_mode_errors_and_then_saves(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_rules(h, template_id)
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:custom")

        await h.text("Нет такого фрагмента")
        self.assertIn("Свой фрагмент", h.screen_text(), "автор остался на вводе")
        cat.assert_shows(self, h.screen_text(), "templates.cond.custom_not_all")
        self.assertEqual(_parts(h, template_id), [])

        await h.text("–[время_окончания]")
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].fragment, "–[время_окончания]")
        self.assertIn(TITLE, h.screen_text(), "вернулись на единый экран")

    @scenario_test
    async def test_repeated_brick_is_one_rule(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(
            h,
            name="Повтор",
            content="[время_начала] • [время_окончания] • [время_окончания]",
        )
        await _open_rules(h, template_id)
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:sep")

        # Строки БД — по вхождению, кнопка — одна.
        self.assertEqual(len(_parts(h, template_id)), 2, "строки БД — по вхождению")
        labels = _labels(h)
        self.assertEqual(labels.count("Время окончания ✓"), 1, "одно условие")

        # Правка условия заменяет оба вхождения, а не добавляет третье.
        await h.callback(_callback(h, f"tp_condb:{template_id}:e:"))
        await h.callback(f"tp_condf:{template_id}:custom")
        await h.text(" • [время_окончания]")
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 2, "дублей нет")
        self.assertEqual({part.fragment for part in parts}, {" • [время_окончания]"})

    @scenario_test
    async def test_edit_and_delete_from_brick_screen(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_rules(h, template_id)
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:sep")

        # Повторный вход в кирпичик — просмотр/правка с текущим способом.
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        self.assertIn("· выбрано", h.screen_text())
        await h.callback(f"tp_condf:{template_id}:custom")
        await h.text("–[время_окончания]")
        self.assertEqual(len(_parts(h, template_id)), 1, "правка не создаёт дубль")

        # Удаление — только через подтверждение.
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condd:{template_id}:e:{end_brick.id}")
        self.assertIn("Удалить условие?", h.screen_text())
        self.assertEqual(len(_parts(h, template_id)), 1, "без подтверждения не удаляем")
        await h.callback(f"tp_conddy:{template_id}:e:{end_brick.id}")
        self.assertEqual(_parts(h, template_id), [])
        self.assertIn(TITLE, h.screen_text())
        self.assertIn("Время окончания", _labels(h), "условия больше нет — без ✓")

    @scenario_test
    async def test_back_returns_to_card_from_every_step(self, h) -> None:
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_rules(h, template_id)
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:custom")

        await h.callback(f"tp_condback:{template_id}:mode")
        cat.assert_shows(self, h.screen_text(), "templates.cond.mode_title")
        await h.callback(f"tp_condback:{template_id}:bricks")
        self.assertIn(TITLE, h.screen_text())
        # Прежний шаг «список правил» в 2.5.3 — тот же экран кирпичиков.
        await h.callback(f"tp_condback:{template_id}:list")
        self.assertIn(TITLE, h.screen_text())
        await h.callback(f"tp_condback:{template_id}:card")
        self.assertIn("Версия", h.screen_text(), "вернулись в карточку шаблона")
        self.assertEqual(_parts(h, template_id), [], "«Назад» ничего не сохранил")

    @scenario_test
    async def test_pagination_five_per_page_with_inert_indicator(self, h) -> None:
        place_id, _standard = _place_with_admin(h, h.user_id)  # noqa: F841
        # Шесть кирпичиков строятся на личных кирпичиках: по одному на строку.
        from tests.test_stage27b_announcement_ui import _seed_personal_template

        specs: list[tuple[str, str, str, dict[str, object]]] = [
            (f"f{index}", f"Поле {index}", "short_text", {}) for index in range(6)
        ]
        content = "\n".join(f"Строка {index}: [f{index}]" for index in range(6))
        template_id, revision, bricks = _seed_personal_template(
            h,
            name="Много условий",
            specs=specs,
            content=content,
            extended_content=content,
        )
        with h.session_factory() as session:
            for index in range(6):
                result = create_conditional_part(
                    session,
                    actor_telegram_user_id=h.user_id,
                    template_id=template_id,
                    expected_revision=revision,
                    section=SECTION_EXTENDED_CONTENT,
                    brick_id=bricks[f"f{index}"],
                    fragment=f"Строка {index}: [f{index}]",
                    occurrence=1,
                )
                self.assertTrue(result.ok, result.message)
        await _open_rules(h, template_id)

        # Страница 1: пять кирпичиков и строка навигации `1 / 2`.
        labels = _labels(h)
        self.assertIn("Поле 0 ✓", labels)
        self.assertIn("Поле 4 ✓", labels)
        self.assertNotIn("Поле 5 ✓", labels, "шестой кирпичик на второй странице")
        self.assertIn("1 / 2", labels)
        self.assertIn("▶️", labels)
        self.assertNotIn("◀️", labels, "на первой странице левой стрелки нет")
        noop = _callbacks(h, "noop:")
        self.assertEqual(noop, ["noop:1:2"], "индикатор — безопасный no-op")

        # Средняя кнопка ничего не делает и не падает.
        screen_before = h.screen_texts()[-1]
        await h.callback("noop:1:2")
        self.assertEqual(h.screen_texts()[-1], screen_before, "индикатор инертен")
        self.assertEqual(len(_parts(h, template_id)), 6, "данные не изменились")

        # Вторая страница: `2 / 2` и нет правой стрелки.
        await h.callback(_callback(h, "tp_condp:"))
        labels = _labels(h)
        self.assertIn("Поле 5 ✓", labels)
        self.assertNotIn("Поле 0 ✓", labels)
        self.assertIn("2 / 2", labels)
        self.assertNotIn("▶️", labels, "на последней странице правой стрелки нет")
        self.assertIn("◀️", labels)

        # Назад на первую: навигация не теряет страницу.
        await h.callback(_callback(h, "tp_condp:"))
        self.assertIn("1 / 2", _labels(h))

    @scenario_test
    async def test_custom_fragment_keeps_leading_space(self, h) -> None:
        """Ведущий пробел фрагмента значим и сохраняется как введён."""
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            template = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="С адресом",
                content="📍 [место] ([адрес])",
                extended_content="📍 [место] ([адрес])",
            )
            session.commit()
            template_id = template.template_id
        await _open_rules(h, template_id)
        with h.session_factory() as session:
            address = get_system_brick(session, "адрес")
            assert address is not None
        await h.callback(f"tp_condb:{template_id}:e:{address.id}")
        await h.callback(f"tp_condf:{template_id}:custom")
        await h.text(" ([адрес])")
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].fragment, " ([адрес])")

    @scenario_test
    async def test_admin_of_place_edits_standard_template_rules(self, h) -> None:
        """
        Админ места проходит маршрут в стандартном шаблоне (§3: один движок).

        Этап A1, §2: стандарт создаётся сразу с 11 встроенными правилами
        (3 простых + 8 расширенных) и без авторских. Правка текста и новое
        авторское правило не должны их ни дублировать, ни терять.
        """
        place_id, template_id = _place_with_admin(h, h.user_id)
        builtin = _parts(h, template_id)
        self.assertEqual(len(builtin), 11, "11 встроенных правил стандарта")
        self.assertEqual(
            sum(1 for part in builtin if part.section == TEMPLATE_CONTENT),
            3,
            "3 встроенных правила простого варианта",
        )
        self.assertEqual(
            sum(1 for part in builtin if part.section == TEMPLATE_EXTENDED_CONTENT),
            8,
            "8 встроенных правил расширенного варианта",
        )
        self.assertEqual(_authored_parts(h, template_id), [], "авторских правил нет")
        # Стандарт получает расширенную версию: без неё раздела условий нет.
        with h.session_factory() as session:
            from template_store import get_standard_template

            details = get_standard_template(session, h.user_id, place_id)
            result = update_template_content(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=details.revision,
                content=details.current_version.content,
                extended_content=details.current_version.content,
            )
            self.assertTrue(result.ok, result.message)
        await h.callback(f"tp_std:{place_id}")
        self.assertIn("Стандартный шаблон места", h.screen_text())

        await h.callback(f"tp_cond:{template_id}")
        self.assertIn(TITLE, h.screen_text())
        with h.session_factory() as session:
            address = get_system_brick(session, "адрес")
            assert address is not None
        await h.callback(f"tp_condb:{template_id}:e:{address.id}")
        cat.assert_shows(
            self, h.screen_text(), "templates.cond.mode_title", "ложного отказа нет"
        )
        await h.callback(f"tp_condf:{template_id}:custom")
        await h.text("[адрес]")
        authored = _authored_parts(h, template_id)
        self.assertEqual(len(authored), 1, "ровно одно авторское правило")
        self.assertEqual(authored[0].fragment, "[адрес]")
        self.assertEqual(authored[0].section, TEMPLATE_EXTENDED_CONTENT)
        self.assertEqual(authored[0].status, STATUS_ACTIVE)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class ConditionalAnnouncementTests(unittest.TestCase):
    """§42: условие, созданное через интерфейс, работает в готовом анонсе."""

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
        template_id, _revision = _template_with_system_time_bricks(
            h,
            content="🕒 [время_начала]",
            extended_content="🕒 [время_начала]–[время_окончания]",
        )
        await _open_rules(h, template_id)
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
            start_brick = get_system_brick(session, "время_начала")
            assert start_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:sep")
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
            # Доп. поле выбрано пустым: расширенный текст включён, время пусто.
            from announcement_store import add_announcement_extra

            empty = add_announcement_extra(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=saved.draft_id,
                brick_id=end_brick.id,
                value=None,
            )
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=empty.draft_id,
            )
            self.assertEqual(preview.html, "🕒 19:00", "§42-А: пустое время скрыто")

            filled = save_announcement_value(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=empty.draft_id,
                brick_id=end_brick.id,
                value="22:00",
                expected_revision=empty.revision,
            )
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=filled.draft_id,
            )
            self.assertEqual(preview.html, "🕒 19:00–22:00")

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
                extended_content=content,
            )
            session.commit()
            template_id = template.template_id
        await _open_rules(h, template_id)
        await h.callback(_callback(h, f"tp_condb:{template_id}:e:"))
        await h.callback(f"tp_condf:{template_id}:line")
        self.assertEqual(_parts(h, template_id)[0].fragment, "💰 Цена: [цена]")

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
class ConditionalRefreshAndAccessTests(unittest.TestCase):
    """§3: исчез кирпичик — условие удалено; чужое не открыть."""

    @scenario_test
    async def test_removed_brick_deletes_rule(self, h) -> None:
        template_id, revision = _template_with_system_time_bricks(h)
        await _open_rules(h, template_id)
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:sep")
        self.assertEqual(len(_parts(h, template_id)), 1)

        # Правка текста мимо интерфейса условий: кирпичика больше нет.
        with h.session_factory() as session:
            result = update_template_content(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                content="Совсем другой текст",
                extended_content=None,
            )
            self.assertTrue(result.ok, result.message)
        self.assertEqual(
            _parts(h, template_id),
            [],
            "условие исчезнувшего кирпичика удалено в той же транзакции",
        )

        # Карточка без расширения: раздела условий нет и нет плашки.
        await h.callback("tp_hub")
        await h.callback(f"tp_open:{template_id}")
        self.assertNotIn("требуют перенастройки", h.screen_text())
        self.assertNotIn(f"tp_cond:{template_id}", _callbacks(h, "tp_cond:"))

    @scenario_test
    async def test_reset_keeps_rule_that_transfers_to_standard(self, h) -> None:
        """
        Сброс к эталону переносит авторское правило, пока кирпичик есть.

        Этап A1, §2.3 и §3: сброс меняет текст на встроенный стандарт, но не
        удаляет условия. Кирпичик `[время_окончания]` есть в расширенном
        тексте стандарта, поэтому правило `–[время_окончания]` переезжает в
        новую версию активным, а не исчезает и не становится `stale`.
        """
        template_id, _revision = _template_with_system_time_bricks(h)
        await _open_rules(h, template_id)
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        await h.callback(f"tp_condf:{template_id}:sep")
        self.assertEqual(len(_parts(h, template_id)), 1)

        await h.callback(f"tp_condback:{template_id}:card")
        await h.callback(f"tp_reset:{template_id}")
        await h.callback(f"tp_reset_yes:{template_id}")
        self.assertNotIn("требуют перенастройки", h.screen_text())
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1, "правило не потеряно при сбросе")
        rule = parts[0]
        self.assertEqual(rule.fragment, "–[время_окончания]")
        self.assertEqual(rule.section, TEMPLATE_EXTENDED_CONTENT)
        self.assertEqual(rule.status, STATUS_ACTIVE, "кирпичик есть — правило активно")
        self.assertEqual(rule.version, 2, "правило живёт в новой версии")
        # Эталонного правила в базе не появилось: у личного шаблона сброс не
        # добавляет встроенные правила, а фрагмент совпадает с эталонным.
        self.assertEqual(
            [part for part in parts if part.code == "время_окончания"],
            [rule],
            "правило одно: эталонное не дублируется",
        )

    @scenario_test
    async def test_foreign_personal_template_is_refused(self, h) -> None:
        with h.session_factory() as session:
            _user(session, OTHER_TG)
            template = create_personal_template(
                session,
                actor_telegram_user_id=OTHER_TG,
                name="Чужой",
                content=TIME_TEMPLATE,
                extended_content=TIME_TEMPLATE,
            )
            session.commit()
            foreign_id = template.template_id
        await h.text("/templates")
        answers_before = len(h.answer_texts())
        await h.callback(f"tp_cond:{foreign_id}")
        # Ни раздела условий, ни изменений в БД: чужой шаблон неотличим от
        # несуществующего.
        self.assertGreater(len(h.answer_texts()), answers_before)
        self.assertEqual(_parts(h, foreign_id), [])
        self.assertNotIn(TITLE, h.screen_text())
