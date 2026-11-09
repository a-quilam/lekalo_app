# tests/test_stage26_e_acceptance.py
"""
«Лекало» 2.6, этап E: НЕЗАВИСИМАЯ приёмка связок между этапами (§19).

Модуль не повторяет приёмки этапов A–D и не проверяет внутренние функции:
сценарии идут через настоящее приложение (`bot.build_application`), FakeBot
и временную SQLite. Рабочая `post_bot.db`, `.env` и сеть Telegram не
используются. Ожидания формулировок берутся из действующего каталога
`config/ui_texts.yaml` через `tests/ui_texts_expect.py`.

Что закрепляется (сквозные швы, которые не видны внутри одного этапа):

1. §19.1/§19.2: стандартный шаблон с включённым расширенным вариантом
   спрашивает `[куратор]` и `[подвал]` в первоначальном мастере, значения
   сохраняются в конкретном черновике, а «Дополнительные поля» этот же
   подвал показывают и правят (§10.2). Переключение флага применяется к
   НОВОМУ черновику: начатый анонс продолжает прежний снимок шаблона.
2. §19.3: справочник площадки, созданный в административном интерфейсе,
   сразу доступен конструктору; архивная запись из выбора исчезает и
   возвращается после восстановления одним нажатием.
3. §19.4: личные справочники принадлежат владельцу — свой шаблон видит
   только свои записи, чужая запись не раскрывается, администратор
   площадки в личном сценарии не упоминается.
4. §19.8: навигация по Rich-страницам («Помощь» → «О проекте» → «К
   помощи» → «Назад») возвращает на ТОТ ЖЕ шаг конструктора, а его
   callback-кнопки остаются рабочими (`help_back` после «О проекте», а не
   повтор уже закреплённого возврата из самой справки).
5. §19.8/§19.9: `/report`, открытый посреди конструктора, по «Отмене»
   возвращает тот же вопрос с рабочими кнопками.

Сводка по §21 (критерии завершения). Закрыты этим модулем вместе с
приёмками этапов: 1–20 (шаблон, переключатель, справочники, конструктор,
подсказки), 24 (навигация и восстановление состояния), 25 (автотесты и CI
запускаются на границе этапа). НЕ проверены здесь по объективным причинам:
21.23 (оригинальные PNG отсутствуют — слайд-шоу не отправляется, раздел
собирается без медиа), живая проверка Rich-медиа и disabled-кнопок в
настоящем Telegram (доступна только сериализация Bot API), 21.26–21.27
(тестовое развёртывание и публичный выпуск — вне зоны тестов).

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage26_e_acceptance -v
"""

from __future__ import annotations

import unittest
from typing import Any

from tests import ui_texts_expect as cat

try:

    from announcement_store import get_announcement_draft
    from brick_types import BrickValueStatus
    from catalogs_store import (
        create_curator,
        create_footer,
        list_curators,
        selectable_footers,
    )
    from models import PlaceAdmin, utcnow
    from place_store import create_place
    from template_store import (
        create_personal_template,
        get_standard_template,
        set_standard_extended_active,
        update_template_content,
    )
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        assert_delivered,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import inline_buttons

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)

ADMIN_TG = 26_090_001
AUTHOR_TG = 26_090_002
OTHER_TG = 26_090_003

CURATOR_CODE = "куратор"
FOOTER_CODE = "подвал"

NO_CURATORS = cat.text("wizard.catalog.no_curators_place")
NO_CURATORS_HINT = cat.text("wizard.catalog.no_curators_place_hint")
MENU_TEXT = cat.text("menu.main")


# --- Помощники ---------------------------------------------------------------


def _raw_rows(h: Harness) -> list[list]:
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return list(markup.inline_keyboard)


def _callbacks(h: Harness, prefix: str) -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


def _callback(h: Harness, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(
            f"нет callback {prefix!r}: {h.screen_buttons()}; экран: "
            f"{h.screen_text()[:300]!r}"
        )
    return found[0]


def _labels(h: Harness) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _ui(h: Harness, *, user_id: int = AUTHOR_TG) -> dict:
    return h.user_data(user_id=user_id).get("announcement_ui") or {}


def _draft_id(h: Harness, *, user_id: int = AUTHOR_TG) -> int:
    return int(_ui(h, user_id=user_id)["draft_id"])


def _details(h: Harness, draft_id: int, *, user_id: int = AUTHOR_TG):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=user_id,
            draft_id=draft_id,
        )


def _brick_value(details, code: str):
    snapshot = details.template_snapshot
    assert snapshot is not None, "у черновика нет снимка шаблона"
    brick_id = next(
        binding.brick_id for binding in snapshot.bindings if binding.code == code
    )
    return details.value_for(brick_id)


def _value_text(details, code: str) -> str | None:
    value = _brick_value(details, code)
    if value is None or value.status is not BrickValueStatus.VALID:
        return None
    payload = value.payload or {}
    raw = payload.get("text")
    return raw if isinstance(raw, str) else None


def _seed_place(h: Harness, name: str = "Дом культуры") -> int:
    with h.session_factory() as session:
        admin = get_or_create_user(session, ADMIN_TG, username="admin")
        get_or_create_user(session, AUTHOR_TG, username="author")
        get_or_create_user(session, OTHER_TG, username="other")
        place = create_place(
            session,
            name,
            city="Москва",
            created_by_user_id=admin.id,
            address="ул. Ленина, 1",
            url="https://example.com/place",
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
        return int(place.id)


def _set_standard(
    h: Harness,
    place_id: int,
    extended_body: str | None,
    *,
    active: bool = True,
) -> int:
    """Стандарт площадки: простой текст `**[название]**` + расширенный."""
    with h.session_factory() as session:
        standard = get_standard_template(session, ADMIN_TG, place_id)
        extended = (
            None if extended_body is None else f"**[название]**\n\n{extended_body}"
        )
        result = update_template_content(
            session,
            actor_telegram_user_id=ADMIN_TG,
            template_id=standard.template_id,
            expected_revision=standard.revision,
            content="**[название]**",
            extended_content=extended,
        )
        assert result.ok, result.message
        assert result.template is not None
        set_standard_extended_active(
            session,
            actor_telegram_user_id=ADMIN_TG,
            template_id=standard.template_id,
            active=active,
            expected_revision=result.template.revision,
        )
        session.commit()
        return int(standard.template_id)


def _seed_curators(h: Harness, place_id: int | None, values) -> None:
    with h.session_factory() as session:
        for value in values:
            create_curator(
                session,
                actor_telegram_user_id=ADMIN_TG if place_id else AUTHOR_TG,
                place_id=place_id,
                value=value,
            )
        session.commit()


def _seed_footers(h: Harness, place_id: int | None, entries) -> None:
    with h.session_factory() as session:
        for name, text in entries:
            create_footer(
                session,
                actor_telegram_user_id=ADMIN_TG if place_id else AUTHOR_TG,
                place_id=place_id,
                name=name,
                text=text,
            )
        session.commit()


async def _open_place_questions(h: Harness, place_id: int) -> None:
    await h.text("/start", user_id=AUTHOR_TG)
    await h.callback("pick_place", user_id=AUTHOR_TG)
    await h.callback(f"pl_pick:{place_id}", user_id=AUTHOR_TG)


async def _answer_name(h: Harness, value: str = "Летний концерт") -> None:
    await h.text(value, user_id=AUTHOR_TG)


def _assert_disabled_row(
    case: unittest.TestCase, h: Harness, first: str, second: str
) -> None:
    """§11: две кнопки в одной строке, обе настоящие неактивные."""
    found = None
    for row in _raw_rows(h):
        if [button.text for button in row] == [first, second]:
            found = row
            break
    case.assertIsNotNone(found, f"нет ряда [{first}] [{second}]: {_labels(h)}")
    assert found is not None
    case.assertEqual(len(found), 2, "обе подсказки должны быть в одной строке")
    for button in found:
        case.assertIsNone(button.callback_data, "у неактивной кнопки нет callback")
        case.assertTrue(
            inline_buttons.is_disabled(button), "кнопка должна быть disabled"
        )


def _rich_blocks(record: dict) -> list[dict[str, Any]]:
    payload = record["rich_message"]
    assert isinstance(payload, dict)
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    return blocks


def _section_titles(record: dict) -> list[str]:
    return [
        block["summary"] for block in _rich_blocks(record) if block["type"] == "details"
    ]


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class WizardCatalogSeamTests(unittest.TestCase):
    """§19.1/§19.2: расширенный стандарт, справочники и переключатель."""

    @scenario_test
    async def test_extended_standard_asks_catalogs_and_saves_per_draft(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]\n\n[подвал]\n\n[описание]")
        _seed_curators(h, place_id, ["@c1", "@c2"])
        _seed_footers(h, place_id, [("F1", "Текст 1"), ("F2", "Текст 2")])

        await _open_place_questions(h, place_id)
        await _answer_name(h)

        # Куратор — вопрос мастера: выбор из двух и ручной ввод.
        self.assertEqual(len(_callbacks(h, "aw_curpick:")), 2)
        self.assertEqual(len(_callbacks(h, "aw_qskip:")), 1)
        await h.callback(_callback(h, "aw_curpick:"), user_id=AUTHOR_TG)

        # Подвал — следующий вопрос мастера, тоже выбор.
        self.assertEqual(len(_callbacks(h, "aw_footpick:")), 2)
        await h.callback(_callback(h, "aw_footpick:"), user_id=AUTHOR_TG)
        self.assertTrue(_callbacks(h, "aw_photo_none:"), "мастер не дошёл до фото")

        details = _details(h, _draft_id(h))
        self.assertEqual(_value_text(details, CURATOR_CODE), "@c1")
        self.assertEqual(_value_text(details, FOOTER_CODE), "Текст 1")

        # Тот же черновик: выбранные справочники видны и правятся в «Доп.
        # полях» (§10.2 — второй штатный сценарий подвала).
        ui = _ui(h)
        await h.callback(
            f"aw_extras:{ui['draft_id']}:{ui['revision']}", user_id=AUTHOR_TG
        )
        self.assertIn("@c1", h.screen_text())
        self.assertIn("Текст 1", h.screen_text())
        self.assertTrue(
            _callbacks(h, "aw_footch:"),
            f"в доп. полях нет редактора подвала: {h.screen_buttons()}",
        )

        # Значение принадлежит конкретному анонсу, а не справочнику.
        await h.callback(_callback(h, "aw_exback:"), user_id=AUTHOR_TG)
        await _open_place_questions(h, place_id)
        await _answer_name(h, "Второй концерт")
        second = _details(h, _draft_id(h))
        self.assertIsNone(
            _value_text(second, CURATOR_CODE), "справочник протёк во второй черновик"
        )

    @scenario_test
    async def test_flag_switch_affects_new_draft_only(self, h) -> None:
        place_id = _seed_place(h)
        template_id = _set_standard(h, place_id, "Куратор: [куратор]", active=False)

        # Флаг выключен: вопрос один — название, справочники не спрашиваются.
        await _open_place_questions(h, place_id)
        self.assertRegex(h.screen_text(), cat.question_re(1, 1))
        await _answer_name(h)
        self.assertTrue(_callbacks(h, "aw_photo_none:"))
        self.assertFalse(_callbacks(h, "aw_curpick:"))

        # Включаем расширенный вариант посреди начатого черновика.
        with h.session_factory() as session:
            standard = get_standard_template(session, ADMIN_TG, place_id)
            set_standard_extended_active(
                session,
                actor_telegram_user_id=ADMIN_TG,
                template_id=template_id,
                active=True,
                expected_revision=standard.revision,
            )
            session.commit()

        # Начатый анонс продолжает прежний снимок: тот же шаг фото.
        self.assertTrue(
            _callbacks(h, "aw_photo_none:"),
            f"снимок начатого черновика изменился: {h.screen_buttons()}",
        )

        # Новый черновик берёт актуальный флаг: два вопроса, второй — куратор.
        # `[подвал]` в шаблоне не упомянут, поэтому его в вопросах нет.
        _seed_curators(h, place_id, ["@fresh"])
        await _open_place_questions(h, place_id)
        self.assertRegex(h.screen_text(), cat.question_re(1, 2))
        await _answer_name(h, "Новый концерт")
        self.assertRegex(h.screen_text(), cat.question_re(2, 2))
        self.assertEqual(len(_callbacks(h, "aw_curpick:")), 1)

        # После выбора — сразу фото: в шаблоне нет `[подвал]`.
        await h.callback(_callback(h, "aw_curpick:"), user_id=AUTHOR_TG)
        self.assertTrue(_callbacks(h, "aw_photo_none:"))
        self.assertEqual(_value_text(_details(h, _draft_id(h)), CURATOR_CODE), "@fresh")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceCatalogToWizardTests(unittest.TestCase):
    """§19.3: справочник площадки → архив → восстановление → конструктор."""

    @scenario_test
    async def test_archive_hides_curator_and_restore_returns_it(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]")

        # 1. Администратор добавляет куратора без подтверждений.
        await h.text("/start", user_id=ADMIN_TG)
        await h.callback("manage", user_id=ADMIN_TG)
        await h.callback("manage_places", user_id=ADMIN_TG)
        await h.callback(f"plm_open:{place_id}", user_id=ADMIN_TG)
        self.assertIn(
            "📝 Кураторы", _labels(h), f"нет входа в справочник: {_labels(h)}"
        )
        await h.callback(f"cat_open:curator:p{place_id}", user_id=ADMIN_TG)
        await h.callback(f"cat_add:curator:p{place_id}", user_id=ADMIN_TG)
        await h.text("@live", user_id=ADMIN_TG)
        self.assertIn("@live", _labels(h), "новая запись не появилась в списке")
        archive_id = _callback(h, f"cat_del:curator:p{place_id}:").rsplit(":", 1)[-1]

        # 2. Конструктор автора видит запись.
        await _open_place_questions(h, place_id)
        await _answer_name(h)
        self.assertEqual(len(_callbacks(h, "aw_curpick:")), 1)
        self.assertIn("@live", _labels(h))

        # 3. Архивирование с подтверждением: из выбора исчезла.
        await h.text("/start", user_id=ADMIN_TG)
        await h.callback("manage", user_id=ADMIN_TG)
        await h.callback("manage_places", user_id=ADMIN_TG)
        await h.callback(f"plm_open:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"cat_open:curator:p{place_id}", user_id=ADMIN_TG)
        await h.callback(f"cat_del:curator:p{place_id}:{archive_id}", user_id=ADMIN_TG)
        self.assertIn(
            cat.text("catalogs.curators.archive_confirm", value="@live"),
            h.screen_text(),
            "нет подтверждения архивирования",
        )
        await h.callback(
            f"cat_delyes:curator:p{place_id}:{archive_id}", user_id=ADMIN_TG
        )
        self.assertNotIn("@live", _labels(h))
        with h.session_factory() as session:
            active = list_curators(
                session, actor_telegram_user_id=ADMIN_TG, place_id=place_id
            )
        self.assertEqual([entry.value_plain for entry in active.items], [])

        await _open_place_questions(h, place_id)
        await _answer_name(h, "Без куратора")
        _assert_disabled_row(self, h, NO_CURATORS, NO_CURATORS_HINT)
        self.assertFalse(_callbacks(h, "aw_curpick:"), "архивный попал в выбор")
        # Пустой список не блокирует создание: ручной ввод доступен.
        await h.text("@manual", user_id=AUTHOR_TG)
        self.assertTrue(_callbacks(h, "aw_photo_none:"))

        # 4. Восстановление одним нажатием из архива.
        await h.text("/start", user_id=ADMIN_TG)
        await h.callback("manage", user_id=ADMIN_TG)
        await h.callback("manage_places", user_id=ADMIN_TG)
        await h.callback(f"plm_open:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"cat_open:curator:p{place_id}", user_id=ADMIN_TG)
        await h.callback(f"cat_arch:curator:p{place_id}", user_id=ADMIN_TG)
        self.assertIn(cat.text("catalogs.curators.archive_title"), h.screen_text())
        await h.callback(f"cat_rest:curator:p{place_id}:{archive_id}", user_id=ADMIN_TG)
        with h.session_factory() as session:
            active = list_curators(
                session, actor_telegram_user_id=ADMIN_TG, place_id=place_id
            )
        self.assertEqual([entry.value_plain for entry in active.items], ["@live"])

        await _open_place_questions(h, place_id)
        await _answer_name(h, "Снова с куратором")
        self.assertEqual(len(_callbacks(h, "aw_curpick:")), 1)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PersonalCatalogIsolationTests(unittest.TestCase):
    """§19.4: личные справочники в конструкторе и их изоляция."""

    def _template(self, h: Harness, tg: int, name: str, content: str) -> int:
        with h.session_factory() as session:
            get_or_create_user(session, tg, username=f"u{tg}")
            template = create_personal_template(
                session,
                actor_telegram_user_id=tg,
                name=name,
                content=content,
            )
            session.commit()
            return int(template.template_id)

    async def _open_personal(self, h: Harness, tg: int, template_id: int) -> None:
        await h.text("/start", user_id=tg)
        await h.callback("pick_place", user_id=tg)
        await h.callback("pick_template", user_id=tg)
        await h.callback(f"tp_pick:{template_id}", user_id=tg)

    @scenario_test
    async def test_own_template_uses_own_catalogs_only(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, None, active=False)
        own = self._template(
            h, AUTHOR_TG, "Мой", "**[название]**\n\n[куратор]\n\n[подвал]"
        )
        foreign = self._template(h, OTHER_TG, "Чужой", "**[название]**\n\n[куратор]")
        _seed_curators(h, None, ["@mine"])
        _seed_footers(h, None, [("Мой подвал", "Мой текст")])
        with h.session_factory() as session:
            create_curator(
                session,
                actor_telegram_user_id=OTHER_TG,
                place_id=None,
                value="@not-mine",
            )
            session.commit()

        # Экран «Создать по личному шаблону»: входы в СВОИ справочники,
        # администратор площадки не упоминается.
        await h.text("/start", user_id=AUTHOR_TG)
        await h.callback("pick_place", user_id=AUTHOR_TG)
        await h.callback("pick_template", user_id=AUTHOR_TG)
        self.assertIn("📝 Кураторы", _labels(h))
        self.assertIn("📝 Подвалы", _labels(h))
        self.assertFalse(
            [data for data in _callbacks(h, "") if data.startswith("adm_")],
            f"личный сценарий зовёт администратора: {h.screen_buttons()}",
        )

        # Конструктор: только свой куратор, чужой не виден.
        await h.callback(f"tp_pick:{own}", user_id=AUTHOR_TG)
        await h.text("Свой концерт", user_id=AUTHOR_TG)
        self.assertEqual(len(_callbacks(h, "aw_curpick:")), 1)
        labels = _labels(h)
        self.assertIn("@mine", labels)
        self.assertNotIn("@not-mine", labels)

        await h.callback(_callback(h, "aw_curpick:"), user_id=AUTHOR_TG)
        details = _details(h, _draft_id(h), user_id=AUTHOR_TG)
        self.assertEqual(_value_text(details, CURATOR_CODE), "@mine")

        # Единственный свой подвал подставлен автоматически (§10.1).
        self.assertEqual(_value_text(details, FOOTER_CODE), "Мой текст")

        # Чужой пользователь видит только свои записи в своём шаблоне.
        await h.text("/start", user_id=OTHER_TG)
        await h.callback("pick_place", user_id=OTHER_TG)
        await h.callback("pick_template", user_id=OTHER_TG)

        # Прямой callback в чужую карточку — отказ, запись не раскрывается.
        # Проверяем на экране выбора шаблона: внутри конструктора callback
        # `cat_*` перехватывает разговор мастера и отвечает «Кнопка устарела».
        await h.callback("cat_open:curator:me", user_id=OTHER_TG)
        await h.callback("cat_card:curator:me:1", user_id=OTHER_TG)
        self.assertEqual(h.answer_texts()[-1], cat.text("catalogs.denied"))
        self.assertNotIn("@mine", h.screen_text())
        await h.callback("cat_home:curator:me", user_id=OTHER_TG)

        await h.callback(f"tp_pick:{foreign}", user_id=OTHER_TG)
        await h.text("Чужой концерт", user_id=OTHER_TG)
        other_labels = _labels(h)
        self.assertIn("@not-mine", other_labels)
        self.assertNotIn("@mine", other_labels)

    @scenario_test
    async def test_personal_footer_choice_stays_inside_owner(self, h) -> None:
        place_id = _seed_place(h)
        _set_standard(h, place_id, None, active=False)
        own = self._template(h, AUTHOR_TG, "Мой", "**[название]**\n\n[подвал]")
        _seed_footers(h, None, [("F1", "Текст 1"), ("F2", "Текст 2")])
        with h.session_factory() as session:
            create_footer(
                session,
                actor_telegram_user_id=OTHER_TG,
                place_id=None,
                name="Чужой",
                text="Чужой текст",
            )
            session.commit()

        await self._open_personal(h, AUTHOR_TG, own)
        await h.text("Концерт", user_id=AUTHOR_TG)
        self.assertEqual(len(_callbacks(h, "aw_footpick:")), 2)
        labels = _labels(h)
        self.assertIn("F1", labels)
        self.assertIn("F2", labels)
        self.assertNotIn("Чужой", labels)

        await h.callback(_callback(h, "aw_footpick:"), user_id=AUTHOR_TG)
        details = _details(h, _draft_id(h), user_id=AUTHOR_TG)
        self.assertEqual(_value_text(details, FOOTER_CODE), "Текст 1")
        with h.session_factory() as session:
            owned = selectable_footers(
                session, actor_telegram_user_id=AUTHOR_TG, place_id=None
            )
        self.assertEqual([entry.name for entry in owned], ["F1", "F2"])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RichNavigationAndReportTests(unittest.TestCase):
    """§19.8: Rich-страницы и `/report` не теряют шаг конструктора."""

    async def _open_question(self, h: Harness) -> int:
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]")
        _seed_curators(h, place_id, ["@c1"])
        await _open_place_questions(h, place_id)
        await _answer_name(h)
        self.assertRegex(h.screen_text(), cat.question_re(2, 2))
        return place_id

    @scenario_test
    async def test_help_about_help_back_returns_to_same_question(self, h) -> None:
        await self._open_question(h)
        question = h.screen_text()
        # На шаге куратора разговор не держит state (per_message=False +
        # allow_reentry), но кнопки конструктора живы. Поэтому сверяем
        # состояние ДО и ПОСЛЕ навигации, а не его наличие.
        state = h.dialog_state(name="announcement_wizard")

        await h.text("/help", user_id=AUTHOR_TG)
        self.assertEqual(len(h.bot.rich_records), 1)
        help_record = h.bot.rich_records[0]
        self.assertEqual(len(_section_titles(help_record)), 9)
        self.assertIn(
            "Помощь",
            [
                block.get("text")
                for block in _rich_blocks(help_record)
                if block["type"] == "heading"
            ],
        )

        # «О проекте» — второй Rich Message; справка вытесняется.
        await h.callback("help_about", user_id=AUTHOR_TG)
        self.assertEqual(len(h.bot.rich_records), 2)
        about_record = h.bot.rich_records[1]
        self.assertEqual(len(_section_titles(about_record)), 6)
        self.assertIn(help_record["message_id"], h.deleted_ids())

        # «◀️ К помощи» и затем «◀️ Назад» — на ТОТ ЖЕ шаг.
        await h.callback("about_help", user_id=AUTHOR_TG)
        self.assertEqual(len(h.bot.rich_records), 3)
        self.assertEqual(len(_section_titles(h.bot.rich_records[2])), 9)
        await h.callback("help_back", user_id=AUTHOR_TG)

        self.assertEqual(h.screen_text(), question, "шаг конструктора не восстановлен")
        self.assertEqual(
            h.dialog_state(name="announcement_wizard"),
            state,
            "состояние диалога не восстановлено",
        )
        # Возврат оживляет кнопки `aw_*`, а не только картинку экрана.
        await h.callback(_callback(h, "aw_curpick:"), user_id=AUTHOR_TG)
        self.assertTrue(
            _callbacks(h, "aw_photo_none:"),
            f"кнопка конструктора после Rich-страниц не работает: {h.screen_buttons()}",
        )
        self.assertEqual(_value_text(_details(h, _draft_id(h)), CURATOR_CODE), "@c1")

    @scenario_test
    async def test_report_cancel_returns_to_same_question(self, h) -> None:
        await self._open_question(h)
        question = h.screen_text()
        state = h.dialog_state(name="announcement_wizard")

        await h.text("/report", user_id=AUTHOR_TG)
        cat.assert_shows(self, h.screen_text(), "report.screen.start")
        self.assertEqual(
            [data for _label, data in h.screen_buttons()],
            ["rp_compose", "rp_cancel"],
        )
        self.assertEqual(h.dialog_state(name="announcement_wizard"), state)

        await h.callback("rp_cancel", user_id=AUTHOR_TG)
        self.assertEqual(
            h.screen_text(), question, "экран конструктора не восстановлен"
        )
        self.assertEqual(h.dialog_state(name="announcement_wizard"), state)

        # Возврат не оставляет кнопку конструктора мёртвой.
        await h.callback(_callback(h, "aw_curpick:"), user_id=AUTHOR_TG)
        self.assertTrue(_callbacks(h, "aw_photo_none:"))
        self.assertEqual(_value_text(_details(h, _draft_id(h)), CURATOR_CODE), "@c1")

    @scenario_test
    async def test_empty_catalog_row_after_rich_pages_keeps_navigation(self, h) -> None:
        """§19.6/§19.8: после Rich-страниц пустой справочник не блокирует шаг."""
        place_id = _seed_place(h)
        _set_standard(h, place_id, "Куратор: [куратор]")
        await _open_place_questions(h, place_id)
        await _answer_name(h)

        await h.text("/help", user_id=AUTHOR_TG)
        await h.callback("help_back", user_id=AUTHOR_TG)
        self.assertRegex(h.screen_text(), cat.question_re(2, 2))

        _assert_disabled_row(self, h, NO_CURATORS, NO_CURATORS_HINT)
        skip = _callbacks(h, "aw_qskip:")
        self.assertEqual(len(skip), 1, "«Пропустить» должно остаться активным")
        await h.callback(skip[0], user_id=AUTHOR_TG)

        await h.callback(_callback(h, "aw_photo_none:"), user_id=AUTHOR_TG)
        draft_id = _draft_id(h)
        assert_delivered(h, draft_id)
        self.assertEqual(h.posts()[-1], "<b>Летний концерт</b>")
        self.assertNotIn(MENU_TEXT, h.screen_text())


if __name__ == "__main__":  # pragma: no cover — ручной запуск
    unittest.main()
