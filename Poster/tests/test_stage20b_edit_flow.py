# tests/test_stage20b_edit_flow.py
"""
Stage 20B: интеграционные сценарии редактирования данных места.

Все сценарии идут через настоящий Application.process_update, FakeBot и
временную SQLite. Проверяются:
- меню редактирования: суперадмин и админ этого места;
- изменение канонического названия (старое название остаётся альтернативой);
- установка и удаление адреса и ссылки с подтверждением;
- добавление/переименование/удаление альтернативных названий;
- отказ посторонним (обычный пользователь, админ чужого места);
- очистка ввода после подтверждения и устаревшие кнопки;
- гонка подтверждения: значение изменил другой администратор.
"""

from __future__ import annotations

import unittest
from datetime import datetime as dt
from unittest.mock import patch

from sqlalchemy import select

from models import Place, PlaceAdmin, PlaceEditLog, PlaceName, User
from tests import ui_texts_expect as cat

try:
    from place_store import normalize_place_name
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )

    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_ERROR = str(e)


SUPERADMIN_TG = 710100
PLACE_ADMIN_TG = 710200
OTHER_ADMIN_TG = 710210
ORDINARY_TG = 710300

DENIED_ALERT = "У вас нет доступа к разделу администрирования."
STALE_ALERT = cat.text("errors.stale.admin")


def insert_place(
    h,
    name: str,
    *,
    address: str | None = None,
    url: str | None = None,
) -> int:
    """Вставляет место с каноническим названием напрямую в базу."""
    session = h.session_factory()
    try:
        place = Place(address=address, url=url)
        session.add(place)
        session.flush()
        session.add(
            PlaceName(
                place_id=place.id,
                name=name,
                name_search=normalize_place_name(name),
                is_canonical=1,
            )
        )
        session.commit()
        return int(place.id)
    finally:
        session.close()


def ensure_user(
    h,
    telegram_user_id: int,
    *,
    username: str | None = None,
    full_name: str | None = None,
):
    """Создаёт известного пользователя напрямую (без Telegram-обновления)."""
    from user_store import get_or_create_user

    session = h.session_factory()
    try:
        user = get_or_create_user(
            session,
            telegram_user_id,
            username=username,
            full_name=full_name,
        )
        return user.telegram_user_id
    finally:
        session.close()


def assign_admin(h, place_id: int, telegram_user_id: int) -> None:
    """Назначает администратора места напрямую в базу."""
    session = h.session_factory()
    try:
        user = session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        session.add(
            PlaceAdmin(place_id=place_id, user_id=user.id, assigned_at=dt.now()),
        )
        session.commit()
    finally:
        session.close()


def canonical_name(h, place_id: int) -> str:
    session = h.session_factory()
    try:
        row = session.scalar(
            select(PlaceName).where(
                PlaceName.place_id == place_id,
                PlaceName.is_canonical == 1,
            )
        )
        return str(row.name)
    finally:
        session.close()


def alias_names(h, place_id: int) -> list[str]:
    session = h.session_factory()
    try:
        rows = list(
            session.scalars(
                select(PlaceName)
                .where(
                    PlaceName.place_id == place_id,
                    PlaceName.is_canonical == 0,
                )
                .order_by(PlaceName.id)
            ).all()
        )
        return [str(row.name) for row in rows]
    finally:
        session.close()


def place_address(h, place_id: int) -> str | None:
    session = h.session_factory()
    try:
        return session.scalar(select(Place.address).where(Place.id == place_id))
    finally:
        session.close()


def place_url(h, place_id: int) -> str | None:
    session = h.session_factory()
    try:
        return session.scalar(select(Place.url).where(Place.id == place_id))
    finally:
        session.close()


def audit_rows(h, place_id: int) -> list[PlaceEditLog]:
    session = h.session_factory()
    try:
        return list(
            session.scalars(
                select(PlaceEditLog)
                .where(PlaceEditLog.place_id == place_id)
                .order_by(PlaceEditLog.id)
            ).all()
        )
    finally:
        session.close()


def screen_callback(h, prefix: str) -> str:
    matches = [
        data for _, data in h.screen_buttons() if data and data.startswith(prefix)
    ]
    if not matches:
        raise AssertionError(
            f"нет кнопки '{prefix}' на экране: {h.screen_buttons()!r}\n{h.screen_text()}"
        )
    return matches[0]


def last_alert(h) -> str | None:
    answers = [c for c in h.calls if c["type"] == "callback_answer"]
    if not answers:
        return None
    record = answers[-1]
    args = record.get("args") or ()
    if args:
        return str(args[0])
    return record.get("text")


def patched_superadmin(*extra: int):
    """Роль суперадмина на время сценария (и для основного пользователя)."""
    return patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG, *extra))


async def open_edit_menu(h, place_id: int) -> None:
    """Кабинет → карточка места → меню редактирования."""
    await h.text("/admin")
    await h.callback(screen_callback(h, "adm_place:"))
    await h.callback(screen_callback(h, "adm_edit_place:"))
    assert cat.shows(
        h.screen_text(), "admin.build.redaktirovanie_mesta_nazvanie"
    ), h.screen_text()


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class EditMenuAccessTests(unittest.TestCase):
    """Меню редактирования: кому доступно."""

    @scenario_test
    async def test_superadmin_sees_edit_menu(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»")
            await open_edit_menu(h, place_id)
            text = h.screen_text()
            cat.assert_shows(self, text, "admin.build.redaktirovanie_mesta_nazvanie")
            self.assertIn("Кофейня «Локал»", text)
            self.assertIn("Не указан", text)
            labels = [label for label, _ in h.screen_buttons()]
            self.assertIn("🏷 Изменить название", labels)
            self.assertIn("📝 Альтернативные названия", labels)
            # H3-C: правка и удаление поля — рядом, одной парой.
            self.assertIn("✏️ Адрес", labels)
            self.assertIn("✏️ Ссылка", labels)
            # адреса и ссылки нет — корзин быть не должно
            self.assertNotIn("🗑", labels)

    @scenario_test
    async def test_place_admin_sees_edit_menu_for_own_place(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        ensure_user(h, h.user_id, username="placeadmin", full_name="Местный Админ")
        assign_admin(h, place_id, h.user_id)

        await h.text("/admin")
        await h.callback(screen_callback(h, "adm_place:"))
        await h.callback(screen_callback(h, "adm_edit_place:"))
        cat.assert_shows(
            self, h.screen_text(), "admin.build.redaktirovanie_mesta_nazvanie"
        )
        labels = [label for label, _ in h.screen_buttons()]
        self.assertIn("🏷 Изменить название", labels)

    @scenario_test
    async def test_ordinary_user_cannot_open_edit_menu(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")

        # Обычный пользователь не видит карточки в кабинете и не может
        # вызвать редактирование: сессии кабинета нет — кнопка устарела.
        await h.text("/admin")
        cat.assert_shows(self, h.screen_text(), "admin.module.razdel_dostupen_tolko")
        await h.callback(f"adm_edit_place:{place_id}")
        self.assertEqual(last_alert(h), STALE_ALERT)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class CanonicalRenameTests(unittest.TestCase):
    """Изменение канонического названия через UI."""

    @scenario_test
    async def test_rename_canonical_keeps_old_as_alias(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»")
            await open_edit_menu(h, place_id)

            await h.callback(screen_callback(h, "adm_edit_name:"))
            cat.assert_shows(
                self, h.screen_text(), "admin.edit_input.izmenenie_nazvaniya_tekuschee"
            )
            await h.text("Кафе Локал")
            self.assertIn("Изменить название?", h.screen_text())
            self.assertIn("Кафе Локал", h.screen_text())
            self.assertIn("Кофейня «Локал»", h.screen_text())

            await h.callback("adm_edit_confirm")
            cat.assert_shows(
                self,
                h.screen_text(),
                "admin.edit_result_notice.nazvanie_mesta_izmeneno",
            )
            self.assertEqual(canonical_name(h, place_id), "Кафе Локал")
            self.assertEqual(alias_names(h, place_id), ["Кофейня «Локал»"])

            rows = audit_rows(h, place_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].action, "rename_canonical")
            self.assertEqual(rows[0].old_value, "Кофейня «Локал»")
            self.assertEqual(rows[0].new_value, "Кафе Локал")

            # фаза редактирования закрыта: карточка места показывает новое имя
            await h.text("/admin")
            self.assertIn("Кафе Локал", h.screen_text())

    @scenario_test
    async def test_rename_cancel_returns_to_place_card(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»")
            await open_edit_menu(h, place_id)

            await h.callback(screen_callback(h, "adm_edit_name:"))
            await h.text("Новое название")
            self.assertIn("Изменить название?", h.screen_text())

            await h.callback("adm_cancel")
            self.assertEqual(canonical_name(h, place_id), "Кофейня «Локал»")
            self.assertEqual(alias_names(h, place_id), [])

    @scenario_test
    async def test_rename_race_shows_changed_notice(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»")
            await open_edit_menu(h, place_id)

            await h.callback(screen_callback(h, "adm_edit_name:"))
            await h.text("Кафе Локал")
            self.assertIn("Изменить название?", h.screen_text())

            # второй администратор успел изменить название первым
            session = h.session_factory()
            try:
                canonical = session.scalar(
                    select(PlaceName).where(
                        PlaceName.place_id == place_id,
                        PlaceName.is_canonical == 1,
                    )
                )
                canonical.name = "Кофейня «Локал» 2"
                canonical.name_search = normalize_place_name("Кофейня «Локал» 2")
                session.commit()
            finally:
                session.close()

            await h.callback("adm_edit_confirm")
            cat.assert_shows(
                self,
                h.screen_text(),
                "admin.edit_result_notice.znachenie_izmenilos_vremya",
            )
            self.assertEqual(canonical_name(h, place_id), "Кофейня «Локал» 2")


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class AddressUrlEditTests(unittest.TestCase):
    """Установка, изменение и удаление адреса и ссылки."""

    @scenario_test
    async def test_set_address_flow(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»")
            await open_edit_menu(h, place_id)

            await h.callback(screen_callback(h, "adm_edit_address:"))
            cat.assert_shows(
                self, h.screen_text(), "admin.build.izmenenie_adresa_tekuschiy_adres"
            )
            await h.text("  ул. Ленина,   5  ")
            self.assertIn("Сохранить новый адрес?", h.screen_text())
            self.assertIn("ул. Ленина, 5", h.screen_text())

            await h.callback("adm_edit_confirm")
            cat.assert_shows(
                self, h.screen_text(), "admin.edit_result_notice.adres_mesta_obnovlen"
            )
            self.assertEqual(place_address(h, place_id), "ул. Ленина, 5")
            rows = audit_rows(h, place_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].action, "address_set")

    @scenario_test
    async def test_set_url_flow(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»")
            await open_edit_menu(h, place_id)

            await h.callback(screen_callback(h, "adm_edit_url:"))
            cat.assert_shows(
                self, h.screen_text(), "admin.build.izmenenie_ssylki_tekuschaya_ssylka"
            )
            await h.text(" t.me/poster ")
            self.assertIn("Сохранить новую ссылку?", h.screen_text())
            self.assertIn("https://t.me/poster", h.screen_text())

            await h.callback("adm_edit_confirm")
            cat.assert_shows(
                self, h.screen_text(), "admin.edit_result_notice.ssylka_mesta_obnovlena"
            )
            self.assertEqual(place_url(h, place_id), "https://t.me/poster")

    @scenario_test
    async def test_delete_address_flow(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»", address="ул. Старая, 9")
            await open_edit_menu(h, place_id)
            labels = [label for label, _ in h.screen_buttons()]
            self.assertIn("🗑", labels)

            await h.callback(screen_callback(h, "adm_edit_address_delete:"))
            cat.assert_shows(
                self, h.screen_text(), "admin.build.podtverdite_udalenie_adresa"
            )
            await h.callback("adm_edit_confirm")
            cat.assert_shows(
                self, h.screen_text(), "admin.edit_result_notice.adres_mesta_udalen"
            )
            self.assertIsNone(place_address(h, place_id))

            # корзина исчезла, адрес показывает «Не указан»
            labels = [label for label, _ in h.screen_buttons()]
            self.assertNotIn("🗑", labels)
            self.assertIn("Не указан", h.screen_text())

    @scenario_test
    async def test_delete_url_flow(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»", url="https://t.me/poster")
            await open_edit_menu(h, place_id)
            labels = [label for label, _ in h.screen_buttons()]
            self.assertIn("🗑", labels)

            await h.callback(screen_callback(h, "adm_edit_url_delete:"))
            cat.assert_shows(
                self, h.screen_text(), "admin.build.podtverdite_udalenie_ssylki"
            )
            await h.callback("adm_edit_confirm")
            cat.assert_shows(
                self, h.screen_text(), "admin.edit_result_notice.ssylka_mesta_udalena"
            )
            self.assertIsNone(place_url(h, place_id))

            labels = [label for label, _ in h.screen_buttons()]
            self.assertNotIn("🗑", labels)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class AliasFlowTests(unittest.TestCase):
    """Альтернативные названия: добавление, переименование, удаление."""

    @scenario_test
    async def test_add_alias_flow(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»")
            await open_edit_menu(h, place_id)

            await h.callback(screen_callback(h, "adm_aliases:"))
            cat.assert_shows(
                self,
                h.screen_text(),
                "admin.edit_aliases.alternativnye_nazvaniya_poka_net",
            )

            await h.callback(screen_callback(h, "adm_alias_add:"))
            cat.assert_shows(
                self, h.screen_text(), "admin.edit_input.dobavlenie_alternativnogo"
            )
            await h.text("Локал")
            cat.assert_shows(
                self, h.screen_text(), "admin.edit_confirm.podtverdite_dobavlenie"
            )

            await h.callback("adm_edit_confirm")
            cat.assert_shows(
                self,
                h.screen_text(),
                "admin.edit_result_notice.alternativnoe_nazvanie_dobavleno",
            )
            self.assertEqual(alias_names(h, place_id), ["Локал"])
            self.assertEqual(canonical_name(h, place_id), "Кофейня «Локал»")

    @scenario_test
    async def test_rename_and_delete_alias_flow(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»")
            session = h.session_factory()
            try:
                session.add(
                    PlaceName(
                        place_id=place_id,
                        name="Локал",
                        name_search=normalize_place_name("Локал"),
                        is_canonical=0,
                    )
                )
                session.commit()
            finally:
                session.close()

            await open_edit_menu(h, place_id)
            await h.callback(screen_callback(h, "adm_aliases:"))
            self.assertIn("Локал", h.screen_text())

            # переименование
            await h.callback(screen_callback(h, "adm_alias_rename:"))
            cat.assert_shows(
                self, h.screen_text(), "admin.edit_input.pereimenovanie_alternativnogo"
            )
            await h.text("Локал Кофе")
            cat.assert_shows(
                self, h.screen_text(), "admin.edit_confirm.podtverdite_pereimenovanie"
            )
            await h.callback("adm_edit_confirm")
            cat.assert_shows(
                self, h.screen_text(), "admin.edit_result_notice.alternativnoe_nazvanie"
            )
            self.assertEqual(alias_names(h, place_id), ["Локал Кофе"])

            # удаление
            await h.callback(screen_callback(h, "adm_alias_delete:"))
            self.assertIn("Удалить альтернативное название?", h.screen_text())
            await h.callback("adm_edit_confirm")
            cat.assert_shows(
                self,
                h.screen_text(),
                "admin.edit_result_notice.alternativnoe_nazvanie_udaleno",
            )
            self.assertEqual(alias_names(h, place_id), [])

    @scenario_test
    async def test_duplicate_alias_shows_notice(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»")
            await open_edit_menu(h, place_id)

            await h.callback(screen_callback(h, "adm_aliases:"))
            await h.callback(screen_callback(h, "adm_alias_add:"))
            # Дубликат канонического названия отсекается уже на вводе:
            # экран подтверждения не показывается, значение не сохраняется.
            await h.text("Кофейня «Локал»")
            self.assertIn("уже есть у этого места", h.screen_text())
            self.assertEqual(alias_names(h, place_id), [])


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class EditSecurityTests(unittest.TestCase):
    """Безопасность потока редактирования: чужое место, устаревшие кнопки."""

    @scenario_test
    async def test_place_admin_cannot_edit_foreign_place(self, h):
        own_place = insert_place(h, "Кофейня «Локал»")
        foreign_place = insert_place(h, "Театр «Этаж»")
        ensure_user(h, h.user_id, username="placeadmin", full_name="Местный Админ")
        assign_admin(h, own_place, h.user_id)
        ensure_user(h, OTHER_ADMIN_TG, username="other", full_name="Чужой Админ")
        assign_admin(h, foreign_place, OTHER_ADMIN_TG)

        await h.text("/admin")
        await h.callback(screen_callback(h, "adm_place:"))
        await h.callback(screen_callback(h, "adm_edit_place:"))
        self.assertIn("Кофейня «Локал»", h.screen_text())

        # Подделанный callback с чужим place_id: цель берётся из сессии,
        # а не из данных callback — редактирование откроется для СВОЕГО места,
        # чужое место не затрагивается.
        await h.callback(f"adm_edit_name:{foreign_place}")
        self.assertIn("Изменить название", h.screen_text())
        self.assertIn("Кофейня «Локал»", h.screen_text())
        self.assertNotIn("Театр «Этаж»", h.screen_text())
        self.assertEqual(canonical_name(h, foreign_place), "Театр «Этаж»")

    @scenario_test
    async def test_stale_edit_confirm_callback(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = insert_place(h, "Кофейня «Локал»")
            await open_edit_menu(h, place_id)

            # подтверждение без активной фазы подтверждения: кнопка устарела
            await h.callback("adm_edit_confirm")
            self.assertEqual(last_alert(h), STALE_ALERT)
            self.assertEqual(canonical_name(h, place_id), "Кофейня «Локал»")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
