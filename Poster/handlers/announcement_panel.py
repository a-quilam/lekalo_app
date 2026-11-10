# handlers/announcement_panel.py
"""
Poster 2.5, C3: управление готовым анонсом вне конструктора (§30–33).

Кнопки `anp_{action}:{instance_id}` живут на самом анонсе (0–1 фото) или на
отдельной панели медиагруппы (`announcement_controls.panel_keyboard`). Они
привязаны к КОНКРЕТНОМУ экземпляру и обязаны работать после рестарта службы,
когда состояния диалога уже нет. Поэтому обработчик зарегистрирован отдельно
от `ConversationHandler` и раньше него в группе 0: PTB вызывает не более
одного обработчика на группу, и `anp_`-нажатие не доходит до fallback `.*`
конструктора.

Порядок проверок — защита старых callback (§32): сначала экземпляр по id,
затем чат и владелец черновика. Нет экземпляра, он архивный, нажатие пришло
из другого чата или запись удалена — короткий ответ и НИКАКИХ правок экранов
и БД: старая кнопка не восстанавливает прежнюю версию и не открывает чужой
редактор.

Действий ровно четыре (как и кнопок): «Текстовые поля» → список полей на
самом управлении, «Фото» → коллекция вложений, «Доп. поля» → список доп.
полей, «В меню» → главное меню. Список полей (`anf_`) разбирается тем же
модулем: проверки экземпляра у него те же, а экран — не отдельное сообщение,
а клавиатура анонса или текст единственной панели. Экраны переиспользуются
из конструктора: второй набор экранов для готового анонса не нужен. После
действия состояние диалога выставляется явно
(`remember_conversation_state`), иначе следующий `aw_`-callback попал бы
в глобальный обработчик «кнопка устарела».
"""

from __future__ import annotations

import logging
from datetime import date

from telegram import Update
from telegram.ext import BaseHandler, CallbackQueryHandler, ContextTypes

import announcement_controls
from announcement_instances import AnnouncementInstance, get_instance
from announcement_store import (
    AnnouncementActorError,
    AnnouncementError,
    AnnouncementNotFoundError,
    extras_enabled,
    get_announcement_draft,
)
from brick_types import (
    BRICK_TYPE_CHOICE,
    BRICK_TYPE_DATE,
    BRICK_TYPE_TIME,
)
from database import SessionLocal
from handlers import announcement_field_input, wizard_questions
from handlers import announcement_wizard as wizard
from utils import tg_context as ctx
from utils import user_events
from utils.ui_texts import tr

logger = logging.getLogger(__name__)

# Технические коды экранов и действий журнала событий (этап B). Короткие
# латинские коды: они попадают в .txt-снимок репорта, и по ним видно, ГДЕ
# пользователь нажал, без содержимого экрана и значений полей.
SCREEN_FIELD_LIST = "field_list"
SCREEN_PHOTOS = "photos"
SCREEN_EXTRAS = "extras"
SCREEN_FIELD_INPUT = "field_input"
SCREEN_MAIN_MENU = "main_menu"
PANEL_SCREENS = {
    announcement_controls.ACTION_DATA: SCREEN_FIELD_LIST,
    announcement_controls.ACTION_PHOTOS: SCREEN_PHOTOS,
    announcement_controls.ACTION_EXTRAS: SCREEN_EXTRAS,
    announcement_controls.ACTION_MENU: SCREEN_MAIN_MENU,
}


def _event(
    update: Update,
    action: str,
    *,
    screen: str | None = None,
    result: str | None = None,
    detail: str | None = None,
) -> None:
    """
    Пишет техническое событие в журнал, не мешая обработчику.

    Журнал — диагностика (этап B): сбой записи не должен ломать навигацию,
    поэтому фактические вызовы идут через `user_events.event_log`, который
    глотает ошибки. В `detail` передаются только идентификаторы: значение
    поля и текст сообщения сюда не попадают никогда.
    """
    user = update.effective_user
    if user is None:
        return
    user_events.event_log(
        user.id,
        action,
        screen=screen,
        scenario=None,
        result=result,
        detail=detail,
        # Фабрика сессии ЭТОГО модуля: интеграционные тесты подменяют её
        # на временную базу, и журнал обязан писать туда же.
        session_factory=SessionLocal,
    )


# Какое состояние диалога соответствует экрану после действия панели.
# «В меню» не открывает экран конструктора — состояние закрывается.
PANEL_STATES = {
    announcement_controls.ACTION_DATA: wizard.AW_PREVIEW,
    announcement_controls.ACTION_PHOTOS: wizard.AW_PREVIEW,
    announcement_controls.ACTION_EXTRAS: wizard.AW_EXTRAS,
}


def _instance(instance_id: int) -> AnnouncementInstance | None:
    """Экземпляр по id; None — строки нет (в т.ч. после удаления анонса)."""
    with SessionLocal() as session:
        return get_instance(session, instance_id)


def _details(user_id: int, instance: AnnouncementInstance):
    """
    Черновик экземпляра только его автору; None — запись недоступна.

    Проверка нужна даже при найденном экземпляре: без включённого `PRAGMA
    foreign_keys` (или при снятом каскаде) строка экземпляра может пережить
    удаление анонса (§33). Такой callback обязан получить то же пояснение,
    что и архивный, а не открыть редактор удалённой записи.
    """
    try:
        with SessionLocal() as session:
            return get_announcement_draft(
                session,
                actor_telegram_user_id=user_id,
                draft_id=instance.draft_id,
            )
    except (
        AnnouncementNotFoundError,
        AnnouncementActorError,
        AnnouncementError,
    ) as exc:
        logger.info(
            "Экземпляр #%s недоступен: черновик #%s не читается (%s).",
            instance.id,
            instance.draft_id,
            exc,
        )
        return None


async def _stale_answer(update: Update) -> None:
    """Короткое пояснение на устаревшую кнопку: без правок и сообщений."""
    await wizard._answer(update, announcement_controls.STALE_INSTANCE_TEXT, alert=True)


async def handle_panel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Действие панели готового анонса по id экземпляра (§26–27, §32).

    Спиннер на клиенте закрывается `answer()` в любом исходе: устаревшая
    кнопка получает пояснение, действующая — выбранный экран. Данные
    черновика здесь не меняются: это навигация, правку делает сам редактор.
    """
    parsed = announcement_controls.parse_panel_callback(ctx.query(update).data)
    if parsed is None:
        # Формат уже отфильтрован `PANEL_CALLBACK_PATTERN`; подделанный
        # callback всё равно не должен оставить клиент без ответа.
        await _stale_answer(update)
        return
    action, instance_id = parsed

    instance = _instance(instance_id)
    if instance is None or instance.is_archived:
        _event(update, "screen.open", screen="announcement_panel", result="stale")
        await _stale_answer(update)
        return
    if instance.chat_id != ctx.chat(update).id:
        # Кнопка пересланного анонса в другом чате не открывает редактор:
        # экран и клавиатура экземпляра принадлежат чату доставки.
        _event(update, "screen.open", screen="announcement_panel", result="stale")
        await _stale_answer(update)
        return
    details = _details(ctx.user(update).id, instance)
    if details is None:
        _event(update, "screen.open", screen="announcement_panel", result="stale")
        await _stale_answer(update)
        return

    # Журнал событий (этап B): только техническое действие и id экземпляра.
    # Содержимое анонса и значения полей сюда не попадают.
    _event(
        update,
        "screen.open",
        screen=PANEL_SCREENS[action],
        result="ok",
        detail=f"instance={instance.id}",
    )
    await wizard._answer(update)
    if action == announcement_controls.ACTION_MENU:
        # Панель и сам анонс остаются на месте: «В меню» — навигация.
        await wizard._exit_to_main_menu(update, context)
        wizard.forget_conversation_state(update, context)
        return

    if action != announcement_controls.ACTION_PHOTOS and wizard._edit_blocked(details):
        # Идёт отправка: правка запрещена так же, как по кнопке «✏️ Изменить».
        # Коллекция фото объясняет запрет сама, поэтому сюда не попадает.
        await wizard._answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
        await wizard._render_publish_status(update, context, details)
        return

    if action == announcement_controls.ACTION_EXTRAS and not extras_enabled(details):
        # §4 2.6.1: у выключенного расширенного шаблона доп. полей нет.
        # Старая кнопка панели получает короткий ответ, экран не меняется;
        # состояние диалога остаётся состоянием управления анонсом.
        wizard.remember_conversation_state(update, context, wizard.AW_PREVIEW)
        await wizard._answer(update, tr("wizard.extras.disabled"), alert=True)
        return

    wizard.remember_conversation_state(update, context, PANEL_STATES[action])
    if action == announcement_controls.ACTION_DATA:
        # Открытие редактора начинает путь экземпляра: список полей ляжет
        # поверх управления, и «◀️ Назад» вернёт именно туда.
        wizard._enter_editor_path(wizard._ui(context), instance.id)
        await wizard._render_field_list(
            update, context, details, instance_id=instance.id
        )
    elif action == announcement_controls.ACTION_PHOTOS:
        # 2.5.3 §6: фото-режим — на управлении самого анонса/панели, не
        # отдельным экраном. Экземпляр известен из callback.
        await wizard._show_photos(update, context, details, instance_id=instance.id)
    else:
        # Доп. поля открываются из управления, а не из списка текстовых
        # полей: путь редактора тоже начинается с управления.
        wizard._enter_editor_path(wizard._ui(context), instance.id)
        await wizard._enter_extras(update, context, details, instance_id=instance.id)


async def handle_field_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Кнопка списка текстовых полей доставленного анонса (`anf_`, §5).

    Проверки те же, что у панели (§32): экземпляр по id, чат, владелец
    черновика и архив. Устаревшая кнопка получает короткий ответ и ничего не
    меняет. Действия: выбор поля — ровно один временный вопрос, страница —
    перерисовка списка, «Назад» — возврат управления анонсу.
    """
    parsed = announcement_controls.parse_field_callback(ctx.query(update).data)
    if parsed is None:
        await _stale_answer(update)
        return
    action, instance_id, value = parsed

    instance = _instance(instance_id)
    if instance is None or instance.is_archived:
        await _stale_answer(update)
        return
    if instance.chat_id != ctx.chat(update).id:
        await _stale_answer(update)
        return
    details = _details(ctx.user(update).id, instance)
    if details is None:
        await _stale_answer(update)
        return

    if action == announcement_controls.FIELD_ACTION_BACK:
        # Журнал событий (этап B): только навигация и id экземпляра.
        _event(
            update,
            "screen.back",
            screen=SCREEN_FIELD_LIST,
            result="ok",
            detail=f"instance={instance.id}",
        )
        await wizard._answer(update)
        # Список полей — вложенный экран редактора: «◀️ Назад» возвращает
        # ровно на управление анонсом и оставляет путь редактора этого
        # экземпляра (без него следующий «Назад» из доп. полей снова искал
        # бы путь в истории конструктора).
        wizard._enter_editor_path(wizard._ui(context), instance.id)
        await wizard._show_announcement_controls(
            context, ctx.chat(update).id, details, instance=instance
        )
        wizard.remember_conversation_state(update, context, wizard.AW_PREVIEW)
        return

    if action == announcement_controls.FIELD_ACTION_PAGE:
        await wizard._answer(update)
        state = await wizard._render_field_list(
            update,
            context,
            details,
            instance_id=instance.id,
            page=wizard._field_page(int(value or 0)),
        )
        wizard.remember_conversation_state(update, context, state)
        return

    if wizard._edit_blocked(details):
        # Идёт отправка: правка запрещена так же, как по кнопке панели.
        await wizard._answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
        wizard.remember_conversation_state(update, context, wizard.AW_PREVIEW)
        return
    await wizard._answer(update)
    state = await wizard._open_field_question(
        update, context, details, instance, int(value or 0)
    )
    wizard.remember_conversation_state(update, context, state)


async def handle_field_input_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Кнопки ВВОДА значения поля доставленного анонса (`anq_`, 2.5.4 §1).

    Один маршрут на все типы полей и все действия: копий на каждое поле нет.
    Проверки те же, что у списка и панели (§32): экземпляр по id, чат,
    владелец черновика, архив. Устаревшая кнопка получает короткий ответ и
    ничего не меняет.

    Готовые варианты (время, дата, выбор) сохраняются общим
    `_save_field_input_value`: значение применяется к доставленной версии на
    месте, а клавиатура того же сообщения возвращается к списку полей.
    «Отменить» (`abort`) прекращает ввод, ничего не записывая: прежнее
    значение остаётся, автор снова видит список полей. Устаревшие «Назад» и
    «Пропустить» прежних клавиатур обрабатываются так же — без записи.
    Уровни календаря перерисовывают клавиатуру ввода, новых сообщений не
    появляется.
    """
    parsed = announcement_controls.parse_field_input_callback(ctx.query(update).data)
    if parsed is None:
        await _stale_answer(update)
        return
    action, instance_id, brick_id, tail = parsed

    instance = _instance(instance_id)
    if instance is None or instance.is_archived:
        await _stale_answer(update)
        return
    if instance.chat_id != ctx.chat(update).id:
        await _stale_answer(update)
        return
    details = _details(ctx.user(update).id, instance)
    if details is None:
        await _stale_answer(update)
        return
    field = wizard._field_for_input(ctx.user(update).id, details, instance_id, brick_id)
    if field is None:
        # Поле ушло из анонса, принадлежит площадке (§5) или экземпляр
        # переиздан: подделанная/старая кнопка не пишет значение.
        await _stale_answer(update)
        return

    if wizard._edit_blocked(details):
        # Идёт отправка: правка запрещена так же, как по кнопке панели.
        await wizard._answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
        wizard.remember_conversation_state(update, context, wizard.AW_PREVIEW)
        return

    await wizard._answer(update)
    if action == announcement_controls.FIELD_INPUT_ACTION_CANCEL:
        # 2.5.4 §3: отмена НЕОПУБЛИКОВАННОЙ правки с экрана ошибки длины.
        # Хвост — ревизия черновика: позднее/повторное нажатие после
        # успешного сохранения не откатывает новое значение.
        if len(tail) != 1 or not tail[0].isdigit():
            await _stale_answer(update)
            return
        _event(
            update,
            "field.edit.cancel",
            screen=SCREEN_FIELD_INPUT,
            result="ok",
            detail=f"instance={instance.id} field={brick_id}",
        )
        state = await wizard._cancel_field_length_edit(
            update, context, details, instance, field, int(tail[0])
        )
        wizard.remember_conversation_state(update, context, state)
        return

    if action == announcement_controls.FIELD_INPUT_ACTION_ABORT:
        # 2.5.5 §1: «Отменить» на клавиатуре ВВОДА поля. Незавершённый ввод
        # отбрасывается, прежнее значение остаётся: до кнопки в БД ничего не
        # писалось. Автор возвращается к списку полей (он же снимает режим
        # ввода), текстовых сообщений не отправляется.
        _event(
            update,
            "field.edit.cancel",
            screen=SCREEN_FIELD_INPUT,
            result="ok",
            detail=f"instance={instance.id} field={brick_id}",
        )
        state = await wizard._return_to_field_list(
            update, context, details, instance.id
        )
        wizard.remember_conversation_state(update, context, state)
        return

    if action in {
        announcement_controls.FIELD_INPUT_ACTION_BACK,
        announcement_controls.FIELD_INPUT_ACTION_SKIP,
    }:
        # «Пропустить» не очищает значение (2.4, требование VI): прежнее
        # значение остаётся, меняется только экран.
        state = await wizard._return_to_field_list(
            update, context, details, instance.id
        )
        wizard.remember_conversation_state(update, context, state)
        return

    value: object | None = None
    stay = False
    if action == announcement_controls.FIELD_INPUT_ACTION_TIME:
        if field.value_type != BRICK_TYPE_TIME:
            await _stale_answer(update)
            return
        value = ":".join(tail)
        if value not in wizard_questions.TIME_PRESETS:
            await _stale_answer(update)
            return
    elif action == announcement_controls.FIELD_INPUT_ACTION_DAY:
        if field.value_type != BRICK_TYPE_DATE:
            await _stale_answer(update)
            return
        value = _field_input_day(field, tail)
        if value is None:
            await _stale_answer(update)
            return
    elif action == announcement_controls.FIELD_INPUT_ACTION_CHOICE:
        if field.value_type != BRICK_TYPE_CHOICE:
            await _stale_answer(update)
            return
        found = _field_input_choice(field, tail)
        if found is None:
            await _stale_answer(update)
            return
        value, stay = found
    elif action == announcement_controls.FIELD_INPUT_ACTION_CHOICE_DONE:
        if field.value_type != BRICK_TYPE_CHOICE or not field.rules.get("multiple"):
            await _stale_answer(update)
            return
        if field.status == wizard.FIELD_STATUS_MISSING:
            await wizard._answer(
                update, tr("alerts.wizard.otmette_hotya_by_odin"), alert=True
            )
            wizard.remember_conversation_state(update, context, wizard.AW_PREVIEW)
            return

    if action == announcement_controls.FIELD_INPUT_ACTION_CHOICE_DONE:
        state = await wizard._return_to_field_list(
            update, context, details, instance.id
        )
        wizard.remember_conversation_state(update, context, state)
        return

    if value is None:
        # Календарь: новый уровень той же клавиатуры, без записи значения.
        if field.value_type != BRICK_TYPE_DATE:
            await _stale_answer(update)
            return
        view = _field_input_calendar_view(field, instance.id, action, tail)
        if view is None:
            await _stale_answer(update)
            return
        state = await wizard._show_field_input(
            update, context, details, instance, field, view=view
        )
        wizard.remember_conversation_state(update, context, state)
        return

    state = await wizard._save_field_input_value(
        update, context, details, instance, field, value, stay=stay
    )
    wizard.remember_conversation_state(update, context, state)


def _field_input_day(field, tail: tuple[str, ...]) -> str | None:
    """Дата из `anq_day` в формате значения (`ДД.ММ.ГГГГ`) или `None`."""
    if len(tail) != 1:
        return None
    try:
        day = date.fromisoformat(tail[0])
    except ValueError:
        return None
    start, end = wizard_questions.calendar_bounds(dict(field.rules))
    if day < start or day > end:
        return None
    return day.strftime("%d.%m.%Y")


def _field_input_choice(field, tail: tuple[str, ...]) -> tuple[object, bool] | None:
    """
    Значение выбора по номеру варианта: `(значение, остаться_на_вводе)`.

    Множественный выбор отмечает/снимает вариант и остаётся на клавиатуре
    (автор отмечает несколько подряд), одиночный — сохраняет и закрывает.
    """
    if not tail or not tail[0].isdigit():
        return None
    options = wizard_questions.choice_options(field)
    index = int(tail[0])
    if index < 0 or index >= len(options):
        return None
    option = options[index]
    if not field.rules.get("multiple"):
        return option, False
    selected = wizard_questions.selected_choices(field)
    if option in selected:
        selected = [item for item in selected if item != option]
    else:
        selected.append(option)
    return selected, True


def _field_input_calendar_view(
    field, instance_id: int, action: str, tail: tuple[str, ...]
):
    """
    Уровень календаря по действию `anq_`; `None` — адрес уровня неверен.

    Устаревший уровень (диапазон/месяц вне горизонта) безопасно сводится к
    первому экрану: сломанная клавиатура не показывается.
    """
    if action == announcement_controls.FIELD_INPUT_ACTION_CALQUICK:
        return announcement_field_input.CalendarView()
    if action == announcement_controls.FIELD_INPUT_ACTION_CALENDAR:
        view = announcement_field_input.CalendarView(
            announcement_field_input.VIEW_MONTHS
        )
    elif action == announcement_controls.FIELD_INPUT_ACTION_RANGE:
        if len(tail) != 1 or not tail[0].isdigit():
            return None
        view = announcement_field_input.CalendarView(
            announcement_field_input.VIEW_RANGE, index=int(tail[0])
        )
    elif action == announcement_controls.FIELD_INPUT_ACTION_MONTH:
        if len(tail) != 2 or not all(item.isdigit() for item in tail):
            return None
        view = announcement_field_input.CalendarView(
            announcement_field_input.VIEW_MONTH,
            year=int(tail[0]),
            month=int(tail[1]),
        )
    else:
        return None
    if not announcement_field_input.calendar_rows(
        field, int(instance_id), field.brick_id, view
    ):
        return announcement_field_input.CalendarView()
    return view


def announcement_panel_handlers() -> list[BaseHandler]:
    """
    Обработчик кнопок готового анонса — один на всё приложение.

    Регистрируется в группе 0 раньше конструктора: `anp_` не пересекается с
    префиксами сценариев, а порядок гарантирует, что нажатие не съест
    fallback активного диалога.
    """
    return [
        CallbackQueryHandler(
            handle_panel_callback,
            pattern=announcement_controls.PANEL_CALLBACK_PATTERN,
        )
    ]


def field_handlers() -> list[BaseHandler]:
    """
    Кнопки списка текстовых полей (`anf_`) и ВВОДА значения (`anq_`).

    Регистрируются в той же группе 0 и в том же порядке, что и панель:
    список и ввод живут на самом анонсе и обязаны работать после рестарта
    службы, когда состояния диалога нет.
    """
    return [
        CallbackQueryHandler(
            handle_field_callback,
            pattern=announcement_controls.FIELD_CALLBACK_PATTERN,
        ),
        CallbackQueryHandler(
            handle_field_input_callback,
            pattern=announcement_controls.FIELD_INPUT_CALLBACK_PATTERN,
        ),
    ]


__all__ = [
    "handle_field_callback",
    "handle_field_input_callback",
    "handle_panel_callback",
    "announcement_panel_handlers",
    "field_handlers",
]
