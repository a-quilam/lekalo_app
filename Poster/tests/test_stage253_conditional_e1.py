# tests/test_stage253_conditional_e1.py
"""
Poster 2.5.3 §3 — приёмка E1: условия только у расширенного шаблона.

Что закрепляется:

1. Экран условий показывает ТОЛЬКО кирпичики расширенного текста; кирпичик,
   встречающийся в обоих текстах, показывается один раз; кирпичик простого
   текста в список не попадает.
2. Тело экрана — заголовок и подсказка, без перечня полей; у настроенного
   условия в подписи кнопки стоит «✓».
3. Исчез кирпичик из расширенного текста — условие удаляется вместе с
   сохранением версии; снимок уже созданного черновика продолжает работать
   по прежним правилам.
4. Прямой callback со старой секцией `content` получает отказ и не пишет
   в БД.
5. Пагинация: не больше пяти кирпичиков на страницу; 0/1/5 — без навигации,
   6/10/11 — `1 / N`; индикатор инертен, по краям лишних стрелок нет;
   после рестарта навигация работает.

Реальная `post_bot.db`, `.env` и сеть Telegram не используются: настоящий
`Application.process_update` поверх временной SQLite (`Harness`).

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage253_conditional_e1 -v
"""

from __future__ import annotations

import unittest
from math import ceil

from announcement_store import (
    add_announcement_extra,
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
)
from template_store import (
    create_personal_template,
    update_template_content,
)
from tests.integration_harness import (
    HARNESS_IMPORT_ERROR,
    scenario_test,
)
from tests.test_stage25_conditional_ui import (
    HINT,
    TITLE,
    _callbacks,
    _labels,
    _parts,
    _user,
)
from tests.test_stage27b_announcement_ui import (
    _seed_personal_template,
    _seed_place,
)

try:
    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_ERROR = str(exc)

SKIP = unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)


def _nav_labels(h) -> tuple[list[str], list[str]]:
    """Подписи строки навигации и её callback-данные."""
    labels = [
        label
        for label, _data in h.screen_buttons()
        if label in {"◀️", "▶️"} or " / " in label
    ]
    data = [
        value or ""
        for _label, value in h.screen_buttons()
        if (value or "").startswith(("tp_condp:", "noop:"))
    ]
    return labels, data


def _slice_template(h, count: int, *, name: str = "Кирпичики"):
    """
    Личный шаблон с `count` кирпичиками в расширенном тексте.

    Текст простой версии тот же; условия принадлежат расширению, поэтому
    экран показывает именно эти кирпичики.
    """
    specs: list[tuple[str, str, str, dict[str, object]]] = [
        (f"f{index}", f"Поле {index}", "short_text", {}) for index in range(count)
    ]
    body = (
        "\n".join(f"Строка {index}: [f{index}]" for index in range(count))
        if count
        else "Просто текст без кирпичиков"
    )
    if count == 0:
        # Хотя бы один кирпичик нужен сеятелю, но в тексте его нет.
        specs = [("spare", "Запасное поле", "short_text", {})]
    return _seed_personal_template(
        h,
        name=name,
        specs=specs,
        content=body,
        extended_content=body,
    )


async def _to_list(h) -> None:
    """Возврат к списку шаблонов: `tp_open` принимается только из списка."""
    await h.callback("tp_hub")
    if "Мои шаблоны" not in h.screen_text():
        await h.text("/templates")


async def _open_screen(h, template_id: int) -> None:
    await _to_list(h)
    await h.callback(f"tp_open:{template_id}")
    await h.callback(f"tp_cond:{template_id}")


@SKIP
class ConditionalEntriesTests(unittest.TestCase):
    """§3: экран перечисляет только кирпичики расширенного текста."""

    @scenario_test
    async def test_only_extended_bricks_and_no_duplicates(self, h) -> None:
        from brick_store import create_personal_brick

        with h.session_factory() as session:
            user = _user(session, h.user_id)
            for code, title in (
                ("простой", "Простой кирпичик"),
                ("общий", "Общий"),
                ("только_расш", "Только расш"),
            ):
                create_personal_brick(
                    session,
                    actor_telegram_user_id=user.telegram_user_id,
                    code=code,
                    title=title,
                    value_type="short_text",
                )
            template = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="Оба текста",
                content="Основной вариант: [простой] и [общий]",
                extended_content="Расширенный: [общий] и [только_расш]",
            )
            session.commit()
            template_id = template.template_id

        await _open_screen(h, template_id)
        labels = _labels(h)
        self.assertIn("Общий", labels)
        self.assertIn("Только расш", labels)
        self.assertNotIn("Простой кирпичик", labels, "кирпичик простого текста")
        # Дубля нет: `[общий]` встречается в обоих текстах, но показан раз.
        self.assertEqual(labels.count("Общий"), 1)
        # В теле нет перечня полей — только заголовок и подсказка.
        text = h.screen_text()
        self.assertIn(TITLE, text)
        self.assertIn(HINT, text)
        self.assertNotIn("Общий", text)
        self.assertNotIn("Только расш", text)

    @scenario_test
    async def test_configured_brick_is_marked(self, h) -> None:
        template_id, revision, bricks = _slice_template(h, 2)
        with h.session_factory() as session:
            created = create_conditional_part(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                section=SECTION_EXTENDED_CONTENT,
                brick_id=bricks["f0"],
                fragment="Строка 0: [f0]",
                occurrence=1,
            )
            self.assertTrue(created.ok, created.message)
        await _open_screen(h, template_id)
        labels = _labels(h)
        self.assertIn("Поле 0 ✓", labels, "настроенное условие помечено")
        self.assertIn("Поле 1", labels)
        self.assertNotIn("Поле 1 ✓", labels)

    @scenario_test
    async def test_empty_extension_shows_no_bricks_notice(self, h) -> None:
        template_id, _revision, _bricks = _slice_template(h, 0)
        await _open_screen(h, template_id)
        self.assertIn("нет кирпичиков", h.screen_text())
        self.assertEqual(_callbacks(h, "tp_condb:"), [])
        self.assertEqual(_nav_labels(h), ([], []), "навигации нет")


@SKIP
class ConditionalRemovalTests(unittest.TestCase):
    """§3: исчез кирпичик — условие удалено, снимок черновика живёт."""

    @scenario_test
    async def test_removed_brick_deletes_rule_and_draft_snapshot_survives(
        self, h
    ) -> None:
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            main = self._brick(session, h.user_id, "main", "Основное")
            cost = self._brick(session, h.user_id, "cost", "Цена")
            template = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="С ценой",
                content="**[main]**",
                extended_content="**[main] – [cost]**",
            )
            session.commit()
            template_id = template.template_id
            revision = template.revision
            main_id, cost_id = main, cost
        with h.session_factory() as session:
            created = create_conditional_part(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                section=SECTION_EXTENDED_CONTENT,
                brick_id=cost_id,
                fragment=" – [cost]",
                occurrence=1,
            )
            self.assertTrue(created.ok, created.message)

        # Черновик создан ДО правки шаблона: его снимок должен пережить
        # удаление условия в живом шаблоне.
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
            chosen = choose_announcement_template(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=confirmed.draft_id,
                template_id=template_id,
                expected_revision=confirmed.revision,
            )
            chosen = save_announcement_value(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=chosen.draft_id,
                brick_id=main_id,
                value="Концерт",
                expected_revision=chosen.revision,
            )
            chosen = add_announcement_extra(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=chosen.draft_id,
                brick_id=cost_id,
                value="",
            )
            before = build_announcement_preview(
                session, actor_telegram_user_id=h.user_id, draft_id=chosen.draft_id
            ).html
        self.assertEqual(before, "<b>Концерт</b>", "правило применилось к черновику")

        # Правка текста без кирпичика цены должна убрать и условие.
        with h.session_factory() as session:
            result = update_template_content(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                content="**[main]**",
                extended_content="**[main]**",
            )
            self.assertTrue(result.ok, result.message)
        self.assertEqual(
            _parts(h, template_id),
            [],
            "условие исчезнувшего кирпичика удалено вместе с сохранением",
        )

        with h.session_factory() as session:
            after = build_announcement_preview(
                session, actor_telegram_user_id=h.user_id, draft_id=chosen.draft_id
            ).html
        self.assertEqual(
            after, before, "снимок прежнего черновика работает по прежним правилам"
        )

    @staticmethod
    def _brick(session, telegram_user_id: int, code: str, title: str) -> int:
        from brick_store import create_personal_brick

        return create_personal_brick(
            session,
            actor_telegram_user_id=telegram_user_id,
            code=code,
            title=title,
            value_type="short_text",
        ).id


@SKIP
class ConditionalRefusalTests(unittest.TestCase):
    """§3: у простого текста условий нет — отказ без записи в БД."""

    @scenario_test
    async def test_content_section_callback_is_refused(self, h) -> None:
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            template = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="Время",
                content="[время_начала]–[время_окончания]",
                extended_content="[время_начала]–[время_окончания]",
            )
            session.commit()
            template_id = template.template_id
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
            end_id = end_brick.id

        await _open_screen(h, template_id)
        answers_before = len(h.answer_texts())
        await h.callback(f"tp_condb:{template_id}:c:{end_id}")
        self.assertGreater(
            len(h.answer_texts()), answers_before, "старая секция отклонена"
        )
        self.assertTrue(h.answer_texts()[-1])
        self.assertEqual(_parts(h, template_id), [], "в БД ничего не записано")
        self.assertIn(TITLE, h.screen_text(), "автор остался на экране условий")

        # Та же кнопка с секцией расширенного текста работает.
        await h.callback(f"tp_condb:{template_id}:e:{end_id}")
        self.assertIn("Как скрывать текст", h.screen_text())
        await h.callback(f"tp_condf:{template_id}:sep")
        parts = _parts(h, template_id)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].status, STATUS_ACTIVE)
        self.assertEqual(parts[0].section, SECTION_EXTENDED_CONTENT)

    @scenario_test
    async def test_legacy_tp_condl_callback_is_refused(self, h) -> None:
        """§3: старая кнопка `tp_condl:` получает отказ, а не экран."""
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            template = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="Время",
                content="[время_начала]–[время_окончания]",
                extended_content="[время_начала]–[время_окончания]",
            )
            session.commit()
            template_id = template.template_id

        await _to_list(h)
        await h.callback(f"tp_open:{template_id}")
        card_text = h.screen_text()
        screens_before = len(h.screen_texts())
        answers_before = len(h.answer_texts())
        awaits_before = len(_parts(h, template_id))

        await h.callback(f"tp_condl:{template_id}")

        self.assertEqual(
            len(h.screen_texts()), screens_before, "легаси-кнопка не рисует экран"
        )
        self.assertEqual(h.screen_text(), card_text, "автор остался в карточке")
        self.assertNotIn(TITLE, h.screen_text(), "экран условий не открылся")
        self.assertGreater(
            len(h.answer_texts()), answers_before, "старая кнопка получила ответ"
        )
        refusal = h.answer_texts()[-1]
        self.assertIn("больше не работает", refusal, refusal)
        self.assertNotIn("Что-то пошло не так", refusal, "не внутренняя ошибка")
        self.assertEqual(
            len(_parts(h, template_id)), awaits_before, "в БД ничего не записано"
        )

    @scenario_test
    async def test_content_rule_in_db_does_not_reach_screen(self, h) -> None:
        """Правило секции `content` не показывается и не применяется (§3)."""
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            template = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="Время",
                content="[время_начала]–[время_окончания]",
                extended_content="[время_начала]–[время_окончания]",
            )
            session.commit()
            template_id = template.template_id
            revision = template.revision
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
            end_id = end_brick.id
        with h.session_factory() as session:
            created = create_conditional_part(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                section="content",
                brick_id=end_id,
                fragment="–[время_окончания]",
            )
            self.assertTrue(created.ok, created.message)

        await _open_screen(h, template_id)
        labels = _labels(h)
        self.assertIn("Время окончания", labels)
        self.assertNotIn("Время окончания ✓", labels, "правило простого текста скрыто")


@SKIP
class ConditionalPaginationTests(unittest.TestCase):
    """§3: единый экран с пагинацией по пять кирпичиков."""

    @scenario_test
    async def test_pagination_by_brick_count(self, h) -> None:
        for count in (0, 1, 5, 6, 10, 11):
            with self.subTest(count=count):
                template_id, _revision, _bricks = _slice_template(
                    h, count, name=f"Срез {count}"
                )
                await _open_screen(h, template_id)
                labels = _labels(h)
                nav_labels, nav_data = _nav_labels(h)
                pages = ceil(count / 5)
                if pages <= 1:
                    self.assertEqual(nav_labels, [], "до пяти кирпичиков навигации нет")
                    self.assertEqual(nav_data, [])
                else:
                    self.assertIn(f"1 / {pages}", nav_labels)
                    self.assertNotIn("◀️", nav_labels, "на первой странице")
                    self.assertIn("▶️", nav_labels)
                    self.assertEqual(nav_data[0], f"noop:1:{pages}")
                self.assertEqual(
                    len([label for label in labels if label.startswith("Поле ")]),
                    min(count, 5),
                    "не больше пяти кирпичиков на страницу",
                )
                # Возврат в карточку всегда доступен.
                self.assertIn("◀️ Назад", labels)
                await h.callback(f"tp_condback:{template_id}:card")

    @scenario_test
    async def test_indicator_is_inert_and_edges_have_no_arrows(self, h) -> None:
        template_id, _revision, _bricks = _slice_template(h, 11, name="Три страницы")
        await _open_screen(h, template_id)

        labels, data = _nav_labels(h)
        self.assertIn("1 / 3", labels)
        self.assertEqual(data[0], "noop:1:3")
        before = h.screen_texts()[-1]
        calls_before = len(h.calls)
        await h.callback("noop:1:3")
        self.assertEqual(
            h.calls[calls_before:][0]["type"], "callback_answer", "ответ без экрана"
        )
        self.assertEqual(h.screen_texts()[-1], before, "индикатор инертен")

        # Первая → вторая: обе стрелки, индикатор 2 / 3.
        await h.callback(_callbacks(h, "tp_condp:")[0])
        labels, _data = _nav_labels(h)
        self.assertIn("2 / 3", labels)
        self.assertIn("◀️", labels)
        self.assertIn("▶️", labels)

        # Вторая → третья: правой стрелки нет.
        callbacks = _callbacks(h, "tp_condp:")
        await h.callback(callbacks[-1])
        labels, _data = _nav_labels(h)
        self.assertIn("3 / 3", labels)
        self.assertNotIn("▶️", labels, "на последней странице")
        self.assertIn("◀️", labels)

        # Устаревший callback (маршрут закрыт рестартом) получает отказ.
        await h.restart()
        await h.text("/start")
        answers_before = len(h.answer_texts())
        await h.callback(f"tp_condp:{template_id}:0")
        self.assertGreater(
            len(h.answer_texts()), answers_before, "устаревший callback отклонён"
        )
        self.assertNotIn(TITLE, h.screen_text(), "экран условий не открылся")

    @scenario_test
    async def test_pagination_survives_restart(self, h) -> None:
        template_id, _revision, _bricks = _slice_template(h, 6, name="Рестарт")
        await _open_screen(h, template_id)
        await h.callback(_callbacks(h, "tp_condp:")[0])
        labels, _data = _nav_labels(h)
        self.assertIn("2 / 2", labels)

        await h.restart()
        self.assertEqual(h.user_data(), {}, "состояние диалога потеряно, как в жизни")
        await h.text("/start")
        await _open_screen(h, template_id)
        nav, _data = _nav_labels(h)
        self.assertIn("1 / 2", nav, "после рестарта страницы те же")
        self.assertIn("Поле 0", _labels(h))
        await h.callback(_callbacks(h, "tp_condp:")[0])
        nav, _data = _nav_labels(h)
        self.assertIn("2 / 2", nav, "навигация работает после рестарта")
        self.assertIn("Поле 5", _labels(h))
        self.assertEqual(_parts(h, template_id), [])
