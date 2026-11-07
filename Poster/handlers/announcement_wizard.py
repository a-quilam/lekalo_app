# handlers/announcement_wizard.py
"""
Telegram-интерфейс адаптивного мастера анонсов Stage 27B/27C.

Мастер использует только сервисы Stage 27A и компилятор Stage 24:

- место и вариант названия выбираются в существующем справочнике;
- шаблон фиксируется независимым снимком, повторно снимок не создаётся;
- вопросы строит `announcement_store.build_announcement_questions`, а не
  отдельный алгоритм Telegram-слоя;
- значения и дополнительные кирпичики сохраняются по `brick_id`;
- предпросмотр собирается `build_announcement_preview` из снимка;
- фотографии — отдельная коллекция вложений анонса, не кирпичики;
- публикация замораживает готовый HTML/фото и отправляет их единым
  доставщиком `delivery.send_announcement_post` (тот же формат и прогресс,
  что у остальных сценариев).

В `user_data` хранится только навигационное состояние интерфейса. Место,
шаблон, снимок, ответы, extras, фото, этап, revision и прогресс публикации
живут в SQLite. Поэтому рестарт процесса не теряет работу: черновик
открывается из базы в «📂 Мои анонсы». Кнопки содержат draft_id и
revision; устаревший экран не перезаписывает данные.

Легаси-мастер, его WIP и формат публикации удалены в S5 — единственный
конструктор и единственный доставщик.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from functools import partial
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import announcement_controls
import delivery
from announcement_instances import (
    archive_instance,
    get_active_instance,
    get_instance,
    register_instance,
    replace_instance,
    set_panel_message,
    strip_panel,
    update_instance_fingerprint,
)
from announcement_store import (
    FIELD_STATUS_KNOWN,
    FIELD_STATUS_MISSING,
    FIELD_STATUS_SKIPPED,
    FIELD_STATUS_VALID,
    PUBLICATION_STATUS_FAILED,
    PUBLICATION_STATUS_PARTIAL,
    PUBLICATION_STATUS_SENDING,
    PUBLICATION_STATUS_SENT,
    AnnouncementActorError,
    AnnouncementConflictError,
    AnnouncementError,
    AnnouncementFieldState,
    AnnouncementNotFoundError,
    AnnouncementPreview,
    add_announcement_extra,
    announcement_value_snapshot,
    begin_announcement_publication,
    build_announcement_fields,
    build_announcement_preview,
    build_announcement_render_payload,
    choose_announcement_template,
    clear_announcement_photos,
    confirm_announcement_place,
    delete_announcement,
    extras_available,
    extras_available_for_draft,
    get_announcement_draft,
    list_announcement_drafts,
    mark_announcement_publication_failed,
    mark_announcement_publication_sent,
    record_announcement_publication_progress,
    record_in_place_publication,
    remove_announcement_extra,
    remove_announcement_photo,
    reset_failed_announcement_publication,
    restore_announcement_value,
    resume_announcement_publication,
    save_announcement_value,
    set_announcement_live_message_ids,
    set_announcement_stage,
    start_announcement_wizard,
)
from announcement_wizard import (
    WizardStage,
    answer_payload_to_raw,
    extra_candidate_brick_ids,
    snapshot_brick_ids_by_code,
)
from brick_store import BrickError
from brick_types import (
    BRICK_SOURCE_PLACE,
    BRICK_TYPE_CHOICE,
    BRICK_TYPE_DATE,
    BRICK_TYPE_LONG_TEXT,
    BRICK_TYPE_NUMBER,
    BRICK_TYPE_SHORT_TEXT,
    BRICK_TYPE_TIME,
    SYSTEM_BRICKS,
    BrickValueResult,
    BrickValueStatus,
    brick_title_display,
)
from database import SessionLocal
from handlers import (
    announcement_field_input,
    wizard_live,
    wizard_photos,
    wizard_questions,
)
from place_store import (
    get_place_details,
    list_places,
    search_places,
)
from template_compiler import (
    PLACEHOLDER_TEXT,
    TemplateCompileError,
    check_announcement_length,
)
from template_store import (
    TemplateError,
    get_standard_template,
    list_personal_templates,
)
from template_types import TEMPLATE_CONTENT, TEMPLATE_EXTENDED_CONTENT
from utils import (
    calendar_view,
    current_value,
    keyboard_layouts,
    pagination,
    screen,
    ui_humor,
    user_events,
)
from utils import tg_context as ctx
from utils.parsers import app_today
from utils.place_labels import place_title
from utils.tg_html import capture_message_text, escape_html
from utils.ui_texts import join_blocks, tr, tr_plain, tr_safe

# Poster 2.0 S3 п.1: вид вопроса/календаря вынесен в `wizard_questions`,
# поведение прежнее. Имена с подчёркиванием сохранены как локальные
# адаптеры: оркестратор диалога не переписывается целиком, а тесты и
# соседние экраны продолжают работать без изменений.
_question_text = wizard_questions.question_text
_type_hint = wizard_questions.type_hint
_date_rules_hint = wizard_questions.date_rules_hint
_number_rules_hint = wizard_questions.number_rules_hint
_choice_options = wizard_questions.choice_options
_selected_choices = wizard_questions.selected_choices
_field_status_text = wizard_questions.field_status_text
_field_value_text = wizard_questions.field_value_text
_parse_rule_date = wizard_questions.parse_rule_date
_calendar_bounds = wizard_questions.calendar_bounds
_day_rows = wizard_questions.day_rows

logger = logging.getLogger(__name__)

CONVERSATION_NAME = "announcement_wizard"

AW_PLACE, AW_TEMPLATE, AW_QUESTIONS, AW_EXTRAS, AW_PREVIEW = range(5)

# Poster 2.3 H1: ключ UI-снимка в `user_states.payload`. PTB хранит
# состояние ConversationHandler только в памяти, поэтому после рестарта
# службы следующий текст уходил в глобальную подсказку главного меню, а
# введённое значение терялось. Снимок — минимальный: какой черновик активен
# и в каком режиме ввода находится человек. Данные черновика, как и прежде,
# живут в `announcement_drafts`; здесь только навигация.
RESUME_PAYLOAD_KEY = "announcement_resume"
RESUME_MODE_TEXT = "text"
RESUME_MODE_PHOTO = "photo"
# Режимы, в которых текст продолжает шаг, и этапы черновика для каждого.
RESUME_TEXT_STAGES = frozenset({WizardStage.QUESTIONS, WizardStage.EXTRAS})

# Poster 2.5 §14: длинные однотипные списки конструктора идут по пять
# записей на страницу — тем же механизмом, что и остальные списки бота.
PLACE_PAGE_SIZE = pagination.PAGE_SIZE
TEMPLATE_PAGE_SIZE = pagination.PAGE_SIZE
EXTRA_PAGE_SIZE = pagination.PAGE_SIZE
# Компактный набор фотографий — своя сетка, контракт §14 на него не
# распространяется.
PHOTO_PAGE_SIZE = 8
PREVIEW_VISIBLE_LIMIT = 1800
FIELD_DISPLAY_LIMIT = 120
MAX_PREVIEW_FIELDS = 12
MAX_PREVIEW_EXTRAS = 6

# 2.5.4 §6: заголовок мероприятия — системный кирпичик `[название]`. Код,
# а не ID: в рабочей базе brick_id берётся из снимка анонса.
TITLE_BRICK_CODE = "название"
# Подпись строки «Мои анонсы»: заголовок сокращается первым, поэтому место
# ограничено отдельно — так место и состояние видны в любом случае (§6.3).
ENTRY_LABEL_LIMIT = 62
ENTRY_PLACE_LIMIT = 34
ENTRY_TITLE_MIN = 8

STAGE_LABELS = {
    WizardStage.PLACE.value: tr("wizard.module.mesto"),
    WizardStage.TEMPLATE.value: tr("wizard.module.shablon"),
    WizardStage.QUESTIONS.value: tr("wizard.module.voprosy"),
    WizardStage.EXTRAS.value: tr("wizard.module.dop_polya"),
    WizardStage.PREVIEW.value: tr("wizard.module.obzor"),
    WizardStage.COMPLETED.value: tr("wizard.module.opublikovan"),
}

STALE_TEXT = tr("errors.stale.wizard")
EXPIRED_TEXT = tr("errors.expired.wizard")
PRIVATE_CHAT_ONLY = tr("errors.private_chat_only")


def _user_id(update: Update) -> int:
    return ctx.user(update).id


# Короткие технические коды журнала событий (этап B). `SCREEN_FIELD_INPUT`
# повторяет код из `handlers/announcement_panel.py`: экран один и тот же,
# а журнал должен оставаться сопоставимым по коду.
SCREEN_FIELD_INPUT = "field_input"


def _event_field_saved(update: Update, instance: object, field: object) -> None:
    """
    Пишет в журнал факт сохранения значения поля доставленного анонса.

    В `detail` — только идентификаторы экземпляра и кирпичика: значение поля
    (то есть содержимое анонса) сюда не попадает никогда. Ошибка журнала не
    поднимается — `event_log` её глотает.
    """
    user_events.event_log(
        _user_id(update),
        "field.edit.save",
        screen=SCREEN_FIELD_INPUT,
        result="ok",
        detail=(
            f"instance={getattr(instance, 'id', '?')} "
            f"field={getattr(field, 'brick_id', '?')}"
        ),
        # Фабрика сессии ЭТОГО модуля: тесты подменяют её на временную базу.
        session_factory=SessionLocal,
    )


def _is_private_chat(update: Update) -> bool:
    """
    Анонс доставляется только в личный чат автора.

    Мастер можно открыть и в группе (`/new_wizard`), но автоматическая
    выдача готового текста туда превратила бы личный анонс в сообщение для
    посторонних. Проверка стоит на каждом пути отправки, а не только на
    входе: callback может прийти из чата, где диалог начинался иначе.
    """
    chat = update.effective_chat
    if chat is None:
        return False
    return chat.type == ChatType.PRIVATE


def _ui(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    data = ctx.data(context)
    ui = data.get("announcement_ui")
    if not isinstance(ui, dict):
        ui = _fresh_ui()
        data["announcement_ui"] = ui
    return ui


def _fresh_ui() -> dict[str, Any]:
    return {
        "draft_id": None,
        "revision": None,
        # Poster 2.4 P2b: реальный путь по экранам (требование V).
        "nav_current": None,
        "nav_stack": [],
        "home_page": 0,
        "template_page": 0,
        "extras_page": 0,
        "question_index": 0,
        "active_brick_id": None,
        "active_source": None,
        "editing": False,
        # 2.5.3 §5: временный вопрос открыт из списка текстовых полей
        # доставленного анонса. Вопрос ровно один, после сохранения экран
        # закрывается, а управление возвращается анонсу.
        "field_edit": False,
        "field_instance_id": None,
        # 2.5.4 §1: ввод значения поля идёт НА САМОМ анонсе (клавиатура
        # сообщения или единственной панели). Значение — id экземпляра;
        # `field_input_view` — текущий уровень календаря (None — первый).
        "field_input": None,
        "field_input_view": None,
        "photos_page": 0,
        "photo_add": False,
        # H1: шаг фото ждёт ввода — сохраняется в resume-снимке.
        "photo_step": False,
        "place": {
            "phase": "home",
            "query": "",
            "page": 0,
            "place_id": None,
            "name_id": None,
            "return_phase": "hub",
            "return_page": 0,
            "idempotency_key": None,
        },
    }


def _clear_ui(context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.data(context).pop("announcement_ui", None)


def _reset_for_place(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    ui = _fresh_ui()
    ui["place"]["phase"] = "hub"
    ui["place"]["idempotency_key"] = uuid.uuid4().hex
    ctx.data(context)["announcement_ui"] = ui
    return ui


def _set_details(ui: dict[str, Any], details) -> None:
    ui["draft_id"] = details.draft_id
    ui["revision"] = details.revision


def _clear_active(ui: dict[str, Any]) -> None:
    ui["active_brick_id"] = None
    ui["active_source"] = None
    ui["editing"] = False
    ui["photo_add"] = False
    # H1: шаг фото — тоже режим ввода. Снимается на любом экране без ввода,
    # иначе снимок «ждём фото» пережил бы переход к обзору.
    ui["photo_step"] = False
    # 2.5.3 §5: временный вопрос из списка текстовых полей — тоже режим
    # ввода. Флаг снимается на любом экране без ввода, иначе следующая
    # правка значения ушла бы в чужой сценарий закрытия вопроса.
    ui.pop("field_edit", None)
    ui.pop("field_instance_id", None)
    # 2.5.4 §1: ввод значения на самом анонсе — тоже режим ввода. Снимается
    # на любом экране без ввода, иначе следующий текст ушёл бы не туда.
    ui.pop("field_input", None)
    ui.pop("field_input_view", None)


# —— Poster 2.4 P2b: навигационный стек конструктора (требование V) ————————
#
# «◀️ Назад» обязан вести на ТОТ экран, с которого человек пришёл, а не в
# главное меню и не «на шаг раньше по индексу». Путь хранится здесь же, в
# `announcement_ui` (user_data), и только как история экранов: данные
# черновика, значения и фото по-прежнему живут в SQLite. После рестарта
# службы user_data теряется — стек пуст, и «Назад» показывает безопасный
# экран текущего этапа (`_resume_current`/фолбэки маршрутов).
NAV_STACK_LIMIT = 32

# Имена экранов пути. Только они попадают в стек; маршруты возврата
# сопоставляют имя экрана с реальным рендером.
NAV_HOME = "home"
NAV_TEMPLATE = "template"
NAV_QUESTION = "question"
NAV_MANAGER = "manager"
NAV_EXTRAS = "extras"
NAV_EXTRA_VALUE = "extra_value"
NAV_PHOTOS = "photos"
NAV_PREVIEW = "preview"
NAV_FIELDS = "fields"
# 2.5.5 §3: общий редактор доставленного анонса — клавиатура управления
# экземпляром («Текстовые поля | Фото | Доп. поля | В меню»). Это корень
# собственного пути редактора: из него открываются список полей и доп. поля,
# и «Назад» из них возвращает сюда, а не в «Текстовые поля».
NAV_EDITOR = "editor"

# Экраны пути редактора доставленного анонса. Их записи несут id экземпляра:
# путь одного анонса не переиспользуется другим (даже в том же чате).
EDITOR_SCREENS = frozenset({NAV_EDITOR, NAV_FIELDS, NAV_EXTRAS, NAV_EXTRA_VALUE})
# Глубина пути редактора: одно место управления, список полей и два уровня
# вложенности. Больше не нужно, а ограничение не даёт пути расти бесконечно.
EDITOR_STACK_LIMIT = 8


def _nav_stack(ui: dict[str, Any]) -> list[dict[str, Any]]:
    """Стек экранов пути; после рестарта/сброса — пустой список."""
    stack = ui.get("nav_stack")
    if not isinstance(stack, list):
        stack = []
        ui["nav_stack"] = stack
    return stack


def _nav_reset(ui: dict[str, Any]) -> None:
    """Новый корневой путь (главное меню, старт конструктора)."""
    ui["nav_stack"] = []
    ui["nav_current"] = None


def _nav_identity(
    screen: str,
    *,
    index: int | None = None,
    brick_id: int | None = None,
) -> tuple:
    """
    Ключ шага пути.

    Вопрос отличается от соседнего вопроса того же экрана индексом и
    кирпичиком: это разные шаги. У остальных экранов (списки, обзоры)
    страница и позиция — данные экрана, а не новый шаг пути: «Назад» с
    листания списка ведёт туда же, откуда список открыли.
    """
    if screen == NAV_QUESTION:
        return (screen, brick_id if brick_id is not None else index)
    return (screen,)


def _nav_enter(
    ui: dict[str, Any],
    screen: str,
    *,
    page: int | None = None,
    index: int | None = None,
    brick_id: int | None = None,
    root: bool = False,
    instance_id: int | None = None,
) -> None:
    """
    Фиксирует показ экрана в пути.

    Запись несёт `draft_id`/`revision` и место в пути (страница списка,
    индекс вопроса, кирпичик), поэтому возврат восстанавливает именно тот
    экран. Повторный показ того же шага обновляет запись, а возврат на уже
    пройденный экран (например, к списку после сохранения значения)
    обрезает путь до него — копии не копятся, циклы невозможны.

    `root` — экран начинает собственный маршрут (правка доставленного
    анонса): история конструктора, из которой пришли, к нему не относится.
    Без этого дедупликация ниже принимала бы новый список полей за возврат
    на вопрос того же кирпичика, который остался в пути после доставки.

    `instance_id` — экземпляр доставленного анонса. Для экранов редактора
    (`EDITOR_SCREENS`) путь нормализуется: в нём нет истории конструктора и
    записей другого анонса, а внизу всегда лежит общий редактор
    (`NAV_EDITOR`). Поэтому «Назад» с доп. полей ведёт в управление
    анонсом, а не в «Текстовые поля».
    """
    stack = _nav_stack(ui)
    if screen in EDITOR_SCREENS and isinstance(instance_id, int):
        if root:
            stack.clear()
        _nav_editor_path(ui, stack, instance_id, screen)
    elif root:
        stack.clear()
    identity = _nav_identity(screen, index=index, brick_id=brick_id)
    entry: dict[str, Any] = {
        "screen": screen,
        "draft_id": ui.get("draft_id"),
        "revision": ui.get("revision"),
        # Источник перехода: экран, с которого пришли (для диагностики).
        "source": stack[-1]["screen"] if stack else None,
        "instance_id": instance_id if screen in EDITOR_SCREENS else None,
        "page": page,
        "index": index,
        "brick_id": brick_id,
    }
    ui["nav_current"] = screen
    if (
        stack
        and _nav_identity(
            stack[-1]["screen"],
            index=stack[-1].get("index"),
            brick_id=stack[-1].get("brick_id"),
        )
        == identity
    ):
        # Перерисовка того же шага (пагинация, повтор, ошибка ввода).
        stack[-1] = entry
        return
    if (
        len(stack) >= 2
        and _nav_identity(
            stack[-2]["screen"],
            index=stack[-2].get("index"),
            brick_id=stack[-2].get("brick_id"),
        )
        == identity
    ):
        # Действие вернуло на непосредственного родителя (сохранение
        # значения — обратно к списку): шаг пути не удваивается, цикла нет.
        stack.pop()
        stack[-1] = entry
        return
    stack.append(entry)
    if screen in EDITOR_SCREENS and isinstance(instance_id, int):
        if len(stack) > EDITOR_STACK_LIMIT:
            # Корень пути сохраняем: без него «Назад» из доп. полей некуда
            # возвращаться. Отбрасываем самые старые промежуточные шаги.
            del stack[1 : len(stack) - EDITOR_STACK_LIMIT + 1]
        return
    if len(stack) > NAV_STACK_LIMIT:
        del stack[: len(stack) - NAV_STACK_LIMIT]


def _nav_editor_path(
    ui: dict[str, Any],
    stack: list[dict[str, Any]],
    instance_id: int,
    screen: str,
) -> None:
    """
    Оставляет в пути только экраны редактора ЭТОГО экземпляра.

    Единая точка нормализации пути редактора: история конструктора, экраны
    другого анонса и устаревшие записи отбрасываются. Открытие общего
    редактора начинает путь заново — иначе «Назад» из доп. полей, открытых
    после списка полей, вело бы в «Текстовые поля», а не в управление
    анонсом. Внизу пути всегда лежит `NAV_EDITOR`: это тот экран, откуда
    открываются и список полей, и доп. поля.
    """
    if screen == NAV_EDITOR:
        # Общий редактор — корень пути: прежние вложенные экраны не
        # остаются под ним.
        stack.clear()
    else:
        stack[:] = [
            entry
            for entry in stack
            if entry.get("screen") in EDITOR_SCREENS
            and entry.get("instance_id") == instance_id
        ]
    if not stack:
        stack.append(
            {
                "screen": NAV_EDITOR,
                "draft_id": ui.get("draft_id"),
                "revision": ui.get("revision"),
                "source": None,
                "instance_id": instance_id,
                "page": None,
                "index": None,
                "brick_id": None,
            }
        )


def _enter_editor_path(ui: dict[str, Any], instance_id: int | None) -> None:
    """
    Начинает путь редактора экземпляра с общего экрана управления.

    Единая точка входа в редактор доставленного анонса: путь обрезается до
    `NAV_EDITOR`, поэтому следующие экраны (список полей, доп. поля)
    кладутся поверх управления, а «◀️ Назад» возвращает туда же. Путь
    другого экземпляра и история конструктора при этом отбрасываются.
    У живого черновика (`instance_id` нет) путь не трогается.
    """
    if isinstance(instance_id, int):
        _nav_enter(ui, NAV_EDITOR, instance_id=int(instance_id))


def _nav_entry_instance(entry: dict[str, Any] | None) -> int | None:
    """id экземпляра из записи пути редактора (None — записи нет)."""
    if not isinstance(entry, dict):
        return None
    value = entry.get("instance_id")
    return value if isinstance(value, int) else None


def _nav_pop(ui: dict[str, Any]) -> dict[str, Any] | None:
    """Снимает текущий экран и возвращает предыдущий (None — стек пуст)."""
    stack = _nav_stack(ui)
    if not stack:
        ui["nav_current"] = None
        return None
    stack.pop()
    if not stack:
        ui["nav_current"] = None
        return None
    ui["nav_current"] = stack[-1]["screen"]
    return stack[-1]


def _nav_pop_from(ui: dict[str, Any], screen: str) -> dict[str, Any] | None:
    """
    Возврат со строго ожидаемого экрана.

    Устаревшая кнопка «Назад» с уже покинутого экрана не должна снимать
    текущий шаг пути: сверяем имя экрана и при расхождении отдаём `None` —
    маршрут покажет безопасный экран этапа.
    """
    if ui.get("nav_current") != screen:
        return None
    return _nav_pop(ui)


async def _nav_render(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    entry: dict[str, Any] | None,
) -> int | None:
    """
    Показывает экран, записанный в пути (реальный предыдущий шаг).

    `None` — записи нет, она про другой черновик или экран больше
    недоступен: вызывающий маршрут обязан показать безопасный экран
    текущего этапа. Исключений наружу не выходит.
    """
    if not isinstance(entry, dict):
        return None
    screen_name = entry.get("screen")
    draft_id = entry.get("draft_id")
    if draft_id not in (None, details.draft_id):
        # Путь другого черновика не переиспользуется.
        return None
    if screen_name == NAV_QUESTION:
        fields = await _question_fields_for(update, context, details)
        if not fields:
            return None
        found = None
        brick_id = entry.get("brick_id")
        if isinstance(brick_id, int):
            found = _find_field(fields, brick_id)
        if found is None:
            index = entry.get("index")
            if isinstance(index, int) and 0 <= index < len(fields):
                found = (index, fields[index])
        if found is None:
            return None
        index, field = found
        if not _delivered_text_question_allowed(details, field):
            # Старая запись пути на поле площадки у доставленного анонса:
            # редактор текстовых полей таких вопросов не открывает (§5).
            return None
        return await _render_question(update, context, details, fields, index)
    if screen_name == NAV_MANAGER:
        return await _render_question_manager(update, context, details)
    if screen_name == NAV_EXTRAS:
        page = entry.get("page")
        return await _render_extras(
            update,
            context,
            details,
            page=page if isinstance(page, int) else None,
            instance_id=_nav_entry_instance(entry),
        )
    if screen_name == NAV_EXTRA_VALUE:
        brick_id = entry.get("brick_id")
        extra = next(
            (
                item
                for item in details.extras
                if isinstance(brick_id, int) and item.brick_id == brick_id
            ),
            None,
        )
        if extra is None:
            return None
        return await _render_extra_value_prompt(
            update,
            context,
            details,
            extra,
            instance_id=_nav_entry_instance(entry),
        )
    if screen_name == NAV_TEMPLATE:
        return await _render_template_screen(update, context, details)
    if screen_name == NAV_PHOTOS:
        page = entry.get("page")
        return await _show_photos(
            update, context, details, page=page if isinstance(page, int) else None
        )
    if screen_name == NAV_FIELDS:
        instance_id = entry.get("instance_id")
        page = entry.get("page")
        return await _render_field_list(
            update,
            context,
            details,
            instance_id=instance_id if isinstance(instance_id, int) else None,
            page=page if isinstance(page, int) else 0,
        )
    if screen_name == NAV_EDITOR:
        instance_id = _nav_entry_instance(entry)
        instance = _instance_for(details, instance_id)
        if instance is None:
            return None
        # Общий редактор анонса: возвращаем управление экземпляру (клавиатура
        # анонса или текст единственной панели). Новых сообщений нет, а
        # временный служебный экран (доп. поля, шаг мастера) закрывается:
        # иначе в чате осталась бы вторая, уже нерабочая клавиатура.
        await _close_service_screen(update, context)
        await _show_announcement_controls(
            context, ctx.chat(update).id, details, instance=instance
        )
        return AW_PREVIEW
    if screen_name == NAV_PREVIEW:
        return await _render_preview(update, context, details)
    if screen_name == NAV_HOME:
        return await _render_home(update, context)
    return None


# —— Poster 2.3 H1: авто-resume активного черновика ————————————————————


def _resume_snapshot(
    ui: dict[str, Any], chat_id: int | None = None
) -> dict[str, Any] | None:
    """
    Минимальный снимок «экран ждёт ввода» для `user_states.payload`.

    None — экран ничего не ждёт (меню, списки, обзор, доставка): после
    рестарта службы текст должен уйти в обычную подсказку, а не в черновик.
    Данных черновика и секретов здесь нет — только `draft_id`, режим и
    идентификатор кирпичика; значения живут в `announcement_drafts`.
    """
    draft_id = ui.get("draft_id")
    source = ui.get("active_source")
    brick_id = ui.get("active_brick_id")
    if brick_id is not None and source in {"question", "extra", "field"} and draft_id:
        snapshot: dict[str, Any] = {
            "mode": RESUME_MODE_TEXT,
            "draft_id": int(draft_id),
            "brick_id": int(brick_id),
            "source": str(source),
        }
        instance_id = ui.get("field_input")
        if source == "field":
            # 2.5.4 §1: ввод значения поля доставленного анонса живёт на самом
            # анонсе/панели, поэтому для продолжения после рестарта нужен id
            # экземпляра: по нему восстанавливаются адреса сообщений. Чат
            # хранится рядом: анонс принадлежит чату доставки, и ввод в другом
            # чате (пересланная кнопка) продолжаться не должен.
            if not isinstance(instance_id, int) or not isinstance(chat_id, int):
                return None
            snapshot["instance_id"] = int(instance_id)
            snapshot["chat_id"] = int(chat_id)
        return snapshot
    if ui.get("photo_step") and draft_id:
        return {"mode": RESUME_MODE_PHOTO, "draft_id": int(draft_id)}
    return None


def _remember_resume(
    user_id: int,
    user_data: dict[str, Any],
    chat_id: int,
    ui: dict[str, Any],
) -> None:
    """
    Сохраняет ref экрана И режим ввода одной записью в `user_states`.

    Заменяет `screen.save_screen_ref` на пути отрисовки: раньше снимок
    делался без resume-ключа, теперь режим ввода обновляется там же — не
    появляется ни второй записи, ни расхождения между ref и снимком.
    Сбой БД не ломает отрисовку: экран уже нарисован.
    """
    payload = screen.load_payload(user_id) or {}
    snapshot = _resume_snapshot(ui, chat_id)
    current = payload.get(RESUME_PAYLOAD_KEY)
    if snapshot is None:
        payload.pop(RESUME_PAYLOAD_KEY, None)
        if current is not None:
            logger.debug("Resume-снимок снят: user_id=%s", user_id)
    else:
        payload[RESUME_PAYLOAD_KEY] = snapshot
        if current != snapshot:
            logger.debug(
                "Resume-снимок обновлён: user_id=%s draft_id=%s фаза=%s",
                user_id,
                snapshot.get("draft_id"),
                snapshot["mode"],
            )
    screen.save_state(user_id, user_data, chat_id, payload or None)


def _clear_resume(
    user_id: int,
    user_data: dict[str, Any],
    chat_id: int,
) -> None:
    """
    Снимает resume-снимок при явном выходе из конструктора.

    Закрытый конструктор (`/cancel`, возврат в меню, другой раздел) не
    должен «оживать» от обычного текста: черновик остаётся в «📂 Мои
    анонсы», но ввод продолжается только явным открытием. Ref экрана
    сохраняется: screen_message_id не теряется вместе со снимком.
    """
    payload = screen.load_payload(user_id)
    if not payload or RESUME_PAYLOAD_KEY not in payload:
        return
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    payload.pop(RESUME_PAYLOAD_KEY, None)
    screen.save_state(user_id, user_data, chat_id, payload or None)


def drop_resume_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Снимает авто-resume при явном выходе из конструктора (Poster 2.3 H1).

    Нужен глобальным экранам меню (`handlers/main_menu.py`): `/cancel`,
    `/start`, `/help` и «🏠 В меню» после рестарта службы обрабатываются там,
    где состояния ConversationHandler уже нет. Без этого вызова снимок
    «экран ждёт ввода» пережил бы выход, и следующий текст записался бы в
    значение шага, затерев сохранённое. Обёртка скрывает внутренние
    аргументы `_clear_resume` от меню.
    """
    _clear_resume(_user_id(update), ctx.data(context), ctx.chat(update).id)


def _other_session_active(user_id: int) -> bool:
    """
    Идёт ли другой сценарий, который сам ждёт ввод.

    «Места», кабинет, кирпичики и шаблоны регистрируются ПОСЛЕ конструктора
    и глобального меню: без этой проверки resume перехватил бы их текст.
    """
    from admin_state import has_admin_session
    from brick_state import has_brick_draft
    from place_state import has_place_session
    from template_state import has_template_session

    return (
        has_place_session(user_id)
        or has_admin_session(user_id)
        or has_brick_draft(user_id)
        or has_template_session(user_id)
    )


def _resume_target(
    user_id: int,
    snapshot: dict[str, Any],
    chat_id: int | None = None,
):
    """
    Проверяет снимок по АКТУАЛЬНОМУ состоянию черновика.

    Возвращает `(details, ui)` — готовое навигационное состояние для
    продолжения шага, либо None. Проверка в `check_update` (до выбора
    обработчика): если resume невозможен, update обязан уйти в обычную
    подсказку главного меню, а не пропасть.

    `chat_id` — чат, из которого пришёл update: ввод значения доставленного
    анонса продолжается только в чате самого экземпляра (2.5.4 §1).
    """
    mode = snapshot.get("mode")
    draft_id = snapshot.get("draft_id")
    if mode not in {RESUME_MODE_TEXT, RESUME_MODE_PHOTO} or not isinstance(
        draft_id, int
    ):
        return None
    try:
        details = _session_details(user_id, draft_id)
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        logger.warning(
            "Resume невозможен: user_id=%s draft_id=%s фаза=%s — черновик "
            "не найден или недоступен.",
            user_id,
            draft_id,
            mode,
        )
        return None

    ui = _fresh_ui()
    _set_details(ui, details)
    if mode == RESUME_MODE_TEXT:
        brick_id = snapshot.get("brick_id")
        source = snapshot.get("source")
        if source == "field":
            # 2.5.4 §1: ввод значения поля доставленного анонса. Восстанавливаем
            # его только пока экземпляр и поле на месте; иначе снимок снимается
            # и текст уходит обычной подсказкой — «зависшего» ввода, который
            # писал бы в чужое поле, не остаётся (список полей открывается
            # кнопкой управления заново).
            instance_id = snapshot.get("instance_id")
            snap_chat_id = snapshot.get("chat_id")
            if (
                not isinstance(brick_id, int)
                or not isinstance(instance_id, int)
                or not isinstance(snap_chat_id, int)
            ):
                return None
            if not details.publication_sent:
                return None
            instance = _instance_for(details, instance_id)
            if instance is None or int(instance.chat_id) != snap_chat_id:
                logger.info(
                    "Resume не нужен: user_id=%s draft_id=%s — экземпляр #%s "
                    "не в этом чате.",
                    user_id,
                    draft_id,
                    instance_id,
                )
                return None
            if chat_id is not None and int(chat_id) != int(instance.chat_id):
                # Update пришёл из другого чата (пересланная клавиатура):
                # ввод не продолжается, значение не пишется в чужой анонс.
                logger.info(
                    "Resume не нужен: user_id=%s draft_id=%s — update из чата "
                    "%s, а анонс #%s в чате %s.",
                    user_id,
                    draft_id,
                    chat_id,
                    instance_id,
                    instance.chat_id,
                )
                return None
            if _field_for_input(user_id, details, instance_id, brick_id) is None:
                logger.info(
                    "Resume не нужен: user_id=%s draft_id=%s — поле %s больше "
                    "не редактируется.",
                    user_id,
                    draft_id,
                    brick_id,
                )
                return None
            ui["active_brick_id"] = brick_id
            ui["active_source"] = "field"
            ui["field_input"] = instance_id
            return details, ui
        if details.stage not in RESUME_TEXT_STAGES:
            logger.info(
                "Resume не нужен: user_id=%s draft_id=%s фаза=%s — этап %s.",
                user_id,
                draft_id,
                mode,
                details.stage,
            )
            return None
        if not isinstance(brick_id, int) or source not in {"question", "extra"}:
            return None
        if source == "question":
            fields = _askable_fields(_load_fields(user_id, draft_id))
            found = _find_field(fields, brick_id)
            if found is None or not _delivered_text_question_allowed(details, found[1]):
                logger.info(
                    "Resume не нужен: user_id=%s draft_id=%s — кирпичик %s "
                    "больше не спрашивается.",
                    user_id,
                    draft_id,
                    brick_id,
                )
                return None
        elif all(extra.brick_id != brick_id for extra in details.extras):
            return None
        ui["active_brick_id"] = brick_id
        ui["active_source"] = source
        return details, ui

    photos_allowed = details.stage == WizardStage.PREVIEW or (
        details.publication_sent and details.stage == WizardStage.COMPLETED
    )
    if not photos_allowed:
        logger.info(
            "Resume не нужен: user_id=%s draft_id=%s фаза=%s — этап %s.",
            user_id,
            draft_id,
            mode,
            details.stage,
        )
        return None
    ui["photo_step"] = True
    return details, ui


def _load_resume(user_id: int) -> dict[str, Any] | None:
    """Resume-снимок из `user_states.payload` (None — ввода не ждём)."""
    payload = screen.load_payload(user_id)
    if not payload:
        return None
    snapshot = payload.get(RESUME_PAYLOAD_KEY)
    return snapshot if isinstance(snapshot, dict) else None


class AnnouncementResumeHandler(MessageHandler):
    """
    Текст/фото, продолжающие шаг активного черновика после рестарта службы.

    PTB хранит состояние ConversationHandler только в памяти, поэтому после
    перезапуска процесса следующий текст попадал в глобальную подсказку
    главного меню, а введённое значение терялось (журнал 30.10, 23:48/23:51).
    Обработчик регистрируется ПОСЛЕ диалога конструктора в той же группе:
    пока состояние диалога живо, он не вызывается вовсе. Resume включается
    только когда:

    - чат личный (в группе анонс не продолжается);
    - в БД есть авто-снимок «экран ждёт ввода» по активному черновику;
    - не идёт другой сценарий ввода («Места», кабинет, кирпичики, шаблоны).

    Записи, не подходящие под условия, получают прежнюю подсказку меню.
    """

    def check_update(self, update):
        matched = super().check_update(update)
        if not matched:
            return None
        if not _is_private_chat(update):
            return None
        user = getattr(update, "effective_user", None)
        if user is None:
            return None
        # Сначала дешёвая проверка снимка: у большинства обычных сообщений
        # его нет, и запросы о чужих сценариях не нужны.
        if _load_resume(user.id) is None:
            return None
        if _other_session_active(user.id):
            return None
        return matched


async def handle_resume_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Продолжает активный шаг черновика после потери состояния диалога.

    Значение сохраняется обычным путём шага (CAS, entities, лимиты) — здесь
    только восстанавливается навигация. Если снимок устарел (черновик
    удалён, этап сменился, кирпичик больше не спрашивается), снимок
    снимается, а пользователь видит прежнюю подсказку меню: текст не
    пропадает и не уходит в чужой шаг.
    """
    user_id = _user_id(update)
    snapshot = _load_resume(user_id)
    chat = ctx.chat(update)
    target = (
        _resume_target(user_id, snapshot, chat.id if chat is not None else None)
        if snapshot is not None
        else None
    )
    if target is None:
        _clear_resume(user_id, ctx.data(context), ctx.chat(update).id)
        logger.info(
            "Resume после рестарта не выполнен: user_id=%s — обычная подсказка.",
            user_id,
        )
        from handlers.main_menu import outside_dialog_hint

        await outside_dialog_hint(update, context)
        return ConversationHandler.END
    details, ui = target
    ctx.data(context)["announcement_ui"] = ui
    logger.info(
        "Resume после рестарта: user_id=%s draft_id=%s фаза=%s.",
        user_id,
        details.draft_id,
        (snapshot or {}).get("mode"),
    )
    message = update.effective_message
    if message is not None and message.photo:
        state: int = await handle_photo(update, context)
    else:
        state = await handle_text(update, context)
    # Диалог продолжается: следующий ввод обслуживает сам конструктор.
    remember_conversation_state(update, context, state)
    return state


def _short(value: object, limit: int = 60) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _h(value: object) -> str:
    return escape_html(str(value if value is not None else ""))


def _place_lines(details: object) -> list[str]:
    """
    Строки «Место: …» для экранов анонса — только если площадка реально есть.

    У личного анонса площадки нет по схеме (`ck_announcement_drafts_origin_place`:
    `origin='personal'` требует `place_id IS NULL` и `place_display_name IS NULL`).
    Печатать в этом случае «Место: » с пустым значением нельзя: пользователь
    читает это как потерянные данные. Единый вариант для всех пяти экранов
    (шаг выбора шаблона, обзор, отправка, доставлено, отказ в неличном чате) —
    строки нет вовсе. Возвращается список 0/1 строк, чтобы вызывающий вставлял
    его через `extend` без ветвлений.
    """
    name = getattr(details, "place_display_name", None)
    if name is None or not str(name).strip():
        return []
    return [tr_safe("wizard.place_lines.mesto", name=_h(name))]


async def _answer(
    update: Update, text: str | None = None, *, alert: bool = False
) -> None:
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    try:
        await query.answer(text or "", show_alert=alert)
    except Exception as exc:  # noqa: BLE001 — ответ на callback не должен ломать экран
        logger.debug("Не удалось ответить на callback: %s", exc)


async def _render(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    user_data = ctx.data(context)
    user_id = _user_id(update)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    await screen.render_screen(
        context, user_data, ctx.chat(update).id, text, keyboard, user_id=user_id
    )
    # Poster 2.3 H1: ref экрана и режим ввода фиксируются одной записью.
    # Снимок нужен, чтобы после рестарта службы следующий текст продолжил шаг,
    # а не провалился в подсказку главного меню.
    _remember_resume(user_id, user_data, ctx.chat(update).id, _ui(context))


async def _reanchor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await screen.reanchor_screen(update, context)


async def _sync_live(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    text: str | None = None,
) -> None:
    """
    Обновляет живой верхний блок ПЕРЕД отрисовкой нижнего экрана (S3 п.2).

    Вызывается на всех нижних экранах конструктора (вопрос, extras, шаг
    фото, обзор): иначе при шаблоне без кирпичиков верхний блок не появился
    бы вовсе, а после последнего ответа остался бы устаревшим. Уже
    доставленный анонс не трогаем: там верхний блок — сама доставка.

    `text` — точный финальный рендер (U5): при завершении живой блок
    доводится ровно до того текста, который будет зафиксирован как
    доставленный, чтобы `publication_text` совпадал с сообщением в чате.
    """
    if details is None or details.publication_status is not None:
        return
    user_data = ctx.data(context)
    try:
        block = await wizard_live.sync_live_block(
            context.bot,
            ctx.chat(update).id,
            telegram_user_id=_user_id(update),
            draft_id=details.draft_id,
            revision=details.revision,
            known_ids=wizard_live.current_live_ids(details),
            text=text,
            has_photos=bool(details.photos),
            # S4: живой блок — медиаблок. Фото и прогресс частичной отправки
            # передаются явно: без них состав блока не совпал бы с payload
            # финальной отправки, и готовый пост появился бы вторым.
            photos=tuple(details.photos),
            user_data=user_data,
        )
    except Exception as exc:  # noqa: BLE001 — живой блок не должен ломать диалог
        logger.info("Живой блок #%s не обновлён: %s", details.draft_id, exc)
        return
    wizard_live.remember_live_state(user_data, draft_id=details.draft_id, block=block)
    await _keep_live_above_screen(update, context, block)


async def _keep_live_above_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    block,
) -> None:
    """
    Гарантирует порядок «анонс сверху, служебный экран снизу» (S3 §4).

    Telegram не умеет переставлять сообщения, поэтому исправляется только
    хронология: если живой блок оказался НИЖЕ служебного экрана (первый вход
    `pl_pick`/`tp_pick`, пересоздание живого блока после потери сообщения),
    наш собственный экран удаляется, и следующий `_render` создаёт его заново
    уже под анонсом.

    Удаляется только своё сообщение-экран из `screen_ref` (`user_data` или
    БД) и только в этом же чате: чужие сообщения и посты это не затрагивает.
    Отказ Telegram на удаление не прерывает сценарий (экран остаётся) — тогда
    порядок сохранить нельзя, и это единственное известное ограничение
    fallback'а; при первом входе сообщение свежее и удаляется штатно.
    """
    ids = tuple(getattr(block, "message_ids", ()) or ())
    if not ids:
        return
    user_data = ctx.data(context)
    ref = screen.screen_ref(user_data)
    if ref is None:
        ref = screen.hydrate_screen_ref(user_data, _user_id(update))
    if ref is None:
        return
    chat_id = ctx.chat(update).id
    if int(ref.get("chat_id") or 0) != int(chat_id):
        return
    # Живой блок выше экрана — порядок уже правильный, трогать нечего.
    if int(ref.get("message_id") or 0) > min(ids):
        return
    try:
        await context.bot.delete_message(chat_id, ref["message_id"])
    except Exception as exc:  # noqa: BLE001 — удаление best-effort
        logger.info(
            "Служебный экран %s не удалён (%s): живой блок останется ниже.",
            ref["message_id"],
            exc,
        )
        return
    user_data.pop("screen", None)
    screen.save_screen_ref(_user_id(update), user_data, chat_id)
    logger.info("Служебный экран пересоздаётся ниже живого блока анонса.")


def _end_foreign_conversations(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Явный вход в новый мастер завершает другие диалоги.

    Иначе старый девятишаговый ConversationHandler остался бы активным и
    перехватывал бы следующий текст. WIP при этом не удаляется: завершение
    диалога — не удаление черновика.
    """
    application = getattr(context, "application", None)
    if application is None:
        return
    for group in application.handlers.values():
        for handler in group:
            if not isinstance(handler, ConversationHandler):
                continue
            if getattr(handler, "name", None) == CONVERSATION_NAME:
                continue
            try:
                key = handler._get_key(update)  # noqa: SLF001 — завершение диалога
                if handler._conversations.get(key) is not None:  # noqa: SLF001
                    handler._update_state(ConversationHandler.END, key)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                logger.debug("Не удалось завершить посторонний диалог: %s", exc)


def _clear_other_sessions(user_id: int) -> None:
    from admin_state import clear_admin_session
    from brick_state import clear_brick_session
    from place_state import clear_place_session
    from template_state import clear_template_session

    clear_place_session(user_id)
    clear_admin_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)


def _session_details(user_id: int, draft_id: int):
    with SessionLocal() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=user_id,
            draft_id=draft_id,
        )


def _active_place_draft(user_id: int, place_id: int):
    """
    Незавершённый черновик этого пользователя по этой площадке (U4b).

    Повторное открытие ссылки на площадку должно ПРОДОЛЖАТЬ уже начатый
    анонс, а не создавать второй черновик той же площадки. Завершённые
    (отправленный анонс, `stage=completed`) и чужие черновики не
    считаются: ссылка на площадку после отправки открывает новый анонс,
    как и раньше. Из нескольких незавершённых берётся самый свежий.
    """
    with SessionLocal() as session:
        drafts = list_announcement_drafts(session, actor_telegram_user_id=user_id)
    for draft in drafts:
        if draft.place_id != place_id:
            continue
        if draft.stage == WizardStage.COMPLETED:
            continue
        if draft.publication_status == PUBLICATION_STATUS_SENT:
            continue
        return draft
    return None


def _resume_question_brick(user_id: int, details) -> int | None:
    """
    Вопрос, на котором автор остановился: последний заполненный.

    Так повторный вход по ссылке показывает уже введённое значение
    («Текущее значение: …») вместо пустого вопроса, а «Далее» ведёт к
    следующему незаполненному полю.
    """
    if details.stage != WizardStage.QUESTIONS:
        return None
    answered: int | None = None
    for field in _askable_fields(_load_fields(user_id, details.draft_id)):
        if field.status in {FIELD_STATUS_VALID, FIELD_STATUS_SKIPPED}:
            answered = field.brick_id
    return answered


async def resume_place_announcement(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    place_id: int,
) -> int | None:
    """
    Продолжает незавершённый черновик площадки, если он есть (U4b).

    Возвращает состояние диалога или None — тогда вызывающий открывает
    новый черновик обычным путём. Используется входом по ссылке
    `/start <token>`: та же площадка и уже начатый анонс → продолжение
    с сохранёнными данными.
    """
    user_id = _user_id(update)
    draft = _active_place_draft(user_id, place_id)
    if draft is None:
        return None
    await _answer(update)
    _end_foreign_conversations(update, context)
    _clear_other_sessions(user_id)
    await _reanchor(update, context)
    # R1, дефект №23: вход в конструктор — актуальный UI, без reply-клавиатур.
    await screen.drop_legacy_reply_keyboard(update, context)
    return await _open_draft(
        update,
        context,
        draft.draft_id,
        active_brick_id=_resume_question_brick(user_id, draft),
    )


def _guard_details(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
) -> tuple[dict[str, Any], Any] | None:
    user_id = _user_id(update)
    ui = _ui(context)
    if ui.get("draft_id") != draft_id or ui.get("revision") != revision:
        return None
    try:
        details = _session_details(user_id, draft_id)
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        return None
    if details.revision != revision:
        ui["revision"] = details.revision
        return None
    ui["revision"] = details.revision
    return ui, details


async def _stale(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str = STALE_TEXT,
) -> int:
    await _answer(update, text, alert=True)
    return await _resume_current(update, context, notice=text)


async def _expired(update: Update) -> int:
    await _answer(update, EXPIRED_TEXT, alert=True)
    return ConversationHandler.END


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    from handlers.main_menu import main_menu_keyboard

    return main_menu_keyboard()


def _edit_blocked(details) -> bool:
    """
    Идёт отправка: данные менять нельзя, пока она не завершилась.

    Доставленный анонс (`sent`) редактируем: следующая сборка заменит
    прошлую версию.
    """
    return details.publication_status in {
        PUBLICATION_STATUS_SENDING,
        PUBLICATION_STATUS_PARTIAL,
        PUBLICATION_STATUS_FAILED,
    }


def _draft_status_label(draft) -> str:
    status = getattr(draft, "publication_status", None)
    if status == PUBLICATION_STATUS_SENDING:
        return tr("wizard.draft_status_label.otpravka")
    if status == PUBLICATION_STATUS_PARTIAL:
        return tr("wizard.draft_status_label.chastichno_otpravlen")
    if status == PUBLICATION_STATUS_FAILED:
        return tr("wizard.draft_status_label.oshibka_otpravki")
    if status == PUBLICATION_STATUS_SENT:
        return tr("wizard.draft_status_label.gotovyy_anons_otpravlen")
    return STAGE_LABELS.get(
        str(getattr(draft, "stage", "")), tr("wizard.draft_status_label.chernovik")
    )


def _draft_place_name(draft) -> str:
    """
    Понятное имя места анонса для «Мои анонсы» (без HTML-экранирования).

    У анонса по площадке показываем подтверждённое место, у анонса по
    личному шаблону площадки нет — имя шаблона из снимка. Если имени нет
    вовсе, остаётся нейтральная подпись источника. Экранирует вызывающий
    слой: текст экрана — HTML, подпись кнопки — обычный текст.
    """
    snapshot = getattr(draft, "template_snapshot", None)
    name = getattr(draft, "place_display_name", None)
    if not name and snapshot is not None:
        name = getattr(snapshot, "source_name", None)
    if not name:
        origin = getattr(draft, "origin", None)
        name = (
            tr("wizard.draft_display_name.anons_lichnomu_shablonu")
            if origin == "personal"
            else tr("wizard.draft_display_name.anons")
        )
    return str(name)


def _draft_display_name(draft) -> str:
    """Имя места анонса для экрана подтверждения удаления (P3)."""
    return _draft_place_name(draft)


def _draft_title(draft) -> str:
    """
    Заголовок мероприятия анонса (кирпичик `[название]`), без HTML.

    2.5.4 §6: в списке «Мои анонсы» первым идёт именно название события, а
    не место. Значение берётся по реальному brick_id снимка (в рабочей базе
    он не обязан совпадать с каноническим ID каталога); непустой текст
    показывается как есть, пустое/отсутствующее значение — «Без названия».
    """
    snapshot = getattr(draft, "template_snapshot", None)
    if snapshot is not None:
        brick_id = snapshot_brick_ids_by_code(snapshot).get(TITLE_BRICK_CODE)
        if brick_id is not None:
            value = draft.value_for(brick_id)
            payload = getattr(value, "payload", None)
            if isinstance(payload, Mapping):
                raw = payload.get("text")
                if isinstance(raw, str) and raw.strip():
                    return " ".join(raw.split())
    return tr("wizard.draft_title.bez_nazvaniya")


def _draft_short_status(draft) -> str:
    """
    Состояние записи для списка «Мои анонсы»: только «готов» или «черновик».

    2.5.4 §6: технические состояния (`sending`/`partial`/`failed`) — это
    всегда незавершённый черновик; пользователю они не показываются.
    """
    if getattr(draft, "publication_status", None) == PUBLICATION_STATUS_SENT:
        return tr("wizard.draft_status_label.gotov")
    return tr("wizard.draft_status_label.chernovik")


def _entry_label(draft) -> str:
    """
    Подпись строки списка «Мои анонсы»: `Заголовок — Место (состояние)`.

    2.5.4 §6: номер и эмодзи убраны (номер остаётся в тексте экрана), на
    первом месте — название мероприятия, затем место и простое состояние.
    При обрезке первым сокращается заголовок: место и состояние терять
    нельзя. Подпись кнопки — обычный текст, поэтому HTML не экранируется.
    """
    place = _short(_draft_place_name(draft), ENTRY_PLACE_LIMIT)
    status = _draft_short_status(draft)
    tail = f" — {place} ({status})"
    budget = max(ENTRY_LABEL_LIMIT - len(tail), ENTRY_TITLE_MIN)
    return f"{_short(_draft_title(draft), budget)}{tail}"


def _home_content(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup, int]:
    try:
        with SessionLocal() as session:
            drafts = list_announcement_drafts(session, actor_telegram_user_id=user_id)
    except (AnnouncementActorError, AnnouncementError):
        drafts = []
    # Poster 2.5 §14: единая пагинация — 5 записей на страницу, 0–5 без
    # навигации. Номер страницы приводится к допустимому при рендере, поэтому
    # после удаления последней записи пустая страница не показывается.
    page_slice = pagination.page_items(drafts, page)
    page = page_slice.page
    shown = page_slice.items

    lines = [
        tr("buttons.main.announcements"),
        "",
        tr("wizard.home.nezavershennye_anonsy_mozhno"),
    ]
    if shown:
        lines.extend(["", tr("wizard.home.vashi_anonsy")])
        for number, draft in enumerate(shown, start=1):
            # 2.5.4 §6: номер остаётся только в тексте экрана (кнопки — без
            # нумерации), формат названия — тот же, что у кнопки.
            lines.append(f"{number}. {_h(_entry_label(draft))}")
    elif drafts:
        lines.extend(["", tr("wizard.home.na_etoy_stranice_anonsov")])
    else:
        lines.extend(
            [
                "",
                tr("wizard.home.anonsov_poka_net_nachnite"),
            ]
        )

    # P3: кнопки создания анонса на экране списка НЕТ — создание живёт в
    # главном меню. У каждой записи (черновик и готовый) ровно одна строка:
    # открытие + «🗑» удаление.
    buttons: list[list[InlineKeyboardButton]] = keyboard_layouts.list_rows(
        (
            keyboard_layouts.button(_entry_label(draft), f"aw_open:{draft.draft_id}"),
            keyboard_layouts.button(
                tr("buttons.wizard.udalit_anons"), f"aw_del:{draft.draft_id}"
            ),
        )
        for draft in shown
    )
    nav = pagination.nav_keyboard(
        page, page_slice.total_pages, "aw_home_page", as_offset=False
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(buttons), page


async def _render_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    ui["draft_id"] = None
    ui["revision"] = None
    _clear_active(ui)
    _nav_reset(ui)
    _nav_enter(ui, NAV_HOME)
    page = int(ui.get("home_page") or 0)
    text, keyboard, page = _home_content(_user_id(update), page)
    ui["home_page"] = page
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, keyboard)
    return AW_PLACE


def _delete_confirm_content(draft) -> tuple[str, InlineKeyboardMarkup]:
    """
    Отдельный экран подтверждения удаления записи списка (P3).

    Подтверждение нельзя ставить рядом с «Открыть»: удаление необратимо.
    Поэтому у экрана собственная клавиатура — «Удалить» и «Отмена», а
    «Отмена» возвращает в список без изменений.
    """
    text = tr(
        "wizard.home.podtverdite_udalenie_anonsa",
        name=_draft_display_name(draft),
        status=_draft_status_label(draft),
    )
    keyboard = InlineKeyboardMarkup(
        keyboard_layouts.pair(
            keyboard_layouts.button(
                tr("buttons.wizard.udalit_anons"), f"aw_del_yes:{draft.draft_id}"
            ),
            keyboard_layouts.button(
                tr("buttons.wizard.otmena_udaleniya"), f"aw_del_no:{draft.draft_id}"
            ),
        )
    )
    return text, keyboard


async def _render_delete_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
) -> int:
    """
    Показывает подтверждение удаления (P3, «🗑» в «Мои анонсы»).

    Ничего не удаляет: только экран. Чужой/удалённый id — понятный ответ и
    возврат к списку, без внутренней ошибки.
    """
    user_id = _user_id(update)
    try:
        details = _session_details(user_id, draft_id)
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        gone = tr("alerts.wizard.anons_bolshe_ne_hranitsya")
        await _answer(update, gone, alert=True)
        return await _render_my_announcements(update, context, notice=gone)
    ui = _ui(context)
    ui["draft_id"] = None
    ui["revision"] = None
    _clear_active(ui)
    _nav_enter(ui, NAV_HOME)
    text, keyboard = _delete_confirm_content(details)
    await _render(update, context, text, keyboard)
    return AW_PLACE


def _place_hub_content() -> tuple[str, InlineKeyboardMarkup]:
    text = tr("wizard.place_hub.shag_1_vyberite_mesto")
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.places.search"), callback_data="aw_place_search"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.places.all"), callback_data="aw_place_all:0"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.nav.back"), callback_data="aw_place_back"
                )
            ],
        ]
    )
    return text, keyboard


async def _render_place_hub(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    ui["place"]["phase"] = "hub"
    ui["place"]["page"] = 0
    text, keyboard = _place_hub_content()
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, keyboard)
    return AW_PLACE


def _clamped_page(page: int, total: int, size: int) -> int:
    if total <= 0:
        return 0
    last = (total - 1) // size
    return max(0, min(page, last))


def _place_buttons(
    places: list[tuple[int, str]], *, prefix: str, page: int, total: int
) -> list[list[InlineKeyboardButton]]:
    # U3a №13: строки списка — общий шаблон компоновки.
    rows = keyboard_layouts.list_rows(
        (
            keyboard_layouts.button(
                _short(f"📍 {name}", 60), f"aw_place_open:{place_id}"
            ),
            None,
        )
        for place_id, name in places
    )
    # Poster 2.5 §14: строка `◀️ | 2/4 | ▶️` из единого механизма; callback
    # хранит НОМЕР страницы — так его читает обработчик `aw_place_all`.
    nav = pagination.nav_keyboard(
        page,
        pagination.page_count(total, PLACE_PAGE_SIZE),
        prefix,
        page_size=PLACE_PAGE_SIZE,
        as_offset=False,
    )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="aw_place_back")]
    )
    return rows


async def _render_all_places(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    ui["place"]["phase"] = "all"
    ui["place"]["page"] = max(0, page)
    with SessionLocal() as session:
        places, total = list_places(
            session,
            offset=ui["place"]["page"] * PLACE_PAGE_SIZE,
            limit=PLACE_PAGE_SIZE,
        )
    page = _clamped_page(ui["place"]["page"], total, PLACE_PAGE_SIZE)
    if page != ui["place"]["page"]:
        ui["place"]["page"] = page
        with SessionLocal() as session:
            places, total = list_places(
                session,
                offset=page * PLACE_PAGE_SIZE,
                limit=PLACE_PAGE_SIZE,
            )
    lines = [
        tr("buttons.places.all"),
        "",
        (
            tr("wizard.render_all_places.vsego_stranica", total=total, value=page + 1)
            if total
            else tr("admin.places.v_spravochnike_poka_net")
        ),
    ]
    rows = _place_buttons(
        [
            (item.place_id, place_title(item.canonical_name, item.city))
            for item in places
        ],
        prefix="aw_place_all",
        page=page,
        total=total,
    )
    if not places:
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    tr("buttons.places.search"), callback_data="aw_place_search"
                )
            ],
        )
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_PLACE


async def _render_search_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    state = ui["place"]
    state["phase"] = "results"
    query = str(state.get("query") or "")
    with SessionLocal() as session:
        results = search_places(session, query)
    total = len(results)
    page = _clamped_page(max(0, page), total, PLACE_PAGE_SIZE)
    state["page"] = page
    shown = results[page * PLACE_PAGE_SIZE : (page + 1) * PLACE_PAGE_SIZE]
    lines = [
        tr("wizard.render_search_results.rezultaty_poiska"),
        "",
        tr_safe("wizard.render_search_results.zapros", query=_h(query)),
        tr("wizard.render_search_results.naydeno_mest", total=total),
    ]
    for result in shown:
        title = _h(place_title(result.canonical_name, result.city))
        if result.matched_name != result.canonical_name:
            lines.append(
                tr_safe(
                    "wizard.render_search_results.sovpadenie",
                    title=title,
                    matched_name=_h(result.matched_name),
                )
            )
        else:
            lines.append(f"• {title}")
    if not results:
        lines.extend(
            [
                "",
                tr("wizard.render_search_results.nichego_naydeno_izmenite_zapros"),
            ]
        )
    rows = [
        [
            InlineKeyboardButton(
                _short(f"📍 {place_title(result.canonical_name, result.city)}", 60),
                callback_data=f"aw_place_open:{result.place_id}",
            )
        ]
        for result in shown
    ]
    nav = pagination.nav_keyboard(
        page,
        pagination.page_count(total, PLACE_PAGE_SIZE),
        "aw_place_results",
        page_size=PLACE_PAGE_SIZE,
        as_offset=False,
    )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.wizard.izmenit_zapros"), callback_data="aw_place_search"
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(tr("buttons.places.all"), callback_data="aw_place_all:0")]
    )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="aw_place_back")]
    )
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_PLACE


async def _render_search_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    ui["place"]["phase"] = "search_input"
    text = tr("wizard.render_search_prompt.poisk_mesta_otpravte_nazvanie")
    body = f"{notice}\n\n{text}" if notice else text
    await _render(
        update,
        context,
        body,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        tr("buttons.nav.back"), callback_data="aw_place_back"
                    )
                ]
            ]
        ),
    )
    return AW_PLACE


def _place_card_text(details, selected_name_id: int | None) -> str:
    lines = [
        tr("wizard.place_card.proverte_mesto"),
        "",
        tr_safe(
            "wizard.place_card.nazvanie", canonical_name=_h(details.canonical_name)
        ),
        tr_safe(
            "wizard.place_card.gorod", city=_h(details.city) if details.city else "—"
        ),
        tr_safe(
            "wizard.place_card.adres",
            address=(
                _h(details.address)
                if details.address
                else tr("wizard.place_card.ne_ukazan")
            ),
        ),
        tr_safe(
            "wizard.place_card.ssylka",
            url=(
                _h(details.url)
                if details.url
                else tr("wizard.preview_summary_lines.ne_ukazana")
            ),
        ),
        "",
        tr("wizard.place_card.nazvaniya"),
    ]
    for name in details.names:
        marker = "✅" if name.id == selected_name_id else "•"
        suffix = (
            tr("wizard.place_card.kanonicheskoe")
            if name.is_canonical
            else tr("wizard.place_card.alternativnoe")
        )
        lines.append(f"{marker} {_h(name.name)}{suffix}")
    lines.extend(
        [
            "",
            tr("wizard.place_card.izvestnye_adres_ssylka_budut"),
        ]
    )
    return "\n".join(lines)


async def _render_place_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    state = ui["place"]
    place_id = state.get("place_id")
    name_id = state.get("name_id")
    if not place_id:
        return await _render_place_hub(update, context, notice=notice)
    with SessionLocal() as session:
        details = get_place_details(session, int(place_id))
    if details is None:
        state["phase"] = "hub"
        return await _render_place_hub(
            update, context, notice=tr("wizard.render_place_card.mesto_bolshe_dostupno")
        )
    if name_id is None:
        name_id = details.canonical_name_id
        state["name_id"] = name_id
    known_name_ids = {name.id for name in details.names}
    if int(name_id) not in known_name_ids:
        name_id = details.canonical_name_id
        state["name_id"] = name_id
    state["phase"] = "confirm"
    text = _place_card_text(details, int(name_id))
    body = f"{notice}\n\n{text}" if notice else text
    rows = [
        [
            InlineKeyboardButton(
                tr("buttons.admin.podtverdit"),
                callback_data=(f"aw_place_confirm:{details.place_id}:{int(name_id)}"),
            )
        ]
    ]
    if len(details.names) > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.vybrat_nazvanie"),
                    callback_data=f"aw_place_names:{details.place_id}:0",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="aw_place_back")]
    )
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_PLACE


async def _render_place_names(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    state = ui["place"]
    place_id = state.get("place_id")
    if not place_id:
        return await _render_place_hub(update, context, notice=notice)
    with SessionLocal() as session:
        details = get_place_details(session, int(place_id))
    if details is None:
        return await _render_place_hub(
            update, context, notice=tr("wizard.render_place_card.mesto_bolshe_dostupno")
        )
    total = len(details.names)
    page = _clamped_page(max(0, page), total, PLACE_PAGE_SIZE)
    state["phase"] = "names"
    selected = state.get("name_id") or details.canonical_name_id
    names = details.names[page * PLACE_PAGE_SIZE : (page + 1) * PLACE_PAGE_SIZE]
    lines = [
        tr("wizard.render_place_names.nazvanie_mesta"),
        "",
        tr_safe(
            "wizard.render_place_names.mesto", canonical_name=_h(details.canonical_name)
        ),
        "",
        tr("wizard.render_place_names.vyberite_kanonicheskoe"),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for name in names:
        marker = "✅" if name.id == selected else "•"
        suffix = "" if name.is_canonical else tr("wizard.render_place_names.alt")
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"{marker} {name.name}{suffix}", 60),
                    callback_data=(
                        f"aw_place_name_pick:{details.place_id}:{name.id}:{page}"
                    ),
                )
            ]
        )
    # Poster 2.5 §14: строка `◀️ | 2/4 | ▶️` из единого механизма; callback
    # хранит НОМЕР страницы — так его читает обработчик `aw_place_names`.
    nav = pagination.nav_keyboard(
        page,
        pagination.page_count(total, PLACE_PAGE_SIZE),
        f"aw_place_names:{details.place_id}",
        page_size=PLACE_PAGE_SIZE,
        as_offset=False,
    )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="aw_place_back")]
    )
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_PLACE


def _place_return_phase(ui: dict[str, Any]) -> str:
    phase = str(ui["place"].get("return_phase") or "hub")
    if phase not in {"hub", "all", "results"}:
        return "hub"
    return phase


async def _place_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ui = _ui(context)
    state = ui["place"]
    phase = state.get("phase")
    if phase == "names":
        return await _render_place_card(update, context)
    if phase == "confirm":
        if ui.get("draft_id"):
            return await _render_home(update, context)
        target = _place_return_phase(ui)
        if target == "results":
            return await _render_search_results(
                update, context, int(state.get("return_page") or 0)
            )
        if target == "all":
            return await _render_all_places(
                update, context, int(state.get("return_page") or 0)
            )
        return await _render_place_hub(update, context)
    if phase in {"results", "all", "search_input"}:
        return await _render_place_hub(update, context)
    return await _render_home(update, context)


async def _open_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    *,
    active_brick_id: int | None = None,
    notice: str | None = None,
) -> int:
    user_id = _user_id(update)
    try:
        details = _session_details(user_id, draft_id)
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        # P3: запись могла быть удалена (вручную, вытеснением или очисткой),
        # а кнопка «открыть» осталась в чате. Пользователь видит понятный
        # ответ, а не внутреннюю ошибку; экран — актуальный список.
        ui = _fresh_ui()
        ctx.data(context)["announcement_ui"] = ui
        gone = tr("alerts.wizard.anons_bolshe_ne_hranitsya")
        await _answer(update, gone, alert=True)
        return await _render_home(update, context, notice=gone)

    ui = _fresh_ui()
    _set_details(ui, details)
    # U4b: повторное открытие ссылки возвращает к вопросу с последним
    # сохранённым ответом — человек видит свои данные, а не пустой вопрос.
    if active_brick_id is not None:
        ui["active_brick_id"] = active_brick_id
    ctx.data(context)["announcement_ui"] = ui
    if details.publication_sent and not _edit_blocked(details):
        # §29–30: готовый анонс из «Мои анонсы» открывается НОВЫМ цельным
        # экземпляром. Прежний остаётся в чате архивным снимком без кнопок
        # (`register_instance` в цепочке доставки), данные не пересобираются
        # в новый логический анонс.
        return await _republish_delivered(update, context, details, notice=notice)
    return await _render_draft_stage(update, context, details, notice=notice)


async def _republish_delivered(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """
    Переиздаёт готовый анонс новым экземпляром (spec §29–30).

    Повторное открытие из «Мои анонсы» — не редактор и не повторное
    использование прежнего сообщения: автор всегда получает свежий цельный
    анонс с управлением. Идентичный текст тоже уходит заново — именно это
    и просят владельцы, поэтому обход проверки «ничего не изменилось» здесь
    намеренный (`_finish_and_deliver` такой вызов отверг бы).

    Прежний экземпляр архивируется в той же транзакции регистрации нового
    (§30 п.3), его кнопки снимаются, панель медиагруппы убирается — всё это
    уже делает `_ensure_delivered_instance`. Если отправка не удалась,
    управление прежнего экземпляра НЕ трогается (§30, последний абзац):
    регистрация происходит только после подтверждённой доставки.
    """
    user_id = _user_id(update)
    if not _is_private_chat(update):
        await _answer(update, PRIVATE_CHAT_ONLY, alert=True)
        return await _render_private_chat_refusal(update, context, details)
    try:
        with SessionLocal() as session:
            publication = begin_announcement_publication(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                expected_revision=details.revision,
                today=app_today(),
            )
    except (
        AnnouncementError,
        TemplateError,
        TemplateCompileError,
        BrickError,
    ) as exc:
        # Причину видно на экране проблемы: короткий ответ не добавляем,
        # чтобы не вводить нового текста в каталог.
        return await _render_preview_error(update, context, details, str(exc))
    _set_details(_ui(context), publication.draft)
    # `previous_ids` прошлой версии вторым аргументом не передаются: по
    # §30–31 прежний экземпляр остаётся в чате архивным снимком.
    return await _run_announcement_publication(
        update,
        context,
        publication,
        stale_live_ids=(),
    )


async def _render_draft_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """Экран сохранённого черновика по его этапу (общий для всех входов)."""
    ui = _ui(context)
    stage = details.stage
    if stage == WizardStage.PLACE:
        ui["place"].update(
            {
                "phase": "confirm",
                "place_id": details.place_id,
                "name_id": details.place_name_id,
                "return_phase": "hub",
            }
        )
        return await _render_place_card(update, context, notice=notice)
    if stage == WizardStage.TEMPLATE:
        return await _render_template_screen(update, context, details, notice=notice)
    if stage == WizardStage.QUESTIONS:
        return await _enter_questions(update, context, details, notice=notice)
    if stage == WizardStage.EXTRAS:
        return await _render_extras(update, context, details, notice=notice)
    if details.publication_status not in {None, PUBLICATION_STATUS_SENT}:
        return await _render_publish_status(update, context, details, notice=notice)
    return await _render_preview(update, context, details, notice=notice)


async def _confirm_place(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    place_id: int,
    name_id: int,
) -> int:
    ui = _ui(context)
    state = ui["place"]
    if state.get("phase") != "confirm":
        return await _stale(update, context)
    if int(state.get("place_id") or 0) != place_id:
        return await _stale(update, context)
    if int(state.get("name_id") or 0) != name_id:
        return await _stale(update, context)

    user_id = _user_id(update)
    try:
        with SessionLocal() as session:
            if ui.get("draft_id"):
                details = get_announcement_draft(
                    session,
                    actor_telegram_user_id=user_id,
                    draft_id=int(ui["draft_id"]),
                )
                if details.place_id != place_id:
                    return await _stale(update, context)
                if details.stage == WizardStage.PLACE:
                    details = confirm_announcement_place(
                        session,
                        actor_telegram_user_id=user_id,
                        draft_id=details.draft_id,
                        expected_revision=details.revision,
                    )
            else:
                key = state.get("idempotency_key") or uuid.uuid4().hex
                state["idempotency_key"] = key
                details = start_announcement_wizard(
                    session,
                    actor_telegram_user_id=user_id,
                    place_id=place_id,
                    place_name_id=name_id,
                    idempotency_key=key,
                )
                details = confirm_announcement_place(
                    session,
                    actor_telegram_user_id=user_id,
                    draft_id=details.draft_id,
                    expected_revision=details.revision,
                )
    except AnnouncementError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    except AnnouncementActorError:
        await _answer(
            update,
            tr("alerts.wizard.profil_zaregistrirovan_otpravte"),
            alert=True,
        )
        return ConversationHandler.END

    _set_details(ui, details)
    state["phase"] = "confirmed"
    await _answer(update, tr("alerts.wizard.mesto_podtverzhdeno"))
    return await _render_template_screen(update, context, details)


def _template_description(details) -> str:
    version = details.current_version
    count = len(version.bindings)
    kind = (
        tr("wizard.template_description.rasshirennyy")
        if version.extended_content is not None
        else tr("wizard.template_description.obychnyy")
    )
    return tr("wizard.template_description.tekst_kirpichikov", kind=kind, count=count)


def _selected_template_text(details) -> str:
    snapshot = details.template_snapshot
    assert snapshot is not None
    kind = (
        tr("wizard.selected_template.standartnyy")
        if snapshot.source_kind == "standard"
        else tr("wizard.module.lichnyy")
    )
    name = snapshot.source_name or (
        tr("wizard.selected_template.standartnyy_shablon_mesta")
        if snapshot.source_kind == "standard"
        else tr("wizard.selected_template.lichnyy_shablon")
    )
    texts = [
        label
        for label, key in (
            (tr("wizard.template_description.obychnyy"), TEMPLATE_CONTENT),
            (tr("wizard.template_description.rasshirennyy"), TEMPLATE_EXTENDED_CONTENT),
        )
        if snapshot.texts.get(key, "").strip()
    ]
    part = ", ".join(texts) if texts else tr("wizard.selected_template.bez_teksta")
    return tr_safe(
        "wizard.selected_template.vybrannyy_shablon_tip_versiya",
        name=_h(name),
        kind=kind,
        source_template_version=snapshot.source_template_version,
        part=part,
        count_brick_ids=len(snapshot.brick_ids),
    )


async def _render_template_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    _nav_enter(ui, NAV_TEMPLATE)
    if details.template_snapshot is not None:
        text = _selected_template_text(details)
        body = f"{notice}\n\n{text}" if notice else text
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        tr("buttons.wizard.prodolzhit"),
                        callback_data=(
                            f"aw_cont:{details.draft_id}:{details.revision}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        tr("buttons.wizard.k_spisku_chernovikov"),
                        callback_data="aw_home",
                    )
                ],
                [
                    InlineKeyboardButton(
                        tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
                    )
                ],
            ]
        )
        _set_template_phase(ui)
        await _render(update, context, body, keyboard)
        return AW_TEMPLATE

    _set_template_phase(ui)
    with SessionLocal() as session:
        try:
            standard = get_standard_template(
                session,
                _user_id(update),
                details.place_id,
            )
        except TemplateError:
            standard = None
        personal = list_personal_templates(session, _user_id(update))

    page = int(ui.get("template_page") or 0)
    page = _clamped_page(page, len(personal), TEMPLATE_PAGE_SIZE)
    ui["template_page"] = page
    shown = personal[page * TEMPLATE_PAGE_SIZE : (page + 1) * TEMPLATE_PAGE_SIZE]

    lines = [
        tr("wizard.render_template.shag_2_vyberite_shablon"),
        "",
        *_place_lines(details),
        "",
        tr("wizard.render_template.standartnyy_shablon_mesta"),
        (
            f"• {_h(standard.display_name)} — {_h(_template_description(standard))}"
            if standard is not None
            else tr("wizard.render_template.ne_nastroen")
        ),
        "",
        tr("wizard.render_template.lichnye_shablony"),
    ]
    if shown:
        for number, template in enumerate(shown, start=page * TEMPLATE_PAGE_SIZE + 1):
            lines.append(
                f"{number}. {_h(template.display_name)} — "
                f"{_h(_template_description(template))}"
            )
    else:
        lines.append(tr("wizard.render_template.net_lichnyh_shablonov"))
    lines.extend(
        [
            "",
            tr("wizard.render_template.shablon_fiksiruetsya_nezavisimym"),
        ]
    )

    rows: list[list[InlineKeyboardButton]] = []
    if standard is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"✅ {standard.display_name}", 60),
                    callback_data=(
                        f"aw_template:{details.draft_id}:"
                        f"{details.revision}:{standard.template_id}"
                    ),
                )
            ]
        )
    for template in shown:
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"📄 {template.display_name}", 60),
                    callback_data=(
                        f"aw_template:{details.draft_id}:"
                        f"{details.revision}:{template.template_id}"
                    ),
                )
            ]
        )
    # Poster 2.5 §14: строка `◀️ | 2/4 | ▶️` из единого механизма; callback
    # хранит НОМЕР страницы — так его читает обработчик `aw_tplpage`.
    nav = pagination.nav_keyboard(
        page,
        pagination.page_count(len(personal), TEMPLATE_PAGE_SIZE),
        f"aw_tplpage:{details.draft_id}:{details.revision}",
        page_size=TEMPLATE_PAGE_SIZE,
        as_offset=False,
    )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.wizard.k_spisku_chernovikov"), callback_data="aw_home"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
            )
        ]
    )
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_TEMPLATE


def _set_template_phase(ui: dict[str, Any]) -> None:
    ui["place"]["phase"] = "template"


async def _select_template(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    template_id: int,
) -> int:
    guarded = _guard_details(update, context, draft_id, revision)
    if guarded is None:
        return await _stale(update, context)
    ui, details = guarded
    if details.template_snapshot is not None:
        if details.template_id == template_id:
            return await _render_template_screen(update, context, details)
        return await _stale(update, context)

    user_id = _user_id(update)
    try:
        with SessionLocal() as session:
            standard = None
            try:
                standard = get_standard_template(session, user_id, details.place_id)
            except TemplateError:
                pass
            personal = list_personal_templates(session, user_id)
            allowed_ids = {template.template_id for template in personal}
            if standard is not None:
                allowed_ids.add(standard.template_id)
            if template_id not in allowed_ids:
                return await _stale(
                    update,
                    context,
                    tr("wizard.select_template.etot_shablon_nedostupen_vyberite"),
                )
            updated = choose_announcement_template(
                session,
                actor_telegram_user_id=user_id,
                draft_id=draft_id,
                template_id=template_id,
                expected_revision=details.revision,
            )
    except AnnouncementError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    except TemplateError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    _set_details(ui, updated)
    await _answer(update, tr("alerts.wizard.shablon_vybran_snimok_sohranen"))
    return await _enter_questions(update, context, updated)


def _askable_fields(
    fields: tuple[AnnouncementFieldState, ...],
) -> list[AnnouncementFieldState]:
    return [field for field in fields if field.status != FIELD_STATUS_KNOWN]


# Poster 2.5.3 §5: коды системных кирпичиков площадки. Их значение приходит
# из карточки площадки и в редакторе текстовых полей не спрашивается: автор
# не должен «переписывать» место/адрес/ссылку/город в анонсе — это данные
# справочника. Пропуск по одному `FIELD_STATUS_KNOWN` недостаточен: пустое
# значение площадки получает статус MISSING и иначе попало бы в список.
PLACE_FIELD_CODES = frozenset(
    definition.code
    for definition in SYSTEM_BRICKS
    if definition.source == BRICK_SOURCE_PLACE
)


def _is_text_field(field: AnnouncementFieldState) -> bool:
    """
    Поле доступно в списке «📝 Текстовые поля» доставленного анонса.

    Исключаются поля площадки (место, адрес, ссылка, город): их значения
    берутся из справочника (§5). Личный шаблон площадки не имеет, поэтому
    его поля остаются доступными как есть — фильтр по коду ничего не
    отбрасывает.
    """
    return field.code not in PLACE_FIELD_CODES


def _delivered_text_question_allowed(details, field: AnnouncementFieldState) -> bool:
    """
    Вопрос по этому полю открывается из текущего режима правки.

    Доставленный анонс (2.5.3 §5) редактируется только через список
    «📝 Текстовые поля», поэтому его вопрос открывается ровно для текстового
    поля — как в легитимном `_open_field_question`. Подделанный callback
    вопроса (`aw_qgoto`/`aw_qback`/`aw_qskip`) с brick_id адреса/места/
    ссылки/города получает отказ, и значение справочника не попадает в
    анонс. Не доставленный черновик идёт прежним путём конструктора, где
    поле площадки без значения из карточки — обычный вопрос.
    """
    if not details.publication_sent:
        return True
    return _is_text_field(field)


def _text_fields(
    fields: tuple[AnnouncementFieldState, ...],
) -> list[AnnouncementFieldState]:
    """Поля редактора доставленного анонса: без переданных площадкой."""
    return [field for field in _askable_fields(fields) if _is_text_field(field)]


def _first_missing_index(fields: list[AnnouncementFieldState]) -> int:
    for index, field in enumerate(fields):
        if field.status == FIELD_STATUS_MISSING:
            return index
    return -1


def _load_fields(user_id: int, draft_id: int):
    with SessionLocal() as session:
        return build_announcement_fields(
            session,
            actor_telegram_user_id=user_id,
            draft_id=draft_id,
            today=app_today(),
        )


def _question_humor_line(
    update: Update,
    details,
    field: AnnouncementFieldState,
    *,
    error: str | None,
) -> str | None:
    """
    Редкая шутливая вариация для экрана вопроса — или `None`.

    Экран с ошибкой получает запрещённый вид (`error`), поэтому шуток в
    диагностике нет ни при каком ключе. Остальные ограничения (лимиты,
    права, потеря данных) живут на своих экранах и сюда не обращаются.
    """
    screen_kind = ui_humor.SCREEN_ERROR if error else ui_humor.SCREEN_QUESTION
    key = (
        f"{_user_id(update)}:{details.draft_id}:" f"{details.revision}:{field.brick_id}"
    )
    return ui_humor.pick_variant(key, screen_kind=screen_kind)


async def _render_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    fields: list[AnnouncementFieldState],
    index: int,
    *,
    editing: bool = False,
    error: str | None = None,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    if not fields:
        return await _enter_extras(update, context, details, notice=notice)
    index = max(0, min(index, len(fields) - 1))
    field = fields[index]
    ui["question_index"] = index
    ui["active_brick_id"] = field.brick_id
    ui["active_source"] = "question"
    ui["editing"] = editing
    _nav_enter(
        ui,
        NAV_QUESTION,
        index=index,
        brick_id=field.brick_id,
    )
    # Poster 2.0 S3 п.2: сначала настоящий анонс сверху, затем служебный
    # экран. Порядок в чате фиксирован хронологией сообщений, поэтому
    # верхний блок синхронизируется ДО отрисовки нижнего вопроса.
    await _sync_live(update, context, details)
    text = wizard_questions.question_text(
        field, index, len(fields), editing=editing, error=error, notice=notice
    )
    rows: list[list[InlineKeyboardButton]] = []

    if field.value_type == BRICK_TYPE_CHOICE:
        options = _choice_options(field)
        selected = set(_selected_choices(field))
        for option_index, option in enumerate(options):
            marker = "✅ " if option in selected else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        _short(f"{marker}{option}", 60),
                        callback_data=(
                            f"aw_choice:{details.draft_id}:{details.revision}:"
                            f"{field.brick_id}:{option_index}"
                        ),
                    )
                ]
            )
        if field.rules.get("multiple") and options:
            rows.append(
                [
                    InlineKeyboardButton(
                        tr("buttons.wizard.gotovo"),
                        callback_data=(
                            f"aw_choice_done:{details.draft_id}:"
                            f"{details.revision}:{field.brick_id}"
                        ),
                    )
                ]
            )
    elif field.value_type == BRICK_TYPE_DATE:
        # Poster 2.0 S3 п.4: календарь показывается сразу на вопросе даты —
        # отдельной кнопки «📅 Календарь» больше нет. Callback'и прежние,
        # поэтому ручная навигация по неделям/месяцам не изменилась.
        rows.extend(
            wizard_questions.calendar_shortcut_rows(
                field, f"{details.draft_id}:{details.revision}:{field.brick_id}"
            )
        )
    elif field.value_type == BRICK_TYPE_TIME:
        # S3 п.5: быстрые кнопки времени рядом с ручным вводом.
        rows.extend(
            wizard_questions.time_preset_rows(
                f"{details.draft_id}:{details.revision}:{field.brick_id}"
            )
        )

    # Poster 2.4 (P2, требование VI): на вопросе остаются только «◀️ Назад» и
    # «⏭ Пропустить» (плюс спецкнопки дат/времени/выбора). «➡️ Далее» и
    # «✏️ Изменить ответ» убраны: сохранение значения само ведёт дальше, а
    # повторный ввод на том же экране заменяет значение. «Готово» у
    # множественного выбора — это выбор вариантов (`aw_choice_done`), а не
    # навигация, поэтому остаётся.
    skip_button = InlineKeyboardButton(
        tr("buttons.wizard.skip"),
        callback_data=(
            f"aw_qskip:{details.draft_id}:{details.revision}:{field.brick_id}"
        ),
    )
    back_button = InlineKeyboardButton(
        tr("buttons.nav.back"),
        callback_data=(
            f"aw_qback:{details.draft_id}:{details.revision}:{field.brick_id}"
        ),
    )
    # U3a №13: пара «Назад | Пропустить» — общий шаблон компоновки, одинаковый
    # для всех типов поля.
    rows += keyboard_layouts.pair(back_button, skip_button)
    keyboard = InlineKeyboardMarkup(rows)
    # S5 §5.7: шутка подставляется в текст ТОЛЬКО после того, как собраны
    # `callback_data` и клавиатура, — повлиять на действия она не может.
    # Ключ стабилен между перерисовками одного экрана (только ID черновика,
    # ревизии и кирпичика), поэтому текст не «прыгает».
    humor_line = _question_humor_line(update, details, field, error=error)
    if humor_line:
        text = f"{text}\n\n{_h(humor_line)}"
    await _render(update, context, text, keyboard)
    return AW_QUESTIONS


async def _enter_questions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    fields = _askable_fields(_load_fields(_user_id(update), details.draft_id))
    if not fields:
        return await _enter_extras(update, context, details, notice=notice)
    active = ui.get("active_brick_id")
    index = -1
    for position, field in enumerate(fields):
        if field.brick_id == active:
            index = position
            break
    if index < 0:
        index = _first_missing_index(fields)
    if index < 0:
        return await _enter_extras(update, context, details, notice=notice)
    return await _render_question(
        update, context, details, fields, index, notice=notice
    )


async def _question_fields_for(
    update: Update, context: ContextTypes.DEFAULT_TYPE, details
) -> list[AnnouncementFieldState]:
    return _askable_fields(_load_fields(_user_id(update), details.draft_id))


def _find_field(
    fields: list[AnnouncementFieldState], brick_id: int
) -> tuple[int, AnnouncementFieldState] | None:
    for index, field in enumerate(fields):
        if field.brick_id == brick_id:
            return index, field
    return None


async def _question_from_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
) -> (
    tuple[
        dict[str, Any], Any, list[AnnouncementFieldState], int, AnnouncementFieldState
    ]
    | None
):
    guarded = _guard_details(update, context, draft_id, revision)
    if guarded is None:
        return None
    ui, details = guarded
    fields = await _question_fields_for(update, context, details)
    found = _find_field(fields, brick_id)
    if found is None:
        return None
    index, field = found
    if ui.get("active_brick_id") != brick_id:
        return None
    if not _delivered_text_question_allowed(details, field):
        # Подделанный brick_id поля площадки у доставленного анонса: отказ
        # без изменения данных (§5). Легитимный вопрос открыт только для
        # текстового поля и проверку проходит.
        return None
    return ui, details, fields, index, field


async def _advance_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    next_index: int,
    *,
    notice: str | None = None,
) -> int:
    fields = await _question_fields_for(update, context, details)
    if not fields:
        return await _enter_extras(update, context, details, notice=notice)
    if next_index >= len(fields):
        return await _after_questions(update, context, details, notice=notice)
    if next_index < 0:
        next_index = 0
    return await _render_question(
        update, context, details, fields, next_index, notice=notice
    )


async def _after_questions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """
    Последний кирпичик ведёт на шаг фотографий, а не к готовому посту.

    Poster 2.0 S3 п.6: после всех кирпичиков обязательно спрашиваются
    фотографии; завершает анонс «Без фотографий» или готовая партия фото.
    Кнопки «Готово»/«Сформировать» нет ни на одном шаге.
    """
    try:
        updated = _ensure_stage(update, context, details, WizardStage.PREVIEW)
    except AnnouncementError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    if updated.publication_sent:
        # Анонс уже доставлен. Экрана «✅ Анонс готов» нет (spec §18):
        # автор возвращается к управлению готовым анонсом, а правка
        # применяется сразу, без промежуточной кнопки. Вход в редактор —
        # список текстовых полей (§5, 2.5.3), второго экрана нет.
        return await _render_field_list(update, context, updated)
    return await wizard_photos.render_photo_step(
        update,
        context,
        updated,
        notice=notice,
        announcement_is_empty=_announcement_is_empty(update, updated),
    )


async def _back_from_photo_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
) -> int:
    """
    «◀️ Назад» с шага фото: возврат к предыдущему шагу мастера.

    Обычный путь — последний кирпичик (шаг фото идёт сразу после вопросов).
    Если кирпичиков нет или все они необязательные и уже пропущены,
    показывается менеджер полей: это тот же шаг вопросов, просто списком.
    Черновик и фотографии не меняются.
    """
    fields = await _question_fields_for(update, context, details)
    if not fields:
        return await _render_question_manager(update, context, details)
    return await _render_question(update, context, details, fields, len(fields) - 1)


async def _drop_accepted_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    field_key: str,
) -> None:
    """
    Удаляет ввод пользователя после ПОДТВЕРЖДЁННОГО сохранения (2.5.3 §7).

    Раньше ввод удалялся сразу после записи значения, ещё до проверки длины
    и доставки: при превышении лимита сообщение с длинным текстом исчезало,
    хотя экран обещал обратное. Теперь вызывающий слой зовёт это только
    тогда, когда данные уже в БД, а доставка (если она нужна) подтверждена.
    """
    user_data = ctx.data(context)
    stale = screen.forget_failed_inputs(user_data, field_key)
    if stale:
        await screen.cleanup_messages(context.bot, ctx.chat(update).id, stale)
    await screen.drop_user_input(update, context)


async def _save_question_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    value: object,
    *,
    index: int,
    drop_input: bool = False,
) -> int:
    user_id = _user_id(update)
    try:
        with SessionLocal() as session:
            updated = save_announcement_value(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                brick_id=field.brick_id,
                value=value,
                expected_revision=details.revision,
                today=app_today(),
            )
    except AnnouncementError as exc:
        message = str(exc)
        if drop_input:
            user_data = ctx.data(context)
            screen.track_failed_input(
                user_data,
                f"aw:{field.brick_id}",
                update.effective_message,
            )
        fields = _askable_fields(_load_fields(user_id, details.draft_id))
        return await _render_question(
            update,
            context,
            details,
            fields,
            index,
            editing=True,
            error=message,
        )

    ui = _ui(context)
    field_edit_instance = _field_edit_instance(ui)
    _set_details(ui, updated)
    _clear_active(ui)
    if drop_input and not updated.publication_sent:
        # Черновик ещё не доставлен: значение сохранено в БД, ввод можно
        # убирать — это не потеря.
        await _drop_accepted_input(update, context, f"aw:{field.brick_id}")
    if updated.publication_sent:
        # H3 (требование B): правка готового анонса применяется сразу.
        # Отдельной кнопки «Применить изменения» нет, поэтому после
        # сохранения значения новая версия заменяет прошлую, а автор
        # видит экран завершения с обновлённым постом выше.
        applied = await _apply_immediately(update, context, updated)
        if applied is None:
            # Причину уже показал `_finish_and_deliver` (лимит, ошибка
            # сборки, отправка): перекрывать её нельзя. Ввод остаётся в
            # чате — доставка не подтверждена (2.5.3 §7). Если причина —
            # превышение лимита, автор видит КОРОТКУЮ ошибку и может
            # сократить текст сразу: вопрос остаётся открытым.
            refusal = _length_check(update, updated)
            if refusal is not None:
                fields = _askable_fields(_load_fields(user_id, updated.draft_id))
                found = _find_field(fields, field.brick_id)
                if found is not None:
                    return await _render_question(
                        update,
                        context,
                        updated,
                        fields,
                        found[0],
                        editing=True,
                        error=_length_refusal_text(refusal),
                    )
                await _answer(update, _length_refusal_text(refusal), alert=True)
            return AW_PREVIEW
        if drop_input:
            # Ввод удаляется только после ПОДТВЕРЖДЁННОЙ доставки.
            await _drop_accepted_input(update, context, f"aw:{field.brick_id}")
        await _answer(
            update,
            tr(
                "alerts.wizard.znachenie_sohraneno",
                title=_short(brick_title_display(field.title, field.code), 50),
            ),
        )
        if field_edit_instance is not None:
            # §5 (2.5.3): выбор поля из списка «📝 Текстовые поля» даёт один
            # временный вопрос. Значение сохранено и применено — вопрос
            # закрывается, управление возвращается анонсу; экрана
            # «Применить изменения»/«Обзор» в этом сценарии нет.
            ui.pop("field_edit", None)
            await _close_service_screen(update, context)
            await _show_announcement_controls(
                context,
                ctx.chat(update).id,
                applied,
                instance=_instance_for(applied, field_edit_instance),
            )
        return AW_PREVIEW
    return await _advance_question(
        update,
        context,
        updated,
        index + 1,
        notice=tr(
            "alerts.wizard.znachenie_sohraneno",
            title=_short(brick_title_display(field.title, field.code), 50),
        ),
    )


async def _skip_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
) -> int:
    """
    «⏭ Пропустить» НЕ очищает значение (Poster 2.4, требование VI).

    Уже заполненное поле сохраняется как есть: строка значения не трогается,
    ревизия не растёт, лишних сообщений и копий анонса нет — мастер просто
    идёт дальше. Пустое/пропущенное поле остаётся пустым (`empty_optional`
    — осознанный пропуск, а не записанный поверх valid).
    """
    if field.status == FIELD_STATUS_VALID:
        ui = _ui(context)
        _clear_active(ui)
        return await _advance_question(
            update,
            context,
            details,
            index + 1,
            notice=tr(
                "wizard.skip_question.znachenie_ostavleno",
                title=_short(brick_title_display(field.title, field.code), 50),
            ),
        )
    return await _save_question_value(update, context, details, field, "", index=index)


async def _render_question_manager(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    fields = await _question_fields_for(update, context, details)
    if not fields:
        return await _enter_extras(update, context, details, notice=notice)
    _nav_enter(ui, NAV_MANAGER)
    lines = [
        tr("wizard.render_question_manager.polya_anonsa"),
        "",
        tr("wizard.render_question_manager.vyberite_pole_chtoby_izmenit"),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for index, field in enumerate(fields, start=1):
        status = _field_status_text(field)
        rows.append(
            [
                InlineKeyboardButton(
                    _short(
                        f"{index}. "
                        f"{brick_title_display(field.title, field.code)} — {status}",
                        60,
                    ),
                    callback_data=(
                        f"aw_qgoto:{details.draft_id}:"
                        f"{details.revision}:{field.brick_id}"
                    ),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="aw_manage_back")]
    )
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_QUESTIONS


# —— Poster 2.5.3 §5 / 2.5.4 §1: текстовые поля доставленного анонса ——————
#
# Список полей НЕ живёт отдельным экраном: у 0–1 фото он заменяет клавиатуру
# самого анонса (`editMessageReplyMarkup`, текст и подпись не меняются), у
# медиагруппы — текст её единственной панели (`editMessageText`). Второго
# сообщения с копией текста и второй панели не появляется.
#
# Выбор поля (2.5.4 §1) НЕ создаёт служебного вопроса: клавиатура того же
# сообщения заменяется клавиатурой ввода (`announcement_field_input`), а
# сохранение значения и возврат к списку полей идут общим маршрутом
# `_save_field_input_value`. Ни sendMessage, ни «Теперь введите…», ни
# отдельного сообщения об успехе в этом сценарии нет.


def _field_list_text(fields: list[AnnouncementFieldState]) -> str:
    """Тело списка полей: заголовок и одна подсказка, без перечня полей."""
    lines = [tr("wizard.fields.title"), ""]
    lines.append(tr("wizard.fields.hint") if fields else tr("wizard.fields.empty"))
    return "\n".join(lines)


def _field_list_keyboard(
    fields: list[AnnouncementFieldState],
    instance_id: int,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """
    Клавиатура списка текстовых полей (5 на страницу, как остальные списки).

    У поля — та же подпись, что в «Полях анонса»: «1. Название — статус».
    «◀️ Назад» возвращает управление анонсу, а не выдумывает новый экран.
    """
    page_slice = pagination.page_items(fields, page)
    rows: list[list[InlineKeyboardButton]] = []
    for number, field in enumerate(page_slice.items, start=page_slice.offset + 1):
        rows.append(
            [
                InlineKeyboardButton(
                    _short(
                        # Подпись кнопки — plain-текст Telegram: `tr_plain`
                        # не экранирует название кирпичика (`<u>` остаётся
                        # тегом в названии, а не `&lt;u&gt;` на кнопке).
                        tr_plain(
                            "wizard.fields.button",
                            number=number,
                            title=brick_title_display(field.title, field.code),
                            status=_field_status_text(field),
                        ),
                        60,
                    ),
                    callback_data=announcement_controls.field_callback(
                        announcement_controls.FIELD_ACTION_SELECT,
                        instance_id,
                        field.brick_id,
                    ),
                )
            ]
        )
    nav = pagination.nav_buttons(
        page_slice.page,
        page_slice.total_pages,
        (
            f"{announcement_controls.FIELD_CALLBACK_PREFIX}"
            f"{announcement_controls.FIELD_ACTION_PAGE}:{instance_id}"
        ),
    )
    if nav:
        rows.append(
            [InlineKeyboardButton(label, callback_data=data) for label, data in nav]
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.nav.back"),
                callback_data=announcement_controls.field_callback(
                    announcement_controls.FIELD_ACTION_BACK, instance_id
                ),
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _field_page(offset: int) -> int:
    """Номер страницы по смещению из callback (протокол списков Poster)."""
    size = pagination.PAGE_SIZE
    return max(0, int(offset) // size) if size > 0 else 0


def _instance_for(details, instance_id: int | None = None):
    """
    Экземпляр для списка полей: заданный (сверяется с черновиком) или активный.

    `None` — экземпляра нет, он архивный или принадлежит другому черновику:
    вызывающий слой показывает безопасный экран, а не открывает редактор.
    """
    try:
        with SessionLocal() as session:
            if instance_id is None:
                return get_active_instance(session, int(details.draft_id))
            instance = get_instance(session, int(instance_id))
    except Exception as exc:  # noqa: BLE001 — без экземпляра нет редактора
        logger.info("Экземпляр анонса #%s не прочитан: %s", instance_id, exc)
        return None
    if instance is None or instance.is_archived:
        return None
    if int(instance.draft_id) != int(details.draft_id):
        return None
    return instance


def _editor_instance_id(
    ui: dict[str, Any], details, instance_id: int | None = None
) -> int | None:
    """
    Экземпляр редактора доставленного анонса для пути навигации.

    `None` — черновик ещё не доставлен (история конструктора) или
    экземпляра нет: тогда работает прежний путь мастера. У доставленного
    анонса путь принадлежит конкретному экземпляру и не переносится на
    другой анонс даже в том же чате.

    Явный `instance_id` — вход в редактор из управления экземпляром. Без
    него (пагинация, возврат после сохранения значения) путь продолжается
    только если он УЖЕ редакторский: иначе активный экземпляр чата подменил
    бы маршрут мастера чужим редактором.
    """
    if not details.publication_sent:
        return None
    if isinstance(instance_id, int):
        instance = _instance_for(details, instance_id)
        return None if instance is None else int(instance.id)
    stack = _nav_stack(ui)
    if not stack or stack[0].get("screen") != NAV_EDITOR:
        return None
    value = stack[0].get("instance_id")
    if not isinstance(value, int):
        return None
    instance = _instance_for(details, value)
    return None if instance is None else int(instance.id)


def _field_edit_instance(ui: dict[str, Any]) -> int | None:
    """
    id экземпляра, чей текстовый вопрос сейчас открыт (2.5.3 §5).

    `None` — вопрос открыт обычным путём конструктора, а не из списка
    «📝 Текстовые поля»: тогда после сохранения работает прежний поток
    (следующий вопрос или шаг фото). Иначе значение принадлежит
    доставленному анонсу, и после применения экран вопроса закрывается.
    """
    if not ui.get("field_edit"):
        return None
    instance_id = ui.get("field_instance_id")
    return int(instance_id) if isinstance(instance_id, int) else None


def _control_keyboard(details, instance) -> InlineKeyboardMarkup:
    """Клавиатура управления экземпляром с актуальным числом фото."""
    photos = len(details.publication_photos)
    return announcement_controls.panel_keyboard(
        instance.id,
        photos_count=photos,
        extras_available=_draft_extras_available(details.draft_id),
    )


async def _show_announcement_controls(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    details,
    *,
    instance=None,
) -> None:
    """
    Возвращает управление анонсу: клавиатуру сообщения или текст панели.

    Используется «◀️ Назад» из списка полей и завершением правки: в чате
    снова остаётся ровно одно место управления (§18/§22). Ошибка Telegram
    не ломает сценарий — анонс уже в чате, а кнопки восстановит повтор.
    """
    instance = instance if instance is not None else _instance_for(details)
    if instance is None:
        return
    keyboard = _control_keyboard(details, instance)
    if delivery.needs_control_panel(len(details.publication_photos)):
        panel_id = instance.panel_message_id
        if panel_id is None:
            return
        try:
            await delivery.call_telegram(
                lambda: context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(panel_id),
                    text=announcement_controls.panel_text(),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    link_preview_options=announcement_controls.PANEL_LINK_PREVIEW,
                )
            )
        except Exception as exc:  # noqa: BLE001 — панель уже в чате
            logger.info(
                "Панель экземпляра #%s не вернулась к управлению: %s",
                instance.id,
                exc,
            )
        return
    message_id = delivery.caption_message_id(instance.message_ids)
    if message_id is None:
        return
    try:
        await delivery.call_telegram(
            lambda: context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=int(message_id),
                reply_markup=keyboard,
            )
        )
    except Exception as exc:  # noqa: BLE001 — кнопки уже на анонсе
        logger.info(
            "Клавиатура управления экземпляра #%s не восстановлена: %s",
            instance.id,
            exc,
        )


async def _apply_field_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    instance,
    *,
    page: int = 0,
) -> bool:
    """
    Показывает список полей НА существующем управлении анонса.

    Для 2+ фото это текст единственной панели (`editMessageText`), иначе —
    только клавиатура сообщения анонса (`editMessageReplyMarkup`): текст и
    подпись не трогаются, второго сообщения не появляется.

    `False` — поля не прочитались (например, кирпичик ушёл в архив): это не
    повод ронять экран, но и обещать редактор нельзя. Вызывающий слой
    возвращает управление анонсу, а причину уже показал свой обработчик.
    """
    try:
        fields = _text_fields(_load_fields(_user_id(update), details.draft_id))
    except AnnouncementError as exc:
        logger.info("Поля анонса #%s не прочитаны: %s", details.draft_id, exc)
        await _show_announcement_controls(
            context, ctx.chat(update).id, details, instance=instance
        )
        return False
    chat_id = ctx.chat(update).id
    keyboard = _field_list_keyboard(fields, instance.id, page)
    if delivery.needs_control_panel(len(details.publication_photos)):
        panel_id = instance.panel_message_id
        if panel_id is None:
            return True
        try:
            await delivery.call_telegram(
                lambda: context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(panel_id),
                    text=_field_list_text(fields),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    link_preview_options=announcement_controls.PANEL_LINK_PREVIEW,
                )
            )
        except Exception as exc:  # noqa: BLE001 — панель уже в чате
            logger.info("Список полей не показан в панели #%s: %s", instance.id, exc)
        return True
    message_id = delivery.caption_message_id(instance.message_ids)
    if message_id is None:
        return True
    try:
        await delivery.call_telegram(
            lambda: context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=int(message_id),
                reply_markup=keyboard,
            )
        )
    except Exception as exc:  # noqa: BLE001 — клавиатура анонса не критична
        logger.info("Список полей не показан у анонса #%s: %s", instance.id, exc)
    return True


async def _render_field_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    instance_id: int | None = None,
    page: int = 0,
) -> int:
    """
    Список текстовых полей доставленного анонса (§5, 2.5.3).

    Экран не создаётся: список заменяет управление существующего анонса.
    Временный экран конструктора, с которого пришли, закрывается, чтобы в
    чате не осталось второй клавиатуры.

    Активного экземпляра нет (анонс переиздан или запись удалена) — правка
    недоступна: короткое пояснение и «Мои анонсы» вместо выдуманного
    редактора. Возврат в обзор здесь запрещён: он снова привёл бы сюда.
    """
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    instance = _instance_for(details, instance_id)
    if instance is None:
        await _answer(update, announcement_controls.STALE_INSTANCE_TEXT, alert=True)
        return await _render_home(update, context)
    ui["field_instance_id"] = instance.id
    # Список полей — вложенный экран СВОЕГО маршрута правки доставленного
    # анонса, а не продолжение пути конструктора: под ним в стеке ещё лежат
    # вопросы, на которые уже ответили. Путь нормализуется по экземпляру:
    # история конструктора отбрасывается, а внизу остаётся общий редактор —
    # иначе дедупликация ниже приняла бы новый список за возврат на вопрос
    # того же кирпичика, и «Назад» из ввода поля уводил бы на «Вопрос N из M».
    _nav_enter(ui, NAV_FIELDS, page=page, instance_id=instance.id)
    stack = _nav_stack(ui)
    if stack:
        # Возврат «Назад» с пути знает, чей это список: id экземпляра, а не
        # черновик из user_data (после рестарта он тоже восстанавливается).
        stack[-1]["instance_id"] = instance.id
    await _close_service_screen(update, context)
    await _apply_field_list(update, context, details, instance, page=page)
    return AW_PREVIEW


async def _open_field_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    instance,
    brick_id: int,
) -> int:
    """
    Открывает ВВОД выбранного поля НА самом анонсе (2.5.4 §1).

    Нового сообщения не создаётся: клавиатура сообщения анонса (0–1 фото)
    или единственной панели медиагруппы заменяется клавиатурой ввода
    (`announcement_field_input`). Управление анонсу не «возвращается» — его
    и не отбирали: список полей и ввод живут на одном месте. Состояние
    (`field_input` = id экземпляра) позволяет общему обработчику текста
    понять, что введённое значение принадлежит доставленному анонсу.
    """
    if _edit_blocked(details):
        await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
        return AW_PREVIEW
    fields = await _question_fields_for(update, context, details)
    found = _find_field(fields, int(brick_id))
    if found is None:
        return await _stale(update, context)
    _index, field = found
    if not _is_text_field(field):
        # Поле площадки в редакторе не спрашивается (§5): устаревшая кнопка
        # старого списка получает короткий ответ, данные не меняются.
        return await _stale(update, context)
    return await _show_field_input(update, context, details, instance, field)


def _field_for_input(
    user_id: int, details, instance_id: int, brick_id: int
) -> AnnouncementFieldState | None:
    """
    Текстовое поле доставленного анонса по адресу из `anq_`-callback.

    Нужен обработчику кнопок ввода: он живёт вне `update`-контекста диалога
    и после рестарта службы, поэтому поле восстанавливается по id экземпляра
    и кирпичика. `user_id` — Telegram id автора: доменные запросы принимают
    именно его, а не внутренний id пользователя. `None` — экземпляр
    устарел/архивный, поле больше не входит в анонс или оно принадлежит
    площадке (§5): такая кнопка ничего не меняет.
    """
    instance = _instance_for(details, instance_id)
    if instance is None:
        return None
    try:
        fields = _text_fields(_load_fields(int(user_id), details.draft_id))
    except AnnouncementError as exc:
        logger.info("Поля анонса #%s не прочитаны: %s", details.draft_id, exc)
        return None
    found = _find_field(fields, int(brick_id))
    return found[1] if found is not None else None


def _field_input_context(user_id: int, ui: dict[str, Any], details):
    """
    Контекст ввода на самом анонсе: (экземпляр, поле) или `None`.

    `None` — ввод уже закрыт (список полей, другой экран, рестарт): тогда
    текст уходит обычным путём и значение не записывается в чужое поле.
    `user_id` — Telegram id автора черновика: маршрут работает и после
    рестарта, когда в `update` уже нет состояния диалога.
    """
    instance_id = ui.get("field_input")
    brick_id = ui.get("active_brick_id")
    if not isinstance(instance_id, int) or not isinstance(brick_id, int):
        return None
    instance = _instance_for(details, instance_id)
    if instance is None:
        return None
    field = _field_for_input(int(user_id), details, instance_id, brick_id)
    if field is None:
        return None
    return instance, field


def _field_input_page(ui: dict[str, Any]) -> int:
    """Страница списка полей, с которой открыт ввод (возврат «Назад/Готово»)."""
    for entry in reversed(_nav_stack(ui)):
        if entry.get("screen") == NAV_FIELDS:
            page = entry.get("page")
            return page if isinstance(page, int) and page >= 0 else 0
    return 0


async def _field_input_error(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    instance,
    field: AnnouncementFieldState,
    message: str,
) -> int:
    """
    Ошибка входа значения БЕЗ потери данных (2.5.4 §1, п.6 / §3).

    Клавиатура ввода остаётся/восстанавливается на том же месте, введённое
    значение не записывается (CAS в домене не прошёл). Превышение лимита
    имеет отдельный экран (`_field_length_error`): там сохранённый ввод
    предлагается сократить или отменить. Все прочие ошибки получают
    короткий alert/ответ на сообщение автора — без нового экрана.
    """
    view = _field_input_view(_ui(context))
    await announcement_field_input.apply_input_screen(
        context.bot, ctx.chat(update).id, details, instance, field, view=view
    )
    if getattr(update, "callback_query", None) is not None:
        await _answer(update, message, alert=True)
        return AW_PREVIEW
    msg = update.effective_message
    if msg is not None:
        try:
            notice = await msg.reply_text(message[:200])
        except Exception as exc:  # noqa: BLE001 — ответ не должен ломать ввод
            logger.info("Ошибка ввода поля не показана: %s", exc)
        else:
            screen.track_temp(ctx.data(context), notice)
    return AW_PREVIEW


def _field_input_view(ui: dict[str, Any]) -> announcement_field_input.CalendarView:
    """Уровень календаря на клавиатуре ввода из состояния (None — первый)."""
    value = ui.get("field_input_view")
    if isinstance(value, announcement_field_input.CalendarView):
        return value
    return announcement_field_input.CalendarView()


# —— 2.5.4 §3: экран ошибки длины и отмена незавершённой правки ————————
#
# Отдельного хранилища незавершённой правки нет: длинный текст уже лежит в
# `announcement_values` (строка состояния `valid`), а опубликованная версия
# и сообщение в чате не менялись. Поэтому достаточно запомнить СНИМОК
# значения, которое было до правки (последнее опубликованное), — по нему
# «Отменить» вернёт ровно это значение и ничего не опубликует.
#
# Снимок живёт в `user_states.payload` рядом с resume-ключом: он переживает
# рестарт службы и не требует миграции. Ревизия черновика в снимке и в
# `callback_data` отсекает поздние нажатия: после успешного сохранения
# ревизия другая, и старая кнопка «Отменить» нового значения не откатывает.
FIELD_LENGTH_EDIT_KEY = "announcement_field_length_edit"


def _field_value_snapshot(user_id: int, details, brick_id: int) -> dict[str, object]:
    """Снимок значения поля в БД ДО правки: `{state, value_json}`."""
    try:
        with SessionLocal() as session:
            state, value_json = announcement_value_snapshot(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                brick_id=brick_id,
            )
    except AnnouncementError:
        return {"state": "missing", "value_json": None}
    return {"state": state, "value_json": value_json}


def _remember_length_edit(
    user_id: int,
    details,
    instance_id: int,
    brick_id: int,
    snapshot: dict[str, object],
) -> None:
    """
    Запоминает незавершённую правку для экрана ошибки длины (2.5.4 §3).

    Снимок «до правки» берётся один раз за незавершённую правку: повторная
    непоместившаяся попытка увеличивает ревизию, но «Отменить» обязана
    вернуть последнее ОПУБЛИКОВАННОЕ значение, а не предыдущую неудачную
    попытку. Поэтому у существующей отметки того же поля обновляется только
    ревизия (она нужна для защиты от позднего callback), а `state`/
    `value_json` остаются первыми.

    Отметка снимается при успешном сохранении или отмене — тогда следующая
    ошибка длины снова возьмёт свежий снимок опубликованного значения.
    Сбой записи не ломает экран: отмена просто не сработает, данные целы.
    """
    existing = screen.load_payload(user_id) or {}
    current = existing.get(FIELD_LENGTH_EDIT_KEY)
    base_state = str(snapshot.get("state") or "missing")
    base_json = snapshot.get("value_json")
    if (
        isinstance(current, dict)
        and current.get("draft_id") == details.draft_id
        and current.get("brick_id") == brick_id
    ):
        base_state = str(current.get("state") or base_state)
        base_json = current.get("value_json", base_json)
    screen.save_payload_key(
        user_id,
        FIELD_LENGTH_EDIT_KEY,
        {
            "draft_id": int(details.draft_id),
            "brick_id": int(brick_id),
            "instance_id": int(instance_id),
            "revision": int(details.revision),
            "state": base_state,
            "value_json": base_json,
        },
    )


def _drop_length_edit(user_id: int, draft_id: int, brick_id: int) -> None:
    """Снимает отметку о незавершённой правке поля (успех/отмена)."""
    payload = screen.load_payload(user_id) or {}
    stored = payload.get(FIELD_LENGTH_EDIT_KEY)
    if not isinstance(stored, dict):
        return
    if stored.get("draft_id") != draft_id or stored.get("brick_id") != brick_id:
        return
    screen.save_payload_key(user_id, FIELD_LENGTH_EDIT_KEY, None)


def _load_length_edit(user_id: int, details, brick_id: int) -> dict[str, object] | None:
    """Незавершённая правка поля из `user_states.payload` (None — её нет)."""
    payload = screen.load_payload(user_id) or {}
    stored = payload.get(FIELD_LENGTH_EDIT_KEY)
    if not isinstance(stored, dict):
        return None
    if stored.get("draft_id") != details.draft_id:
        return None
    if stored.get("brick_id") != brick_id:
        return None
    return stored


async def _field_length_error(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    instance,
    field: AnnouncementFieldState,
    check,
    snapshot: dict[str, object],
) -> int:
    """
    Экран ошибки длины для правки ДОСТАВЛЕННОГО анонса (2.5.4 §3).

    Длинный текст уже сохранён в незавершённой правке (значение в БД), а
    опубликованный пост и его сообщение в чате не тронуты. На самом
    управлении анонса показывается короткая причина и ровно две кнопки:
    неактивная подсказка «⏳ Ожидаю текст короче» и «Отменить». Никаких
    списков полей, «Назад», «Обзора» и «уберите фото»: фото может не быть.

    Пользовательское сообщение с длинным текстом НЕ удаляется — это и есть
    сохранённый ввод, которым автор сократит текст.
    """
    _remember_length_edit(
        _user_id(update),
        details,
        instance.id,
        field.brick_id,
        snapshot,
    )
    # Ревизия в состоянии должна совпадать с БД: иначе следующий (уже
    # короткий) текст считался бы устаревшим и не заменил незавершённую
    # правку.
    _set_details(_ui(context), details)
    text = tr(
        "wizard.length_refusal.edit_not_applied",
        length=check.length,
        limit=check.limit,
    )
    has_panel = delivery.needs_control_panel(len(details.publication_photos))
    shown = await announcement_field_input.apply_length_error_screen(
        context.bot,
        ctx.chat(update).id,
        details,
        instance,
        field,
        revision=details.revision,
        text=text,
    )
    if shown and not has_panel:
        # 0–1 фото: текст анонса и подпись не меняются, поэтому причина
        # приходит отдельным коротким ответом на ввод (Telegram не умеет
        # показать текст кнопкой). Клавиатура анонса заменена экраном из
        # двух кнопок. Ответ best-effort: данные и ввод уже целы.
        msg = update.effective_message
        if msg is not None:
            try:
                notice = await msg.reply_text(text)
            except Exception as exc:  # noqa: BLE001 — ответ не ломает ввод
                logger.info("Ошибка длины не показана: %s", exc)
            else:
                screen.track_temp(ctx.data(context), notice)
    if not shown:
        # Адрес управления потерян: не оставляем «зависший» экран, короткая
        # причина уходит ответом, данные и ввод целы.
        if getattr(update, "callback_query", None) is not None:
            await _answer(update, text, alert=True)
        else:
            msg = update.effective_message
            if msg is not None:
                try:
                    notice = await msg.reply_text(text[:200])
                except Exception as exc:  # noqa: BLE001 — ответ не ломает ввод
                    logger.info("Ошибка длины не показана: %s", exc)
                else:
                    screen.track_temp(ctx.data(context), notice)
        return AW_PREVIEW
    _remember_resume(
        _user_id(update), ctx.data(context), ctx.chat(update).id, _ui(context)
    )
    return AW_PREVIEW


async def _cancel_field_length_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    instance,
    field: AnnouncementFieldState,
    revision: int,
) -> int:
    """
    «Отменить» на экране ошибки длины (2.5.4 §3).

    Возвращает ТОЛЬКО значение этого поля к последнему опубликованному
    (снимок до правки) и закрывает экран. Публикации нет: доставленная
    версия и её сообщение не менялись, поэтому повторно применять нечего.
    Другие поля не откатываются.

    Устаревшая кнопка безопасна: если ревизия черновика другая (правка уже
    применена или изменена другим действием), снимка нет или он про другое
    поле, ничего не пишется, а клиент получает короткий alert «Кнопка
    устарела».
    """
    user_id = _user_id(update)
    stored = _load_length_edit(user_id, details, field.brick_id)
    if stored is None or stored.get("revision") != int(revision):
        await _answer(update, announcement_controls.STALE_INSTANCE_TEXT, alert=True)
        return AW_PREVIEW
    if int(details.revision) != int(revision):
        # Позднее нажатие после успешного сохранения: ревизия сдвинулась,
        # новое значение уже в анонсе — откатывать нечего.
        await _answer(update, announcement_controls.STALE_INSTANCE_TEXT, alert=True)
        return AW_PREVIEW
    try:
        with SessionLocal() as session:
            raw_json = stored.get("value_json")
            restored = restore_announcement_value(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                brick_id=field.brick_id,
                state=str(stored.get("state") or "missing"),
                value_json=raw_json if isinstance(raw_json, str) else None,
                expected_revision=details.revision,
            )
    except (AnnouncementError, AnnouncementConflictError) as exc:
        logger.info("Отмена правки поля не выполнена: %s", exc)
        await _answer(update, announcement_controls.STALE_INSTANCE_TEXT, alert=True)
        return AW_PREVIEW
    _drop_length_edit(user_id, restored.draft_id, field.brick_id)
    _set_details(_ui(context), restored)
    return await _return_to_field_list(update, context, restored, instance.id)


async def _return_to_field_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    instance_id: int,
) -> int:
    """
    Возврат к СПИСКУ ПОЛЕЙ на том же сообщении (2.5.4 §1, п.4–5).

    Список — клавиатура анонса (0–1 фото) или текст единственной панели
    (2+ фото) с актуальными значениями; страница берётся из пути, поэтому
    «Назад» из ввода поля не сбрасывает список на первую страницу.
    """
    page = _field_input_page(_ui(context))
    return await _render_field_list(
        update, context, details, instance_id=instance_id, page=page
    )


async def _save_field_input_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    instance,
    field: AnnouncementFieldState,
    value: object,
    *,
    drop_input: bool = False,
    stay: bool = False,
) -> int:
    """
    ЕДИНЫЙ маршрут сохранения значения поля готового анонса (2.5.4 §1).

    Используется и ручным вводом текста, и готовыми вариантами
    (время/календарь/выбор): копий на каждый тип поля нет. После записи
    значение применяется к доставленной версии на месте (механизм 2.5.3,
    `_apply_immediately`/`_apply_in_place_change`), а клавиатура того же
    сообщения возвращается к списку полей. Отдельного сообщения об успехе и
    второй копии анонса не появляется.

    `stay` — множественный выбор: отметка варианта обновляет анонс, но
    клавиатура ввода остаётся, чтобы автор мог отметить несколько значений
    подряд (как на экране вопроса конструктора); «✅ Готово» возвращает к
    списку полей.
    """
    ui = _ui(context)
    # 2.5.4 §3: снимок значения ДО правки — по нему «Отменить» на экране
    # ошибки длины вернёт последнее опубликованное значение. Снимок берётся
    # до записи, только для доставленного анонса и только для текста: у
    # времени/даты/выбора лимита длины нет.
    draft_edit_snapshot = (
        _field_value_snapshot(_user_id(update), details, field.brick_id)
        if details.publication_sent
        and field.value_type in (BRICK_TYPE_SHORT_TEXT, BRICK_TYPE_LONG_TEXT)
        else None
    )
    try:
        with SessionLocal() as session:
            updated = save_announcement_value(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                brick_id=field.brick_id,
                value=value,
                expected_revision=details.revision,
                today=app_today(),
            )
    except AnnouncementError as exc:
        return await _field_input_error(
            update, context, details, instance, field, str(exc)
        )
    # Журнал событий (этап B): подтверждённая запись значения поля. В detail
    # только идентификаторы — значение поля (содержимое анонса) не пишется.
    _event_field_saved(update, instance, field)
    _set_details(ui, updated)
    fresh = updated
    if updated.publication_sent:
        # H3: правка доставленного анонса применяется сразу — кнопки
        # «Применить изменения» нет. `None` — версия не обновлена (лимит,
        # ошибка сборки): причину уже показал `_finish_and_deliver`, данные
        # и фото целы, клавиатура ввода восстанавливается.
        applied = await _apply_immediately(update, context, updated)
        fresh = _reload_details(update, updated)
        if applied is None:
            refusal = _length_check(update, fresh)
            if refusal is not None and draft_edit_snapshot is not None:
                # 2.5.4 §3: превышение лимита — отдельный экран с двумя
                # кнопками. Длинный текст уже в незавершённой правке (БД),
                # опубликованный анонс и сообщение не менялись.
                return await _field_length_error(
                    update,
                    context,
                    fresh,
                    instance,
                    field,
                    refusal,
                    draft_edit_snapshot,
                )
            message = (
                _length_refusal_text(refusal)
                if refusal is not None
                else APPLY_FAILED_TEXT
            )
            return await _field_input_error(
                update, context, fresh, instance, field, message
            )
    if drop_input:
        # Ввод удаляется только после ПОДТВЕРЖДЁННОГО сохранения (2.5.3 §7).
        await _drop_accepted_input(update, context, f"aw:{field.brick_id}")
    # 2.5.4 §3: правка применена — незавершённой правки этого поля больше
    # нет, снимок «до правки» снимается (следующая ошибка длины возьмёт
    # свежий, уже опубликованный снимок).
    _drop_length_edit(_user_id(update), updated.draft_id, field.brick_id)
    if stay:
        # Множественный выбор: перерисовываем клавиатуру по СВЕЖЕМУ полю,
        # иначе отметки ✅ остались бы от значения до сохранения.
        fresh_field = (
            _field_for_input(_user_id(update), fresh, instance.id, field.brick_id)
            or field
        )
        return await _show_field_input(update, context, fresh, instance, fresh_field)
    return await _return_to_field_list(update, context, fresh, instance.id)


async def _show_field_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    instance,
    field: AnnouncementFieldState,
    view: announcement_field_input.CalendarView | None = None,
) -> int:
    """
    Показывает (или перерисовывает) клавиатуру ввода поля на анонсе.

    Состояние `field_input`/`field_input_view` выставляется здесь же, а
    resume-снимок записывается в БД: после рестарта службы введённый текст
    продолжает попадать в это поле (см. `_resume_target`), а не в подсказку
    меню. Новых сообщений нет — правится клавиатура управления.
    """
    ui = _ui(context)
    _set_details(ui, details)
    ui["active_brick_id"] = field.brick_id
    ui["active_source"] = "field"
    ui["field_input"] = instance.id
    ui["field_input_view"] = view
    shown = await announcement_field_input.apply_input_screen(
        context.bot, ctx.chat(update).id, details, instance, field, view=view
    )
    if not shown:
        # Адрес сообщения/панели потерян: ввод показать некуда, «зависших»
        # состояний не оставляем — возвращаемся к списку полей.
        _clear_active(ui)
        ui.pop("field_input", None)
        ui.pop("field_input_view", None)
        return await _return_to_field_list(update, context, details, instance.id)
    _remember_resume(_user_id(update), ctx.data(context), ctx.chat(update).id, ui)
    return AW_PREVIEW


def _extra_field_state(details, definition) -> AnnouncementFieldState:
    """
    Состояние значения кирпичика для экрана доп. полей.

    `definition` — определение кирпичика (`BrickDefinition`) из payload
    рендера: экран показывает и незаполненные кирпичики, поэтому для
    «нет строки» возвращается MISSING, а не ошибка.
    """
    value = details.value_for(definition.id)
    status = FIELD_STATUS_MISSING
    raw: object | None = None
    raw_html: str | None = None
    if value is not None and value.status is BrickValueStatus.VALID:
        status = FIELD_STATUS_VALID
        raw = answer_payload_to_raw(
            BrickValueResult(
                status=value.status,
                payload=dict(value.payload) if value.payload is not None else None,
            )
        )
        stored_html = (value.payload or {}).get("html")
        if isinstance(stored_html, str):
            raw_html = stored_html
    elif value is not None and value.status is BrickValueStatus.EMPTY_OPTIONAL:
        status = FIELD_STATUS_SKIPPED
    return AnnouncementFieldState(
        brick_id=definition.id,
        code=definition.code,
        title=definition.title,
        value_type=definition.value_type,
        status=status,
        raw_value=raw,
        rules=dict(definition.rules),
        description=definition.description,
        raw_html=raw_html,
    )


def _extra_definition(details, extra, telegram_user_id: int):
    with SessionLocal() as session:
        payload = build_announcement_render_payload(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=details.draft_id,
        )
    return payload.definitions_by_id[extra.brick_id]


def _choice_context(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
) -> tuple[dict[str, Any], Any, AnnouncementFieldState, int, str] | None:
    guarded = _guard_details(update, context, draft_id, revision)
    if guarded is None:
        return None
    ui, details = guarded
    if ui.get("active_brick_id") != brick_id:
        return None
    source = ui.get("active_source")
    if source == "question":
        fields = _askable_fields(_load_fields(_user_id(update), draft_id))
        found = _find_field(fields, brick_id)
        if found is None:
            return None
        index, field = found
    elif source == "extra":
        extra = next(
            (item for item in details.extras if item.brick_id == brick_id),
            None,
        )
        if extra is None:
            return None
        definition = _extra_definition(details, extra, _user_id(update))
        field = _extra_field_state(details, definition)
        index = -1
    else:
        return None
    if field.value_type != BRICK_TYPE_CHOICE:
        return None
    return ui, details, field, index, source


async def _handle_choice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
    option_index: int,
) -> int:
    found = _choice_context(update, context, draft_id, revision, brick_id)
    if found is None:
        return await _stale(update, context)
    ui, details, field, index, source = found
    options = _choice_options(field)
    if option_index < 0 or option_index >= len(options):
        return await _stale(update, context)
    option = options[option_index]
    multiple = bool(field.rules.get("multiple"))
    if multiple:
        selected = _selected_choices(field)
        if option in selected:
            selected = [item for item in selected if item != option]
        else:
            selected.append(option)
        raw_value: object = selected
    else:
        raw_value = option
    if source == "extra":
        extra = next(item for item in details.extras if item.brick_id == brick_id)
        return await _save_extra_value(
            update,
            context,
            details,
            extra,
            raw_value,
            stay_prompt=multiple,
            notice=(
                tr("wizard.handle_choice.vybor_sohranen_mozhno_otmetit")
                if multiple
                else tr("wizard.handle_choice.znachenie_dopolnitelnogo")
            ),
        )
    if multiple:
        try:
            with SessionLocal() as session:
                updated = save_announcement_value(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    brick_id=brick_id,
                    value=raw_value,
                    expected_revision=details.revision,
                    today=app_today(),
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
        _set_details(ui, updated)
        fields = await _question_fields_for(update, context, updated)
        new_found = _find_field(fields, brick_id)
        if new_found is None:
            return await _enter_extras(update, context, updated)
        return await _render_question(
            update,
            context,
            updated,
            fields,
            new_found[0],
            notice=tr("wizard.handle_choice.vybor_sohranen_mozhno_otmetit"),
        )
    return await _save_question_value(
        update, context, details, field, raw_value, index=index
    )


async def _handle_choice_done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
) -> int:
    found = _choice_context(update, context, draft_id, revision, brick_id)
    if found is None:
        return await _stale(update, context)
    _ui_data, details, field, index, source = found
    if not field.rules.get("multiple"):
        return await _stale(update, context)
    if field.status == FIELD_STATUS_MISSING:
        await _answer(update, tr("alerts.wizard.otmette_hotya_by_odin"), alert=True)
        return await _resume_current(update, context)
    if source == "extra":
        return await _enter_extras(
            update,
            context,
            details,
            notice=tr("wizard.handle_choice.znachenie_dopolnitelnogo"),
        )
    return await _advance_question(update, context, details, index + 1)


def _active_field(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
):
    """
    Поле активного вопроса по callback-адресу (общий путь для даты/времени).

    Устаревший revision или чужой brick_id → None: экран уже перерисован.
    """
    guarded = _guard_details(update, context, draft_id, revision)
    if guarded is None:
        return None
    ui, details = guarded
    if ui.get("active_brick_id") != brick_id:
        return None
    source = ui.get("active_source")
    if source == "question":
        fields = _askable_fields(_load_fields(_user_id(update), draft_id))
        found = _find_field(fields, brick_id)
        if found is None:
            return None
        index, field = found
    elif source == "extra":
        extra = next(
            (item for item in details.extras if item.brick_id == brick_id),
            None,
        )
        if extra is None:
            return None
        definition = _extra_definition(details, extra, _user_id(update))
        field = _extra_field_state(details, definition)
        index = -1
    else:
        return None
    return details, field, index, source


def _calendar_context(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
):
    found = _active_field(update, context, draft_id, revision, brick_id)
    if found is None:
        return None
    details, field, index, source = found
    if field.value_type != BRICK_TYPE_DATE:
        return None
    return details, field, index, source, _calendar_bounds(dict(field.rules))


async def _render_calendar_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    *,
    error: str | None = None,
    notice: str | None = None,
) -> int:
    if source == "extra":
        extra = next(item for item in details.extras if item.brick_id == field.brick_id)
        return await _render_extra_value_prompt(
            update, context, details, extra, error=error, notice=notice
        )
    fields = await _question_fields_for(update, context, details)
    return await _render_question(
        update,
        context,
        details,
        fields,
        index,
        error=error,
        notice=notice,
    )


async def _render_calendar_days(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    *,
    title: str,
    days: list[date],
    back_callback: str,
    back_label: str | None = None,
    per_row: int = 4,
    label: Callable[[date], str] = calendar_view.format_quick_day,
) -> int:
    """Экран дней (раскрытый диапазон или сетка месяца) с возвратом."""
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    rows = _day_rows(days, prefix, per_row=per_row, label=label)
    # Подпись читается в момент отрисовки: правка каталога видна без
    # перезапуска процесса (импорт-time значение застыло бы навсегда).
    label_text = back_label if back_label is not None else tr("buttons.nav.back")
    rows.append([InlineKeyboardButton(label_text, callback_data=back_callback)])
    await _render(update, context, title, InlineKeyboardMarkup(rows))
    return AW_QUESTIONS


async def _render_calendar_range_days(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    value_range: calendar_view.DateRange,
) -> int:
    """
    Раскрытие семидневного диапазона на том же служебном экране.

    Внизу — «◀️ К быстрым датам»: возврат к первому набору из восьми дат
    без перезахода в конструктор.
    """
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    return await _render_calendar_days(
        update,
        context,
        details,
        field,
        index,
        source,
        title=(
            tr(
                "wizard.render_calendar_range_days.vyberite_datu",
                format_range=calendar_view.format_range(*value_range),
            )
        ),
        days=calendar_view.days_between(*value_range),
        back_callback=f"aw_calquick:{prefix}",
        back_label=tr("wizard.render_calendar_range_days.k_bystrym_datam"),
    )


async def _render_calendar_months(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
) -> int:
    """Сетка месяцев горизонта: не больше 12 кнопок (месяц горизонта — стрелкой)."""
    start, end = _calendar_bounds(dict(field.rules))
    allowed = (start, end)
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    seam = calendar_view.selection_seam(app_today(), allowed)
    months = calendar_view.month_sequence(seam, end)
    months = [key for key in months if calendar_view.month_days(key, allowed)][
        : calendar_view.MONTH_GRID_MAX
    ]
    if not months:
        return await _render_calendar_value(
            update,
            context,
            details,
            field,
            index,
            source,
            error=tr("wizard.render_calendar_months.dlya_vybora_mesyaca_net"),
        )
    rows = keyboard_layouts.rows_of(
        (
            InlineKeyboardButton(
                calendar_view.format_month(key),
                callback_data=f"aw_calmonth:{prefix}:{key[0]}:{key[1]}",
            )
            for key in months
        ),
        3,
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("wizard.render_calendar_range_days.k_bystrym_datam"),
                callback_data=f"aw_calquick:{prefix}",
            )
        ]
    )
    await _render(
        update,
        context,
        tr("wizard.render_calendar_months.vyberite_mesyac"),
        InlineKeyboardMarkup(rows),
    )
    return AW_QUESTIONS


async def _render_calendar_month(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    key: calendar_view.MonthKey,
) -> int:
    """
    Календарь месяца: дни сеткой по семь, стрелки соседних месяцев.

    Стрелки показываются только внутри горизонта (`calendar_bounds`),
    поэтому уйти за допустимые даты нельзя.
    """
    start, end = _calendar_bounds(dict(field.rules))
    allowed = (start, end)
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    seam = calendar_view.selection_seam(app_today(), allowed)
    # Полная последовательность месяцев горизонта: последний месяц может не
    # попасть в сетку (кнопок не больше 12) и открывается стрелкой «▶️».
    months = [
        item
        for item in calendar_view.month_sequence(seam, end)
        if calendar_view.month_days(item, allowed)
    ]
    if key not in months:
        return await _stale(update, context)
    days = calendar_view.month_days(key, allowed)
    if not days:
        return await _stale(update, context)
    rows = _day_rows(days, prefix, per_row=7, label=calendar_view.format_month_day)
    position = months.index(key)
    # U3a №13: стрелки соседних месяцев — общий шаблон пагинации.
    rows += keyboard_layouts.nav_row(
        (
            keyboard_layouts.button(
                "◀️",
                f"aw_calmonth:{prefix}:{months[position - 1][0]}:"
                f"{months[position - 1][1]}",
            )
            if position > 0
            else None
        ),
        (
            keyboard_layouts.button(
                "▶️",
                f"aw_calmonth:{prefix}:{months[position + 1][0]}:"
                f"{months[position + 1][1]}",
            )
            if position + 1 < len(months)
            else None
        ),
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.wizard.mesyacy"), callback_data=f"aw_calmonths:{prefix}"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("wizard.render_calendar_range_days.k_bystrym_datam"),
                callback_data=f"aw_calquick:{prefix}",
            )
        ]
    )
    await _render(
        update,
        context,
        tr(
            "wizard.render_calendar_month.vyberite_den",
            format_month=calendar_view.format_month(key),
        ),
        InlineKeyboardMarkup(rows),
    )
    return AW_QUESTIONS


async def _save_calendar_day(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    day: date,
) -> int:
    start, end = _calendar_bounds(dict(field.rules))
    if day < start or day > end:
        return await _stale(
            update,
            context,
            tr("wizard.save_calendar_day.eta_data_vne_dopustimogo"),
        )
    raw = day.strftime("%d.%m.%Y")
    if source == "extra":
        extra = next(item for item in details.extras if item.brick_id == field.brick_id)
        return await _save_extra_value(
            update,
            context,
            details,
            extra,
            raw,
            notice=tr("wizard.save_calendar_day.data_sohranena", raw=raw),
        )
    return await _save_question_value(
        update,
        context,
        details,
        field,
        raw,
        index=index,
    )


def _ensure_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    stage: WizardStage,
):
    if details.stage == stage:
        return details
    with SessionLocal() as session:
        updated = set_announcement_stage(
            session,
            actor_telegram_user_id=_user_id(update),
            draft_id=details.draft_id,
            stage=stage,
            expected_revision=details.revision,
        )
    ui = _ui(context)
    _set_details(ui, updated)
    return updated


def _extras_available(details, telegram_user_id: int | None) -> bool:
    """
    Есть ли смысл показывать «🧩 Дополнительные поля» (Poster 2.5.2 §4).

    Единый предикат для ВСЕХ точек показа кнопки: обзор (карточка шаблона),
    экран ошибки обзора, компактный редактор доставленного анонса и панель
    экземпляра (`panel_keyboard` получает уже посчитанный флаг). Само
    правило живёт в `announcement_store.extras_available`; здесь — только
    сессия для проверки доступности определений.
    """
    with SessionLocal() as session:
        return extras_available(
            session,
            details,
            actor_telegram_user_id=telegram_user_id,
        )


def _draft_extras_available(draft_id: int) -> bool:
    """
    Тот же предикат по id черновика — для управления готовым анонсом.

    Панель и клавиатура экземпляра живут вне диалога (`anp_`), поэтому
    детали черновика читаются из БД. Сбой чтения закрывает кнопку: экран
    без доступных определений был бы пустым, а его отсутствие безопаснее
    ложного обещания «🧩 Дополнительные поля».
    """
    try:
        with SessionLocal() as session:
            return extras_available_for_draft(session, int(draft_id))
    except Exception as exc:  # noqa: BLE001 — пустой экран хуже скрытой кнопки
        logger.info("Доп. поля анонса #%s не проверены: %s", draft_id, exc)
        return False


def _extras_with_definitions(details, telegram_user_id: int):
    with SessionLocal() as session:
        payload = build_announcement_render_payload(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=details.draft_id,
        )
    return [
        (extra, payload.definitions_by_id[extra.brick_id]) for extra in details.extras
    ]


def _extras_entries(details, telegram_user_id: int):
    """
    Все доп. кирпичики экрана «🧱 Дополнительные поля» в порядке текста.

    Poster 2.4 (P2, требование IV): экран открывается сразу полным списком
    кирпичиков расширенной версии снимка — расширенные токены минус
    канонические (`extra_candidate_brick_ids`), включая незаполненные.
    Уже выбранные доп. поля помечены значением и получают корзину; их
    удаление убирает только значение/выбор, определение кирпичика остаётся.

    Устаревшие выбранные поля (снимок без `extended_content` или кирпичик
    вне расширенного текста) показываются после списка, чтобы значение можно
    было увидеть и удалить: ничего не дописывается в анонс молча.
    """
    snapshot = details.template_snapshot
    if snapshot is None:
        return [], False
    with SessionLocal() as session:
        payload = build_announcement_render_payload(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=details.draft_id,
        )
    candidates = list(extra_candidate_brick_ids(snapshot))
    chosen = {extra.brick_id: extra for extra in details.extras}
    order = list(candidates)
    order.extend(
        extra.brick_id for extra in details.extras if extra.brick_id not in candidates
    )
    entries: list[dict[str, Any]] = []
    for brick_id in order:
        definition = payload.definitions_by_id.get(brick_id)
        if definition is None:
            continue
        extra = chosen.get(brick_id)
        entries.append(
            {
                "brick_id": brick_id,
                "extra_id": extra.extra_id if extra is not None else None,
                "title": brick_title_display(definition.title, definition.code),
                "field": _extra_field_state(details, definition),
            }
        )
    return entries, bool(candidates)


async def _render_extras(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
    page: int | None = None,
    instance_id: int | None = None,
) -> int:
    """
    Экран «🧱 Дополнительные поля»: сразу список, внизу ровно «◀️ Назад».

    Требование IV: «➕ Добавить поле» и «🏠 В меню» здесь запрещены —
    добавление и правка идут одной кнопкой кирпичика, а пагинация
    появляется только если полей больше страницы.
    """
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    await _ensure_service_screen_below(update, context, details.delivered_message_ids)
    await _sync_live(update, context, details)
    # Признак «экран не смог построить список»: тогда штатная подпись
    # «нет расширенной версии» не показывается — причина другая.
    failed = False
    try:
        entries, has_extended = _extras_entries(details, _user_id(update))
    except AnnouncementError as exc:
        # Снимок старого черновика может ссылаться на удалённое определение
        # (P1: «адрес_места»). Экран не должен падать «внутренней ошибкой»:
        # показываем честный текст и безопасный возврат.
        logger.warning("Доп. поля анонса #%s недоступны: %s", details.draft_id, exc)
        entries, has_extended = [], False
        # Причина может быть любой доменной ошибкой (пропал снимок шаблона,
        # кирпичик стал недоступен), а не только «нет расширенного текста»:
        # поэтому здесь нейтральный текст причины, а не `no_extended`.
        notice = notice or tr("wizard.extras.unavailable")
        failed = True
    current_page = ui.get("extras_page") if page is None else page
    current_page = _clamped_page(
        max(0, int(current_page or 0)), len(entries), EXTRA_PAGE_SIZE
    )
    ui["extras_page"] = current_page
    editor_instance = _editor_instance_id(ui, details, instance_id)
    _nav_enter(ui, NAV_EXTRAS, page=current_page, instance_id=editor_instance)
    shown = entries[
        current_page * EXTRA_PAGE_SIZE : (current_page + 1) * EXTRA_PAGE_SIZE
    ]
    # H3: заголовок, вступление и пустое состояние — из каталога текстов.
    header = tr("wizard.extras.title")
    intro = tr("wizard.extras.intro")

    def build(limits: list[int]) -> str:
        # Заголовок и вступление — отдельные блоки каталога: пустое
        # вступление (владелец вправе оставить его пустым) не должно давать
        # ни пустой строки-разделителя, ни двойного перевода строки.
        blocks = [header, intro]
        if not has_extended and not failed:
            blocks.append(tr("wizard.extras.no_extended"))
        if not shown and has_extended:
            blocks.append(tr("wizard.extras.empty"))
        head = join_blocks(blocks)
        # Строки списка — плотный список: между ними один перевод строки,
        # а от текстовой обвязки их отделяет пустая строка.
        lines: list[str] = []
        for offset, entry in enumerate(shown, start=1):
            number = current_page * EXTRA_PAGE_SIZE + offset
            field = entry["field"]
            value = _full_value_text(field)
            if field.status == FIELD_STATUS_MISSING:
                value_text = tr("wizard.build.znachenie_zadano")
            elif field.status == FIELD_STATUS_SKIPPED:
                value_text = tr("wizard.status.skipped")
            else:
                value_text = value or tr("wizard.build.pusto")
            # H3 (требование D): строка списка — тоже показ значения.
            # Раньше огромное значение обрывалось «…» без пометки; теперь
            # у него бюджет на весь экран и честная пометка о сокращении.
            shown_value = current_value.clip_html(
                escape_html(value_text), limit=limits[offset - 1]
            )
            lines.append(
                tr_safe(
                    "wizard.build.znachenie",
                    number=number,
                    title=_h(entry["title"]),
                    shown=shown_value,
                )
            )
        if not lines:
            return head
        body = "\n".join(lines)
        return f"{head}\n\n{body}" if head else body

    screen_text = current_value.fit_screen(build, blocks=max(len(shown), 1))

    rows: list[list[InlineKeyboardButton]] = keyboard_layouts.list_rows(
        (
            InlineKeyboardButton(
                _short(
                    tr(
                        "buttons.extras.edit",
                        number=current_page * EXTRA_PAGE_SIZE + offset,
                        title=entry["title"],
                    ),
                    60,
                ),
                callback_data=(
                    f"aw_exedit:{details.draft_id}:"
                    f"{details.revision}:{entry['brick_id']}"
                ),
            ),
            (
                InlineKeyboardButton(
                    _short(
                        tr(
                            "buttons.extras.delete",
                            number=current_page * EXTRA_PAGE_SIZE + offset,
                        ),
                        8,
                    ),
                    callback_data=(
                        f"aw_exdel:{details.draft_id}:"
                        f"{details.revision}:{entry['extra_id']}"
                    ),
                )
                if entry["extra_id"] is not None
                else None
            ),
        )
        for offset, entry in enumerate(shown, start=1)
    )
    # Poster 2.5 §14: единый механизм сам решает, нужна ли навигация (одна
    # страница — ни строки, ни индикатора), поэтому условие по количеству
    # записей больше не нужно. Callback хранит НОМЕР страницы.
    nav = pagination.nav_keyboard(
        current_page,
        pagination.page_count(len(entries), EXTRA_PAGE_SIZE),
        f"aw_exbpage:{details.draft_id}:{details.revision}",
        page_size=EXTRA_PAGE_SIZE,
        as_offset=False,
    )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.nav.back"),
                callback_data=(f"aw_exback:{details.draft_id}:{details.revision}"),
            )
        ]
    )
    text = f"{notice}\n\n{screen_text}" if notice else screen_text
    await _render(update, context, text, InlineKeyboardMarkup(rows))
    return AW_EXTRAS


async def _enter_extras(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
    instance_id: int | None = None,
) -> int:
    """
    Открывает список доп. полей; `instance_id` — экземпляр редактора.

    `instance_id` передаётся только входом из управления доставленным
    анонсом: по нему путь редактора отделяется от истории конструктора и
    «◀️ Назад» возвращает в управление анонсом, а не в «Текстовые поля».
    """
    try:
        updated = _ensure_stage(update, context, details, WizardStage.EXTRAS)
    except AnnouncementError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    return await _render_extras(
        update, context, updated, notice=notice, instance_id=instance_id
    )


async def _back_from_extras(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
) -> int:
    """
    «◀️ Назад» со списка доп. полей — на экран, с которого пришли.

    Требование V (Poster 2.4 P2b): настоящий путь важнее индекса. Экран
    открывают из разных мест («✏️ Изменить» готового анонса, обзор, вопросы,
    шаг фото), поэтому возврат берётся из навигационного стека. Пустой стек
    (рестарт службы, старый экран) не выдумывает переход: показывается
    безопасный экран текущего этапа черновика.
    """
    previous = _nav_pop_from(_ui(context), NAV_EXTRAS)
    rendered = await _nav_render(update, context, details, previous)
    if rendered is not None:
        return rendered
    if details.publication_sent:
        # Путь потерян (рестарт службы, старый экран) у доставленного анонса:
        # безопасный экран этапа — само управление анонсом, а не список
        # текстовых полей. Список — отдельный раздел редактора, и возврат из
        # доп. полей в него вводил бы в заблуждение.
        await _show_announcement_controls(
            context, ctx.chat(update).id, details, instance=_instance_for(details)
        )
        return AW_PREVIEW
    # Живой черновик: предыдущий шаг мастера (список открывают из вопросов
    # или шага фото), а не главное меню.
    return await _render_template_screen(update, context, details)


EXTRA_DESCRIPTION_LIMIT = 800


def _full_value_text(field: AnnouncementFieldState) -> str:
    """
    Полный видимый текст значения — без обрезки `format_value`.

    `field_value_text`/`format_value` (U1b) режут показ до
    `DISPLAY_TEXT_LIMIT` c молчаливым «…». Для строки списка дополнительных
    полей это недопустимо (H3, требование D): длинное значение должно
    получить бюджет экрана и явную пометку, что полное сохранено.
    """
    raw = field.raw_value
    if isinstance(raw, (list, tuple)):
        text = ", ".join(str(item) for item in raw)
    else:
        text = "" if raw is None else str(raw)
    suffix = field.rules.get("suffix")
    if (
        field.value_type == BRICK_TYPE_NUMBER
        and text
        and isinstance(suffix, str)
        and suffix
    ):
        return f"{text} {suffix}"
    return text


def _stored_value_with_note(field: AnnouncementFieldState) -> str:
    """
    Сохранённое значение для экрана и честная пометка о сокращении.

    `stored_value_html` (U1b-fix) режет длинное значение до
    `DISPLAY_TEXT_LIMIT` и обрывает его многоточием: так экран вопроса
    укладывается в лимит Telegram. Но молчаливая потеря запрещена (H3,
    требование D), поэтому рядом встаёт та же пометка общего компонента
    «Текущее значение», что и на экранах шаблонов и мест: показано начало,
    полное значение сохранено.
    """
    shown = wizard_questions.stored_value_html(field)
    source = field.raw_html
    if not (isinstance(source, str) and source.strip()):
        raw = field.raw_value
        if isinstance(raw, (list, tuple)):
            raw = ", ".join(str(item) for item in raw)
        source = _h("" if raw is None else raw)
    full_visible = wizard_questions.visible_html_text(source)
    shown_visible = wizard_questions.visible_html_text(shown)
    if len(full_visible) > len(shown_visible):
        return f"{shown}\n{current_value.TRUNCATED_NOTE}"
    return shown


def _extra_value_text(
    extra,
    field: AnnouncementFieldState,
    *,
    description_limit: int = EXTRA_DESCRIPTION_LIMIT,
) -> str:
    lines = [
        # Заголовок кирпичика задаёт человеком и уходит в parse_mode=HTML
        # экран — экранируем (аудит: неэкранированный HTML ломал экран).
        f"➕ {_h(brick_title_display(field.title, field.code))}",
        tr("wizard.extra_value.position_from_template"),
    ]
    description = wizard_questions.description_html(field.description)
    if description:
        # №27: описание — моноширинно и уже экранировано внутри `<code>`.
        # Описание тоже вводит человек: у огромного значения есть бюджет
        # экрана и явная пометка вместо молчаливого обрыва.
        lines.append(
            f"📝 {current_value.clip_html(description, limit=description_limit)}"
        )
    lines.append(_type_hint(field))
    if field.status == FIELD_STATUS_VALID:
        # U1b: сохранённое значение — с форматированием (см. stored_value_html).
        lines.append(
            tr_safe(
                "wizard.question.current_value",
                value=_stored_value_with_note(field),
            )
        )
    elif field.status == FIELD_STATUS_SKIPPED:
        lines.append(tr("wizard.question.skipped"))
    else:
        lines.append(tr("wizard.question.missing"))
    # Пустая строка каталога (`wizard.question.missing` может быть пустой по
    # решению владельца) не оставляет пустого блока с лишним отступом.
    return join_blocks(lines)


async def _render_extra_value_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    extra,
    *,
    editing: bool = True,
    error: str | None = None,
    notice: str | None = None,
    instance_id: int | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    definition = _extra_definition(details, extra, _user_id(update))
    field = _extra_field_state(details, definition)
    ui["active_brick_id"] = field.brick_id
    ui["active_source"] = "extra"
    ui["editing"] = editing
    editor_instance = _editor_instance_id(ui, details, instance_id)
    _nav_enter(
        ui, NAV_EXTRA_VALUE, brick_id=field.brick_id, instance_id=editor_instance
    )

    def build(limits: list[int]) -> str:
        lines = [_extra_value_text(extra, field, description_limit=limits[0])]
        if error:
            lines.append(f"⚠️ {_h(error)}")
        if notice:
            lines.append(_h(notice))
        return "\n\n".join(lines)

    # Бюджет значения считается от обвязки экрана (заголовок кирпичика задаёт
    # человек, описание и подсказка типа тоже занимают место): иначе
    # сохранённое значение на 5000 символов выносило вопрос за лимит Telegram.
    text = current_value.fit_screen(build, blocks=1)
    rows: list[list[InlineKeyboardButton]] = []
    if field.value_type == BRICK_TYPE_CHOICE:
        selected = set(_selected_choices(field))
        for option_index, option in enumerate(_choice_options(field)):
            marker = "✅ " if option in selected else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        _short(f"{marker}{option}", 60),
                        callback_data=(
                            f"aw_choice:{details.draft_id}:{details.revision}:"
                            f"{field.brick_id}:{option_index}"
                        ),
                    )
                ]
            )
        if field.rules.get("multiple") and _choice_options(field):
            rows.append(
                [
                    InlineKeyboardButton(
                        tr("buttons.wizard.gotovo"),
                        callback_data=(
                            f"aw_choice_done:{details.draft_id}:"
                            f"{details.revision}:{field.brick_id}"
                        ),
                    )
                ]
            )
    elif field.value_type == BRICK_TYPE_DATE:
        # U2a: компактный календарь показывается прямо на экране значения
        # дополнительного кирпичика — отдельного входа «📅 Календарь» нет.
        rows.extend(
            wizard_questions.calendar_shortcut_rows(
                field, f"{details.draft_id}:{details.revision}:{field.brick_id}"
            )
        )
    skip_button = InlineKeyboardButton(
        tr("buttons.wizard.skip"),
        callback_data=(
            f"aw_exskip:{details.draft_id}:{details.revision}:{field.brick_id}"
        ),
    )
    back_button = InlineKeyboardButton(
        tr("buttons.nav.back"),
        callback_data=(f"aw_exbackval:{details.draft_id}:{details.revision}"),
    )
    # Требование VI: на экране значения — та же пара «◀️ Назад | ⏭ Пропустить».
    rows += keyboard_layouts.pair(back_button, skip_button)
    await _render(update, context, text, InlineKeyboardMarkup(rows))
    return AW_EXTRAS


async def _save_extra_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    extra,
    value: object,
    *,
    drop_input: bool = False,
    stay_prompt: bool = False,
    notice: str | None = None,
) -> int:
    try:
        with SessionLocal() as session:
            updated = save_announcement_value(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                brick_id=extra.brick_id,
                value=value,
                expected_revision=details.revision,
                today=app_today(),
            )
    except AnnouncementError as exc:
        message = str(exc)
        if drop_input:
            screen.track_failed_input(
                ctx.data(context),
                f"aw:{extra.brick_id}",
                update.effective_message,
            )
        current_extra = next(
            (item for item in details.extras if item.extra_id == extra.extra_id),
            extra,
        )
        return await _render_extra_value_prompt(
            update,
            context,
            details,
            current_extra,
            error=message,
        )
    ui = _ui(context)
    _set_details(ui, updated)
    if drop_input and not updated.publication_sent:
        # Черновик ещё не доставлен: значение в БД, ввод убирать безопасно.
        await _drop_accepted_input(update, context, f"aw:{extra.brick_id}")
    current_extra = next(
        (item for item in updated.extras if item.extra_id == extra.extra_id),
        None,
    )
    if current_extra is None:
        return await _render_extras(
            update,
            context,
            updated,
            notice=tr("wizard.save_extra_value.kirpichik_bolshe_nedostupen"),
        )
    if stay_prompt:
        _clear_active(ui)
        return await _render_extra_value_prompt(
            update, context, updated, current_extra, notice=notice
        )
    _clear_active(ui)
    if updated.publication_sent:
        # H3: правка доставленного анонса применяется сразу — кнопки
        # «Применить изменения» больше нет.
        applied = await _apply_immediately(update, context, updated)
        fresh = _reload_details(update, updated)
        if applied is None:
            # Доставка не подтверждена (лимит/ошибка): причину уже показал
            # `_finish_and_deliver`, а ввод остаётся в чате (2.5.3 §7).
            refusal = _length_check(update, fresh)
            notice_failed = (
                _length_refusal_text(refusal)
                if refusal is not None
                else APPLY_FAILED_TEXT
            )
            return await _render_extras(update, context, fresh, notice=notice_failed)
        if drop_input:
            # Ввод удаляется только после ПОДТВЕРЖДЁННОЙ доставки.
            await _drop_accepted_input(update, context, f"aw:{extra.brick_id}")
        updated = fresh
        notice = notice or tr(
            "wizard.save_extra_value.znachenie_sohraneno_anons_obnovlen"
        )
    return await _render_extras(update, context, updated, notice=notice)


async def _add_extra(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    brick_id: int,
) -> int:
    try:
        with SessionLocal() as session:
            updated = add_announcement_extra(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                brick_id=brick_id,
                expected_revision=details.revision,
            )
    except AnnouncementError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    extra = next((item for item in updated.extras if item.brick_id == brick_id), None)
    if extra is None:
        return await _render_extras(
            update, context, updated, notice=tr("wizard.add_extra.kirpichik_dobavlen")
        )
    ui = _ui(context)
    _set_details(ui, updated)
    return await _render_extra_value_prompt(
        update,
        context,
        updated,
        extra,
        notice=tr("wizard.add_extra.kirpichik_dobavlen_zadayte"),
    )


def _preview_template_name(details) -> str:
    snapshot = details.template_snapshot
    if snapshot is None:
        return tr("wizard.preview_template_name.ne_vybran")
    if snapshot.source_name:
        return snapshot.source_name
    if snapshot.source_kind == "standard":
        return tr("wizard.selected_template.standartnyy_shablon_mesta")
    return tr("wizard.selected_template.lichnyy_shablon")


def _preview_summary_lines(
    preview: AnnouncementPreview, telegram_user_id: int
) -> list[str]:
    details = preview.draft
    filled: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    for field in preview.fields:
        if field.status == FIELD_STATUS_VALID:
            value = _field_value_text(field)
            filled.append(
                f"• {_h(brick_title_display(field.title, field.code))}: "
                f"{_h(_short(value, FIELD_DISPLAY_LIMIT))}"
            )
        elif field.status == FIELD_STATUS_KNOWN:
            value = _field_value_text(field)
            filled.append(
                tr_safe(
                    "wizard.preview_summary_lines.iz_spravochnika",
                    title=_h(brick_title_display(field.title, field.code)),
                    short=_h(_short(value, FIELD_DISPLAY_LIMIT)),
                )
            )
        elif field.status == FIELD_STATUS_SKIPPED:
            skipped.append(f"• {_h(brick_title_display(field.title, field.code))}")
        elif field.status == FIELD_STATUS_MISSING:
            missing.append(f"• {_h(brick_title_display(field.title, field.code))}")
    lines = [
        tr("buttons.wizard.obzor_anonsa"),
        "",
        *_place_lines(details),
        tr_safe(
            "wizard.preview_summary_lines.gorod",
            place_city=_h(details.place_city) if details.place_city else "—",
        ),
        tr_safe(
            "wizard.preview_summary_lines.adres",
            place_address=(
                _h(details.place_address)
                if details.place_address
                else tr("wizard.place_card.ne_ukazan")
            ),
        ),
        tr_safe(
            "wizard.preview_summary_lines.ssylka",
            place_url=(
                _h(details.place_url)
                if details.place_url
                else tr("wizard.preview_summary_lines.ne_ukazana")
            ),
        ),
        tr_safe(
            "wizard.preview_summary_lines.shablon",
            preview_template_name=_h(_preview_template_name(details)),
        ),
        tr(
            "wizard.preview_summary_lines.fotografii_otdelnye_vlozheniya",
            count_photos=len(details.photos),
        ),
    ]
    if details.template_snapshot is not None:
        lines.append(
            tr(
                "wizard.preview_summary_lines.versiya_snimka",
                source_template_version=details.template_snapshot.source_template_version,
            )
        )
    lines.append("")
    if filled:
        lines.append(tr("wizard.preview_summary_lines.zapolnennye_znacheniya"))
        lines.extend(filled[:MAX_PREVIEW_FIELDS])
        if len(filled) > MAX_PREVIEW_FIELDS:
            lines.append(
                tr(
                    "wizard.preview_summary_lines.i_znacheniy",
                    value=len(filled) - MAX_PREVIEW_FIELDS,
                )
            )
    else:
        lines.append(tr("wizard.preview_summary_lines.zapolnennyh_znacheniy_poka_net"))
    if skipped:
        lines.extend(["", tr("wizard.preview_summary_lines.propuschennye_polya")])
        lines.extend(skipped[:MAX_PREVIEW_FIELDS])
        if len(skipped) > MAX_PREVIEW_FIELDS:
            lines.append(
                tr(
                    "wizard.preview_summary_lines.i_poley",
                    value=len(skipped) - MAX_PREVIEW_FIELDS,
                )
            )
    if missing:
        lines.extend(["", tr("wizard.preview_summary_lines.ne_zapolneny")])
        lines.extend(missing[:MAX_PREVIEW_FIELDS])
        if len(missing) > MAX_PREVIEW_FIELDS:
            lines.append(
                tr(
                    "wizard.preview_summary_lines.i_poley",
                    value=len(missing) - MAX_PREVIEW_FIELDS,
                )
            )

    extras = _extras_with_definitions(details, telegram_user_id)
    if extras:
        lines.extend(["", tr("wizard.preview_summary_lines.dopolnitelnye_kirpichiki")])
        for extra, definition in extras[:MAX_PREVIEW_EXTRAS]:
            field = _extra_field_state(details, definition)
            if field.status == FIELD_STATUS_VALID:
                value = _short(_field_value_text(field), FIELD_DISPLAY_LIMIT)
                value_text = tr_safe(
                    "wizard.preview_summary_lines.znachenie", h=_h(value)
                )
            elif field.status == FIELD_STATUS_SKIPPED:
                value_text = tr("wizard.preview_summary_lines.znachenie_propuscheno")
            else:
                value_text = tr("wizard.build.znachenie_zadano")
            lines.append(
                f"• {_h(brick_title_display(extra.title, extra.code))} — {value_text}"
            )
        if len(extras) > MAX_PREVIEW_EXTRAS:
            lines.append(
                tr(
                    "wizard.preview_summary_lines.i",
                    value=len(extras) - MAX_PREVIEW_EXTRAS,
                )
            )
    return lines


def _preview_block(preview: AnnouncementPreview) -> list[str]:
    if preview.ready:
        lines = [
            "",
            tr("wizard.ready.collecting"),
        ]
        if preview.warnings:
            lines.append(_h(preview.warnings[0]))
        lines.extend(["", tr("wizard.preview_block.predprosmotr")])
        visible = preview.visible_text
        if len(visible) > PREVIEW_VISIBLE_LIMIT:
            shortened = visible[:PREVIEW_VISIBLE_LIMIT].rstrip() + "…"
            lines.append(_h(shortened))
            lines.append(tr("wizard.preview_block.predprosmotr_sokraschen_polnye"))
        else:
            lines.append(preview.html)
        return lines

    lines = ["", tr("wizard.preview_block.anons_poka_gotov")]
    for issue in preview.issues:
        lines.append(f"• {_h(issue)}")
    if preview.missing:
        titles = ", ".join(
            _h(brick_title_display(question.title, question.code))
            for question in preview.missing[:5]
        )
        suffix = "…" if len(preview.missing) > 5 else ""
        lines.append(
            tr(
                "wizard.preview_block.chto_zapolnit_mozhno_takzhe",
                titles=titles,
                suffix=suffix,
            )
        )
    if preview.visible_text:
        lines.extend(["", tr("wizard.preview_block.chernovoy_fragment_anons_gotov")])
        fragment = preview.visible_text[:PREVIEW_VISIBLE_LIMIT].rstrip()
        if len(preview.visible_text) > PREVIEW_VISIBLE_LIMIT:
            fragment += "…"
        lines.append(_h(fragment))
    return lines


def _preview_keyboard(
    details,
    *,
    ready: bool = False,
    saved: bool = False,
    extras_available: bool = True,
) -> InlineKeyboardMarkup:
    draft_id = details.draft_id
    revision = details.revision
    delivered = bool(details.publication_sent)
    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(
                (
                    tr("buttons.wizard.sohranit_vyyti")
                    if delivered
                    else (
                        tr("buttons.wizard.chernovik_sohranen")
                        if saved
                        else tr("buttons.wizard.sohranit_chernovik")
                    )
                ),
                callback_data=f"aw_save:{draft_id}:{revision}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.wizard.izmenit_polya"),
                callback_data=f"aw_qmanage:{draft_id}:{revision}",
            )
        ],
    )
    # Poster 2.5.2 §4: кнопка доп. полей условна и решается тем же предикатом,
    # что и в компактном редакторе/панели — иначе экран добавления пуст.
    if extras_available:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.dopolnitelnye_kirpichiki"),
                    callback_data=f"aw_extras:{draft_id}:{revision}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.wizard.fotografii", count_photos=len(details.photos)),
                callback_data=f"aw_photos:{draft_id}:{revision}:0",
            )
        ]
    )
    if details.photos:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.predprosmotr_foto"),
                    callback_data=f"aw_media_preview:{draft_id}:{revision}",
                )
            ]
        )
    # H3: у доставленного анонса здесь нет кнопки «Применить изменения» —
    # правки применяются сразу, а сам обзор недоступен из интерфейса
    # (он остаётся внутренним маршрутом для проверки текста).
    if ready and not details.publication_sent:
        # Обычный путь доставляет анонс автоматически после последнего ответа.
        # Эта кнопка нужна только черновикам, остановленным на обзоре
        # (сохранённый черновик, рестарт, возврат после полного отказа
        # отправки): без неё их нельзя было бы завершить.
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.otpravit_etot_chat"),
                    callback_data=f"aw_send:{draft_id}:{revision}",
                )
            ]
        )
    if delivered:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.k_upravleniyu"),
                    callback_data=f"aw_panel:{draft_id}:{revision}",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.obnovit"),
                    callback_data=f"aw_preview:{draft_id}:{revision}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _render_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
    saved: bool = False,
    preview_only: bool = False,
) -> int:
    """
    Обзор анонса; для уже доставленного — экран управления без отправки.

    Просмотр и повторное открытие черновика никогда не отправляют сообщения
    сами: новую версию создаёт либо последний ответ в мастере, либо явное
    «Применить изменения». `preview_only` показывает сам текст обзора (для
    проверки длинного текста и вложений) и не меняет данные.
    """
    ui = _ui(context)
    _clear_active(ui)
    if details.publication_status not in {None, PUBLICATION_STATUS_SENT}:
        return await _render_publish_status(update, context, details, notice=notice)
    if details.publication_sent and not preview_only:
        pending = _has_pending_changes(update, details)
        if saved:
            # H3: кнопки «Применить изменения» больше нет. Здесь статус уже
            # `sent`, а содержимое отличается от доставленного — значит,
            # немедленное применение сорвалось. Обещать несуществующую
            # кнопку нельзя: отправляем автора к повторной правке (она
            # применится сразу) или к экрану повтора отправки.
            notice = (
                APPLY_FAILED_TEXT
                if pending
                else tr("wizard.render_preview.chernovik_sohranen_otpravlennyy")
            )
        # §18: промежуточного экрана «✅ Анонс готов» нет — управление
        # живёт у самого анонса (кнопки `anp_`), а правка начинается со
        # списка текстовых полей (§5, 2.5.3). Экран не создаётся: список
        # заменяет клавиатуру анонса. Пояснение уходит коротким ответом.
        if notice:
            await _answer(update, notice)
        return await _render_field_list(update, context, details)
    updated = details
    _nav_enter(ui, NAV_PREVIEW)
    try:
        if not details.publication_sent:
            # Обзор доставленного анонса не возвращает его на этап правки:
            # иначе сохранённый черновик терял бы статус готового.
            updated = _ensure_stage(update, context, details, WizardStage.PREVIEW)
        with SessionLocal() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=updated.draft_id,
                today=app_today(),
            )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError) as exc:
        return await _render_preview_error(update, context, updated, str(exc))
    if preview.draft.publication_status not in {None, PUBLICATION_STATUS_SENT}:
        return await _render_publish_status(
            update, context, preview.draft, notice=notice
        )
    _set_details(ui, preview.draft)
    lines = _preview_summary_lines(preview, _user_id(update))
    lines.extend(_preview_block(preview))
    # 2.5.3 §7: превышение лимита — состояние черновика, а не флаг в
    # `user_data`. Оно пересчитывается из БД, поэтому короткая пометка видна
    # и после рестарта службы: «не отправлено: текст не помещается».
    length = check_announcement_length(
        preview.html, has_photos=bool(preview.draft.photos)
    )
    if length.over:
        lines.insert(2, _length_refusal_text(length))
    # S3 п.11: шутка пустого анонса — только в нижнем служебном экране,
    # никогда внутри самого текста анонса выше.
    if not preview.visible_text and not preview.draft.photos:
        humor = ui_humor.get_humor_line(announcement_is_empty=True)
        if humor:
            # Тот же экран, что и шаг фото: текст уходит с parse_mode=HTML,
            # поэтому фраза каталога экранируется как обычный текст.
            lines.extend(["", _h(humor)])
    if notice:
        lines.insert(1, notice)
    await _render(
        update,
        context,
        "\n".join(lines),
        _preview_keyboard(
            preview.draft,
            ready=preview.ready,
            saved=saved,
            extras_available=_extras_available(preview.draft, _user_id(update)),
        ),
    )
    return AW_PREVIEW


async def _render_preview_error(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    reason: str,
) -> int:
    ui = _ui(context)
    if details is not None:
        _set_details(ui, details)
    _clear_active(ui)
    text = tr_safe(
        "wizard.render_preview_error.obzor_anonsa_udalos_sobrat", reason=_h(reason)
    )
    rows = []
    if details is not None:
        retry_action = "aw_apply" if details.publication_sent else "aw_preview"
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.obnovit"),
                    callback_data=(
                        f"{retry_action}:{details.draft_id}:{details.revision}"
                    ),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.izmenit_polya"),
                    callback_data=(f"aw_qmanage:{details.draft_id}:{details.revision}"),
                )
            ]
        )
        # Poster 2.5.2 §4: тот же предикат, что и на экране обзора. Ошибка
        # сборки не делает экран добавления непустым.
        if _extras_available(details, _user_id(update)):
            rows.append(
                [
                    InlineKeyboardButton(
                        tr("buttons.wizard.dopolnitelnye_kirpichiki"),
                        callback_data=(
                            f"aw_extras:{details.draft_id}:{details.revision}"
                        ),
                    )
                ]
            )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
            )
        ]
    )
    await _render(update, context, text, InlineKeyboardMarkup(rows))
    return AW_PREVIEW


def _length_refusal_text(check) -> str:
    """
    КОРОТКАЯ ошибка превышения лимита (2.5.3 §7, 2.5.4 §3.3).

    Совет зависит от фактического вида сообщения: у подписи к фото («уберите
    фото») он уместен, а у анонса БЕЗ фотографий предлагать убрать нечего —
    там только «сократите текст». Посторонние действия не предлагаются.
    """
    key = (
        "wizard.length_refusal.not_pomeschaetsya"
        if getattr(check, "has_photos", False)
        else "wizard.length_refusal.not_pomeschaetsya_bez_foto"
    )
    return tr(key, length=check.length, limit=check.limit)


def _length_check(update: Update, details, *, user_id: int | None = None):
    """
    Проверяет лимит готового текста по данным БД (`None` — не измерить).

    Состояние «текст не помещается» вычисляется заново из черновика, а не
    хранится флагом в `user_data`: после рестарта службы оно остаётся верным.
    """
    try:
        with SessionLocal() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=(
                    user_id if user_id is not None else _user_id(update)
                ),
                draft_id=details.draft_id,
                today=app_today(),
            )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError):
        return None
    check = check_announcement_length(
        preview.html, has_photos=bool(preview.draft.photos)
    )
    return check if check.over else None


async def _render_length_refusal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    preview,
    check,
) -> int:
    """
    Отказ от публикации при превышении лимита: короткая ошибка (2.5.3 §7).

    Данные и фото НЕ меняются и не удаляются: значения кирпичиков сохранены
    с CAS, `file_id` лежат в черновике, сообщение пользователя остаётся в
    чате. Большого экрана восстановления, автосокращения и разрезания поста
    нет: причина — одна строка, а вернуться к правке можно обычными
    кнопками (список полей/шаг фото).
    """
    details = preview.draft
    ui = _ui(context)
    _set_details(ui, details)
    text = _length_refusal_text(check)
    if getattr(update, "callback_query", None) is not None:
        # Нажатие кнопки: короткий ответ, экран в чате не появляется.
        await _answer(update, text, alert=True)
        return AW_PREVIEW
    if details.publication_sent:
        # Правка доставленного анонса: прежняя версия осталась рабочей.
        # Экран не рисуем — короткую причину покажет вызывающий слой
        # (вопрос правки/доп. поля), а ввод остаётся в чате.
        logger.info("Анонс #%s не обновлён: %s", details.draft_id, _short(text, 200))
        return AW_PREVIEW
    _clear_active(ui)
    return await wizard_photos.render_photo_step(
        update,
        context,
        details,
        notice=text,
        announcement_is_empty=_announcement_is_empty(update, details),
    )


def _reload_details(update: Update, details):
    """Перечитывает черновик после действий, менявших revision."""
    try:
        return _session_details(_user_id(update), details.draft_id)
    except AnnouncementError:
        return details


async def _apply_immediately(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
):
    """
    H3 (требование B): сохранить правку готового анонса сразу.

    Кнопки «Применить изменения» больше нет, поэтому каждое сохранённое
    значение заменяет доставленную версию без промежуточного действия.
    Возвращает свежие детали, если новая версия действительно ушла, и
    `None`, когда применять нечего (анонс ещё не доставлен) или применить
    не удалось — тогда `_finish_and_deliver` уже показал причину, и
    вызывающий слой не должен её перекрывать.
    """
    if not details.publication_sent or _edit_blocked(details):
        return None
    await _finish_and_deliver(update, context, details)
    try:
        fresh = _session_details(_user_id(update), details.draft_id)
    except AnnouncementError:
        return None
    if not fresh.publication_sent:
        return None
    if _has_pending_changes(update, fresh):
        return None
    return fresh


async def _resume_current(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    draft_id = ui.get("draft_id")
    if not draft_id:
        return await _render_home(update, context, notice=notice)
    try:
        details = _session_details(_user_id(update), int(draft_id))
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        ctx.data(context)["announcement_ui"] = _fresh_ui()
        return await _render_home(
            update,
            context,
            notice=tr("wizard.resume_current.chernovik_nayden_bolshe_nedostupen"),
        )
    _set_details(ui, details)
    stage = details.stage
    if stage == WizardStage.PLACE:
        ui["place"].update(
            {
                "phase": "confirm",
                "place_id": details.place_id,
                "name_id": details.place_name_id,
                "return_phase": "hub",
            }
        )
        return await _render_place_card(update, context, notice=notice)
    if stage == WizardStage.TEMPLATE:
        return await _render_template_screen(update, context, details, notice=notice)
    if stage == WizardStage.QUESTIONS:
        return await _enter_questions(update, context, details, notice=notice)
    if stage == WizardStage.EXTRAS:
        return await _render_extras(update, context, details, notice=notice)
    if details.publication_status not in {None, PUBLICATION_STATUS_SENT}:
        return await _render_publish_status(update, context, details, notice=notice)
    return await _render_preview(update, context, details, notice=notice)


def _photo_hint_button(key: str) -> InlineKeyboardButton:
    """
    Информационная кнопка фото-режима: подпись без действия.

    Отдельного обработчика нет: callback `noop:0:0` закрывает спиннер и
    ничего не меняет (§6, 2.5.3). Поле `disabled` PTB 22.8 не поддерживает,
    поэтому кнопка выглядит обычной, а действие остаётся безвредным.
    """
    return InlineKeyboardButton(tr(key), callback_data="noop:0:0")


def _photo_mode_keyboard(
    details,
    instance,
    page: int,
    *,
    clear_confirm: bool = False,
) -> InlineKeyboardMarkup:
    """
    Клавиатура фото-режима доставленного анонса (§6, 2.5.3).

    Кнопки «📥 Добавить фото» больше нет: снимки принимаются обычным
    сообщением, поэтому фото-режим только показывает состояние коллекции и
    даёт удалить лишнее. Раскладка зависит от числа фото:

    - 0 — информационная «📷 Пришлите фото» и «◀️ Назад»;
    - 1 — «📷 Можно прислать ещё» и «🗑 Удалить фото» в одной строке;
    - 2–10 — удаление конкретных снимков (по два в строке, 8 на страницу),
      «🗑 Удалить все фото» с подтверждением и «◀️ Назад».
    """
    photos = list(details.photos)
    total = len(photos)
    editable = not _edit_blocked(details)
    # «Назад» возвращает управление анонсу тем же протоколом, что и список
    # текстовых полей: кнопка работает по id экземпляра и после рестарта,
    # когда состояния диалога уже нет. Ветки «без экземпляра» нет: клавиатуру
    # строит только `_render_photo_mode`, а он вызывается лишь при найденном
    # активном экземпляре (§6, 2.5.3 MINOR-2).
    back = announcement_controls.field_callback(
        announcement_controls.FIELD_ACTION_BACK, instance.id
    )

    if clear_confirm:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        tr("buttons.wizard.da_udalit_vse"),
                        callback_data=(
                            f"aw_photo_clear_yes:{details.draft_id}:"
                            f"{details.revision}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        tr("buttons.admin.otmena_2"),
                        callback_data=(
                            f"aw_photos:{details.draft_id}:"
                            f"{details.revision}:{page}"
                        ),
                    )
                ],
            ]
        )

    rows: list[list[InlineKeyboardButton]] = []
    if total == 0:
        rows.append([_photo_hint_button("wizard.photos.mode_empty")])
    elif total == 1:
        row = [_photo_hint_button("wizard.photos.mode_more")]
        if editable:
            row.append(
                InlineKeyboardButton(
                    tr("buttons.wizard.udalit_edinstvennoe_foto"),
                    callback_data=(
                        f"aw_photo_del:{details.draft_id}:{details.revision}:0"
                    ),
                )
            )
        rows.append(row)
    else:
        start = page * PHOTO_PAGE_SIZE
        current: list[InlineKeyboardButton] = []
        for offset in range(min(PHOTO_PAGE_SIZE, max(0, total - start))):
            index = start + offset
            current.append(
                InlineKeyboardButton(
                    tr("buttons.wizard.udalit_foto", value=index + 1),
                    callback_data=(
                        f"aw_photo_del:{details.draft_id}:"
                        f"{details.revision}:{index}"
                    ),
                )
            )
            if len(current) == 2:
                rows.append(current)
                current = []
        if current:
            rows.append(current)
        pages = max(1, (total + PHOTO_PAGE_SIZE - 1) // PHOTO_PAGE_SIZE)
        nav = pagination.nav_buttons(
            page,
            pages,
            f"aw_photos:{details.draft_id}:{details.revision}",
            as_offset=False,
        )
        if nav:
            rows.append(
                [InlineKeyboardButton(label, callback_data=data) for label, data in nav]
            )
        if editable:
            rows.append(
                [
                    InlineKeyboardButton(
                        tr("buttons.wizard.udalit_vse_foto"),
                        callback_data=(
                            f"aw_photo_clear:{details.draft_id}:{details.revision}"
                        ),
                    )
                ]
            )
    rows.append([InlineKeyboardButton(tr("buttons.nav.back"), callback_data=back)])
    return InlineKeyboardMarkup(rows)


async def _photo_mode_notice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    notice: str,
) -> None:
    """
    Короткое пояснение фото-режима без служебного экрана.

    Кнопки пришли из callback — пояснение уходит ответом на нажатие. После
    присланного снимка callback нет, и для 0–1 фото отдельное сообщение
    создало бы второй интерфейс рядом с анонсом: пояснение пишется в журнал,
    а факт принятия виден по обновлённому анонсу.
    """
    if getattr(update, "callback_query", None) is not None:
        await _answer(update, _short(notice, 200))
        return
    logger.info("Фото-режим анонса: %s", _short(notice, 200))


async def _render_photo_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    instance_id: int | None = None,
    notice: str | None = None,
    page: int = 0,
    clear_confirm: bool = False,
    answered: bool = False,
) -> int:
    """
    Фото-режим доставленного анонса: правка ЕГО управления, не новый экран.

    У 0–1 фото клавиатура ставится на само сообщение анонса
    (`editMessageReplyMarkup`): текст и подпись не меняются. У 2–10 фото
    правится текст единственной панели (`editMessageText`) — второй панели
    и второго места управления не появляется (§18/§22/§6 2.5.3).

    Активного экземпляра нет (анонс переиздан, запись удалена) — безопасный
    откат на экран шага: показывать фото-режим несуществующего управления
    нельзя.
    """
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    # Фото-режим продолжает принимать снимки обычным сообщением (H1/S4):
    # без этого после рестарта снимок ушёл бы в подсказку меню.
    ui["photo_step"] = True
    total = len(details.photos)
    pages = max(1, (total + PHOTO_PAGE_SIZE - 1) // PHOTO_PAGE_SIZE)
    page = max(0, min(int(page or 0), pages - 1))
    ui["photos_page"] = page
    _nav_enter(ui, NAV_PHOTOS, page=page)
    # §6: фото-режим — тоже приём ввода. Снимок продолжает анонс и после
    # рестарта службы, поэтому «ждём фото» фиксируется в `user_states`
    # вместе с ref экрана (отдельного экрана у режима нет).
    _remember_resume(_user_id(update), ctx.data(context), ctx.chat(update).id, ui)

    instance = _instance_for(details, instance_id)
    if instance is None:
        if notice and not answered:
            await _answer(update, notice)
        return await _render_photos(update, context, details, page=page)
    keyboard = _photo_mode_keyboard(
        details, instance, page, clear_confirm=clear_confirm
    )
    chat_id = ctx.chat(update).id
    if delivery.needs_control_panel(total):
        panel_id = instance.panel_message_id
        if panel_id is None:
            return AW_PREVIEW
        text = (
            tr("wizard.render_photos.udalit_vse_fotografii_etogo")
            if clear_confirm
            else announcement_controls.panel_text()
        )
        if notice and not answered:
            # Панель — единственный текст управления: пояснение показывается
            # в ней, отдельного сообщения не появляется.
            text = f"{notice}\n\n{text}"
        try:
            await delivery.call_telegram(
                lambda: context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(panel_id),
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    link_preview_options=announcement_controls.PANEL_LINK_PREVIEW,
                )
            )
        except Exception as exc:  # noqa: BLE001 — панель уже в чате
            logger.info("Фото-режим не показан в панели #%s: %s", instance.id, exc)
        return AW_PREVIEW
    message_id = delivery.caption_message_id(instance.message_ids)
    if message_id is None:
        return AW_PREVIEW
    try:
        await delivery.call_telegram(
            lambda: context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=int(message_id),
                reply_markup=keyboard,
            )
        )
    except Exception as exc:  # noqa: BLE001 — клавиатура анонса не критична
        logger.info("Фото-режим не показан у анонса #%s: %s", instance.id, exc)
    if notice and not answered:
        await _photo_mode_notice(update, context, notice)
    return AW_PREVIEW


async def _show_photos(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    instance_id: int | None = None,
    notice: str | None = None,
    page: int | None = None,
    clear_confirm: bool = False,
    answered: bool = False,
) -> int:
    """
    Показывает фотографии текущего анонса в правильном месте.

    Доставленный анонс — фото-режим на его собственном управлении (§6,
    2.5.3). Черновик до отправки — прежний экран шага: у живого блока
    клавиатуры нет (§4), поэтому кнопки живут на служебном экране.
    """
    if details.publication_sent:
        return await _render_photo_mode(
            update,
            context,
            details,
            instance_id=instance_id,
            notice=notice,
            page=page or 0,
            clear_confirm=clear_confirm,
            answered=answered,
        )
    return await _render_photos(
        update,
        context,
        details,
        notice=notice,
        page=page,
        clear_confirm=clear_confirm,
    )


async def _render_photos(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
    page: int | None = None,
    clear_confirm: bool = False,
) -> int:
    """
    Экран шага фото ЧЕРНОВИКА: приём снимков, удаление лишних.

    «📥 Добавить фото» здесь тоже нет: фото принимаются обычным сообщением,
    о чём говорит текст экрана (§6, 2.5.3). Экран остаётся только до
    отправки — у доставленного анонса кнопки живут на нём самом.
    """
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    # H1: экран коллекции тоже принимает фото — режим ввода сохраняется,
    # и после рестарта снимок продолжает шаг, а не теряется.
    ui["photo_step"] = True
    photos = list(details.photos)
    total = len(photos)
    if page is None:
        page = int(ui.get("photos_page") or 0)
    pages = max(1, (total + PHOTO_PAGE_SIZE - 1) // PHOTO_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    ui["photos_page"] = page
    _nav_enter(ui, NAV_PHOTOS, page=page)

    lines = [
        tr("wizard.photos.title"),
        "",
        tr("wizard.render_photos.dobavleno_foto_neobyazatelny", total=total),
        tr("wizard.render_photos.mozhno_otpravit_odinochnye_snimki"),
    ]
    if notice:
        lines.insert(1, notice)
    if clear_confirm:
        lines.extend(
            [
                "",
                tr("wizard.render_photos.udalit_vse_fotografii_etogo"),
            ]
        )
        await _render(
            update,
            context,
            "\n".join(lines),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            tr("buttons.wizard.da_udalit_vse"),
                            callback_data=(
                                f"aw_photo_clear_yes:{details.draft_id}:"
                                f"{details.revision}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            tr("buttons.admin.otmena_2"),
                            callback_data=(
                                f"aw_photos:{details.draft_id}:"
                                f"{details.revision}:{page}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            tr("buttons.nav.back"),
                            callback_data=(
                                f"aw_photos:{details.draft_id}:"
                                f"{details.revision}:{page}"
                            ),
                        )
                    ],
                ]
            ),
        )
        return AW_PREVIEW

    if total:
        start = page * PHOTO_PAGE_SIZE
        lines.extend(
            ["", tr("wizard.render_photos.stranica", value=page + 1, pages=pages)]
        )
        for number, _file_id in enumerate(
            photos[start : start + PHOTO_PAGE_SIZE], start=start + 1
        ):
            lines.append(tr("wizard.render_photos.foto_dobavleno", number=number))
        lines.append("")
        lines.append(tr("wizard.render_photos.chtoby_zamenit_foto_udalite"))
    else:
        lines.extend(
            [
                "",
                tr("wizard.render_photos.fotografiy_poka_net_anons"),
            ]
        )
    if _edit_blocked(details):
        lines.extend(
            [
                "",
                tr("wizard.render_photos.otpravka_nachata_kollekciya"),
            ]
        )

    # §6 (2.5.3): кнопки «📥 Добавить фото» больше нет — снимки принимаются
    # обычным сообщением, о чём говорит текст экрана. Раскладка зависит от
    # числа фото и пересчитывается после каждого изменения. «🏠 В меню» здесь
    # нет: выход с экрана фотографий — «◀️ Назад» к обзору черновика.
    editable = not _edit_blocked(details)
    rows: list[list[InlineKeyboardButton]] = []
    if not editable:
        pass
    elif total == 1:
        # §36: одна фотография — удаление и подсказка в одной строке;
        # нумерованных удалений и «удалить все» быть не должно.
        rows.append(
            [
                _photo_hint_button("wizard.photos.mode_more"),
                InlineKeyboardButton(
                    tr("buttons.wizard.udalit_edinstvennoe_foto"),
                    callback_data=(
                        f"aw_photo_del:{details.draft_id}:{details.revision}:0"
                    ),
                ),
            ]
        )
    elif total > 1:
        # §37: две и более — удаление конкретных фото по две в строке и
        # отдельное «удалить все» с подтверждением.
        start = page * PHOTO_PAGE_SIZE
        current: list[InlineKeyboardButton] = []
        for offset in range(min(PHOTO_PAGE_SIZE, max(0, total - start))):
            index = start + offset
            current.append(
                InlineKeyboardButton(
                    tr("buttons.wizard.udalit_foto", value=index + 1),
                    callback_data=(
                        f"aw_photo_del:{details.draft_id}:"
                        f"{details.revision}:{index}"
                    ),
                )
            )
            if len(current) == 2:
                rows.append(current)
                current = []
        if current:
            rows.append(current)
    if total and editable and not details.publication_sent:
        # H3: у доставленного анонса вложения уже стоят в чате выше —
        # отдельный предпросмотр дублировал бы готовый пост.
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.pokazat_vlozheniya"),
                    callback_data=(
                        f"aw_photo_show:{details.draft_id}:{details.revision}"
                    ),
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️",
                callback_data=(
                    f"aw_photos:{details.draft_id}:{details.revision}:{page - 1}"
                ),
            )
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(
                "▶️",
                callback_data=(
                    f"aw_photos:{details.draft_id}:{details.revision}:{page + 1}"
                ),
            )
        )
    if nav:
        rows.append(nav)
    if total > 1 and editable:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.udalit_vse_foto"),
                    callback_data=(
                        f"aw_photo_clear:{details.draft_id}:{details.revision}"
                    ),
                )
            ]
        )
    # §34: «Назад» ведёт к управлению текущим анонсом — к экрану правки у
    # доставленного анонса (его кнопки живут у самого поста/панели) или к
    # обзору до первой отправки.
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.nav.back"),
                callback_data=(
                    f"aw_edit:{details.draft_id}:{details.revision}"
                    if details.publication_sent
                    else f"aw_preview:{details.draft_id}:{details.revision}"
                ),
            )
        ]
    )
    await _render(update, context, "\n".join(lines), InlineKeyboardMarkup(rows))
    return AW_PREVIEW


async def _show_media_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
) -> tuple[bool, str]:
    """
    Показывает временный предпросмотр ровно тем же отправителем, что и пост.

    Это не публикация: сообщения учитываются как preview и удаляются после
    успешной отправки/смены фото. При сбое уже ушедшая часть запоминается
    для очистки, а данные анонса не меняются.
    """
    if not _is_private_chat(update):
        # Временный предпросмотр — тоже реальные фото от имени бота:
        # в группе/канале он недопустим по тем же причинам, что и анонс.
        return False, PRIVATE_CHAT_ONLY
    try:
        with SessionLocal() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                today=app_today(),
            )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError) as exc:
        return False, tr(
            "wizard.show_media_preview.ne_udalos_sobrat_predprosmotr",
            exc=_short(exc, 300),
        )
    if not preview.ready:
        reason = (
            preview.issues[0]
            if preview.issues
            else tr("wizard.show_media_preview.anons_gotov")
        )
        return False, tr(
            "wizard.show_media_preview.predprosmotr_nedostupen",
            reason=_short(reason, 300),
        )
    if not preview.draft.photos:
        return False, tr("wizard.show_media_preview.v_anonse_net_fotografiy")
    user_data = ctx.data(context)
    # Контейнер продолжения: частичная отправка запоминается для очистки.
    progress: dict = {}
    try:
        # Единый доставщик Poster 2.0 (S4): тот же отправитель, что у поста.
        message_ids = await delivery.send_announcement_post(
            context.bot,
            ctx.chat(update).id,
            preview.html,
            list(preview.draft.photos),
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001 — показываем честный сбой UI
        partial = [int(mid) for mid in progress.get("ids") or []]
        if partial:
            old = [int(mid) for mid in (user_data.get("preview_messages") or [])]
            screen.track_preview(user_data, old + partial)
        logger.exception("Не удалось показать предпросмотр вложений: %s", exc)
        return False, tr("wizard.show_media_preview.ne_udalos_pokazat_predprosmotr")
    await screen.replace_preview(context, ctx.chat(update).id, user_data, message_ids)
    return (
        True,
        tr("wizard.show_media_preview.otpravlen_vremennyy_predprosmotr"),
    )


async def _render_publish_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """Экран начатой/частичной/неудачной отправки без права правки."""
    if details.publication_status == PUBLICATION_STATUS_SENT:
        return await _render_field_list(update, context, details)
    if details.publication_status is None:
        return await _render_preview(update, context, details, notice=notice)
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    status = details.publication_status
    step = details.publication_step
    total = details.publication_total
    lines = [
        tr("wizard.render_publish_status.otpravka_gotovogo_anonsa_etot"),
        "",
        *_place_lines(details),
        tr(
            "wizard.render_publish_status.fotografiy",
            count_publication_photos=len(details.publication_photos),
        ),
    ]
    if notice:
        lines.insert(1, notice)
    if status == PUBLICATION_STATUS_SENDING:
        lines.extend(
            [
                "",
                tr("wizard.render_publish_status.otpravka_zavershena_vozmozhno"),
            ]
        )
    elif status == PUBLICATION_STATUS_PARTIAL:
        lines.extend(
            [
                "",
                tr("wizard.render_publish_status.anons_otpravlen_chastichno"),
            ]
        )
    elif status == PUBLICATION_STATUS_FAILED:
        lines.extend(
            [
                "",
                tr("wizard.render_publish_status.telegram_podtverdil_otpravku_ni"),
            ]
        )
    if total:
        lines.append(
            tr(
                "wizard.render_publish_status.podtverzhdeno_operaciy",
                min=min(step, total),
                total=total,
            )
        )
    else:
        lines.append(
            tr("wizard.render_publish_status.sohranennogo_progressa_net_pervay")
        )
    if details.previous_message_ids:
        lines.extend(
            [
                "",
                tr("wizard.render_publish_status.v_chate_ostaetsya_proshlaya"),
            ]
        )
    if details.publication_error:
        lines.extend(
            [
                "",
                tr_safe(
                    "wizard.render_publish_status.prichina",
                    publication_error=_h(details.publication_error),
                ),
            ]
        )
    lines.extend(
        [
            "",
            tr("wizard.render_publish_status.tekst_znacheniya_kirpichikov"),
        ]
    )
    rows: list[list[InlineKeyboardButton]] = []
    label = (
        tr("wizard.render_publish_status.prodolzhit_otpravku")
        if status == PUBLICATION_STATUS_SENDING
        else (
            tr("wizard.render_publish_status.doslat_ostavsheesya")
            if status == PUBLICATION_STATUS_PARTIAL
            else tr("wizard.render_publish_status.povtorit_otpravku")
        )
    )
    rows.append(
        [
            InlineKeyboardButton(
                label,
                callback_data=(f"aw_pub_resume:{details.draft_id}:{details.revision}"),
            )
        ]
    )
    if details.publication_can_reset:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.vernutsya_redaktirovaniyu"),
                    callback_data=(
                        f"aw_pub_reset:{details.draft_id}:{details.revision}"
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.wizard.moi_anonsy"), callback_data="aw_start"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
            )
        ]
    )
    await _render(update, context, "\n".join(lines), InlineKeyboardMarkup(rows))
    return AW_PREVIEW


def _delivery_fingerprint(text: str, photos: Sequence[str]) -> str:
    """
    Отпечаток payload — общий с живым блоком и доставкой (S4 п.1).

    Раньше адаптер звал приватную функцию легаси-мастера; теперь один
    алгоритм живёт в `delivery.py`, поэтому fingerprint живого блока и
    финальной отправки не может разойтись.
    """
    return delivery.delivery_fingerprint(text, list(photos))


def _announcement_is_empty(update: Update, details) -> bool:
    """
    Пуст ли анонс по видимому тексту (для заглушки/шутки в нижнем экране).

    Использует тот же рендер, что live-блок и итоговая отправка; ошибка
    сборки не считается пустотой — иначе шутка появлялась бы вместо
    диагностики. Фото учитываются: текст с фото не «пустой анонс».
    """
    try:
        with SessionLocal() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                today=app_today(),
            )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError):
        return False
    return not preview.visible_text and not details.photos


def _has_pending_changes(update: Update, details) -> bool:
    """
    Отличается ли текущий анонс от последней доставленной версии.

    Сравнивается только отпечаток текста и фото — то, что реально видит
    автор. При ошибке сборки считаем, что изменения есть: пусть явное
    действие покажет причину, а не «всё отправлено».
    """
    if not details.publication_sent:
        return False
    if not details.delivery_fingerprint:
        return True
    try:
        with SessionLocal() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                today=app_today(),
            )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError):
        return True
    return (
        _delivery_fingerprint(preview.html, preview.draft.photos)
        != details.delivery_fingerprint
    )


async def _finish_and_deliver(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """
    Собирает анонс из текущих данных и сразу доставляет его в этот чат.

    Это финальный шаг мастера: отдельного подтверждения и кнопки
    «опубликовать» нет. Повторный вызов с тем же содержимым не отправляет
    дубль: если такая версия уже доставлена, показывается экран действий.
    """
    user_id = _user_id(update)
    if not _is_private_chat(update):
        await _answer(update, PRIVATE_CHAT_ONLY, alert=True)
        return await _render_private_chat_refusal(update, context, details)
    if details.publication_status in {
        PUBLICATION_STATUS_SENDING,
        PUBLICATION_STATUS_PARTIAL,
        PUBLICATION_STATUS_FAILED,
    }:
        # Незавершённая отправка продолжается отдельной кнопкой — новая
        # версия поверх неё создала бы дубли.
        return await _render_publish_status(update, context, details, notice=notice)
    if details.stage != WizardStage.PREVIEW and not details.publication_sent:
        # Сборка всегда начинается из обзора: extras/вопросы уже применены.
        # Уже доставленный анонс остаётся `completed`: перевод в `preview`
        # выдавал бы готовый анонс за черновик, который можно продолжить
        # (§18), а правка на месте сама возвращает этап в `completed`.
        try:
            details = _ensure_stage(update, context, details, WizardStage.PREVIEW)
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
    try:
        with SessionLocal() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                today=app_today(),
            )
    except (
        AnnouncementError,
        TemplateError,
        TemplateCompileError,
        BrickError,
    ) as exc:
        return await _render_preview_error(update, context, details, str(exc))
    if not preview.ready:
        reason = (
            preview.issues[0]
            if preview.issues
            else tr("wizard.show_media_preview.anons_gotov")
        )
        return await _render_preview_error(
            update,
            context,
            preview.draft,
            tr(
                "wizard.finish_and_deliver.zapolnite_polya_poprobuyte_snova",
                reason=reason,
            ),
        )
    # S4 §5.2 / §7 (2.5.3): лимит проверяется ДО доставки по данным из БД.
    # Превышение не лечится автосокращением и не режется на части: текст и
    # фото остаются в черновике, а автор видит КОРОТКУЮ ошибку. Большого
    # экрана восстановления больше нет.
    length = check_announcement_length(
        preview.html, has_photos=bool(preview.draft.photos)
    )
    if length.over:
        return await _render_length_refusal(update, context, preview, length)
    current = _delivery_fingerprint(preview.html, preview.draft.photos)
    if details.publication_status == PUBLICATION_STATUS_SENT:
        if (
            details.delivery_fingerprint == current
            and preview.draft.delivered_message_ids
        ):
            # Ничего не изменилось: дубль не отправляем.
            return await _close_publication_without_screen(
                update,
                context,
                details,
                notice=notice
                or tr("wizard.finish_and_deliver.eta_versiya_anonsa_otpravlena"),
            )
        # §26–28: активный экземпляр обновляется НА МЕСТЕ, пока структура
        # медиа та же. Новая отправка здесь создала бы вторую копию анонса,
        # хотя Telegram умеет править текст/подпись/файл существующего
        # сообщения. Смена структуры (0↔1↔N) остаётся путём замены
        # экземпляра — тогда `None` пропускает поток к обычной доставке.
        in_place = await _apply_in_place_change(
            update,
            context,
            details,
            preview,
            fingerprint=current,
            notice=notice,
        )
        if in_place is not None:
            return in_place
    # S4 п.5 / U5: верхний блок (медиа или текст) уже стоит в чате. Если его
    # payload совпадает с текущим и прошлую версию заменять не нужно, он и есть
    # готовый пост: повторная отправка создала бы второй альбом или вторую
    # копию текста. Текстовый блок для этого доводится до финального HTML
    # (`editMessageText`), и только подтверждённая правка доказывает, что в
    # чате лежит именно текущая версия.
    #
    # Снимок адресов живого блока нужен для крайнего fallback: если Telegram
    # отказал и в правке, и в пересборке блока, промоушен невозможен и пост
    # уходит отдельным сообщением — тогда прежний живой блок (временный
    # интерфейсный мусор первого прохода) убирается после подтверждённой
    # доставки, иначе в чате остались бы две копии.
    stale_live_ids: tuple[int, ...] = (
        tuple(details.live_message_ids) if details.publication_status is None else ()
    )
    # Промоушен — только первая доставка. Уже отправленный анонс правится
    # штатно: новая версия уходит отдельным сообщением и лишь затем удаляется
    # прошлая (иначе предыдущую копию нельзя было бы заменить).
    if details.publication_status is None and not details.previous_message_ids:
        promoted = await _promote_live_post(
            update,
            context,
            details,
            html=preview.html,
            text=preview.html,
            photos=tuple(preview.draft.photos),
        )
        if promoted is not None:
            # §18: итогового экрана нет — результат уходит ответом на
            # callback, а диалог остаётся на рабочем экране редактора.
            return await _close_publication_without_screen(
                update,
                context,
                promoted,
                notice=notice or tr("wizard.ready.sent"),
            )
    try:
        with SessionLocal() as session:
            publication = begin_announcement_publication(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                expected_revision=details.revision,
                today=app_today(),
            )
    except (
        AnnouncementError,
        TemplateError,
        TemplateCompileError,
        BrickError,
    ) as exc:
        return await _render_preview_error(update, context, details, str(exc))
    _set_details(_ui(context), publication.draft)
    return await _run_announcement_publication(
        update, context, publication, stale_live_ids=stale_live_ids
    )


async def _promote_live_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    html: str,
    photos: tuple[str, ...],
    text: str | None = None,
):
    """
    Признаёт подтверждённый верхний блок готовым постом без новой отправки.

    S4 п.5: живой блок — это и есть анонс, который видит автор. Если его
    состав и текст совпадают с текущим payload, отправлять второй такой же
    пост нельзя: Telegram-история показала бы две копии.

    U5: то же правило действует и для текстового анонса без фото. Раньше
    текст всегда доставлялся отдельным сообщением, и в чате оставались две
    одинаковые копии — живой блок и доставленный пост. Теперь живой блок
    доводится до финального рендера (`editMessageText`, `text`) и, если
    правка подтверждена, сам становится доставленной версией.

    Совпадение проверяется по отпечатку, который `_sync_live` записывает
    после подтверждённой отправки/правки блока. Перед сверкой блок
    синхронизируется: после рестарта `user_data` пуст, и только фактическая
    правка/создание блока доказывает, что в чате лежит именно текущая версия.

    `text` — точный финальный рендер для живого блока (`None` — собрать из
    черновика как обычно). Передаётся вызывающим слоем, чтобы в чате лежал
    именно тот текст, который будет зафиксирован как `publication_text`.

    Возвращает обновлённый черновик, если блок стал `sent`, иначе None —
    тогда вызывающий собирает и отправляет новую версию обычным путём.
    """
    user_data = ctx.data(context)
    try:
        await _sync_live(update, context, details, text=text)
    except Exception as exc:  # noqa: BLE001 — без доказанной синхронизации нет финала
        logger.info("Живой блок #%s не синхронизирован: %s", details.draft_id, exc)
        return None
    state = wizard_live.live_state(user_data)
    current = _delivery_fingerprint(html, photos)
    ids = tuple(int(item) for item in state.get("ids") or () if isinstance(item, int))
    # Дефект S4-1: блок без фото не может стать готовым медиапостом. Отпечаток
    # у пустого текста один и тот же с фото и без них (подпись пустая), поэтому
    # дополнительно сверяется фактический состав показанного блока.
    shown_photos = int(state.get("photo_count") or 0)
    # Дефект U2b: счётчика фото мало — при том же числе сегментов набор
    # `file_id` мог измениться (добавили второе фото при одном сегменте).
    # Тогда в чате лежит ДРУГОЙ пост, и признавать его доставленным нельзя.
    shown_files = wizard_live.shown_file_ids(
        user_data, draft_id=details.draft_id, message_ids=ids
    )
    if (
        not ids
        or int(state.get("draft_id") or 0) != int(details.draft_id)
        or state.get("fingerprint") != current
        or shown_photos != len(photos)
        or (shown_files is not None and shown_files != photos)
    ):
        return None
    total = len(delivery.media_batches(len(photos))) or 1
    progress: dict[str, object] = {
        "fingerprint": current,
        "step": total,
        "ids": list(ids),
        "total": total,
    }
    try:
        with SessionLocal() as session:
            # Существующий путь: заморозка payload → подтверждённый прогресс →
            # `sent`. Триггеры 13/14 допускают этот переход, потому что
            # published_at, frozen payload и текущие `ids` согласованы.
            begin_announcement_publication(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                expected_revision=details.revision,
                today=app_today(),
            )
            record_announcement_publication_progress(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                progress=progress,
            )
            promoted = mark_announcement_publication_sent(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                message_ids=list(ids),
                fingerprint=current,
            )
    except Exception as exc:  # noqa: BLE001 — не выдаём успех без записи в БД
        logger.warning(
            "Живой блок #%s не удалось признать доставленным: %s",
            details.draft_id,
            exc,
        )
        return None
    _set_details(_ui(context), promoted)
    wizard_live.forget_live_state(user_data)
    # §24/§30: подтверждённый пост становится активным экземпляром, прежний
    # активный архивируется. Для 2+ фото досылается одна панель управления.
    # Заменённые версии НЕ удаляются: они остаются архивными в истории.
    await _ensure_delivered_instance(
        context.bot,
        ctx.chat(update).id,
        details.draft_id,
        ids,
        photos,
        fingerprint=current,
    )
    return promoted


# H3: кнопка «Применить изменения» удалена, правка доставленного анонса
# применяется сразу. Если после сохранения содержимое всё ещё отличается от
# доставленного (статус `sent` и pending), немедленное применение сорвалось:
# честно говорим об этом и не отсылаем к несуществующей кнопке. Повтор
# доступен двумя путями — ещё одна правка (она применится сразу) или экран
# повтора отправки, который появляется после неудачной попытки.
APPLY_FAILED_TEXT = tr("wizard.module.chernovik_sohranen_no_izmeneniya")


async def _ensure_service_screen_below(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message_ids,
) -> None:
    """
    Гарантирует порядок «готовый анонс выше, служебный экран ниже» (U2b).

    Инвариант проверяется по фактическим `message_id`: при замене версии
    пост отправляется ПОСЛЕ того, как экран уже существует, и порядок в
    чате нарушается. Telegram не переставляет сообщения, поэтому служебный
    экран удаляется и пересоздаётся следующим `_render` уже под постом.
    Удаляется только собственный экран из `screen_ref` и только в этом чате;
    отказ Telegram не ломает сценарий (экран остаётся, анонс доступен).
    """
    ids = [int(item) for item in message_ids or () if item is not None]
    if not ids:
        return
    user_data = ctx.data(context)
    ref = screen.screen_ref(user_data)
    if ref is None:
        ref = screen.hydrate_screen_ref(user_data, _user_id(update))
    if ref is None or int(ref.get("message_id") or 0) > max(ids):
        return
    chat_id = ctx.chat(update).id
    if int(ref.get("chat_id") or 0) != int(chat_id):
        return
    try:
        await context.bot.delete_message(chat_id, ref["message_id"])
    except Exception as exc:  # noqa: BLE001 — удаление best-effort
        logger.info(
            "Служебный экран %s не удалён (%s): анонс останется ниже.",
            ref["message_id"],
            exc,
        )
        return
    user_data.pop("screen", None)
    screen.save_screen_ref(_user_id(update), user_data, chat_id)


async def _render_delivered_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """
    Устаревший вход в правку доставленного анонса (2.5.3 §5).

    Отдельного экрана «✏️ Изменить анонс» больше нет: правка начинается со
    списка текстовых полей. Активного экземпляра нет — кнопка устарела:
    короткое пояснение и безопасный экран текущего этапа.
    """
    if _instance_for(details) is None:
        return await _stale(update, context)
    if notice:
        await _answer(update, notice)
    return await _render_field_list(update, context, details)


def _publication_error_text(exc: BaseException) -> str:
    detail = " ".join(str(exc).split())[:500]
    name = exc.__class__.__name__
    return f"{name}: {detail}" if detail else name


async def _render_private_chat_refusal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
) -> int:
    """
    Отказ от выдачи в неличный чат: данные не теряются, отправки нет.

    Экран остаётся рабочим, чтобы автор мог открыть тот же черновик в
    личном чате и завершить его. Ничего не удаляется и не замораживается.
    """
    ui = _ui(context)
    if details is not None:
        _set_details(ui, details)
    _clear_active(ui)
    lines = [
        tr("wizard.render_private_chat.gotovyy_anons_otpravlen"),
        "",
        PRIVATE_CHAT_ONLY,
    ]
    if details is not None:
        lines.extend(
            [
                "",
                *_place_lines(details),
                tr("wizard.render_private_chat.dannye_fotografii_sohraneny"),
            ]
        )
    rows: list[list[InlineKeyboardButton]] = []
    if details is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.wizard.obzor_anonsa"),
                    callback_data=f"aw_preview:{details.draft_id}:{details.revision}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.wizard.moi_anonsy"), callback_data="aw_start"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
            )
        ]
    )
    await _render(update, context, "\n".join(lines), InlineKeyboardMarkup(rows))
    return AW_PREVIEW


async def _render_publish_status_safely(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """Сбой перерисовки статуса не должен скрывать уже сохранённый прогресс."""
    try:
        return await _render_publish_status(update, context, details, notice=notice)
    except Exception:  # noqa: BLE001 — экран можно обновить следующим действием
        logger.exception("Не удалось перерисовать экран статуса отправки.")
        return AW_PREVIEW


async def _send_control_panel_safely(
    bot,
    chat_id: int,
    instance_id: int,
    photos_count: int,
    *,
    extras_available: bool = True,
) -> int | None:
    """
    Отправляет единственную панель управления медиагруппой (spec §22).

    `RetryAfter` — не сбой доставки: один повтор через `delivery.call_telegram`.
    Любая другая ошибка Telegram логируется и возвращает None: контент уже
    доставлен, поэтому сбой панели не имеет права «отменить» анонс (§24).
    """
    try:
        return await delivery.call_telegram(
            lambda: announcement_controls.send_panel(
                bot,
                chat_id,
                instance_id,
                photos_count=photos_count,
                extras_available=extras_available,
            )
        )
    except Exception as exc:  # noqa: BLE001 — панель не валит доставку контента
        logger.warning(
            "Панель управления анонса (экземпляр #%s) не отправлена: %s",
            instance_id,
            exc,
        )
        return None


async def _attach_instance_keyboard(
    bot,
    chat_id: int,
    instance_id: int,
    message_ids: Sequence[int],
    photos_count: int,
    *,
    extras_available: bool = True,
) -> None:
    """
    Прикрепляет клавиатуру управления к самому сообщению анонса (§20–21).

    `callback_data` кнопок содержит id экземпляра (§32), а экземпляр можно
    зарегистрировать только после подтверждённой отправки контента: до этого
    адресов сообщений ещё нет, а идентичность экземпляра в схеме неизменяема.
    Поэтому кнопки приходят к уже доставленному сообщению отдельным
    `editMessageReplyMarkup` — в чате остаётся ОДНО сообщение с текстом и
    кнопками, второй служебной копии нет. Ошибка прикрепления логируется:
    анонс уже в чате, и это не сбой доставки.
    """
    message_id = delivery.caption_message_id(message_ids)
    if message_id is None:
        return
    try:
        await delivery.call_telegram(
            lambda: bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=announcement_controls.panel_keyboard(
                    instance_id,
                    photos_count=photos_count,
                    extras_available=extras_available,
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001 — кнопки досылаются повтором/редактором
        logger.warning(
            "Клавиатура управления анонса (экземпляр #%s) не прикреплена: %s",
            instance_id,
            exc,
        )


async def _archive_instance_controls(bot, chat_id: int, instance) -> None:
    """
    Снимает управление с прежнего активного экземпляра (spec §30 п.4–5).

    Архивный снимок остаётся в чате с текстом и фото (§31), но его кнопки
    обязаны перестать работать, а отдельная панель медиагруппы — исчезнуть.
    Обе операции best-effort: Telegram не удаляет старые сообщения и не
    всегда даёт править клавиатуру, а доставка новой версии уже состоялась.
    """
    panel_id = getattr(instance, "panel_message_id", None)
    if panel_id and int(panel_id) > 0:
        try:
            await delivery.call_telegram(
                lambda: bot.delete_message(chat_id=chat_id, message_id=panel_id)
            )
        except Exception as exc:  # noqa: BLE001 — панель лишь UI архивного снимка
            logger.info(
                "Панель архивного экземпляра #%s не удалена: %s", instance.id, exc
            )
        else:
            # Панели в чате больше нет — адрес на удалённое сообщение не
            # оставляем: снимок хранит текст, фото и состав вложений (§31).
            try:
                with SessionLocal() as session:
                    strip_panel(session, instance.id)
            except Exception as exc:  # noqa: BLE001 — снимок уже архивный
                logger.info(
                    "Адрес панели архивного экземпляра #%s не снят: %s",
                    instance.id,
                    exc,
                )
    message_id = delivery.caption_message_id(instance.message_ids)
    if message_id is None:
        return
    try:
        await delivery.call_telegram(
            lambda: bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        )
    except Exception as exc:  # noqa: BLE001 — кнопки архивного снимка не критичны
        logger.info(
            "Клавиатура архивного экземпляра #%s не снята: %s", instance.id, exc
        )


async def _apply_in_place_change(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    preview,
    *,
    fingerprint: str,
    notice: str | None = None,
) -> int | None:
    """
    Применяет правку доставленного анонса к его активному экземпляру (§26–28).

    Пока структура медиа та же (0/1/N фото не меняется), новая версия НЕ
    отправляется: правится существующее сообщение (`editMessageText` /
    `editMessageCaption` / `editMessageMedia`), поэтому адреса доставки,
    поколение экземпляра и кнопки остаются на месте, а в чате не появляется
    вторая копия анонса.

    `None` — правка на месте неприменима: состав медиа изменился, активного
    экземпляра нет, либо Telegram отказал. Тогда вызывающий слой идёт обычным
    путём замены экземпляра (§28: смена структуры — контролируемая замена),
    а прежний активный остаётся нетронутым до подтверждённой доставки.

    `int` — экран, на котором остаётся автор (правка применена).
    """
    photos = tuple(preview.draft.photos)
    # Прежний payload доставленной версии — единственный надёжный источник
    # структуры: у текстового экземпляра `message_ids` тоже длина 1.
    previous_photos = tuple(details.publication_photos)
    if len(photos) != len(previous_photos):
        return None
    try:
        with SessionLocal() as session:
            instance = get_active_instance(session, details.draft_id)
    except Exception as exc:  # noqa: BLE001 — без экземпляра остаётся обычный путь
        logger.warning(
            "Активный экземпляр анонса #%s не прочитан: %s", details.draft_id, exc
        )
        return None
    if instance is None or not instance.is_active:
        return None
    chat_id = ctx.chat(update).id
    # Текст для правки — тот же, что ушёл бы в новую отправку: с фото пустой
    # видимый текст остаётся пустой подписью, а не технической заглушкой.
    text = preview.html or ""
    if photos and (text == PLACEHOLDER_TEXT or not delivery.visible_text(text)):
        text = ""
    # Клавиатура управления передаётся в правку ЯВНО (§27): Bot API не
    # документирует сохранение прежней клавиатуры у `editMessageText`/
    # `editMessageCaption`, поэтому поведение не должно зависеть от его
    # семантики. Для 0–1 фото кнопки приатачены к самому сообщению анонса —
    # их и передаём; у альбома клавиатура живёт на отдельной панели, а
    # элементы группы её не несут.
    keyboard = None
    if not delivery.needs_control_panel(len(photos)):
        keyboard = announcement_controls.panel_keyboard(
            instance.id,
            photos_count=len(photos),
            extras_available=_draft_extras_available(details.draft_id),
        )
    edited = await _edit_active_instance_in_place(
        context.bot,
        chat_id,
        instance,
        text=text,
        photos=photos,
        previous_photos=previous_photos,
        previous_text=details.publication_text,
        fingerprint=fingerprint,
        reply_markup=keyboard,
    )
    if not edited:
        return None
    try:
        with SessionLocal() as session:
            fresh = record_in_place_publication(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                message_ids=list(instance.message_ids),
                html=preview.html,
                photos=photos,
                fingerprint=fingerprint,
            )
    except Exception:  # noqa: BLE001 — сообщение в чате уже обновлено
        # Правка Telegram подтверждена, поэтому автору не показываем сбой
        # учёта. Повторное действие доведёт запись: правка того же
        # содержимого отвечает «message is not modified», и прогресс
        # обновится без второй копии в чате.
        logger.exception(
            "Правка анонса #%s не зафиксирована в прогрессе", details.draft_id
        )
        try:
            fresh = _session_details(_user_id(update), details.draft_id)
        except AnnouncementError:
            fresh = details
    _set_details(_ui(context), fresh)
    return await _close_publication_without_screen(
        update,
        context,
        fresh,
        notice=notice,
    )


def _instance_photo_count(instance) -> int:
    """Сколько вложений описывает экземпляр (0 — текст, 1 — фото, N — альбом)."""
    return len(getattr(instance, "message_ids", ()) or ())


async def _edit_active_instance_in_place(
    bot,
    chat_id: int,
    instance,
    *,
    text: str,
    photos: Sequence[str],
    previous_photos: Sequence[str],
    previous_text: str | None = None,
    fingerprint: str,
    reply_markup=None,
) -> bool:
    """
    Обновляет АКТИВНЫЙ экземпляр правкой сообщения, без новой отправки (§28).

    Что и чем правится — зависит от числа фото, потому что Telegram
    по-разному хранит текст:

    - 0 фото — текст живёт в сообщении: `editMessageText`;
    - 1 фото — текст в подписи единственного сообщения: `editMessageCaption`;
    - N фото — подпись альбома принадлежит ПЕРВОМУ элементу группы
      (`delivery.caption_message_id`), поэтому подпись правится по нему.

    Подпись правится только когда текст действительно изменился: при замене
    одного файла лишний `editMessageCaption` перерисовывал бы сообщение без
    причины. Замена самих файлов при том же числе фото — `editMessageMedia`
    по элементам, которые отличаются от прежней доставленной версии.

    Меняются только содержимое и отпечаток экземпляра: `generation` и
    `message_ids` не трогаются (триггер схемы их и не разрешает менять),
    архивных снимков не появляется, кнопки/панель остаются на месте.

    `reply_markup` передаётся в правку содержимого явно: Bot API не
    гарантирует сохранение прежней клавиатуры, поэтому вызывающий слой
    передаёт актуальную клавиатуру управления (0–1 фото), а для альбома —
    `None` (кнопки живут на отдельной панели).

    `True` — правка подтверждена Telegram (в том числе «message is not
    modified»). `False` — правка не удалась: вызывающий слой обязан оставить
    прежний экземпляр рабочим и не терять фото (§28/§30).

    Предусловие: число фото payload совпадает с числом фото экземпляра.
    Смена структуры (0↔1↔N) идёт путём замены экземпляра, а не правкой.
    """
    current_photos = list(photos)
    # Число фото экземпляра берётся из прежнего payload: `message_ids` у
    # текстового экземпляра тоже длина 1, поэтому по ним 0 и 1 не различить.
    instance_photos = len(tuple(previous_photos or ()))
    if len(current_photos) != instance_photos:
        logger.info(
            "Экземпляр #%s: структура медиа изменилась (%s → %s) — нужна замена.",
            instance.id,
            instance_photos,
            len(current_photos),
        )
        return False
    message_id = delivery.caption_message_id(instance.message_ids)
    if message_id is None:
        return False
    caption = text or ""
    media_changed = list(current_photos) != list(previous_photos or ())
    # Текст сравнивается по видимому содержимому: маркер пустой подписи и
    # техническая заглушка не являются правкой (S4 §5.6).
    text_changed = previous_text is None or delivery.visible_text(
        caption
    ) != delivery.visible_text(previous_text)
    if text_changed:
        try:
            if instance_photos == 0:
                await delivery.call_telegram(
                    lambda: bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=caption,
                        parse_mode="HTML",
                        link_preview_options=delivery.FINAL_LINK_PREVIEW,
                        reply_markup=reply_markup,
                    )
                )
            else:
                # 1 фото и альбом: текст — подпись. В альбоме это ПЕРВЫЙ
                # элемент (`caption_message_id`), а не последний.
                await delivery.call_telegram(
                    lambda: bot.edit_message_caption(
                        chat_id=chat_id,
                        message_id=message_id,
                        caption=caption or None,
                        parse_mode="HTML" if caption else None,
                        reply_markup=reply_markup,
                    )
                )
        except Exception as exc:  # noqa: BLE001 — решение по типу ниже
            if delivery.is_not_modified(exc):
                # Содержимое уже актуально: это успех, а не сбой правки.
                logger.info(
                    "Экземпляр #%s уже актуален (message is not modified).",
                    instance.id,
                )
            else:
                logger.warning(
                    "Экземпляр #%s не обновлён на месте (%s) — "
                    "прежний остаётся рабочим.",
                    instance.id,
                    exc,
                )
                return False
    elif not media_changed:
        # Ни текста, ни файлов: править нечего, но отпечаток всё равно
        # фиксируется вызывающим слоем.
        return True
    if media_changed:
        # Замена файлов при неизменном числе фото: альбом нельзя «дополнить»,
        # но отдельный элемент группы правится `editMessageMedia`.
        if not await _replace_instance_media(
            bot,
            chat_id,
            instance,
            current_photos,
            previous_photos=previous_photos,
            caption=caption,
        ):
            return False
    try:
        with SessionLocal() as session:
            update_instance_fingerprint(session, instance.id, fingerprint)
    except Exception as exc:  # noqa: BLE001 — правка уже в чате
        logger.warning("Отпечаток экземпляра #%s не сохранён: %s", instance.id, exc)
    return True


async def _replace_instance_media(
    bot,
    chat_id: int,
    instance,
    photos: Sequence[str],
    *,
    previous_photos: Sequence[str],
    caption: str,
) -> bool:
    """
    Доводит вложения экземпляра до текущего payload (§28).

    `editMessageCaption` меняет только подпись, поэтому заменённый файл при
    том же числе фото правится `editMessageMedia` по каждому элементу,
    который отличается от прежнего. Адреса сообщений при этом те же: это
    по-прежнему один экземпляр, а не новая доставка. `True` — состав
    вложений соответствует payload (или правка не требуется).
    """
    from telegram import InputMediaPhoto

    shown = tuple(str(item) for item in (photos or ()))
    previous = tuple(str(item) for item in (previous_photos or ()))
    if len(previous) != len(shown):
        # Состав изменился — это путь замены экземпляра, не правки на месте.
        return False
    for index, (old_id, new_id) in enumerate(zip(previous, shown)):
        if old_id == new_id:
            continue
        message_id = int(instance.message_ids[index])
        media = InputMediaPhoto(
            media=new_id,
            caption=(caption or None) if index == 0 else None,
            parse_mode="HTML" if (index == 0 and caption) else None,
        )
        try:
            await delivery.call_telegram(
                partial(
                    bot.edit_message_media,
                    chat_id=chat_id,
                    message_id=message_id,
                    media=media,
                )
            )
        except Exception as exc:  # noqa: BLE001 — решение по типу ниже
            if delivery.is_not_modified(exc):
                continue
            logger.warning(
                "Вложение %s экземпляра #%s не заменено: %s",
                index,
                instance.id,
                exc,
            )
            return False
    return True


async def _ensure_delivered_instance(
    bot,
    chat_id: int,
    draft_id: int,
    sent_ids: Sequence[int],
    photos: Sequence[str],
    *,
    fingerprint: str | None = None,
) -> object | None:
    """
    Регистрирует активный экземпляр доставленного анонса и его управление.

    Экземпляр создаётся ПОСЛЕ подтверждённой доставки контента (§24): только
    тогда известны `message_id`, а `register_instance` сам архивирует прежний
    активный экземпляр (§30). Цепочка идемпотентна для ретрая: если активный
    экземпляр уже описывает ровно эти сообщения, второй не создаётся, а
    панель досылается ТОЛЬКО когда её ещё нет. Сбой учёта не отменяет уже
    доставленный анонс — он лишь лишает управление привязки к экземпляру.
    """
    checked_ids = [int(item) for item in sent_ids]
    instance = None
    fresh = False
    previous = None
    try:
        with SessionLocal() as session:
            current = get_active_instance(session, draft_id)
            if current is None:
                # Первая доставка: активного экземпляра ещё нет.
                instance = register_instance(
                    session,
                    draft_id,
                    chat_id,
                    checked_ids,
                    fingerprint=fingerprint,
                )
                fresh = True
            elif tuple(current.message_ids) != tuple(checked_ids):
                # §28: смена структуры медиа — контролируемая замена активного
                # экземпляра. `replace_instance` архивирует прежний и вставляет
                # новый в ОДНОЙ транзакции, поэтому двух активных не бывает.
                instance = replace_instance(
                    session,
                    current.id,
                    checked_ids,
                    fingerprint=fingerprint,
                )
                fresh = True
                previous = current
            else:
                instance = current
    except Exception as exc:  # noqa: BLE001 — доставка уже состоялась
        logger.exception(
            "Экземпляр доставленного анонса #%s не зарегистрирован: %s",
            draft_id,
            exc,
        )
        return None
    if instance is None:
        return None
    if previous is not None:
        # §30 п.4–5: прежний экземпляр уже архивный в БД, теперь у него
        # снимается управление, а панель медиагруппы убирается из чата.
        await _archive_instance_controls(bot, chat_id, previous)
    photos_count = len(photos)
    # Poster 2.5.2 §4: панель/клавиатура экземпляра подчиняются тому же
    # предикату, что и экраны редактора. Ошибка чтения скрывает кнопку:
    # пустой экран доп. полей хуже её отсутствия.
    show_extras = _draft_extras_available(draft_id)
    if delivery.needs_control_panel(photos_count):
        # Панель — отдельная операция строго после контента (§22). Уже
        # отправленную панель повтор не дублирует.
        if instance.has_panel:
            return instance
        panel_id = await _send_control_panel_safely(
            bot,
            chat_id,
            instance.id,
            photos_count,
            extras_available=show_extras,
        )
        if panel_id is None:
            return instance
        try:
            with SessionLocal() as session:
                set_panel_message(session, instance.id, panel_id)
        except Exception as exc:  # noqa: BLE001 — экземпляр и контент уже есть
            logger.warning(
                "Адрес панели экземпляра #%s не сохранён: %s", instance.id, exc
            )
        return instance
    if fresh:
        await _attach_instance_keyboard(
            bot,
            chat_id,
            instance.id,
            instance.message_ids,
            photos_count,
            extras_available=show_extras,
        )
    return instance


async def _close_service_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Закрывает временный служебный экран конструктора (Poster 2.5.2, §2).

    У опубликованного анонса ОДНО место управления: сам пост (0–1 фото) или
    его панель (2+ фото). Экран шага/редактора, с которого пришла отправка,
    — временный интерфейс: после подтверждённой доставки он удаляется, а его
    ref снимается, иначе в чате осталась бы вторая, уже нерабочая
    клавиатура.

    Вместе с экраном снимается resume-снимок: шаг фото ждал ввода, и без
    этого следующий текст ушёл бы в черновик вместо подсказки меню. WIP при
    этом не трогается — данные анонса остаются в БД. Удаление best-effort:
    отказ Telegram не ломает уже доставленный анонс.
    """
    user_id = _user_id(update)
    chat_id = ctx.chat(update).id
    user_data = ctx.data(context)
    _clear_resume(user_id, user_data, chat_id)
    ref = screen.screen_ref(user_data)
    if ref is None:
        ref = screen.hydrate_screen_ref(user_data, user_id)
    if ref is None:
        return
    user_data.pop("screen", None)
    if int(ref.get("chat_id") or 0) == int(chat_id):
        await screen.cleanup_messages(
            context.bot, chat_id, [int(ref.get("message_id") or 0)]
        )
    screen.save_screen_ref(user_id, user_data, chat_id)


async def _close_publication_without_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """
    Финал публикации БЕЗ служебного экрана (spec §18, 2.5.2 §2).

    Готовый анонс уже стоит в чате цельным сообщением (или альбомом с
    панелью), а его управление привязано к экземпляру (`anp_`). Служебный
    экран, с которого ушла публикация, закрывается: второе место управления
    (экран «✏️ Изменить анонс») в чате не остаётся.

    Диалог не обрывается: состояние `AW_PREVIEW` сохраняется, поэтому
    нажатия `anp_` продолжают работать, а экран редактора открывается по
    явной кнопке «📝 Данные» — это временный экран, который закрывается
    после успешной правки (§2, сценарий 7).

    `notice` не попадает в текст экрана: результат доставки сообщается
    коротким ответом на callback.
    """
    if notice:
        await _answer(update, notice[:200])
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    await _close_service_screen(update, context)
    return AW_PREVIEW


async def _run_announcement_publication(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    publication,
    *,
    stale_live_ids: Sequence[int] = (),
) -> int:
    """
    Отправляет только замороженный payload и сохраняет прогресс после каждой
    подтверждённой операции. Успех фиксируется лишь после ответа Telegram.

    `stale_live_ids` — адреса живого блока первой попытки (U5). Если его не
    удалось признать доставленным и пост ушёл отдельным сообщением, прежний
    живой блок — временный интерфейсный мусор конструктора, а не результат:
    после подтверждённой доставки он убирается, иначе в чате остались бы две
    копии одного текста. Ошибка удаления не считается ошибкой доставки.
    """
    user_id = _user_id(update)
    draft_id = publication.draft.draft_id
    chat_id = ctx.chat(update).id
    progress = dict(publication.progress)

    # S4 §5.6: с фото пустой текст уходит без подписи. В замороженном
    # payload лежит техническая заглушка (`.`) или невидимый маркер пустой
    # подписи (их требует форма публикации), но подписью они не становятся —
    # отпечаток от этого не меняется.
    text = publication.text
    if publication.photos and (
        text == PLACEHOLDER_TEXT or not delivery.visible_text(text)
    ):
        text = ""

    def _persist_progress(snapshot: dict) -> None:
        # Сбой записи НЕ отменяет уже подтверждённую отправку: id остаются
        # в памяти прогресса и будут сохранены следующей удачной записью или
        # фиксацией `sent`. Иначе повтор отправил бы подтверждённое заново.
        try:
            with SessionLocal() as session:
                record_announcement_publication_progress(
                    session,
                    actor_telegram_user_id=user_id,
                    draft_id=draft_id,
                    progress=snapshot,
                )
        except Exception as exc:  # noqa: BLE001 — прогресс досохраним позже
            logger.warning(
                "Не удалось сохранить прогресс анонса #%s: %s", draft_id, exc
            )

    # Адреса прошлой доставленной версии больше НЕ удаляются: по §30–31
    # прежний экземпляр остаётся в чате архивным снимком (его управление
    # снимает/архивирует `register_instance`). Поэтому `previous_ids` здесь
    # не читаются вовсе — иначе новая версия уничтожала бы историю.
    try:
        sent_ids = await delivery.send_announcement_post(
            context.bot,
            chat_id,
            text,
            list(publication.photos),
            progress=progress,
            on_progress=_persist_progress,
        )
    except Exception as exc:  # noqa: BLE001 — сбой Telegram/БД должен быть виден
        logger.exception("Отправка анонса #%s не завершена: %s", draft_id, exc)
        # Подтверждённое до сбоя могло не попасть в БД из-за сбоя записи —
        # досохраняем из памяти, чтобы продолжение не отправило дубль.
        _persist_progress(progress)
        error_text = _publication_error_text(exc)
        try:
            with SessionLocal() as session:
                details = mark_announcement_publication_failed(
                    session,
                    actor_telegram_user_id=user_id,
                    draft_id=draft_id,
                    error=error_text,
                )
        except Exception:  # noqa: BLE001 — сохраняем исходную ошибку отправки
            logger.exception(
                "Не удалось сохранить статус сбоя отправки анонса #%s",
                draft_id,
            )
            try:
                with SessionLocal() as session:
                    details = get_announcement_draft(
                        session,
                        actor_telegram_user_id=user_id,
                        draft_id=draft_id,
                    )
            except Exception:  # noqa: BLE001 — показываем хотя бы прошлый снимок
                details = publication.draft
        return await _render_publish_status_safely(
            update,
            context,
            details,
            notice=tr("wizard.run_announcement.otpravku_udalos_podtverdit"),
        )

    try:
        with SessionLocal() as session:
            details = mark_announcement_publication_sent(
                session,
                actor_telegram_user_id=user_id,
                draft_id=draft_id,
                message_ids=sent_ids,
                fingerprint=_delivery_fingerprint(publication.text, publication.photos),
            )
    except Exception as exc:  # noqa: BLE001 — сообщения могли уже уйти
        logger.exception(
            "Telegram подтвердил отправку, но статус анонса #%s не сохранён: %s",
            draft_id,
            exc,
        )
        # Подтверждённые id уже есть в памяти: сохраняем их как прогресс,
        # чтобы следующая попытка дослала только недостающее.
        _persist_progress(progress)
        try:
            with SessionLocal() as session:
                details = get_announcement_draft(
                    session,
                    actor_telegram_user_id=user_id,
                    draft_id=draft_id,
                )
        except Exception:  # noqa: BLE001 — не врём об успехе
            details = publication.draft
        return await _render_publish_status_safely(
            update,
            context,
            details,
            notice=(tr("wizard.run_announcement.telegram_podtverdil_otpravku_no")),
        )
    await screen.cleanup_preview(context, chat_id, ctx.data(context))
    # Экземпляр и его управление регистрируются после подтверждённой
    # доставки (§24): 2+ фото получают отдельную панель (§22), 0/1 — кнопки
    # на самом сообщении. Сбой учёта не отменяет уже доставленный анонс.
    await _ensure_delivered_instance(
        context.bot,
        chat_id,
        draft_id,
        sent_ids,
        publication.photos,
        fingerprint=_delivery_fingerprint(publication.text, publication.photos),
    )
    # U5: живой блок, который не удалось признать доставленным (нет
    # доказанной синхронизации, отказ правки/пересборки), остался в чате
    # второй копией текста. Это временный интерфейсный мусор конструктора,
    # и убирается он только сейчас — после подтверждённой доставки.
    #
    # §30–31: адреса ПРОШЛОГО доставленного экземпляра — не мусор. После
    # промоушена живого блока `live_message_ids` продолжает указывать на
    # сообщение, которое уже стало доставленной версией; при повторной
    # отправке после сбоя оно попадает и в `previous_ids`. Удалять его
    # нельзя: прежний экземпляр остаётся в чате архивным снимком.
    previous_ids = {
        int(message_id) for message_id in publication.draft.previous_message_ids
    }
    stale = [
        int(message_id)
        for message_id in stale_live_ids
        if int(message_id) not in sent_ids and int(message_id) not in previous_ids
    ]
    if stale:
        await screen.cleanup_messages(context.bot, chat_id, stale)
        try:
            with SessionLocal() as session:
                details = set_announcement_live_message_ids(
                    session,
                    actor_telegram_user_id=user_id,
                    draft_id=draft_id,
                    message_ids=[],
                )
        except Exception as exc:  # noqa: BLE001 — адреса лишь UI-состояние
            logger.info("Адреса живого блока #%s не очищены: %s", draft_id, exc)
    _set_details(_ui(context), details)
    return await _close_publication_without_screen(
        update,
        context,
        details,
        notice=tr("wizard.run_announcement.anons_otpravlen_etot_chat"),
    )


async def _handle_place_search_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    ui = _ui(context)
    await _reanchor(update, context)
    raw = update.effective_message.text if update.effective_message else ""
    query = (raw or "").strip()
    if not query:
        return await _render_search_prompt(
            update,
            context,
            notice=tr("wizard.handle_place_search.vvedite_nepustoy_zapros"),
        )
    ui["place"]["query"] = query
    ui["place"]["page"] = 0
    result = await _render_search_results(update, context, 0)
    await screen.drop_user_input(update, context)
    return result


async def _reject_choice_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
) -> int:
    message = tr("wizard.reject_choice.vyberite_variant_knopkoy_tekstovyy")
    screen.track_failed_input(
        ctx.data(context), f"aw:{field.brick_id}", update.effective_message
    )
    if source == "extra":
        extra = next(item for item in details.extras if item.brick_id == field.brick_id)
        return await _render_extra_value_prompt(
            update, context, details, extra, error=message
        )
    fields = await _question_fields_for(update, context, details)
    found = _find_field(fields, field.brick_id)
    if found is None:
        return await _enter_extras(update, context, details)
    return await _render_question(
        update,
        context,
        details,
        fields,
        found[0],
        editing=True,
        error=message,
    )


async def _handle_active_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    ui = _ui(context)
    brick_id = ui.get("active_brick_id")
    source = ui.get("active_source")
    if brick_id is None or source not in {"question", "extra", "field"}:
        return None
    draft_id = ui.get("draft_id")
    if not draft_id:
        return None
    try:
        details = _session_details(_user_id(update), int(draft_id))
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        return await _stale(update, context)
    if details.revision != ui.get("revision"):
        ui["revision"] = details.revision
        return await _stale(update, context)
    _set_details(ui, details)

    if source == "field":
        # 2.5.4 §1: значение поля доставленного анонса. Данные не теряются:
        # маршрут общий с готовыми вариантами, а ошибка восстанавливает
        # клавиатуру ввода на том же сообщении.
        found = _field_input_context(_user_id(update), ui, details)
        if found is None:
            return await _stale(update, context)
        instance, field = found
        if field.value_type == BRICK_TYPE_CHOICE:
            return await _field_input_error(
                update,
                context,
                details,
                instance,
                field,
                tr("wizard.reject_choice.vyberite_variant_knopkoy_tekstovyy"),
            )
        return await _save_field_input_value(
            update,
            context,
            details,
            instance,
            field,
            capture_message_text(update.effective_message),
            drop_input=True,
        )

    if source == "question":
        fields = await _question_fields_for(update, context, details)
        found = _find_field(fields, int(brick_id))
        if found is None:
            return await _stale(update, context)
        index, field = found
        if ui.get("active_brick_id") != field.brick_id:
            return await _stale(update, context)
        if not _delivered_text_question_allowed(details, field):
            # Подделанный/устаревший активный кирпичик поля площадки у
            # доставленного анонса: значение справочника вводом не пишется
            # (§5), даже если вопрос был подставлен старым снимком resume.
            return await _stale(update, context)
        await _reanchor(update, context)
        if field.value_type == BRICK_TYPE_CHOICE:
            return await _reject_choice_text(
                update, context, details, field, index, "question"
            )
        raw = capture_message_text(update.effective_message)
        return await _save_question_value(
            update,
            context,
            details,
            field,
            raw,
            index=index,
            drop_input=True,
        )

    extra = next(
        (item for item in details.extras if item.brick_id == int(brick_id)),
        None,
    )
    if extra is None:
        return await _stale(update, context)
    definition = _extra_definition(details, extra, _user_id(update))
    field = _extra_field_state(details, definition)
    await _reanchor(update, context)
    if field.value_type == BRICK_TYPE_CHOICE:
        return await _reject_choice_text(update, context, details, field, -1, "extra")
    raw = capture_message_text(update.effective_message)
    return await _save_extra_value(
        update,
        context,
        details,
        extra,
        raw,
        drop_input=True,
        notice=tr("wizard.handle_choice.znachenie_dopolnitelnogo"),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    active = await _handle_active_text(update, context)
    if active is not None:
        return active
    ui = _ui(context)
    if ui["place"].get("phase") == "search_input" and not ui.get("draft_id"):
        return await _handle_place_search_text(update, context)
    await _reanchor(update, context)
    return await _resume_current(
        update,
        context,
        notice=tr("wizard.handle.vyberite_deystvie_knopkami_ekrane"),
    )


# Сообщения-источники фото по партиям альбома. Удаляются только после
# подтверждённой записи И только если партия принята целиком: при превышении
# лимита ввод остаётся в чате (S4 §5.2 п.2).
BATCH_INPUTS_KEY = "aw_photo_batch_inputs"


def _track_batch_input(user_data: dict[str, Any], key: str, message) -> None:
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return
    stored = user_data.get(BATCH_INPUTS_KEY)
    if not isinstance(stored, dict):
        stored = {}
        user_data[BATCH_INPUTS_KEY] = stored
    items = stored.setdefault(key, [])
    if int(message_id) not in items:
        items.append(int(message_id))


def _take_batch_inputs(user_data: dict[str, Any], key: str) -> list[int]:
    stored = user_data.get(BATCH_INPUTS_KEY)
    if not isinstance(stored, dict):
        return []
    items = stored.pop(key, []) or []
    if not stored:
        user_data.pop(BATCH_INPUTS_KEY, None)
    return [int(mid) for mid in items]


def _drop_batch_inputs(user_data: dict[str, Any], key: str) -> None:
    _take_batch_inputs(user_data, key)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Принимает фото и альбомы: партия целиком, затем автоматический финал.

    Telegram доставляет каждое фото альбома отдельным Update с общим
    `media_group_id`, поэтому партия собирается в буфере `wizard_photos` и
    закрывается короткой паузой (или следующим действием пользователя).
    Сохранение в SQLite идёт при закрытии партии: до её окончания анонс НЕ
    считается готовым, иначе трёхсоставный альбом дал бы три финала.

    Одиночное фото без `media_group_id` — партия из одного снимка, она
    закрывается сразу. 11-е фото отклоняется с объяснением: уже принятые
    снимки не теряются, а сообщение пользователя не удаляется.
    """
    ui = _ui(context)
    draft_id = ui.get("draft_id")
    message = update.effective_message
    photo = message.photo[-1] if message is not None and message.photo else None
    if not draft_id or photo is None:
        await _reanchor(update, context)
        return await _resume_current(
            update,
            context,
            notice=tr("wizard.handle_photo.ne_udalos_prochitat_fotografiyu"),
        )
    try:
        details = _session_details(_user_id(update), int(draft_id))
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        ctx.data(context)["announcement_ui"] = _fresh_ui()
        return await _render_home(
            update,
            context,
            notice=tr("wizard.resume_current.chernovik_nayden_bolshe_nedostupen"),
        )
    photos_allowed = details.stage == WizardStage.PREVIEW or (
        details.publication_sent and details.stage == WizardStage.COMPLETED
    )
    if not photos_allowed:
        await _reanchor(update, context)
        return await _resume_current(
            update,
            context,
            notice=(tr("wizard.handle_photo.fotografii_dobavlyayutsya_etape")),
        )
    if _edit_blocked(details):
        return await _render_publish_status(
            update,
            context,
            details,
            notice=(tr("wizard.handle_photo.otpravka_nachata_novye_fotografii")),
        )
    if details.publication_sent and getattr(message, "media_group_id", None) is None:
        # Уже доставленный анонс и одиночный снимок: H3 применяет его сразу
        # (новой версии не ждёт вся партия), ввод удаляется после
        # подтверждённой записи (S4 §5.3).
        return await _add_photo_to_delivered(update, context, details, photo, message)
    user_data = ctx.data(context)
    key = wizard_photos.buffer_photo(
        user_data,
        getattr(message, "media_group_id", None),
        photo.file_id,
    )
    _track_batch_input(user_data, key, message)
    # Каждый снимок сохраняется сразу (идемпотентно, с CAS и лимитом 10):
    # рестарт посреди альбома ничего не теряет. Готовность наступает
    # отдельно — только когда партия целиком закрыта.
    try:
        batch = wizard_photos.add_photo_batch(
            telegram_user_id=_user_id(update),
            draft_id=details.draft_id,
            revision=details.revision,
            file_ids=[photo.file_id],
            # Фабрика сессий берётся из этого модуля: тесты подменяют именно
            # её временной SQLite, рабочая база не затрагивается.
            session_factory=SessionLocal,
        )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError) as exc:
        screen.track_failed_input(user_data, f"aw-photo:{details.draft_id}", message)
        _drop_batch_inputs(user_data, key)
        return await wizard_photos.render_photo_step(
            update,
            context,
            details,
            notice=wizard_photos.rejected_photo_notice(exc),
            announcement_is_empty=_announcement_is_empty(update, details),
        )
    wizard_photos.absorb_result(user_data, key, batch)
    _set_details(ui, batch.details)
    if getattr(message, "media_group_id", None) is None:
        # Одиночный снимок — целая партия: финал наступает сразу, без паузы.
        return await _close_photo_batch(update, context, key)
    wizard_photos.schedule_album_close(
        getattr(context, "application", None),
        user_data,
        lambda: _close_photo_batch(update, context, key),
    )
    # До закрытия партии экран не перерисовываем: анонс ещё не готов.
    return AW_PREVIEW


async def _add_photo_to_delivered(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    photo,
    message,
) -> int:
    """
    Одиночное фото к уже доставленному анонсу: сохранить и сразу применить.

    H3: кнопки «Применить изменения» нет, поэтому подтверждённый снимок
    сразу заменяет доставленную версию. Сюда попадают только одиночные
    снимки (`media_group_id is None`): партия альбома применяется один раз
    при её закрытии в `_close_photo_batch`, иначе 0→N давала бы по доставке
    на каждый снимок (§28, §58 И). Каждый `file_id` сохраняется с CAS, 11-е
    фото отклоняется, а сообщение пользователя удаляется только после
    подтверждённой записи.
    """
    user_data = ctx.data(context)
    try:
        batch = wizard_photos.add_photo_batch(
            telegram_user_id=_user_id(update),
            draft_id=details.draft_id,
            revision=details.revision,
            file_ids=[photo.file_id],
            session_factory=SessionLocal,
        )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError) as exc:
        # Ввод остаётся в чате: пользователь видит, что именно не принято.
        screen.track_failed_input(user_data, f"aw-photo:{details.draft_id}", message)
        return await _show_photos(
            update,
            context,
            details,
            notice=wizard_photos.rejected_photo_notice(exc),
        )
    _set_details(_ui(context), batch.details)
    fresh = None
    if batch.changed:
        # H3: правка доставленного анонса применяется сразу — новая версия
        # заменяет прошлую без кнопки «Применить изменения».
        fresh = await _apply_immediately(update, context, batch.details)
        if fresh is None:
            # Доставка не подтверждена: `_finish_and_deliver` уже показал
            # причину, а ввод остаётся в чате — автор видит свой снимок.
            return AW_PREVIEW
    if batch.changed or batch.duplicate:
        # Ввод удаляется только после подтверждённой доставки.
        await screen.drop_user_input(update, context)
    return await _show_photos(
        update,
        context,
        fresh or batch.details,
        notice=wizard_photos.batch_notice(batch),
    )


async def _close_photo_batch(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
) -> int:
    """
    Закрывает партию фото: решает, готов ли анонс, и запускает автофинал.

    Снимки уже сохранены по одному; здесь фиксируется «партия получена
    целиком». Отказ по лимиту 10 не теряет принятые фото и оставляет
    сообщение пользователя в чате. Принятая партия удаляет ввод только
    после подтверждённой записи.
    """
    user_data = ctx.data(context)
    ui = _ui(context)
    draft_id = ui.get("draft_id")
    wizard_photos.take_batch(user_data, key)
    result = wizard_photos.take_result(user_data, key)
    if not draft_id or result is None:
        return AW_PREVIEW
    user_id = _user_id(update)
    try:
        details = _session_details(user_id, int(draft_id))
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        return await _resume_current(update, context)
    inputs = _take_batch_inputs(user_data, key)
    _set_details(ui, details)
    if details.publication_sent:
        # Партия фото к уже доставленному анонсу применяется ОДИН раз, после
        # её закрытия: смена структуры 0→N — одна контролируемая замена и
        # один архивный снимок (§28, §58 И), а промежуточных доставок по
        # снимку не появляется.
        fresh = details
        if result.changed:
            applied = await _apply_immediately(update, context, details)
            if applied is None:
                # Доставка не подтверждена: `_finish_and_deliver` уже показал
                # причину, прежняя версия остаётся рабочей, ввод не удаляем.
                return AW_PREVIEW
            fresh = applied
        if result.rejected:
            # Лимит 10: принятые снимки уже применены одной заменой, ввод
            # остаётся в чате — автор видит, что именно не принято.
            return await _show_photos(
                update,
                context,
                fresh,
                notice=wizard_photos.limit_notice(result),
            )
        if result.changed or result.duplicate:
            await screen.cleanup_messages(context.bot, ctx.chat(update).id, inputs)
        return await _show_photos(
            update,
            context,
            fresh,
            notice=wizard_photos.batch_notice(result),
        )
    if result.rejected:
        # Лимит 10: объясняем отказ, принятые фото сохранены, ввод оставляем
        # в чате — пользователь видит, что именно не принято.
        return await wizard_photos.render_photo_step(
            update,
            context,
            details,
            notice=wizard_photos.limit_notice(result),
            announcement_is_empty=_announcement_is_empty(update, details),
        )
    # Партия подтверждённо сохранена. Ввод удаляется только после
    # ПОДТВЕРЖДЁННОЙ доставки: при превышении лимита экран обещает, что
    # сообщение пользователя осталось (S4 §5.2 п.2), и это должно быть правдой.
    # Вся партия принята → анонс готов автоматически, без кнопки «Готово».
    state = await _finish_and_deliver(
        update,
        context,
        details,
        notice=wizard_photos.batch_notice(result) or None,
    )
    # Ввод удаляется только после ПОДТВЕРЖДЁННОЙ доставки (S4 §5.2 п.2):
    # при превышении лимита экран обещает, что сообщение пользователя
    # осталось, и это должно быть правдой.
    try:
        delivered = _session_details(user_id, details.draft_id)
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        delivered = None
    if delivered is not None and delivered.publication_sent:
        await screen.cleanup_messages(context.bot, ctx.chat(update).id, inputs)
    return state


async def _archive_published_controls(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    «🏠 В меню» завершает работу с опубликованным анонсом (2.5.2 §2).

    Экземпляр активного черновика становится архивным, а его управление
    снимается с фактического сообщения: клавиатура (`editMessageReplyMarkup`
    без разметки) и отдельная панель медиагруппы. Сам анонс — текст и фото —
    остаётся в чате как исторический снимок (§31) и НЕ удаляется.

    Дальше старые кнопки не работают: `anp_`-обработчик видит архивный
    экземпляр и отвечает коротким alert'ом без правок (§32). Повторное
    открытие из «📂 Мои анонсы» создаёт новый экземпляр (§29–30).

    Вызывается только на выходе в меню; ошибки best-effort: навигация не
    должна ломаться из-за того, что Telegram не дал снять клавиатуру.
    """
    draft_id = _ui(context).get("draft_id")
    if not draft_id:
        return
    chat_id = ctx.chat(update).id
    instance = None
    try:
        with SessionLocal() as session:
            current = get_active_instance(session, int(draft_id))
            if current is None or int(current.chat_id) != int(chat_id):
                return
            if archive_instance(session, current.id):
                instance = current
    except Exception as exc:  # noqa: BLE001 — выход в меню не отменяется
        logger.warning(
            "Экземпляр анонса #%s не архивирован при выходе: %s", draft_id, exc
        )
        return
    if instance is None:
        return
    await _archive_instance_controls(context.bot, chat_id, instance)


async def _exit_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = _user_id(update)
    # 2.5.2 §2: у опубликованного анонса одно место управления. Выход в меню
    # закрывает управление экземпляром, иначе прежние кнопки остались бы в
    # чате рядом с меню. Черновик при этом не меняется.
    await _archive_published_controls(update, context)
    _clear_ui(context)
    # H1: явный выход закрывает приём ввода — обычный текст больше не
    # должен «оживлять» черновик.
    _clear_resume(user_id, ctx.data(context), ctx.chat(update).id)
    _clear_other_sessions(user_id)
    from handlers.main_menu import render_main_menu

    await render_main_menu(update, context)
    return ConversationHandler.END


async def _close_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = _user_id(update)
    _clear_ui(context)
    _clear_other_sessions(user_id)
    # H1: `/cancel` закрывает конструктор; черновик остаётся в «📂 Мои
    # анонсы», но текст после отмены в него уже не попадает.
    _clear_resume(user_id, ctx.data(context), ctx.chat(update).id)
    await _reanchor(update, context)
    await _render(
        update,
        context,
        tr("wizard.close_wizard.konstruktor_anonsa_zakryt"),
        _main_menu_keyboard(),
    )
    await screen.drop_user_input(update, context)
    return ConversationHandler.END


async def handle_navigation_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = ctx.query(update)
    data = query.data or ""
    if data == "main_menu":
        await _answer(update)
        return await _exit_to_main_menu(update, context)
    if data == "drafts":
        # /drafts и старый callback ведут в единый список «📂 Мои анонсы»:
        # легаси-хранилища Draft больше нет (S5).
        await _answer(update)
        _clear_ui(context)
        return await open_my_announcements(update, context)
    if data == "places":
        _clear_ui(context)
        from handlers.places import open_places

        await open_places(update, context)
        return ConversationHandler.END
    if data == "bricks":
        _clear_ui(context)
        from handlers.bricks import HUB_ORIGIN_WIZARD, open_bricks

        await open_bricks(update, context, origin=HUB_ORIGIN_WIZARD)
        return ConversationHandler.END
    if data == "templates":
        _clear_ui(context)
        from handlers.templates import open_templates

        await open_templates(update, context)
        return ConversationHandler.END
    if data == "admin_panel":
        _clear_ui(context)
        from handlers.admin import open_admin

        await open_admin(update, context)
        return ConversationHandler.END
    if data == "help":
        await _answer(update)
        _clear_ui(context)
        from handlers.main_menu import render_help

        await render_help(update, context)
        return ConversationHandler.END
    if data == "create_post":
        # Старого мастера больше нет (S5): устаревшая кнопка не запускает
        # ничего, а ведёт в меню перерисовкой существующего экрана.
        await _answer(
            update, tr("alerts.wizard.knopka_ustarela_vyberite_razdel"), alert=True
        )
        return await _exit_to_main_menu(update, context)
    if data == "restore_wip":
        await _answer(
            update,
            tr("alerts.wizard.nezavershennye_anonsy"),
            alert=True,
        )
        return await _exit_to_main_menu(update, context)
    if data == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    await _answer(update, tr("alerts.wizard.knopka_ustarela"), alert=True)
    return await _resume_current(update, context, notice=STALE_TEXT)


async def handle_unexpected_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await _answer(update, tr("alerts.wizard.knopka_ustarela"), alert=True)
    return await _resume_current(update, context, notice=STALE_TEXT)


async def _command_to_section(
    update: Update, context: ContextTypes.DEFAULT_TYPE, target: str
) -> int:
    _clear_ui(context)
    _clear_other_sessions(_user_id(update))
    # H1: переход в другой раздел закрывает приём ввода конструктора.
    # Справка выходом не является (контракт 2.5.1, так же как кнопка
    # «Помощь» и `/help` после рестарта): её «◀️ Назад» возвращает на тот же
    # шаг, поэтому resume-снимок сохраняется — иначе источник справки
    # определился бы как «main» и шаг черновика терялся.
    if target != "help":
        _clear_resume(_user_id(update), ctx.data(context), ctx.chat(update).id)
    if target == "drafts":
        # Единый список анонсов вместо легаси-черновиков (S5).
        return await open_my_announcements(update, context)
    if target == "places":
        from handlers.places import open_places

        await open_places(update, context)
        return ConversationHandler.END
    if target == "bricks":
        from handlers.bricks import open_bricks

        await open_bricks(update, context)
        return ConversationHandler.END
    if target == "templates":
        from handlers.templates import open_templates

        await open_templates(update, context)
        return ConversationHandler.END
    if target == "admin":
        from handlers.admin import open_admin

        await open_admin(update, context)
        return ConversationHandler.END
    if target == "help":
        from handlers.main_menu import render_help

        await render_help(update, context)
        return ConversationHandler.END
    from handlers.main_menu import render_main_menu

    await render_main_menu(update, context)
    return ConversationHandler.END


def _callback_parts(update: Update) -> list[str]:
    return (ctx.query(update).data or "").split(":")


async def handle_place_state_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    parts = _callback_parts(update)
    action = parts[0]
    ui = _ui(context)

    if action == "aw_new":
        await _answer(update)
        _reset_for_place(context)
        return await _render_place_hub(update, context)
    if action == "aw_home":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_home_page" and len(parts) == 2:
        await _answer(update)
        if ui["place"].get("phase") != "home":
            return await _stale(update, context)
        try:
            ui["home_page"] = max(0, int(parts[1]))
        except ValueError:
            return await _stale(update, context)
        return await _render_home(update, context)
    if action == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_open" and len(parts) == 2:
        await _answer(update)
        try:
            draft_id = int(parts[1])
        except ValueError:
            return await _stale(update, context)
        return await _open_draft(update, context, draft_id)

    # P3: удаление записи «Мои анонсы» — только с подтверждением. Telegram-
    # сообщения (экран, готовый пост) при этом не удаляются никогда.
    if action == "aw_del" and len(parts) == 2:
        await _answer(update)
        try:
            draft_id = int(parts[1])
        except ValueError:
            return await _stale(update, context)
        return await _render_delete_confirm(update, context, draft_id)
    if action == "aw_del_no" and len(parts) == 2:
        await _answer(update, tr("alerts.wizard.udalenie_anonsa_otmeneno"))
        return await _render_my_announcements(update, context)
    if action == "aw_del_yes" and len(parts) == 2:
        try:
            draft_id = int(parts[1])
        except ValueError:
            return await _stale(update, context)
        return await _delete_announcement_entry(update, context, draft_id)

    if action == "aw_place_search":
        await _answer(update)
        return await _render_search_prompt(update, context)
    if action == "aw_place_all" and len(parts) == 2:
        await _answer(update)
        try:
            page = int(parts[1])
        except ValueError:
            return await _stale(update, context)
        return await _render_all_places(update, context, page)
    if action == "aw_place_results" and len(parts) == 2:
        if ui["place"].get("phase") != "results":
            return await _stale(update, context)
        await _answer(update)
        try:
            page = int(parts[1])
        except ValueError:
            return await _stale(update, context)
        return await _render_search_results(update, context, page)
    if action == "aw_place_open" and len(parts) == 2:
        try:
            place_id = int(parts[1])
        except ValueError:
            return await _stale(update, context)
        phase = ui["place"].get("phase")
        with SessionLocal() as session:
            details = get_place_details(session, place_id)
            if phase == "results":
                query = str(ui["place"].get("query") or "")
                visible = {result.place_id for result in search_places(session, query)}
                if place_id not in visible:
                    details = None
        if details is None:
            return await _stale(update, context)
        await _answer(update)
        ui["place"].update(
            {
                "place_id": place_id,
                "name_id": details.canonical_name_id,
                "return_phase": phase if phase in {"results", "all"} else "hub",
                "return_page": int(ui["place"].get("page") or 0),
            }
        )
        return await _render_place_card(update, context)
    if action == "aw_place_names" and len(parts) == 3:
        try:
            place_id = int(parts[1])
            page = int(parts[2])
        except ValueError:
            return await _stale(update, context)
        if ui["place"].get("place_id") != place_id:
            return await _stale(update, context)
        await _answer(update)
        return await _render_place_names(update, context, page)
    if action == "aw_place_name_pick" and len(parts) == 4:
        try:
            place_id = int(parts[1])
            name_id = int(parts[2])
            int(parts[3])
        except ValueError:
            return await _stale(update, context)
        if ui["place"].get("place_id") != place_id:
            return await _stale(update, context)
        with SessionLocal() as session:
            details = get_place_details(session, place_id)
        if details is None or name_id not in {name.id for name in details.names}:
            return await _stale(update, context)
        await _answer(update)
        ui["place"]["name_id"] = name_id
        return await _render_place_card(update, context)
    if action == "aw_place_back":
        await _answer(update)
        return await _place_back(update, context)
    if action == "aw_place_confirm" and len(parts) == 3:
        try:
            place_id = int(parts[1])
            name_id = int(parts[2])
        except ValueError:
            return await _stale(update, context)
        return await _confirm_place(update, context, place_id, name_id)

    return await _stale(update, context)


async def handle_template_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    parts = _callback_parts(update)
    action = parts[0]
    if action == "aw_cont" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        await _answer(update)
        return await _resume_current(update, context)
    if action == "aw_tplpage" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        ui, details = guarded
        ui["template_page"] = int(parts[3])
        await _answer(update)
        return await _render_template_screen(update, context, details)
    if action == "aw_template" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        return await _select_template(
            update, context, int(parts[1]), int(parts[2]), int(parts[3])
        )
    if action == "aw_home":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    return await _stale(update, context)


async def _handle_calendar_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Callback'и компактного календаря (U2a).

    Новые действия: `aw_calquick` (первый экран), `aw_calrange` (раскрытие
    семидневного диапазона), `aw_calmonths` (сетка 12 месяцев),
    `aw_calmonth` (дни месяца), `aw_calday` (выбор даты без подтверждения).

    Poster 2.5.2 (§8): отдельная совместимость со старыми экранами недель и
    «другой даты» удалена — Poster не публиковался, сообщений с этими
    кнопками в чатах нет. Неизвестное действие `aw_cal*` уходит в общий
    безопасный путь `_stale`: ответ «кнопка устарела» и перерисовка
    текущего экрана, без внутренней ошибки.
    """
    parts = _callback_parts(update)
    action = parts[0]
    if len(parts) < 4:
        return await _stale(update, context)
    try:
        draft_id = int(parts[1])
        revision = int(parts[2])
        brick_id = int(parts[3])
    except ValueError:
        return await _stale(update, context)
    found = _calendar_context(update, context, draft_id, revision, brick_id)
    if found is None:
        return await _stale(update, context)
    details, field, index, source, bounds = found
    start, end = bounds
    allowed = (start, end)
    today = app_today()

    if action == "aw_calquick":
        await _answer(update)
        return await _render_calendar_value(
            update, context, details, field, index, source
        )
    if action == "aw_calrange" and len(parts) == 5:
        try:
            range_index = int(parts[4])
        except ValueError:
            return await _stale(update, context)
        ranges = calendar_view.followup_ranges(today, allowed)
        if range_index < 0 or range_index >= len(ranges):
            return await _stale(update, context)
        value_range = ranges[range_index]
        await _answer(update)
        return await _render_calendar_range_days(
            update, context, details, field, index, source, value_range
        )
    if action == "aw_calmonths":
        await _answer(update)
        return await _render_calendar_months(
            update, context, details, field, index, source
        )
    if action == "aw_calmonth" and len(parts) == 6:
        try:
            key = (int(parts[4]), int(parts[5]))
        except ValueError:
            return await _stale(update, context)
        # Год и месяц проверяются ДО month_days: подделанный callback
        # (`:0:5`, `:-1:5`, `:99999:12`) раньше доходил до date(...) и
        # отдавал пользователю «Произошла внутренняя ошибка».
        if not calendar_view.is_valid_month_key(key):
            return await _stale(update, context)
        if not calendar_view.month_days(key, allowed):
            return await _stale(update, context)
        await _answer(update)
        return await _render_calendar_month(
            update, context, details, field, index, source, key
        )
    if action == "aw_calday" and len(parts) == 5:
        try:
            day = date.fromisoformat(parts[4])
        except ValueError:
            return await _stale(update, context)
        await _answer(update)
        return await _save_calendar_day(
            update, context, details, field, index, source, day
        )
    if action == "aw_calback":
        await _answer(update)
        return await _render_calendar_value(
            update, context, details, field, index, source
        )
    return await _stale(update, context)


async def handle_question_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    parts = _callback_parts(update)
    action = parts[0]
    ui = _ui(context)

    if action.startswith("aw_cal"):
        return await _handle_calendar_callback(update, context)
    if action == "aw_choice" and len(parts) == 5:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        await _answer(update)
        return await _handle_choice(
            update,
            context,
            int(parts[1]),
            int(parts[2]),
            int(parts[3]),
            int(parts[4]),
        )
    if action == "aw_choice_done" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        await _answer(update)
        return await _handle_choice_done(
            update, context, int(parts[1]), int(parts[2]), int(parts[3])
        )

    if action == "aw_timeset" and len(parts) >= 5:
        # S3 п.5: быстрая кнопка времени. Путь сохранения тот же, что у
        # ручного ввода: CAS по revision, drop_user_input, перерисовка
        # вопроса с уже обновлённым верхним блоком.
        # Значение содержит двоеточие («18:00»), поэтому callback разбит
        # больше чем на пять частей — остаток склеиваем обратно.
        if not all(item.isdigit() for item in parts[1:4]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        found = _active_field(
            update, context, int(parts[1]), int(parts[2]), int(parts[3])
        )
        if found is None:
            return await _stale(update, context)
        _owned, field, index, _source = found
        if field.value_type != BRICK_TYPE_TIME:
            return await _stale(update, context)
        preset = ":".join(parts[4:])
        await _answer(update, preset)
        return await _save_question_value(
            update,
            context,
            details,
            field,
            preset,
            index=index,
            drop_input=True,
        )

    if action == "aw_qmanage" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        if details.publication_sent:
            # 2.5.3 §5: у доставленного анонса «изменить поля» — это список
            # текстовых полей на самом управлении, а не экран вопросов
            # конструктора.
            return await _render_field_list(update, context, details)
        return await _render_question_manager(update, context, details)
    if action == "aw_qgoto" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        # Отказ полю площадки решается ДО `_ensure_stage`: иначе отказ сам
        # менял бы stage/revision черновика.
        fields = await _question_fields_for(update, context, details)
        field_found = _find_field(fields, int(parts[3]))
        if field_found is None:
            return await _stale(update, context)
        index, field = field_found
        if not _delivered_text_question_allowed(details, field):
            # Та же валидация, что у `_open_field_question`: у доставленного
            # анонса подделанный callback на поле площадки получает отказ и
            # ничего не меняет (§5). Значения справочника вводом не
            # переписываются.
            return await _stale(update, context)
        try:
            updated = _ensure_stage(update, context, details, WizardStage.QUESTIONS)
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
        fields = await _question_fields_for(update, context, updated)
        field_found = _find_field(fields, int(parts[3]))
        if field_found is None:
            return await _stale(update, context)
        index, field = field_found
        ui["active_brick_id"] = field.brick_id
        ui["active_source"] = "question"
        await _answer(update)
        return await _render_question(update, context, updated, fields, index)
    if action in {"aw_qback", "aw_qskip"} and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        found = await _question_from_callback(
            update,
            context,
            int(parts[1]),
            int(parts[2]),
            int(parts[3]),
        )
        if found is None:
            return await _stale(update, context)
        _ui_data, details, fields, index, field = found
        await _answer(update)
        if action == "aw_qback":
            # Требование V: сначала реальный путь. Со стека возвращаемся на
            # тот экран, с которого пришли (список полей, «Изменить»);
            # фолбэк без стека — прежний шаг по индексу, а с первого
            # вопроса — экран шаблона.
            ui_stack = _ui(context)
            previous = _nav_pop_from(ui_stack, NAV_QUESTION)
            rendered = await _nav_render(update, context, details, previous)
            if rendered is not None:
                return rendered
            if index <= 0:
                return await _render_template_screen(update, context, details)
            return await _render_question(update, context, details, fields, index - 1)
        return await _skip_question(update, context, details, field, index)
    if action == "aw_manage_back":
        draft_id = ui.get("draft_id")
        revision = ui.get("revision")
        guarded = None
        if draft_id and revision:
            guarded = _guard_details(update, context, int(draft_id), int(revision))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        # Требование V: «Поля анонса» возвращают на экран, с которого их
        # открыли (например, «✏️ Изменить» готового анонса), а не к выбору
        # шаблона. Без стека — безопасный экран текущего этапа.
        previous = _nav_pop_from(_ui(context), NAV_MANAGER)
        rendered = await _nav_render(update, context, details, previous)
        if rendered is not None:
            return rendered
        if details.stage == WizardStage.PREVIEW:
            return await _render_preview(update, context, details)
        if details.stage == WizardStage.EXTRAS:
            return await _render_extras(update, context, details)
        return await _render_template_screen(update, context, details)
    if action == "aw_extras" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _enter_extras(update, context, details)
    if action == "aw_preview" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_preview(update, context, details)
    if action == "aw_home":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    return await _stale(update, context)


async def handle_extras_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    parts = _callback_parts(update)
    action = parts[0]
    ui = _ui(context)

    if action.startswith("aw_cal") or action in {
        "aw_choice",
        "aw_choice_done",
    }:
        return await handle_question_callback(update, context)
    if action in {"aw_apply", "aw_send", "aw_panel", "aw_save"}:
        # Эти действия живут на экране обзора/управления, но доступны и с
        # экрана доп. кирпичиков — иначе правку нельзя было бы применить.
        return await handle_preview_callback(update, context)

    if action == "aw_extras" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _enter_extras(update, context, details)
    if action == "aw_exback" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _back_from_extras(update, context, details)
    if action == "aw_exbpage" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_extras(update, context, details, page=int(parts[3]))
    if action == "aw_exdel" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        extra_id = int(parts[3])
        extra = next(
            (item for item in details.extras if item.extra_id == extra_id),
            None,
        )
        if extra is None:
            return await _stale(update, context)
        try:
            with SessionLocal() as session:
                updated = remove_announcement_extra(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    extra_id=extra_id,
                    expected_revision=details.revision,
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
        await _answer(update, tr("alerts.wizard.dopolnitelnyy_kirpichik_udalen"))
        notice = tr(
            "wizard.handle_extras_callback.udalen_kirpichik",
            title=_short(brick_title_display(extra.title, extra.code), 50),
        )
        if updated.publication_sent:
            # H3 (требование A): удаление применяется сразу, как и ввод.
            # Последнее удалённое поле возвращает анонс к каноническому
            # тексту без отдельного подтверждения.
            await _apply_immediately(update, context, updated)
            updated = _reload_details(update, updated)
        return await _render_extras(update, context, updated, notice=notice)
    if action == "aw_exedit" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        brick_id = int(parts[3])
        await _answer(update)
        # Требование IV: кирпичик списка открывается одним маршрутом —
        # незаполненный сначала становится выбранным (значение попадает на
        # место токена в расширенном тексте), затем сразу открывается экран
        # значения. Заполненный открывается без лишней записи.
        extra = next(
            (item for item in details.extras if item.brick_id == brick_id),
            None,
        )
        if extra is not None:
            return await _render_extra_value_prompt(update, context, details, extra)
        return await _add_extra(update, context, details, brick_id)
    if action == "aw_exskip" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        brick_id = int(parts[3])
        extra = next(
            (item for item in details.extras if item.brick_id == brick_id),
            None,
        )
        if extra is None or ui.get("active_brick_id") != brick_id:
            return await _stale(update, context)
        definition = _extra_definition(details, extra, _user_id(update))
        field = _extra_field_state(details, definition)
        await _answer(update)
        if field.status == FIELD_STATUS_VALID:
            # Требование VI: «Пропустить» не очищает значение и не
            # пересобирает анонс — просто возвращаемся к списку.
            _clear_active(ui)
            return await _render_extras(
                update,
                context,
                details,
                notice=tr(
                    "wizard.handle_extras_callback.znachenie_ostavleno",
                    title=_short(brick_title_display(extra.title, extra.code), 50),
                ),
            )
        return await _save_extra_value(
            update,
            context,
            details,
            extra,
            "",
            notice=tr("wizard.handle_extras_callback.znachenie_dopolnitelnogo"),
        )
    if action == "aw_exbackval" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        _clear_active(ui)
        await _answer(update)
        # Требование V: экран значения доп. поля возвращает туда, откуда его
        # открыли (в реальном пути это всегда список доп. полей); без стека —
        # сам список как безопасный экран этого шага.
        previous = _nav_pop_from(ui, NAV_EXTRA_VALUE)
        rendered = await _nav_render(update, context, details, previous)
        if rendered is not None:
            return rendered
        return await _render_extras(update, context, details)
    if action == "aw_qmanage" and len(parts) == 3:
        return await handle_question_callback(update, context)
    if action == "aw_preview" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_preview(update, context, details)
    if action == "aw_home":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    return await _stale(update, context)


async def handle_preview_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    parts = _callback_parts(update)
    action = parts[0]

    def _guided() -> tuple[dict[str, Any], Any] | None:
        if len(parts) < 3 or not all(item.isdigit() for item in parts[1:3]):
            return None
        return _guard_details(update, context, int(parts[1]), int(parts[2]))

    if action == "aw_save" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
            return await _render_publish_status(update, context, details)
        if details.publication_sent:
            # Анонс уже доставлен: «сохранить» здесь означает выйти, не
            # трогая отправленную версию. Этап и снимок не меняются.
            pending = _has_pending_changes(update, details)
            notice = (
                # H3: кнопки «Применить изменения» больше нет — см.
                # `_render_preview`: правка применяется сразу, а
                # оставшиеся изменения означают сорвавшееся применение.
                APPLY_FAILED_TEXT
                if pending
                else tr("wizard.render_preview.chernovik_sohranen_otpravlennyy")
            )
            if _instance_for(details) is None:
                return await _stale(update, context)
            # 2.5.3 §5: служебного экрана «✏️ Изменить анонс» нет — правка
            # начинается со списка текстовых полей на самом управлении.
            # Пояснение уходит ОДНИМ коротким ответом: второй ответ на тот же
            # callback Telegram не покажет, а строка «Сохранено.» без него
            # ничего не сообщает.
            await _answer(update, notice)
            return await _render_field_list(update, context, details)
        try:
            updated = _ensure_stage(update, context, details, WizardStage.PREVIEW)
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
        await _answer(update, tr("alerts.wizard.chernovik_sohranen"))
        return await _render_preview(
            update,
            context,
            updated,
            notice=tr("wizard.handle_preview_callback.chernovik_sohranen_anons_chat"),
            saved=True,
        )
    if action == "aw_send" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update, tr("alerts.wizard.otpravlyayu_anons"))
        return await _finish_and_deliver(update, context, details)
    if action == "aw_apply" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update, tr("alerts.wizard.primenyayu_izmeneniya"))
        return await _finish_and_deliver(update, context, details)
    if action == "aw_preview" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_preview(update, context, details, preview_only=True)
    if action == "aw_edit" and len(parts) == 3:
        # Прежний экран «✏️ Изменить анонс» удалён (2.5.3 §5): устаревшая
        # кнопка ведёт туда же, куда актуальная — в список текстовых полей.
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _instance_for(details) is None:
            return await _stale(update, context)
        if _edit_blocked(details):
            await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        return await _render_field_list(update, context, details)
    if action == "aw_panel" and len(parts) == 3:
        # callback прежних версий («◀️ Назад» на удалённый экран «Анонс
        # готов»): устаревшая кнопка ведёт в список текстовых полей, пока
        # у анонса есть активный экземпляр; иначе — «устарело».
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _instance_for(details) is None:
            return await _stale(update, context)
        await _answer(update)
        return await _render_field_list(update, context, details)
    if action == "aw_qmanage" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
            return await _render_publish_status(update, context, details)
        return await handle_question_callback(update, context)
    if action == "aw_extras" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        return await _enter_extras(update, context, details)

    # --- фотографии -----------------------------------------------------
    if action == "aw_photos" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _show_photos(update, context, details, page=int(parts[3]))
    if action == "aw_photo_back" and len(parts) == 3:
        # U2b: «Назад» с шага фото — к предыдущему шагу (последний кирпичик),
        # без потери уже сохранённых фото и значений. Механизм тот же, что у
        # навигации по вопросам: `_render_question` с последним кирпичиком.
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        return await _back_from_photo_step(update, context, details)
    if action == "aw_photo_none" and len(parts) == 3:
        # S4 п.2: «Без фотографий» на шаге фото — это финал: шаг собран в
        # `wizard_photos`, доставка идёт общим путём без кнопки «Готово».
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update, tr("alerts.wizard.prodolzhaem_bez_fotografiy"))
        return await _finish_and_deliver(
            update,
            context,
            details,
            notice=tr("wizard.handle_preview_callback.anons_ostanetsya_bez_vlozheniy"),
        )
    if action == "aw_photo_add" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        return await wizard_photos.render_photo_step(
            update,
            context,
            details,
            notice=(
                tr("wizard.handle_preview_callback.otpravte_odnu_fotografiyu_albom")
            ),
            announcement_is_empty=_announcement_is_empty(update, details),
        )
    if action == "aw_photo_del" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(
                update, tr("alerts.wizard.kollekciya_foto_zamorozhena"), alert=True
            )
            return await _render_publish_status(update, context, details)
        index = int(parts[3])
        try:
            with SessionLocal() as session:
                updated = remove_announcement_photo(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    index=index,
                    expected_revision=details.revision,
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _show_photos(
                update,
                context,
                details,
                notice=tr(
                    "wizard.handle_preview_callback.foto_udaleno", exc=_short(exc, 300)
                ),
            )
        if updated.publication_sent:
            # H3: удаление фото у доставленного анонса тоже применяется
            # сразу — иначе без кнопки «Применить изменения» правка
            # осталась бы только в черновике.
            fresh = await _apply_immediately(update, context, updated)
            updated = fresh or _reload_details(update, updated)
        _set_details(_ui(context), updated)
        await _answer(update, tr("alerts.wizard.foto_udaleno"))
        await screen.cleanup_preview(context, ctx.chat(update).id, ctx.data(context))
        return await _show_photos(
            update,
            context,
            updated,
            notice=tr("wizard.handle_preview_callback.foto_udaleno_chtoby_zamenit"),
            page=index // PHOTO_PAGE_SIZE,
            answered=True,
        )
    if action == "aw_photo_clear" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(
                update, tr("alerts.wizard.kollekciya_foto_zamorozhena"), alert=True
            )
            return await _render_publish_status(update, context, details)
        await _answer(update)
        return await _show_photos(update, context, details, clear_confirm=True)
    if action == "aw_photo_clear_yes" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(
                update, tr("alerts.wizard.kollekciya_foto_zamorozhena"), alert=True
            )
            return await _render_publish_status(update, context, details)
        try:
            with SessionLocal() as session:
                updated = clear_announcement_photos(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    expected_revision=details.revision,
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _show_photos(
                update,
                context,
                details,
                notice=tr(
                    "wizard.handle_preview_callback.foto_udaleny", exc=_short(exc, 300)
                ),
                answered=True,
            )
        if updated.publication_sent:
            # H3: очистка коллекции применяется сразу, как и удаление одного
            # снимка: иначе готовый пост навсегда остался бы с фото.
            fresh = await _apply_immediately(update, context, updated)
            updated = fresh or _reload_details(update, updated)
        _set_details(_ui(context), updated)
        await _answer(update, tr("alerts.wizard.vse_fotografii_udaleny"))
        await screen.cleanup_preview(context, ctx.chat(update).id, ctx.data(context))
        return await _show_photos(
            update,
            context,
            updated,
            notice=tr("wizard.handle_preview_callback.vse_fotografii_udaleny"),
            answered=True,
        )
    if action == "aw_photo_show" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        ok, notice = await _show_media_preview(update, context, details)
        return await _show_photos(
            update,
            context,
            details,
            notice=notice if ok else f"⚠️ {notice}",
            answered=True,
        )
    if action == "aw_media_preview" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        ok, notice = await _show_media_preview(update, context, details)
        return await _render_preview(
            update,
            context,
            details,
            notice=notice if ok else f"⚠️ {notice}",
        )

    # --- отправка готового анонса ---------------------------------------
    if action == "aw_pub_resume" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if not _is_private_chat(update):
            await _answer(update, PRIVATE_CHAT_ONLY, alert=True)
            return await _render_private_chat_refusal(update, context, details)
        # U5-D1: повтор идёт мимо `_finish_and_deliver`, поэтому снимок
        # адресов живого блока снимается здесь — из черновика в БД. Если блок
        # так и не удалось признать доставленным (сбой правки/пересборки, нет
        # доказанной синхронизации), после подтверждённого повтора он —
        # временный интерфейсный мусор конструктора и остался бы второй
        # копией текста. Снимок берётся ДО отправки: новый пост может получить
        # другие message_id, а совпавший id отфильтрует сама публикация.
        try:
            with SessionLocal() as session:
                publication = resume_announcement_publication(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    expected_revision=details.revision,
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
        stale_live_ids = tuple(publication.draft.live_message_ids)
        _set_details(_ui(context), publication.draft)
        await _answer(update, tr("alerts.wizard.prodolzhayu_otpravku"))
        return await _run_announcement_publication(
            update, context, publication, stale_live_ids=stale_live_ids
        )
    if action == "aw_pub_reset" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        try:
            with SessionLocal() as session:
                updated = reset_failed_announcement_publication(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    expected_revision=details.revision,
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _render_publish_status(update, context, details)
        _set_details(_ui(context), updated)
        await _answer(update, tr("alerts.wizard.mozhno_snova_redaktirovat_anons"))
        return await _render_preview(
            update,
            context,
            updated,
            notice=(tr("wizard.handle_preview_callback.publikaciya_nachinalas_mozhno")),
        )

    if action == "aw_new":
        await _answer(update)
        _reset_for_place(context)
        return await _render_place_hub(update, context)
    if action == "aw_home":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    return await _stale(update, context)


def _common_state_handlers() -> list[BaseHandler]:
    return [
        CallbackQueryHandler(
            handle_navigation_callback,
            pattern=(
                r"^(main_menu|drafts|places|bricks|templates|"
                r"admin_panel|help|create_post|restore_wip)$"
            ),
        )
    ]


async def open_new_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    _end_foreign_conversations(update, context)
    _clear_other_sessions(_user_id(update))
    await _reanchor(update, context)
    ctx.data(context)["announcement_ui"] = _fresh_ui()
    return await _render_home(update, context)


async def start_place_announcement_pick(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    place_id: int,
) -> int:
    """
    Немедленный старт анонса по выбранной площадке (Poster 2.0 S2).

    Тап по названию в списке площадок не задаёт вопросов: место
    подтверждается программно, снимком становится стандартный шаблон
    площадки, и пользователь сразу попадает к вопросам. Идемпотентность
    обеспечивает сервис: повтор с тем же черновиком не создаёт второй.
    """
    await _answer(update)
    _end_foreign_conversations(update, context)
    _clear_other_sessions(_user_id(update))
    await _reanchor(update, context)
    # R1, дефект №23: старт конструктора — вход в актуальный UI; у клиента
    # могла остаться reply-клавиатура старых версий.
    await screen.drop_legacy_reply_keyboard(update, context)
    user_id = _user_id(update)
    ui = _fresh_ui()
    ctx.data(context)["announcement_ui"] = ui
    try:
        with SessionLocal() as session:
            details = start_announcement_wizard(
                session,
                actor_telegram_user_id=user_id,
                place_id=place_id,
                idempotency_key=ui["place"]["idempotency_key"] or uuid.uuid4().hex,
            )
            details = confirm_announcement_place(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                expected_revision=details.revision,
            )
            standard = get_standard_template(session, user_id, place_id)
            details = choose_announcement_template(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                template_id=standard.template_id,
                expected_revision=details.revision,
                expected_template_revision=standard.revision,
            )
    except AnnouncementNotFoundError:
        await _answer(
            update, tr("alerts.wizard.ploschadka_ee_shablon_bolshe"), alert=True
        )
        return await _render_home(update, context)
    except (AnnouncementError, TemplateError) as exc:
        await _answer(update, str(exc), alert=True)
        return await _render_home(update, context, notice=str(exc))
    _set_details(ui, details)
    return await _enter_questions(update, context, details)


async def start_personal_announcement_pick(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    template_id: int,
) -> int:
    """
    Немедленный старт анонса по личному шаблону (Poster 2.0 S2).

    Снимок шаблона создаётся сервисом S1 сразу; площадки у такого анонса нет.
    Чужой или удалённый шаблон неотличим от несуществующего.
    """
    await _answer(update)
    _end_foreign_conversations(update, context)
    _clear_other_sessions(_user_id(update))
    await _reanchor(update, context)
    # R1, дефект №23: старт конструктора — вход в актуальный UI.
    await screen.drop_legacy_reply_keyboard(update, context)
    user_id = _user_id(update)
    ui = _fresh_ui()
    ctx.data(context)["announcement_ui"] = ui
    try:
        with SessionLocal() as session:
            details = start_announcement_wizard(
                session,
                actor_telegram_user_id=user_id,
                template_id=template_id,
                origin="personal",
                idempotency_key=ui["place"]["idempotency_key"] or uuid.uuid4().hex,
            )
    except AnnouncementNotFoundError:
        await _answer(
            update, tr("alerts.wizard.lichnyy_shablon_nayden_bolshe"), alert=True
        )
        return await _render_my_announcements(update, context)
    except (AnnouncementError, TemplateError) as exc:
        await _answer(update, str(exc), alert=True)
        return await _render_my_announcements(update, context, notice=str(exc))
    _set_details(ui, details)
    return await _enter_questions(update, context, details)


async def open_my_announcements(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Пункт меню «📂 Мои анонсы»: история и незавершённые черновики.

    Чтение только через `list_announcement_drafts` (автор = текущий
    пользователь); чужой `draft_id` в callback откроется как «не найден».
    """
    await _answer(update)
    _end_foreign_conversations(update, context)
    _clear_other_sessions(_user_id(update))
    # H1: список черновиков — экран без ввода: снимок «ждём значение» снят.
    _clear_resume(_user_id(update), ctx.data(context), ctx.chat(update).id)
    await _reanchor(update, context)
    ctx.data(context)["announcement_ui"] = _fresh_ui()
    return await _render_home(update, context)


async def _render_my_announcements(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    return await _render_home(update, context, notice=notice)


async def _delete_announcement_entry(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
) -> int:
    """
    Подтверждённое удаление записи из «Мои анонсы» (Poster 2.4 P3).

    Удаляется только строка БД (черновик или готовый анонс): Telegram-
    сообщения — экран и доставленный пост — остаются в чате. Владелец
    проверяется доменным сервисом: чужой/уже удалённый анонс неотличим от
    несуществующего и даёт понятный ответ вместо ошибки.
    """
    user_id = _user_id(update)
    try:
        with SessionLocal() as session:
            delete_announcement(
                session,
                actor_telegram_user_id=user_id,
                draft_id=draft_id,
            )
    except (AnnouncementNotFoundError, AnnouncementActorError):
        await _answer(update, tr("alerts.wizard.anons_bolshe_ne_hranitsya"), alert=True)
        return await _render_my_announcements(update, context)
    except AnnouncementError as exc:
        await _answer(update, _short(exc, 300), alert=True)
        return await _render_my_announcements(update, context)
    # Удалённая запись больше не может быть активным черновиком диалога.
    ui = _ui(context)
    if ui.get("draft_id") == draft_id:
        ui["draft_id"] = None
        ui["revision"] = None
    _clear_active(ui)
    await _answer(update, tr("alerts.wizard.anons_udalen"))
    return await _render_my_announcements(update, context)


async def _command_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    `/start` внутри активного конструктора.

    Обычный `/start` закрывает конструктор и показывает меню; deep-link
    payload (передача прав или ссылка на создание анонса площадки)
    обрабатывается по общим правилам, иначе открытие ссылки внутри диалога
    молча возвращало бы в меню (Poster 2.2 U4b).
    """
    from handlers.place_invite import open_invite_payload

    invite_state = await open_invite_payload(update, context)
    if invite_state is not None:
        return invite_state
    return await _exit_to_main_menu(update, context)


async def _command_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _close_wizard(update, context)


async def _command_new_wizard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await _reanchor(update, context)
    ctx.data(context)["announcement_ui"] = _fresh_ui()
    return await _render_home(update, context)


async def _command_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "drafts")


async def _command_places(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "places")


async def _command_bricks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "bricks")


async def _command_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "templates")


async def _command_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "admin")


async def _command_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "help")


async def handle_unknown_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _reanchor(update, context)
    await _render(
        update,
        context,
        tr("wizard.handle_unknown_command.konstruktor_anonsa_aktiven"),
        _main_menu_keyboard(),
    )


def _conversation_handler(context: ContextTypes.DEFAULT_TYPE):
    """Диалог анонсов в текущем приложении (None — приложение не собрано)."""
    application = getattr(context, "application", None)
    if application is None:
        return None
    for group in application.handlers.values():
        for handler in group:
            if not isinstance(handler, ConversationHandler):
                continue
            if getattr(handler, "name", None) == CONVERSATION_NAME:
                return handler
    return None


def remember_conversation_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: object,
) -> None:
    """
    Запоминает состояние диалога анонсов, начатого вне его entry-точки.

    «➕ Новая площадка» создаётся обработчиком справочника, а конструктор —
    отдельный ConversationHandler. Состояние выставляется явно, иначе
    следующий текст пользователя не попал бы в вопросы анонса. Ошибка не
    ломает уже нарисованный экран: пользователь продолжит кнопками.
    """
    handler = _conversation_handler(context)
    if handler is None:
        return
    try:
        key = handler._get_key(update)  # noqa: SLF001 — старт вне entry
        handler._update_state(state, key)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось открыть диалог анонса: %s", exc)


def forget_conversation_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Закрывает диалог анонсов, открытый вне его entry-точки (C3).

    Кнопки готового анонса (`anp_`) обрабатываются отдельно от
    ConversationHandler: «🏠 В меню» из панели обязана завершить и само
    состояние диалога, иначе следующий `aw_`-callback попал бы в ветку
    прежнего экрана. Ошибка не ломает уже нарисованное меню.
    """
    handler = _conversation_handler(context)
    if handler is None:
        return
    try:
        key = handler._get_key(update)  # noqa: SLF001 — закрытие вне entry
        handler._update_state(ConversationHandler.END, key)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось закрыть диалог анонса: %s", exc)


async def handle_pick_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Точки входа Poster 2.0 из списков площадок и личных шаблонов.

    Callback приходит с `pl_pick:<id>` (площадка) или `tp_pick:<id>`
    (личный шаблон). Здесь же обрабатывается меню «Мои анонсы»; все ветки
    перепроверяют доступ доменными сервисами, поэтому подделанный id
    открывает только «не найдено».
    """
    data = (ctx.query(update).data or "").split(":")
    action = data[0]
    if action == "pl_pick" and len(data) == 2 and data[1].isdigit():
        return await start_place_announcement_pick(update, context, int(data[1]))
    if action == "tp_pick" and len(data) == 2 and data[1].isdigit():
        return await start_personal_announcement_pick(update, context, int(data[1]))
    await _answer(update, EXPIRED_TEXT, alert=True)
    return ConversationHandler.END


def announcement_wizard_handlers() -> list[BaseHandler]:
    from handlers.place_invite import invite_start_handler

    text_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    photo_handler = MessageHandler(filters.PHOTO, handle_photo)
    states: dict[object, list[Any]] = {
        AW_PLACE: [
            CallbackQueryHandler(handle_place_state_callback, pattern=r"^aw_"),
            *_common_state_handlers(),
            text_handler,
            photo_handler,
        ],
        AW_TEMPLATE: [
            CallbackQueryHandler(handle_template_callback, pattern=r"^aw_"),
            *_common_state_handlers(),
            text_handler,
            photo_handler,
        ],
        AW_QUESTIONS: [
            CallbackQueryHandler(handle_question_callback, pattern=r"^aw_"),
            *_common_state_handlers(),
            text_handler,
            photo_handler,
        ],
        AW_EXTRAS: [
            CallbackQueryHandler(handle_extras_callback, pattern=r"^aw_"),
            *_common_state_handlers(),
            text_handler,
            photo_handler,
        ],
        AW_PREVIEW: [
            CallbackQueryHandler(handle_preview_callback, pattern=r"^aw_"),
            *_common_state_handlers(),
            text_handler,
            photo_handler,
        ],
    }
    conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(open_new_wizard, pattern=r"^aw_start$"),
            CommandHandler("new_wizard", open_new_wizard),
            # Poster 2.0 S2: тап по площадке/личному шаблону сразу запускает
            # конструктор — entry-точки, поэтому состояние диалога ведётся.
            CallbackQueryHandler(handle_pick_callback, pattern=r"^(pl|tp)_pick:\d+$"),
            CallbackQueryHandler(open_my_announcements, pattern=r"^my_announcements$"),
            # Poster 2.2 U4b: ссылка `/start <token>` открывает конструктор
            # сразу — только как entry-point диалога он продолжит принимать
            # ответы на вопросы.
            invite_start_handler(),
        ],
        states=states,
        fallbacks=[
            CommandHandler("start", _command_start),
            CommandHandler("cancel", _command_cancel),
            CommandHandler("new_wizard", _command_new_wizard),
            CommandHandler("drafts", _command_drafts),
            CommandHandler("places", _command_places),
            CommandHandler("bricks", _command_bricks),
            CommandHandler("templates", _command_templates),
            CommandHandler("admin", _command_admin),
            CommandHandler("help", _command_help),
            MessageHandler(filters.COMMAND, handle_unknown_command),
            CallbackQueryHandler(handle_unexpected_callback, pattern=r".*"),
        ],
        name=CONVERSATION_NAME,
    )
    return [
        conversation,
        # Poster 2.3 H1: после потери состояния диалога (рестарт службы)
        # текст/фото продолжают активный шаг. Стоит ПОСЛЕ диалога: пока его
        # состояние живо, обработчик не вызывается, а вне диалога он
        # пропускает всё, кроме личного чата с настоящим черновиком.
        AnnouncementResumeHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_resume_input
        ),
        CallbackQueryHandler(outside_wizard_callback, pattern=r"^aw_"),
    ]


async def outside_wizard_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _answer(update, EXPIRED_TEXT, alert=True)
