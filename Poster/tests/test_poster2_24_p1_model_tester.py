# tests/test_poster2_24_p1_model_tester.py
"""
Poster 2.4, этап P1 — независимые тесты ТРЕБУЕМОЙ модели кирпичиков
(тест-до-кода). Написаны до реализации: часть сценариев обязана падать на
текущем коде, часть фиксирует уже правильное поведение.

Контракт 2.4 (пересказ требований менеджера, продуктовый код не менялся):

S1. Один системный кирпичик адреса — id=10, код «адрес». Определения
    `адрес_места` (id=6) больше нет ни в каталоге, ни в БД (см. S11).
    Автоподстановка адреса площадки не создаёт значения «адрес_места».
S2. Предложение ЛИЧНОГО кирпичика площадке: одобрение создаёт независимую
    локальную копию; личный кирпичик остаётся у автора с тем же id и
    описанием; описания независимы.
S3. Предложение ЛИЧНОГО кирпичика всей системе: при одобрении повышается
    ИСХОДНЫЙ кирпичик (тот же id, scope=system, owner_user_id=NULL), копии
    не появляется, в личной области его больше нет; история предложения
    ссылается на тот же id.
S4. Суперадмин может изменить ТОЛЬКО описание системного кирпичика
    (название/код/тип неизменны); админ места этого делать не может.
S5. Админ площадки может изменить ТОЛЬКО описание локального кирпичика
    своей площадки; админ другой площадки — нет.
S6. Административный редактор описания (маршрут `adm_bedit:<brick_id>`,
    конвенция `adm_*` из handlers/admin.py): на карточке есть путь к правке
    описания и НЕТ кнопок правки названия/кода/типа; подделанные callback'и
    дают alert и ничего не меняют.
S7. Неиспользуемый личный кирпичик удаляется окончательно, с
    подтверждением (в подтверждении пара кнопок «🗑 Удалить | Отмена»).
S8. Используемый (привязанный к версии шаблона) кирпичик удалить нельзя:
    отказ с понятным объяснением, шаблон цел. Системные кирпичики защищены
    на уровне БД.
S9. Отклонение и отзыв предложения не меняют исходный личный кирпичик.
S10. Конфликт кода даёт статус `conflict` без изменений; повторное решение
    уже решённого предложения — `already_decided`.
S11. Миграция устраняет `адрес_места`: строка bricks id=6 отсутствует,
    системный код «адрес» один, привязки/значения на id=6 удалены,
    FK/integrity целы (и для апгрейда базы 24, и для чистой установки).

Требуемый доменный API (документируется здесь, а не выдумывается в
приватных деталях): `brick_store.update_brick_description(session, *,
actor_telegram_user_id, brick_id, description)` — зеркало
`update_personal_brick`, меняет только `description`, права проверяются по
актуальной роли, отказ — `BrickPermissionError`, чужая/невидимая строка —
`BrickNotFoundError`.

Все сценарии идут через настоящий `Application.process_update` (FakeBot,
временная SQLite) либо через публичные доменные сервисы, где UI ещё нет.
Реальная `post_bot.db`, `.env` и сеть Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_poster2_24_p1_model_tester -v
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from typing import Any, cast
from unittest.mock import patch

try:
    from sqlalchemy import func, select

    from announcement_store import (
        build_announcement_fields,
        build_announcement_preview,
        choose_announcement_template,
        confirm_announcement_place,
        start_announcement_wizard,
    )
    from announcement_wizard import place_values_by_code
    from brick_proposal_store import (
        PROPOSAL_STATUS_APPROVED,
        PROPOSAL_STATUS_PENDING,
        PROPOSAL_STATUS_REJECTED,
        PROPOSAL_STATUS_WITHDRAWN,
        approve_brick_proposal,
        reject_brick_proposal,
    )
    from brick_store import (
        get_personal_brick,
        get_system_brick,
        list_personal_bricks,
        list_place_bricks,
        list_system_bricks,
        update_personal_brick,
    )
    from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
    from models import (
        Brick,
        BrickProposal,
        PlaceAdmin,
        TemplateVersionBrick,
        utcnow,
    )
    from place_store import create_place
    from template_store import create_personal_template, get_standard_template
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from tests.stage19a_helpers import make_engine, temp_db_path
    from user_store import get_or_create_user

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина уходит в skip-сообщение
    IMPORT_ERROR = str(exc)


SUPER_TG = 2_401_001
ADMIN_TG = 2_401_002
OWNER_TG = 2_401_003
OTHER_ADMIN_TG = 2_401_004
STRANGER_TG = 2_401_005

ADDRESS_CODE = "адрес"
LEGACY_ADDRESS_CODE = "адрес_места"


# --- общие помощники (подготовка данных и чтение экрана) -------------------


def ensure_user(h: Harness, tg: int) -> int:
    with h.session_factory() as session:
        return get_or_create_user(
            session, tg, username=f"u{tg}", full_name=f"Юзер {tg}"
        ).id


def make_place(
    h: Harness,
    name: str,
    *,
    admin_tg: int | None = None,
    address: str | None = "ул. Приёмная, 7",
    url: str | None = "https://example.com/place",
) -> int:
    with h.session_factory() as session:
        admin = None
        if admin_tg is not None:
            admin = get_or_create_user(
                session, admin_tg, username=f"a{admin_tg}", full_name="Админ места"
            )
        place = create_place(
            session,
            name,
            city="Город приёмки",
            created_by_user_id=admin.id if admin is not None else None,
            address=address,
            url=url,
        )
        if admin is not None:
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=admin.id,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
        return int(place.id)


def make_personal(
    h: Harness,
    code: str,
    title: str = "Личный",
    *,
    tg: int = OWNER_TG,
    description: str | None = "Личное описание",
    value_type: str = "short_text",
):
    ensure_user(h, tg)
    from brick_store import create_personal_brick

    with h.session_factory() as session:
        return create_personal_brick(
            session,
            actor_telegram_user_id=tg,
            code=code,
            title=title,
            value_type=value_type,
            rules={},
            description=description,
        )


def make_place_brick(
    h: Harness,
    place_id: int,
    code: str,
    title: str = "Местный",
    *,
    tg: int = ADMIN_TG,
    description: str | None = None,
):
    ensure_user(h, tg)
    from brick_store import create_place_brick

    with h.session_factory() as session:
        return create_place_brick(
            session,
            actor_telegram_user_id=tg,
            place_id=place_id,
            code=code,
            title=title,
            value_type="short_text",
            rules={},
            description=description,
        )


def brick_row(h: Harness, brick_id: int) -> Brick | None:
    with h.session_factory() as session:
        return session.get(Brick, brick_id)


def brick_by_code(h: Harness, code: str) -> list[Brick]:
    with h.session_factory() as session:
        return list(session.scalars(select(Brick).where(Brick.code == code)).all())


def proposal_row(h: Harness, proposal_id: int) -> BrickProposal:
    with h.session_factory() as session:
        row = session.get(BrickProposal, proposal_id)
        assert row is not None
        return row


def proposal_count(h: Harness) -> int:
    with h.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(BrickProposal)) or 0)


def buttons(h: Harness) -> list[tuple[str, str | None]]:
    return list(h.screen_buttons())


def rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    record = h.screen()
    assert record is not None, "экрана не было"
    markup = record.get("reply_markup")
    if markup is None:
        return []
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def callbacks(h: Harness, prefix: str = "") -> list[str]:
    return [data for _label, data in buttons(h) if data and data.startswith(prefix)]


def button_with(h: Harness, *needles: str) -> tuple[str, str | None] | None:
    """Кнопка текущего экрана, в подписи которой есть одно из слов."""
    for label, data in buttons(h):
        text = (label or "").lower()
        if any(needle.lower() in text for needle in needles):
            return label, data
    return None


def require_button(h: Harness, *needles: str, hint: str = "") -> str:
    found = button_with(h, *needles)
    if found is None:
        raise AssertionError(
            f"нет кнопки {needles!r} {hint}; экран: {h.screen_text()[:400]!r}; "
            f"кнопки: {buttons(h)!r}"
        )
    assert found[1] is not None
    return found[1]


def last_alert(h: Harness) -> str | None:
    answers = h.answers()
    if not answers:
        return None
    record = answers[-1]
    args = tuple(record.get("args") or ())
    if args and isinstance(args[0], str):
        return args[0]
    return record.get("text")


def screen_and_alerts(h: Harness) -> str:
    return " | ".join([h.screen_text(), *h.answer_texts()])


def update_description(h: Harness, actor_tg: int, brick_id: int, description: str):
    """Требуемый доменный редактор описания (см. docstring модуля)."""
    try:
        from brick_store import update_brick_description  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover — до реализации API
        raise AssertionError(
            "требуется brick_store.update_brick_description(...): "
            "редактор описания по правам роли"
        ) from exc
    with h.session_factory() as session:
        return update_brick_description(
            session,
            actor_telegram_user_id=actor_tg,
            brick_id=brick_id,
            description=description,
        )


async def open_brick_hub(h: Harness, *, tg: int) -> None:
    """
    Возвращает пользователя на хаб «Мои кирпичики».

    Раздел помнит фазу прошлого действия (карточка предложения, карточка
    кирпичика). Тест-сценарий не должен зависеть от того, где остановился
    предыдущий шаг: «Назад» доводит до хаба, где есть строки кирпичиков.
    """
    await h.text("/bricks", user_id=tg)
    for _attempt in range(8):
        if callbacks(h, "bk_open:") or callbacks(h, "bk_create"):
            return
        back = callbacks(h, "bp_back") or callbacks(h, "bk_back")
        if not back:
            break
        await h.callback(back[0], user_id=tg)
    raise AssertionError(
        f"не удалось вернуться на хаб кирпичиков: {h.screen_text()[:400]!r}"
    )


async def propose_to_place(h: Harness, brick_id: int, place_id: int, *, tg: int) -> int:
    """Полный пользовательский путь предложения кирпичика площадке."""
    await open_brick_hub(h, tg=tg)
    await h.callback(f"bp_start:{brick_id}", user_id=tg)
    await h.callback("bp_target:place", user_id=tg)
    await h.callback(f"bp_place:{place_id}", user_id=tg)
    await h.callback("bp_send", user_id=tg)
    with h.session_factory() as session:
        row = session.scalar(select(BrickProposal).order_by(BrickProposal.id.desc()))
        assert row is not None
        return int(row.id)


async def propose_to_system(h: Harness, brick_id: int, *, tg: int) -> int:
    await open_brick_hub(h, tg=tg)
    await h.callback(f"bp_start:{brick_id}", user_id=tg)
    await h.callback("bp_target:system", user_id=tg)
    await h.callback("bp_send", user_id=tg)
    with h.session_factory() as session:
        row = session.scalar(select(BrickProposal).order_by(BrickProposal.id.desc()))
        assert row is not None
        return int(row.id)


async def approve_place_via_ui(
    h: Harness, place_id: int, proposal_id: int, *, tg: int = ADMIN_TG
) -> None:
    await h.text("/admin", user_id=tg)
    await h.callback(f"adm_place:{place_id}", user_id=tg)
    await h.callback(f"adm_bprop_place:{place_id}", user_id=tg)
    await h.callback(f"adm_bprop_open:{proposal_id}", user_id=tg)
    await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=tg)
    await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=tg)


async def approve_system_via_ui(
    h: Harness, proposal_id: int, *, tg: int = SUPER_TG
) -> None:
    await h.text("/admin", user_id=tg)
    await h.callback("adm_bprop_global", user_id=tg)
    await h.callback(f"adm_bprop_open:{proposal_id}", user_id=tg)
    await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=tg)
    await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=tg)


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class Poster24ModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    # ==================================================================
    # S1. Один адрес: автоподстановка только в кирпичик «адрес»
    # ==================================================================

    @scenario_test
    async def test_s1_address_autofill_uses_single_address_brick(
        self, h: Harness
    ) -> None:
        """S1: адрес из справочника живёт в коде «адрес», не «адрес_места»."""
        ensure_user(h, OWNER_TG)
        place_id = make_place(h, "Дом приёмки")

        # Каталог системных кирпичиков: адрес ровно один.
        with h.session_factory() as session:
            system = list_system_bricks(session)
        codes = [brick.code for brick in system]
        self.assertNotIn(
            LEGACY_ADDRESS_CODE,
            codes,
            "системное определение «адрес_места» должно исчезнуть из каталога",
        )
        address_codes = [code for code in codes if "адрес" in code]
        self.assertEqual(
            address_codes,
            [ADDRESS_CODE],
            f"должен остаться один адресный код: {address_codes}",
        )
        address_bricks = [brick for brick in system if brick.code == ADDRESS_CODE]
        self.assertEqual(len(address_bricks), 1)
        self.assertEqual(address_bricks[0].source, "place")

        # Доменный контракт автоподстановки: ключа «адрес_места» нет.
        values = place_values_by_code(
            display_name="Дом приёмки",
            address="ул. Приёмная, 7",
            url="https://example.com/place",
            city="Город приёмки",
        )
        self.assertEqual(values.get("адрес"), "ул. Приёмная, 7")
        self.assertNotIn(
            LEGACY_ADDRESS_CODE,
            values,
            "автоподстановка не должна заполнять устранённый «адрес_места»",
        )

        # Пользовательский путь: анонс площадки по стандартному шаблону.
        with h.session_factory() as session:
            standard = get_standard_template(session, OWNER_TG, place_id)
            draft = start_announcement_wizard(
                session,
                actor_telegram_user_id=OWNER_TG,
                place_id=place_id,
            )
            confirmed = confirm_announcement_place(
                session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                expected_revision=draft.revision,
            )
            chosen = choose_announcement_template(
                session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=confirmed.draft_id,
                template_id=standard.template_id,
                expected_revision=confirmed.revision,
            )
            fields = build_announcement_fields(
                session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=chosen.draft_id,
            )
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=chosen.draft_id,
            )

        by_code = {field.code: field for field in fields}
        self.assertIn(ADDRESS_CODE, by_code)
        self.assertEqual(by_code[ADDRESS_CODE].raw_value, "ул. Приёмная, 7")
        self.assertNotIn(
            LEGACY_ADDRESS_CODE,
            by_code,
            "в анонсе не должно быть поля «адрес_места»",
        )
        self.assertEqual(
            preview.html.count("ул. Приёмная, 7"),
            1,
            "адрес площадки должен попасть в текст ровно один раз",
        )

    # ==================================================================
    # S2. Личный → площадке: независимая локальная копия
    # ==================================================================

    @scenario_test
    async def test_s2_place_approval_keeps_personal_and_creates_local(
        self, h: Harness
    ) -> None:
        """S2: локальная копия, личный кирпичик не меняется."""
        place_id = make_place(h, "Зал копии", admin_tg=ADMIN_TG)
        personal = make_personal(
            h, "мест_копия", "Копия", description="Описание автора"
        )
        proposal_id = await propose_to_place(h, personal.id, place_id, tg=OWNER_TG)
        await approve_place_via_ui(h, place_id, proposal_id)

        with h.session_factory() as session:
            proposal = session.get(BrickProposal, proposal_id)
            assert proposal is not None
            personal_row = session.get(Brick, personal.id)
            assert personal_row is not None
            local = session.scalar(
                select(Brick).where(
                    Brick.scope == "place",
                    Brick.place_id == place_id,
                    Brick.code == "мест_копия",
                )
            )
        self.assertEqual(proposal.status, PROPOSAL_STATUS_APPROVED)
        self.assertIsNotNone(local, "у площадки должна появиться локальная копия")
        assert local is not None
        self.assertNotEqual(local.id, personal.id, "копия — отдельный id")
        self.assertEqual(personal_row.scope, "personal")
        self.assertEqual(personal_row.description, "Описание автора")
        self.assertEqual(local.description, "Описание автора", "снимок описания")
        self.assertEqual(proposal.approved_brick_id, local.id)

        # Независимость описаний: правка личного не меняет локальный.
        with h.session_factory() as session:
            update_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                brick_id=personal.id,
                expected_revision=personal.revision,
                code="мест_копия",
                title="Копия",
                rules={},
                description="Новое описание автора",
            )
        with h.session_factory() as session:
            local_after = session.get(Brick, local.id)
            personal_after = session.get(Brick, personal.id)
        assert local_after is not None and personal_after is not None
        self.assertEqual(personal_after.description, "Новое описание автора")
        self.assertEqual(
            local_after.description,
            "Описание автора",
            "правка личного кирпичика не должна менять локальную копию",
        )

    # ==================================================================
    # S3. Личный → системе: повышение ИСХОДНОГО кирпичика
    # ==================================================================

    @scenario_test
    async def test_s3_system_approval_promotes_source_brick_in_place(
        self, h: Harness
    ) -> None:
        """S3: тот же id становится системным, копии и личного дубля нет."""
        ensure_user(h, OWNER_TG)
        personal = make_personal(
            h, "глоб_приёмка", "Глобальная", description="Глобальное описание"
        )
        source_id = int(personal.id)
        source_created_by_user_id = personal.created_by_user_id
        proposal_id = await propose_to_system(h, source_id, tg=OWNER_TG)
        await approve_system_via_ui(h, proposal_id)

        # Перечитываем результат в НОВОЙ сессии (пустая identity map), чтобы
        # сравнение id не опиралось на закешированный в прежней сессии объект.
        with h.session_factory() as session:
            session.expire_all()
            proposal_row = session.get(BrickProposal, proposal_id)
            assert proposal_row is not None
            proposal_status = proposal_row.status
            approved_id = int(cast(int, proposal_row.approved_brick_id))
            proposal_source_id = int(proposal_row.source_brick_id)
            proposal_author_user_id = proposal_row.author_user_id

        with h.session_factory() as session:
            session.expire_all()
            same = session.get(Brick, source_id)
            assert same is not None
            same_code = list(
                session.scalars(select(Brick).where(Brick.code == "глоб_приёмка")).all()
            )
            personal_list = list_personal_bricks(session, OWNER_TG)
            system_def = get_system_brick(session, "глоб_приёмка")

        self.assertEqual(proposal_status, PROPOSAL_STATUS_APPROVED)
        self.assertEqual(
            approved_id,
            source_id,
            "одобрение в систему повышает исходный кирпичик, а не создаёт копию",
        )
        self.assertEqual(
            len(same_code),
            1,
            f"второго глобального кирпичика быть не должно: {[b.id for b in same_code]}",
        )
        self.assertEqual(same.id, source_id, "id исходного кирпичика сохраняется")
        self.assertEqual(same.scope, "system")
        self.assertIsNone(same.owner_user_id)
        self.assertIsNone(same.place_id)
        self.assertEqual(same.description, "Глобальное описание")
        self.assertEqual(same.title, "Глобальная")
        self.assertEqual(same.code, "глоб_приёмка")
        self.assertIsNotNone(same.created_by_user_id, "авторство не должно обнуляться")
        self.assertIn(
            same.created_by_user_id,
            {source_created_by_user_id, proposal_author_user_id},
            "created_by_user_id — прежний автор или записанный автор предложения",
        )
        self.assertNotIn(
            source_id, [brick.id for brick in personal_list], "личного дубля нет"
        )
        self.assertIsNotNone(system_def)
        assert system_def is not None
        self.assertEqual(system_def.id, source_id)
        self.assertEqual(proposal_source_id, source_id)

    # ==================================================================
    # S4. Суперадмин: только описание системного кирпичика
    # ==================================================================

    @scenario_test
    async def test_s4_superadmin_edits_only_system_description(
        self, h: Harness
    ) -> None:
        """S4: меняется описание, название/код/тип неизменны."""
        ensure_user(h, SUPER_TG)
        with h.session_factory() as session:
            system = get_system_brick(session, ADDRESS_CODE)
        assert system is not None
        before = (system.title, system.code, system.value_type)

        update_description(h, SUPER_TG, system.id, "Адрес площадки из справочника")

        with h.session_factory() as session:
            after = session.get(Brick, system.id)
        assert after is not None
        self.assertEqual(after.description, "Адрес площадки из справочника")
        self.assertEqual(
            (after.title, after.code, after.value_type),
            before,
            "административный редактор не меняет название/код/тип",
        )

        # Админ площадки не правит системный кирпичик.
        make_place(h, "Место без прав", admin_tg=ADMIN_TG)
        try:
            update_description(h, ADMIN_TG, system.id, "Чужая правка")
        except PermissionError:
            pass
        else:
            raise AssertionError(
                "админ площадки не должен менять описание системного кирпичика"
            )
        with h.session_factory() as session:
            untouched = session.get(Brick, system.id)
        assert untouched is not None
        self.assertEqual(untouched.description, "Адрес площадки из справочника")

    # ==================================================================
    # S5. Админ площадки: только описание локального кирпичика
    # ==================================================================

    @scenario_test
    async def test_s5_place_admin_edits_only_own_local_description(
        self, h: Harness
    ) -> None:
        """S5: своя площадка — да, чужая — отказ, данные целы."""
        place_id = make_place(h, "Своя площадка", admin_tg=ADMIN_TG)
        foreign_id = make_place(h, "Чужая площадка", admin_tg=OTHER_ADMIN_TG)
        local = make_place_brick(h, place_id, "мест_опис", "Местный")
        foreign = make_place_brick(
            h, foreign_id, "чуж_опис", "Чужой", tg=OTHER_ADMIN_TG
        )
        before = (local.title, local.code, local.value_type)

        update_description(h, ADMIN_TG, local.id, "Пояснение для автора анонса")

        with h.session_factory() as session:
            after = session.get(Brick, local.id)
        assert after is not None
        self.assertEqual(after.description, "Пояснение для автора анонса")
        self.assertEqual((after.title, after.code, after.value_type), before)

        try:
            update_description(h, ADMIN_TG, foreign.id, "Чужая правка")
        except PermissionError:
            pass
        else:
            raise AssertionError(
                "админ площадки не должен менять описание чужого локального кирпичика"
            )
        with h.session_factory() as session:
            foreign_after = session.get(Brick, foreign.id)
        assert foreign_after is not None
        self.assertIsNone(foreign_after.description)

    # ==================================================================
    # S6. Административный редактор описания: карточка и подделки
    # ==================================================================

    @scenario_test
    async def test_s6_admin_description_card_has_no_title_code_type_actions(
        self, h: Harness
    ) -> None:
        """S6: карточка `adm_bedit:<id>` — описание есть, смены метаданных нет."""
        place_id = make_place(h, "Место редактора", admin_tg=ADMIN_TG)
        local = make_place_brick(h, place_id, "ред_опис", "Редактируемый")
        with h.session_factory() as session:
            system = get_system_brick(session, ADDRESS_CODE)
        assert system is not None

        # Карточка системного кирпичика доступна суперадмину.
        await h.text("/admin", user_id=SUPER_TG)
        await h.callback(f"adm_bedit:{system.id}", user_id=SUPER_TG)
        self.assertIn(
            system.title,
            h.screen_text(),
            "маршрут adm_bedit:<id> должен открыть карточку кирпичика",
        )
        self.assertIsNotNone(
            button_with(h, "описан"),
            "на карточке нужна кнопка изменения описания",
        )

        # Карточка локального кирпичика доступна админу этой площадки.
        await h.text("/admin", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bedit:{local.id}", user_id=ADMIN_TG)
        self.assertIn(local.title, h.screen_text())
        self.assertIsNotNone(button_with(h, "описан"))

        # Кнопок правки названия/кода/типа нет.
        for label, data in buttons(h):
            lowered = (label or "").lower()
            for forbidden in ("изменить название", "изменить код", "изменить тип"):
                self.assertNotIn(
                    forbidden,
                    lowered,
                    f"административный редактор не меняет метаданные: {label!r}",
                )
            self.assertNotIn(
                (data or ""),
                {
                    f"adm_bedit_title:{local.id}",
                    f"adm_bedit_code:{local.id}",
                    f"adm_bedit_type:{local.id}",
                },
            )

        # Подделанные callback'и: alert, данные не меняются.
        with h.session_factory() as session:
            before = session.get(Brick, local.id)
        assert before is not None
        snapshot = (before.title, before.code, before.value_type, before.description)
        for forged in (
            f"adm_bedit_title:{local.id}",
            f"adm_bedit_code:{local.id}",
            f"adm_bedit_type:{local.id}",
        ):
            answers_before = len(h.answers())
            await h.callback(forged, user_id=ADMIN_TG)
            self.assertGreater(
                len(h.answers()),
                answers_before,
                f"подделанный callback {forged} должен дать alert",
            )
            alert = last_alert(h) or ""
            self.assertNotIn("внутренняя ошибка", alert.lower())
        with h.session_factory() as session:
            after = session.get(Brick, local.id)
        assert after is not None
        self.assertEqual(
            (after.title, after.code, after.value_type, after.description),
            snapshot,
            "подделанные callback'и не меняют метаданные",
        )

        # Обычный пользователь не открывает административную карточку.
        answers_before = len(h.answers())
        await h.callback(f"adm_bedit:{local.id}", user_id=STRANGER_TG)
        self.assertGreater(len(h.answers()), answers_before)
        self.assertTrue(last_alert(h))

    # ==================================================================
    # S7. Удаление неиспользуемого личного кирпичика
    # ==================================================================

    @scenario_test
    async def test_s7_unused_personal_brick_is_deleted_with_confirmation(
        self, h: Harness
    ) -> None:
        """S7: «🗑 Удалить | Отмена», затем окончательное удаление."""
        brick = make_personal(h, "удал_личный", "Удаляемый")
        await h.text("/bricks", user_id=OWNER_TG)
        await h.callback(f"bk_open:{brick.id}", user_id=OWNER_TG)
        delete_data = require_button(
            h, "удал", hint="на карточке личного кирпичика нужна кнопка удаления"
        )
        await h.callback(delete_data, user_id=OWNER_TG)

        confirm_rows = rows(h)
        pair = [
            row
            for row in confirm_rows
            if any("удал" in (label or "").lower() for label, _d in row)
            and any("отмен" in (label or "").lower() for label, _d in row)
        ]
        self.assertTrue(
            pair,
            "подтверждение удаления — пара кнопок «🗑 Удалить | Отмена»: "
            f"{confirm_rows!r}",
        )
        confirm_data = next(
            data for label, data in pair[0] if "удал" in (label or "").lower()
        )
        assert confirm_data is not None
        await h.callback(confirm_data, user_id=OWNER_TG)

        with h.session_factory() as session:
            gone = session.get(Brick, brick.id)
            personal = get_personal_brick(
                session, OWNER_TG, brick.id, include_archived=True
            )
        self.assertIsNone(gone, "неиспользуемый кирпичик удаляется окончательно")
        self.assertIsNone(personal)
        await h.text("/bricks", user_id=OWNER_TG)
        self.assertNotIn(
            "Удаляемый — [удал_личный]",
            h.screen_text(),
            "удалённый кирпичик не остаётся в списке",
        )

    # ==================================================================
    # S8. Используемый кирпичик удалить нельзя; системный защищён
    # ==================================================================

    @scenario_test
    async def test_s8_used_personal_brick_delete_is_refused(self, h: Harness) -> None:
        """S8: отказ с объяснением, шаблон и привязка целы."""
        ensure_user(h, OWNER_TG)
        brick = make_personal(h, "исп_личный", "Используемый")
        with h.session_factory() as session:
            template = create_personal_template(
                session,
                actor_telegram_user_id=OWNER_TG,
                name="Шаблон с кирпичиком",
                content=f"Текст [{'исп_личный'}]",
            )
            version_id = template.current_version.version_id
            bound = int(
                session.scalar(
                    select(func.count())
                    .select_from(TemplateVersionBrick)
                    .where(
                        TemplateVersionBrick.template_version_id == version_id,
                        TemplateVersionBrick.brick_id == brick.id,
                    )
                )
                or 0
            )
        self.assertEqual(bound, 1, "подготовка: кирпичик привязан к версии")

        await h.text("/bricks", user_id=OWNER_TG)
        await h.callback(f"bk_open:{brick.id}", user_id=OWNER_TG)
        delete_data = require_button(
            h, "удал", hint="на карточке нужна кнопка удаления"
        )
        await h.callback(delete_data, user_id=OWNER_TG)
        confirm_rows = rows(h)
        pair = [
            row
            for row in confirm_rows
            if any("удал" in (label or "").lower() for label, _d in row)
            and any("отмен" in (label or "").lower() for label, _d in row)
        ]
        if not pair:
            raise AssertionError(
                "подтверждение удаления — пара кнопок «🗑 Удалить | Отмена»: "
                f"{confirm_rows!r}"
            )
        confirm_data = next(
            data for label, data in pair[0] if "удал" in (label or "").lower()
        )
        assert confirm_data is not None
        await h.callback(confirm_data, user_id=OWNER_TG)

        explanation = screen_and_alerts(h).lower()
        self.assertTrue(
            "использ" in explanation or "шаблон" in explanation,
            "отказ должен объяснять, что кирпичик используется: "
            f"{screen_and_alerts(h)[:400]!r}",
        )
        with h.session_factory() as session:
            still_there = session.get(Brick, brick.id)
            bindings = int(
                session.scalar(
                    select(func.count())
                    .select_from(TemplateVersionBrick)
                    .where(
                        TemplateVersionBrick.template_version_id == version_id,
                        TemplateVersionBrick.brick_id == brick.id,
                    )
                )
                or 0
            )
        self.assertIsNotNone(still_there, "используемый кирпичик не удаляется")
        self.assertEqual(bindings, 1, "привязка шаблона осталась прежней")

    @scenario_test
    async def test_s8_system_brick_is_protected_in_database(self, h: Harness) -> None:
        """S8: системный кирпичик нельзя удалить даже прямым DELETE."""
        with h.session_factory() as session:
            system = get_system_brick(session, ADDRESS_CODE)
        assert system is not None
        path = str(h.engine.url.database)
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM bricks WHERE id = ?", (system.id,))
                connection.commit()
        finally:
            connection.close()
        with h.session_factory() as session:
            remaining = session.get(Brick, system.id)
        self.assertIsNotNone(remaining, "системный кирпичик защищён от удаления")

    # ==================================================================
    # S9. Отклонение и отзыв не меняют исходный кирпичик
    # ==================================================================

    @scenario_test
    async def test_s9_reject_and_withdraw_keep_source_brick(self, h: Harness) -> None:
        """S9: решения по предложению не трогают личный кирпичик."""
        place_id = make_place(h, "Зал решений", admin_tg=ADMIN_TG)
        rejected = make_personal(h, "откл_личный", "Отклоняемый")
        rejected_id = await propose_to_place(h, rejected.id, place_id, tg=OWNER_TG)

        await h.text("/admin", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_place:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_open:{rejected_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_reject:{rejected_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_reject_yes:{rejected_id}", user_id=ADMIN_TG)

        withdrawn = make_personal(h, "отозв_личный", "Отзываемый")
        withdrawn_id = await propose_to_place(h, withdrawn.id, place_id, tg=OWNER_TG)
        await h.callback("bp_withdraw", user_id=OWNER_TG)
        await h.callback("bp_withdraw_yes", user_id=OWNER_TG)

        with h.session_factory() as session:
            reject_proposal = session.get(BrickProposal, rejected_id)
            withdraw_proposal = session.get(BrickProposal, withdrawn_id)
            reject_brick = session.get(Brick, rejected.id)
            withdraw_brick = session.get(Brick, withdrawn.id)
        assert reject_proposal is not None and withdraw_proposal is not None
        assert reject_brick is not None and withdraw_brick is not None
        self.assertEqual(reject_proposal.status, PROPOSAL_STATUS_REJECTED)
        self.assertEqual(withdraw_proposal.status, PROPOSAL_STATUS_WITHDRAWN)
        self.assertEqual(reject_brick.scope, "personal")
        self.assertEqual(reject_brick.description, "Личное описание")
        self.assertEqual(withdraw_brick.scope, "personal")
        self.assertEqual(withdraw_brick.description, "Личное описание")
        self.assertEqual(reject_brick.revision, rejected.revision)

    # ==================================================================
    # S10. Конфликт кода и повторное решение
    # ==================================================================

    @scenario_test
    async def test_s10_code_conflict_and_repeated_decision(self, h: Harness) -> None:
        """S10: conflict не пишет данных; повтор — already_decided."""
        place_id = make_place(h, "Зал конфликта", admin_tg=ADMIN_TG)
        personal = make_personal(h, "код_дубль", "Дубль")
        proposal_id = await propose_to_place(h, personal.id, place_id, tg=OWNER_TG)
        # Пока предложение ждало решения, код заняли на площадке.
        make_place_brick(h, place_id, "код_дубль", "Местный дубль")

        with h.session_factory() as session:
            place_before = list_place_bricks(session, place_id)
            brick_total_before = int(
                session.scalar(select(func.count()).select_from(Brick)) or 0
            )
            result = approve_brick_proposal(
                session,
                actor_telegram_user_id=ADMIN_TG,
                proposal_id=proposal_id,
            )
            proposal = session.get(BrickProposal, proposal_id)
            place_after = list_place_bricks(session, place_id)
            brick_total_after = int(
                session.scalar(select(func.count()).select_from(Brick)) or 0
            )
        self.assertEqual(result.status, "conflict")
        assert proposal is not None
        self.assertEqual(proposal.status, PROPOSAL_STATUS_PENDING)
        self.assertEqual(
            [brick.id for brick in place_after], [brick.id for brick in place_before]
        )
        self.assertEqual(brick_total_before, brick_total_after)
        with h.session_factory() as session:
            personal_after = session.get(Brick, personal.id)
        assert personal_after is not None
        self.assertEqual(personal_after.code, "код_дубль")
        self.assertEqual(personal_after.description, "Личное описание")

        # Повторное решение уже одобренного предложения.
        second_place = make_place(h, "Зал повтора", admin_tg=ADMIN_TG)
        second = make_personal(h, "код_повтор", "Повтор")
        second_proposal = await propose_to_place(
            h, second.id, second_place, tg=OWNER_TG
        )
        with h.session_factory() as session:
            first = approve_brick_proposal(
                session,
                actor_telegram_user_id=ADMIN_TG,
                proposal_id=second_proposal,
            )
            again = approve_brick_proposal(
                session,
                actor_telegram_user_id=ADMIN_TG,
                proposal_id=second_proposal,
            )
            rejected_again = reject_brick_proposal(
                session,
                actor_telegram_user_id=ADMIN_TG,
                proposal_id=second_proposal,
            )
        self.assertEqual(first.status, PROPOSAL_STATUS_APPROVED)
        self.assertEqual(again.status, "already_decided")
        self.assertEqual(rejected_again.status, "already_decided")
        with h.session_factory() as session:
            second_after = session.get(Brick, second.id)
        assert second_after is not None
        self.assertEqual(second_after.scope, "personal", "личный не повышен")
        self.assertEqual(proposal_count(h), 2)

    # ==================================================================
    # S11. Миграция: «адрес_места» устранён, FK/integrity целы
    # ==================================================================

    def test_s11a_migration_removes_legacy_address_brick(self) -> None:
        """S11: апгрейд базы версии 24 без строки bricks id=6."""
        path = temp_db_path("poster-24-p1-migration-")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        engine = make_engine(path)
        self.addCleanup(engine.dispose)
        self.assertEqual(run_migrations(engine, DEFAULT_MIGRATIONS[:24]), 24)
        self.assertGreater(
            LATEST_VERSION,
            24,
            "требуется новая миграция Poster 2.4, устраняющая «адрес_места»",
        )
        self._seed_version_24(path)
        self.assertEqual(run_migrations(engine), LATEST_VERSION)
        self._assert_no_legacy_address(path)
        self.assertEqual(run_migrations(engine), LATEST_VERSION)

    def test_s11b_fresh_install_has_single_address_brick(self) -> None:
        """S11: чистая установка сразу без «адрес_места», ровно один «адрес»."""
        path = temp_db_path("poster-24-p1-fresh-")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        engine = make_engine(path)
        self.addCleanup(engine.dispose)
        self.assertEqual(run_migrations(engine), LATEST_VERSION)
        self._assert_no_legacy_address(path)

    # --- помощники S11 ----------------------------------------------------

    def _seed_version_24(self, path: str) -> None:
        """Данные, которые миграция обязана обработать без потери целостности."""
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 940001, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO places (id, created_by_user_id, created_at) "
                "VALUES (1, 1, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO announcement_drafts "
                "(id, author_user_id, place_id, place_display_name, origin, "
                " stage, revision, created_at, updated_at) "
                "VALUES (1, 1, 1, 'Старая площадка', 'place', 'place', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO announcement_values "
                "(id, draft_id, brick_id, state, value_json, created_at, updated_at) "
                "VALUES (1, 1, 6, 'valid', '{\"text\": \"ул. Старая, 1\"}', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO templates "
                "(id, kind, owner_user_id, name, revision, created_by_user_id, "
                " created_at, updated_at) "
                "VALUES (1, 'personal', 1, 'Старый шаблон', 1, 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO template_versions "
                "(id, template_id, version, content, extended_content, "
                " created_by_user_id, created_at, builtin_version) "
                "VALUES (1, 1, 1, '[адрес_места] и [адрес]', NULL, 1, "
                "'2026-01-01 00:00:00', NULL)"
            )
            connection.execute(
                "INSERT INTO template_version_bricks "
                "(template_version_id, brick_id, code) VALUES (1, 6, 'адрес_места')"
            )
            connection.execute(
                "INSERT INTO template_version_bricks "
                "(template_version_id, brick_id, code) VALUES (1, 10, 'адрес')"
            )
            connection.commit()
        finally:
            connection.close()

    def _assert_no_legacy_address(self, path: str) -> None:
        """Контракт: ровно один системный «адрес»; «адрес_места» нет нигде."""
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            legacy_brick = connection.execute(
                "SELECT id, scope FROM bricks WHERE code = ?",
                (LEGACY_ADDRESS_CODE,),
            ).fetchall()
            system_address = connection.execute(
                "SELECT code FROM bricks WHERE scope = 'system' AND code = ?",
                (ADDRESS_CODE,),
            ).fetchall()
            dangling_values = connection.execute(
                "SELECT v.id FROM announcement_values AS v "
                "LEFT JOIN bricks AS b ON b.id = v.brick_id "
                "WHERE b.id IS NULL"
            ).fetchall()
            dangling_bindings = connection.execute(
                "SELECT t.template_version_id, t.brick_id "
                "FROM template_version_bricks AS t "
                "LEFT JOIN bricks AS b ON b.id = t.brick_id "
                "WHERE b.id IS NULL"
            ).fetchall()
            legacy_bindings = connection.execute(
                "SELECT 1 FROM template_version_bricks WHERE code = ?",
                (LEGACY_ADDRESS_CODE,),
            ).fetchall()
            fk_problems = connection.execute("PRAGMA foreign_key_check").fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
        self.assertEqual(
            legacy_brick,
            [],
            "кода «адрес_места» не должно быть ни в одном scope",
        )
        self.assertEqual(
            system_address,
            [(ADDRESS_CODE,)],
            f"должен остаться ровно один системный кирпичик «адрес»: {system_address}",
        )
        self.assertEqual(
            dangling_values,
            [],
            f"значения не должны ссылаться на удалённый кирпичик: {dangling_values}",
        )
        self.assertEqual(
            dangling_bindings,
            [],
            f"привязки шаблонов не должны ссылаться на удалённый кирпичик: "
            f"{dangling_bindings}",
        )
        self.assertEqual(
            legacy_bindings, [], "привязок с кодом «адрес_места» быть не должно"
        )
        self.assertEqual(fk_problems, [], f"нарушения FK: {fk_problems}")
        self.assertEqual(integrity[0], "ok", f"integrity_check: {integrity}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
