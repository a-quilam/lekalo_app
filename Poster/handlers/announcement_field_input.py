# handlers/announcement_field_input.py
"""
Poster 2.5.4 §1: ВВОД значения текстового поля готового анонса.

У доставленного анонса нет отдельного служебного сообщения-вопроса. Ввод
живёт там же, где управление:

- 0–1 фото — клавиатура самого сообщения анонса
  (`editMessageReplyMarkup`: текст и подпись не трогаются);
- 2+ фото — единственная панель медиагруппы (`editMessageText`): второй
  панели не появляется.

Поэтому модуль держит только ВИД и правку существующего сообщения:

- подсказка «⏳ Ожидаю <поле>» — настоящая неактивная кнопка
  (`utils.inline_buttons.disabled_button`, Bot API 10.3);
- готовые варианты: быстрые значения времени, компактный календарь даты,
  варианты выбора;
- последняя строка — «◀️ Назад | ⏭ Пропустить».

Сохранение значения и возврат к списку полей — маршрут
`handlers.announcement_panel.handle_field_input_callback` и общий
`_save_question_value` конструктора: копий логики на каждый тип поля нет.
`callback_data` (`anq_`) несут только id экземпляра и кирпичика, поэтому
клавиатура продолжает работать после рестарта службы.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import announcement_controls
import delivery
from announcement_store import AnnouncementFieldState
from brick_types import (
    BRICK_TYPE_CHOICE,
    BRICK_TYPE_DATE,
    BRICK_TYPE_TIME,
)
from handlers import wizard_questions
from utils import calendar_view, keyboard_layouts
from utils.inline_buttons import disabled_button
from utils.parsers import app_today
from utils.ui_texts import tr, tr_plain

logger = logging.getLogger(__name__)

# Уровни календаря на клавиатуре ввода. `None` — первый экран (быстрые даты,
# диапазоны и «Выбрать месяц»). Уровни задаются кнопками `anq_` и потому
# восстанавливаются после рестарта по одному callback, без состояния диалога.
VIEW_QUICK = "quick"
VIEW_RANGE = "range"
VIEW_MONTHS = "months"
VIEW_MONTH = "month"

# Сколько кнопок в строке: дни быстрых дат — как на экране вопроса (`U2a`),
# дни месяца — неделей, месяцы сетки — по три.
QUICK_PER_ROW = 4
MONTH_DAYS_PER_ROW = 7
MONTHS_PER_ROW = 3


@dataclass(frozen=True)
class CalendarView:
    """Текущий уровень календаря на клавиатуре ввода."""

    kind: str = VIEW_QUICK
    index: int = 0
    year: int = 0
    month: int = 0

    def as_payload(self) -> tuple[object, ...]:
        """Хвост `callback_data` для возврата на этот же уровень."""
        if self.kind == VIEW_RANGE:
            return (self.index,)
        if self.kind == VIEW_MONTH:
            return (self.year, self.month)
        return ()


def _title(field: AnnouncementFieldState) -> str:
    """Короткое имя поля для подсказки (plain-текст кнопки)."""
    from brick_types import brick_title_display

    return brick_title_display(field.title, field.code)


def hint_label(field: AnnouncementFieldState) -> str:
    """Подпись неактивной кнопки «⏳ Ожидаю <поле>»."""
    return tr_plain(
        "wizard.field_input.hint",
        title=_title(field),
    )


def panel_text(field: AnnouncementFieldState, view: CalendarView | None = None) -> str:
    """
    Тело единственной панели медиагруппы на время ввода.

    Для календаря в тело добавляется месяц: на клавиатуре подписи кнопок
    короткие, а панель — единственное место, где можно показать контекст.
    """
    view = view or CalendarView()
    if view.kind == VIEW_MONTH and view.year and view.month:
        return tr(
            "wizard.field_input.panel_month",
            title=_title(field),
            month=calendar_view.format_month((view.year, view.month)),
        )
    return tr("wizard.field_input.panel", title=_title(field))


def _day_rows(
    days: list[date],
    instance_id: int,
    brick_id: int,
    *,
    per_row: int,
    label,
) -> list[list[InlineKeyboardButton]]:
    """Кнопки-дни: `anq_day:{instance}:{brick}:ГГГГ-ММ-ДД`."""
    return keyboard_layouts.rows_of(
        (
            InlineKeyboardButton(
                label(day),
                callback_data=announcement_controls.field_input_callback(
                    announcement_controls.FIELD_INPUT_ACTION_DAY,
                    instance_id,
                    brick_id,
                    day.isoformat(),
                ),
            )
            for day in days
        ),
        per_row,
    )


def _quick_rows(
    field: AnnouncementFieldState, instance_id: int, brick_id: int
) -> list[list[InlineKeyboardButton]]:
    """
    Первый экран календаря: восемь быстрых дат, два диапазона, месяцы.

    Тот же состав, что у вопроса даты конструктора (`U2a`), но `anq_`-кнопки:
    они работают без состояния диалога и потому переживают рестарт.
    """
    start, end = wizard_questions.calendar_bounds(dict(field.rules))
    if start > end:
        return []
    allowed = (start, end)
    today = app_today()
    rows: list[list[InlineKeyboardButton]] = []
    quick = calendar_view.quick_days(today, allowed)
    if quick:
        rows.extend(
            _day_rows(
                quick,
                instance_id,
                brick_id,
                per_row=QUICK_PER_ROW,
                label=calendar_view.format_quick_day,
            )
        )
    ranges = calendar_view.followup_ranges(today, allowed)
    if ranges:
        rows.append(
            [
                InlineKeyboardButton(
                    calendar_view.format_range(*item),
                    callback_data=announcement_controls.field_input_callback(
                        announcement_controls.FIELD_INPUT_ACTION_RANGE,
                        instance_id,
                        brick_id,
                        index,
                    ),
                )
                for index, item in enumerate(ranges)
            ]
        )
    if calendar_view.month_grid(calendar_view.selection_seam(today, allowed), end):
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.choose_month"),
                    callback_data=announcement_controls.field_input_callback(
                        announcement_controls.FIELD_INPUT_ACTION_CALENDAR,
                        instance_id,
                        brick_id,
                    ),
                )
            ]
        )
    return rows


def _range_rows(
    field: AnnouncementFieldState,
    instance_id: int,
    brick_id: int,
    index: int,
) -> list[list[InlineKeyboardButton]] | None:
    """Дни раскрытого семидневного диапазона; `None` — индекс устарел."""
    start, end = wizard_questions.calendar_bounds(dict(field.rules))
    ranges = calendar_view.followup_ranges(app_today(), (start, end))
    if index < 0 or index >= len(ranges):
        return None
    rows = _day_rows(
        calendar_view.days_between(*ranges[index]),
        instance_id,
        brick_id,
        per_row=QUICK_PER_ROW,
        label=calendar_view.format_quick_day,
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("wizard.render_calendar_range_days.k_bystrym_datam"),
                callback_data=announcement_controls.field_input_callback(
                    announcement_controls.FIELD_INPUT_ACTION_CALQUICK,
                    instance_id,
                    brick_id,
                ),
            )
        ]
    )
    return rows


def _months_rows(
    field: AnnouncementFieldState, instance_id: int, brick_id: int
) -> list[list[InlineKeyboardButton]] | None:
    """Сетка месяцев горизонта; `None` — месяцев в горизонте нет."""
    start, end = wizard_questions.calendar_bounds(dict(field.rules))
    allowed = (start, end)
    seam = calendar_view.selection_seam(app_today(), allowed)
    months = [
        key
        for key in calendar_view.month_grid(seam, end)
        if calendar_view.month_days(key, allowed)
    ]
    if not months:
        return None
    rows = keyboard_layouts.rows_of(
        (
            InlineKeyboardButton(
                calendar_view.format_month(key),
                callback_data=announcement_controls.field_input_callback(
                    announcement_controls.FIELD_INPUT_ACTION_MONTH,
                    instance_id,
                    brick_id,
                    key[0],
                    key[1],
                ),
            )
            for key in months
        ),
        MONTHS_PER_ROW,
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("wizard.render_calendar_range_days.k_bystrym_datam"),
                callback_data=announcement_controls.field_input_callback(
                    announcement_controls.FIELD_INPUT_ACTION_CALQUICK,
                    instance_id,
                    brick_id,
                ),
            )
        ]
    )
    return rows


def _month_rows(
    field: AnnouncementFieldState,
    instance_id: int,
    brick_id: int,
    year: int,
    month: int,
) -> list[list[InlineKeyboardButton]] | None:
    """Дни выбранного месяца сеткой по семь + переход к месяцам."""
    start, end = wizard_questions.calendar_bounds(dict(field.rules))
    allowed = (start, end)
    key = (year, month)
    if not calendar_view.is_valid_month_key(key):
        return None
    months = [
        item
        for item in calendar_view.month_sequence(
            calendar_view.selection_seam(app_today(), allowed), end
        )
        if calendar_view.month_days(item, allowed)
    ]
    if key not in months:
        return None
    days = calendar_view.month_days(key, allowed)
    if not days:
        return None
    rows = _day_rows(
        days,
        instance_id,
        brick_id,
        per_row=MONTH_DAYS_PER_ROW,
        label=calendar_view.format_month_day,
    )
    position = months.index(key)
    rows += keyboard_layouts.nav_row(
        (
            keyboard_layouts.button(
                "◀️",
                announcement_controls.field_input_callback(
                    announcement_controls.FIELD_INPUT_ACTION_MONTH,
                    instance_id,
                    brick_id,
                    months[position - 1][0],
                    months[position - 1][1],
                ),
            )
            if position > 0
            else None
        ),
        (
            keyboard_layouts.button(
                "▶️",
                announcement_controls.field_input_callback(
                    announcement_controls.FIELD_INPUT_ACTION_MONTH,
                    instance_id,
                    brick_id,
                    months[position + 1][0],
                    months[position + 1][1],
                ),
            )
            if position + 1 < len(months)
            else None
        ),
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.wizard.mesyacy"),
                callback_data=announcement_controls.field_input_callback(
                    announcement_controls.FIELD_INPUT_ACTION_CALENDAR,
                    instance_id,
                    brick_id,
                ),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("wizard.render_calendar_range_days.k_bystrym_datam"),
                callback_data=announcement_controls.field_input_callback(
                    announcement_controls.FIELD_INPUT_ACTION_CALQUICK,
                    instance_id,
                    brick_id,
                ),
            )
        ]
    )
    return rows


def calendar_rows(
    field: AnnouncementFieldState,
    instance_id: int,
    brick_id: int,
    view: CalendarView | None = None,
) -> list[list[InlineKeyboardButton]]:
    """
    Строки календаря для уровня `view` (пустой список — уровень устарел).

    Устаревший уровень (диапазон/месяц вне горизонта) возвращает пустой
    список: вызывающий слой безопасно возвращает первый экран календаря, а
    не показывает сломанную клавиатуру.
    """
    view = view or CalendarView()
    if view.kind == VIEW_RANGE:
        return _range_rows(field, instance_id, brick_id, view.index) or []
    if view.kind == VIEW_MONTHS:
        return _months_rows(field, instance_id, brick_id) or []
    if view.kind == VIEW_MONTH:
        return _month_rows(field, instance_id, brick_id, view.year, view.month) or []
    return _quick_rows(field, instance_id, brick_id)


def _choice_rows(
    field: AnnouncementFieldState, instance_id: int, brick_id: int
) -> list[list[InlineKeyboardButton]]:
    """Варианты выбора: отметки ✅ у уже выбранных, «Готово» у множественного."""
    options = wizard_questions.choice_options(field)
    selected = set(wizard_questions.selected_choices(field))
    rows: list[list[InlineKeyboardButton]] = []
    for option_index, option in enumerate(options):
        marker = "✅ " if option in selected else ""
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"{marker}{option}", 60),
                    callback_data=announcement_controls.field_input_callback(
                        announcement_controls.FIELD_INPUT_ACTION_CHOICE,
                        instance_id,
                        brick_id,
                        option_index,
                    ),
                )
            ]
        )
    if field.rules.get("multiple") and options:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.gotovo"),
                    callback_data=announcement_controls.field_input_callback(
                        announcement_controls.FIELD_INPUT_ACTION_CHOICE_DONE,
                        instance_id,
                        brick_id,
                    ),
                )
            ]
        )
    return rows


def _short(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def input_keyboard(
    field: AnnouncementFieldState,
    instance_id: int,
    *,
    view: CalendarView | None = None,
) -> InlineKeyboardMarkup:
    """
    ЕДИНАЯ клавиатура ввода значения поля (2.5.4 §1, п.2 контракта).

    Строка 1 — неактивная подсказка «⏳ Ожидаю <поле>», средние строки —
    готовые варианты (время/календарь/выбор), последняя — «◀️ Назад |
    ⏭ Пропустить». Поля без готовых вариантов получают только подсказку и
    пару навигации: отдельного «Теперь введите…» сообщения нет.
    """
    rows: list[list[InlineKeyboardButton]] = [[disabled_button(hint_label(field))]]
    if field.value_type == BRICK_TYPE_CHOICE:
        rows.extend(_choice_rows(field, instance_id, field.brick_id))
    elif field.value_type == BRICK_TYPE_TIME:
        rows.append(
            [
                InlineKeyboardButton(
                    value,
                    callback_data=announcement_controls.field_input_callback(
                        announcement_controls.FIELD_INPUT_ACTION_TIME,
                        instance_id,
                        field.brick_id,
                        value,
                    ),
                )
                for value in wizard_questions.TIME_PRESETS
            ]
        )
    elif field.value_type == BRICK_TYPE_DATE:
        rows.extend(calendar_rows(field, instance_id, field.brick_id, view))

    back_button = InlineKeyboardButton(
        tr("buttons.nav.back"),
        callback_data=announcement_controls.field_input_callback(
            announcement_controls.FIELD_INPUT_ACTION_BACK,
            instance_id,
            field.brick_id,
        ),
    )
    skip_button = InlineKeyboardButton(
        tr("buttons.wizard.skip"),
        callback_data=announcement_controls.field_input_callback(
            announcement_controls.FIELD_INPUT_ACTION_SKIP,
            instance_id,
            field.brick_id,
        ),
    )
    rows += keyboard_layouts.pair(back_button, skip_button)
    return InlineKeyboardMarkup(rows)


async def apply_input_screen(
    bot,
    chat_id: int,
    details: Any,
    instance: Any,
    field: AnnouncementFieldState,
    *,
    view: CalendarView | None = None,
) -> bool:
    """
    Показывает клавиатуру ввода на СУЩЕСТВУЮЩЕМ управлении анонса.

    0–1 фото — только `editMessageReplyMarkup` (текст и подпись анонса не
    меняются); 2+ фото — `editMessageText` единственной панели. Новое
    сообщение не отправляется ни в одном случае.

    `True` — экран обновлён; `False` — не нашлось адреса или Telegram
    отказал. Отказ не ломает сценарий: данные не тронуты, повторное нажатие
    кнопки списка покажет ввод снова.
    """
    keyboard = input_keyboard(field, instance.id, view=view)
    if delivery.needs_control_panel(len(details.publication_photos)):
        panel_id = instance.panel_message_id
        if panel_id is None:
            return False
        try:
            await delivery.call_telegram(
                lambda: bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(panel_id),
                    text=panel_text(field, view),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    link_preview_options=announcement_controls.PANEL_LINK_PREVIEW,
                )
            )
        except Exception as exc:  # noqa: BLE001 — панель уже в чате
            if delivery.is_not_modified(exc):
                # Панель уже показывает этот ввод: повторное нажатие того же
                # поля — успех, а не отказ (иначе автор улетал бы к списку).
                logger.info("Ввод поля уже показан в панели #%s.", instance.id)
                return True
            logger.info("Ввод поля не показан в панели #%s: %s", instance.id, exc)
            return False
        return True
    message_id = delivery.caption_message_id(instance.message_ids)
    if message_id is None:
        return False
    try:
        await delivery.call_telegram(
            lambda: bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=int(message_id),
                reply_markup=keyboard,
            )
        )
    except Exception as exc:  # noqa: BLE001 — клавиатура анонса не критична
        if delivery.is_not_modified(exc):
            # Клавиатура ввода уже стоит: это успех, данные и адрес целы.
            logger.info("Ввод поля уже показан у анонса #%s.", instance.id)
            return True
        logger.info("Ввод поля не показан у анонса #%s: %s", instance.id, exc)
        return False
    return True


__all__ = [
    "CalendarView",
    "VIEW_MONTH",
    "VIEW_MONTHS",
    "VIEW_QUICK",
    "VIEW_RANGE",
    "apply_input_screen",
    "calendar_rows",
    "hint_label",
    "input_keyboard",
    "panel_text",
]
