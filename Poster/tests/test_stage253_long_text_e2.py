# tests/test_stage253_long_text_e2.py
"""
Poster 2.5.3 §7 — приёмка E2: длинный текст остаётся в черновике.

Проверяется пользовательский результат через настоящее приложение, FakeBot и
временную SQLite (`tests/integration_harness.py`): реальная `post_bot.db`,
`.env` и сеть Telegram не используются.

Что закрепляется (утверждённый владелец §7):

- **G.** Длинный текст сохраняется в черновике, но НЕ публикуется; ошибка
  КОРОТКАЯ — одна строка про лимит, без большого экрана восстановления
  («Самые длинные поля», «Изменить другое поле», «Без фотографий»).
  Ввод пользователя (сообщение с текстом и снимок) остаётся в чате, фото и
  значение — в БД; уже доставленная версия не меняется.
- **H.** Состояние «текст не помещается» живёт в БД и переживает рестарт
  службы: короткая пометка видна при открытии черновика, прежний готовый
  анонс цел.
- **I.** Старого маршрута восстановления больше нет: ни функций
  `_render_length_problem`/`length_problem`, ни обработчика `aw_photo_drop`;
  подделанный `aw_photo_drop` не меняет данные и отвечает «устарело»;
  мёртвых ключей каталога нет.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage253_long_text_e2 -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests import ui_texts_expect as cat

try:
    import announcement_controls as controls
    from handlers.announcement_wizard import AW_PREVIEW, CONVERSATION_NAME
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage25_delivery_instances import (
        _only_active,
        _stored,
    )
    from tests.test_stage27b_announcement_ui import (
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from utils import ui_texts
    from utils.ui_texts import tr

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# Подписи — из каталога текстов (config/ui_texts.yaml), а не литералами.
PHOTOS_TITLE = tr("wizard.photos.title")
# Отказ по длине различает два места: шаг фото/черновик и правку уже
# ДОСТАВЛЕННОГО анонса (2.5.4 §3). Смысл один — текст не помещается и не
# публикуется, поэтому проверка идёт по ключам `wizard.length_refusal.*`.
CAPTION_LIMIT = 1024

# Формулировки старого экрана восстановления: их быть не должно.
REMOVED_MARKERS = (
    "Самые длинные поля",
    "Изменить другое поле",
    "Без фотографий",
    "Бот не обрезает текст",
)

# 2.5.3 §7: длинный текст — больше подписи к фото (1024) и меньше лимита без
# фото (4096), поэтому одно и то же значение проходит без снимка и не
# проходит с ним.
LONG_VALUE = "Длинный текст анонса " * 150  # ~3150 символов

ROOT = Path(__file__).resolve().parent.parent


# --- помощники -------------------------------------------------------------


async def _draft_at_photo_step(h, *, value: str = LONG_VALUE):
    """Личный шаблон с одним кирпичиком и ответом `value`, шаг фото."""
    place_id, _alias = _seed_place(h)
    template_id, _revision, _bricks = _seed_personal_template(h, content="[main]")
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text(value)
    if PHOTOS_TITLE not in h.screen_text():
        raise AssertionError(f"Не шаг фото: {h.screen_text()[:200]!r}")
    return _latest_draft(h)


async def _delivered_with_photo(h, *, value: str):
    """Готовый анонс с одним фото и коротким текстом `value`."""
    draft = await _draft_at_photo_step(h, value=value)
    await h.photo("short-photo")
    stored = _stored(h, draft.id)
    if stored.publication_status != "sent":
        raise AssertionError(f"Анонс не доставлен: {h.screen_text()[:200]!r}")
    return draft


def _screen_and_answers(h) -> str:
    """
    Всё, что бот показал в этот раз: экран и короткие ответы.

    2.5.4 §1: ошибка ввода поля приходит ответом на сообщение автора (ввод
    живёт на самом анонсе), поэтому пустые ответы на callback тоже
    включаются — иначе короткая ошибка не видна.
    """
    return "\n".join([h.screen_text(), *h.answer_texts(), *h.reply_texts()])


def _no_removed_screen(h) -> None:
    """Большого экрана восстановления больше нет ни в тексте, ни в кнопках."""
    shown = _screen_and_answers(h)
    for marker in REMOVED_MARKERS:
        if marker in ("Без фотографий",):
            # «Без фотографий» — прежняя кнопка проблемного экрана; на шаге
            # фото её тоже нет («Пропустить»), поэтому проверяется весь чат.
            labels = [label for label, _data in h.screen_buttons()]
            assert marker not in labels, f"кнопка {marker!r} вернулась: {labels}"
            continue
        assert marker not in shown, f"старый экран вернулся: {marker!r}"


# --- G. длинный текст: черновик, короткая ошибка, нет публикации -----------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LongTextKeepsDraftTests(unittest.TestCase):
    """§7: текст сохраняется в черновике, публикации нет, ошибка короткая."""

    @scenario_test
    async def test_long_text_with_photo_is_not_published(self, h) -> None:
        await h.text("/start")
        draft = await _draft_at_photo_step(h)
        photo_update = await h.photo("over-limit-photo")

        shown = _screen_and_answers(h)
        cat.assert_length_refusal(
            self, shown, CAPTION_LIMIT, f"нет короткой ошибки: {shown!r}"
        )
        _no_removed_screen(h)

        stored = _stored(h, draft.id)
        self.assertIsNone(stored.publication_status, "превышение не публикуется")
        self.assertEqual(stored.photos, ("over-limit-photo",), "фото сохранено")
        # Длинное значение целиком в черновике, а не «предпросмотр».
        with h.session_factory() as session:
            from announcement_store import build_announcement_preview
            from tests.integration_harness import DEFAULT_USER_ID
            from utils.parsers import app_today

            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                today=app_today(),
            )
        self.assertIn(LONG_VALUE.strip(), preview.visible_text)
        # Ввод не потерян: сообщение со снимком осталось в чате.
        self.assertNotIn(photo_update.message.message_id, h.deleted_ids())
        self.assertNotIn("Кнопка устарела", h.answer_texts())

    @scenario_test
    async def test_published_version_stays_intact_after_refusal(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_with_photo(h, value="Коротко")
        published = _stored(h, draft.id).publication_text
        delivered_ids = tuple(_stored(h, draft.id).delivered_message_ids)
        self.assertEqual(published, "Коротко")

        # Правка поля через список «📝 Текстовые поля» на самом анонсе.
        instance = _only_active(h, draft.id)
        await h.callback(controls.panel_callback(controls.ACTION_DATA, instance.id))
        selected = [
            data
            for _label, data in h.live_buttons()
            if data and data.startswith("anf_sel:")
        ]
        self.assertTrue(selected, f"нет списка полей: {h.live_buttons()}")
        await h.callback(selected[0])
        long_message = await h.text(LONG_VALUE)

        shown = _screen_and_answers(h)
        cat.assert_length_refusal(self, shown, msg=f"нет короткой ошибки: {shown!r}")
        _no_removed_screen(h)
        # Ввод с новым текстом остался в чате — сократить можно им же.
        self.assertNotIn(long_message.message.message_id, h.deleted_ids())

        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(
            stored.publication_text, published, "доставленная версия изменилась"
        )
        self.assertEqual(stored.delivered_message_ids, delivered_ids, "анонс переиздан")
        # В чате по-прежнему ровно одна версия анонса с прежним текстом.
        self.assertEqual(h.bot.content_text(h.chat_id, delivered_ids[0]), published)


# --- H. состояние из БД, переживает рестарт --------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LongTextSurvivesRestartTests(unittest.TestCase):
    """§7: «не помещается» вычисляется из БД, а не из флага в `user_data`."""

    @scenario_test
    async def test_restart_keeps_long_draft_and_published_announcement(self, h) -> None:
        await h.text("/start")
        # Готовый анонс: его версия после рестарта обязана остаться прежней.
        delivered = await _delivered_with_photo(h, value="Коротко")
        published = _stored(h, delivered.id)
        self.assertEqual(published.publication_status, "sent")

        # Новый черновик с длинным текстом и фото: не публикуется.
        await h.text("/start")
        draft = await _draft_at_photo_step(h)
        await h.photo("long-photo")
        cat.assert_length_refusal(self, _screen_and_answers(h))
        self.assertIsNone(_stored(h, draft.id).publication_status)

        await h.restart()
        self.assertIsNone(
            h.user_data().get("announcement_ui"),
            "состояние диалога должно потеряться",
        )

        # Открытие черновика из «Мои анонсы»: пометка пересчитана из БД.
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")
        screen = h.screen_text()
        cat.assert_length_refusal(
            self, screen, CAPTION_LIMIT, f"нет пометки после рестарта: {screen!r}"
        )
        _no_removed_screen(h)

        restored = _stored(h, draft.id)
        self.assertIsNone(restored.publication_status, "черновик не опубликован")
        self.assertEqual(restored.photos, ("long-photo",), "фото пережило рестарт")

        # Прежний готовый анонс цел: тот же текст и те же адреса.
        still = _stored(h, delivered.id)
        self.assertEqual(still.publication_text, published.publication_text)
        self.assertEqual(still.delivered_message_ids, published.delivered_message_ids)
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)


# --- I. старый маршрут удалён ---------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RemovedRouteTests(unittest.TestCase):
    """§7: сломанный маршрут восстановления удалён целиком."""

    def test_source_has_no_legacy_length_route(self) -> None:
        source = (ROOT / "handlers" / "announcement_wizard.py").read_text(
            encoding="utf-8"
        )
        for name in (
            "_render_length_problem",
            "_longest_fields",
            "_length_problem_pending",
            "_resume_after_shortening",
            "aw_photo_drop",
            '"length_problem"',
            "'length_problem'",
        ):
            self.assertNotIn(name, source, f"старый маршрут остался: {name}")

    def test_catalog_has_no_length_problem_keys(self) -> None:
        dead = [
            key
            for key in ui_texts.DEFAULTS
            if key.startswith("wizard.render_length_problem.")
            or key
            in {
                "buttons.wizard.bez_fotografiy",
                "buttons.wizard.izmenit_drugoy_kirpichik",
                "alerts.wizard.limit_snova_4096_simvolov",
                "wizard.handle_preview_callback.otpravlyaem_bez_fotografiy_limi",
            }
        ]
        self.assertEqual(dead, [], f"мёртвые ключи каталога: {dead}")
        self.assertIn("wizard.length_refusal.not_pomeschaetsya", ui_texts.DEFAULTS)

    @scenario_test
    async def test_forged_photo_drop_is_stale_and_keeps_data(self, h) -> None:
        await h.text("/start")
        draft = await _draft_at_photo_step(h)
        await h.photo("kept-photo")
        stored = _stored(h, draft.id)
        self.assertEqual(stored.photos, ("kept-photo",))
        self.assertIsNone(stored.publication_status)

        await h.callback(f"aw_photo_drop:{draft.id}:{stored.revision}")

        # Обработчика больше нет: подделанный callback уходит в общий stale.
        answers = " | ".join(h.answer_texts())
        cat.assert_stale(self, answers, f"ответ на подделку: {answers!r}")
        after = _stored(h, draft.id)
        self.assertEqual(after.photos, ("kept-photo",), "фото не удалены")
        self.assertIsNone(after.publication_status, "подделка ничего не отправила")


if __name__ == "__main__":
    unittest.main()
