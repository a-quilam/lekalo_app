# tests/test_stage262_public_deep_link.py
"""
Poster 2.6.2 §4: публичная карточка места и deep link конструктора.

Контракт:

1. публичная карточка (её видит ЛЮБОЙ пользователь, в том числе не-админ)
   показывает блок «Создать анонс для этой площадки:» и кнопку со ссылкой
   `https://t.me/<бот>?start=<token>`;
2. ссылка одна на площадку и не персональная: два разных пользователя видят
   один и тот же адрес, второй строки в `place_invite_links` не появляется;
3. переход по ссылке сразу открывает конструктор ИМЕННО этой площадки — без
   выбора места и без выбора шаблона;
4. ссылка НЕ даёт прав: `place_admins` по ней не создаётся, админ-экраны
   места (`adm_*`, `plm_*`) недоступны;
5. удалённое место: переход по ссылке даёт понятное сообщение и главное меню,
   без ошибки и без внутренних подробностей;
6. «◀️ Назад» из конструктора по ссылке ведёт в главное меню: исходного
   списка у пришедшего по ссылке нет, а черновик остаётся в «Мои анонсы».

Все сценарии идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (`tests.integration_harness`): рабочая база, `.env` и сеть
Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage262_public_deep_link -v
"""

from __future__ import annotations

import sqlite3
import unittest

from sqlalchemy import select

from tests import ui_texts_expect as cat
from tests.integration_harness import (
    HARNESS_IMPORT_ERROR,
    Harness,
    scenario_test,
)

OWNER_TG = 2_622_101
VIEWER_TG = 2_622_102
VIEWER2_TG = 2_622_103

CONVERSATION_NAME = "announcement_wizard"


# --- помощники ---------------------------------------------------------------


def _seed_place(
    h: Harness, name: str = "Дом культуры", *, with_link: bool = True
) -> int:
    """
    Площадка со стандартным шаблоном и ВЫПУЩЕННОЙ ссылкой.

    Ссылку выпускает администратор площадки (OWNER_TG): публичный просмотр
    её только показывает и ничего не создаёт, поэтому в сценариях «ссылка
    уже есть» она подготовлена заранее — как в рабочей базе после того, как
    администратор открыл свой раздел ссылки.
    """
    from models import PlaceAdmin, utcnow
    from place_invite_store import ensure_invite
    from place_store import create_place
    from user_store import get_or_create_user

    with h.session_factory() as session:
        owner = get_or_create_user(session, OWNER_TG, username="owner")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=owner.id,
            address="ул. Тестовая, 1",
            url="https://example.com/place",
        )
        session.add(
            PlaceAdmin(
                place_id=place.id,
                user_id=owner.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
            )
        )
        session.commit()
        if with_link:
            ensure_invite(
                session, actor_telegram_user_id=OWNER_TG, place_id=int(place.id)
            )
        return int(place.id)


def _seed_user(h: Harness, telegram_user_id: int) -> None:
    from user_store import get_or_create_user

    with h.session_factory() as session:
        get_or_create_user(session, telegram_user_id, username=f"u{telegram_user_id}")
        session.commit()


def _callbacks(h: Harness, prefix: str = "") -> list[str]:
    return [
        str(data)
        for _label, data in h.screen_buttons()
        if data is not None and str(data).startswith(prefix)
    ]


def _url_buttons(h: Harness) -> list[tuple[str, str]]:
    """Кнопки-ссылки текущего экрана: (подпись, url)."""
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        (button.text, button.url)
        for row in markup.inline_keyboard
        for button in row
        if getattr(button, "url", None)
    ]


def _public_link(h: Harness) -> str:
    links = _url_buttons(h)
    assert links, f"на карточке нет кнопки-ссылки: {h.screen_buttons()}"
    return links[0][1]


def _token_from_link(link: str) -> str:
    marker = "?start="
    assert marker in link, f"это не deep link: {link!r}"
    return link.split(marker, 1)[1]


def _active_tokens(h: Harness, place_id: int) -> list[str]:
    from models import PlaceInviteLink

    with h.session_factory() as session:
        rows = list(
            session.scalars(
                select(PlaceInviteLink)
                .where(PlaceInviteLink.place_id == place_id)
                .order_by(PlaceInviteLink.id)
            ).all()
        )
    return [row.token for row in rows if row.revoked_at is None]


def _drafts(h: Harness, telegram_user_id: int):
    from announcement_store import list_announcement_drafts

    with h.session_factory() as session:
        return list(
            list_announcement_drafts(session, actor_telegram_user_id=telegram_user_id)
        )


def _can_manage(h: Harness, telegram_user_id: int, place_id: int) -> bool:
    from permissions import can_manage_place

    with h.session_factory() as session:
        return can_manage_place(session, telegram_user_id, place_id)


async def _open_public_card(h: Harness, place_id: int, *, tg: int) -> None:
    """Публичная карточка не-админа: «Управление → Площадки → место»."""
    await h.text("/start", user_id=tg)
    await h.callback("manage", user_id=tg)
    await h.callback("manage_places", user_id=tg)
    await h.callback(f"plm_open:{place_id}", user_id=tg)


# --- §4: публичная карточка и ссылка ----------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PublicCardLinkTests(unittest.TestCase):
    """Пункты 1–2: блок ссылки на публичной карточке и её стабильность."""

    @scenario_test
    async def test_visitor_sees_invite_block_with_link(self, h: Harness) -> None:
        """Пункт 1: не-админ видит блок и кнопку с deep link."""
        place_id = _seed_place(h)
        _seed_user(h, VIEWER_TG)

        await _open_public_card(h, place_id, tg=VIEWER_TG)

        cat.assert_shows(self, h.screen_text(), "places.public.invite_title")
        self.assertFalse(
            _can_manage(h, VIEWER_TG, place_id), "посетитель не должен быть админом"
        )
        links = _url_buttons(h)
        self.assertEqual(len(links), 1, f"ожидалась одна кнопка-ссылка: {links}")
        label, link = links[0]
        cat.assert_shows(self, label, "buttons.places.public_invite")
        self.assertTrue(
            link.startswith(f"https://t.me/{h.bot.username}?start="),
            f"ссылка должна вести на бота: {link!r}",
        )
        token = _token_from_link(link)
        self.assertNotEqual(token, str(place_id), "payload не равен id площадки")
        self.assertGreaterEqual(len(token), 16, "payload — случайный токен")
        self.assertEqual(len(_active_tokens(h, place_id)), 1)
        # Админ-действий на публичной карточке нет.
        self.assertEqual(_callbacks(h, "adm_"), [])
        self.assertEqual(_callbacks(h, "plm_edit"), [])
        # «◀️ К списку» возвращает в тот же справочник, откуда открыли карточку.
        await h.callback("plm_back", user_id=VIEWER_TG)
        cat.assert_shows(self, h.screen_text(), "places.manage.title")

    @scenario_test
    async def test_card_without_issued_link_has_no_block(self, h: Harness) -> None:
        """
        Пока ссылку не выпустил администратор, блока нет — и просмотр её НЕ
        создаёт: публичная карточка ничего не пишет в БД.
        """
        place_id = _seed_place(h, with_link=False)
        _seed_user(h, VIEWER_TG)

        await _open_public_card(h, place_id, tg=VIEWER_TG)

        text = h.screen_text()
        self.assertIn("Дом культуры", text)
        self.assertNotIn(cat.text("places.public.invite_title"), text)
        self.assertEqual(_url_buttons(h), [])
        self.assertEqual(_active_tokens(h, place_id), [], "просмотр выдал ссылку")

    @scenario_test
    async def test_link_is_the_same_for_two_users(self, h: Harness) -> None:
        """Пункт 2: ссылка одна и та же, второй строки не создаётся."""
        place_id = _seed_place(h)
        _seed_user(h, VIEWER_TG)
        _seed_user(h, VIEWER2_TG)

        await _open_public_card(h, place_id, tg=VIEWER_TG)
        first = _public_link(h)

        await _open_public_card(h, place_id, tg=VIEWER2_TG)
        second = _public_link(h)

        self.assertEqual(first, second, "ссылка должна быть общей для всех")
        self.assertEqual(
            len(_active_tokens(h, place_id)), 1, "ссылок стало больше одной"
        )


# --- §4: переход по ссылке ---------------------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PublicDeepLinkOpenTests(unittest.TestCase):
    """Пункты 3–6: конструктор по ссылке, права, удалённое место, «Назад»."""

    @scenario_test
    async def test_link_opens_wizard_for_exact_place(self, h: Harness) -> None:
        """Пункт 3: по ссылке сразу конструктор именно этой площадки."""
        place_id = _seed_place(h)
        _seed_user(h, VIEWER_TG)
        await _open_public_card(h, place_id, tg=VIEWER_TG)
        token = _token_from_link(_public_link(h))

        await h.text(f"/start {token}", user_id=VIEWER_TG)

        text = h.screen_text()
        self.assertRegex(text, cat.question_re(1))
        self.assertNotIn(cat.text("places.pick.title"), text)
        self.assertIsNotNone(
            h.dialog_state(user_id=VIEWER_TG, name=CONVERSATION_NAME),
            "конструктор должен быть активен",
        )
        drafts = _drafts(h, VIEWER_TG)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].place_id, place_id)
        self.assertEqual(drafts[0].origin, "place")
        # Следующий ответ принимается: диалог живой.
        await h.text("Мой концерт", user_id=VIEWER_TG)
        self.assertNotRegex(h.screen_text(), cat.question_re(1))

    @scenario_test
    async def test_link_grants_no_place_edit_rights(self, h: Harness) -> None:
        """Пункт 4: ссылка не даёт прав и не открывает админ-экраны места."""
        place_id = _seed_place(h)
        _seed_user(h, VIEWER_TG)
        await _open_public_card(h, place_id, tg=VIEWER_TG)
        token = _token_from_link(_public_link(h))

        await h.text(f"/start {token}", user_id=VIEWER_TG)

        # Роль не выдана: ссылка не создаёт `place_admins`.
        self.assertFalse(_can_manage(h, VIEWER_TG, place_id))
        # В конструкторе нет кнопок администрирования места.
        self.assertEqual(_callbacks(h, "adm_"), [])
        self.assertEqual(_callbacks(h, "plm_"), [])
        # Раздел администрирования по-прежнему закрыт.
        await h.text("/admin", user_id=VIEWER_TG)
        self.assertIn("доступ", h.screen_text().lower())

    @scenario_test
    async def test_deleted_place_link_is_handled_safely(self, h: Harness) -> None:
        """Пункт 5: удалённое место — понятное сообщение, без ошибки."""
        place_id = _seed_place(h)
        _seed_user(h, VIEWER_TG)
        await _open_public_card(h, place_id, tg=VIEWER_TG)
        token = _token_from_link(_public_link(h))

        # Удаление вне приложения (обслуживание базы): строка ссылки остаётся
        # сиротой, отсечь её обязана проверка существования места.
        connection = sqlite3.connect(str(h.engine.url.database))
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DELETE FROM places WHERE id = ?", (place_id,))
            connection.commit()
        finally:
            connection.close()

        await h.text(f"/start {token}", user_id=VIEWER_TG)

        text = h.screen_text()
        cat.assert_shows(self, text, "places.invite.invalid")
        self.assertNotIn("Traceback", text)
        self.assertNotIn("внутренняя ошибка", text.lower())
        self.assertIsNone(
            h.dialog_state(user_id=VIEWER_TG, name=CONVERSATION_NAME),
            "конструктор не должен открываться по удалённой площадке",
        )
        self.assertEqual(_drafts(h, VIEWER_TG), [])
        self.assertIn("pick_place", _callbacks(h))

    @scenario_test
    async def test_back_from_linked_wizard_goes_to_main_menu(self, h: Harness) -> None:
        """Пункт 6: «◀️ Назад» на первом вопросе — в главное меню."""
        place_id = _seed_place(h)
        _seed_user(h, VIEWER_TG)
        await _open_public_card(h, place_id, tg=VIEWER_TG)
        token = _token_from_link(_public_link(h))

        await h.text(f"/start {token}", user_id=VIEWER_TG)
        self.assertRegex(h.screen_text(), cat.question_re(1))

        backs = _callbacks(h, "aw_qback:")
        self.assertEqual(len(backs), 1, f"нет кнопки «Назад»: {h.screen_buttons()}")
        await h.callback(backs[0], user_id=VIEWER_TG)

        # Исходного списка у пришедшего по ссылке нет — ведёт главное меню.
        cat.assert_shows(self, h.screen_text(), "menu.main")
        self.assertIn("pick_place", _callbacks(h))
        # Черновик не удалён: он остаётся в «Мои анонсы».
        drafts = _drafts(h, VIEWER_TG)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].place_id, place_id)
        # Ни справочника, ни админ-экранов по пути не показано.
        self.assertEqual(_callbacks(h, "plm_"), [])
        self.assertEqual(_callbacks(h, "adm_"), [])

    @scenario_test
    async def test_back_after_restart_still_goes_to_main_menu(self, h: Harness) -> None:
        """
        Пункт 6 + рестарт: вход по ссылке переживает перезапуск службы.

        Отметка «вход по ссылке» живёт в памяти (`user_data`) и теряется при
        рестарте. Resume-снимок обязан её сохранить, иначе «◀️ Назад» с
        первого вопроса после продолжения ввода ведёт к выбору шаблона
        вместо главного меню.
        """
        place_id = _seed_place(h)
        _seed_user(h, VIEWER_TG)
        await _open_public_card(h, place_id, tg=VIEWER_TG)
        token = _token_from_link(_public_link(h))

        await h.text(f"/start {token}", user_id=VIEWER_TG)
        self.assertRegex(h.screen_text(), cat.question_re(1))

        # Рестарт службы: состояние диалога и user_data потеряны, resume-снимок
        # и черновик остаются в БД.
        await h.restart()
        # Первый текст после рестарта продолжает шаг (resume), а не уходит в
        # подсказку главного меню.
        await h.text("Мой концерт", user_id=VIEWER_TG)
        self.assertIsNotNone(h.dialog_state(user_id=VIEWER_TG, name=CONVERSATION_NAME))

        # «◀️ Назад» до первого вопроса: сначала предыдущий шаг, затем выход.
        backs = _callbacks(h, "aw_qback:")
        self.assertTrue(backs, f"нет кнопки «Назад»: {h.screen_buttons()}")
        await h.callback(backs[0], user_id=VIEWER_TG)
        backs = _callbacks(h, "aw_qback:")
        self.assertTrue(backs, f"нет кнопки «Назад»: {h.screen_buttons()}")
        await h.callback(backs[0], user_id=VIEWER_TG)

        # Исходного списка у пришедшего по ссылке нет — ведёт главное меню.
        cat.assert_shows(self, h.screen_text(), "menu.main")
        self.assertIn("pick_place", _callbacks(h))
        # Черновик с введённым значением не потерян.
        drafts = _drafts(h, VIEWER_TG)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].place_id, place_id)
        self.assertEqual(_callbacks(h, "plm_"), [])
        self.assertEqual(_callbacks(h, "adm_"), [])
