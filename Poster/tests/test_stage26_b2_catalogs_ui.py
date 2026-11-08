# tests/test_stage26_b2_catalogs_ui.py
"""
«Лекало» 2.6, этап B2 (§5.2, §6–8, §19.3–19.4): интерфейс справочников
кураторов и подвалов.

Проверяется пользовательский контракт, а не внутренние функции: сценарии
идут через настоящее приложение (`bot.build_application`), FakeBot и
временную SQLite — рабочая `post_bot.db`, `.env` и сеть Telegram не
используются.

Что закрепляется:

- список: строка `[значение] [🗑]`, пагинация, пустые тексты, «Добавить»,
  «Архив», «Назад»; добавление без предпросмотра и подтверждений;
- добавление куратора сохраняет форматирование (карточка показывает HTML);
- подвал: ровно два вопроса, отмена до второго не оставляет записи;
- карточки: полный контакт / название и текст, правка без подтверждений;
- архив: подтверждение «Отмена»/«В архив», восстановление одним нажатием,
  архивные не попадают в активный список;
- права проверяются на каждом callback и на введённом тексте;
- справочники площадок не смешиваются, личные — изолированы по владельцу;
- видимость кнопок входа: площадка — формула §6 (простой ИЛИ (включён
  расширенный И расширенный)), личные — кирпичик хотя бы в одном личном
  шаблоне; скрытие кнопки не удаляет записи;
- «Назад»: площадка → меню площадки, личный → «Создать по личному шаблону».

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage26_b2_catalogs_ui -v
"""

from __future__ import annotations

import unittest

from telegram import MessageEntity

from tests import ui_texts_expect as cat

try:

    from catalogs_store import count_curators, count_footers
    from models import PlaceAdmin, Template, utcnow
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
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)

ADMIN_TG = 26_021_001
OTHER_TG = 26_021_002
STRANGER_TG = 26_021_003

CURATORS_LABEL = cat.text("buttons.catalogs.open_curators")
FOOTERS_LABEL = cat.text("buttons.catalogs.open_footers")
BACK_LABEL = cat.text("buttons.nav.back")
ADD_CURATOR_LABEL = cat.text("buttons.catalogs.add_curator")
ADD_FOOTER_LABEL = cat.text("buttons.catalogs.add_footer")
ARCHIVE_LABEL = cat.text("buttons.catalogs.archive")
RESTORE_LABEL = cat.text("buttons.catalogs.restore")
CANCEL_LABEL = cat.text("buttons.catalogs.cancel")
TO_ARCHIVE_LABEL = cat.text("buttons.catalogs.to_archive")
TRASH_LABEL = cat.text("buttons.catalogs.delete")
EDIT_LABEL = cat.text("buttons.catalogs.edit")
EDIT_TEXT_LABEL = cat.text("buttons.catalogs.edit_text")
EDIT_NAME_LABEL = cat.text("buttons.catalogs.edit_name")

CURATORS_TITLE = cat.text("catalogs.curators.title")
FOOTERS_TITLE = cat.text("catalogs.footers.title")
CURATORS_EMPTY = cat.text("catalogs.curators.empty")
FOOTERS_EMPTY = cat.text("catalogs.footers.empty")
CURATOR_ADD_PROMPT = cat.text("catalogs.curator.add_prompt")
CURATOR_EDIT_PROMPT = cat.text("catalogs.curator.edit_prompt")
FOOTER_TEXT_PROMPT = cat.text("catalogs.footer.text_prompt")
FOOTER_NAME_PROMPT = cat.text("catalogs.footer.name_prompt")
FOOTER_TEXT_EDIT_PROMPT = cat.text("catalogs.footer.text_edit_prompt")
FOOTER_NAME_EDIT_PROMPT = cat.text("catalogs.footer.name_edit_prompt")
DENIED_TEXT = cat.text("catalogs.denied")
STALE_TEXT = cat.text("catalogs.stale")


# --- Помощники ---------------------------------------------------------------


def _rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _buttons(h: Harness) -> list[tuple[str, str | None]]:
    return h.screen_buttons()


def _callbacks(h: Harness) -> list[str]:
    return [data or "" for _label, data in _buttons(h)]


def _labels(h: Harness) -> list[str]:
    return [label for label, _data in _buttons(h)]


def _alert(h: Harness) -> str:
    """Текст последнего всплывающего ответа на нажатие."""
    answers = h.answers()
    if not answers:
        return ""
    record = answers[-1]
    text = record.get("text")
    if text is None:
        args = tuple(record.get("args") or ())
        text = args[0] if args and isinstance(args[0], str) else None
    return str(text or "")


def _utf16_span(text: str, label: str, start: int = 0) -> tuple[int, int]:
    """Смещение и длина в код-юнитах UTF-16 — так их считает Telegram."""
    index = text.index(label, start)
    offset = len(text[:index].encode("utf-16-le")) // 2
    return offset, len(label.encode("utf-16-le")) // 2


def _seed_place(
    h: Harness,
    name: str,
    *,
    admin_tgs: tuple[int, ...] = (ADMIN_TG,),
) -> int:
    """Площадка с администраторами; создатель — первый из списка."""
    with h.session_factory() as session:
        users = [get_or_create_user(session, tg, username=f"u{tg}") for tg in admin_tgs]
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=users[0].id if users else None,
        )
        for user in users:
            session.add(
                PlaceAdmin(place_id=place.id, user_id=user.id, assigned_at=utcnow())
            )
        session.commit()
        return int(place.id)


def _enable_extended(h: Harness, place_id: int, *, tg: int = ADMIN_TG) -> None:
    """Включает стандартный расширенный вариант: в нём есть [куратор]/[подвал]."""
    with h.session_factory() as session:
        template = get_standard_template(session, tg, place_id)
        set_standard_extended_active(
            session,
            actor_telegram_user_id=tg,
            template_id=template.template_id,
            active=True,
            expected_revision=template.revision,
        )
        session.commit()


def _set_standard_texts(
    h: Harness,
    place_id: int,
    *,
    content: str,
    extended_content: str | None,
    tg: int = ADMIN_TG,
) -> None:
    with h.session_factory() as session:
        template = get_standard_template(session, tg, place_id)
        result = update_template_content(
            session,
            actor_telegram_user_id=tg,
            template_id=template.template_id,
            expected_revision=template.revision,
            content=content,
            extended_content=extended_content,
        )
        assert result.ok, result.message
        session.commit()


def _count(h: Harness, kind: str, place_id: int | None, *, tg: int, status: str):
    with h.session_factory() as session:
        if kind == "curator":
            return count_curators(
                session,
                actor_telegram_user_id=tg,
                place_id=place_id,
                status=status,
            )
        return count_footers(
            session,
            actor_telegram_user_id=tg,
            place_id=place_id,
            status=status,
        )


async def _open_place(h: Harness, place_id: int, *, tg: int = ADMIN_TG) -> None:
    await h.text("/admin", user_id=tg)
    await h.callback(f"adm_place:{place_id}", user_id=tg)


async def _add_curator(
    h: Harness,
    place_id: int | None,
    value: str,
    *,
    tg: int = ADMIN_TG,
    entities: list[MessageEntity] | None = None,
) -> None:
    scope = "me" if place_id is None else f"p{place_id}"
    await h.callback(f"cat_add:curator:{scope}", user_id=tg)
    await h.text(value, user_id=tg, entities=entities)


async def _add_footer(
    h: Harness,
    place_id: int | None,
    *,
    name: str,
    text: str,
    tg: int = ADMIN_TG,
    entities: list[MessageEntity] | None = None,
) -> None:
    scope = "me" if place_id is None else f"p{place_id}"
    await h.callback(f"cat_add:footer:{scope}", user_id=tg)
    await h.text(text, user_id=tg, entities=entities)
    await h.text(name, user_id=tg)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CatalogPlaceTests(unittest.TestCase):
    """§7–8 через меню площадки: списки, карточки, архив, права."""

    @scenario_test
    async def test_curator_add_updates_list_without_confirmation(self, h) -> None:
        place_id = _seed_place(h, "Дом культуры")
        _enable_extended(h, place_id)
        await _open_place(h, place_id)
        self.assertIn(f"cat_open:curator:p{place_id}", _callbacks(h))
        self.assertIn(f"cat_open:footer:p{place_id}", _callbacks(h))

        await h.callback(f"cat_open:curator:p{place_id}", user_id=ADMIN_TG)
        self.assertEqual(
            h.screen_text(), f"{CURATORS_TITLE}\n\n{CURATORS_EMPTY}", h.screen_text()
        )
        # Пустой список не мешает добавлению.
        self.assertIn(ADD_CURATOR_LABEL, _labels(h))
        self.assertEqual(
            _callbacks(h)[-3:],
            [
                f"cat_add:curator:p{place_id}",
                f"cat_arch:curator:p{place_id}",
                f"cat_home:curator:p{place_id}",
            ],
        )

        screen_before = h.screen_message_id()
        await h.callback(f"cat_add:curator:p{place_id}", user_id=ADMIN_TG)
        self.assertEqual(h.screen_text(), CURATOR_ADD_PROMPT)
        replies_before = len(h.reply_texts())

        value = "Иван Петров @ivan"
        offset, length = _utf16_span(value, "@ivan")
        await h.text(
            value,
            user_id=ADMIN_TG,
            entities=[
                MessageEntity(type=MessageEntity.BOLD, offset=offset, length=length)
            ],
        )

        # Сразу список с новой записью: без предпросмотра и «сохранено».
        self.assertEqual(h.screen_text(), CURATORS_TITLE)
        self.assertEqual(len(h.reply_texts()), replies_before)
        self.assertEqual(
            h.screen_message_id(), screen_before, "экран не переиспользован"
        )
        self.assertIn((value, f"cat_card:curator:p{place_id}:1"), _buttons(h))
        self.assertIn((TRASH_LABEL, f"cat_del:curator:p{place_id}:1"), _buttons(h))

        # Полное значение с форматированием живёт в карточке.
        await h.callback(f"cat_card:curator:p{place_id}:1", user_id=ADMIN_TG)
        self.assertIn("Иван Петров", h.screen_text())
        self.assertIn("<b>@ivan</b>", h.screen_text())
        self.assertEqual(_callbacks(h)[:1], [f"cat_edit:curator:p{place_id}"])
        self.assertIn(f"cat_back:curator:p{place_id}", _callbacks(h))

        # «Назад» из карточки — тот же список.
        await h.callback(f"cat_back:curator:p{place_id}", user_id=ADMIN_TG)
        self.assertIn(value, _labels(h))

    @scenario_test
    async def test_curator_edit_returns_to_card_without_confirmation(self, h) -> None:
        place_id = _seed_place(h, "Клуб")
        _enable_extended(h, place_id)
        await _open_place(h, place_id)
        await h.callback(f"cat_open:curator:p{place_id}", user_id=ADMIN_TG)
        await _add_curator(h, place_id, "старый @old")
        await h.callback(f"cat_card:curator:p{place_id}:1", user_id=ADMIN_TG)
        await h.callback(f"cat_edit:curator:p{place_id}", user_id=ADMIN_TG)
        self.assertEqual(h.screen_text(), CURATOR_EDIT_PROMPT)

        await h.text("новый @new", user_id=ADMIN_TG)
        self.assertEqual(h.screen_text(), "новый @new")
        self.assertNotIn("сохран", h.screen_text().lower())
        # Карточка обновилась, список тоже.
        await h.callback(f"cat_back:curator:p{place_id}", user_id=ADMIN_TG)
        self.assertIn("новый @new", _labels(h))
        self.assertNotIn("старый @old", _labels(h))

    @scenario_test
    async def test_curator_archive_confirm_and_single_tap_restore(self, h) -> None:
        place_id = _seed_place(h, "Театр")
        _enable_extended(h, place_id)
        await _open_place(h, place_id)
        await h.callback(f"cat_open:curator:p{place_id}", user_id=ADMIN_TG)
        await _add_curator(h, place_id, "@curator")

        await h.callback(f"cat_del:curator:p{place_id}:1", user_id=ADMIN_TG)
        self.assertEqual(
            h.screen_text(),
            cat.text("catalogs.curators.archive_confirm", value="@curator"),
        )
        self.assertEqual(
            [
                (CANCEL_LABEL, f"cat_back:curator:p{place_id}"),
                (TO_ARCHIVE_LABEL, f"cat_delyes:curator:p{place_id}:1"),
            ],
            _buttons(h),
        )
        # «Отмена» — запись остаётся активной.
        await h.callback(f"cat_back:curator:p{place_id}", user_id=ADMIN_TG)
        self.assertIn("@curator", _labels(h))
        self.assertEqual(
            _count(h, "curator", place_id, tg=ADMIN_TG, status="active"), 1
        )

        await h.callback(f"cat_del:curator:p{place_id}:1", user_id=ADMIN_TG)
        await h.callback(f"cat_delyes:curator:p{place_id}:1", user_id=ADMIN_TG)
        self.assertEqual(
            h.screen_text(), f"{CURATORS_TITLE}\n\n{CURATORS_EMPTY}", h.screen_text()
        )
        self.assertEqual(
            _count(h, "curator", place_id, tg=ADMIN_TG, status="active"), 0
        )
        self.assertEqual(
            _count(h, "curator", place_id, tg=ADMIN_TG, status="archived"), 1
        )

        await h.callback(f"cat_arch:curator:p{place_id}", user_id=ADMIN_TG)
        self.assertIn(cat.text("catalogs.curators.archive_title"), h.screen_text())
        self.assertIn(
            ("@curator", f"cat_card:curator:p{place_id}:1"),
            [(label, data) for label, data in _buttons(h)],
        )
        self.assertIn((RESTORE_LABEL, f"cat_rest:curator:p{place_id}:1"), _buttons(h))
        # Из архива «Назад» ведёт в активный список, а не в меню площадки.
        self.assertIn((BACK_LABEL, f"cat_back:curator:p{place_id}"), _buttons(h))

        # Восстановление — одно нажатие, без подтверждения.
        await h.callback(f"cat_rest:curator:p{place_id}:1", user_id=ADMIN_TG)
        self.assertIn(cat.text("catalogs.curators.archive_empty"), h.screen_text())
        self.assertNotIn(RESTORE_LABEL, _labels(h))
        await h.callback(f"cat_back:curator:p{place_id}", user_id=ADMIN_TG)
        self.assertIn("@curator", _labels(h))

    @scenario_test
    async def test_archive_card_returns_to_archive_and_offers_restore(self, h) -> None:
        place_id = _seed_place(h, "Музей")
        _enable_extended(h, place_id)
        await _open_place(h, place_id)
        await h.callback(f"cat_open:curator:p{place_id}", user_id=ADMIN_TG)
        await _add_curator(h, place_id, "@arch")
        await h.callback(f"cat_del:curator:p{place_id}:1", user_id=ADMIN_TG)
        await h.callback(f"cat_delyes:curator:p{place_id}:1", user_id=ADMIN_TG)
        await h.callback(f"cat_arch:curator:p{place_id}", user_id=ADMIN_TG)

        await h.callback(f"cat_card:curator:p{place_id}:1", user_id=ADMIN_TG)
        # Архивная карточка не предлагает правку (её запрещает хранилище).
        self.assertNotIn(EDIT_LABEL, _labels(h))
        self.assertIn((RESTORE_LABEL, f"cat_rest:curator:p{place_id}:1"), _buttons(h))

        await h.callback(f"cat_back:curator:p{place_id}", user_id=ADMIN_TG)
        self.assertIn(cat.text("catalogs.curators.archive_title"), h.screen_text())
        self.assertIn((RESTORE_LABEL, f"cat_rest:curator:p{place_id}:1"), _buttons(h))

    @scenario_test
    async def test_footer_two_questions_and_cancel_leaves_no_record(self, h) -> None:
        place_id = _seed_place(h, "Арт-площадка")
        _enable_extended(h, place_id)
        await _open_place(h, place_id)
        await h.callback(f"cat_open:footer:p{place_id}", user_id=ADMIN_TG)
        self.assertEqual(
            h.screen_text(), f"{FOOTERS_TITLE}\n\n{FOOTERS_EMPTY}", h.screen_text()
        )
        self.assertIn(ADD_FOOTER_LABEL, _labels(h))

        await h.callback(f"cat_add:footer:p{place_id}", user_id=ADMIN_TG)
        self.assertEqual(h.screen_text(), FOOTER_TEXT_PROMPT)
        await h.text("Вход со двора, донат в баночку", user_id=ADMIN_TG)
        self.assertEqual(h.screen_text(), FOOTER_NAME_PROMPT)
        await h.callback(f"cat_cancel:footer:p{place_id}", user_id=ADMIN_TG)

        self.assertEqual(
            h.screen_text(), f"{FOOTERS_TITLE}\n\n{FOOTERS_EMPTY}", h.screen_text()
        )
        self.assertEqual(_count(h, "footer", place_id, tg=ADMIN_TG, status="active"), 0)
        self.assertEqual(
            _count(h, "footer", place_id, tg=ADMIN_TG, status="archived"), 0
        )

    @scenario_test
    async def test_footer_card_edits_text_and_name(self, h) -> None:
        place_id = _seed_place(h, "Лофт")
        _enable_extended(h, place_id)
        await _open_place(h, place_id)
        await h.callback(f"cat_open:footer:p{place_id}", user_id=ADMIN_TG)
        text = "Сменная обувь обязательна"
        offset, length = _utf16_span(text, "обязательна")
        await _add_footer(
            h,
            place_id,
            name="Обувь",
            text=text,
            entities=[
                MessageEntity(type=MessageEntity.BOLD, offset=offset, length=length)
            ],
        )
        # Сразу список: подпись — название, рядом корзина.
        self.assertEqual(h.screen_text(), FOOTERS_TITLE)
        self.assertIn(("Обувь", f"cat_card:footer:p{place_id}:1"), _buttons(h))

        await h.callback(f"cat_card:footer:p{place_id}:1", user_id=ADMIN_TG)
        self.assertIn("Обувь", h.screen_text())
        self.assertIn("Сменная обувь", h.screen_text())
        self.assertIn("<b>обязательна</b>", h.screen_text())
        self.assertIn(EDIT_TEXT_LABEL, _labels(h))
        self.assertIn(EDIT_NAME_LABEL, _labels(h))

        await h.callback(f"cat_etext:footer:p{place_id}", user_id=ADMIN_TG)
        self.assertEqual(h.screen_text(), FOOTER_TEXT_EDIT_PROMPT)
        await h.text("Новый текст подвала", user_id=ADMIN_TG)
        self.assertIn("Новый текст подвала", h.screen_text())

        await h.callback(f"cat_ename:footer:p{place_id}", user_id=ADMIN_TG)
        self.assertEqual(h.screen_text(), FOOTER_NAME_EDIT_PROMPT)
        await h.text("Новое название", user_id=ADMIN_TG)
        self.assertIn("Новое название", h.screen_text())
        self.assertIn("Новый текст подвала", h.screen_text())

        await h.callback(f"cat_back:footer:p{place_id}", user_id=ADMIN_TG)
        self.assertIn("Новое название", _labels(h))

    @scenario_test
    async def test_footer_duplicate_name_is_reported_in_place(self, h) -> None:
        place_id = _seed_place(h, "Сцена")
        _enable_extended(h, place_id)
        await _open_place(h, place_id)
        await h.callback(f"cat_open:footer:p{place_id}", user_id=ADMIN_TG)
        await _add_footer(h, place_id, name="Донат", text="Карта рядом")

        await h.callback(f"cat_add:footer:p{place_id}", user_id=ADMIN_TG)
        await h.text("Второй текст", user_id=ADMIN_TG)
        await h.text("донат", user_id=ADMIN_TG)
        # Остаёмся на шаге названия с понятной подсказкой, без второй записи.
        self.assertIn(FOOTER_NAME_PROMPT, h.screen_text())
        self.assertIn(cat.text("catalogs.errors.footer_name_taken"), h.screen_text())
        self.assertEqual(_count(h, "footer", place_id, tg=ADMIN_TG, status="active"), 1)

    @scenario_test
    async def test_pagination_and_kind_independence(self, h) -> None:
        place_id = _seed_place(h, "Большой зал")
        _enable_extended(h, place_id)
        await _open_place(h, place_id)
        await h.callback(f"cat_open:curator:p{place_id}", user_id=ADMIN_TG)
        for number in range(1, 8):
            await _add_curator(h, place_id, f"контакт {number}")

        # После добавления показана последняя страница (там новая запись).
        self.assertIn("контакт 7", _labels(h))
        await h.callback(f"cat_page:curator:p{place_id}:0", user_id=ADMIN_TG)

        rows = _rows(h)
        # Пять записей на страницу — ровно 5 строк-записей + навигация.
        record_rows = [
            row for row in rows if len(row) == 2 and row[1][0] == TRASH_LABEL
        ]
        self.assertEqual(len(record_rows), 5, rows)
        nav = [row for row in rows if row and row[0][0].startswith("1/2")]
        self.assertEqual(len(nav), 1, rows)
        self.assertIn("контакт 1", _labels(h))
        self.assertNotIn("контакт 6", _labels(h))

        await h.callback(f"cat_page:curator:p{place_id}:5", user_id=ADMIN_TG)
        self.assertIn("контакт 6", _labels(h))
        self.assertIn("контакт 7", _labels(h))
        self.assertNotIn("контакт 1", _labels(h))

        # Подвалы — отдельный справочник, записи кураторов в нём нет.
        await h.callback(f"cat_home:curator:p{place_id}", user_id=ADMIN_TG)
        await h.callback(f"cat_open:footer:p{place_id}", user_id=ADMIN_TG)
        self.assertIn(FOOTERS_EMPTY, h.screen_text())

    @scenario_test
    async def test_foreign_user_gets_denied_on_every_step(self, h) -> None:
        place_id = _seed_place(h, "Чужое место")
        _enable_extended(h, place_id)
        await _open_place(h, place_id)
        await h.callback(f"cat_open:curator:p{place_id}", user_id=ADMIN_TG)
        await _add_curator(h, place_id, "@secret")

        with h.session_factory() as session:
            get_or_create_user(session, STRANGER_TG, username="stranger")
            session.commit()

        # Открытие: прав нет — явный отказ, содержимое не раскрывается.
        await h.callback(f"cat_open:curator:p{place_id}", user_id=STRANGER_TG)
        self.assertEqual(_alert(h), DENIED_TEXT)
        self.assertNotIn("@secret", h.screen_text())

        # Прямое нажатие «в архив» из чужого чата: состояние не создано.
        await h.callback(f"cat_del:curator:p{place_id}:1", user_id=STRANGER_TG)
        self.assertEqual(_alert(h), STALE_TEXT)
        self.assertEqual(
            _count(h, "curator", place_id, tg=ADMIN_TG, status="active"), 1
        )

        # Ввод текста без состояния справочника не создаёт записей.
        await h.text("взлом", user_id=STRANGER_TG)
        self.assertEqual(
            _count(h, "curator", place_id, tg=ADMIN_TG, status="active"), 1
        )

    @scenario_test
    async def test_two_places_do_not_share_records(self, h) -> None:
        first = _seed_place(h, "Первый зал", admin_tgs=(ADMIN_TG,))
        second = _seed_place(h, "Второй зал", admin_tgs=(ADMIN_TG,))
        _enable_extended(h, first)
        _enable_extended(h, second)

        await _open_place(h, first)
        await h.callback(f"cat_open:curator:p{first}", user_id=ADMIN_TG)
        await _add_curator(h, first, "@first-only")

        await h.callback(f"cat_home:curator:p{first}", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{second}", user_id=ADMIN_TG)
        await h.callback(f"cat_open:curator:p{second}", user_id=ADMIN_TG)
        self.assertIn(CURATORS_EMPTY, h.screen_text())
        self.assertNotIn("@first-only", _labels(h))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CatalogVisibilityTests(unittest.TestCase):
    """§6 и §5.2: когда кнопки входа видны."""

    @scenario_test
    async def test_place_buttons_follow_extended_flag_and_texts(self, h) -> None:
        place_id = _seed_place(h, "Новая площадка", admin_tgs=(ADMIN_TG,))

        # Простой вариант стандарта не содержит [куратор] и [подвал].
        await _open_place(h, place_id)
        self.assertNotIn(CURATORS_LABEL, _labels(h))
        self.assertNotIn(FOOTERS_LABEL, _labels(h))

        # Включён стандартный расширенный — обе кнопки на одной строке.
        _enable_extended(h, place_id)
        await h.callback("adm_places", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        rows = _rows(h)
        catalogs_rows = [
            row
            for row in rows
            if [label for label, _d in row] == [CURATORS_LABEL, FOOTERS_LABEL]
        ]
        self.assertEqual(len(catalogs_rows), 1, rows)
        self.assertEqual(
            [data for _l, data in catalogs_rows[0]],
            [f"cat_open:curator:p{place_id}", f"cat_open:footer:p{place_id}"],
        )
        self.assertLessEqual(max(len(row) for row in rows), 2, rows)

        # Из расширенного убрали [куратор] — видна только кнопка подвалов.
        _set_standard_texts(
            h,
            place_id,
            content="**[название]**\n\nМероприятие ведёт: [контакт]",
            extended_content="**[название]**\n\n[подвал]",
        )
        await h.callback("adm_places", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        self.assertNotIn(CURATORS_LABEL, _labels(h))
        self.assertIn(FOOTERS_LABEL, _labels(h))

        # Кирпичик в простом варианте виден независимо от переключателя.
        with h.session_factory() as session:
            template = get_standard_template(session, ADMIN_TG, place_id)
            set_standard_extended_active(
                session,
                actor_telegram_user_id=ADMIN_TG,
                template_id=template.template_id,
                active=False,
                expected_revision=template.revision,
            )
            session.commit()
        _set_standard_texts(
            h,
            place_id,
            content="**[название]**\n\nКуратор от площадки: [куратор]",
            extended_content="**[название]**\n\n[подвал]",
        )
        await h.callback("adm_places", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        self.assertIn(CURATORS_LABEL, _labels(h))
        self.assertNotIn(FOOTERS_LABEL, _labels(h))

    @scenario_test
    async def test_personal_buttons_follow_personal_templates(self, h) -> None:
        with h.session_factory() as session:
            get_or_create_user(session, ADMIN_TG, username="owner")
            create_personal_template(
                session,
                actor_telegram_user_id=ADMIN_TG,
                name="Без кирпичиков",
                content="**[название]**",
            )
            session.commit()

        await h.text("/start", user_id=ADMIN_TG)
        await h.callback("pick_place", user_id=ADMIN_TG)
        await h.callback("pick_template", user_id=ADMIN_TG)
        self.assertNotIn(CURATORS_LABEL, _labels(h))
        self.assertNotIn(FOOTERS_LABEL, _labels(h))
        back_rows = [
            row for row in _rows(h) if [label for label, _d in row] == [BACK_LABEL]
        ]
        self.assertTrue(back_rows, _rows(h))

        # Один шаблон с [куратор] — только «Кураторы».
        with h.session_factory() as session:
            create_personal_template(
                session,
                actor_telegram_user_id=ADMIN_TG,
                name="С куратором",
                content="**[название]**\n\nКуратор: [куратор]",
            )
            session.commit()
        await h.callback("tp_pick_back", user_id=ADMIN_TG)
        await h.callback("pick_template", user_id=ADMIN_TG)
        self.assertIn(CURATORS_LABEL, _labels(h))
        self.assertNotIn(FOOTERS_LABEL, _labels(h))

        # Второй шаблон с [подвал] — обе кнопки на одной строке.
        with h.session_factory() as session:
            create_personal_template(
                session,
                actor_telegram_user_id=ADMIN_TG,
                name="С подвалом",
                content="**[название]**\n\n[подвал]",
            )
            session.commit()
        await h.callback("tp_pick_back", user_id=ADMIN_TG)
        await h.callback("pick_template", user_id=ADMIN_TG)
        rows = _rows(h)
        catalogs_rows = [
            row
            for row in rows
            if [label for label, _d in row] == [CURATORS_LABEL, FOOTERS_LABEL]
        ]
        self.assertEqual(len(catalogs_rows), 1, rows)
        self.assertEqual(
            [data for _l, data in catalogs_rows[0]],
            ["cat_open:curator:me", "cat_open:footer:me"],
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CatalogPersonalTests(unittest.TestCase):
    """§5.2, §19.4: личные справочники и их изоляция."""

    async def _open_personal(self, h: Harness, *, tg: int = ADMIN_TG) -> None:
        await h.text("/start", user_id=tg)
        await h.callback("pick_place", user_id=tg)
        await h.callback("pick_template", user_id=tg)

    @scenario_test
    async def test_personal_catalog_full_flow_and_back_to_picker(self, h) -> None:
        with h.session_factory() as session:
            get_or_create_user(session, ADMIN_TG, username="owner")
            create_personal_template(
                session,
                actor_telegram_user_id=ADMIN_TG,
                name="Мой шаблон",
                content="**[название]**\n\nКуратор: [куратор]\n\n[подвал]",
            )
            session.commit()

        await self._open_personal(h)
        # Личный сценарий не отправляет к администратору площадки.
        self.assertFalse(
            [data for data in _callbacks(h) if data.startswith("adm_")],
            _callbacks(h),
        )

        await h.callback("cat_open:curator:me", user_id=ADMIN_TG)
        self.assertIn(CURATORS_EMPTY, h.screen_text())
        await _add_curator(h, None, "личный @me")
        self.assertIn("личный @me", _labels(h))

        # «Назад» возвращает на экран «Создать по личному шаблону».
        await h.callback("cat_home:curator:me", user_id=ADMIN_TG)
        self.assertIn("Мой шаблон", h.screen_text())
        self.assertIn("tp_pick:1", _callbacks(h))

        # Подвал в личном справочнике: два вопроса и карточка.
        await h.callback("cat_open:footer:me", user_id=ADMIN_TG)
        self.assertIn(FOOTERS_EMPTY, h.screen_text())
        await _add_footer(h, None, name="Личный", text="Личный текст")
        self.assertIn("Личный", _labels(h))
        await h.callback("cat_card:footer:me:1", user_id=ADMIN_TG)
        self.assertIn("Личный текст", h.screen_text())
        await h.callback("cat_back:footer:me", user_id=ADMIN_TG)
        await h.callback("cat_home:footer:me", user_id=ADMIN_TG)
        self.assertIn("Мой шаблон", h.screen_text())

    @scenario_test
    async def test_personal_catalogs_are_isolated_between_users(self, h) -> None:
        for tg, name in ((ADMIN_TG, "Свой"), (OTHER_TG, "Чужой")):
            with h.session_factory() as session:
                get_or_create_user(session, tg, username=f"u{tg}")
                create_personal_template(
                    session,
                    actor_telegram_user_id=tg,
                    name=name,
                    content="**[название]**\n\nКуратор: [куратор]",
                )
                session.commit()

        await self._open_personal(h)
        await h.callback("cat_open:curator:me", user_id=ADMIN_TG)
        await _add_curator(h, None, "@owner-entry")
        await h.callback("cat_home:curator:me", user_id=ADMIN_TG)

        await self._open_personal(h, tg=OTHER_TG)
        await h.callback("cat_open:curator:me", user_id=OTHER_TG)
        self.assertIn(CURATORS_EMPTY, h.screen_text())
        self.assertNotIn("@owner-entry", _labels(h))

        # Чужой личный справочник недоступен даже по прямому callback:
        # содержимое не раскрывается, отказ явный (§18).
        await h.callback("cat_card:curator:me:1", user_id=OTHER_TG)
        self.assertEqual(_alert(h), DENIED_TEXT)
        self.assertNotIn("@owner-entry", h.screen_text())

        self.assertEqual(_count(h, "curator", None, tg=ADMIN_TG, status="active"), 1)
        self.assertEqual(_count(h, "curator", None, tg=OTHER_TG, status="active"), 0)

    @scenario_test
    async def test_hidden_buttons_keep_personal_records(self, h) -> None:
        with h.session_factory() as session:
            get_or_create_user(session, ADMIN_TG, username="owner")
            template = create_personal_template(
                session,
                actor_telegram_user_id=ADMIN_TG,
                name="С куратором",
                content="**[название]**\n\nКуратор: [куратор]",
            )
            session.commit()
            template_id = template.template_id

        await self._open_personal(h)
        await h.callback("cat_open:curator:me", user_id=ADMIN_TG)
        await _add_curator(h, None, "@kept")
        await h.callback("cat_home:curator:me", user_id=ADMIN_TG)

        # Кирпичик убран из единственного личного шаблона — кнопка исчезла.
        with h.session_factory() as session:
            current = session.get(Template, template_id)
            assert current is not None
            result = update_template_content(
                session,
                actor_telegram_user_id=ADMIN_TG,
                template_id=template_id,
                expected_revision=current.revision,
                content="**[название]**",
            )
            assert result.ok, result.message
            session.commit()

        await h.callback("tp_pick_back", user_id=ADMIN_TG)
        await h.callback("pick_template", user_id=ADMIN_TG)
        self.assertNotIn(CURATORS_LABEL, _labels(h))
        # Запись осталась в справочнике.
        self.assertEqual(_count(h, "curator", None, tg=ADMIN_TG, status="active"), 1)


if __name__ == "__main__":  # pragma: no cover — ручной запуск
    unittest.main()
