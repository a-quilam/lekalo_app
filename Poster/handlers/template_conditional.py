# handlers/template_conditional.py
"""
Условные части шаблонов: Telegram-интерфейс (Poster 2.5.3 §3).

Путь автора: карточка расширенного шаблона → «🧩 Условия расширенного
шаблона» → ОДИН экран со списком кирпичиков расширенного текста → кирпичик →
способ скрытия → сохранение. Экран показывает не больше пяти кирпичиков на
страницу; у настроенного условия в подписи стоит «✓». Нажатие на кирпичик
открывает просмотр/изменение/удаление условия. Детали и примеры живут в
«Помощи», а не на экране.

Семантика (Poster 2.5.3 §3), которую держит этот модуль:

- условные части принадлежат ТОЛЬКО расширенному тексту: у простого шаблона
  нет ни кнопки в карточке, ни маршрута, ни применения при рендере, а прямой
  callback с секцией `content` получает отказ без записи в БД;
- один движок для личных шаблонов и стандарта места: никаких проверок по
  признаку `origin`, только факт расширенной версии и права редактора;
- кирпичик, встречающийся в обоих текстах, показывается ОДИН раз и его
  условие никогда не меняет простой текст;
- повторяющийся кирпичик в тексте — ОДНО правило автора: выбранный способ
  скрытия применяется ко всем его вхождениям (в БД это по-прежнему строки
  на каждое вхождение — так рендер и замороженные снимки работают с
  точными фрагментами);
- в интерфейсе нет номеров вхождений, технических диапазонов и сырого
  текста фрагментов: только название поля и понятный способ скрытия;
- литеральный статический текст не вычищается: скрывается ровно то, что
  выбрал автор.

Модуль намеренно не меняет доменный слой: правила читает и пишет
`conditional_parts`, шаблон и версии — `template_store`. Здесь только
экраны, состояние навигации и вычисление фрагментов для трёх режимов.

Состояние маршрута живёт в `context.user_data["cond_parts"]` (это
интерфейсная навигация, а не доменные данные), фаза шаблонной сессии
остаётся `card`: редактор текста и правила не должны мешать друг другу.
Признак «ждём свой фрагмент» дублируется в шаблонной сессии (`cond_input`),
потому что фильтр текстового обработчика видит только состояние из БД —
`user_data` в `check_update` недоступен.

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
    list_conditional_parts,
    replace_conditional_rules,
)
from handlers import templates as tp
from template_state import load_template_session
from utils import pagination
from utils.ui_texts import tr

logger = logging.getLogger(__name__)

# Ключ состояния маршрута. Живёт только в user_data.
COND_KEY = "cond_parts"
# Признак текстового ввода в шаблонной сессии (виден фильтру обработчика).
INPUT_FLAG = "cond_input"

STEP_LIST = "list"
STEP_BRICKS = "bricks"
STEP_MODE = "mode"
STEP_CUSTOM = "custom"
STEP_DELETE = "delete"

# Способы скрытия (§7): вся строка, строка с разделителем, свой фрагмент.
MODE_LINE = "line"
MODE_SEPARATOR = "sep"
MODE_FRAGMENT = "custom"
MODES = (MODE_LINE, MODE_SEPARATOR, MODE_FRAGMENT)
# Короткие человеческие названия способов (для списка и экрана выбора).
MODE_TITLE_KEYS = {
    MODE_LINE: "templates.cond.mode_line_short",
    MODE_SEPARATOR: "templates.cond.mode_sep_short",
    MODE_FRAGMENT: "templates.cond.mode_custom_short",
}

# Секции текста шаблона. Короткий флаг `e` — часть callback-протокола;
# флаг `c` принимается только чтобы старую кнопку можно было безопасно
# отклонить: условия принадлежат ТОЛЬКО расширенному тексту (2.5.3 §3).
SECTION_FLAGS = {SECTION_CONTENT: "c", SECTION_EXTENDED_CONTENT: "e"}
FLAG_SECTIONS = {flag: section for section, flag in SECTION_FLAGS.items()}
ALLOWED_SECTION = SECTION_EXTENDED_CONTENT

# Символы, которые автор считает «разделителем» вокруг кирпичика (§38/§42).
# Квадратные скобки сюда не входят: иначе фрагмент «съел» бы соседний токен.
SEPARATOR_CHARS = frozenset(" \t\u00a0–—-‐‑.,;:!?…()«»\"'·•—")
# Справа в фрагмент берутся только закрывающие знаки: пробел и тире справа
# чаще относятся к следующему смыслу («[конец] (снова)»), а не к кирпичику.
CLOSING_CHARS = frozenset(")]».,;:!?…")
BACK_TARGETS = frozenset({"card", STEP_LIST, STEP_BRICKS, STEP_MODE})


@dataclass(frozen=True)
class RuleGroup:
    """
    Одно правило автора: один кирпичик одной секции.

    Несколько строк БД (по строке на вхождение) показываются как одно
    правило: номера вхождений и фрагменты — технические детали (§7).
    """

    section: str
    brick_id: int
    code: str
    title: str
    status: str
    mode: str
    part_ids: tuple[int, ...]
    first_position: int

    @property
    def is_stale(self) -> bool:
        return self.status == STATUS_STALE

    @property
    def flag(self) -> str:
        return SECTION_FLAGS.get(self.section, "e")

    def mode_title(self) -> str:
        """Понятный способ скрытия без технических подробностей."""
        if self.is_stale:
            return tr("templates.cond.status_stale")
        return tr(MODE_TITLE_KEYS.get(self.mode, MODE_TITLE_KEYS[MODE_FRAGMENT]))


@dataclass(frozen=True)
class BrickEntry:
    """Один кирпичик из текста шаблона (без номера вхождения в UI)."""

    section: str
    brick_id: int
    code: str
    title: str
    occurrences: tuple[int, ...]

    @property
    def flag(self) -> str:
        return SECTION_FLAGS.get(self.section, "e")


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
        "page": 0,
        "section": SECTION_EXTENDED_CONTENT,
        "brick_id": None,
        "error": None,
        "notice": None,
    }


# --- Текст шаблона и вхождения ----------------------------------------------


def _section_text(template: tp.TemplateDetails, section: str) -> str:
    """Текст секции шаблона; условия существуют только в расширенном тексте."""
    version = template.current_version
    if section == SECTION_EXTENDED_CONTENT:
        return version.extended_content or ""
    return version.content


def _token_positions(text: str, code: str) -> list[int]:
    token = f"[{code}]"
    positions: list[int] = []
    start = text.find(token)
    while start >= 0:
        positions.append(start)
        start = text.find(token, start + 1)
    return positions


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
    Расширяет токен на примыкающую пунктуацию (§40).

    Слева берутся пробелы и любые разделители (`–[конец]`), справа — только
    пробелы и закрывающие знаки: иначе «[конец] (снова)» дало бы фрагмент
    «[конец] (» и скрыло бы чужую скобку.
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


def _line_span(text: str, start: int, end: int) -> tuple[int, int]:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    return line_start, line_end


def _entries(template: tp.TemplateDetails) -> list[BrickEntry]:
    """
    Кирпичики расширенного текста шаблона — по одному на кирпичик.

    Poster 2.5.3 §3: условия принадлежат ТОЛЬКО `extended_content`. Кирпичик,
    встречающийся в тексте несколько раз (или в обоих текстах), показывается
    ОДИН раз: автор выбирает способ скрытия один раз на все вхождения.
    Порядок — по первому вхождению в расширенном тексте, как читает автор.
    """
    text = _section_text(template, SECTION_EXTENDED_CONTENT)
    if not text:
        return []
    bindings = template.current_version.bindings
    found: list[tuple[int, BrickEntry]] = []
    for binding in bindings:
        code = binding.code
        if not code:
            continue
        positions = _token_positions(text, code)
        if not positions:
            continue
        found.append(
            (
                positions[0],
                BrickEntry(
                    section=SECTION_EXTENDED_CONTENT,
                    brick_id=binding.brick_id,
                    code=code,
                    title=binding.title or code,
                    occurrences=tuple(range(1, len(positions) + 1)),
                ),
            )
        )
    found.sort(key=lambda item: item[0])
    return [entry for _position, entry in found]


def _entry(
    template: tp.TemplateDetails, section: str, brick_id: int
) -> BrickEntry | None:
    """Кирпичик расширенного текста; у других секций условий нет (§3)."""
    if section != ALLOWED_SECTION:
        return None
    for entry in _entries(template):
        if entry.brick_id == brick_id:
            return entry
    return None


# --- Фрагменты трёх режимов -------------------------------------------------


def _mode_fragments(
    text: str, code: str, mode: str, custom: str | None = None
) -> list[tuple[str, int]] | None:
    """
    Пары `(фрагмент, вхождение)` для ВСЕХ вхождений кирпичика (или `None`).

    Автор выбирает способ один раз, а применяется он ко всем вхождениям
    кирпичика (§6): повторяющийся кирпичик — одно правило.
    """
    positions = _token_positions(text, code)
    if not positions:
        return None
    found: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for start in positions:
        end = start + len(code) + 2
        fragment: str | None
        if mode == MODE_LINE:
            line_start, line_end = _line_span(text, start, end)
            fragment = text[line_start:line_end]
            # Строка из одного кирпичика уже скрывается самим рендером.
            if not fragment.strip() or fragment.strip() == text[start:end]:
                continue
        elif mode == MODE_SEPARATOR:
            new_start, new_end = _expand(text, start, end)
            if (new_start, new_end) == (start, end):
                continue
            fragment = text[new_start:new_end]
        else:
            if not custom:
                return None
            fragment = _custom_around(text, custom, start)
        if fragment is None:
            return None
        occurrence = _occurrence_of_span(text, fragment, start)
        if occurrence is None:
            return None
        key = (fragment, occurrence)
        if key in seen:
            continue
        seen.add(key)
        found.append(key)
    return found or None


def _custom_around(text: str, custom: str, token_start: int) -> str | None:
    """
    Фрагмент автора, привязанный именно к этому вхождению кирпичика.

    Свой текст может повторяться в шаблоне, поэтому совпадение ищется по
    позиции кирпичика внутри фрагмента, а не по порядковому номеру.
    """
    if not custom.strip():
        return None
    anchors: list[int] = []
    offset = custom.find("[")
    while offset >= 0:
        if offset + 1 < len(custom) and custom[offset + 1] != "[":
            anchors.append(offset)
        offset = custom.find("[", offset + 1)
    if not anchors:
        return None
    position = -1
    while True:
        position = text.find(custom, position + 1)
        if position < 0:
            return None
        for anchor in anchors:
            if position + anchor == token_start:
                return custom


def _stored_mode(text: str, code: str, fragments: set[tuple[str, int]]) -> str:
    """Понятный способ скрытия по сохранённым фрагментам (для списка)."""
    for mode in (MODE_LINE, MODE_SEPARATOR):
        candidate = _mode_fragments(text, code, mode)
        if candidate and set(candidate) == fragments:
            return mode
    return MODE_FRAGMENT


# --- Группировка правил ------------------------------------------------------


def _group_rules(
    session,
    telegram_user_id: int,
    template: tp.TemplateDetails,
) -> list[RuleGroup]:
    """
    Правила расширенного текста как список правил автора.

    Один кирпичик — одно правило. Строки устаревшей секции `content` в
    интерфейс не попадают: условий у простого текста больше нет (§3), а
    `refresh_conditional_parts` удаляет их при сохранении версии.

    `stale`-правила не теряются: они тоже входят в список со своим статусом
    (§44), иначе автор не увидел бы проблему.
    """
    parts = _load_parts(session, telegram_user_id, template.template_id)
    current_version = template.current_version.version_id
    grouped: dict[int, list] = {}
    for part in parts:
        if part.section != ALLOWED_SECTION:
            continue
        grouped.setdefault(part.brick_id, []).append(part)
    text = _section_text(template, ALLOWED_SECTION)
    result: list[RuleGroup] = []
    for brick_id, rows in grouped.items():
        code = next((row.code for row in rows if row.code), "")
        title = code
        entry = _entry(template, ALLOWED_SECTION, brick_id)
        if entry is not None:
            title = entry.title
        active = [row for row in rows if row.status != STATUS_STALE]
        fragments = {(row.fragment, row.occurrence) for row in active}
        stale = any(
            row.status == STATUS_STALE or row.version_id != current_version
            for row in rows
        )
        first_position = min(
            (_first_span_position(text, row.fragment, row.occurrence) for row in rows),
            default=0,
        )
        result.append(
            RuleGroup(
                section=ALLOWED_SECTION,
                brick_id=brick_id,
                code=code,
                title=title,
                status=STATUS_STALE if stale else rows[0].status,
                mode=_stored_mode(text, code, fragments) if code else MODE_FRAGMENT,
                part_ids=tuple(row.id for row in rows),
                first_position=first_position,
            )
        )
    result.sort(key=lambda group: (group.first_position, group.title))
    return result


def _first_span_position(text: str, fragment: str, occurrence: int) -> int:
    start = -1
    for _ in range(max(1, occurrence)):
        start = text.find(fragment, start + 1)
        if start < 0:
            return 1 << 30
    return start


# --- Экраны -----------------------------------------------------------------


def _back(callback: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(tr("buttons.nav.back"), callback_data=callback)


def _nav_row(
    page: int, total: int, template_id: int
) -> list[InlineKeyboardButton] | None:
    """
    Строка `◀️ | 1 / 3 | ▶️`; при одной странице навигации нет (§7).

    Средняя кнопка инертна: её callback — глобальный `noop:<page>:<total>`,
    который отвечает коротким `answer()` и ничего не меняет. Стрелки несут
    смещение страницы (`tp_condp:<id>:<offset>`), как в остальных списках.
    """
    if total <= 1:
        return None
    row = pagination.nav_buttons(page, total, f"tp_condp:{template_id}")
    if row is None:  # pragma: no cover — total > 1 гарантирует строку
        return None
    buttons: list[InlineKeyboardButton] = []
    for label, data in row:
        # Кнопка в PTB неизменяемая, поэтому подпись индикатора
        # подставляется при создании: «1 / 3» вместо служебного «1/3».
        if data.startswith(f"{pagination.NOOP_PREFIX}:"):
            label = f"{page + 1} / {total}"
        buttons.append(InlineKeyboardButton(label, callback_data=data))
    return buttons


def _bricks_content(
    entries: list[BrickEntry],
    groups: list[RuleGroup],
    template_id: int,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Единственный экран раздела: кирпичики расширенного текста.

    Poster 2.5.3 §3: перечня полей в теле нет — только заголовок и подсказка,
    дальше кнопки кирпичиков (не больше пяти на страницу) и «◀️ Назад» в
    карточку. У настроенного кирпичика в подписи стоит «✓»; нажатие открывает
    экран способа скрытия.
    """
    configured = {group.brick_id for group in groups}
    page_slice = pagination.page_items(entries, page)
    lines = [tr("templates.cond.title"), "", tr("templates.cond.hint")]
    if not entries:
        lines = [tr("templates.cond.title"), "", tr("templates.cond.no_bricks")]
    rows: list[list[InlineKeyboardButton]] = []
    for entry in page_slice.items:
        key = (
            "templates.cond.brick_button_done"
            if entry.brick_id in configured
            else "templates.cond.brick_button"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    tp._short(tr(key, title=entry.title)),
                    callback_data=(
                        f"tp_condb:{template_id}:{entry.flag}:{entry.brick_id}"
                    ),
                )
            ]
        )
    nav = _nav_row(page_slice.page, page_slice.total_pages, template_id)
    if nav is not None:
        rows.append(nav)
    rows.append([_back(f"tp_condback:{template_id}:card")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _mode_content(
    template_id: int,
    entry: BrickEntry,
    group: RuleGroup | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Три способа скрытия с короткими человеческими примерами."""
    lines = [
        tr("templates.cond.mode_title", title=entry.title),
        "",
        tr("templates.cond.mode_line_example"),
        tr("templates.cond.mode_sep_example"),
        tr("templates.cond.mode_custom_example"),
        "",
        tr("templates.cond.mode_hint"),
    ]
    if group is not None and group.mode in MODES:
        # Действующий способ виден в тексте, а не только подписью кнопки:
        # автор сразу понимает, что произойдёт при повторном выборе.
        lines.append(
            tr(
                "templates.cond.mode_current",
                label=tr(MODE_TITLE_KEYS[group.mode]),
            )
        )
    rows: list[list[InlineKeyboardButton]] = []
    for mode, key in (
        (MODE_LINE, "templates.cond.mode_line_button"),
        (MODE_SEPARATOR, "templates.cond.mode_sep_button"),
        (MODE_FRAGMENT, "templates.cond.mode_custom_button"),
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    tp._short(tr(key)),
                    callback_data=f"tp_condf:{template_id}:{mode}",
                )
            ]
        )
    if group is not None:
        # У настроенного кирпичика удаление живёт здесь: отдельного списка
        # правил в 2.5.3 §3 нет.
        rows.append(
            [
                InlineKeyboardButton(
                    tr("templates.cond.delete_item"),
                    callback_data=(
                        f"tp_condd:{template_id}:{group.flag}:{group.brick_id}"
                    ),
                )
            ]
        )
    rows.append([_back(f"tp_condback:{template_id}:bricks")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _custom_content(
    template_id: int, entry: BrickEntry
) -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join(
        [
            tr("templates.cond.label", title=entry.title),
            "",
            tr("templates.cond.custom_example"),
            "",
            tr("templates.cond.custom_prompt"),
        ]
    )
    keyboard = InlineKeyboardMarkup([[_back(f"tp_condback:{template_id}:mode")]])
    return text, keyboard


def _delete_content(
    group: RuleGroup, template_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    text = tr(
        "templates.cond.delete_confirm",
        title=group.title,
        mode=group.mode_title(),
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("templates.cond.delete_yes"),
                    callback_data=(
                        f"tp_conddy:{template_id}:{group.flag}:{group.brick_id}"
                    ),
                )
            ],
            [_back(f"tp_condback:{template_id}:bricks")],
        ]
    )
    return text, keyboard


# --- Актуальность правил (§44) ----------------------------------------------


def stale_count(session, template: "tp.TemplateDetails", telegram_user_id: int) -> int:
    """
    Сколько условий расширенного шаблона требуют перенастройки (плашка карточки).

    Только ЧТЕНИЕ. Актуальность правил проверяет `template_store` в момент
    создания новой версии текста: карточка не имеет права менять доменные
    данные при отрисовке (иначе порядок экранов влиял бы на БД). Правила
    секции `content` в счёт не входят: условий у простого текста нет (§3), а
    `refresh_conditional_parts` такие строки удаляет при сохранении версии.
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
        if part.section == ALLOWED_SECTION
        and (part.status == STATUS_STALE or part.version_id != current_version_id)
    )


# --- Общие помощники маршрута ------------------------------------------------


def _editable(session, state: dict, telegram_user_id: int, template_id: int):
    """
    Шаблон, который автор всё ещё может менять.

    Права проверяет `tp._load_editable` (для стандарта — действующий админ
    места или суперадмин). Признак `origin` здесь не используется: движок
    правил один для личных шаблонов и стандарта места (Poster 2.5.2 §6).
    """
    return tp._load_editable(session, telegram_user_id, template_id)


def _guard(
    update: Update, context: ContextTypes.DEFAULT_TYPE, template_id: int
) -> dict | None:
    """Общая проверка: экран правил открыт из актуальной карточки."""
    cond = _state(ctx.data(context))
    if cond.get("template_id") != template_id or cond.get("step") is None:
        return None
    return cond


async def _stale(update: Update) -> None:
    await _answer(update, tp.STALE_TEXT, show_alert=True)


async def _answer(
    update: Update, text: str | None = None, *, show_alert: bool = False
) -> None:
    """
    Короткий ответ на действие автора.

    Свой фрагмент приходит текстовым сообщением (события callback нет),
    поэтому ответ отправляется только тогда, когда он вообще возможен:
    иначе всплывающая подсказка уронила бы обработчик.
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer(text, show_alert=show_alert)


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


def _section_brick(data: str, prefix: str) -> tuple[int, str, int] | None:
    """Разбирает `prefix:<template>:<c|e>:<brick>`; иначе `None`."""
    if not data.startswith(prefix):
        return None
    raw = data[len(prefix) :].split(":")
    if len(raw) != 3:
        return None
    try:
        template_id, brick_id = int(raw[0]), int(raw[2])
    except ValueError:
        return None
    section = FLAG_SECTIONS.get(raw[1])
    if section is None:
        return None
    return template_id, section, brick_id


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


def _find_group(
    groups: list[RuleGroup], section: str, brick_id: int
) -> RuleGroup | None:
    for group in groups:
        if group.section == section and group.brick_id == brick_id:
            return group
    return None


def _content_for(
    session,
    template: "tp.TemplateDetails",
    cond: dict,
    telegram_user_id: int,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """
    Содержимое текущего шага; чистая функция от домена и состояния.

    `None` — выбранный кирпичик исчез из текста (правка в другом окне).
    Вызывающий возвращает автора к списку кирпичиков, а не рисует экран с
    пустым правилом.
    """
    template_id = template.template_id
    step = cond.get("step")
    groups = _group_rules(session, telegram_user_id, template)
    entries = _entries(template)
    page = int(cond.get("page") or 0)
    if step in (STEP_LIST, STEP_BRICKS):
        # Единственный экран раздела: список кирпичиков расширенного текста
        # (2.5.3 §3). Отдельного шага «выберите кирпичик» больше нет.
        return _bricks_content(entries, groups, template_id, page)
    section = str(cond.get("section") or ALLOWED_SECTION)
    brick_id = int(cond.get("brick_id") or 0)
    entry = _entry(template, section, brick_id)
    if step == STEP_DELETE:
        group = _find_group(groups, section, brick_id)
        if group is None:
            cond["step"] = STEP_BRICKS
            return _bricks_content(entries, groups, template_id, page)
        return _delete_content(group, template_id)
    if entry is None:
        return None
    if step == STEP_MODE:
        group = _find_group(groups, section, brick_id)
        return _mode_content(template_id, entry, group)
    if step == STEP_CUSTOM:
        return _custom_content(template_id, entry)
    return None


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
    Если шаблон исчез, пропали права или исчезла расширенная версия, автор
    возвращается в карточку с понятным сообщением (`tp._render_phase` сам
    восстановит хаб).
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
        if template is None or not tp.conditional_parts_available(template):
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
            # Кирпичик исчез после правки текста в другом окне.
            cond["step"] = STEP_BRICKS
            cond["error"] = None
            cond["notice"] = tr("templates.cond.entry_gone")
            content = _content_for(session, template, cond, telegram_user_id)
            if content is None:
                content = _bricks_content(
                    _entries(template),
                    _group_rules(session, telegram_user_id, template),
                    template.template_id,
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
    prefixes = [prefix for prefix in (notice, cond.get("notice")) if prefix]
    cond.pop("notice", None)
    if cond.get("error"):
        # Ошибка шага живёт до следующего действия: автор должен увидеть,
        # почему фрагмент не принят, и остаться на том же экране.
        prefixes.append(str(cond["error"]))
    shown = "\n\n".join([*prefixes, text]) if prefixes else text
    body = shown
    return await tp._show(update, context, state, body, keyboard, drop_input=drop_input)


# --- Обработчики шагов -------------------------------------------------------


async def handle_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Карточка расширенного шаблона → «🧩 Условные части»."""
    query = ctx.query(update)
    template_id = tp._callback_id(query.data or "", "tp_cond:")
    if template_id is None:
        template_id = tp._callback_id(query.data or "", "tp_condl:")
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
        if template is None or not tp.conditional_parts_available(template):
            # Простой шаблон правила не показывает и не заводит (§6).
            await query.answer(tp.DENIED_TEXT, show_alert=True)
            return
        await query.answer()
    finally:
        session.close()
    cond = _new_state(template_id)
    cond["user_id"] = telegram_user_id
    await _render_cond(update, context, cond)


async def handle_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Навигация `◀️ | 1 / 3 | ▶️` по списку кирпичиков."""
    query = ctx.query(update)
    ids = _ids(query.data or "", "tp_condp:", 2)
    if ids is None:
        await _stale(update)
        return
    template_id, offset = ids
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    await query.answer()
    cond["step"] = STEP_BRICKS
    cond["page"] = max(offset, 0) // pagination.PAGE_SIZE
    await _render_cond(update, context, cond)


async def handle_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Прежняя кнопка «➕ Новая условная часть».

    В 2.5.3 §3 отдельного шага создания нет: экран сразу показывает все
    кирпичики расширенного текста. Старая кнопка из отправленного ранее
    сообщения безопасно открывает этот же список.
    """
    query = ctx.query(update)
    template_id = tp._callback_id(query.data or "", "tp_condadd:")
    cond = _guard(update, context, template_id) if template_id is not None else None
    if cond is None:
        await _stale(update)
        return
    await query.answer()
    _cond_input_on(context, cond, flag=False)
    cond.update({"step": STEP_BRICKS, "brick_id": None, "error": None})
    await _render_cond(update, context, cond)


def _bricks_target(data: str) -> tuple[int, str, int] | None:
    """
    Разбирает выбор кирпичика.

    Действующий протокол — `tp_condb:<template>:<c|e>:<brick>`. Прежние
    варианты (`tp_condb:<template>:<brick>` и `…:<c|e>:<brick>:<вхождение>`)
    принимаются ради старых кнопок: номер вхождения в интерфейсе больше не
    показывается, но нажатие не должно ломаться.
    """
    data = data or ""
    target = _section_brick(data, "tp_condb:")
    if target is not None:
        return target
    if not data.startswith("tp_condb:"):
        return None
    raw = data[len("tp_condb:") :].split(":")
    if len(raw) == 2:
        try:
            return int(raw[0]), SECTION_EXTENDED_CONTENT, int(raw[1])
        except ValueError:
            return None
    if len(raw) == 3 and raw[1] in FLAG_SECTIONS:
        try:
            return int(raw[0]), FLAG_SECTIONS[raw[1]], int(raw[2])
        except ValueError:
            return None
    if len(raw) == 4 and raw[1] in FLAG_SECTIONS:
        try:
            return int(raw[0]), FLAG_SECTIONS[raw[1]], int(raw[2])
        except ValueError:
            return None
    return None


async def handle_bricks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбран кирпичик расширенного текста: автор выбирает способ скрытия."""
    query = ctx.query(update)
    target = _bricks_target(query.data or "")
    if target is None:
        await _stale(update)
        return
    template_id, section, brick_id = target
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    if section != ALLOWED_SECTION:
        # Старая кнопка с секцией простого текста: условий у него нет (§3).
        # Отказ без записи в БД: доменный слой даже не вызывается.
        await query.answer(tp.STALE_TEXT, show_alert=True)
        cond["step"] = STEP_BRICKS
        await _render_cond(update, context, cond)
        return
    telegram_user_id = ctx.user(update).id
    session = tp.SessionLocal()
    try:
        template = _editable(session, cond, telegram_user_id, template_id)
        entry = (
            _entry(template, ALLOWED_SECTION, brick_id)
            if template is not None
            else None
        )
        groups = (
            _group_rules(session, telegram_user_id, template)
            if template is not None
            else []
        )
    finally:
        session.close()
    if entry is None:
        # Кирпичик есть в старом правиле, но не в тексте: правило всё равно
        # должно остаться доступным (§44).
        group = _find_group(groups, ALLOWED_SECTION, brick_id)
        if group is None:
            await query.answer(tr("templates.cond.entry_gone"), show_alert=True)
            cond["step"] = STEP_BRICKS
            await _render_cond(update, context, cond)
            return
        entry = BrickEntry(
            section=group.section,
            brick_id=group.brick_id,
            code=group.code,
            title=group.title,
            occurrences=(),
        )
    await query.answer()
    cond.update(
        {
            "step": STEP_MODE,
            "section": entry.section,
            "brick_id": brick_id,
            "error": None,
        }
    )
    await _render_cond(update, context, cond)


async def handle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбран способ скрытия: сразу сохраняем одно правило на все вхождения."""
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
    mode = parts[1]
    if mode not in MODES:
        await _stale(update)
        return
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    if mode == MODE_FRAGMENT:
        await query.answer()
        cond["step"] = STEP_CUSTOM
        cond["error"] = None
        _cond_input_on(context, cond, flag=True)
        await _render_cond(update, context, cond)
        return
    await query.answer()
    await _apply_mode(update, context, cond, mode)


async def _apply_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    cond: dict,
    mode: str,
    custom: str | None = None,
) -> None:
    """
    Сохраняет правило выбранного способа.

    Один кирпичик — одно правило: строки прежнего правила этого кирпичика
    заменяются целиком, поэтому повторное применение не создаёт дублей.
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
        section = str(cond.get("section") or ALLOWED_SECTION)
        if section != ALLOWED_SECTION:
            # Условия принадлежат только расширенному тексту (§3): отказ без
            # записи в БД.
            await _answer(update, tp.STALE_TEXT, show_alert=True)
            cond.update({"step": STEP_BRICKS, "brick_id": None})
            await _render_cond(update, context, cond)
            return
        brick_id = int(cond.get("brick_id") or 0)
        entry = _entry(template, section, brick_id)
        if entry is None:
            cond["step"] = STEP_BRICKS
            cond["error"] = tr("templates.cond.entry_gone")
            await _render_cond(update, context, cond)
            return
        text = _section_text(template, section)
        fragments = _mode_fragments(text, entry.code, mode, custom)
        if not fragments:
            cond["error"] = tr(
                "templates.cond.custom_not_all"
                if custom
                else "templates.cond.proposal_unavailable"
            )
            cond["step"] = STEP_CUSTOM if custom else STEP_MODE
            await _render_cond(update, context, cond)
            return
        result = replace_conditional_rules(
            session,
            actor_telegram_user_id=telegram_user_id,
            template_id=template_id,
            expected_revision=template.revision,
            section=section,
            brick_id=brick_id,
            fragments=fragments,
        )
    except Exception as exc:  # noqa: BLE001 — автор не должен видеть traceback
        logger.exception("Не удалось сохранить условную часть: %s", exc)
        await _answer(update, tr("templates.cond.save_failed"), show_alert=True)
        return
    finally:
        session.close()
    if result.status == "stale":
        await _answer(update, tr("templates.cond.template_changed"), show_alert=True)
        cond.update({"step": STEP_BRICKS, "brick_id": None})
        _cond_input_on(context, cond, flag=False)
        await _render_cond(update, context, cond)
        return
    if not result.ok:
        await _answer(update)
        cond["error"] = result.message or tr("templates.cond.save_failed")
        cond["step"] = STEP_CUSTOM if custom else STEP_MODE
        await _render_cond(update, context, cond)
        return
    # Ответ на callback — подсказка; при вводе текста его нет, и это норма.
    await _answer(update, tr("templates.cond.saved_toast"))
    _cond_input_on(context, cond, flag=False)
    cond.update(
        {
            "step": STEP_BRICKS,
            "brick_id": None,
            "error": None,
        }
    )
    await _render_cond(update, context, cond)


def _resolve_edit_target(
    session,
    telegram_user_id: int,
    template_id: int,
    data: str,
) -> tuple[str, int] | None:
    """
    Кирпичик для правки: действующий протокол или прежний `:<id правила>`.

    Старый протокол передавал id правила; он принимается, чтобы нажатие
    кнопки, отправленной до обновления, не привело к пустому экрану.
    """
    target = _section_brick(data, "tp_conde:")
    if target is not None:
        return target[1], target[2]
    ids = _ids(data, "tp_conde:", 2)
    if ids is None:
        return None
    part = next(
        (
            row
            for row in _load_parts(session, telegram_user_id, template_id)
            if row.id == ids[1]
        ),
        None,
    )
    if part is None:
        return None
    return part.section, part.brick_id


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«✏️ Изменить» из компактного списка: те же три способа скрытия."""
    query = ctx.query(update)
    data = query.data or ""
    ids = _ids(data, "tp_conde:", 2)
    target = _section_brick(data, "tp_conde:")
    if ids is None and target is None:
        await _stale(update)
        return
    template_id = ids[0] if ids is not None else target[0]  # type: ignore[index]
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    telegram_user_id = ctx.user(update).id
    session = tp.SessionLocal()
    try:
        template = _editable(session, cond, telegram_user_id, template_id)
        resolved = (
            _resolve_edit_target(session, telegram_user_id, template_id, data)
            if template is not None
            else None
        )
        groups = (
            _group_rules(session, telegram_user_id, template)
            if template is not None
            else []
        )
    finally:
        session.close()
    if resolved is None:
        await query.answer(tr("templates.cond.rule_gone"), show_alert=True)
        cond.update({"step": STEP_BRICKS, "brick_id": None})
        await _render_cond(update, context, cond)
        return
    section, brick_id = resolved
    group = _find_group(groups, section, brick_id)
    if group is None:
        await query.answer(tr("templates.cond.rule_gone"), show_alert=True)
        cond.update({"step": STEP_BRICKS, "brick_id": None})
        await _render_cond(update, context, cond)
        return
    await query.answer()
    cond.update(
        {
            "step": STEP_MODE,
            "section": group.section,
            "brick_id": group.brick_id,
            "error": None,
        }
    )
    await _render_cond(update, context, cond)


def _group_for_callback(
    session,
    telegram_user_id: int,
    template_id: int,
    data: str,
    prefix: str,
) -> RuleGroup | None:
    """
    Правило по callback-данным удаления.

    Действующий протокол — `:<секция>:<кирпичик>`; прежний `:<id правила>`
    принимается, чтобы старая кнопка не открывала пустой экран.
    """
    template = tp._load_editable(session, telegram_user_id, template_id)
    if template is None:
        return None
    groups = _group_rules(session, telegram_user_id, template)
    target = _section_brick(data, prefix)
    if target is not None:
        return _find_group(groups, target[1], target[2])
    ids = _ids(data, prefix, 2)
    if ids is None:
        return None
    brick_id = ids[1]
    for group in groups:
        if group.brick_id == brick_id or brick_id in group.part_ids:
            return group
    return None


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Удалить» — экран подтверждения, правило удаляется только явно."""
    query = ctx.query(update)
    data = query.data or ""
    ids = _ids(data, "tp_condd:", 2)
    target = _section_brick(data, "tp_condd:")
    if ids is None and target is None:
        await _stale(update)
        return
    template_id = ids[0] if ids is not None else target[0]  # type: ignore[index]
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    telegram_user_id = ctx.user(update).id
    session = tp.SessionLocal()
    try:
        group = _group_for_callback(
            session, telegram_user_id, template_id, data, "tp_condd:"
        )
    finally:
        session.close()
    if group is None:
        await query.answer(tr("templates.cond.rule_gone"), show_alert=True)
        cond.update({"step": STEP_BRICKS, "brick_id": None})
        await _render_cond(update, context, cond)
        return
    await query.answer()
    cond.update(
        {
            "step": STEP_DELETE,
            "section": group.section,
            "brick_id": group.brick_id,
            "error": None,
        }
    )
    await _render_cond(update, context, cond)


async def handle_delete_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подтверждённое удаление правила целиком (все вхождения кирпичика)."""
    query = ctx.query(update)
    data = query.data or ""
    ids = _ids(data, "tp_conddy:", 2)
    target = _section_brick(data, "tp_conddy:")
    if ids is None and target is None:
        await _stale(update)
        return
    template_id = ids[0] if ids is not None else target[0]  # type: ignore[index]
    cond = _guard(update, context, template_id)
    if cond is None:
        await _stale(update)
        return
    telegram_user_id = ctx.user(update).id
    session = tp.SessionLocal()
    try:
        template = _editable(session, cond, telegram_user_id, template_id)
        group = _group_for_callback(
            session, telegram_user_id, template_id, data, "tp_conddy:"
        )
        if template is None or group is None:
            await query.answer(tr("templates.cond.rule_gone"), show_alert=True)
            cond.update({"step": STEP_BRICKS, "brick_id": None})
            await _render_cond(update, context, cond)
            return
        result = replace_conditional_rules(
            session,
            actor_telegram_user_id=telegram_user_id,
            template_id=template_id,
            expected_revision=template.revision,
            section=group.section,
            brick_id=group.brick_id,
            fragments=(),
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
    cond.update({"step": STEP_BRICKS, "brick_id": None, "error": None})
    await _render_cond(update, context, cond)


async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Назад» внутри раздела правил — фактический предыдущий шаг."""
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
        # остаются прежними.
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
    # Прежний шаг «список правил» в 2.5.3 §3 — тот же экран кирпичиков.
    cond["step"] = STEP_BRICKS if target == STEP_LIST else target
    cond["error"] = None
    if cond["step"] in {STEP_LIST, STEP_BRICKS}:
        cond["brick_id"] = None
    await _render_cond(update, context, cond)


async def handle_custom_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Свой фрагмент: применяем ко всем вхождениям кирпичика (§6)."""
    cond = _state(ctx.data(context))
    if cond.get("step") != STEP_CUSTOM:
        return
    template_id = cond.get("template_id")
    if not isinstance(template_id, int):
        return
    message = ctx.message(update)
    # Срезаются только внешние переводы строк: ведущий пробел фрагмента
    # значим (§42-В: ` ([адрес])` — часть авторского текста).
    fragment = (message.text or "").strip("\n")
    if not fragment.strip():
        cond["error"] = tr("templates.cond.custom_error_empty")
        await _render_cond(update, context, cond)
        return
    await _apply_mode(update, context, cond, MODE_FRAGMENT, fragment)


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
    """Обработчики раздела «Условные части» шаблона (Poster 2.5.2 §6–7)."""
    return [
        CallbackQueryHandler(handle_open, pattern=r"^tp_cond:\d+$"),
        CallbackQueryHandler(handle_page, pattern=r"^tp_condp:\d+:\d+$"),
        CallbackQueryHandler(handle_add, pattern=r"^tp_condadd:\d+$"),
        CallbackQueryHandler(
            handle_bricks,
            pattern=r"^tp_condb:\d+(?::[ce])?:\d+(?::\d+)?$",
        ),
        CallbackQueryHandler(handle_mode, pattern=r"^tp_condf:\d+:(line|sep|custom)$"),
        CallbackQueryHandler(
            handle_edit, pattern=r"^tp_conde:\d+(?::[ce])?:\d+(?::\d+)?$"
        ),
        CallbackQueryHandler(
            handle_delete, pattern=r"^tp_condd:\d+(?::[ce])?:\d+(?::\d+)?$"
        ),
        CallbackQueryHandler(
            handle_delete_yes, pattern=r"^tp_conddy:\d+(?::[ce])?:\d+(?::\d+)?$"
        ),
        CallbackQueryHandler(handle_back, pattern=r"^tp_condback:\d+:\w+$"),
        CallbackQueryHandler(handle_open, pattern=r"^tp_condl:\d+$"),
        CondTextHandler(filters.TEXT & ~filters.COMMAND, handle_custom_text),
    ]
