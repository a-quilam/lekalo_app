# tests/test_poster2_r2_owner_tester.py
"""
R2 — независимая проверка требований владельца (poster-tester).

Ничего не импортируется из «ожидаемых» констант продукта: эталон анонса и
имя стартовой копии записаны здесь ЛИТЕРАЛЬНО. Проверки идут через настоящее
приложение `bot.build_application`, FakeBot и временную SQLite
(`tests.integration_harness`). Реальная `post_bot.db`, `.env`, сеть и Telegram
не используются.

Покрыто:
1. BUILTIN v5: жирный заголовок, пустая строка, жирное «дата, время»,
   жирная КЛИКАБЕЛЬНАЯ площадка, нежирный адрес строкой ниже, пустая строка,
   описание, пустая строка, контакт — побайтово в отправленном сообщении;
   пропуски сохраняют разделители; пустой шаблон → «.».
2. Новый пользователь (/start) получает «Дефолтный шаблон» с текстом v5.
3. №25 «Управление → Шаблоны»: ни один путь раздела не запускает анонс;
   удаление с историей — понятный отказ без потери данных; подделки отклонены.
4. №26 «Управление → Площадки»: карточка, права трёх ролей, IDOR, создание
   места со стандартом v5 без анонса, регресс `pl_pick` → анонс, stale.
"""

from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from tests import ui_texts_expect as cat

try:
    from handlers.announcement_wizard import CONVERSATION_NAME
    from models import (
        AnnouncementDraft,
        Place,
        PlaceAdmin,
        Template,
        TemplateSnapshot,
        TemplateVersion,
        utcnow,
    )
    from place_state import load_place_session
    from place_store import add_alternative_name, create_place
    from template_state import load_template_session
    from template_store import (
        create_personal_template,
        get_personal_template,
        get_standard_template,
        list_personal_templates,
        render_template_version,
    )
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001
    INTEGRATION_IMPORT_ERROR = str(exc)


# --- Эталоны владельца, записанные буквально ---------------------------------

# Эталон владельца в редакции этапа A1 (§1.1): запятая между датой и
# временем, контакт с подписью «Мероприятие ведёт:».
OWNER_REFERENCE = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07 (пн), 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1\n"
    "\n"
    "Большой зал, вход свободный.\n"
    "\n"
    "Мероприятие ведёт: @contact"
)

# Эталон v5 (этап A1): простой и расширенный тексты стандарта. Записаны
# литералами — продукт сверяется с ними побайтово.
V5_CONTENT = (
    "**[название]**\n"
    "\n"
    "**[дата], [время_начала]**\n"
    "**[место]([ссылка_места])**\n"
    "[адрес]\n"
    "\n"
    "[описание]\n"
    "\n"
    "Мероприятие ведёт: [контакт]"
)
V5_EXTENDED_CONTENT = (
    "**[название]**\n"
    "[условие_входа]\n"
    "\n"
    "**[дата], [время_начала]–[время_окончания]**\n"
    "**[место]([ссылка_места])**\n"
    "[город], [адрес]\n"
    "\n"
    "[описание]\n"
    "\n"
    "Мероприятие ведёт: [контакт]\n"
    "Куратор от площадки: [куратор]\n"
    "\n"
    "[подвал]"
)

NEW_INITIAL_NAME = "Дефолтный шаблон"

OTHER_TG = 8_930_002
SUPERADMIN_TG = 8_930_003
AW_QUESTIONS_PHASE = 2
PICK_CALLBACK = re.compile(r"^(tp|pl)_pick:\d+$")


# --- Помощники ---------------------------------------------------------------


def _alert(h) -> str:
    answers = h.answers()
    if not answers:
        return ""
    record = answers[-1]
    args = record.get("args") or ()
    return str(args[0]) if args else str(record.get("text") or "")


def _callbacks(h) -> list[str]:
    return [data for _label, data in h.screen_buttons() if data]


def _drafts(h) -> int:
    with h.session_factory() as session:
        return int(session.scalar(select(func.count(AnnouncementDraft.id))) or 0)


def _seed_place(
    h,
    *,
    name: str = "Дом культуры",
    actor_tg: int = DEFAULT_USER_ID,
    admin: bool = False,
    address: str | None = "ул. Тестовая, 1",
    url: str | None = "https://example.com/place",
) -> int:
    with h.session_factory() as session:
        user = get_or_create_user(session, actor_tg, username=f"owner-{actor_tg}")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
            address=address,
            url=url,
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


def _personal_template_id(h, name: str = "Личный") -> int:
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="owner")
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name=name,
            content="\n".join(p for p in ("", "[описание]", "") if p),
        )
        session.commit()
        return int(template.template_id)


def _values(session) -> dict[int, str]:
    from brick_store import get_system_brick

    raw = {
        "название": "Летний концерт",
        "дата": "12.07.2027",
        "время_начала": "19:00",
        "место": "Дом культуры",
        "ссылка_места": "https://example.com/place",
        "адрес": "ул. Ленина, 1",
        "описание": "Большой зал, вход свободный.",
        "контакт": "@contact",
    }
    values: dict[int, str] = {}
    for code, value in raw.items():
        brick = get_system_brick(session, code)
        assert brick is not None, f"нет системного кирпичика {code}"
        values[int(brick.id)] = value
    return values


# ============================================================================
# 1. BUILTIN v3 — побайтово в реально отправленном сообщении
# ============================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveredReferenceTests(unittest.TestCase):
    """Анонс по стандарту места уходит в чат ровно эталоном владельца."""

    @scenario_test
    async def test_place_announcement_post_is_byte_exact(self, h):
        place_id = _seed_place(h, name="Дом культуры", address="ул. Ленина, 1")
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback(f"pl_pick:{place_id}")
        # Вопросы: название → дата → время → описание → контакт.
        for answer in (
            "Летний концерт",
            "12.07.2027",
            "19:00",
            "Большой зал, вход свободный.",
            "@contact",
        ):
            await h.text(answer)
        no_photo = [data for data in _callbacks(h) if data.startswith("aw_photo_none:")]
        self.assertTrue(no_photo, f"нет кнопки «Без фотографий»: {h.screen_buttons()}")
        await h.callback(no_photo[0])

        posts = h.posts()
        self.assertTrue(posts, "анонс не отправлен")
        delivered = posts[-1]
        self.assertEqual(delivered, OWNER_REFERENCE)
        # Структура по строкам: жирность и пустые строки на своих местах.
        lines = (delivered or "").split("\n")
        # v4: между заголовком и «дата // время» ровно одна пустая строка.
        self.assertEqual(len(lines), 9, f"строки: {lines!r}")
        for index in (0, 2, 3):
            self.assertTrue(
                lines[index].startswith("<b>"), f"строка {index}: {lines[index]!r}"
            )
            self.assertTrue(
                lines[index].endswith("</b>"), f"строка {index}: {lines[index]!r}"
            )
        self.assertEqual(
            lines[3], '<b><a href="https://example.com/place">Дом культуры</a></b>'
        )
        self.assertEqual(lines[4], "ул. Ленина, 1")
        self.assertNotIn("<b>", lines[4], "адрес не должен быть жирным")
        for index in (1, 5, 7):
            self.assertEqual(lines[index], "", f"строка {index} должна быть пустой")
        self.assertNotIn("**", delivered)
        self.assertNotIn("[", delivered)
        self.assertNotIn("]", delivered)

    @scenario_test
    async def test_skipped_bricks_keep_literal_separator_no_markup(self, h):
        """Пропуск всех значений не стирает `//` и не оставляет сырой разметки."""
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback(f"pl_pick:{place_id}")
        for _step in range(12):
            skips = [data for data in _callbacks(h) if data.startswith("aw_qskip:")]
            nones = [
                data for data in _callbacks(h) if data.startswith("aw_photo_none:")
            ]
            if skips:
                await h.callback(skips[0])
            elif nones:
                await h.callback(nones[0])
            else:
                break
        delivered = h.posts()[-1] or ""
        self.assertIn("//", delivered, "разделитель обязан сохраниться буквально")
        self.assertNotIn("**", delivered)
        self.assertNotIn("[", delivered)
        self.assertNotIn("]", delivered)

    @scenario_test
    async def test_empty_custom_template_gives_single_dot(self, h):
        """Пустой шаблон без литералов → ровно «.» (не пустое сообщение)."""
        from announcement_store import (
            build_announcement_preview,
            start_announcement_wizard,
        )
        from brick_store import get_system_brick

        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="owner")
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Только текст",
                content="\n".join(p for p in ("", "[контакт]", "") if p),
            )
            session.commit()
            draft = start_announcement_wizard(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                origin="personal",
                template_id=template.template_id,
            )
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.draft_id,
            )
            self.assertEqual(preview.html, ".")
            contact = get_system_brick(session, "контакт")
            self.assertIsNotNone(contact)


# ============================================================================
# 3. Новый пользователь
# ============================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NewUserDefaultsTests(unittest.TestCase):
    @scenario_test
    async def test_start_creates_default_template_with_v5_content(self, h):
        await h.text("/start")
        with h.session_factory() as session:
            rows = list_personal_templates(session, DEFAULT_USER_ID)
            self.assertEqual(len(rows), 1)
            template = rows[0]
            self.assertEqual(template.name, NEW_INITIAL_NAME)
            version = template.current_version
            # Этап A1: стартовая копия получает оба текста эталона v5.
            self.assertEqual(version.content, V5_CONTENT)
            self.assertEqual(version.extended_content, V5_EXTENDED_CONTENT)
            rendered = render_template_version(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                template_id=template.template_id,
                values_by_id=_values(session),
            )
        self.assertEqual(rendered, OWNER_REFERENCE)
        # Повторный /start не выдаёт вторую копию.
        await h.text("/start")
        with h.session_factory() as session:
            self.assertEqual(len(list_personal_templates(session, DEFAULT_USER_ID)), 1)


# ============================================================================
# 4. №25 «Управление → Шаблоны» — только управление
# ============================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TemplatesSectionNeverStartsAnnouncementTests(unittest.TestCase):
    @scenario_test
    async def test_no_path_from_section_starts_announcement(self, h):
        """Ни один экран/кнопка раздела не даёт старта анонса и не создаёт черновик."""
        template_id = _personal_template_id(h, name="Управляемый")
        await h.text("/start")
        # Сохранённая фаза выбора из соседнего входа не должна возобновиться.
        await h.callback("pick_template")
        self.assertIn(f"tp_pick:{template_id}", _callbacks(h))

        await h.callback("manage_templates")
        self.assertEqual(
            (load_template_session(DEFAULT_USER_ID) or {}).get("phase"), "hub"
        )
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        await h.callback(f"tp_open:{template_id}")

        seen: set[str] = set()
        for _round in range(12):
            current = _callbacks(h)
            pending = [
                data
                for data in current
                if data.startswith("tp_")
                and data not in seen
                and not data.startswith(("tp_delete_yes", "tp_reset_yes"))
            ]
            if not pending:
                break
            data = pending[0]
            seen.add(data)
            await h.callback(data)
            self.assertIsNone(
                h.dialog_state(name=CONVERSATION_NAME),
                f"кнопка {data} открыла конструктор анонса",
            )
            self.assertEqual(_drafts(h), 0, f"кнопка {data} создала черновик")
            self.assertFalse(
                any(PICK_CALLBACK.match(item) for item in _callbacks(h)),
                f"после {data} появился старт анонса: {_callbacks(h)}",
            )
            if data.startswith(("tp_reset", "tp_delete")):
                # Подтверждения не нажимаем: возвращаемся в карточку.
                await h.callback(f"tp_open:{template_id}")
        self.assertGreater(len(seen), 4, "карточка проверена не полностью")
        self.assertEqual(_drafts(h), 0)

    @scenario_test
    async def test_delete_with_history_is_refused_without_data_loss(self, h):
        await h.text("/start")
        with h.session_factory() as session:
            template = list_personal_templates(session, DEFAULT_USER_ID)[0]
            template_id = int(template.template_id)
            snapshots_before = int(
                session.scalar(
                    select(func.count(TemplateSnapshot.id)).where(
                        TemplateSnapshot.source_template_id == template_id
                    )
                )
                or 0
            )
            versions_before = int(
                session.scalar(
                    select(func.count(TemplateVersion.id)).where(
                        TemplateVersion.template_id == template_id
                    )
                )
                or 0
            )
        self.assertGreaterEqual(snapshots_before, 1, "у стартовой копии есть снимок")

        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_delete:{template_id}")
        await h.callback(f"tp_delete_yes:{template_id}")

        cat.assert_shows(
            self,
            h.screen_text(),
            "templates.module.shablon_nelzya_udalit_nemu",
            "нужен понятный отказ",
        )
        with h.session_factory() as session:
            self.assertIsNotNone(
                get_personal_template(session, DEFAULT_USER_ID, template_id)
            )
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(TemplateSnapshot.id)).where(
                            TemplateSnapshot.source_template_id == template_id
                        )
                    )
                    or 0
                ),
                snapshots_before,
            )
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(TemplateVersion.id)).where(
                            TemplateVersion.template_id == template_id
                        )
                    )
                    or 0
                ),
                versions_before,
            )
            self.assertEqual(
                int(session.scalar(select(func.count(AnnouncementDraft.id))) or 0), 0
            )
        state = load_template_session(DEFAULT_USER_ID) or {}
        self.assertEqual(state.get("phase"), "card", "пользователь остаётся в карточке")
        self.assertIn(f"tp_delete:{template_id}", _callbacks(h))

    @scenario_test
    async def test_forged_callbacks_are_rejected(self, h):
        place_id = _seed_place(h, name="Стандарт", admin=True)
        personal_id = _personal_template_id(h, name="Личный")
        with h.session_factory() as session:
            standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
            assert standard is not None
            standard_id = int(standard.template_id)
            versions_before = int(
                session.scalar(
                    select(func.count(TemplateVersion.id)).where(
                        TemplateVersion.template_id == standard_id
                    )
                )
                or 0
            )

        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{personal_id}")
        # Подделка: удаление стандарта из карточки личного шаблона.
        await h.callback(f"tp_delete:{standard_id}")
        cat.assert_stale(self, _alert(h))
        await h.callback(f"tp_delete_yes:{personal_id}")
        cat.assert_stale(self, _alert(h))
        with h.session_factory() as session:
            self.assertIsNotNone(
                get_personal_template(session, DEFAULT_USER_ID, personal_id)
            )
            standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
            self.assertIsNotNone(standard)
            assert standard is not None
            self.assertEqual(standard.template_id, standard_id)
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(TemplateVersion.id)).where(
                            TemplateVersion.template_id == standard_id
                        )
                    )
                    or 0
                ),
                versions_before,
                "стандарт места не переписан",
            )


# ============================================================================
# 5. №26 «Управление → Площадки» — справочник
# ============================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceHandbookOwnerTests(unittest.TestCase):
    @scenario_test
    async def test_card_shows_name_aliases_address_and_link(self, h):
        place_id = _seed_place(
            h,
            name="Дом культуры",
            address="ул. Ленина, 1",
            url="https://example.com/place",
        )
        with h.session_factory() as session:
            user = get_or_create_user(session, DEFAULT_USER_ID, username="owner")
            add_alternative_name(session, place_id, "ДК", created_by_user_id=user.id)
            session.commit()

        await h.text("/start")
        await h.callback("manage_places")
        text = h.screen_text()
        self.assertIn("Дом культуры", text)
        self.assertFalse(
            any(PICK_CALLBACK.match(item) for item in _callbacks(h)),
            "справочник не должен предлагать старт анонса",
        )
        self.assertEqual(_drafts(h), 0)

        await h.callback(f"plm_open:{place_id}")
        card = h.screen_text()
        self.assertIn("Дом культуры", card)
        # Не-управляющий видит просмотровую карточку справочника: она
        # по-прежнему показывает число альтернативных названий.
        self.assertIn("Альтернативные названия: 1", card)
        self.assertIn("ул. Ленина, 1", card)
        self.assertIn("https://example.com/place", card)

    @scenario_test
    async def test_three_roles_actions(self, h):
        own = _seed_place(h, name="Своё", admin=True)
        foreign = _seed_place(h, name="Чужое", actor_tg=OTHER_TG)

        # Админ места: T2 — сразу полная карточка кабинета без выдачи прав
        # на чужие места.
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{own}")
        buttons = _callbacks(h)
        self.assertIn(f"adm_edit_place:{own}", buttons)
        self.assertIn(f"tp_std:{own}", buttons)
        self.assertIn(f"adm_invite:{own}", buttons)
        self.assertNotIn(f"adm_assign:{own}", buttons, "не суперадмин")
        self.assertNotIn(f"plm_edit:{own}", buttons, "старой кнопки нет")
        self.assertNotIn(f"plm_invite:{own}", buttons, "старой кнопки нет")
        self.assertNotIn(f"plm_admin_request:{own}", buttons)

        # Не-админ: только просмотр и «Я администратор места».
        await h.callback("adm_places")
        await h.callback(f"plm_open:{foreign}")
        buttons = _callbacks(h)
        self.assertNotIn(f"adm_edit_place:{foreign}", buttons)
        self.assertNotIn(f"adm_assign:{foreign}", buttons)
        self.assertNotIn(f"adm_invite:{foreign}", buttons)
        self.assertNotIn(f"plm_edit:{foreign}", buttons)
        self.assertNotIn(f"plm_admins:{foreign}", buttons)
        self.assertIn(f"plm_admin_request:{foreign}", buttons)
        await h.callback(f"plm_admin_request:{foreign}")
        cat.assert_shows(
            self, h.screen_text(), "places.admin_request.ya_admin_mesta_naznachit"
        )
        with h.session_factory() as session:
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(PlaceAdmin.id)).where(
                            PlaceAdmin.place_id == foreign
                        )
                    )
                    or 0
                ),
                0,
                "кнопка заявки не выдаёт права",
            )

        # Суперадмин: правка и администраторы любого места.
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
            await h.text("/start", user_id=SUPERADMIN_TG)
            await h.callback("manage_places", user_id=SUPERADMIN_TG)
            await h.callback(f"plm_open:{foreign}", user_id=SUPERADMIN_TG)
            buttons = _callbacks(h)
            self.assertIn(f"adm_edit_place:{foreign}", buttons)
            self.assertIn(f"adm_assign:{foreign}", buttons)
            self.assertIn(f"adm_invite:{foreign}", buttons)
            self.assertNotIn(f"plm_edit:{foreign}", buttons)
            self.assertNotIn(f"plm_admins:{foreign}", buttons)
            self.assertNotIn(f"plm_admin_request:{foreign}", buttons)

    @scenario_test
    async def test_idor_callbacks_are_denied(self, h):
        foreign = _seed_place(h, name="Чужое", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{foreign}")
        # Карточка открыта тем же id: проверяется именно роль, а не совпадение.
        await h.callback(f"plm_edit:{foreign}")
        cat.assert_stale(self, _alert(h))
        await h.callback(f"plm_admins:{foreign}")
        cat.assert_stale(self, _alert(h))
        from admin_state import load_admin_session

        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))
        state = load_place_session(DEFAULT_USER_ID) or {}
        self.assertEqual(state.get("phase"), "manage_card")
        with h.session_factory() as session:
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(Template.id)).where(
                            Template.place_id == foreign, Template.kind == "standard"
                        )
                    )
                    or 0
                ),
                1,
                "стандарт чужого места не тронут",
            )

    @scenario_test
    async def test_stale_callbacks_on_new_phases_change_nothing(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")
        before = load_place_session(DEFAULT_USER_ID)
        # Пагинация списка и выдачи неприменима в фазе карточки.
        await h.callback("plm_page:8")
        cat.assert_stale(self, _alert(h))
        await h.callback("plm_spage:8")
        cat.assert_stale(self, _alert(h))
        # Stale-кнопки не меняют состояние: всё ещё карточка того же места.
        after_stale = load_place_session(DEFAULT_USER_ID) or {}
        self.assertEqual(after_stale, before or {}, "состояние не сброшено")
        self.assertEqual(after_stale.get("phase"), "manage_card")
        self.assertEqual(after_stale.get("place_id"), place_id)

        # `plm_back` в карточке — легитимная навигация, а не stale:
        # возвращает в список справочника и не запускает анонс.
        await h.callback("plm_back")
        back_state = load_place_session(DEFAULT_USER_ID) or {}
        self.assertEqual(
            back_state.get("phase"), "manage_list", "plm_back ведёт в список"
        )
        self.assertNotIn("place_id", back_state)
        self.assertIn("Площадки", h.screen_text())
        self.assertEqual(_drafts(h), 0)

    @scenario_test
    async def test_created_place_gets_v5_standard_without_announcement(self, h):
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback("plm_add")
        cat.assert_shows(self, h.screen_text(), "places.create_input.manage_hint")
        await h.text("Новая площадка R2")
        await h.callback("pl_create_confirm")
        await h.text("Москва")
        # Poster 2.5: адрес и ссылка — необязательные шаги мастера.
        await h.callback("pl_create_skip")
        await h.callback("pl_create_skip")

        cat.assert_shows(
            self,
            h.screen_text(),
            "places.create_place_from_state.mesto_dobavleno_vmeste_standart",
        )
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertEqual(_drafts(h), 0)
        with h.session_factory() as session:
            place = session.scalar(select(Place).order_by(Place.id.desc()))
            assert place is not None
            standard = get_standard_template(session, DEFAULT_USER_ID, int(place.id))
            self.assertIsNotNone(standard)
            assert standard is not None
            version = standard.current_version
            # Этап A1: одна площадка — один шаблон с двумя текстами; флаг
            # расширенного варианта у новой площадки выключен (§3).
            self.assertEqual(version.content, V5_CONTENT)
            self.assertEqual(version.extended_content, V5_EXTENDED_CONTENT)
            self.assertFalse(standard.extended_active)
            rendered = render_template_version(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                template_id=standard.template_id,
                values_by_id=_values(session),
            )
        self.assertEqual(rendered, OWNER_REFERENCE)
        state = load_place_session(DEFAULT_USER_ID) or {}
        self.assertEqual(state.get("phase"), "manage_card")

    @scenario_test
    async def test_pick_place_tap_still_starts_announcement(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("pick_place")
        self.assertIn(f"pl_pick:{place_id}", _callbacks(h))
        await h.callback(f"pl_pick:{place_id}")
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_QUESTIONS_PHASE)
        self.assertRegex(h.screen_text(), cat.question_re(1))
        self.assertEqual(_drafts(h), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
