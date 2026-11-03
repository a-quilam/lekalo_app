# tests/test_poster2_24_p3_announcements_tester.py
"""
Poster 2.4, этап P3 — независимые тесты Tester (тест-до-кода).

Контракт владельца «меню и анонсы» (docs/agent_team/state.md, P3):

A. Главное меню — ровно 3 строки:
   [📝 Создать анонс],
   [📂 Мои анонсы],
   [⚙️ Управление, ❓ Помощь].

B. «Мои анонсы»:
   - кнопки создания анонса на экране НЕТ (ни `aw_new`, ни «Создать…»);
   - у каждой записи (черновик и готовый) пара кнопок в одной строке:
     открытие + «🗑» удаление; черновик открывается в конструктор,
     готовый — на свой экран;
   - «🗑» сначала спрашивает подтверждение; «Отмена» возвращает к списку
     без удаления; подтверждение удаляет запись из БД и обновляет список;
     Telegram-сообщения НЕ удаляются (в FakeBot нет `delete_message`);
   - повторное открытие удалённой записи по старому callback даёт
     понятное сообщение («анонс больше не хранится»/«не найден»), а не
     внутреннюю ошибку.

C. Лимиты (по created_at, независимо):
   - черновиков — 4: создание 5-го вытесняет самый старый черновик;
   - готовых — 4: публикация 5-го вытесняет самую старую готовую запись;
   - вытеснение черновиков не трогает готовые и наоборот.

D. Автоочистка:
   - серверная функция очистки удаляет записи старше 30 суток по
     created_at; граница «>= 30 суток = удалить» (30-суточная запись
     удаляется, 29-суточная остаётся), дата передаётся параметром;
   - Telegram-сообщения не удаляются;
   - очистка зарегистрирована в `bot.build_application` (JobQueue с
     повтором не реже раза в час либо post_init-регистрация).

Все сценарии идут через настоящее `Application.process_update`, реальные
обработчики, FakeBot и временный SQLite (`tests.integration_harness`).
Прямые доменные вызовы — только для подготовки данных (черновики с
заданным created_at, готовые записи «как после доставки»).

Не реализованное на текущем main помечено «P3: ожидаемая реализация» —
эти тесты падают намеренно (тест-до-кода).
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
from datetime import timedelta

try:
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    import announcement_store
    from announcement_store import start_announcement_wizard
    from models import AnnouncementDraft, User, utcnow
    from place_store import create_place
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        Harness,
        call_text,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callbacks,
        _seed_personal_template,
    )

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    IMPORT_ERROR = str(exc)


# --- литералы владельца (значения из config/ui_texts.yaml) --------------------

MENU_ROWS = [
    [("📝 Создать анонс", "pick_place")],
    [("📂 Мои анонсы", "my_announcements")],
    [("⚙️ Управление", "manage"), ("❓ Помощь", "help")],
]

LIST_HEAD = "📂 Мои анонсы"
DRAFT_STATUS = "черновик"
SENT_STATUS = "готовый анонс отправлен"
# §18 (2.5): экрана «✅ Анонс готов» нет — после доставки открыт редактор.
DELIVERED_HEAD = "✏️ Изменить анонс"
QUESTION_HEAD = "🧩 Вопрос"
INTERNAL_FRAGMENT = "Что-то пошло не так"
GONE_FRAGMENTS = ("больше не хранится", "не найден", "больше недоступ", "больше нет")

CLEANUP_NAMES = (
    "cleanup_old_announcements",
    "cleanup_announcements",
    "purge_old_announcements",
    "delete_old_announcements",
)


# --- наблюдение за экраном ----------------------------------------------------


def _rows(h: Harness) -> list[list]:
    """Матрица inline-клавиатуры текущего экрана (текст, callback_data)."""
    record = h.screen()
    if record is None:
        return []
    markup = record.get("reply_markup")
    if markup is None:
        return []
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _labels(h: Harness) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _datas(h: Harness) -> list[str]:
    return [data for _label, data in h.screen_buttons() if data is not None]


def _user_texts(h: Harness) -> list[str]:
    """Всё, что видел пользователь: экраны, ответы, подписи, алерты."""
    seen: list[str] = []
    for call in h.calls:
        for value in (call_text(call), call.get("caption")):
            if isinstance(value, str):
                seen.append(value)
        if call["type"] == "callback_answer":
            text = call.get("text")
            if isinstance(text, str):
                seen.append(text)
            args = tuple(call.get("args") or ())
            if args and isinstance(args[0], str):
                seen.append(args[0])
    return seen


def _assert_no_internal(test: unittest.TestCase, h: Harness, label: str) -> None:
    internal = [text for text in _user_texts(h) if INTERNAL_FRAGMENT in text]
    test.assertEqual(internal, [], f"{label}: «внутренняя ошибка» вместо экрана")


def _is_trash(label: str, data: str | None) -> bool:
    """Кнопка удаления записи (владелец требует именно «🗑»)."""
    if label.startswith("🗑"):
        return True
    return bool(data and ("del" in data.lower() or "remove" in data.lower()))


def _is_cancel(label: str, data: str | None) -> bool:
    if "отмен" in label.lower():
        return True
    low = (data or "").lower()
    return "cancel" in low or low.endswith(":no") or low.endswith("_no")


def _is_create(label: str, data: str | None) -> bool:
    """Кнопка создания анонса на экране «Мои анонсы» (запрещена)."""
    if data in {"aw_new", "create_post"}:
        return True
    if data and ("new" in data.lower() or "create" in data.lower()):
        return True
    return "создать" in label.lower() or "новый анонс" in label.lower()


def _same_entry(data: str, draft_id: int) -> bool:
    return data.endswith(f":{draft_id}") or f":{draft_id}" in data


def _entry_pair(
    h: Harness, draft_id: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(кнопки открытия, кнопки удаления) выбранной записи."""
    opens: list[tuple[str, str]] = []
    trashes: list[tuple[str, str]] = []
    for label, data in h.screen_buttons():
        if not data or not _same_entry(data, draft_id):
            continue
        if _is_trash(label, data):
            trashes.append((label, data))
        else:
            opens.append((label, data))
    return opens, trashes


def _entry_row(h: Harness, data: str) -> list[tuple[str, str | None]] | None:
    for row in _rows(h):
        if any(cell[1] == data for cell in row):
            return row
    return None


async def _open_list(h: Harness) -> None:
    """Открывает «Мои анонсы» из меню (экран перерисовывается заново)."""
    await h.callback("main_menu")
    await h.callback("my_announcements")
    if LIST_HEAD not in h.screen_text():
        raise AssertionError(f"не открылся список анонсов: {h.screen_text()[:300]!r}")


# --- данные ---------------------------------------------------------------


def _user_row(session, tg: int = DEFAULT_USER_ID) -> User:
    user = session.scalar(select(User).where(User.telegram_user_id == tg))
    if user is None:
        raise AssertionError(f"пользователь {tg} не создан")
    return user


def _seed_draft_rows(
    h: Harness,
    *,
    count: int,
    start_at,
    step_hours: int = 1,
) -> list[int]:
    """
    Подготовка данных: незавершённые анонсы с фиксированным created_at.

    `created_at` неизменяем после вставки (триггер `identity is immutable`),
    поэтому датированные черновики вставляются строками БД, а не через
    доменный вход: сценарий проверяет вытеснение ПО created_at, а не
    создание. Домен «создать черновик» проверяется отдельно реальным UI.
    """
    out: list[int] = []
    with h.session_factory() as session:
        user = _user_row(session)
        for index in range(count):
            stamp = start_at + timedelta(hours=index * step_hours)
            row = AnnouncementDraft(
                author_user_id=user.id,
                place_id=None,
                place_name_id=None,
                place_display_name=None,
                place_address=None,
                place_url=None,
                place_city=None,
                origin="personal",
                template_id=None,
                template_version=None,
                template_snapshot_id=None,
                stage="questions",
                revision=1,
                created_at=stamp,
                updated_at=stamp,
            )
            session.add(row)
            session.flush()
            out.append(row.id)
        session.commit()
    return out


def _seed_real_draft(h: Harness, *, template_id: int) -> int:
    """Незавершённый анонс настоящим доменным входом (со снимком шаблона)."""
    with h.session_factory() as session:
        draft = start_announcement_wizard(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            template_id=template_id,
            origin="personal",
        )
        return draft.draft_id


def _seed_sent(h: Harness, created_at, *, label: str = "") -> int:
    """
    Готовая запись «как после доставки» (подготовка данных).

    Прямая строка БД: у настоящей доставки остаётся снимок шаблона и
    прогресс сообщений, для проверки лимитов/очистки они не нужны
    (`сама доставка` проверяется отдельным тестом через UI).
    """
    with h.session_factory() as session:
        user = _user_row(session)
        row = AnnouncementDraft(
            author_user_id=user.id,
            place_id=None,
            place_name_id=None,
            place_display_name=None,
            place_address=None,
            place_url=None,
            place_city=None,
            origin="personal",
            template_id=None,
            template_version=None,
            template_snapshot_id=None,
            stage="completed",
            revision=1,
            publication_status="sent",
            publication_text="Текст отправленного анонса",
            publication_photos="[]",
            publication_started_at=created_at,
            published_at=created_at,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(row)
        session.commit()
        return row.id


def _records(h: Harness) -> list[tuple[int, str | None, object]]:
    """(id, publication_status, created_at) всех записей, по возрастанию id."""
    with h.session_factory() as session:
        rows = session.query(AnnouncementDraft).order_by(AnnouncementDraft.id).all()
        return [(row.id, row.publication_status, row.created_at) for row in rows]


def _draft_ids(h: Harness) -> list[int]:
    """Незавершённые анонсы: всё, что ещё не готовый (не `sent`)."""
    return [item[0] for item in _records(h) if item[1] != "sent"]


def _sent_ids(h: Harness) -> list[int]:
    return [item[0] for item in _records(h) if item[1] == "sent"]


def _exists(h: Harness, draft_id: int) -> bool:
    return any(item[0] == draft_id for item in _records(h))


async def _publish_personal(h: Harness, template_id: int) -> None:
    """Личный анонс целиком через UI: вопрос → без фото → доставка."""
    await h.callback(f"tp_pick:{template_id}")
    if QUESTION_HEAD in h.screen_text():
        await h.text("Текст анонса")
    data = _callbacks(h, "aw_photo_none")
    if not data:
        raise AssertionError(
            f"нет шага фото после ответа: {h.screen_text()[:300]!r}; "
            f"кнопки: {h.screen_buttons()}"
        )
    await h.callback(data[0])
    if not h.screen_text().startswith(DELIVERED_HEAD):
        raise AssertionError(
            f"анонс не доставлен: {h.screen_text()[:300]!r}; "
            f"ответы: {h.answer_texts()}"
        )
    await h.callback("main_menu")


def _unique_template(h: Harness, name: str) -> int:
    template_id, _revision, _bricks = _seed_personal_template(
        h,
        name=name,
        specs=[("main", "Основное", "short_text", {})],
    )
    return template_id


# --- A. Главное меню --------------------------------------------------------


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class MainMenuLayoutTests(unittest.TestCase):
    """A.1: главное меню — ровно 3 строки в порядке владельца."""

    @scenario_test
    async def test_a1_main_menu_has_exactly_three_rows_in_given_order(self, h):
        """
        A.1: меню 3 строки (Poster 2.4.1 T3), одиночная «Мои анонсы».

        Строка 1 — единый вход «📝 Создать анонс»; строка 2 — «📂 Мои
        анонсы»; строка 3 — «⚙️ Управление» и «❓ Помощь». Лишних кнопок нет.
        """
        await h.text("/start")
        rows = [[(label, data) for label, data in row] for row in _rows(h)]
        self.assertEqual(
            rows,
            MENU_ROWS,
            "главное меню должно быть ровно из трёх строк в порядке владельца; "
            f"фактически: {rows}",
        )
        datas = _datas(h)
        self.assertEqual(len(datas), 4, f"в меню должно быть 4 кнопки: {datas}")
        for forbidden in ("aw_new", "create_post", "my_announcements_new"):
            self.assertNotIn(forbidden, datas, f"в меню лишняя кнопка {forbidden!r}")

    @scenario_test
    async def test_a1b_menu_labels_come_from_text_catalog(self, h):
        """
        A.1 (тексты): подписи меню берутся из каталога через tr(), не литералами.

        Источник `main_menu_keyboard` ссылается на ключи каталога
        `buttons.main.*` и не содержит самих подписей — правка
        `config/ui_texts.yaml` меняет меню без правки кода.
        """
        import handlers.main_menu as main_menu_module

        source = inspect.getsource(main_menu_module.main_menu_keyboard)
        for key in (
            "buttons.main.create_place",
            "buttons.main.announcements",
            "buttons.main.manage",
            "buttons.main.help",
        ):
            self.assertIn(
                key,
                source,
                f"подпись меню должна браться из каталога по ключу {key!r}",
            )
        for label, _data in [item for row in MENU_ROWS for item in row]:
            self.assertNotIn(
                label,
                source,
                f"подпись {label!r} вшита в код вместо tr()-ключа каталога",
            )


# --- B. Мои анонсы ----------------------------------------------------------


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class MyAnnouncementsTests(unittest.TestCase):
    """B.2–B.5: список без создания, пары кнопок, удаление с подтверждением."""

    @scenario_test
    async def test_b2_list_has_no_create_button(self, h):
        """
        B.2: на экране «Мои анонсы» нет кнопки создания анонса.

        Ни в пустом состоянии, ни при наличии записей: ни `aw_new`, ни
        текстового «Создать…»/«➕ Новый анонс» (P3: ожидаемая реализация).
        """
        await h.text("/start")
        await h.callback("my_announcements")
        self.assertIn(LIST_HEAD, h.screen_text(), "не открылся список анонсов")
        empty_state = h.screen_buttons()
        offenders = [
            (label, data) for label, data in empty_state if _is_create(label, data)
        ]
        self.assertEqual(
            offenders,
            [],
            "пустой список: кнопка создания анонса в «Мои анонсы» запрещена: "
            f"{offenders}; весь экран: {empty_state}",
        )

        # Тот же запрет при непустом списке (после настоящей доставки).
        template_id = _unique_template(h, "Анонс Б2")
        await h.callback("main_menu")
        await _publish_personal(h, template_id)
        await _open_list(h)
        filled_state = h.screen_buttons()
        offenders = [
            (label, data) for label, data in filled_state if _is_create(label, data)
        ]
        self.assertEqual(
            offenders,
            [],
            "список с записями: кнопка создания анонса в «Мои анонсы» "
            f"запрещена: {offenders}; весь экран: {filled_state}",
        )

    @scenario_test
    async def test_b3_each_entry_has_open_and_trash_pair(self, h):
        """
        B.3: у черновика и у готового — пара «открытие» + «🗑» в одной строке.

        Открытие черновика ведёт в конструктор (экран вопроса), открытие
        готового — переиздаёт экземпляр (§29–30, 2.5) и открывает редактор.
        """
        await h.text("/start")
        draft_template = _unique_template(h, "Черновик Б3")
        sent_template = _unique_template(h, "Готовый Б3")
        draft_ids = [_seed_real_draft(h, template_id=draft_template)]
        await _publish_personal(h, sent_template)
        sent_ids = _sent_ids(h)
        self.assertTrue(sent_ids, "готовый анонс не сохранился в БД")

        await _open_list(h)
        entries = [(draft_ids[0], "черновик"), (sent_ids[0], "готовый")]
        for draft_id, kind in entries:
            opens, trashes = _entry_pair(h, draft_id)
            self.assertTrue(
                opens,
                f"{kind} {draft_id}: нет кнопки открытия; экран: "
                f"{h.screen_buttons()}",
            )
            self.assertEqual(
                len(trashes),
                1,
                f"{kind} {draft_id}: нужна ровно одна кнопка «🗑» удаления; "
                f"экран: {h.screen_buttons()}",
            )
            self.assertTrue(
                trashes[0][0].startswith("🗑"),
                f"{kind} {draft_id}: кнопка удаления должна называться «🗑 …»: "
                f"{trashes[0]}",
            )
            row = _entry_row(h, opens[0][1])
            self.assertIsNotNone(row, "кнопка открытия не найдена в клавиатуре")
            self.assertIn(
                trashes[0][1],
                [data for _label, data in row],
                f"{kind} {draft_id}: открытие и «🗑» должны стоять парой "
                f"в одной строке; строка: {row}",
            )

        # Открытие черновика → конструктор.
        draft_open = _entry_pair(h, draft_ids[0])[0][0][1]
        await h.callback(draft_open)
        _assert_no_internal(self, h, "открытие черновика из списка")
        self.assertIn(
            QUESTION_HEAD,
            h.screen_text(),
            "открытие черновика должно вести в конструктор (экран вопроса), "
            f"фактически: {h.screen_text()[:300]!r}",
        )

        # Открытие готового → новый экземпляр и рабочий экран редактора.
        await _open_list(h)
        sent_open = _entry_pair(h, sent_ids[0])[0][0][1]
        await h.callback(sent_open)
        _assert_no_internal(self, h, "открытие готового анонса из списка")
        self.assertIn(
            DELIVERED_HEAD,
            h.screen_text(),
            "открытие готового анонса должно вести в редактор анонса, "
            "фактически: "
            f"{h.screen_text()[:300]!r}",
        )

    @scenario_test
    async def test_b4_delete_asks_confirmation_and_keeps_messages(self, h):
        """
        B.4: «🗑» спрашивает подтверждение; отмена и подтверждение.

        - нажатие «🗑» НЕ удаляет запись сразу;
        - «Отмена» возвращает к списку, запись в БД осталась;
        - подтверждение удаляет запись из БД, список обновлён;
        - ни одного `delete_message` за весь сценарий (Telegram-сообщения,
          включая экран, не удаляются) — P3: ожидаемая реализация.
        """
        await h.text("/start")
        first = _unique_template(h, "Черновик Б4-А")
        second = _unique_template(h, "Черновик Б4-Б")
        first_ids = [_seed_real_draft(h, template_id=first)]
        second_ids = [_seed_real_draft(h, template_id=second)]
        target, keeper = second_ids[0], first_ids[0]
        await _open_list(h)
        deletes_before = len(h.deletes())

        opens, trashes = _entry_pair(h, target)
        self.assertTrue(opens and len(trashes) == 1, "у записи нет пары открытие|🗑")
        await h.callback(trashes[0][1])
        _assert_no_internal(self, h, "запрос подтверждения удаления")
        self.assertTrue(
            _exists(h, target),
            "«🗑» не должен удалять запись до подтверждения",
        )
        self.assertTrue(
            "удал" in h.screen_text().lower(),
            "после «🗑» нужен экран подтверждения удаления, фактически: "
            f"{h.screen_text()[:300]!r}",
        )

        cancels = [item for item in h.screen_buttons() if _is_cancel(*item)]
        self.assertTrue(
            cancels,
            f"на подтверждении удаления нужна кнопка «Отмена»: {h.screen_buttons()}",
        )
        await h.callback(cancels[0][1])
        _assert_no_internal(self, h, "отмена удаления")
        self.assertTrue(_exists(h, target), "«Отмена» не должна удалять запись из БД")
        self.assertIn(
            LIST_HEAD,
            h.screen_text(),
            "после отмены должен вернуться список анонсов, фактически: "
            f"{h.screen_text()[:300]!r}",
        )

        # Повторное «🗑» и подтверждение — на актуальном экране списка.
        await _open_list(h)
        _opens, trashes = _entry_pair(h, target)
        self.assertEqual(len(trashes), 1, "после отмены «🗑» должна остаться на месте")
        await h.callback(trashes[0][1])
        _assert_no_internal(self, h, "повторный запрос подтверждения")
        cancels = [item for item in h.screen_buttons() if _is_cancel(*item)]
        candidates = [
            item
            for item in h.screen_buttons()
            if item not in cancels
            and item[1] not in {None, "main_menu"}
            and not (item[1] or "").startswith("aw_open")
        ]
        confirm = [item for item in candidates if _is_trash(*item)]
        if not confirm:
            confirm = candidates
        self.assertEqual(
            len(confirm),
            1,
            "на подтверждении удаления нужна ровно одна кнопка подтверждения, "
            f"фактически: {h.screen_buttons()}",
        )
        await h.callback(confirm[0][1])
        _assert_no_internal(self, h, "подтверждение удаления")

        self.assertFalse(
            _exists(h, target), "после подтверждения запись должна исчезнуть из БД"
        )
        self.assertTrue(
            _exists(h, keeper), "удаление не должно затрагивать другие записи"
        )
        self.assertIn(
            LIST_HEAD,
            h.screen_text(),
            "после подтверждения должен обновиться список анонсов, фактически: "
            f"{h.screen_text()[:300]!r}",
        )
        self.assertNotIn(
            f":{target}",
            " ".join(_datas(h)),
            "удалённой записи не должно остаться в кнопках списка",
        )
        self.assertEqual(
            len(h.deletes()),
            deletes_before,
            "удаление записи не должно удалять Telegram-сообщения "
            f"(delete_message): {h.deleted_ids()}",
        )

    @scenario_test
    async def test_b5_deleted_entry_gives_clear_message_on_stale_callback(self, h):
        """
        B.5: старый callback удалённой записи — понятное сообщение, не сбой.

        Ожидается доменное сообщение вида «анонс больше не хранится» /
        «не найден» (alert), экран списка/меню, без «Что-то пошло не так»
        (P3: ожидаемая реализация).
        """
        await h.text("/start")
        template_id = _unique_template(h, "Черновик Б5")
        draft_ids = [_seed_real_draft(h, template_id=template_id)]
        target = draft_ids[0]
        await _open_list(h)

        _opens, trashes = _entry_pair(h, target)
        if not trashes:
            self.fail(
                "нет кнопки «🗑»: сценарий B.5 требует удаления записи из "
                f"списка; экран: {h.screen_buttons()}"
            )
        await h.callback(trashes[0][1])
        cancels = [item for item in h.screen_buttons() if _is_cancel(*item)]
        candidates = [
            item
            for item in h.screen_buttons()
            if item not in cancels and item[1] not in {None, "main_menu"}
        ]
        confirm = [item for item in candidates if _is_trash(*item)] or candidates
        if not confirm:
            self.fail("на подтверждении удаления нет кнопки подтверждения")
        await h.callback(confirm[0][1])
        self.assertFalse(_exists(h, target), "запись не удалилась — сценарий B.5")

        # Устаревшая кнопка «открыть» удалённой записи.
        await h.callback(f"aw_open:{target}")
        _assert_no_internal(self, h, "старый callback удалённой записи")
        alerts = " | ".join(h.answer_texts())
        self.assertTrue(
            any(fragment in alerts for fragment in GONE_FRAGMENTS),
            "по старому callback нужен понятный ответ о том, что анонс больше "
            f"не хранится; ответы: {h.answer_texts()}",
        )
        self.assertNotIn(
            QUESTION_HEAD,
            h.screen_text(),
            "удалённая запись не должна открываться в конструктор",
        )


# --- C. Лимиты --------------------------------------------------------------


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class LimitTests(unittest.TestCase):
    """C.6–C.7: 4 черновика и 4 готовых, вытеснение по created_at."""

    @scenario_test
    async def test_c6_fifth_draft_evicts_oldest_draft(self, h):
        """
        C.6: при создании 5-го черновика удаляется самый старый по created_at.

        Лимит черновиков = 4; вытеснение по created_at, а не по id
        (P3: ожидаемая реализация).
        """
        await h.text("/start")
        template_id = _unique_template(h, "Черновик C6")
        base = utcnow() - timedelta(days=3)
        seeded = _seed_draft_rows(h, count=4, start_at=base, step_hours=5)
        self.assertEqual(len(_draft_ids(h)), 4, "подготовка: должно быть 4 черновика")

        await h.callback(f"tp_pick:{template_id}")
        if QUESTION_HEAD not in h.screen_text():
            self.fail(
                "не удалось создать 5-й черновик через UI: "
                f"{h.screen_text()[:300]!r}"
            )
        _assert_no_internal(self, h, "создание 5-го черновика")

        drafts = _draft_ids(h)
        self.assertEqual(
            len(drafts),
            4,
            "лимит черновиков = 4: после создания 5-го в БД должно остаться "
            f"4 записи, фактически: {_records(h)}",
        )
        self.assertNotIn(
            seeded[0],
            drafts,
            "должен вытесняться самый старый по created_at черновик; "
            f"вытесненные/оставшиеся: {_records(h)}",
        )
        for kept in seeded[1:]:
            self.assertIn(
                kept,
                drafts,
                "более новые черновики должны сохраниться: " f"{_records(h)}",
            )

    @scenario_test
    async def test_c6b_sent_records_do_not_count_for_draft_limit(self, h):
        """
        C.6 (независимость): готовые записи не входят в лимит черновиков.

        При 4 готовых создание черновика не вытесняет ни готовые, ни сам
        черновик (P3: ожидаемая реализация).
        """
        await h.text("/start")
        template_id = _unique_template(h, "Черновик C6b")
        base = utcnow() - timedelta(days=10)
        sent = [_seed_sent(h, base + timedelta(hours=i)) for i in range(4)]
        existing = _seed_draft_rows(h, count=1, start_at=base)[0]

        await h.callback(f"tp_pick:{template_id}")
        self.assertIn(
            QUESTION_HEAD,
            h.screen_text(),
            f"новый черновик не создан: {h.screen_text()[:300]!r}",
        )
        self.assertTrue(_exists(h, existing), "существующий черновик вытеснен")
        self.assertEqual(
            len(_draft_ids(h)),
            2,
            "лимит черновиков считается отдельно от готовых; "
            f"фактически: {_records(h)}",
        )
        for sent_id in sent:
            self.assertTrue(
                _exists(h, sent_id),
                "готовые записи не должны вытесняться при создании черновика: "
                f"{_records(h)}",
            )

    @scenario_test
    async def test_c7_fifth_sent_evicts_oldest_sent_and_keeps_drafts(self, h):
        """
        C.7: публикация 5-го при 4 готовых вытесняет самую старую готовую.

        Готовых = 4; вытеснение по created_at; черновики не трогаются
        (P3: ожидаемая реализация).
        """
        await h.text("/start")
        sent_template = _unique_template(h, "Готовый C7")
        base = utcnow() - timedelta(days=20)
        sent = [_seed_sent(h, base + timedelta(hours=i)) for i in range(4)]
        drafts = _seed_draft_rows(h, count=2, start_at=base)

        await _publish_personal(h, sent_template)

        sent_after = _sent_ids(h)
        self.assertEqual(
            len(sent_after),
            4,
            "лимит готовых = 4: после публикации 5-го в БД должно остаться "
            f"4 готовых; фактически: {_records(h)}",
        )
        self.assertNotIn(
            sent[0],
            sent_after,
            "должна вытесняться самая старая по created_at готовая запись; "
            f"фактически: {_records(h)}",
        )
        for kept in sent[1:]:
            self.assertIn(
                kept,
                sent_after,
                f"более новые готовые должны сохраниться: {_records(h)}",
            )
        for draft_id in drafts:
            self.assertTrue(
                _exists(h, draft_id),
                "публикация не должна вытеснять черновики: " f"{_records(h)}",
            )
        self.assertEqual(
            len(_draft_ids(h)),
            2,
            f"черновики не тронуты публикацией: {_records(h)}",
        )

    @scenario_test
    async def test_c6c_place_draft_limit_holds_with_production_autoflush(self, h):
        """
        C.6 (прод-конфиг): лимит черновиков при `autoflush=False`.

        Прод-сессия создаётся как `sessionmaker(autoflush=False)`
        (`database.SessionLocal`), поэтому незакоммиченный INSERT не виден
        SELECT'у вытеснения до явного `flush()`. Раньше площадочный путь
        (`_start_place_announcement`) считал лимит ДО flush: `row.id` был
        None (защита нового черновика не работала), а сам черновик не
        попадал в подсчёт — пятый черновик не вытеснял старый, и в БД
        устойчиво оставалось 5. Харнесс этого не ловил: его sessionmaker
        с дефолтным `autoflush=True`. Тест фиксирует именно прод-конфиг.
        """
        await h.text("/start")
        with h.session_factory() as session:
            user = _user_row(session)
            place_id = create_place(
                session,
                "Площадка C6c",
                city="Тестовый город",
                created_by_user_id=user.id,
            ).id
            session.commit()
        base = utcnow() - timedelta(days=2)
        seeded = _seed_draft_rows(h, count=4, start_at=base, step_hours=6)
        self.assertEqual(len(_draft_ids(h)), 4, "подготовка: должно быть 4 черновика")

        # Как в production: autoflush выключен, наружу ничего не подмешивается.
        place_session_factory = sessionmaker(bind=h.engine, autoflush=False)
        with place_session_factory() as session:
            details = start_announcement_wizard(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                place_id=place_id,
            )

        drafts = _draft_ids(h)
        self.assertEqual(
            len(drafts),
            4,
            "при autoflush=False лимит черновиков = 4: flush перед подсчётом "
            f"обязателен, фактически: {_records(h)}",
        )
        self.assertIn(
            details.draft_id,
            drafts,
            "только что созданный черновик не должен вытесняться сам собой: "
            f"{_records(h)}",
        )
        self.assertNotIn(
            seeded[0],
            drafts,
            "должен вытесняться самый старый по created_at черновик: " f"{_records(h)}",
        )
        for kept in seeded[1:]:
            self.assertIn(
                kept,
                drafts,
                f"более новые черновики должны сохраниться: {_records(h)}",
            )


# --- D. Автоочистка ---------------------------------------------------------


def _cleanup_function():
    """Функция очистки из домена (имя — часть контракта P3)."""
    for name in CLEANUP_NAMES:
        candidate = getattr(announcement_store, name, None)
        if callable(candidate):
            return name, candidate
    return None, None


def _cleanup_kwargs(fn, session, now) -> dict:
    """
    Аргументы очистки по именам параметров.

    Контракт допускает любую раскладку: `session` (обязательно),
    текущая дата (`now`/`today`/...) либо готовый порог
    (`cutoff`/`before`/...) и срок хранения в сутках.
    """
    kwargs: dict = {}
    unknown: list[str] = []
    for name, param in inspect.signature(fn).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        low = name.lower()
        if low in {"session", "db", "db_session", "s"}:
            kwargs[name] = session
        elif low in {"cutoff", "before", "threshold", "older_than"}:
            kwargs[name] = now - timedelta(days=30)
        elif low in {
            "now",
            "today",
            "current",
            "current_time",
            "reference",
            "moment",
            "at",
        }:
            kwargs[name] = now
        elif "day" in low or "age" in low or "retention" in low or "ttl" in low:
            kwargs[name] = 30
        elif param.default is param.empty:
            unknown.append(name)
    if unknown:
        raise AssertionError(
            f"неизвестные обязательные параметры очистки: {unknown}; "
            f"сигнатура: {inspect.signature(fn)}"
        )
    return kwargs


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class CleanupTests(unittest.TestCase):
    """D.8–D.9: очистка записей старше 30 суток и её регистрация."""

    @scenario_test
    async def test_d8_cleanup_removes_entries_30_days_and_older(self, h):
        """
        D.8: очистка по created_at с инъекцией «сейчас».

        Семантика зафиксирована: `>= 30 суток = удалить` — запись ровно
        30-суточной давности удаляется, 29-суточная остаётся. Проверяются
        и незавершённые, и готовые записи («записи старше 30 суток»).
        Telegram-сообщения не удаляются. P3: ожидаемая реализация.
        """
        name, fn = _cleanup_function()
        self.assertIsNotNone(
            fn,
            "нет серверной очистки анонсов: ожидается функция "
            f"{CLEANUP_NAMES} в announcement_store (P3: ожидаемая реализация)",
        )

        await h.text("/start")
        now = utcnow()
        fresh_29 = _seed_sent(h, now - timedelta(days=29))
        edge_30 = _seed_sent(h, now - timedelta(days=30))
        old_31 = _seed_sent(h, now - timedelta(days=31))
        draft_29 = _seed_draft_rows(h, count=1, start_at=now - timedelta(days=29))[0]
        draft_31 = _seed_draft_rows(h, count=1, start_at=now - timedelta(days=31))[0]
        deletes_before = len(h.deletes())

        with h.session_factory() as session:
            kwargs = _cleanup_kwargs(fn, session, now)
            result = fn(**kwargs)
            if inspect.isawaitable(result):
                result = asyncio.run(result)

        self.assertTrue(
            _exists(h, fresh_29),
            f"{name}: готовая запись 29 суток должна остаться (граница >= 30); "
            f"фактически: {_records(h)}",
        )
        self.assertFalse(
            _exists(h, edge_30),
            f"{name}: запись ровно 30 суток должна удаляться "
            f"(семантика >= 30 суток); фактически: {_records(h)}",
        )
        self.assertFalse(
            _exists(h, old_31),
            f"{name}: запись 31-суточной давности должна удаляться; "
            f"фактически: {_records(h)}",
        )
        self.assertTrue(
            _exists(h, draft_29),
            f"{name}: черновик 29 суток должен остаться; фактически: {_records(h)}",
        )
        self.assertFalse(
            _exists(h, draft_31),
            f"{name}: черновик старше 30 суток должен удаляться; "
            f"фактически: {_records(h)}",
        )
        self.assertEqual(
            len(h.deletes()),
            deletes_before,
            "очистка БД не должна удалять Telegram-сообщения "
            f"(delete_message): {h.deleted_ids()}",
        )

    @scenario_test
    async def test_d9_cleanup_is_registered_in_build_application(self, h):
        """
        D.9: очистка зарегистрирована при сборке приложения.

        Принимаются два контракта (P3: ожидаемая реализация):
        1) `application.job_queue` содержит job, чей callback связан с
           очисткой, с повтором не реже раза в час (или запуском при старте);
        2) `post_init` приложения ссылается на очистку (например,
           планирует её) — используется, когда APScheduler недоступен.
        Иначе — падение с указанием ожидаемого контракта.
        """
        app = h.app
        evidence = _cleanup_registration(app)
        self.assertIsNotNone(
            evidence,
            "очистка анонсов не зарегистрирована в build_application: "
            "ожидается JobQueue-job с callback очистки (повтор ≤ 1 часа) "
            "или post_init-регистрация; "
            f"job_queue={getattr(app, '_job_queue', None)!r}, "
            f"post_init={getattr(app, '_post_init', None)!r}",
        )
        interval = _cleanup_interval(app)
        if interval is not None:
            self.assertLessEqual(
                interval,
                3600.0,
                f"очистка должна запускаться не реже раза в час: {interval} с",
            )


def _mentions_cleanup(obj) -> bool:
    if obj is None:
        return False
    parts: list[str] = []
    for attr in ("__qualname__", "__name__", "__module__"):
        value = getattr(obj, attr, None)
        if isinstance(value, str):
            parts.append(value)
    func = getattr(obj, "func", None)
    if func is not None and _mentions_cleanup(func):
        parts.append("cleanup")
    try:
        parts.append(inspect.getsource(obj))
    except Exception:  # noqa: BLE001 — источников может не быть (C-функции)
        pass
    try:
        parts.append(repr(obj))
    except Exception:  # noqa: BLE001
        pass
    joined = " ".join(parts).lower()
    return (
        "cleanup" in joined
        or "clean_old" in joined
        or "ochistk" in joined
        or "очистк" in joined
    )


def _cleanup_registration(app) -> str | None:
    """Доказательство регистрации очистки (строкой) или None."""
    queue = getattr(app, "_job_queue", None)
    if queue is not None:
        try:
            jobs = queue.jobs()
        except Exception:  # noqa: BLE001 — очередь может не стартовать
            jobs = ()
        for job in jobs:
            if _mentions_cleanup(getattr(job, "callback", None)):
                return f"job_queue: {getattr(job, 'name', '?')}"
    for attr in ("post_init", "_post_init"):
        hook = getattr(app, attr, None)
        if hook is not None and _mentions_cleanup(hook):
            return f"{attr}: {hook!r}"
    return None


def _cleanup_interval(app) -> float | None:
    """Период повтора job очистки в секундах (None — не удалось определить)."""
    queue = getattr(app, "_job_queue", None)
    if queue is None:
        return None
    try:
        jobs = queue.jobs()
    except Exception:  # noqa: BLE001
        return None
    for job in jobs:
        if not _mentions_cleanup(getattr(job, "callback", None)):
            continue
        trigger = getattr(job, "trigger", None)
        interval = getattr(trigger, "interval", None)
        total = getattr(interval, "total_seconds", None)
        if callable(total):
            return float(total())
    return None
