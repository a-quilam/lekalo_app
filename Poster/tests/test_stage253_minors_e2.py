# tests/test_stage253_minors_e2.py
"""
Poster 2.5.3 — постоянные тесты мелких правок этапа E2 (R1–R3).

Каждый тест закрепляет пользовательский результат, а не внутреннюю функцию:
настоящее приложение, FakeBot и временная SQLite
(`tests/integration_harness.py`). Реальная `post_bot.db`, `.env` и сеть
Telegram не используются.

Что закрепляется:

J (R1). Сбой чтения расширений гасит кнопку «🧩 Дополнительные поля»
   (fail-closed): у анонса с доступными доп. полями кнопка есть (контроль),
   а при падении `extras_available_for_draft` экран управления не обещает
   пустой раздел. Остальное управление (текстовые поля, фото, меню) живо —
   сбой предиката не отнимает редактор.
K (R2). `cleanup_messages` не обращается к Telegram с неположительным
   `message_id` (0/минус — «id ещё не получен», а не адрес сообщения):
   удаляется только настоящий id, исключений нет. Та же защита у
   `wizard_live._delete_old` (снятие прошлой версии блока).
L (R3). Удаление личного шаблона через `tp_delete_yes`, когда шаблон уже
   изменён другим окном (revision в состоянии устарел): пользователь
   остаётся в карточке с пояснением, свежая версия в карточке и в БД,
   ничего не удалено. Happy-path: шаблон без снимков/анонсов удаляется,
   показывается хаб.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage253_minors_e2 -v
"""

from __future__ import annotations

import asyncio
import unittest
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

if TYPE_CHECKING:
    # Только для типов: FakeBot заменяет Bot по duck-typing (см.
    # tests/integration_harness.py), поэтому явный каст к Bot — на месте
    # вызова функций с аннотацией "Bot", а не ослабление проверки.
    from telegram import Bot

try:
    import announcement_controls as controls
    from handlers import templates as templates_module
    from handlers import wizard_live
    from template_state import load_template_session
    from template_store import (
        get_personal_template,
        list_personal_templates,
        update_template_content,
    )
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        FakeBot,
        scenario_test,
    )
    from tests.test_stage25_delivery_instances import _only_active
    from tests.test_stage27b_announcement_ui import (
        _choose_personal,
        _latest_draft,
        _no_photos,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from utils.screen import cleanup_messages
    from utils.ui_texts import tr

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


EXTRAS_LABEL = tr("buttons.finished.extras_edit")
PHOTOS_TITLE = tr("wizard.photos.title")
# Исходы удаления личного шаблона (config/ui_texts.yaml).
DELETED_NOTICE = tr("templates.handle_delete_yes.shablon_udalen")
STALE_NOTICE = tr("templates.handle_delete_yes.udalenie_vypolneno_shablon")
HUB_EMPTY = tr("templates.hub.moi_shablony_u_poka")

TemplateSpec = tuple[str, str, str, dict[str, object]]
PLAIN_SPEC: TemplateSpec = ("main", "Основное поле", "short_text", {})
EXTRA_SPEC: TemplateSpec = ("extra", "Доп. поле", "short_text", {})


# --- помощники -------------------------------------------------------------


def _labels(buttons) -> list[str]:
    return [label for label, _data in buttons]


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data
        for _label, data in h.live_buttons()
        if data is not None and data.startswith(prefix)
    ]


def _personal_row(h, template_id: int):
    with h.session_factory() as session:
        return get_personal_template(session, DEFAULT_USER_ID, template_id)


def _personal_count(h) -> int:
    with h.session_factory() as session:
        return len(list_personal_templates(session, DEFAULT_USER_ID))


async def _delivered_with_extra(h):
    """
    Анонс без фото по шаблону, у которого есть дополнительное поле.

    Кнопка «🧩 Дополнительные поля» у такого анонса обязана быть: экран
    доп. полей не пуст (кандидат `[extra]` есть только в расширенной версии).
    """
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, _bricks = _seed_personal_template(
        h,
        specs=[PLAIN_SPEC, EXTRA_SPEC],
        content="[main]",
        extended_content="[main]\n[extra]",
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    for _attempt in range(5):
        if PHOTOS_TITLE in h.screen_text():
            break
        await h.text("Значение")
    if PHOTOS_TITLE not in h.screen_text():
        raise AssertionError(f"Не шаг фото: {h.screen_text()[:200]!r}")
    await _no_photos(h)
    return _latest_draft(h)


async def _reopen_controls(h, instance_id: int) -> None:
    """
    «📝 Текстовые поля» → «◀️ Назад»: единственный путь, который заново
    собирает клавиатуру управления (`_control_keyboard` → предикат доп. полей).
    """
    data = controls.panel_callback(controls.ACTION_DATA, instance_id)
    await h.callback(data)
    backs = h.live_callbacks("anf_back:")
    if not backs:
        raise AssertionError(f"нет «Назад» списка полей: {h.live_buttons()}")
    await h.callback(backs[0])


# --- J. R1: сбой предиката скрывает кнопку, управление остаётся ------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ExtrasFailClosedTests(unittest.TestCase):
    """R1: «не смогли проверить» — это «нет кнопки», а не «есть»."""

    @scenario_test
    async def test_extras_read_failure_hides_button_only(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_with_extra(h)
        instance = _only_active(h, draft.id)

        # Контроль: при рабочем чтении кнопка есть.
        self.assertIn(EXTRAS_LABEL, _labels(h.live_buttons()))
        await _reopen_controls(h, instance.id)
        self.assertIn(
            EXTRAS_LABEL,
            _labels(h.live_buttons()),
            "без сбоя кнопка доп. полей возвращается вместе с управлением",
        )

        # Сбой чтения расширений: кнопки нет, остальное управление на месте.
        with patch(
            "handlers.announcement_wizard.extras_available_for_draft",
            side_effect=RuntimeError("имитация сбоя чтения расширений"),
        ) as broken:
            await _reopen_controls(h, instance.id)
            labels = _labels(h.live_buttons())

        self.assertGreater(
            broken.call_count,
            0,
            "проверка кнопки обязана пройти через предикат (иначе тест пуст)",
        )
        self.assertNotIn(
            EXTRAS_LABEL,
            labels,
            "сбой предиката не должен обещать пустой экран доп. полей",
        )
        self.assertEqual(
            _callbacks(h, "anp_extras"),
            [],
            "callback доп. полей не остаётся в живых клавиатурах",
        )
        # Управление живо: текстовые поля, фото и выход в меню.
        for prefix, what in (
            ("anp_data:", "текстовые поля"),
            ("anp_photos:", "фото"),
            ("anp_menu:", "меню"),
        ):
            self.assertTrue(
                _callbacks(h, prefix),
                f"{what} должны оставаться доступны при сбое предиката",
            )
        # Анонс не переиздан: единственный активный экземпляр тот же.
        self.assertEqual(
            _only_active(h, draft.id).id,
            instance.id,
            "сбой предиката не создаёт новый экземпляр анонса",
        )


# --- K. R2: неположительный message_id не уходит в Telegram ----------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NonPositiveMessageIdTests(unittest.TestCase):
    """R2: 0 и минус — «id ещё не получен», а не адрес сообщения."""

    def test_cleanup_skips_non_positive_ids(self) -> None:
        async def run() -> list[dict]:
            bot = FakeBot()
            await cleanup_messages(bot, 777, [0, -5, 4242])
            return [call for call in bot.calls if call["type"] == "delete_message"]

        deletes = asyncio.run(run())

        self.assertEqual(len(deletes), 1, f"лишние удаления: {deletes}")
        self.assertEqual(deletes[0]["args"], (777, 4242))
        # Ни 0, ни -5 в вызовах Telegram не попали.
        ids = [call["args"][1] for call in deletes]
        self.assertEqual(ids, [4242])

    def test_delete_old_skips_non_positive_ids(self) -> None:
        """Та же защита при снятии прошлой версии живого блока."""

        async def run() -> list[dict]:
            bot = FakeBot()
            await wizard_live._delete_old(cast("Bot", bot), 777, (0, -5, 5555))
            return [call for call in bot.calls if call["type"] == "delete_message"]

        deletes = asyncio.run(run())

        self.assertEqual(len(deletes), 1, f"лишние удаления: {deletes}")
        self.assertEqual(deletes[0]["args"], (777, 5555))

    def test_all_non_positive_ids_do_not_call_telegram(self) -> None:
        async def run() -> list[dict]:
            bot = FakeBot()
            await cleanup_messages(bot, 777, [0, -1])
            return [call for call in bot.calls if call["type"] == "delete_message"]

        self.assertEqual(asyncio.run(run()), [], "нет настоящих id — нет удалений")


# --- L. R3: удаление личного шаблона --------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeletePersonalTemplateTests(unittest.TestCase):
    """R3: карточка после отказа строится по данным, которые прочитала служба."""

    @scenario_test
    async def test_stale_revision_keeps_card_and_shows_fresh_version(self, h) -> None:
        template_id, revision, _bricks = _seed_personal_template(
            h, name="Изменённый шаблон"
        )
        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_delete:{template_id}")
        self.assertIn("Удалить шаблон?", h.screen_text())

        # Пока подтверждение висело на экране, шаблон изменили другим путём:
        # revision в состоянии удаления уже не совпадает с БД.
        new_content = "Новый текст [main]"
        with h.session_factory() as session:
            updated = update_template_content(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                template_id=template_id,
                expected_revision=revision,
                content=new_content,
            )
        if not updated.ok:
            raise AssertionError(f"шаблон не обновлён: {updated.message}")
        fresh = _personal_row(h, template_id)
        self.assertIsNotNone(fresh)
        assert fresh is not None
        self.assertNotEqual(fresh.revision, revision)

        # R3: шаблон читается РОВНО ОДИН раз — внутри самой операции удаления;
        # ветки stale/blocked используют этот же результат, а не запрашивают
        # строку повторно (иначе между проверкой и карточкой снова появляется
        # окно для чужой правки).
        reads: list[int] = []
        original = templates_module.get_personal_template

        def counting(session, actor_telegram_user_id, read_template_id):
            reads.append(int(read_template_id))
            return original(session, actor_telegram_user_id, read_template_id)

        with patch.object(templates_module, "get_personal_template", counting):
            await h.callback(f"tp_delete_yes:{template_id}")

        self.assertEqual(
            reads,
            [template_id],
            "карточка строится по тому же чтению, что и проверка удаления",
        )

        # Пользователь остался в карточке, видит пояснение и НОВУЮ версию.
        text = h.screen_text()
        self.assertIn(STALE_NOTICE, text, "исход «шаблон изменился» объяснён")
        self.assertIn("Изменённый шаблон", text, "карточка того же шаблона")
        self.assertIn(
            tr("templates.build.versiya", revision=fresh.revision),
            text,
            "карточка свежая",
        )
        self.assertIn(
            new_content,
            text,
            "карточка показывает текст, прочитанный в той же операции",
        )
        self.assertIn(f"tp_delete:{template_id}", _callbacks(h, "tp_delete:"))

        # Данные целы: шаблон на месте, ни снимков, ни анонсов не появилось.
        row = _personal_row(h, template_id)
        self.assertIsNotNone(row, "шаблон не должен удаляться по устаревшей карточке")
        assert row is not None
        self.assertEqual(row.revision, fresh.revision)
        self.assertEqual(row.current_version.content, new_content)
        self.assertEqual(_personal_count(h), 1)

        # Состояние карточки тоже актуально: повторное удаление сразу после
        # пояснения снова открывает подтверждение, а не «кнопка устарела».
        state = load_template_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "card")
        self.assertEqual(state["revision"], fresh.revision)
        await h.callback(f"tp_delete:{template_id}")
        self.assertIn("Удалить шаблон?", h.screen_text())
        self.assertIn(
            tr(
                "templates.delete_confirm.udalit_shablon_shablon_tekuschaya",
                display_name="Изменённый шаблон",
                revision=fresh.revision,
            ),
            h.screen_text(),
            "подтверждение снова показывает актуальную версию",
        )

    @scenario_test
    async def test_happy_path_deletes_template_and_shows_hub(self, h) -> None:
        template_id, revision, _bricks = _seed_personal_template(
            h, name="Удаляемый шаблон"
        )
        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_delete:{template_id}")
        self.assertIn(f"Текущая версия: {revision}", h.screen_text())

        await h.callback(f"tp_delete_yes:{template_id}")

        text = h.screen_text()
        self.assertIn(DELETED_NOTICE, text, "исход удаления подтверждён")
        self.assertIn(HUB_EMPTY, text, "после удаления показан хаб шаблонов")
        self.assertIsNone(_personal_row(h, template_id), "шаблон удалён из БД")
        self.assertNotIn(
            f"tp_open:{template_id}",
            _callbacks(h, "tp_open:"),
            "удалённого шаблона нет в хабе",
        )
        self.assertEqual(_personal_count(h), 0)

        state = load_template_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "hub", "состояние перешло в хаб")


if __name__ == "__main__":
    unittest.main()
