# tests/test_poster2_24_p2_wizard_tester.py
"""
Poster 2.4, этап P2 — независимые тесты Tester (тест-до-кода).

Проверяются требования владельца к конструктору анонса:

IV. Экран «🧩 Дополнительные поля» (`aw_extras`) открывается СРАЗУ списком
    ВСЕХ дополнительных кирпичиков расширенной версии шаблона, включая
    незаполненные (они доступны к заполнению). Кнопки «➕ Добавить поле» и
    «🏠 В меню» на этом экране запрещены; ниже списка — ровно «◀️ Назад».
    Незаполненный кирпичик открывается и заполняется по месту в расширенном
    тексте (не в конце), заполненный — открывается/меняется, у заполненного
    справа компактная корзина; удаление значения не удаляет определение;
    после сохранения возвращаемся к списку; пагинация — только если полей
    много.
V.  «Назад» возвращает на ТОТ экран, с которого пришёл (реальный путь), а
    не в главное меню и не «на шаг раньше по индексу». Пример владельца:
    редактор анонса → «Дополнительные поля» → «Стоимость»;
    «Назад» со стоимости → «Дополнительные поля», ещё «Назад» →
    «Изменить». С 2.5 (§18) экрана «✅ Анонс готов» нет: после доставки
    открыт редактор анонса, «Назад» в старый конструктор запрещён.
VI. На экране вопроса — только «◀️ Назад» | «⏭ Пропустить» (плюс спецкнопки
    дат/времени/выбора). «➡️ Далее» и «✏️ Изменить ответ» запрещены.
    «⏭ Пропустить» НЕ ОЧИЩАЕТ значение: пустое остаётся пустым, заполненное
    сохраняется как есть; сохранение нового ввода заменяет старое; «Назад»
    значение не теряет; пропуск не порождает лишних сообщений и копий
    анонса. Та же семантика — на экране значения доп. поля (`aw_exskip`).

Совместимость: у снимка шаблона без `extended_content` доп. кирпичики НЕ
дописываются в конец канонического текста (legacy append-fallback удалён);
сохранённые значения при этом из базы не исчезают.

Все сценарии идут через настоящее `Application.process_update`, реальные
callback-маршруты, FakeBot и временный SQLite
(`tests.integration_harness`). Прямые доменные вызовы используются только
для подготовки данных (значение/доп. кирпичик в БД, шаблон, место).

Тесты написаны на ТРЕБУЕМОЕ поведение. На текущем коде падают пункты IV
(список и кнопки), VI (Далее/Изменить ответ, семантика «Пропустить»),
навигация «Назад» и legacy-дописывание; зелёные тесты фиксируют уже
правильное (корзина у заполненного, экран завершения, безопасные
устаревшие callback'и).
"""

from __future__ import annotations

import unittest

try:
    from announcement_store import (
        add_announcement_extra,
        get_announcement_draft,
        set_announcement_stage,
    )
    from models import AnnouncementExtra, Brick
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        Harness,
        call_text,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _callbacks,
        _choose_personal,
        _latest_draft,
        _no_photos,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    IMPORT_ERROR = str(exc)


# --- литералы владельца (каталог ui_texts.yaml) ------------------------------

EXTRAS_TITLE = "🧩 Дополнительные поля"
QUESTION_PREFIX = "🧩 Вопрос"
PHOTO_STEP = "📷 Фотографии анонса"
# §18 (2.5): итогового экрана «✅ Анонс готов» нет — после доставки редактор.
DELIVERED_HEAD = "✏️ Изменить анонс"
EDIT_HEAD = "✏️ Изменить анонс"
TEMPLATE_HEAD = "🧩 Выбран шаблон"
MANAGER_HEAD = "🧩 Поля анонса"
MENU_FRAGMENT = "Что хотите сделать?"
INTERNAL_FRAGMENT = "Что-то пошло не так"

LABEL_SKIP = "⏭ Пропустить"
LABEL_BACK = "◀️ Назад"
LABEL_NEXT = "➡️ Далее"
LABEL_EDIT_ANSWER = "✏️ Изменить ответ"
LABEL_ADD_FIELD = "➕ Добавить поле"
LABEL_MENU = "🏠 В меню"
LABEL_DELIVERED_EDIT = "✏️ Изменить"
LABEL_DELIVERED_DATA = "📝 Данные"
LABEL_DELIVERED_PHOTOS = "🖼 Фото (0)"
LABEL_DELIVERED_MENU = "🏠 В меню"
CURRENT_VALUE = "Текущее значение:"

MISSING_FRAGMENT = "не заполнено"


# --- наблюдение за экраном ---------------------------------------------------


def _flat_buttons(h: Harness) -> list[tuple[str, str | None]]:
    """Все inline-кнопки экрана плоским списком (текст, callback_data)."""
    return list(h.screen_buttons())


def _labels(h: Harness) -> list[str]:
    return [label for label, _data in _flat_buttons(h)]


def _datas(h: Harness) -> list[str]:
    return [data for _label, data in _flat_buttons(h) if data is not None]


def _screen_rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    """Матрица inline-клавиатуры: строки и кнопки (для «ниже ровно ...»)."""
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


def _user_texts(h: Harness) -> list[str]:
    """Всё, что видел пользователь: экраны, ответы, подписи, алерты."""
    seen: list[str] = []
    for call in h.calls:
        for value in (call_text(call), call.get("caption")):
            if isinstance(value, str):
                seen.append(value)
    return seen


def _assert_no_internal(test: unittest.TestCase, h: Harness, label: str) -> None:
    internal = [text for text in _user_texts(h) if INTERNAL_FRAGMENT in text]
    test.assertEqual(internal, [], f"{label}: «внутренняя ошибка» вместо экрана")


def _is_back(label: str, data: str | None) -> bool:
    return "Назад" in label or bool(data and "back" in data.lower())


def _is_delete(label: str, data: str | None) -> bool:
    return label.startswith("🗑") or bool(data and data.startswith("aw_exdel"))


def _is_pagination(label: str, data: str | None) -> bool:
    """Кнопка пагинации: стрелка страниц или callback вида *page*."""
    if bool(data and "page" in data.lower()):
        return True
    if label and "Назад" in label:
        return False
    return label.strip() in {"◀️", "▶️", "◀", "▶", "‹", "›"}


def _title_buttons(h: Harness, titles: list[str]) -> dict[str, str]:
    """Карточки доп. кирпичиков: кнопка (не корзина), содержащая заголовок."""
    found: dict[str, str] = {}
    for label, data in _flat_buttons(h):
        if data is None or _is_delete(label, data):
            continue
        for title in titles:
            if title in label and title not in found:
                found[title] = data
    return found


def _require_title_button(
    test: unittest.TestCase, h: Harness, title: str, label: str
) -> str:
    found = _title_buttons(h, [title])
    test.assertIn(
        title,
        found,
        f"{label}: на списке доп. полей нет кирпичика «{title}» — "
        f"список должен сразу показывать ВСЕ доп. кирпичики "
        f"(включая незаполненные). Кнопки: {h.screen_buttons()}; "
        f"экран: {h.screen_text()[:400]!r}",
    )
    return found[title]


# --- данные и подготовка -----------------------------------------------------


def _draft(h: Harness, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _value(h: Harness, draft_id: int, brick_id: int):
    """(state, payload) значения или None, если строки нет."""
    value = _draft(h, draft_id).value_for(brick_id)
    if value is None:
        return None
    return str(value.status), dict(value.payload or {})


def _extras_rows(h: Harness, draft_id: int) -> list[AnnouncementExtra]:
    with h.session_factory() as session:
        return (
            session.query(AnnouncementExtra)
            .filter(AnnouncementExtra.draft_id == draft_id)
            .all()
        )


def _brick(h: Harness, brick_id: int):
    with h.session_factory() as session:
        return session.get(Brick, brick_id)


def _seed_extra(
    h: Harness, draft_id: int, brick_id: int, value: str | None = None
) -> None:
    """
    Подготовка данных: доп. кирпичик и его значение одной транзакцией.

    Ревизия читается непосредственно перед вызовом, поэтому сев не копит
    устаревшие кнопки: добавление и значение идут одним CAS от актуальной
    ревизии (`add_announcement_extra` умеет и то, и другое сразу).
    `None` — «пусто»: хранится как `empty_optional` (осознанный пропуск),
    иначе анонс с выбранным, но незаполненным доп. полем не собирается и
    не доставляется — «кирпичик не имеет значения и не попал в вопросы».
    """
    with h.session_factory() as session:
        current = get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )
        add_announcement_extra(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
            brick_id=brick_id,
            value="" if value is None else value,
            expected_revision=current.revision,
        )


def _prepare_preview_stage(h: Harness, draft_id: int) -> None:
    """
    Подготовка данных: этап обзора после сева доп. полей.

    `add_announcement_extra` переводит черновик на этап доп. полей (так
    работает ручное добавление). Для доставки нужен обзор — переводим его
    явно, чтобы дальше шёл реальный путь «Обзор → Отправить в этот чат».
    """
    with h.session_factory() as session:
        current = get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )
        set_announcement_stage(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
            stage="preview",
            expected_revision=current.revision,
        )


def _specs(*rows: tuple[str, str, str, dict]) -> list[tuple[str, str, str, dict]]:
    return list(rows)


async def _open_edit(h: Harness) -> None:
    """
    Открывает экран правки доставленного анонса (если он ещё не открыт).

    2.5 (§18): после доставки категории правки уже на экране, поэтому
    helper ничего не делает. Устаревшая кнопка «✏️ Изменить» тоже ведёт
    на этот экран.
    """
    if _callbacks(h, "aw_qmanage"):
        return
    data = _callbacks(h, "aw_edit")
    if data:
        await h.callback(data[0])


async def _open_extras(h: Harness) -> None:
    """
    Путь владельца (2.5, §18): экран редактора → «🧩 Дополнительные поля».

    Допускается, что при устаревшей ревизии первый же переход сразу
    показывает актуальный экран доп. полей (так работает восстановление).
    """
    if EXTRAS_TITLE in h.screen_text():
        return
    await _open_edit(h)
    if EXTRAS_TITLE in h.screen_text():
        return
    data = _callbacks(h, "aw_extras")
    if data:
        await h.callback(data[0])


async def _deliver(
    h: Harness,
    *,
    specs: list[tuple[str, str, str, dict]],
    content: str,
    extended: str | None,
    value: str = "M",
    seed: list[tuple[str, str | None]] | None = None,
):
    """
    Личный анонс, доставленный «Отправить в этот чат» с обзора.

    `seed` — [(код кирпичика, значение)] доп. полей, которые готовятся ДО
    доставки: так анонс приходит в чат уже с ними. Сев идёт одним CAS от
    актуальной ревизии, после сева черновик возвращается на этап обзора, а
    доставка — реальными кнопками продукта (`aw_send`).

    «Пропустить» на шаге фото здесь не используется: после сева ревизия
    другая, кнопка шага фото устарела, и продукт (правильно) отвечает
    «Данные изменились» и показывает актуальный экран, а не доставляет.
    """
    place_id, _alias = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(
        h,
        name="Личный шаблон P2",
        specs=specs,
        content=content,
        extended_content=extended,
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text(value)
    draft = _latest_draft(h)
    if seed:
        for code, seeded in seed:
            _seed_extra(h, draft.id, bricks[code], seeded)
        _prepare_preview_stage(h, draft.id)
        # Ревизия выросла, кнопки шага фото устарели. Продукт на такой
        # callback отвечает «Данные изменились» и рисует актуальный экран по
        # этапу черновика (обзор) — дальше идёт реальная кнопка отправки.
        stale = _callbacks(h, "aw_photo_none")
        if stale:
            await h.callback(stale[0])
        send = _callbacks(h, "aw_send")
        if not send:
            raise AssertionError(
                f"после сева нет кнопки отправки: {h.screen_buttons()}; "
                f"экран: {h.screen_text()[:300]!r}"
            )
        await h.callback(send[0])
    else:
        await _no_photos(h)
    if not h.screen_text().startswith(DELIVERED_HEAD):
        raise AssertionError(f"анонс не доставлен: {h.screen_text()[:300]!r}")
    return _latest_draft(h), bricks


# --- IV. Список доп. полей ---------------------------------------------------


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class ExtrasListTests(unittest.TestCase):
    """IV: `aw_extras` — сразу полный список, без «Добавить поле» и «В меню»."""

    @scenario_test
    async def test_s1_extras_lists_every_extra_brick_without_add_or_menu(self, h):
        """
        Требование IV: список всех доп. кирпичиков расширенного шаблона.

        Незаполненные тоже видны как доступные к заполнению; «➕ Добавить
        поле» и «🏠 В меню» на экране запрещены; ниже списка ровно «◀️ Назад».
        """
        await h.text("/start")
        _draft_row, bricks = await _deliver(
            h,
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("e1", "Доп-А", "short_text", {}),
                ("e2", "Доп-Б", "short_text", {}),
                ("e3", "Доп-В", "short_text", {}),
            ),
            content="A [main] B C",
            extended="A [main] B [e1] C [e2] [e3]",
        )

        await _open_extras(h)
        _assert_no_internal(self, h, "переход к доп. полям")
        self.assertIn(EXTRAS_TITLE, h.screen_text())

        # Никакого промежуточного экрана «выберите доступное определение»:
        # все три незаполненных кирпичика уже в списке.
        titles = ["Доп-А", "Доп-Б", "Доп-В"]
        found = _title_buttons(h, titles)
        self.assertEqual(
            sorted(found),
            sorted(titles),
            "экран доп. полей должен открываться сразу списком ВСЕХ "
            f"кирпичиков расширенной версии (в т.ч. незаполненных); "
            f"есть: {h.screen_buttons()}; экран: {h.screen_text()[:400]!r}",
        )
        for code in ("e1", "e2", "e3"):
            self.assertGreater(bricks[code], 0)

        labels = _labels(h)
        datas = _datas(h)
        self.assertEqual(
            [label for label in labels if LABEL_ADD_FIELD in label],
            [],
            "«➕ Добавить поле» на экране списка доп. полей запрещено",
        )
        self.assertEqual(
            [data for data in datas if data.startswith("aw_exadd")],
            [],
            "callback aw_exadd на экране списка доп. полей запрещён",
        )
        self.assertNotIn(
            LABEL_MENU, labels, "«🏠 В меню» на экране доп. полей запрещено"
        )
        self.assertEqual(
            [data for data in datas if data == "main_menu"],
            [],
            "на экране доп. полей не должно быть выхода в главное меню",
        )

        rows = _screen_rows(h)
        flat = [item for row in rows for item in row]
        backs = [item for item in flat if _is_back(*item)]
        self.assertEqual(
            len(backs), 1, f"на экране доп. полей ровно одна кнопка «Назад»: {flat}"
        )
        self.assertEqual(
            rows[-1],
            [backs[0]],
            "ниже списка доп. полей — ровно «◀️ Назад»",
        )

    @scenario_test
    async def test_s2_empty_extra_opens_and_fills_at_template_position(self, h):
        """
        IV: незаполненный кирпичик открывается, заполняется по месту в тексте.

        Значение встаёт на позицию токена расширенного текста, а не в конец.
        """
        await h.text("/start")
        draft, bricks = await _deliver(
            h,
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("e1", "Доп-А", "short_text", {}),
            ),
            content="A [main] B C",
            extended="A [main] B [e1] C",
        )
        self.assertEqual(h.posts()[-1], "A M B C")

        await _open_extras(h)
        data = _require_title_button(self, h, "Доп-А", "незаполненное доп. поле")
        await h.callback(data)
        _assert_no_internal(self, h, "открытие незаполненного доп. поля")
        self.assertIn(
            "Доп-А",
            h.screen_text(),
            "должен открыться экран значения выбранного кирпичика",
        )
        self.assertIn(
            LABEL_SKIP,
            _labels(h),
            "на экране значения доп. поля нужна кнопка «⏭ Пропустить»",
        )
        self.assertTrue(
            any(MISSING_FRAGMENT in h.screen_text() for _ in [0]),
            f"незаполненное поле должно быть честно показано незаполненным: "
            f"{h.screen_text()[:400]!r}",
        )

        await h.text("ADD")
        _assert_no_internal(self, h, "сохранение значения доп. поля")
        self.assertIn(
            EXTRAS_TITLE,
            h.screen_text(),
            "после сохранения значения возвращаемся к списку доп. полей",
        )
        self.assertEqual(
            h.posts()[-1],
            "A M B ADD C",
            "значение доп. поля встаёт ровно на место [e1] расширенного "
            f"текста, а не в конец; фактически: {h.posts()[-1]!r}",
        )
        state = _value(h, draft.id, bricks["e1"])
        self.assertIsNotNone(state)
        self.assertEqual(state[0], "valid")
        self.assertEqual(state[1].get("text"), "ADD")

    @scenario_test
    async def test_s3_filled_extra_can_be_edited_and_returns_to_list(self, h):
        """IV: заполненный кирпичик открывается, меняется, возврат к списку."""
        await h.text("/start")
        draft, bricks = await _deliver(
            h,
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("e1", "Доп-А", "short_text", {}),
            ),
            content="A [main] B C",
            extended="A [main] B [e1] C",
            seed=[("e1", "ADD")],
        )
        self.assertEqual(h.posts()[-1], "A M B ADD C")

        await _open_extras(h)
        data = _require_title_button(self, h, "Доп-А", "заполненное доп. поле")
        await h.callback(data)
        _assert_no_internal(self, h, "открытие заполненного доп. поля")
        self.assertIn(
            CURRENT_VALUE,
            h.screen_text(),
            "заполненный кирпичик открывается с текущим значением",
        )
        self.assertIn("ADD", h.screen_text())

        await h.text("NEW")
        self.assertIn(
            EXTRAS_TITLE,
            h.screen_text(),
            "сохранение значения возвращает к списку доп. полей",
        )
        self.assertEqual(
            h.posts()[-1],
            "A M B NEW C",
            f"новое значение заменяет старое на месте токена: {h.posts()[-1]!r}",
        )
        state = _value(h, draft.id, bricks["e1"])
        self.assertIsNotNone(state)
        self.assertEqual(state[0], "valid")
        self.assertEqual(state[1].get("text"), "NEW")

    @scenario_test
    async def test_s4_trash_removes_value_only_and_keeps_definition(self, h):
        """
        IV: корзина у заполненного удаляет значение, определение живёт.

        После удаления последнего доп. поля анонс рендерится каноническим
        `content` — простая версия шаблона.
        """
        await h.text("/start")
        draft, bricks = await _deliver(
            h,
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("e1", "Доп-А", "short_text", {}),
            ),
            content="A [main] B C",
            extended="A [main] B [e1] C",
            seed=[("e1", "ADD")],
        )
        self.assertEqual(h.posts()[-1], "A M B ADD C")

        await _open_extras(h)
        trash_rows = [
            row for row in _screen_rows(h) if any(_is_delete(*item) for item in row)
        ]
        self.assertEqual(
            len(trash_rows), 1, f"у заполненного поля нужна корзина: {_screen_rows(h)}"
        )
        self.assertEqual(len(trash_rows[0]), 2, "корзина стоит справа от поля")
        title_cell, trash_cell = trash_rows[0]
        self.assertIn("Доп-А", title_cell[0])
        self.assertTrue(_is_delete(*trash_cell))

        await h.callback(trash_cell[1])
        _assert_no_internal(self, h, "удаление доп. поля")
        self.assertIn(EXTRAS_TITLE, h.screen_text())
        self.assertEqual(_extras_rows(h, draft.id), [], "доп. поле удалено из анонса")
        self.assertIsNone(
            _value(h, draft.id, bricks["e1"]), "значение удалено вместе с полем"
        )
        self.assertIsNotNone(
            _brick(h, bricks["e1"]), "удаление значения не удаляет определение"
        )
        self.assertEqual(
            h.posts()[-1],
            "A M B C",
            "после удаления последнего доп. поля — простая версия (content): "
            f"{h.posts()[-1]!r}",
        )

    @scenario_test
    async def test_s5_pagination_only_when_many_fields(self, h):
        """IV: мало полей — без пагинации; много — с пагинацией."""
        await h.text("/start")
        many = [
            (f"e{index}", f"Поле{index:02d}", "short_text", {})
            for index in range(1, 13)
        ]
        draft, _bricks = await _deliver(
            h,
            specs=_specs(("main", "Основа", "short_text", {}), *many),
            content="A [main] B",
            extended="A [main] B " + " ".join(f"[e{index}]" for index in range(1, 13)),
        )
        titles = [f"Поле{index:02d}" for index in range(1, 13)]

        await _open_extras(h)
        visible = _title_buttons(h, titles)
        self.assertTrue(
            visible,
            "список доп. полей должен показывать кирпичики: " f"{h.screen_buttons()}",
        )
        pagination = [
            (label, data)
            for label, data in _flat_buttons(h)
            if _is_pagination(label, data)
        ]
        self.assertTrue(
            pagination,
            "при 12 доп. полях (больше страницы) нужна пагинация: "
            f"{h.screen_buttons()}",
        )
        self.assertLess(
            len(visible),
            len(titles),
            "страница списка не показывает все 12 полей сразу — остальные "
            f"доступны пагинацией: {sorted(visible)}",
        )
        before = set(visible)
        await h.callback(pagination[0][1])
        _assert_no_internal(self, h, "переход страницы доп. полей")
        after = set(_title_buttons(h, titles))
        self.assertNotEqual(
            before, after, "следующая страница показывает другие доп. поля"
        )

        # Мало полей — пагинации нет, все видны сразу.
        h.user_id = DEFAULT_USER_ID
        place_id, _alias = _seed_place(h, name="Второе место")
        template_id, _revision, _few = _seed_personal_template(
            h,
            name="Мало полей",
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("e1", "Немного-А", "short_text", {}),
                ("e2", "Немного-Б", "short_text", {}),
            ),
            content="A [main] B",
            extended="A [main] B [e1] [e2]",
        )
        await _start_to_template(h, place_id, query="Втор")
        await _choose_personal(h, template_id)
        await h.text("M")
        await _no_photos(h)
        await _open_extras(h)
        short_titles = ["Немного-А", "Немного-Б"]
        self.assertEqual(
            sorted(_title_buttons(h, short_titles)),
            sorted(short_titles),
            f"при двух полях видны оба: {h.screen_buttons()}",
        )
        self.assertEqual(
            [
                (label, data)
                for label, data in _flat_buttons(h)
                if _is_pagination(label, data)
            ],
            [],
            "при малом числе полей пагинации быть не должно",
        )

    @scenario_test
    async def test_s6_legacy_snapshot_without_extended_does_not_append(self, h):
        """
        Совместимость: у снимка без `extended_content` extras не дописываются.

        Старое значение в БД остаётся (ничего не удаляется), но в текст
        анонса не попадает и в конец не дописывается.
        """
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Без расширения",
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("e1", "Старое доп", "short_text", {}),
            ),
            content="A [main] B C",
            extended_content=None,
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("M")
        draft = _latest_draft(h)
        # Строка доп. поля могла остаться от прошлой версии продукта.
        with h.session_factory() as session:
            session.add(AnnouncementExtra(draft_id=draft.id, brick_id=bricks["e1"]))
            from models import AnnouncementValue

            session.add(
                AnnouncementValue(
                    draft_id=draft.id,
                    brick_id=bricks["e1"],
                    state="valid",
                    value_json='{"text": "ADD"}',
                )
            )
            session.commit()

        await _no_photos(h)
        text = h.posts()[-1] or ""
        self.assertNotIn(
            "ADD",
            text,
            "legacy-значение не дописывается в конец текста: append-fallback "
            f"удалён; фактический текст: {text!r}",
        )
        self.assertEqual(text.strip(), "A M B C", f"текст анонса: {text!r}")
        # Данные не потеряны: строки остались, экран не сломан.
        self.assertEqual(len(_extras_rows(h, draft.id)), 1)
        state = _value(h, draft.id, bricks["e1"])
        self.assertIsNotNone(state)
        self.assertEqual(state[0], "valid")


# --- VI. Экран вопроса -------------------------------------------------------


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class QuestionScreenTests(unittest.TestCase):
    """VI: «Далее» и «Изменить ответ» на экране вопроса запрещены."""

    @scenario_test
    async def test_s7_question_has_no_next_or_edit_answer_buttons(self, h):
        """Ни на пустом, ни на заполненном, ни на экране ошибки ввода."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Два вопроса",
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("num", "Число", "number", {}),
            ),
            content="[main] | [num]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        def assert_clean(label: str) -> None:
            labels = _labels(h)
            datas = _datas(h)
            self.assertNotIn(
                LABEL_NEXT, labels, f"{label}: «➡️ Далее» на вопросе запрещено"
            )
            self.assertNotIn(
                LABEL_EDIT_ANSWER,
                labels,
                f"{label}: «✏️ Изменить ответ» на вопросе запрещено",
            )
            self.assertEqual(
                [d for d in datas if d.startswith(("aw_qnext", "aw_qedit"))],
                [],
                f"{label}: на экране вопроса только «Назад» | «Пропустить»",
            )
            self.assertIn(LABEL_SKIP, labels, f"{label}: нужна «⏭ Пропустить»")
            self.assertTrue(
                any(_is_back(*item) for item in _flat_buttons(h)),
                f"{label}: нужна «◀️ Назад»",
            )

        # Пустой вопрос.
        self.assertIn(QUESTION_PREFIX, h.screen_text())
        assert_clean("пустое поле")

        # Заполненное поле (возврат с шага фото).
        await h.text("M")
        await h.text("5")
        self.assertIn(PHOTO_STEP, h.screen_text())
        await h.callback(_callback(h, "aw_photo_back"))
        self.assertIn(QUESTION_PREFIX, h.screen_text())
        self.assertIn(CURRENT_VALUE, h.screen_text())
        assert_clean("заполненное поле")

        # Экран ошибки ввода (editing=True) по заполненному полю.
        await h.text("abc")
        self.assertIn("⚠️", h.screen_text())
        assert_clean("экран ошибки ввода")


# --- VI. Семантика «⏭ Пропустить» -------------------------------------------


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class SkipSemanticsTests(unittest.TestCase):
    """«⏭ Пропустить» не очищает значение и не дублирует анонс."""

    async def _two_questions(self, h):
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Два вопроса",
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("second", "Второе", "short_text", {}),
            ),
            content="[main] | [second]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        return _latest_draft(h), bricks

    @scenario_test
    async def test_s8_skip_on_filled_question_keeps_value_and_moves_on(self, h):
        """Пропуск заполненного поля сохраняет значение и идёт дальше."""
        draft, bricks = await self._two_questions(h)
        await h.text("M")
        self.assertIn("Вопрос 2 из 2", h.screen_text())
        await h.callback(_callback(h, "aw_qback"))
        self.assertIn("Вопрос 1 из 2", h.screen_text())

        posts_before = len(h.post_records())
        await h.callback(_callback(h, "aw_qskip"))
        _assert_no_internal(self, h, "пропуск заполненного поля")

        self.assertIn(
            "Вопрос 2 из 2",
            h.screen_text(),
            "после пропуска — следующий вопрос",
        )
        state = _value(h, draft.id, bricks["main"])
        self.assertIsNotNone(
            state,
            "«Пропустить» на заполненном поле не должно стирать значение",
        )
        self.assertEqual(
            state[0],
            "valid",
            "«Пропустить» на заполненном поле сохраняет значение как есть, а "
            f"не делает его empty_optional; фактически: {state}",
        )
        self.assertEqual(state[1].get("text"), "M")
        self.assertEqual(
            len(h.post_records()),
            posts_before,
            "пропуск не порождает лишних сообщений/копий анонса",
        )

    @scenario_test
    async def test_s9_skip_on_empty_question_stays_empty_and_moves_on(self, h):
        """Пропуск пустого поля: значение не задано, вопрос пройден."""
        draft, bricks = await self._two_questions(h)
        self.assertIn("Вопрос 1 из 2", h.screen_text())
        await h.callback(_callback(h, "aw_qskip"))
        _assert_no_internal(self, h, "пропуск пустого поля")

        self.assertIn("Вопрос 2 из 2", h.screen_text())
        state = _value(h, draft.id, bricks["main"])
        self.assertTrue(
            state is None or state[0] == "empty_optional",
            f"пустое поле остаётся пустым (не valid): {state}",
        )

    @scenario_test
    async def test_s10_back_does_not_lose_previous_value(self, h):
        """«Назад» не теряет ранее сохранённое значение."""
        draft, bricks = await self._two_questions(h)
        await h.text("M")
        await h.callback(_callback(h, "aw_qback"))

        state = _value(h, draft.id, bricks["main"])
        self.assertIsNotNone(state)
        self.assertEqual(state[0], "valid")
        self.assertEqual(state[1].get("text"), "M")
        self.assertIn(
            CURRENT_VALUE,
            h.screen_text(),
            "на заполненном вопросе видно текущее значение",
        )
        self.assertIn("M", h.screen_text())

    @scenario_test
    async def test_s11_new_input_replaces_old_value(self, h):
        """Новый ввод заменяет старое значение (одно, а не два)."""
        draft, bricks = await self._two_questions(h)
        await h.text("M")
        await h.callback(_callback(h, "aw_qback"))
        await h.text("N")

        state = _value(h, draft.id, bricks["main"])
        self.assertIsNotNone(state)
        self.assertEqual(state[0], "valid")
        self.assertEqual(state[1].get("text"), "N")
        self.assertIn("Вопрос 2 из 2", h.screen_text())


# --- IV. «Пропустить» на экране значения доп. поля --------------------------


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class ExtraSkipTests(unittest.TestCase):
    """Та же семантика «Пропустить» — на экране значения доп. поля."""

    @scenario_test
    async def test_s12_skip_on_filled_extra_keeps_value_and_post(self, h):
        """Пропуск заполненного доп. поля не очищает значение и не шлёт копию."""
        await h.text("/start")
        draft, bricks = await _deliver(
            h,
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("e1", "Доп-А", "short_text", {}),
            ),
            content="A [main] B C",
            extended="A [main] B [e1] C",
            seed=[("e1", "ADD")],
        )
        posts_before = list(h.posts())
        self.assertEqual(posts_before[-1], "A M B ADD C")

        await _open_extras(h)
        data = _require_title_button(self, h, "Доп-А", "заполненное доп. поле")
        await h.callback(data)
        self.assertIn(CURRENT_VALUE, h.screen_text())

        await h.callback(_callback(h, "aw_exskip"))
        _assert_no_internal(self, h, "пропуск заполненного доп. поля")

        self.assertIn(
            EXTRAS_TITLE,
            h.screen_text(),
            "после пропуска возвращаемся к списку доп. полей",
        )
        state = _value(h, draft.id, bricks["e1"])
        self.assertIsNotNone(state)
        self.assertEqual(
            state[0],
            "valid",
            "«Пропустить» на заполненном доп. поле сохраняет значение как "
            f"есть; фактически: {state}",
        )
        self.assertEqual(state[1].get("text"), "ADD")
        self.assertEqual(
            h.posts(),
            posts_before,
            "пропуск не пересобирает анонс и не создаёт новую версию: "
            f"{h.posts()!r}",
        )

    @scenario_test
    async def test_s13_skip_on_empty_extra_leaves_it_empty(self, h):
        """Пропуск незаполненного доп. поля: значение не задано."""
        await h.text("/start")
        draft, bricks = await _deliver(
            h,
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("e1", "Доп-А", "short_text", {}),
            ),
            content="A [main] B C",
            extended="A [main] B [e1] C",
            seed=[("e1", None)],
        )
        await _open_extras(h)
        data = _require_title_button(self, h, "Доп-А", "незаполненное доп. поле")
        await h.callback(data)
        await h.callback(_callback(h, "aw_exskip"))
        _assert_no_internal(self, h, "пропуск пустого доп. поля")

        self.assertIn(EXTRAS_TITLE, h.screen_text())
        state = _value(h, draft.id, bricks["e1"])
        self.assertTrue(
            state is None or state[0] == "empty_optional",
            f"пустое доп. поле остаётся пустым (не valid): {state}",
        )
        self.assertNotIn("ADD", h.posts()[-1] or "")


# --- V. Навигация «Назад» ----------------------------------------------------


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class BackNavigationTests(unittest.TestCase):
    """V: «Назад» возвращает на реальный предыдущий экран пути."""

    @scenario_test
    async def test_s14_back_from_extra_value_returns_to_list_then_to_edit(self, h):
        """
        Путь владельца: Изменить → Доп. поля → Стоимость → Назад → Доп.
        поля → Назад → Изменить. Главное меню не появляется.
        """
        await h.text("/start")
        _draft_row, _bricks = await _deliver(
            h,
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("e1", "Стоимость", "short_text", {}),
            ),
            content="A [main] B C",
            extended="A [main] B [e1] C",
            seed=[("e1", "500")],
        )
        await _open_edit(h)
        self.assertIn(EDIT_HEAD, h.screen_text())

        await _open_extras(h)
        self.assertIn(EXTRAS_TITLE, h.screen_text())
        data = _require_title_button(self, h, "Стоимость", "доп. поле «Стоимость»")
        await h.callback(data)
        self.assertIn("Стоимость", h.screen_text())
        self.assertNotIn(MENU_FRAGMENT, h.screen_text())

        # «Назад» со стоимости — к списку доп. полей (реальный путь по V).
        # Важна не подпись, а назначение: кнопка возврата на шаг, с которого
        # пришли, а не в главное меню.
        backs = [item for item in _flat_buttons(h) if _is_back(*item)]
        self.assertTrue(
            backs,
            f"на экране значения доп. поля нужна кнопка «Назад»: {h.screen_buttons()}",
        )
        await h.callback(backs[0][1])
        _assert_no_internal(self, h, "назад со значения доп. поля")
        self.assertIn(
            EXTRAS_TITLE,
            h.screen_text(),
            "«Назад» со стоимости ведёт к «Дополнительные поля», а не в меню",
        )
        self.assertNotIn(MENU_FRAGMENT, h.screen_text())

        # Ещё «Назад» — к экрану «Изменить».
        backs = [item for item in _flat_buttons(h) if _is_back(*item)]
        self.assertTrue(
            backs, f"на списке доп. полей нужна кнопка «Назад»: {h.screen_buttons()}"
        )
        await h.callback(backs[0][1])
        _assert_no_internal(self, h, "назад со списка доп. полей")
        self.assertIn(
            EDIT_HEAD,
            h.screen_text(),
            "«Назад» из доп. полей ведёт к «Изменить», а не в главное меню: "
            f"{h.screen_text()[:300]!r}",
        )
        self.assertNotIn(MENU_FRAGMENT, h.screen_text())
        self.assertNotIn(
            TEMPLATE_HEAD,
            h.screen_text(),
            "«Назад» не уводит в старый конструктор/выбор шаблона",
        )

    @scenario_test
    async def test_s15_back_from_first_question_returns_to_template_screen(self, h):
        """«Назад» с первого вопроса обычного конструктора — к выбору шаблона."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Один вопрос",
            specs=_specs(("main", "Основа", "short_text", {})),
            content="[main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        self.assertIn(QUESTION_PREFIX, h.screen_text())

        await h.callback(_callback(h, "aw_qback"))
        _assert_no_internal(self, h, "назад с первого вопроса")
        self.assertIn(
            TEMPLATE_HEAD,
            h.screen_text(),
            "«Назад» с первого вопроса возвращает к экрану шаблона (реальный "
            f"путь), а не в меню: {h.screen_text()[:300]!r}",
        )
        self.assertNotIn(MENU_FRAGMENT, h.screen_text())

    @scenario_test
    async def test_s16_back_from_question_manager_returns_to_edit_screen(self, h):
        """«Поля анонса» → «Назад» — к экрану «Изменить», не к шаблону."""
        await h.text("/start")
        await _deliver(
            h,
            specs=_specs(("main", "Основа", "short_text", {})),
            content="A [main] B",
            extended=None,
        )
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        self.assertIn(MANAGER_HEAD, h.screen_text())

        backs = [item for item in _flat_buttons(h) if _is_back(*item)]
        self.assertTrue(backs, f"на экране полей нужна «Назад»: {h.screen_buttons()}")
        await h.callback(backs[0][1])
        _assert_no_internal(self, h, "назад с экрана полей анонса")
        self.assertIn(
            EDIT_HEAD,
            h.screen_text(),
            "«Назад» с «Поля анонса» возвращает на экран, с которого пришли "
            f"(«Изменить»), а не к выбору шаблона: {h.screen_text()[:300]!r}",
        )
        self.assertNotIn(MENU_FRAGMENT, h.screen_text())

    @scenario_test
    async def test_s17_delivered_screen_has_edit_categories_and_menu(self, h):
        """§18 (2.5): после доставки — категории правки и «🏠 В меню»."""
        await h.text("/start")
        await _deliver(
            h,
            specs=_specs(("main", "Основа", "short_text", {})),
            content="A [main] B",
            extended=None,
        )
        self.assertTrue(h.screen_text().startswith(EDIT_HEAD))

        rows = _screen_rows(h)
        self.assertEqual(
            [label for label, _data in rows[0]],
            [LABEL_DELIVERED_DATA, LABEL_DELIVERED_PHOTOS],
            f"первая строка — категории правки: {rows}",
        )
        self.assertEqual(
            [label for label, _data in rows[-1]],
            [LABEL_DELIVERED_MENU],
            "последняя строка — «🏠 В меню»",
        )
        self.assertEqual(
            rows[-1][0][1],
            "main_menu",
            "последняя кнопка — «🏠 В меню»",
        )
        self.assertEqual(
            [item for item in rows[-1] if _is_back(*item)],
            [],
            "«Назад» в старый конструктор на экране доставки запрещён",
        )
        self.assertFalse(_callbacks(h, "aw_edit"), "кнопки «✏️ Изменить» нет")
        self.assertFalse(_callbacks(h, "aw_apply"), "кнопки применения нет")

    @scenario_test
    async def test_s18_restart_has_no_stack_and_back_stays_safe(self, h):
        """
        Рестарт службы между шагами: стека нет.

        «Назад» ведёт на безопасный актуальный экран, а не в случайное
        место и не в главное меню; черновик и значения не теряются.
        """
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Два вопроса",
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("second", "Второе", "short_text", {}),
            ),
            content="[main] | [second]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("M")
        self.assertIn("Вопрос 2 из 2", h.screen_text())
        draft = _latest_draft(h)
        back_data = _callback(h, "aw_qback")

        await h.restart()
        await h.callback(back_data)
        _assert_no_internal(self, h, "назад после рестарта службы")

        self.assertNotIn(
            MENU_FRAGMENT,
            h.screen_text(),
            "после рестарта «Назад» не выбрасывает в главное меню",
        )
        self.assertTrue(
            QUESTION_PREFIX in h.screen_text()
            or PHOTO_STEP in h.screen_text()
            or TEMPLATE_HEAD in h.screen_text(),
            "«Назад» без стека ведёт на безопасный актуальный экран шага: "
            f"{h.screen_text()[:300]!r}",
        )
        state = _value(h, draft.id, bricks["main"])
        self.assertIsNotNone(state, "черновик и его значения не теряются")
        self.assertEqual(state[0], "valid")
        self.assertEqual(state[1].get("text"), "M")

    @scenario_test
    async def test_s19_forged_navigation_callback_is_safe(self, h):
        """Подделанный/устаревший callback навигации — безопасный экран."""
        await h.text("/start")
        await _deliver(
            h,
            specs=_specs(("main", "Основа", "short_text", {})),
            content="A [main] B",
            extended=None,
        )
        if EXTRAS_TITLE in h.screen_text():
            await _open_extras(h)
        draft = _latest_draft(h)

        forged = [
            "aw_qback:999999:1:1",
            "aw_exbackval:999999:1",
            "aw_exbackval:0:0",
            "aw_extras:999999:1",
            "aw_exedit:999999:1:1",
            "aw_qback:x:y:z",
        ]
        for data in forged:
            with self.subTest(data=data):
                answers_before = len(h.answers())
                await h.callback(data)  # исключение наружу не выходит
                _assert_no_internal(self, h, f"подделанный {data}")
                self.assertGreaterEqual(
                    len(h.answers()) - answers_before,
                    1,
                    f"{data}: callback остался без ответа (висящий спиннер)",
                )
                self.assertTrue(
                    h.screen_text().strip() != "",
                    f"{data}: после подделанного callback экран не пропал",
                )
        state = _value(h, draft.id, _draft(h, draft.id).values[0].brick_id)
        self.assertIsNotNone(state, "подделанные callback'и не портят черновик")
        self.assertEqual(state[0], "valid")


if __name__ == "__main__":
    unittest.main()
