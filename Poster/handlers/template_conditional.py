# handlers/template_conditional.py
"""
Условные части шаблонов: Telegram-интерфейс (Poster 2.5, spec §39–45).

Путь автора: карточка шаблона → «🧩 Условные части» → выбор кирпичика →
выбор скрываемого фрагмента → предпросмотр → сохранение. Отдельный экран
списка правил позволяет открыть, изменить и удалить правило, а также
показывает правила, требующие перенастройки после правки текста.

Модуль намеренно не меняет доменный слой: правила читает и пишет
`conditional_parts`, шаблон и версии — `template_store`. Здесь только
экраны, состояние навигации и проверки, которые нужны ДО сохранения.

Состояние маршрута живёт в `context.user_data["cond_parts"]` (это
интерфейсная навигация, а не доменные данные), фаза шаблонной сессии
остаётся `card`: редактор текста и правила не должны мешать друг другу.
Признак «ждём текст собственного фрагмента» дублируется в шаблонной сессии
(`cond_input`), потому что фильтр текстового обработчика видит только
состояние из БД — `user_data` в `check_update` недоступен.

Callback-префикс `tp_cond*` регистрируется в `templates_handlers()` ДО
общего fallback `^tp_`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import utils.tg_context as ctx
from conditional_parts import (
    SECTION_CONTENT,
    SECTION_EXTENDED_CONTENT,
    STATUS_STALE,
    create_conditional_part,
    delete_conditional_part,
    list_conditional_parts,
    update_conditional_part,
)
from handlers import templates as tp
from template_compiler import ConditionalRule, apply_conditional_parts
from template_state import load_template_session
from utils.ui_texts import tr

logger = logging.getLogger(__name__)

# Ключ состояния маршрута (§40). Живёт только в user_data.
COND_KEY = "cond_parts"
# Признак текстового ввода в шаблонной сессии (виден фильтру обработчика).
INPUT_FLAG = "cond_input"

STEP_BRICKS = "bricks"
STEP_FRAGMENT = "fragment"
STEP_CUSTOM = "custom"
STEP_PROPOSAL = "proposal"
STEP_PREVIEW = "preview"
STEP_LIST = "list"
STEP_RULE = "rule"
STEP_DELETE = "delete"

# Символы, которые автор считает «разделителем» вокруг кирпичика (§38/§42).
# Квадратные скобки сюда не входят: иначе фрагмент «съел» бы соседний токен.
SEPARATOR_CHARS = frozenset(" \t\u00a0–—-‐‑.,;:!?…()«»\"'·•—")
# Справа в фрагмент берутся только закрывающие знаки: пробел и тире справа
# чаще относятся к следующему смыслу («[конец] (снова)»), а не к кирпичику.
CLOSING_CHARS = frozenset(")]».,;:!?…")
CONTEXT_RADIUS = 30
BACK_TARGETS = frozenset(
    {"card", "bricks", "fragment", "proposal", "preview", "list", "rule"}
)


@dataclass(frozen=True)
class BrickEntry:
    """Одно вхождение кирпичика в текст конкретной секции."""

    section: str
    brick_id: int
    code: str
    title: str
    occurrence: int
    start: int
    context: str

    @property
    def is_extended(self) -> bool:
        return self.section == SECTION_EXTENDED_CONTENT


# --- Состояние маршрута ------------------------------------------------------


def _state(user_data: dict) -> dict:
    value = user_data.get(COND_KEY)
    return value if isinstance(value, dict) else {}


def _remember(context: ContextTypes.DEFAULT_TYPE, cond: dict) -> None:
    ctx.data(context)[COND_KEY] = cond


def _forget(context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.data(context).pop(COND_KEY, None)


def _new_state(template_id: int) -> dict:
    return {
        "template_id": template_id,
        "step": STEP_BRICKS,
        "section": SECTION_CONTENT,
        "brick_id": None,
        "code": None,
        "occurrence": 1,
        "fragment": None,
        "part_id": None,
        "error": None,
    }


# --- Текст шаблона и вхождения ----------------------------------------------


def _section_text(template: tp.TemplateDetails, section: str) -> str:
    version = template.current_version
    if section == SECTION_EXTENDED_CONTENT:
        return version.extended_content or ""
    return version.content


def _token_occurrences(text: str, code: str) -> list[int]:
    token = f"[{code}]"
    positions: list[int] = []
    start = text.find(token)
    while start >= 0:
        positions.append(start)
        start = text.find(token, start + 1)
    return positions


def _context(text: str, start: int, end: int) -> str:
    """Окружающий текст вокруг вхождения: ±30 символов, переводы строк видны."""
    left = text[max(0, start - CONTEXT_RADIUS) : start]
    right = text[end : end + CONTEXT_RADIUS]
    return (left + text[start:end] + right).replace("\n", " ⏎ ")


def _entries_for_template(template: tp.TemplateDetails) -> list[BrickEntry]:
    """Кирпичики, реально встречающиеся в тексте (§40, шаг 1)."""
    entries: list[BrickEntry] = []
    for section in (SECTION_CONTENT, SECTION_EXTENDED_CONTENT):
        text = _section_text(template, section)
        if not text:
            continue
        for binding in template.current_version.bindings:
            if not binding.code:
                continue
            for number, start in enumerate(
                _token_occurrences(text, binding.code), start=1
            ):
                end = start + len(binding.code) + 2
                entries.append(
                    BrickEntry(
                        section=section,
                        brick_id=binding.brick_id,
                        code=binding.code,
                        title=binding.title,
                        occurrence=number,
                        start=start,
                        context=_context(text, start, end),
                    )
                )
    return entries


def _find_entry(
    template: tp.TemplateDetails, section: str, brick_id: int, occurrence: int
) -> BrickEntry | None:
    for entry in _entries_for_template(template):
        if (
            entry.section == section
            and entry.brick_id == brick_id
            and entry.occurrence == occurrence
        ):
            return entry
    return None


def _token_span(entry: BrickEntry) -> tuple[int, int]:
    start = entry.start
    return start, start + len(entry.code) + 2


# --- Предложенные фрагменты -------------------------------------------------


def _occurrence_of_span(text: str, fragment: str, token_start: int) -> int | None:
    """Номер вхождения фрагмента, накрывающего выбранный токен."""
    position = -1
    number = 0
    while True:
        position = text.find(fragment, position + 1)
        if position < 0:
            return None
        number += 1
        if position <= token_start < position + len(fragment):
            return number


def _expand(text: str, start: int, end: int, *, limit: int = 3) -> tuple[int, int]:
    """
    Расширяет токен на примыкающую пунктуацию (§40, шаг 2).

    Слева берутся пробелы и любые разделители (`–[конец]` в §42-А), справа —
    только пробелы и закрывающие знаки: иначе «[конец] (снова)» дало бы
    фрагмент «[конец] (» и скрыло бы чужую скобку.
    """
    new_start, new_end = start, end
    steps = 0
    while new_start > 0 and steps < limit and text[new_start - 1] in SEPARATOR_CHARS:
        new_start -= 1
        steps += 1
    steps = 0
    while new_end < len(text) and steps < limit and text[new_end] in CLOSING_CHARS:
        new_end += 1
        steps += 1
    return new_start, new_end


def _separator_fragment(entry: BrickEntry, text: str) -> tuple[str, int] | None:
    start, end = _token_span(entry)
    new_start, new_end = _expand(text, start, end)
    if (new_start, new_end) == (start, end):
        return None
    fragment = text[new_start:new_end]
    occurrence = _occurrence_of_span(text, fragment, start)
    if occurrence is None:
        return None
    return fragment, occurrence


def _line_fragment(entry: BrickEntry, text: str) -> tuple[str, int] | None:
    start, end = _token_span(entry)
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    fragment = text[line_start:line_end]
    if not fragment.strip() or fragment.strip() == text[start:end]:
        return None
    occurrence = _occurrence_of_span(text, fragment, start)
    if occurrence is None:
        return None
    return fragment, occurrence


def _proposal(entry: BrickEntry, text: str, kind: str) -> tuple[str, int] | None:
    if kind == "sep":
        return _separator_fragment(entry, text)
    if kind == "line":
        return _line_fragment(entry, text)
    return None


def _custom_fragment(
    entry: BrickEntry, text: str, fragment: str
) -> tuple[str, int] | str:
    """Проверяет собственный фрагмент до предпросмотра (§40, шаг 3)."""
    if not fragment.strip():
        return tr("templates.cond.custom_error_empty")
    if f"[{entry.code}]" not in fragment:
        return tr("templates.cond.custom_error_no_brick", code=entry.code)
    occurrence = _occurrence_of_span(text, fragment, entry.start)
    if occurrence is None:
        return tr("templates.cond.custom_error_not_found", occurrence=entry.occurrence)
    return fragment, occurrence


# --- Экраны -----------------------------------------------------------------


def _back(callback: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(tr("buttons.nav.back"), callback_data=callback)


def _bricks_text(entries: list[BrickEntry]) -> str:
    lines = [
        tr("templates.cond.title"),
        "",
        tr("templates.cond.choose_brick"),
    ]
    sections = {entry.section for entry in entries}
    for section in (SECTION_CONTENT, SECTION_EXTENDED_CONTENT):
        if section not in sections:
            continue
        if len(sections) > 1:
            lines.append("")
            lines.append(
                tr("templates.cond.section_extended")
                if section == SECTION_EXTENDED_CONTENT
                else tr("templates.cond.section_content")
            )
        for entry in entries:
            if entry.section != section:
                continue
            lines.append(
                tr(
                    "templates.cond.entry",
                    code=entry.code,
                    occurrence=entry.occurrence,
                    context=entry.context,
                )
            )
    return "\n".join(lines)


def _bricks_content(
    entries: list[BrickEntry], template_id: int, *, has_parts: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    for entry in entries:
        flag = "e" if entry.is_extended else "c"
        label = (
            tr(
                "templates.cond.entry_button_extended",
                code=entry.code,
                occurrence=entry.occurrence,
            )
            if entry.is_extended
            else tr(
                "templates.cond.entry_button",
                code=entry.code,
                occurrence=entry.occurrence,
            )
        )
        rows.append(
            [
                InlineKeyboardButton(
                    tp._short(label),
                    callback_data=(
                        f"tp_condb:{template_id}:{flag}:"
                        f"{entry.brick_id}:{entry.occurrence}"
                    ),
                )
            ]
        )
    if has_parts:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("templates.cond.list_button"),
                    callback_data=f"tp_condl:{template_id}",
                )
            ]
        )
    rows.append([_back(f"tp_condback:{template_id}:card")])
    return _bricks_text(entries), InlineKeyboardMarkup(rows)


def _no_bricks_content(
    template_id: int, *, has_parts: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join([tr("templates.cond.title"), "", tr("templates.cond.no_bricks")])
    rows: list[list[InlineKeyboardButton]] = []
    if has_parts:
        # Правила остаются доступными даже когда в новом тексте нет
        # кирпичиков: их можно посмотреть, исправить или удалить вручную.
        rows.append(
            [
                InlineKeyboardButton(
                    tr("templates.cond.list_button"),
                    callback_data=f"tp_condl:{template_id}",
                )
            ]
        )
    rows.append([_back(f"tp_condback:{template_id}:card")])
    return text, InlineKeyboardMarkup(rows)


def _entry_header(cond: dict) -> str:
    return tr(
        "templates.cond.fragment_line",
        code=cond.get("code") or "",
        occurrence=cond.get("occurrence") or 1,
        fragment=cond.get("fragment") or "",
    )


def _fragment_content(
    cond: dict,
    text: str,
    entry: BrickEntry,
) -> tuple[str, InlineKeyboardMarkup]:
    template_id = int(cond["template_id"])
    lines = [
        tr("templates.cond.title"),
        tr(
            "templates.cond.entry_line",
            code=entry.code,
            occurrence=entry.occurrence,
        ),
        "",
        tr("templates.cond.choose_fragment"),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    separator = _separator_fragment(entry, text)
    if separator is not None:
        lines.append(tr("templates.cond.separator_sample", fragment=separator[0]))
        rows.append(
            [
                InlineKeyboardButton(
                    tr("templates.cond.option_separator"),
                    callback_data=f"tp_condf:{template_id}:sep",
                )
            ]
        )
    line = _line_fragment(entry, text)
    if line is not None:
        lines.append(tr("templates.cond.line_sample", fragment=line[0]))
        rows.append(
            [
                InlineKeyboardButton(
                    tr("templates.cond.option_line"),
                    callback_data=f"tp_condf:{template_id}:line",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("templates.cond.option_custom"),
                callback_data=f"tp_condf:{template_id}:custom",
            )
        ]
    )
    rows.append([_back(f"tp_condback:{template_id}:bricks")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _proposal_content(cond: dict) -> tuple[str, InlineKeyboardMarkup]:
    template_id = int(cond["template_id"])
    text = "\n".join(
        [
            tr("templates.cond.title"),
            "",
            tr("templates.cond.fragment_proposal", fragment=cond.get("fragment") or ""),
            "",
            tr("templates.cond.no_autosave"),
        ]
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("templates.cond.use"),
                    callback_data=f"tp_conduse:{template_id}",
                )
            ],
            [_back(f"tp_condback:{template_id}:fragment")],
        ]
    )
    return text, keyboard


def _custom_content(cond: dict) -> tuple[str, InlineKeyboardMarkup]:
    template_id = int(cond["template_id"])
    text = "\n".join(
        [
            tr("templates.cond.title"),
            "",
            tr("templates.cond.custom_prompt", code=cond.get("code") or ""),
        ]
    )
    keyboard = InlineKeyboardMarkup([[_back(f"tp_condback:{template_id}:fragment")]])
    return text, keyboard


def _preview_content(cond: dict, text: str) -> tuple[str, InlineKeyboardMarkup]:
    """Предпросмотр на реальном тексте секции (§40, шаг 4)."""
    template_id = int(cond["template_id"])
    brick_id = int(cond["brick_id"])
    rule = ConditionalRule(
        brick_id=brick_id,
        fragment=str(cond.get("fragment") or ""),
        occurrence=int(cond.get("occurrence") or 1),
    )
    empty_text = apply_conditional_parts(
        text, [rule], is_empty=lambda value: value == brick_id
    )
    lines = [
        tr("templates.cond.preview_title"),
        _entry_header(cond),
        "",
        tr("templates.cond.preview_filled", text=text),
        "",
        tr("templates.cond.preview_empty", text=empty_text),
    ]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("templates.cond.save"),
                    callback_data=f"tp_conds:{template_id}",
                )
            ],
            [_back(f"tp_condback:{template_id}:proposal")],
        ]
    )
    return "\n".join(lines), keyboard


def _with_error(
    content: tuple[str, InlineKeyboardMarkup], cond: dict
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Добавляет понятное сообщение об ошибке к экрану шага.

    Ошибка остаётся на этом же экране: автор исправляет фрагмент, не
    возвращаясь к началу маршрута (§40, шаг 3).
    """
    error = cond.get("error")
    if not error:
        return content
    text, keyboard = content
    return f"{error}\n\n{text}", keyboard


def _book(
    cond: dict, session, telegram_user_id: int
) -> tuple["tp.TemplateDetails | None", "list"]:
    """Шаблон и правила, если автор всё ещё может их менять."""
    template = _editable(
        session, cond, telegram_user_id, int(cond.get("template_id") or 0)
    )
    if template is None:
        return None, []
    return template, _load_parts(session, telegram_user_id, template.template_id)


def _status_title(part) -> str:
    return (
        tr("templates.cond.status_stale")
        if part.status == STATUS_STALE
        else tr("templates.cond.status_active")
    )


def _rule_content(part) -> tuple[str, InlineKeyboardMarkup]:
    template_id = part.template_id
    text = "\n".join(
        [
            tr("templates.cond.rule_title"),
            "",
            tr(
                "templates.cond.rule_line",
                code=part.code,
                status=_status_title(part),
                fragment=part.fragment,
            ),
        ]
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("templates.cond.edit"),
                    callback_data=f"tp_conde:{template_id}:{part.id}",
                ),
                InlineKeyboardButton(
                    tr("buttons.templates.udalit"),
                    callback_data=f"tp_condd:{template_id}:{part.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    tr("templates.cond.list_button"),
                    callback_data=f"tp_condl:{template_id}",
                )
            ],
            [_back(f"tp_condback:{template_id}:card")],
        ]
    )
    return text, keyboard


def _list_content(parts: list, template_id: int) -> tuple[str, InlineKeyboardMarkup]:
    stale = sum(1 for part in parts if part.status == STATUS_STALE)
    lines = [tr("templates.cond.list_title"), ""]
    if stale:
        lines.append(tr("templates.cond.list_stale_warning", count=stale))
        lines.append("")
    if not parts:
        lines.append(tr("templates.cond.list_empty"))
    rows: list[list[InlineKeyboardButton]] = []
    for index, part in enumerate(parts, start=1):
        lines.append(
            tr(
                "templates.cond.list_item",
                index=index,
                code=part.code,
                fragment=part.fragment,
                status=_status_title(part),
            )
        )
        rows.append(
            [
                InlineKeyboardButton(
                    tp._short(
                        tr(
                            "templates.cond.list_button_item",
                            index=index,
                            code=part.code,
                        )
                    ),
                    callback_data=f"tp_condo:{template_id}:{part.id}",
                )
            ]
        )
    rows.append([_back(f"tp_condback:{template_id}:card")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _delete_content(part) -> tuple[str, InlineKeyboardMarkup]:
    template_id = part.template_id
    text = tr(
        "templates.cond.delete_confirm",
        code=part.code,
        fragment=part.fragment,
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("templates.cond.delete_yes"),
                    callback_data=f"tp_conddy:{template_id}:{part.id}",
                )
            ],
            [_back(f"tp_condback:{template_id}:rule")],
        ]
    )
    return text, keyboard


# --- Актуальность правил (§44) ----------------------------------------------


def stale_count(session, template: "tp.TemplateDetails", telegram_user_id: int) -> int:
    """
    Сколько правил требуют перенастройки (для плашки в карточке).

    Только ЧТЕНИЕ. Актуальность правил проверяет `template_store` в момент
    создания новой версии текста: карточка не имеет права менять доменные
    данные при отрисовке (иначе порядок экранов влиял бы на БД).
    """
    try:
        parts = list_conditional_parts(
            session, telegram_user_id, template.template_id, include_stale=True
        )
    except Exception as exc:  # noqa: BLE001 — карточка не должна падать из-за правил
        logger.warning(
            "Не удалось прочитать условные части шаблона %s: %s",
            getattr(template, "template_id", "?"),
            exc,
        )
        return 0
    current_version_id = template.current_version.version_id
    return sum(
        1
        for part in parts
        if part.status == STATUS_STALE or part.version_id != current_version_id
    )


# --- Общие помощники маршрута ------------------------------------------------


def _editable(session, state: dict, telegram_user_id: int, template_id: int):
    """
    Шаблон, который автор всё ещё может менять.

    Права проверяет `tp._load_editable` (для стандарта — действующий админ
    места или суперадмин), поэтому маршрут правил не дублирует проверку по
    признаку `origin`: состояние может быть восстановлено после перезапуска
    без этого поля, а права от этого не меняются.
    """
    return tp._load_editable(session, telegram_user_id, template_id)


def _guard(
    update: Update, context: ContextTypes.DEFAULT_TYPE, template_id: int
) -> dict | None:
    """Общая проверка: экран правила открыт из актуальной карточки."""
    cond = _state(ctx.data(context))
    if cond.get("template_id") != template_id or cond.get("step") is None:
        return None
    return cond


async def _stale(update: Update) -> None:
    await ctx.query(update).answer(tp.STALE_TEXT, show_alert=True)


def _ids(data: str, prefix: str, count: int) -> list[int] | None:
    if not data.startswith(prefix):
        return None
    raw = data[len(prefix) :].split(":")
    if len(raw) != count:
        return None
    try:
        return [int(item) for item in raw]
    except ValueError:
        return None


def _fragment_span(text: str, fragment: str, occurrence: int) -> tuple[int, int] | None:
    start = -1
    for _ in range(max(1, occurrence)):
        start = text.find(fragment, start + 1)
        if start < 0:
            return None
    return start, start + len(fragment)


def _cond_input_on(
    context: ContextTypes.DEFAULT_TYPE, cond: dict, *, flag: bool
) -> None:
    """
    Держит признак текстового ввода в шаблонной сессии.

    Фильтр `MessageHandler.check_update` не видит `user_data`, поэтому
    «ждём фрагмент» дополнительно отмечается в состоянии шаблона. Фаза
    остаётся `card`: правка текста и правила не смешиваются.
    """
    user_id = int(cond.get("user_id") or 0)
    if not user_id:
        return
    state = load_template_session(user_id)
    if not isinstance(state, dict) or not tp._valid_state(state):
        return
    if flag:
        state[INPUT_FLAG] = True
    else:
        state.pop(INPUT_FLAG, None)
    tp.save_template_session(user_id, state)


def _load_parts(session, telegram_user_id: int, template_id: int) -> list:
    """
    Все правила шаблона: активные текущей версии и требующие перенастройки.

    `include_stale` обязателен: после правки текста правило остаётся в БД
    со статусом `stale` (§44), и автор должен его видеть — иначе оно
    «исчезло» бы из редактора без возможности исправления.
    """
    try:
        return list_conditional_parts(
            session, telegram_user_id, template_id, include_stale=True
        )
    except Exception as exc:  # noqa: BLE001 — список не должен ломать экран
        logger.warning("Не удалось прочитать правила шаблона %s: %s", template_id, exc)
        return []


def _find_part(parts: list, part_id: object):
    if not isinstance(part_id, int):
        return None
    for part in parts:
        if part.id == part_id:
            return part
    return None


def _content_for(
    session,
    template: "tp.TemplateDetails",
    cond: dict,
    telegram_user_id: int,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """
    Содержимое текущего шага; чистая функция от домена и состояния.

    `None` — выбранное вхождение кирпичика исчезло (текст изменился в
    другом окне). Вызывающий возвращает автора к выбору кирпичика, а не
    рисует экран с пустым фрагментом.
    """
    template_id = template.template_id
    step = cond.get("step")
    section = str(cond.get("section") or SECTION_CONTENT)
    section_text = _section_text(template, section)
    if step in {STEP_FRAGMENT, STEP_CUSTOM, STEP_PROPOSAL, STEP_PREVIEW}:
        entry = _find_entry(
            template,
            section,
            int(cond.get("brick_id") or 0),
            int(cond.get("occurrence") or 1),
        )
        if entry is None:
            return None
        if step == STEP_FRAGMENT:
            result = _fragment_content(cond, section_text, entry)
        elif step == STEP_CUSTOM:
            result = _custom_content(cond)
        elif step == STEP_PROPOSAL:
            result = _proposal_content(cond)
        else:
            result = _preview_content(cond, section_text)
        return _with_error(result, cond)
    if step in {STEP_LIST, STEP_RULE, STEP_DELETE}:
        parts = _load_parts(session, telegram_user_id, template_id)
        part = _find_part(parts, cond.get("part_id"))
        if step == STEP_LIST or part is None:
            if part is None and step != STEP_LIST:
                # Правило удалено в другом окне: показываем список, а не
                # экран с пустым фрагментом.
                cond["step"] = STEP_LIST
                cond["part_id"] = None
            return _list_content(parts, template_id)
        if step == STEP_RULE:
            return _rule_content(part)
        return _delete_content(part)
    entries = _entries_for_template(template)
    has_parts = bool(_load_parts(session, telegram_user_id, template_id))
    if entries:
        return _bricks_content(entries, template_id, has_parts=has_parts)
    return _no_bricks_content(template_id, has_parts=has_parts)


# --- Отрисовка экрана правила -----------------------------------------------


def _cond_state(cond: dict, template: "tp.TemplateDetails", page: int = 0) -> dict:
    """
    Шаблонное состояние для `_show`, пока открыт раздел правил.

    Фаза остаётся `card`: правила — часть карточки шаблона, а не отдельный
    редактор текста. `revision` берётся из шаблона, поэтому оптимистическая
    блокировка в `_show`/`_render_phase` остаётся рабочей.
    """
    return {
        "phase": tp.PHASE_CARD,
        "template_id": template.template_id,
        "revision": template.revision,
        "origin": tp._template_origin(template),
        "place_id": template.place_id,
        "page": page,
    }


async def _render_cond(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    cond: dict,
    *,
    notice: str | None = None,
    drop_input: bool = False,
) -> bool:
    """
    Рисует текущий шаг раздела правил в том же служебном экране.

    Доменные данные здесь не меняются: функция читает шаблон и правила.
    Если шаблон исчез или пропали права, автор возвращается в карточку с
    понятным сообщением (`tp._render_phase` сам восстановит хаб).
    """
    template_id = cond.get("template_id")
    if not isinstance(template_id, int) or template_id <= 0:
        _forget(context)
        await _stale(update)
        return False
    telegram_user_id = ctx.user(update).id
    state = load_template_session(telegram_user_id)
    if (
        not isinstance(state, dict)
        or not tp._valid_state(state)
        or state.get("template_id") != template_id
    ):
        # Шаблонная сессия — источник истины о правах и ревизии. Без неё
        # маршрут правил бессмысленен: автор открывает карточку заново.
        _forget(context)
        _cond_input_on(context, cond, flag=False)
        await _stale(update)
        return False
    session = tp.SessionLocal()
    try:
        template = _editable(session, state, telegram_user_id, template_id)
        if template is None:
            _forget(context)
            _cond_input_on(context, cond, flag=False)
            await tp._render_phase(
                update,
                context,
                state,
                notice=tr("templates.cond.template_gone"),
            )
            return False
        content = _content_for(session, template, cond, telegram_user_id)
        if content is None:
            # Вхождение кирпичика исчезло после правки текста в другом окне.
            cond["step"] = STEP_BRICKS
            cond["error"] = None
            cond["notice"] = tr("templates.cond.entry_gone")
            content = _content_for(session, template, cond, telegram_user_id)
            if content is None:
                # Правила могли остаться от прежней версии: кнопка списка
                # должна быть и в ветке «вхождений больше нет» (§44).
                content = _no_bricks_content(
                    template.template_id,
                    has_parts=bool(_load_parts(session, telegram_user_id, template_id)),
                )
        text, keyboard = content
        page = state.get("page")
        state.update(_cond_state(cond, template, page if isinstance(page, int) else 0))
    except Exception as exc:  # noqa: BLE001 — экран не должен падать
        logger.exception("Не удалось отрисовать условные части: %s", exc)
        _forget(context)
        await tp._render_phase(
            update,
            context,
            state if isinstance(state, dict) else {},
            notice=tr("templates.cond.render_failed"),
        )
        return False
    finally:
        session.close()
    _remember(context, cond)
    shown = f"{cond['notice']}\n\n{text}" if cond.get("notice") else text
    cond.pop("notice", None)
    body = f"{notice}\n\n{shown}" if notice else shown
    return await tp._show(update, context, state, body, keyboard, drop_input=drop_input)


# --- Обработчики шагов -------------------------------------------------------


async def handle_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Карточка шаблона → «🧩 Условные части» (spec §40, шаг 1)."""
    query = ctx.query(update)
    template_id = tp._callback_id(query.data or "", "tp_cond:")
    cond_state = load_template_session(ctx.user(update).id)
    if template_id is None:
        await _stale(update)
        return
    if not tp._valid_state(cond_state) or cond_state.get("phase") != tp.PHASE_CARD:
        await _stale(update)
        return
    if cond_state.get("template_id") != template_id:
        await _stale(update)
        return
    telegram_user_id = ctx.user(update).id
    session = tp.SessionLocal()
    try:
        template = _editable(session, cond_state, telegram_user_id, template_id)
        if template is None:
            await query.answer(tp.DENIED_TEXT, show_alert=True)
            return
        await query.answer()
    finally:
        session.close()
    cond = _new_state(template_id)
    cond["user_id"] = telegram_user_id
    await _render_cond(update, context, cond)


async def handle_bricks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Шаг 1: выбран конкретный кирпичик (с номером вхождения)."""
    query = ctx.query(update)
    raw = (query.data or "")[len("tp_condb:") :].split(":")
    if len(raw) != 4:
        await _stale(update)
        return
    try:
        template_id = int(raw[0])
        brick_id = int(raw[2])
        occurrence = int(raw[3])
    except ValueError:
        await _stale(update)
        return
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    section = SECTION_EXTENDED_CONTENT if raw[1] == "e" else SECTION_CONTENT
    telegram_user_id = ctx.user(update).id
    session = tp.SessionLocal()
    try:
        template = _editable(session, cond, telegram_user_id, template_id)
        entry = (
            _find_entry(template, section, brick_id, occurrence)
            if template is not None
            else None
        )
    finally:
        session.close()
    if entry is None:
        await query.answer(tr("templates.cond.entry_gone"), show_alert=True)
        cond["step"] = STEP_BRICKS
        await _render_cond(update, context, cond)
        return
    await query.answer()
    cond.update(
        {
            "step": STEP_FRAGMENT,
            "section": section,
            "brick_id": brick_id,
            "code": entry.code,
            "occurrence": occurrence,
            "fragment": None,
            "part_id": None,
            "error": None,
        }
    )
    await _render_cond(update, context, cond)


async def handle_fragment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Шаг 2: выбран способ скрытия — разделитель, строка или свой фрагмент."""
    query = ctx.query(update)
    data = query.data or ""
    parts = data[len("tp_condf:") :].split(":") if data.startswith("tp_condf:") else []
    if len(parts) != 2:
        await _stale(update)
        return
    try:
        template_id = int(parts[0])
    except ValueError:
        await _stale(update)
        return
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    kind = parts[1]
    await query.answer()
    if kind == "custom":
        cond["step"] = STEP_CUSTOM
        cond["error"] = None
        _cond_input_on(context, cond, flag=True)
        await _render_cond(update, context, cond)
        return
    await _apply_proposal(update, context, cond, kind)


async def _apply_proposal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    cond: dict,
    kind: str,
) -> None:
    """
    Готовит предложенный фрагмент к предпросмотру (§40, шаг 2).

    Предложенный вариант не сохраняется автоматически: он показывается на
    экране подтверждения, откуда можно вернуться и выбрать другой.
    """
    template_id = int(cond["template_id"])
    telegram_user_id = ctx.user(update).id
    session = tp.SessionLocal()
    try:
        template = _editable(session, cond, telegram_user_id, template_id)
        if template is None:
            _forget(context)
            await _stale(update)
            return
        entry = _find_entry(
            template,
            str(cond.get("section")),
            int(cond.get("brick_id") or 0),
            int(cond.get("occurrence") or 1),
        )
        if entry is None:
            cond["step"] = STEP_BRICKS
            cond["error"] = tr("templates.cond.entry_gone")
            await _render_cond(update, context, cond)
            return
        text = _section_text(template, entry.section)
        proposal = _proposal(entry, text, kind)
    finally:
        session.close()
    if proposal is None:
        cond["error"] = tr("templates.cond.proposal_unavailable")
        await _render_cond(update, context, cond)
        return
    fragment, occurrence = proposal
    cond.update(
        {
            "step": STEP_PROPOSAL,
            "fragment": fragment,
            "occurrence": occurrence,
            "error": None,
        }
    )
    _cond_input_on(context, cond, flag=False)
    await _render_cond(update, context, cond)


async def handle_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Шаг 3: автор подтвердил фрагмент — предпросмотр «заполнен/пуст»."""
    query = ctx.query(update)
    data = query.data or ""
    try:
        template_id = int(data[len("tp_conduse:") :])
    except ValueError:
        await _stale(update)
        return
    cond = _guard(update, context, template_id)
    if cond is None or not cond.get("fragment"):
        await _stale(update)
        return
    await query.answer()
    cond["step"] = STEP_PREVIEW
    cond["error"] = None
    await _render_cond(update, context, cond)


async def handle_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Шаг 4: сохранение правила (или правка существующего)."""
    query = ctx.query(update)
    data = query.data or ""
    try:
        template_id = int(data[len("tp_conds:") :])
    except ValueError:
        await _stale(update)
        return
    cond = _guard(update, context, template_id)
    if cond is None or not cond.get("fragment"):
        await _stale(update)
        return
    telegram_user_id = ctx.user(update).id
    session = tp.SessionLocal()
    try:
        template = _editable(session, cond, telegram_user_id, template_id)
        if template is None:
            await query.answer(tp.DENIED_TEXT, show_alert=True)
            return
        part_id = cond.get("part_id")
        if isinstance(part_id, int):
            result = update_conditional_part(
                session,
                actor_telegram_user_id=telegram_user_id,
                part_id=part_id,
                expected_revision=template.revision,
                fragment=cond.get("fragment"),
                occurrence=int(cond.get("occurrence") or 1),
                repair=True,
            )
        else:
            result = create_conditional_part(
                session,
                actor_telegram_user_id=telegram_user_id,
                template_id=template_id,
                expected_revision=template.revision,
                section=str(cond.get("section") or SECTION_CONTENT),
                brick_id=int(cond.get("brick_id") or 0),
                fragment=cond.get("fragment"),
                occurrence=int(cond.get("occurrence") or 1),
            )
    except Exception as exc:  # noqa: BLE001 — автор не должен видеть traceback
        logger.exception("Не удалось сохранить условную часть: %s", exc)
        await query.answer(tr("templates.cond.save_failed"), show_alert=True)
        return
    finally:
        session.close()
    if result.status == "stale":
        await query.answer(tr("templates.cond.template_changed"), show_alert=True)
        cond.update({"step": STEP_LIST, "part_id": None})
        await _render_cond(update, context, cond)
        return
    if not result.ok:
        # Понятное сообщение домена — на экран предпросмотра, автор остаётся
        # в маршруте и может исправить фрагмент.
        await query.answer()
        cond["error"] = result.message or tr("templates.cond.save_failed")
        cond["step"] = STEP_PROPOSAL
        await _render_cond(update, context, cond)
        return
    await query.answer(tr("templates.cond.saved_toast"))
    _cond_input_on(context, cond, flag=False)
    cond.update(
        {
            "step": STEP_LIST,
            "brick_id": None,
            "code": None,
            "fragment": None,
            "part_id": None,
            "error": None,
        }
    )
    await _render_cond(update, context, cond)


async def handle_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Все правила шаблона: активные и требующие перенастройки."""
    query = ctx.query(update)
    data = query.data or ""
    try:
        template_id = int(data[len("tp_condl:") :])
    except ValueError:
        await _stale(update)
        return
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    await query.answer()
    _cond_input_on(context, cond, flag=False)
    cond.update({"step": STEP_LIST, "part_id": None, "error": None})
    await _render_cond(update, context, cond)


async def handle_rule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Карточка правила: фрагмент целиком, Изменить/Удалить/Назад."""
    query = ctx.query(update)
    data = query.data or ""
    parts = data[len("tp_condo:") :].split(":") if data.startswith("tp_condo:") else []
    if len(parts) != 2:
        await _stale(update)
        return
    try:
        template_id, part_id = int(parts[0]), int(parts[1])
    except ValueError:
        await _stale(update)
        return
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    await query.answer()
    cond.update({"step": STEP_RULE, "part_id": part_id, "error": None})
    await _render_cond(update, context, cond)


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    «Изменить»: открывает выбор фрагмента для существующего правила.

    Если правило относится к прежней версии или помечено `stale`, оно
    привязывается к текущей версии через `repair=True` при сохранении —
    «висеть» на несуществующем тексте правило не остаётся (§44).
    """
    query = ctx.query(update)
    data = query.data or ""
    parts = data[len("tp_conde:") :].split(":") if data.startswith("tp_conde:") else []
    if len(parts) != 2:
        await _stale(update)
        return
    try:
        template_id, part_id = int(parts[0]), int(parts[1])
    except ValueError:
        await _stale(update)
        return
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    telegram_user_id = ctx.user(update).id
    session = tp.SessionLocal()
    try:
        parts_list = _load_parts(session, telegram_user_id, template_id)
        part = _find_part(parts_list, part_id)
    finally:
        session.close()
    if part is None:
        await query.answer(tr("templates.cond.rule_gone"), show_alert=True)
        cond.update({"step": STEP_LIST, "part_id": None})
        await _render_cond(update, context, cond)
        return
    await query.answer()
    cond.update(
        {
            "step": STEP_FRAGMENT,
            "section": part.section,
            "brick_id": part.brick_id,
            "code": part.code,
            "occurrence": part.occurrence,
            "fragment": part.fragment,
            "part_id": part.id,
            "error": None,
        }
    )
    await _render_cond(update, context, cond)


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Удалить» — экран подтверждения, правило удаляется только явно."""
    query = ctx.query(update)
    data = query.data or ""
    parts = data[len("tp_condd:") :].split(":") if data.startswith("tp_condd:") else []
    if len(parts) != 2:
        await _stale(update)
        return
    try:
        template_id, part_id = int(parts[0]), int(parts[1])
    except ValueError:
        await _stale(update)
        return
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    await query.answer()
    cond.update({"step": STEP_DELETE, "part_id": part_id, "error": None})
    await _render_cond(update, context, cond)


async def handle_delete_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подтверждённое удаление правила."""
    query = ctx.query(update)
    data = query.data or ""
    parts = (
        data[len("tp_conddy:") :].split(":") if data.startswith("tp_conddy:") else []
    )
    if len(parts) != 2:
        await _stale(update)
        return
    try:
        template_id, part_id = int(parts[0]), int(parts[1])
    except ValueError:
        await _stale(update)
        return
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    telegram_user_id = ctx.user(update).id
    session = tp.SessionLocal()
    try:
        template = _editable(session, cond, telegram_user_id, template_id)
        result = delete_conditional_part(
            session,
            actor_telegram_user_id=telegram_user_id,
            part_id=part_id,
            expected_revision=template.revision if template is not None else None,
        )
    except Exception as exc:  # noqa: BLE001 — удаление не должно ломать экран
        logger.exception("Не удалось удалить условную часть: %s", exc)
        await query.answer(tr("templates.cond.delete_failed"), show_alert=True)
        return
    finally:
        session.close()
    if result.status == "stale":
        await query.answer(tr("templates.cond.template_changed"), show_alert=True)
    elif result.ok:
        await query.answer(tr("templates.cond.deleted_toast"))
    else:
        await query.answer(result.message or tr("templates.cond.delete_failed"))
    cond.update({"step": STEP_LIST, "part_id": None, "error": None})
    await _render_cond(update, context, cond)


async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    «Назад» внутри раздела правил — фактический предыдущий шаг (§8).

    Из первого шага возвращает в карточку шаблона; текст и состояние
    шаблона при этом не теряются.
    """
    query = ctx.query(update)
    data = query.data or ""
    parts = (
        data[len("tp_condback:") :].split(":")
        if data.startswith("tp_condback:")
        else []
    )
    if len(parts) != 2:
        await _stale(update)
        return
    try:
        template_id = int(parts[0])
    except ValueError:
        await _stale(update)
        return
    target = parts[1]
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    await query.answer()
    _cond_input_on(context, cond, flag=False)
    if target == "card":
        # Возврат в карточку: правила забываются, текст и страница списка
        # остаются прежними (§8).
        _forget(context)
        state = load_template_session(ctx.user(update).id)
        if not tp._valid_state(state) or state.get("template_id") != template_id:
            await _stale(update)
            return
        state["phase"] = tp.PHASE_CARD
        await tp._render_phase(update, context, state)
        return
    if target not in BACK_TARGETS:
        await _stale(update)
        return
    cond["step"] = target
    cond["error"] = None
    if target == STEP_BRICKS:
        cond["part_id"] = None
        cond["fragment"] = None
    await _render_cond(update, context, cond)


async def handle_custom_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Свой фрагмент: проверка по store, ошибка — остаться на вводе (§40)."""
    cond = _state(ctx.data(context))
    if cond.get("step") != STEP_CUSTOM:
        return
    template_id = cond.get("template_id")
    if not isinstance(template_id, int):
        return
    message = ctx.message(update)
    # Срезаются только внешние переводы строк: ведущий пробел фрагмента
    # значим (§42-В: ` ([адрес])` — часть авторского текста), поэтому
    # предпросмотр обязан совпасть с сохранённым.
    fragment = (message.text or "").strip("\n")
    telegram_user_id = ctx.user(update).id
    session = tp.SessionLocal()
    try:
        template = _editable(session, cond, telegram_user_id, template_id)
        entry = (
            _find_entry(
                template,
                str(cond.get("section")),
                int(cond.get("brick_id") or 0),
                int(cond.get("occurrence") or 1),
            )
            if template is not None
            else None
        )
        text = _section_text(template, str(cond.get("section"))) if template else ""
        if entry is None or template is None:
            error: str | None = tr("templates.cond.entry_gone")
        else:
            checked = _custom_fragment(entry, text, fragment)
            if isinstance(checked, str):
                error = checked
            else:
                cond["fragment"], cond["occurrence"] = checked
                error = None
    finally:
        session.close()
    if error is not None:
        # Остаёмся на вводе: сообщение пользователя не удаляется (§5.3).
        cond["error"] = error
        await _render_cond(update, context, cond)
        return
    cond["step"] = STEP_PREVIEW
    cond["error"] = None
    _cond_input_on(context, cond, flag=False)
    await _render_cond(update, context, cond, drop_input=True)


# --- Регистрация -------------------------------------------------------------


class CondTextHandler(MessageHandler):
    """
    Текст принимается только пока раздел правил ждёт свой фрагмент.

    `check_update` видит состояние шаблона из БД, а не `user_data`, поэтому
    признак ожидания дублируется в шаблонной сессии (`INPUT_FLAG`), которая
    не меняет фазу `card`: редактор текста и правила не мешают друг другу.
    """

    def __init__(self, filters_arg, callback) -> None:
        super().__init__(filters_arg, callback)

    def check_update(self, update):
        matched = super().check_update(update)
        if not matched:
            return None
        user = getattr(update, "effective_user", None)
        if user is None:
            return None
        state = load_template_session(user.id)
        if not isinstance(state, dict) or not tp._valid_state(state):
            return None
        if state.get("phase") != tp.PHASE_CARD:
            return None
        if not state.get(INPUT_FLAG):
            return None
        return matched


def conditional_handlers() -> list[BaseHandler]:
    """Обработчики раздела «Условные части» шаблона (spec §39–45)."""
    return [
        CallbackQueryHandler(handle_open, pattern=r"^tp_cond:\d+$"),
        CallbackQueryHandler(handle_bricks, pattern=r"^tp_condb:\d+:[ce]:\d+:\d+$"),
        CallbackQueryHandler(
            handle_fragment, pattern=r"^tp_condf:\d+:(sep|line|custom)$"
        ),
        CallbackQueryHandler(handle_use, pattern=r"^tp_conduse:\d+$"),
        CallbackQueryHandler(handle_save, pattern=r"^tp_conds:\d+$"),
        CallbackQueryHandler(handle_list, pattern=r"^tp_condl:\d+$"),
        CallbackQueryHandler(handle_rule, pattern=r"^tp_condo:\d+:\d+$"),
        CallbackQueryHandler(handle_edit, pattern=r"^tp_conde:\d+:\d+$"),
        CallbackQueryHandler(handle_delete, pattern=r"^tp_condd:\d+:\d+$"),
        CallbackQueryHandler(handle_delete_yes, pattern=r"^tp_conddy:\d+:\d+$"),
        CallbackQueryHandler(handle_back, pattern=r"^tp_condback:\d+:\w+$"),
        CondTextHandler(filters.TEXT & ~filters.COMMAND, handle_custom_text),
    ]
