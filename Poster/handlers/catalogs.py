# handlers/catalogs.py
"""
Справочники кураторов и подвалов: списки, карточки, архив («Лекало» 2.6, B2).

Один и тот же набор экранов обслуживает две области хранения:

- справочник площадки (`place_id`) — для административного меню площадки;
- личный справочник владельца (`place_id is None`) — для экрана
  «📝 Создать по личному шаблону».

Область передаётся в callback (`p<id>` или `me`), а права проверяются в
`catalogs_store` на КАЖДОМ нажатии и на каждом принятом тексте: UI не
является источником полномочий. Состояние экрана (тип справочника, область,
фаза, страница, запись, шаг ввода) хранится в `user_states.payload`
отдельным ключом и переживает рестарт процесса.

Правила интерфейса из задания §7–8:

- список: строка `[подпись] [🗑]`, тап по подписи открывает карточку;
- добавление и редактирование без предпросмотра и подтверждений;
- архивирование — после подтверждения («Отмена» / «В архив»);
- восстановление — одним нажатием, без подтверждения;
- пагинация — общий механизм списков Poster (пять записей на страницу).

Тексты — только из каталога `config/ui_texts.yaml` (ключи `catalogs.*` и
`buttons.catalogs.*`).
"""

from __future__ import annotations

import logging
from typing import Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from catalogs_store import (
    MAX_FOOTER_NAME_LENGTH,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    CatalogConflictError,
    CatalogError,
    CatalogNotFoundError,
    CatalogPermissionError,
    archive_curator,
    archive_footer,
    count_curators,
    count_footers,
    create_curator,
    create_footer,
    get_curator,
    get_footer,
    list_curators,
    list_footers,
    restore_curator,
    restore_footer,
    update_curator,
    update_footer_name,
    update_footer_text,
)
from database import SessionLocal
from template_parser import (
    BrickNode,
    FormatNode,
    LinkNode,
    TemplateParseError,
    TemplateToken,
    parse_template,
)
from utils import screen
from utils import tg_context as ctx
from utils.pagination import PAGE_SIZE, nav_keyboard_for_offset
from utils.tg_html import FormattedText, capture_message_text, escape_html
from utils.ui_texts import join_blocks, tr, tr_safe

logger = logging.getLogger(__name__)

# Ключ состояния в `user_states.payload`. Отдельный от снимка конструктора,
# шаблонов, кабинета и формы репорта.
PAYLOAD_KEY = "catalogs_ui"

KIND_CURATOR = "curator"
KIND_FOOTER = "footer"
KINDS = frozenset({KIND_CURATOR, KIND_FOOTER})

PHASE_LIST = "list"
PHASE_ARCHIVE = "archive"
PHASE_CARD = "card"
PHASE_CONFIRM = "confirm"
PHASE_INPUT = "input"
PHASES = frozenset({PHASE_LIST, PHASE_ARCHIVE, PHASE_CARD, PHASE_CONFIRM, PHASE_INPUT})

STEP_CURATOR_ADD = "curator_add"
STEP_CURATOR_EDIT = "curator_edit"
STEP_FOOTER_TEXT_ADD = "footer_text_add"
STEP_FOOTER_NAME_ADD = "footer_name_add"
STEP_FOOTER_TEXT_EDIT = "footer_text_edit"
STEP_FOOTER_NAME_EDIT = "footer_name_edit"
STEPS = frozenset(
    {
        STEP_CURATOR_ADD,
        STEP_CURATOR_EDIT,
        STEP_FOOTER_TEXT_ADD,
        STEP_FOOTER_NAME_ADD,
        STEP_FOOTER_TEXT_EDIT,
        STEP_FOOTER_NAME_EDIT,
    }
)

# Подпись кнопки ограничена клиентом Telegram; длинный контакт показывает
# карточка, а не кнопка списка.
BUTTON_TEXT_LIMIT = 60

BRICK_CODE_CURATOR = "куратор"
BRICK_CODE_FOOTER = "подвал"


# --- Состояние ---------------------------------------------------------------


def _load_state(user_id: int) -> dict | None:
    """Состояние раздела из payload; битое значение считается отсутствующим."""
    payload = screen.load_payload(user_id) or {}
    state = payload.get(PAYLOAD_KEY)
    if not isinstance(state, dict):
        return None
    if state.get("kind") not in KINDS:
        return None
    place_id = state.get("place_id")
    if place_id is not None and (
        isinstance(place_id, bool) or not isinstance(place_id, int) or place_id <= 0
    ):
        return None
    if state.get("phase") not in PHASES:
        return None
    if state.get("phase") == PHASE_INPUT and state.get("step") not in STEPS:
        return None
    return state


def _save_state(user_id: int, state: dict) -> bool:
    """Пишет состояние в payload; служебные поля (`_user_id`) не сохраняются."""
    stored = {key: value for key, value in state.items() if not key.startswith("_")}
    return screen.save_payload_key(user_id, PAYLOAD_KEY, stored)


def _clear_state(user_id: int) -> None:
    screen.save_payload_key(user_id, PAYLOAD_KEY, None)


def _new_state(kind: str, place_id: int | None, *, pick_page: int = 0) -> dict:
    return {
        "kind": kind,
        "place_id": place_id,
        "phase": PHASE_LIST,
        "offset": 0,
        "entry_id": None,
        "return_phase": PHASE_LIST,
        "step": None,
        "pending": None,
        "pick_page": pick_page,
    }


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        return max(int(value), 0)
    except ValueError:
        return 0


def _last_offset(total: int) -> int:
    """Смещение последней страницы списка — куда встаёт новая запись."""
    return max(((max(total, 1) - 1) // PAGE_SIZE) * PAGE_SIZE, 0)


# --- Callback-протокол -------------------------------------------------------

SCOPE_PERSONAL = "me"


def scope_token(place_id: int | None) -> str:
    return SCOPE_PERSONAL if place_id is None else f"p{place_id}"


def open_callback(kind: str, place_id: int | None) -> str:
    """Callback кнопки входа в справочник (меню площадки / личный экран)."""
    return f"cat_open:{kind}:{scope_token(place_id)}"


def _parse_scope(token: str) -> tuple[int | None, bool]:
    if token == SCOPE_PERSONAL:
        return None, True
    if token.startswith("p") and token[1:].isdigit():
        place_id = int(token[1:])
        if place_id > 0:
            return place_id, True
    return None, False


def _parse_callback(data: str) -> tuple[str, str, int | None, str | None] | None:
    parts = data.split(":")
    if len(parts) < 3 or not parts[0].startswith("cat_"):
        return None
    action = parts[0][len("cat_") :]
    kind = parts[1]
    if kind not in KINDS or not action:
        return None
    place_id, ok = _parse_scope(parts[2])
    if not ok:
        return None
    arg = parts[3] if len(parts) > 3 else None
    return action, kind, place_id, arg


def _callback_arg_id(arg: str | None) -> int | None:
    if not arg or not arg.isdigit():
        return None
    value = int(arg)
    return value if value > 0 else None


def _callback_arg_offset(arg: str | None) -> int | None:
    if arg is None or not arg.isdigit():
        return None
    return int(arg)


# --- Видимость кнопок входа --------------------------------------------------


def _collect_brick_codes(tokens: Iterable[TemplateToken], out: set[str]) -> None:
    """Рекурсивно собирает коды кирпичиков из дерева шаблона."""
    for token in tokens:
        if isinstance(token, BrickNode):
            out.add(token.code)
        elif isinstance(token, FormatNode):
            _collect_brick_codes(token.children, out)
        elif isinstance(token, LinkNode):
            _collect_brick_codes(token.label, out)
            if isinstance(token.url, BrickNode):
                out.add(token.url.code)


def _template_codes(text: str) -> set[str]:
    """Коды кирпичиков текста шаблона; битый текст не даёт ложных совпадений."""
    try:
        tokens = parse_template(text)
    except TemplateParseError as exc:
        logger.warning("Шаблон справочников не разобран: %s", exc)
        return set()
    codes: set[str] = set()
    _collect_brick_codes(tokens, codes)
    return codes


def personal_catalog_visibility(
    templates: Iterable[object],
) -> tuple[bool, bool]:
    """
    Какие личные справочники нужны этому пользователю (§5.2).

    Кирпичик учитывается, если он есть в ЛЮБОМ личном шаблоне владельца —
    и в простом, и в расширенном тексте: расширенный текст тоже используется
    этим шаблоном (при добавлении дополнительных кирпичиков). Конкретный
    выбранный шаблон не требуется.
    """
    curators = footers = False
    for template in templates:
        version = getattr(template, "current_version", None)
        if version is None:  # pragma: no cover — у TemplateDetails версия есть
            continue
        texts = [version.content]
        extended = getattr(version, "extended_content", None)
        if isinstance(extended, str) and extended:
            texts.append(extended)
        for text in texts:
            codes = _template_codes(text)
            if BRICK_CODE_CURATOR in codes:
                curators = True
            if BRICK_CODE_FOOTER in codes:
                footers = True
    return curators, footers


def place_catalog_visibility(
    session, telegram_user_id: int, place_id: int
) -> tuple[bool, bool]:
    """
    Какие справочники показывает меню площадки (§6).

    Формула: кирпичик есть в ПРОСТОМ тексте ИЛИ (расширенный вариант включён
    И кирпичик есть в расширенном тексте). Наличие определяется штатным
    разбором шаблона, а не поиском подстроки. Скрытие кнопок записи не
    удаляет.
    """
    # Ленивый импорт: `template_store` тянет компилятор шаблонов, а этот
    # модуль импортируется из меню, которому он не нужен.
    from template_store import get_standard_template

    try:
        template = get_standard_template(session, telegram_user_id, place_id)
    except Exception as exc:  # noqa: BLE001 — меню важнее кнопки раздела
        logger.info("Шаблон площадки для кнопок справочников недоступен: %s", exc)
        return False, False
    version = template.current_version
    texts = [version.content]
    if template.extended_active and isinstance(version.extended_content, str):
        texts.append(version.extended_content)
    codes: set[str] = set()
    for text in texts:
        codes.update(_template_codes(text))
    return BRICK_CODE_CURATOR in codes, BRICK_CODE_FOOTER in codes


# --- Общие помощники экрана --------------------------------------------------


def _ivory_plain(text: str) -> str:
    """Короткая plain-подпись кнопки без переносов и разметки."""
    collapsed = " ".join(str(text).split())
    return collapsed or str(text)


def _short(text: str, limit: int = BUTTON_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


def _title(kind: str, *, archive: bool = False) -> str:
    if kind == KIND_CURATOR:
        return tr(
            "catalogs.curators.archive_title" if archive else "catalogs.curators.title"
        )
    return tr("catalogs.footers.archive_title" if archive else "catalogs.footers.title")


def _empty_text(kind: str, *, archive: bool = False) -> str:
    if kind == KIND_CURATOR:
        return tr(
            "catalogs.curators.archive_empty" if archive else "catalogs.curators.empty"
        )
    return tr("catalogs.footers.archive_empty" if archive else "catalogs.footers.empty")


def _home_row(kind: str, place_id: int | None) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            tr("buttons.nav.back"),
            callback_data=f"cat_home:{kind}:{scope_token(place_id)}",
        )
    ]


def _entries(session, state: dict, *, kind: str, status: str) -> tuple[list, int, int]:
    """Страница записей области: (items, total, offset)."""
    place_id = state["place_id"]
    user_id = int(state["_user_id"])
    offset = _int_or_zero(state.get("offset"))
    page = offset // PAGE_SIZE
    if kind == KIND_CURATOR:
        curators = list_curators(
            session,
            actor_telegram_user_id=user_id,
            place_id=place_id,
            status=status,
            page=page,
            page_size=PAGE_SIZE,
        )
        return list(curators.items), curators.total, curators.page * PAGE_SIZE
    footers = list_footers(
        session,
        actor_telegram_user_id=user_id,
        place_id=place_id,
        status=status,
        page=page,
        page_size=PAGE_SIZE,
    )
    return list(footers.items), footers.total, footers.page * PAGE_SIZE


def _count(session, user_id: int, place_id: int | None, *, kind: str, status: str):
    if kind == KIND_CURATOR:
        return count_curators(
            session,
            actor_telegram_user_id=user_id,
            place_id=place_id,
            status=status,
        )
    return count_footers(
        session,
        actor_telegram_user_id=user_id,
        place_id=place_id,
        status=status,
    )


def _entry_label(view, kind: str) -> str:
    if kind == KIND_CURATOR:
        return _short(_ivory_plain(view.label()))
    return _short(_ivory_plain(view.name))


def _list_content(session, state: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Активный список: `[значение] [🗑]`, пагинация, добавление, архив."""
    kind = state["kind"]
    place_id = state["place_id"]
    token = scope_token(place_id)
    items, total, offset = _entries(session, state, kind=kind, status=STATUS_ACTIVE)
    state["offset"] = offset
    text = _title(kind)
    if total == 0:
        text = join_blocks([text, _empty_text(kind)])

    rows: list[list[InlineKeyboardButton]] = []
    for view in items:
        rows.append(
            [
                InlineKeyboardButton(
                    _entry_label(view, kind),
                    callback_data=f"cat_card:{kind}:{token}:{view.id}",
                ),
                InlineKeyboardButton(
                    tr("buttons.catalogs.delete"),
                    callback_data=f"cat_del:{kind}:{token}:{view.id}",
                ),
            ]
        )
    nav = nav_keyboard_for_offset(
        offset,
        total,
        f"cat_page:{kind}:{token}",
        page_size=PAGE_SIZE,
    )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                tr(
                    "buttons.catalogs.add_curator"
                    if kind == KIND_CURATOR
                    else "buttons.catalogs.add_footer"
                ),
                callback_data=f"cat_add:{kind}:{token}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.catalogs.archive"),
                callback_data=f"cat_arch:{kind}:{token}",
            )
        ]
    )
    rows.append(_home_row(kind, place_id))
    return text, InlineKeyboardMarkup(rows)


def _archive_content(session, state: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Архив: те же записи с восстановлением одним нажатием."""
    kind = state["kind"]
    place_id = state["place_id"]
    token = scope_token(place_id)
    items, total, offset = _entries(session, state, kind=kind, status=STATUS_ARCHIVED)
    state["offset"] = offset
    text = _title(kind, archive=True)
    if total == 0:
        text = join_blocks([text, _empty_text(kind, archive=True)])

    rows: list[list[InlineKeyboardButton]] = []
    for view in items:
        rows.append(
            [
                InlineKeyboardButton(
                    _entry_label(view, kind),
                    callback_data=f"cat_card:{kind}:{token}:{view.id}",
                ),
                InlineKeyboardButton(
                    tr("buttons.catalogs.restore"),
                    callback_data=f"cat_rest:{kind}:{token}:{view.id}",
                ),
            ]
        )
    nav = nav_keyboard_for_offset(
        offset,
        total,
        f"cat_apage:{kind}:{token}",
        page_size=PAGE_SIZE,
    )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.nav.back"),
                callback_data=f"cat_back:{kind}:{token}",
            )
        ]
    )
    return text, InlineKeyboardMarkup(rows)


def _card_content(session, state: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка записи: полное значение и действия редактирования."""
    kind = state["kind"]
    place_id = state["place_id"]
    token = scope_token(place_id)
    user_id = state["_user_id"]
    entry_id = state["entry_id"]
    archived = False
    if kind == KIND_CURATOR:
        curator = get_curator(
            session,
            actor_telegram_user_id=user_id,
            place_id=place_id,
            entry_id=entry_id,
        )
        text = str(curator.html)
        archived = curator.status == STATUS_ARCHIVED
    else:
        footer = get_footer(
            session,
            actor_telegram_user_id=user_id,
            place_id=place_id,
            entry_id=entry_id,
        )
        text = tr_safe(
            "catalogs.footer.card_html",
            name=escape_html(footer.name),
            text=str(footer.html),
        )
        archived = footer.status == STATUS_ARCHIVED
    # Архивированная запись открывается из архива только на чтение: правка
    # архивных запрещена хранилищем, поэтому вместо неё — восстановление.
    if archived:
        edit_rows = [
            [
                InlineKeyboardButton(
                    tr("buttons.catalogs.restore"),
                    callback_data=f"cat_rest:{kind}:{token}:{entry_id}",
                )
            ]
        ]
    elif kind == KIND_CURATOR:
        edit_rows = [
            [
                InlineKeyboardButton(
                    tr("buttons.catalogs.edit"),
                    callback_data=f"cat_edit:{kind}:{token}",
                )
            ]
        ]
    else:
        edit_rows = [
            [
                InlineKeyboardButton(
                    tr("buttons.catalogs.edit_text"),
                    callback_data=f"cat_etext:{kind}:{token}",
                ),
                InlineKeyboardButton(
                    tr("buttons.catalogs.edit_name"),
                    callback_data=f"cat_ename:{kind}:{token}",
                ),
            ]
        ]
    keyboard = InlineKeyboardMarkup(
        [
            *edit_rows,
            [
                InlineKeyboardButton(
                    tr("buttons.nav.back"),
                    callback_data=f"cat_back:{kind}:{token}",
                )
            ],
        ]
    )
    return text, keyboard


def _confirm_content(session, state: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Подтверждение архивирования: «Отмена» / «В архив»."""
    kind = state["kind"]
    place_id = state["place_id"]
    token = scope_token(place_id)
    user_id = state["_user_id"]
    entry_id = state["entry_id"]
    if kind == KIND_CURATOR:
        curator = get_curator(
            session,
            actor_telegram_user_id=user_id,
            place_id=place_id,
            entry_id=entry_id,
            include_archived=False,
        )
        text = tr(
            "catalogs.curators.archive_confirm", value=_ivory_plain(curator.label())
        )
    else:
        footer = get_footer(
            session,
            actor_telegram_user_id=user_id,
            place_id=place_id,
            entry_id=entry_id,
            include_archived=False,
        )
        text = tr("catalogs.footers.archive_confirm", value=footer.name)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.catalogs.cancel"),
                    callback_data=f"cat_back:{kind}:{token}",
                ),
                InlineKeyboardButton(
                    tr("buttons.catalogs.to_archive"),
                    callback_data=f"cat_delyes:{kind}:{token}:{entry_id}",
                ),
            ]
        ]
    )
    return text, keyboard


def _input_content(state: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Подсказка шага ввода и «Отмена»."""
    step = state["step"]
    key = {
        STEP_CURATOR_ADD: "catalogs.curator.add_prompt",
        STEP_CURATOR_EDIT: "catalogs.curator.edit_prompt",
        STEP_FOOTER_TEXT_ADD: "catalogs.footer.text_prompt",
        STEP_FOOTER_NAME_ADD: "catalogs.footer.name_prompt",
        STEP_FOOTER_TEXT_EDIT: "catalogs.footer.text_edit_prompt",
        STEP_FOOTER_NAME_EDIT: "catalogs.footer.name_edit_prompt",
    }[step]
    token = scope_token(state["place_id"])
    return tr(key), InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.catalogs.cancel"),
                    callback_data=f"cat_cancel:{state['kind']}:{token}",
                )
            ]
        ]
    )


def _content(state: dict) -> tuple[str, InlineKeyboardMarkup]:
    if state["phase"] == PHASE_INPUT:
        return _input_content(state)
    session = SessionLocal()
    try:
        if state["phase"] == PHASE_LIST:
            return _list_content(session, state)
        if state["phase"] == PHASE_ARCHIVE:
            return _archive_content(session, state)
        if state["phase"] == PHASE_CARD:
            return _card_content(session, state)
        return _confirm_content(session, state)
    finally:
        session.close()


async def _show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    *,
    notice: str | None = None,
    drop_input: bool = False,
) -> bool:
    """Собирает экран по состоянию, сохраняет его и только потом чистит ввод."""
    user_id = ctx.user(update).id
    text, keyboard = _content(state)
    body = f"{notice}\n\n{text}" if notice else text
    saved = _save_state(user_id, state)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(
        context, user_data, chat_id, body, keyboard, user_id=user_id
    )
    screen.save_screen_ref(user_id, user_data, chat_id)
    if saved and drop_input:
        await screen.drop_user_input(update, context)
    return saved


async def _answer_stale(update: Update) -> None:
    query = ctx.query(update)
    await query.answer(tr("catalogs.stale"), show_alert=True)


def _catalog_error_text(exc: BaseException) -> str:
    if isinstance(exc, CatalogPermissionError):
        return tr("catalogs.denied")
    return tr("catalogs.stale")


def _check_access(session, user_id: int, place_id: int | None, kind: str) -> None:
    """Пробное чтение счётчика: та же проверка прав, что и у операций."""
    _count(session, user_id, place_id, kind=kind, status=STATUS_ACTIVE)


# --- Сценарии ----------------------------------------------------------------


async def _open(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: str,
    place_id: int | None,
) -> None:
    user_id = ctx.user(update).id
    query = ctx.query(update)
    session = SessionLocal()
    try:
        _check_access(session, user_id, place_id, kind)
    except (CatalogPermissionError, CatalogNotFoundError, CatalogError) as exc:
        await query.answer(_catalog_error_text(exc), show_alert=True)
        return
    finally:
        session.close()

    pick_page = 0
    if place_id is None:
        # «◀️ Назад» из личного справочника возвращает на тот же экран
        # «Создать по личному шаблону» — сохраняем его страницу.
        from template_state import load_template_session

        template_state = load_template_session(user_id)
        if isinstance(template_state, dict) and template_state.get("phase") == "pick":
            pick_page = _int_or_zero(template_state.get("page"))

    state = _new_state(kind, place_id, pick_page=pick_page)
    state["_user_id"] = user_id
    await query.answer()
    await _show(update, context, state, drop_input=True)


async def _ensure_state(
    update: Update,
    *,
    kind: str,
    place_id: int | None,
) -> dict | None:
    """
    Читает состояние и проверяет, что кнопка принадлежит этому экрану.

    Права проверяются отдельно — операцией: UI не источник полномочий.
    """
    user_id = ctx.user(update).id
    state = _load_state(user_id)
    if state is None or state.get("kind") != kind or state.get("place_id") != place_id:
        await _answer_stale(update)
        return None
    state["_user_id"] = user_id
    return state


def _list_phase_allowed(state: dict) -> bool:
    return state.get("phase") in {PHASE_LIST, PHASE_ARCHIVE}


async def handle_catalog_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    parsed = _parse_callback(query.data or "")
    if parsed is None:
        await _answer_stale(update)
        return
    action, kind, place_id, arg = parsed

    if action == "open":
        await _open(update, context, kind, place_id)
        return

    state = await _ensure_state(update, kind=kind, place_id=place_id)
    if state is None:
        return
    user_id = state["_user_id"]

    # Навигация и действия — по правилам текущей фазы.
    try:
        if action in {"page", "apage"}:
            offset = _callback_arg_offset(arg)
            if offset is None or not _list_phase_allowed(state):
                await _answer_stale(update)
                return
            state["phase"] = PHASE_ARCHIVE if action == "apage" else PHASE_LIST
            state["offset"] = offset
            await query.answer()
            await _show(update, context, state)
            return

        if action == "arch":
            if state.get("phase") != PHASE_LIST:
                await _answer_stale(update)
                return
            state["phase"] = PHASE_ARCHIVE
            state["offset"] = 0
            await query.answer()
            await _show(update, context, state)
            return

        if action == "back":
            if state.get("phase") not in {PHASE_ARCHIVE, PHASE_CARD, PHASE_CONFIRM}:
                await _answer_stale(update)
                return
            state["phase"] = state.get("return_phase") or PHASE_LIST
            if state["phase"] not in {PHASE_LIST, PHASE_ARCHIVE}:
                state["phase"] = PHASE_LIST
            state.pop("entry_id", None)
            await query.answer()
            await _show(update, context, state)
            return

        if action == "home":
            await query.answer()
            await _go_home(update, context, state)
            return

        if action == "card":
            entry_id = _callback_arg_id(arg)
            if entry_id is None or not _list_phase_allowed(state):
                await _answer_stale(update)
                return
            state["return_phase"] = state["phase"]
            state["phase"] = PHASE_CARD
            state["entry_id"] = entry_id
            await query.answer()
            await _show(update, context, state)
            return

        if action == "add":
            if state.get("phase") != PHASE_LIST:
                await _answer_stale(update)
                return
            _start_add(state)
            await query.answer()
            await _show(update, context, state, drop_input=True)
            return

        if action == "del":
            entry_id = _callback_arg_id(arg)
            if entry_id is None or state.get("phase") != PHASE_LIST:
                await _answer_stale(update)
                return
            state["phase"] = PHASE_CONFIRM
            state["entry_id"] = entry_id
            await query.answer()
            await _show(update, context, state)
            return

        if action == "delyes":
            entry_id = _callback_arg_id(arg)
            if entry_id is None or state.get("phase") != PHASE_CONFIRM:
                await _answer_stale(update)
                return
            session = SessionLocal()
            try:
                if kind == KIND_CURATOR:
                    archive_curator(
                        session,
                        actor_telegram_user_id=user_id,
                        place_id=place_id,
                        entry_id=entry_id,
                    )
                else:
                    archive_footer(
                        session,
                        actor_telegram_user_id=user_id,
                        place_id=place_id,
                        entry_id=entry_id,
                    )
                _check_access(session, user_id, place_id, kind)
            except (CatalogPermissionError, CatalogNotFoundError, CatalogError) as exc:
                await query.answer(_catalog_error_text(exc), show_alert=True)
                return
            finally:
                session.close()
            state["phase"] = PHASE_LIST
            state.pop("entry_id", None)
            state["offset"] = _clamped_offset(
                user_id, place_id, kind, STATUS_ACTIVE, state.get("offset")
            )
            await query.answer()
            await _show(update, context, state)
            return

        if action == "rest":
            entry_id = _callback_arg_id(arg)
            if entry_id is None or state.get("phase") != PHASE_ARCHIVE:
                await _answer_stale(update)
                return
            session = SessionLocal()
            try:
                if kind == KIND_CURATOR:
                    restore_curator(
                        session,
                        actor_telegram_user_id=user_id,
                        place_id=place_id,
                        entry_id=entry_id,
                    )
                else:
                    restore_footer(
                        session,
                        actor_telegram_user_id=user_id,
                        place_id=place_id,
                        entry_id=entry_id,
                    )
                _check_access(session, user_id, place_id, kind)
            except (CatalogPermissionError, CatalogNotFoundError, CatalogError) as exc:
                await query.answer(_catalog_error_text(exc), show_alert=True)
                return
            finally:
                session.close()
            state["offset"] = _clamped_offset(
                user_id, place_id, kind, STATUS_ARCHIVED, state.get("offset")
            )
            await query.answer()
            await _show(update, context, state)
            return

        if action in {"edit", "etext", "ename"}:
            entry_id = state.get("entry_id")
            if state.get("phase") != PHASE_CARD or not isinstance(entry_id, int):
                await _answer_stale(update)
                return
            if action == "edit":
                state["step"] = (
                    STEP_CURATOR_EDIT if kind == KIND_CURATOR else STEP_FOOTER_TEXT_EDIT
                )
            elif action == "etext":
                state["step"] = STEP_FOOTER_TEXT_EDIT
            else:
                state["step"] = STEP_FOOTER_NAME_EDIT
            state["phase"] = PHASE_INPUT
            await query.answer()
            await _show(update, context, state, drop_input=True)
            return

        if action == "cancel":
            if state.get("phase") != PHASE_INPUT:
                await _answer_stale(update)
                return
            await query.answer()
            await _cancel_input(update, context, state)
            return

        await _answer_stale(update)
    except (CatalogPermissionError, CatalogNotFoundError, CatalogError) as exc:
        await query.answer(_catalog_error_text(exc), show_alert=True)
    finally:
        state.pop("_user_id", None)


def _clamped_offset(
    user_id: int,
    place_id: int | None,
    kind: str,
    status: str,
    offset: object,
) -> int:
    """Смещение, не уводящее в пустую страницу после архива/восстановления."""
    session = SessionLocal()
    try:
        total = _count(session, user_id, place_id, kind=kind, status=status)
    finally:
        session.close()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    safe = min(max(_int_or_zero(offset) // PAGE_SIZE, 0), pages - 1)
    return safe * PAGE_SIZE


def _start_add(state: dict) -> None:
    state["phase"] = PHASE_INPUT
    state["entry_id"] = None
    state["pending"] = None
    state["step"] = (
        STEP_CURATOR_ADD if state["kind"] == KIND_CURATOR else STEP_FOOTER_TEXT_ADD
    )


async def _cancel_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict
) -> None:
    step = state.get("step")
    if step in {STEP_CURATOR_EDIT, STEP_FOOTER_TEXT_EDIT, STEP_FOOTER_NAME_EDIT}:
        state["phase"] = PHASE_CARD
    else:
        state["phase"] = PHASE_LIST
        state.pop("entry_id", None)
    state["step"] = None
    state["pending"] = None
    await _show(update, context, state)


async def _go_home(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict
) -> None:
    """«◀️ Назад» из списка: меню площадки или «Создать по личному шаблону»."""
    user_id = ctx.user(update).id
    place_id = state["place_id"]
    pick_page = _int_or_zero(state.get("pick_page"))
    _clear_state(user_id)
    if place_id is not None:
        from admin_state import load_admin_session
        from handlers.admin import _render_phase as render_admin_phase

        admin_state = load_admin_session(user_id)
        if not isinstance(admin_state, dict):
            admin_state = {"phase": "places", "page": 0}
        admin_state["phase"] = "place"
        admin_state["place_id"] = place_id
        await render_admin_phase(update, context, admin_state)
        return
    from handlers.templates import _new_pick
    from handlers.templates import _render_phase as render_template_phase

    await render_template_phase(update, context, _new_pick(pick_page))


# --- Текстовый ввод ----------------------------------------------------------


def _pending_text(pending: object) -> FormattedText:
    """Расширенный текст подвала из снимка шага (plain + безопасный HTML)."""
    if not isinstance(pending, dict):
        return FormattedText("", "")
    plain = pending.get("plain")
    markup = pending.get("html")
    return FormattedText(
        plain if isinstance(plain, str) else "",
        markup if isinstance(markup, str) else "",
    )


def _pending_dict(text: FormattedText) -> dict[str, str]:
    return {"plain": str(text), "html": text.html}


def _footer_name(value: str) -> str:
    return " ".join(value.split())


async def handle_catalog_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Принимает текст шага ввода справочника (только в фазе `input`)."""
    user_id = ctx.user(update).id
    state = _load_state(user_id)
    if state is None or state.get("phase") != PHASE_INPUT:
        return
    state["_user_id"] = user_id
    step = state["step"]
    kind = state["kind"]
    place_id = state["place_id"]
    token = scope_token(place_id)
    capture = capture_message_text(ctx.message(update))
    plain = str(capture)
    stripped = plain.strip()

    # Пустой ввод не доходит до хранилища: показываем понятную подсказку.
    if not stripped:
        key = {
            STEP_CURATOR_ADD: "catalogs.errors.empty_curator",
            STEP_CURATOR_EDIT: "catalogs.errors.empty_curator",
            STEP_FOOTER_TEXT_ADD: "catalogs.errors.empty_footer_text",
            STEP_FOOTER_TEXT_EDIT: "catalogs.errors.empty_footer_text",
            STEP_FOOTER_NAME_ADD: "catalogs.errors.empty_footer_name",
            STEP_FOOTER_NAME_EDIT: "catalogs.errors.empty_footer_name",
        }[step]
        await _show(update, context, state, notice=tr(key), drop_input=True)
        return

    if step in {STEP_FOOTER_NAME_ADD, STEP_FOOTER_NAME_EDIT}:
        if len(_footer_name(stripped)) > MAX_FOOTER_NAME_LENGTH:
            await _show(
                update,
                context,
                state,
                notice=tr(
                    "catalogs.errors.footer_name_long", limit=MAX_FOOTER_NAME_LENGTH
                ),
                drop_input=True,
            )
            return

    session = SessionLocal()
    try:
        if step == STEP_CURATOR_ADD:
            create_curator(
                session,
                actor_telegram_user_id=user_id,
                place_id=place_id,
                value=capture,
            )
            total = count_curators(
                session,
                actor_telegram_user_id=user_id,
                place_id=place_id,
                status=STATUS_ACTIVE,
            )
            state["phase"] = PHASE_LIST
            state["step"] = None
            state["offset"] = _last_offset(total)
        elif step == STEP_CURATOR_EDIT:
            entry_id = state.get("entry_id")
            if not isinstance(entry_id, int):
                await _answer_stale(update)
                return
            update_curator(
                session,
                actor_telegram_user_id=user_id,
                place_id=place_id,
                entry_id=entry_id,
                value=capture,
            )
            state["phase"] = PHASE_CARD
            state["step"] = None
        elif step == STEP_FOOTER_TEXT_ADD:
            state["pending"] = _pending_dict(capture)
            state["step"] = STEP_FOOTER_NAME_ADD
        elif step == STEP_FOOTER_NAME_ADD:
            create_footer(
                session,
                actor_telegram_user_id=user_id,
                place_id=place_id,
                name=capture,
                text=_pending_text(state.get("pending")),
            )
            total = count_footers(
                session,
                actor_telegram_user_id=user_id,
                place_id=place_id,
                status=STATUS_ACTIVE,
            )
            state["phase"] = PHASE_LIST
            state["step"] = None
            state["pending"] = None
            state["offset"] = _last_offset(total)
        elif step == STEP_FOOTER_TEXT_EDIT:
            entry_id = state.get("entry_id")
            if not isinstance(entry_id, int):
                await _answer_stale(update)
                return
            update_footer_text(
                session,
                actor_telegram_user_id=user_id,
                place_id=place_id,
                entry_id=entry_id,
                text=capture,
            )
            state["phase"] = PHASE_CARD
            state["step"] = None
        else:
            entry_id = state.get("entry_id")
            if not isinstance(entry_id, int):
                await _answer_stale(update)
                return
            update_footer_name(
                session,
                actor_telegram_user_id=user_id,
                place_id=place_id,
                entry_id=entry_id,
                name=capture,
            )
            state["phase"] = PHASE_CARD
            state["step"] = None
    except CatalogPermissionError as exc:
        await _show_denied(update, context, state)
        logger.info("Справочник %s:%s недоступен: %s", kind, token, exc)
        return
    except CatalogConflictError:
        await _show(
            update,
            context,
            state,
            notice=tr("catalogs.errors.footer_name_taken"),
            drop_input=True,
        )
        return
    except (CatalogNotFoundError, CatalogError) as exc:
        await _show(
            update,
            context,
            state,
            notice=tr("catalogs.errors.save_failed"),
            drop_input=True,
        )
        logger.info("Текст справочника не сохранён: %s", exc)
        return
    finally:
        session.close()

    # `_user_id` живёт только в памяти запроса: `_save_state` вырезает
    # служебные поля, поэтому в payload он не попадает.
    await _show(update, context, state, drop_input=True)


async def _show_denied(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict
) -> None:
    """Права пропали между экраном и вводом: закрываем сценарий."""
    user_id = ctx.user(update).id
    state.pop("_user_id", None)
    _clear_state(user_id)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    await screen.render_screen(
        context,
        user_data,
        ctx.chat(update).id,
        tr("catalogs.denied"),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
                    )
                ]
            ]
        ),
        user_id=user_id,
    )
    await screen.drop_user_input(update, context)


class CatalogTextHandler(MessageHandler):
    """Текст принимается только в фазе ввода справочника."""

    def check_update(self, update):
        matched = super().check_update(update)
        if not matched:
            return None
        user = getattr(update, "effective_user", None)
        if user is None:
            return None
        state = _load_state(user.id)
        if state is None or state.get("phase") != PHASE_INPUT:
            return None
        return matched


def catalog_handlers() -> list[BaseHandler]:
    """Обработчики справочников: один callback-диспетчер и приём текста."""
    return [
        CallbackQueryHandler(handle_catalog_callback, pattern=r"^cat_"),
        CatalogTextHandler(filters.TEXT & ~filters.COMMAND, handle_catalog_text),
    ]


__all__ = [
    "KIND_CURATOR",
    "KIND_FOOTER",
    "catalog_handlers",
    "open_callback",
    "personal_catalog_visibility",
    "place_catalog_visibility",
    "scope_token",
]
