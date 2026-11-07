# handlers/report.py
"""
Команда `/report` — жалоба автора на работу бота (Poster 2.5.5, этап C).

Зачем отдельная команда, а не кнопка раздела: сбой чаще всего виден в
САМОМ сценарии (конструктор анонса посреди вопроса, справочник мест,
кабинет). Поэтому `/report` обязан работать из любого состояния, включая
активный `ConversationHandler` анонсов, и после отмены/отправки вернуть
человека на прежний экран и в прежний режим ввода.

Устройство формы — машина этапов в `user_states.payload` (отдельный ключ
`report_form`, resume-снимок конструктора НЕ трогается):

- `start` — вопрос «Хотите отправить репорт?»;
- `text` — ждём описание (`ReportTextHandler` перехватывает текст только
  в этом этапе, внутри диалога анонса тоже);
- `confirm` — текст сохранён, ждём «Отправить репорт»;
- `done` — отправка уже начата/выполнена. Этап выставляется ДО создания
  строки в БД: повторное нажатие не создаст второй репорт.

Формы хранятся ПО ЧАТУ (`report_form` = `{chat_id: форма}`): `/report` в
группе не снимает форму личного чата и наоборот. В личном чате форма
живёт на экране «бот как приложение»; в группе своего экрана нет, поэтому
шаг формы — отдельное сообщение бота (`message_id` в самой форме), а
экраны и клавиатуры группы не трогаются.

Снимок контекста (какой диалог/раздел был открыт) записывается один раз,
при первом `/report`. Повторный `/report` во время формы перезаписывает
только этап: пользователь не теряет экран, на который вернётся. В группе
восстанавливать нечего — снимок честно `main`.

Отправка: `create_report` → уведомления суперадминистраторам (текст и
.txt-снимок событий за час) → `record_notification` после подтверждённой
доставки. Журнал (`utils.user_events`) получает только технические
события `report.*`: ни текст жалобы, ни значения полей туда не попадают.

Просмотр `rp_view:<id>` — минимальный обработчик этапа (полный раздел
«Репорты» — этап D): проверка `is_superadmin` на каждом вызове, полный
текст с данными автора отдельным сообщением, затем .txt-снимок, без кнопок.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
import report_store
from database import SessionLocal
from permissions import is_superadmin
from utils import screen
from utils import tg_context as ctx
from utils.ui_texts import join_blocks, tr
from utils.user_events import build_events_snapshot, event_log, list_user_events

logger = logging.getLogger(__name__)

# Ключ формы в `user_states.payload`. Отдельный от
# `announcement_wizard.RESUME_PAYLOAD_KEY`: `/report` не должен ломать
# авто-resume конструктора, а конструктор — форму репорта.
REPORT_PAYLOAD_KEY = "report_form"

STAGE_START = "start"
STAGE_TEXT = "text"
STAGE_CONFIRM = "confirm"
STAGE_DONE = "done"
ACTIVE_STAGES = frozenset({STAGE_START, STAGE_TEXT, STAGE_CONFIRM, STAGE_DONE})

# Технические коды разделов, которые попадают в снимок контекста, и их
# ключи каталога. Явная таблица (а не `f"report.section.{code}"`) — чтобы
# каждый ключ было видно статической проверке каталога; неизвестный код не
# должен попадать в подпись (там его нет).
SECTION_KEYS: dict[str, str] = {
    "main": "report.section.main",
    "wizard": "report.section.wizard",
    "places": "report.section.places",
    "places_manage": "report.section.places_manage",
    "templates": "report.section.templates",
    "bricks": "report.section.bricks",
    "admin": "report.section.admin",
}
SECTION_CODES = frozenset(SECTION_KEYS)

# Предел текста жалобы. Сообщение Telegram — 4096 символов, а просмотр
# добавляет к тексту данные автора; 3500 оставляет запас и гарантирует, что
# полный текст увидит и автор, и разработчик.
MAX_REPORT_TEXT = 3500

# Имя .txt-файла: `report_YYYYMMDD_HHMMSS.txt`. Одно и то же у уведомления и
# у просмотра — файл отличается только адресатом.
FILE_PREFIX = "report"

# —— форма в `user_states.payload` —————————————————————————————————————


def _load_form(user_id: int, chat_id: int) -> dict[str, Any] | None:
    """
    Форма репорта ЭТОГО чата (None — формы нет).

    Формы хранятся по чату: `/report` можно начать в личном чате и в
    группе, и вторая запись не должна сносить первую. Внутри `payload`
    один ключ `report_form` — словарь `{chat_id: форма}`.
    """
    payload = screen.load_payload(user_id) or {}
    forms = payload.get(REPORT_PAYLOAD_KEY)
    if not isinstance(forms, dict):
        return None
    form = forms.get(str(int(chat_id)))
    return dict(form) if isinstance(form, dict) else None


def _save_form(user_id: int, chat_id: int, form: dict[str, Any] | None) -> bool:
    """
    Пишет форму ЧАТА в payload, НЕ трогая остальные отметки.

    `save_payload_key` меняет только свой ключ, поэтому resume-снимок
    конструктора и временный Rich Message справки сохраняются. Когда
    последняя форма закрыта, ключ убирается целиком.
    """
    payload = screen.load_payload(user_id) or {}
    stored = payload.get(REPORT_PAYLOAD_KEY)
    forms = dict(stored) if isinstance(stored, dict) else {}
    key = str(int(chat_id))
    if form is None:
        forms.pop(key, None)
    else:
        forms[key] = form
    return screen.save_payload_key(user_id, REPORT_PAYLOAD_KEY, forms or None)


def _stage(form: dict[str, Any] | None) -> str | None:
    value = (form or {}).get("stage")
    return value if isinstance(value, str) else None


# —— снимок контекста (куда вернуться) —————————————————————————————————


def _wizard_state(context: ContextTypes.DEFAULT_TYPE, update: Update) -> int | None:
    """
    Состояние диалога анонсов для этого чата/пользователя (None — не активен).

    Состояние живёт только в памяти `ConversationHandler`; PTB не трогает
    его, когда update обрабатывает чужой (наш) обработчик, поэтому после
    формы репорта диалог продолжается с того же шага. `PendingState`
    (обработчик с `block=False`) разворачивается до прежнего состояния.
    """
    application = getattr(context, "application", None)
    if application is None:
        return None
    from handlers.announcement_wizard import CONVERSATION_NAME

    for group in application.handlers.values():
        for handler in group:
            if not isinstance(handler, ConversationHandler):
                continue
            if getattr(handler, "name", None) != CONVERSATION_NAME:
                continue
            try:
                key = handler._get_key(update)  # noqa: SLF001 — общий разбор ключа
                state = handler._conversations.get(key)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001 — снимок не ломает форму
                logger.debug("Состояние диалога анонсов недоступно: %s", exc)
                return None
            if state is None:
                return None
            value = getattr(state, "old_state", state)
            return value if isinstance(value, int) else None
    return None


def _section_code(user_id: int, user_data: dict, *, dialog_active: bool) -> str:
    """
    Технический код экрана-источника.

    Активный диалог анонсов — всегда `wizard` (у него приоритет: диалог
    перехватывает ввод). Иначе используется тот же разбор, что и у справки
    (`help_return_target`): живые сессии разделов и resume-снимок. Возврат
    «невозможен» — честный `main`.
    """
    if dialog_active:
        return "wizard"
    from handlers.main_menu import help_return_target

    try:
        code = help_return_target(user_id, user_data)
    except Exception as exc:  # noqa: BLE001 — снимок не ломает форму
        logger.debug("Раздел-источник репорта не определён: %s", exc)
        return "main"
    return code if code in SECTION_CODES else "main"


def _context_snapshot(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> dict[str, Any]:
    """
    Минимальный снимок «где человек был»: раздел и состояние диалога.

    В группе экрана бота нет (там живёт только сообщение формы, см.
    `_show_step`) и восстанавливать нечего: раздел честно `main`, а
    состояние диалога не сохраняется.
    """
    if not _is_private(update):
        return {"section": "main", "dialog_state": None}
    user_id = ctx.user(update).id
    state = _wizard_state(context, update)
    dialog_active = state is not None
    return {
        "section": _section_code(
            user_id, ctx.data(context), dialog_active=dialog_active
        ),
        "dialog_state": state,
    }


def _section_label(code: object) -> str:
    """Подпись раздела для уведомления и строки репорта."""
    key = SECTION_KEYS.get(code) if isinstance(code, str) else None
    if key is not None:
        return tr(key)
    return str(code or "")


# —— журнал —————————————————————————————————————————————————————————————


def _event(
    user_id: int,
    action: str,
    *,
    section: object = None,
    result: str = "ok",
    detail: str | None = None,
) -> None:
    """
    Техническое событие репорта.

    `detail` — только идентификаторы, счётчики и коды: текст жалобы и
    значения полей сюда не попадают (контракт `utils.user_events`).
    """
    event_log(
        user_id,
        action,
        screen=section if isinstance(section, str) else None,
        result=result,
        detail=detail,
        session_factory=SessionLocal,
    )


# —— экран ——————————————————————————————————————————————————————————————


async def _render(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    """
    Рисует шаг формы на существующем экране (один экран «бот как приложение»).

    Только личный чат: в группе нет экрана-приложения, а правка сообщения
    экрана показала бы форму посторонним (`_show_step`).

    `save_screen_ref` (а не `save_state` конструктора) нужен потому, что
    форма хранит своё состояние отдельным ключом payload: перезапись
    `screen_message_id` не должна затрагивать resume-снимок.
    """
    user_data = ctx.data(context)
    user_id = ctx.user(update).id
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(
        context, user_data, chat_id, text, keyboard, user_id=user_id
    )
    screen.save_screen_ref(user_id, user_data, chat_id)


async def _show_group_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    form: dict[str, Any],
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    """
    Шаг формы в группе: собственное сообщение бота, экран группы не трогаем.

    Экран «бот как приложение» принадлежит личному чату (`screen_ref` в
    `user_data` пользователя), поэтому в группе форма живёт отдельным
    сообщением: его id хранится в самой форме (`message_id`) и перерисовка
    правит только его. Клавиатуры и экраны группы остаются как были.
    """
    chat_id = ctx.chat(update).id
    message_id = form.get("message_id")
    if isinstance(message_id, int) and message_id > 0:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        except TelegramError as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.info("Сообщение формы репорта не отредактировано (%s).", exc)
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    if message is not None:
        form["message_id"] = message.message_id


async def _show_stale(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    form: dict[str, Any] | None,
) -> None:
    """
    Сообщение об устаревшей форме.

    В личном чате это экран с кнопками «Составить репорт»/«Отмена» (как
    прежде). В группе без формы сообщения не плодятся: кнопка уже мёртвая,
    поэтому достаточно всплывающего ответа на нажатие.
    """
    if form is None and not _is_private(update):
        await _answer(update, tr("report.screen.stale"), alert=True)
        return
    await _show_step(
        update,
        context,
        form or {},
        tr("report.screen.stale"),
        _start_keyboard(),
    )


async def _show_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    form: dict[str, Any],
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    """
    Показывает шаг формы там, где человек её начал.

    Личный чат — экран-приложение (`_render`), группа — отдельное сообщение
    формы (`_show_group_step`). В группе шаг мог получить `message_id`
    нового сообщения: непустая форма сохраняется здесь, чтобы следующий шаг
    правил то же сообщение, а не плодил новые.
    """
    if _is_private(update):
        await _render(update, context, text, keyboard)
        return
    await _show_group_step(update, context, form, text, keyboard)
    if form:
        _save_form(ctx.user(update).id, ctx.chat(update).id, form)


async def _close_group_form(
    update: Update, context: ContextTypes.DEFAULT_TYPE, form: dict[str, Any]
) -> None:
    """
    Убирает сообщение формы в группе (форма больше не ждёт действий).

    В группе незачем оставлять клавиатуры репорта: сообщение удаляется.
    Отказ Telegram не ломает сценарий — тогда с клавиатуры хотя бы снимаются
    кнопки (best-effort), форма в payload уже снята.
    """
    chat_id = ctx.chat(update).id
    message_id = form.get("message_id")
    if not isinstance(message_id, int) or message_id <= 0:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return
    except TelegramError as exc:
        logger.info("Сообщение формы репорта не удалено (%s).", exc)
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except TelegramError as exc:
        logger.info("Клавиатура формы репорта не снята (%s).", exc)


async def _answer(
    update: Update, text: str | None = None, *, alert: bool = False
) -> None:
    """Ответ на callback: повторный `answer` Telegram считает ошибкой."""
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    try:
        await query.answer(text or "", show_alert=alert)
    except Exception as exc:  # noqa: BLE001 — ответ не ломает экран
        logger.debug("Не удалось ответить на callback репорта: %s", exc)


def _start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.report.compose"), callback_data="rp_compose"
                ),
                InlineKeyboardButton(
                    tr("buttons.report.cancel"), callback_data="rp_cancel"
                ),
            ]
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(tr("buttons.report.cancel"), callback_data="rp_cancel")]]
    )


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(tr("buttons.report.send"), callback_data="rp_send")],
            [
                InlineKeyboardButton(
                    tr("buttons.report.cancel"), callback_data="rp_cancel"
                )
            ],
        ]
    )


def _is_private(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == ChatType.PRIVATE


# —— команда `/report` —————————————————————————————————————————————————


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вход в форму репорта из любого состояния.

    Регистрируется ДО `ConversationHandler` анонсов: иначе его fallback
    `.*`/`handle_unknown_command` перехватил бы команду внутри диалога.
    Состояние диалога при этом не меняется — PTB его трогает только в
    своих обработчиках.

    Работает и в группе: там форма живёт отдельным сообщением бота
    (`_show_step`), а экран «бот как приложение» личного чата и его
    клавиатуры не трогаются вовсе.
    """
    if _is_private(update):
        await screen.reanchor_screen(update, context)
    user_id = ctx.user(update).id
    chat_id = ctx.chat(update).id
    form = _load_form(user_id, chat_id)
    stage = _stage(form)
    if form is None or stage not in ACTIVE_STAGES or stage == STAGE_DONE:
        # Первый вход: фиксируем экран-источник и режим ввода. Повторный
        # `/report` эту запись НЕ перезаписывает — иначе отмена вернула бы
        # человека не туда, где он был.
        form = {
            "stage": STAGE_START,
            "text": None,
            "report_id": None,
            "message_id": None,
            "context": _context_snapshot(update, context),
        }
        restart = False
    else:
        # Форма уже идёт: меняется только этап (снимок контекста прежний).
        form = dict(form)
        form["stage"] = STAGE_START
        restart = True
    _save_form(user_id, chat_id, form)
    _event(
        user_id,
        "report.started",
        section=(form.get("context") or {}).get("section"),
        result="restart" if restart else "ok",
    )
    await _show_step(
        update, context, form, tr("report.screen.start"), _start_keyboard()
    )
    if _is_private(update):
        await screen.drop_user_input(update, context)


async def handle_compose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Составить репорт»: переходим к вводу текста."""
    await _answer(update)
    user_id = ctx.user(update).id
    chat_id = ctx.chat(update).id
    form = _load_form(user_id, chat_id)
    if _stage(form) not in {STAGE_START, STAGE_CONFIRM}:
        await _show_stale(update, context, form)
        return
    assert form is not None
    form = dict(form)
    form["stage"] = STAGE_TEXT
    _save_form(user_id, chat_id, form)
    await _show_step(
        update, context, form, tr("report.screen.text"), _cancel_keyboard()
    )


class ReportTextHandler(MessageHandler):
    """
    Текст жалобы. Активен ТОЛЬКО в этапе `text` своей формы.

    Регистрируется в той же группе до диалога анонсов, поэтому обычный текст
    продолжает обслуживаться конструктором: `check_update` возвращает None,
    пока формы репорта нет или она ждёт не текст. Форма ищется по паре
    «пользователь + чат»: репорт, начатый в личном чате, не перехватывает
    текст в группе (и наоборот).
    """

    def check_update(self, update):
        matched = super().check_update(update)
        if not matched:
            return None
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        if user is None or chat is None:
            return None
        form = _load_form(user.id, chat.id)
        if _stage(form) != STAGE_TEXT:
            return None
        return matched


async def handle_report_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Принимает описание жалобы и показывает подтверждение."""
    user_id = ctx.user(update).id
    chat_id = ctx.chat(update).id
    form = _load_form(user_id, chat_id)
    if _stage(form) != STAGE_TEXT:
        # Форму закрыли между фильтром и вызовом: ввод не должен пропасть
        # молча — объясняем и оставляем экран формы.
        await _show_stale(update, context, form)
        return
    assert form is not None
    text = (ctx.message(update).text or "").strip()
    if not text:
        await _show_step(
            update, context, form, tr("report.screen.empty"), _cancel_keyboard()
        )
        return
    if len(text) > MAX_REPORT_TEXT:
        await _show_step(
            update,
            context,
            form,
            tr("report.screen.too_long", limit=MAX_REPORT_TEXT),
            _cancel_keyboard(),
        )
        return
    form = dict(form)
    form["stage"] = STAGE_CONFIRM
    form["text"] = text
    _save_form(user_id, chat_id, form)
    _event(
        user_id,
        "report.text_received",
        section=(form.get("context") or {}).get("section"),
        detail=f"length={len(text)}",
    )
    await _show_step(
        update, context, form, tr("report.screen.confirm"), _confirm_keyboard()
    )
    if _is_private(update):
        await screen.drop_user_input(update, context)


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Отмена» на любом этапе: снять форму и вернуть прежний экран."""
    await _answer(update)
    user_id = ctx.user(update).id
    chat_id = ctx.chat(update).id
    form = _load_form(user_id, chat_id)
    stage = _stage(form)
    if form is None or stage is None:
        await _show_stale(update, context, form)
        return
    snapshot = form.get("context") if isinstance(form.get("context"), dict) else {}
    _save_form(user_id, chat_id, None)
    _event(
        user_id,
        "report.cancelled",
        section=(snapshot or {}).get("section"),
        detail=f"stage={stage}",
    )
    if not _is_private(update):
        # Группа: своего экрана у бота там нет — закрываем только сообщение
        # формы, клавиатуры и экраны группы остаются как были.
        await _close_group_form(update, context, form)
        return
    await _restore_context(update, context, snapshot or {})


# —— отправка ———————————————————————————————————————————————————————————


def _events_text(user_id: int) -> str:
    """
    .txt-снимок событий за час + отдельная отметка пустого журнала.

    `build_events_snapshot` — единственный сборщик снимка (его формат
    закреплён тестом этапа B), поэтому пустой журнал дополняется короткой
    строкой каталога, а не переписывается здесь.
    """
    events = list_user_events(user_id, session_factory=SessionLocal)
    text = build_events_snapshot(user_id, session_factory=SessionLocal)
    if not events:
        text = join_blocks([text, tr("report.events.empty")])
    return text


def _author_label(username: str | None, display_name: str | None, user_id: int) -> str:
    """@username, иначе имя, иначе числовой ID (как в `report_store`)."""
    if username:
        return f"@{username}"
    if display_name:
        return display_name
    return str(user_id)


def _file_name(report: report_store.Report) -> str:
    return f"{FILE_PREFIX}_{report.created_at:%Y%m%d_%H%M%S}.txt"


def _file_payload(text: str) -> BytesIO:
    return BytesIO((text or "").encode("utf-8"))


async def _send_document(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    report: report_store.Report,
    text: str,
) -> None:
    """Отправляет .txt-снимок; ошибка Telegram поднимается вызывающему."""
    await context.bot.send_document(
        chat_id=chat_id,
        document=_file_payload(text),
        filename=_file_name(report),
    )


def _notification_text(report: report_store.Report) -> str:
    return join_blocks(
        [
            tr("report.notification.header"),
            tr(
                "report.notification.author",
                author_label=_author_label(
                    report.username, report.display_name, report.author_user_id
                ),
            ),
            tr(
                "report.notification.section",
                screen_label=_section_label(report.screen_label),
            ),
        ]
    )


async def _notify_superadmins(
    context: ContextTypes.DEFAULT_TYPE, report: report_store.Report, events_text: str
) -> None:
    """
    Уведомляет всех действующих суперадминистраторов (и автора, если он сам
    суперадминистратор). Получатели фиксируются на момент создания репорта.

    Сбой доставки не откатывает репорт: он уже сохранён. Недоставленный
    адресат попадает в журнал (`report.notification_failed`) с технической
    диагностикой, а не в ответ пользователю.
    """
    recipients = tuple(config.SUPERADMIN_USER_IDS)
    for recipient in recipients:
        delivered = True
        try:
            await context.bot.send_message(
                chat_id=recipient,
                text=_notification_text(report),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                tr("buttons.report.view"),
                                callback_data=f"rp_view:{report.id}",
                            )
                        ]
                    ]
                ),
            )
        except Exception as exc:  # noqa: BLE001 — репорт уже сохранён
            delivered = False
            logger.warning(
                "Уведомление о репорте %s не доставлено пользователю %s: %s",
                report.id,
                recipient,
                exc,
            )
            _event(
                recipient,
                "report.notification_failed",
                section="report",
                result="error",
                detail=(
                    f"report_id={report.id}; kind=message; "
                    f"error={type(exc).__name__}"
                ),
            )
        if delivered:
            try:
                await _send_document(context, recipient, report, events_text)
            except Exception as exc:  # noqa: BLE001 — текст уже доставлен
                # Текст уведомления получен: считаем адресата уведомлённым,
                # а отсутствие файла — диагностикой (репорт сохранён).
                logger.warning(
                    "Файл снимка репорта %s не отправлен пользователю %s: %s",
                    report.id,
                    recipient,
                    exc,
                )
                _event(
                    recipient,
                    "report.notification_failed",
                    section=report.screen_label,
                    result="error",
                    detail=(
                        f"report_id={report.id}; kind=document; "
                        f"error={type(exc).__name__}"
                    ),
                )
            try:
                with SessionLocal() as session:
                    report_store.record_notification(session, report.id, recipient)
            except Exception as exc:  # noqa: BLE001 — репорт уже сохранён
                logger.warning(
                    "Отметка уведомления репорта %s для %s не сохранена: %s",
                    report.id,
                    recipient,
                    exc,
                )


async def handle_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Отправить репорт»: сохранение, уведомления, результат и возврат."""
    user_id = ctx.user(update).id
    chat_id = ctx.chat(update).id
    form = _load_form(user_id, chat_id)
    stage = _stage(form)
    private = _is_private(update)
    if stage == STAGE_DONE:
        # Повторное нажатие (или второй клик, пока шла отправка): второй
        # репорт не создаётся. Строки в БД ещё нет (`report_id` не записан) —
        # отправка идёт прямо сейчас; иначе показываем тот же результат.
        snapshot = (form or {}).get("context")
        if (form or {}).get("report_id") is None:
            await _answer(update, tr("report.screen.sending"))
            return
        await _answer(update, tr("report.screen.sent"))
        await _show_step(update, context, form or {}, tr("report.screen.sent"), None)
        if not private:
            # Группа: форма закрывается вместе с клавиатурой, но сообщение
            # результата остаётся (в отличие от отмены) — автору нужен
            # видимый ответ. Текста жалобы в нём нет.
            _save_form(user_id, chat_id, None)
            return
        _save_form(user_id, chat_id, form)
        await _restore_context(
            update, context, snapshot if isinstance(snapshot, dict) else {}
        )
        return
    if stage != STAGE_CONFIRM or not (form or {}).get("text"):
        await _answer(update, tr("report.screen.stale"), alert=True)
        return
    assert form is not None
    # Этап `done` выставляется ДО создания строки: параллельное/повторное
    # нажатие не создаст второй репорт даже при медленной БД.
    form = dict(form)
    form["stage"] = STAGE_DONE
    _save_form(user_id, chat_id, form)
    await _answer(update)

    snapshot = form.get("context") if isinstance(form.get("context"), dict) else {}
    section = (snapshot or {}).get("section") or "main"
    body = str(form.get("text") or "")
    events_text = _events_text(user_id)
    author = ctx.user(update)
    try:
        with SessionLocal() as session:
            report = report_store.create_report(
                session,
                author_user_id=user_id,
                chat_id=chat_id,
                body=body,
                screen_label=_section_label(section),
                events_snapshot=events_text,
                username=author.username,
                display_name=author.full_name,
            )
    except Exception as exc:  # noqa: BLE001 — сбой БД не теряет ввод
        logger.exception("Репорт не сохранён: user_id=%s", user_id)
        form["stage"] = STAGE_CONFIRM
        _save_form(user_id, chat_id, form)
        _event(
            user_id,
            "report.sent",
            section=section,
            result="error",
            detail=f"error={type(exc).__name__}",
        )
        await _show_step(
            update, context, form, tr("report.screen.save_failed"), _confirm_keyboard()
        )
        return

    form["report_id"] = report.id
    _save_form(user_id, chat_id, form)
    _event(user_id, "report.sent", section=section, detail=f"report_id={report.id}")

    await _notify_superadmins(context, report, events_text)
    await _show_step(update, context, form, tr("report.screen.sent"), None)
    if not private:
        # В группе результата на «экране» нет: сообщение формы становится
        # подтверждением без кнопок, а сама форма снимается из payload.
        # Экраны и клавиатуры группы не тронуты.
        _save_form(user_id, chat_id, None)
        return
    _save_form(user_id, chat_id, form)
    await _restore_context(update, context, snapshot or {})


# —— просмотр репорта (суперадминистратор) ———————————————————————————————


def _view_text(report: report_store.Report) -> str:
    return join_blocks(
        [
            tr("report.view.title", report_id=report.id),
            tr(
                "report.view.author",
                author_label=_author_label(
                    report.username, report.display_name, report.author_user_id
                ),
                author_user_id=report.author_user_id,
            ),
            tr("report.view.section", screen_label=_section_label(report.screen_label)),
            tr(
                "report.view.created",
                created_at=f"{report.created_at:%Y-%m-%d %H:%M}",
            ),
            tr("report.view.body", body=report.body),
        ]
    )


async def handle_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    «Посмотреть» в уведомлении: полный текст репорта и .txt-снимок.

    Обработчик минимальный (полный раздел — этап D), но права проверяет на
    КАЖДОМ вызове: подделанный callback не даёт доступа к чужой жалобе.
    Кнопок под сообщениями нет — это не экран управления.
    """
    user_id = ctx.user(update).id
    if not is_superadmin(user_id):
        await _answer(
            update, tr("admin.render_phase.deystvie_dostupno_tolko"), alert=True
        )
        return
    data = (ctx.query(update).data or "").split(":")
    if len(data) != 2 or not data[1].isdigit():
        await _answer(update, tr("report.view.missing"), alert=True)
        return
    report_id = int(data[1])
    with SessionLocal() as session:
        report = report_store.get_report(session, report_id)
    if report is None:
        await _answer(update, tr("report.view.missing"), alert=True)
        return
    await _answer(update)
    chat_id = ctx.chat(update).id
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=_view_text(report),
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception as exc:  # noqa: BLE001 — ответ не должен ломать обработчик
        logger.warning("Репорт %s не показан: %s", report.id, exc)
        return
    try:
        await _send_document(context, chat_id, report, report.events_snapshot)
    except Exception as exc:  # noqa: BLE001 — текст уже показан
        logger.warning("Снимок репорта %s не отправлен: %s", report.id, exc)
    _event(
        user_id,
        "report.view_opened",
        section="report_view",
        detail=f"report_id={report.id}",
    )


# —— возврат на прежний экран ——————————————————————————————————————————


async def _restore_field_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    Возвращает клавиатуру ввода поля ДОСТАВЛЕННОГО анонса (2.5.4 §1).

    `_restore_wizard` для доставленного анонса открывает список полей:
    `_resume_current` различает только этап черновика, а ввод значения —
    отдельный режим того же экземпляра. Здесь он восстанавливается по
    resume-снимку (`mode=text`, `source=field`): экземпляр и поле
    перепроверяются доменным слоем, поэтому устаревший снимок просто
    возвращает False и срабатывает безопасный список полей.

    Служебный экран формы удаляется ДО `_show_field_input`: иначе в чате
    остались бы две конкурирующие клавиатуры. `_close_service_screen`
    снимает и resume-снимок, поэтому ввод поля сохраняет его заново.
    """
    from handlers import announcement_wizard as wizard

    user_id = ctx.user(update).id
    payload = screen.load_payload(user_id) or {}
    resume = payload.get(wizard.RESUME_PAYLOAD_KEY)
    if not isinstance(resume, dict) or resume.get("source") != "field":
        return False
    chat = ctx.chat(update)
    prepared = wizard._resume_target(  # noqa: SLF001 — общий разбор снимка
        user_id, resume, chat.id if chat is not None else None
    )
    if prepared is None:
        return False
    details, ui = prepared
    found = wizard._field_input_context(user_id, ui, details)  # noqa: SLF001
    if found is None:
        return False
    instance, field = found
    await wizard._close_service_screen(update, context)  # noqa: SLF001
    ctx.data(context)["announcement_ui"] = ui
    await wizard._show_field_input(  # noqa: SLF001
        update, context, details, instance, field
    )
    return True


async def _restore_context(
    update: Update, context: ContextTypes.DEFAULT_TYPE, snapshot: dict[str, Any]
) -> None:
    """
    Возвращает человека туда, где он был до `/report`.

    Порядок важен: сначала прежний раздел (он сам отвечает на callback и
    перерисовывает экран из своей персистентной сессии), в конце — главное
    меню. Для конструктора восстанавливается экран текущего шага из
    resume-снимка; состояния диалога мы не меняли, поэтому следующий текст
    снова попадёт в тот же вопрос. Ввод значения доставленного анонса —
    отдельный режим: он восстанавливается клавиатурой ввода поля.
    """
    code = snapshot.get("section") if isinstance(snapshot, dict) else None
    if not isinstance(code, str) or code not in SECTION_CODES:
        code = "main"
    if code == "wizard":
        from handlers.announcement_wizard import remember_conversation_state
        from handlers.main_menu import _restore_wizard, render_main_menu

        state = snapshot.get("dialog_state")
        if isinstance(state, int):
            # Состояние и так на месте; вызов страхует «ручные» пути, где
            # диалог открыт вне entry-точки (как у кнопок панели).
            remember_conversation_state(update, context, state)
        if await _restore_field_input(update, context):
            return
        if await _restore_wizard(update, context):
            return
        await render_main_menu(update, context)
        return
    if code == "places":
        from handlers.places import open_places

        await open_places(update, context)
        return
    if code == "places_manage":
        from handlers.places import open_places_manage

        await open_places_manage(update, context)
        return
    if code == "templates":
        from handlers.templates import open_templates

        await open_templates(update, context)
        return
    if code == "bricks":
        from handlers.bricks import open_bricks

        await open_bricks(update, context)
        return
    if code == "admin":
        from handlers.admin import open_admin

        await open_admin(update, context)
        return
    from handlers.main_menu import render_main_menu

    await render_main_menu(update, context)


def report_handlers() -> list[BaseHandler]:
    """
    Обработчики `/report` и просмотра репорта.

    Регистрируются в группе 0 ДО `ConversationHandler` анонсов: команда и
    её кнопки должны работать внутри диалога, а текстовый обработчик —
    перехватывать ввод только когда форма ждёт описание.
    """
    return [
        CommandHandler("report", report_command),
        CallbackQueryHandler(handle_compose, pattern=r"^rp_compose$"),
        CallbackQueryHandler(handle_cancel, pattern=r"^rp_cancel$"),
        CallbackQueryHandler(handle_send, pattern=r"^rp_send$"),
        CallbackQueryHandler(handle_view, pattern=r"^rp_view:\d+$"),
        ReportTextHandler(filters.TEXT & ~filters.COMMAND, handle_report_text),
    ]
