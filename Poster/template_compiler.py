# template_compiler.py
"""
Компилятор шаблонов анонсов (Stage 24 → Poster 2.3 H2).

Разделяет две операции:

1.  **Разбор и связывание** (`resolve_template_bindings`): находит токены
    `[код]`, проверяет грамматику и определяет доступных кандидатов.
    Возвращает привязки к постоянным `brick_id` для сохранения версии.
    При ошибке частично корректная версия не сохраняется.

2.  **Выполнение** (`render_template`): собирает итоговый текст анонса из
    связанного текста шаблона и значений кирпичиков. Обозначения не
    разрешаются заново: используются сохранённые brick_id.

3.  **Единый рендер анонса** (`render_announcement`): тот же результат плюс
    решение о технической заглушке `.`. Poster 2.0 использует его и для
    живого верхнего блока (S3), и для итоговой отправки (S4): один сборщик,
    одно поведение.

Poster 2.3 H2: шаблон — единый текст `content` и необязательный
`extended_content`. Строка шаблона, в которой есть токены кирпичиков, но
все они отрендерились пустыми, скрывается целиком: исчезает и висячий
литерал (`//`, `—`), и пустая разметка (`<b></b>`). Строки без токенов
выводятся буквально, включая литеральные разделители.

Границы этапа:

- Telegram-редактор шаблонов — Stage 25;
- согласование новых кирпичиков — Stage 26;
- подключение снимков к мастеру постов — Stage 27;
- живой/итоговый рендер Poster 2.0 — S3.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from announcement_wizard import visible_announcement_text
from brick_store import BrickDefinition, resolve_brick_code_candidates
from brick_types import (
    BRICK_SCOPE_SYSTEM,
    validate_brick_value,
)
from template_parser import (
    BrickNode,
    FormatNode,
    LinkNode,
    TemplateParseError,
    TextNode,
    parse_template,
)
from template_types import TEMPLATE_CONTENT, TEMPLATE_EXTENDED_CONTENT
from utils.calendar_view import format_announcement_date
from utils.tg_html import SafeHtml, strip_link_tags, utf16_len
from utils.validators import normalize_url

# Poster 2.5 §17: код системного кирпичика даты. Отображение `ДД.ММ (дн)`
# применяется только к нему; пользовательский кирпичик типа date не трогаем.
SYSTEM_DATE_CODE = "дата"

# Порядок обработки текстов шаблона предсказуем: сначала канонический, затем
# расширенный. Ключи — те же, что в моделях и снимках.
_TEXT_ORDER = (TEMPLATE_CONTENT, TEMPLATE_EXTENDED_CONTENT)

logger = logging.getLogger(__name__)


class TemplateCompileError(ValueError):
    """Ошибка разбора, привязки или исполнения шаблона."""


class TemplateBindingConflictError(TemplateCompileError):
    """Неоднозначный `[код]` требует явного выбора brick_id пользователем."""


@dataclass(frozen=True)
class BindingCandidate:
    """Один кирпичик-кандидат для неоднозначного кода."""

    brick_id: int
    code: str
    title: str
    scope: str
    status: str

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"Candidate(id={self.brick_id}, code={self.code!r}, scope={self.scope!r})"
        )


@dataclass(frozen=True)
class AmbiguousCode:
    """
    Код, по которому найдено несколько доступных кирпичиков.

    Редактор обязан показать список кандидатов и сохранить выбранный
    `brick_id` в привязках версии.
    """

    code: str
    candidates: tuple[BindingCandidate, ...] = ()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"AmbiguousCode(code={self.code!r}, " f"candidates={len(self.candidates)})"
        )


@dataclass
class TemplateBindings:
    """Результат разбора и привязки одной версии шаблона."""

    resolved_brick_ids: dict[str, int] = field(default_factory=dict)
    ambiguous_codes: list[AmbiguousCode] = field(default_factory=list)
    unknown_codes: list[str] = field(default_factory=list)
    parse_errors: list[TemplateParseError] = field(default_factory=list)
    tokens_by_text: dict[str, list[Any]] = field(default_factory=dict)
    # Текст шаблона, в котором встретился токен: нужен диагностике.
    token_origins: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (
            not self.ambiguous_codes
            and not self.unknown_codes
            and not self.parse_errors
        )


def _binding_candidate(definition: BrickDefinition) -> BindingCandidate:
    return BindingCandidate(
        brick_id=definition.id,
        code=definition.code,
        title=definition.title,
        scope=definition.scope,
        status=definition.status,
    )


def _resolve_codes_in_text(
    session: Session,
    tokens: Sequence[Any],
    *,
    actor_telegram_user_id: int | None,
    place_id: int | None,
    chosen_brick_ids: Mapping[str, int] | None = None,
) -> tuple[dict[str, int], list[AmbiguousCode], list[str]]:
    """
    Внутренняя привязка одного текста: заполняет `brick_id` у `BrickNode`.

    Дважды повторяющиеся коды одного варианта не создают дублирующих
    привязок; каждая привязка однозначно указывает на конкретный brick_id.

    `chosen_brick_ids` — явный выбор пользователя для неоднозначных кодов.
    Он используется только для кодов, по которым найдено несколько
    кандидатов, и только если выбранный brick_id есть среди кандидатов.
    Для однозначных кодов выбор вызывающего кода игнорируется — так
    невозможно обойти проверку, передав произвольный список ID.
    """
    resolved: dict[str, int] = {}
    ambiguous: list[AmbiguousCode] = []
    unknown: list[str] = []

    def _visit(node: Any) -> None:
        if isinstance(node, BrickNode):
            code = node.code
            if code in resolved:
                node.brick_id = resolved[code]
                return
            candidates = resolve_brick_code_candidates(
                session,
                code,
                viewer_telegram_user_id=actor_telegram_user_id,
                place_id=place_id,
            )
            if candidates.blocked_by_archived_system:
                unknown.append(code)
                return
            if candidates.ambiguous:
                explicit = (chosen_brick_ids or {}).get(code)
                if explicit is not None:
                    match = next(
                        (row for row in candidates.candidates if row.id == explicit),
                        None,
                    )
                    if match is not None:
                        resolved[code] = explicit
                        node.brick_id = explicit
                        return
                ambiguous.append(
                    AmbiguousCode(
                        code=code,
                        candidates=tuple(
                            _binding_candidate(row) for row in candidates.candidates
                        ),
                    )
                )
                return
            if candidates.selected is None:
                unknown.append(code)
                return
            resolved[code] = candidates.selected.id
            node.brick_id = candidates.selected.id
        elif isinstance(node, LinkNode):
            _visit_token_list(node.label)
            if isinstance(node.url, BrickNode):
                _visit(node.url)
        elif hasattr(node, "children"):
            _visit_token_list(getattr(node, "children"))

    def _visit_token_list(children: Iterable[Any]) -> None:
        for child in children:
            _visit(child)

    for token in tokens:
        _visit(token)
    return resolved, ambiguous, unknown


def resolve_template_bindings(
    session: Session,
    texts: Mapping[str, str],
    *,
    actor_telegram_user_id: int | None,
    place_id: int | None,
    chosen_brick_ids: Mapping[str, int] | None = None,
) -> TemplateBindings:
    """
    Разбирает тексты шаблона и возвращает привязки для сохранения версии.

    Ключи `texts` — `content` и (необязательный) `extended_content`.
    Тексты обрабатываются в стабильном порядке, чтобы порядок ошибок был
    предсказуемым. Список `unknown_codes` содержит неизвестные коды, для
    которых не найдено ни одного доступного кирпичика.

    `chosen_brick_ids` — явный выбор пользователя для неоднозначных кодов:
    отображение «код → brick_id». Выбор применяется только если выбранный
    brick_id действительно является кандидатом для этого кода; для
    однозначных кодов выбор вызывающего кода игнорируется.
    """
    bindings = TemplateBindings()
    for text_name in _TEXT_ORDER:
        raw = texts.get(text_name)
        if raw is None:
            continue
        try:
            bindings.tokens_by_text[text_name] = parse_template(raw)
        except TemplateParseError as exc:
            bindings.parse_errors.append(exc)
            continue
    if bindings.parse_errors:
        return bindings

    for text_name, tokens in bindings.tokens_by_text.items():
        resolved, ambiguous, unknown = _resolve_codes_in_text(
            session,
            tokens,
            actor_telegram_user_id=actor_telegram_user_id,
            place_id=place_id,
            chosen_brick_ids=chosen_brick_ids,
        )
        for code, brick_id in resolved.items():
            bindings.resolved_brick_ids.setdefault(code, brick_id)
        for ambiguous_code in ambiguous:
            if any(
                item.code == ambiguous_code.code for item in bindings.ambiguous_codes
            ):
                continue
            bindings.ambiguous_codes.append(ambiguous_code)
        for code in unknown:
            if code not in bindings.unknown_codes:
                bindings.unknown_codes.append(code)
    return bindings


def _system_date_text(raw: Any) -> str | None:
    """
    `ДД.ММ (дн)` для системного кирпичика даты; `None` — если значение
    не разбирается как дата (тогда остаётся прежний вывод без догадок).
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.strptime(raw.strip(), "%d.%m.%Y").date()
    except (TypeError, ValueError):
        return None
    return format_announcement_date(parsed)


def _is_system_date(definition: Any) -> bool:
    """
    Системный кирпичик даты ли это?

    `BrickDefinition` из хранилища знает `scope`; `SystemBrickDefinition`
    из каталога `brick_types` — нет, потому что там лежат только системные
    определения. Отсутствие `scope` трактуем как system, иначе golden-тесты
    на чистом каталоге падали бы на `AttributeError`.
    """
    if getattr(definition, "code", None) != SYSTEM_DATE_CODE:
        return False
    scope = getattr(definition, "scope", None)
    return scope is None or scope == BRICK_SCOPE_SYSTEM


def _brick_text(value: Any, definition: BrickDefinition) -> str:
    """
    Стабильный текст значения кирпичика с строгой валидацией.

    Все семь типов проверяются через `validate_brick_value` — значение
    не подменяется молча на short_text, ошибки данных не превращаются
    в правдоподобный, но некорректный анонс.

    Poster 2.5 §17: системный кирпичик даты (`[дата]`, scope=system)
    выводится как `ДД.ММ (дн)` — год остаётся только во внутреннем
    хранении `payload["date"]` = ДД.ММ.ГГГГ. Пользовательский кирпичик
    типа date сохраняет прежний формат: правило относится только к
    системному `[дата]`.
    """
    result = validate_brick_value(definition.value_type, value, rules=definition.rules)
    if not result.is_valid:
        raise TemplateCompileError(
            f"Значение кирпичика «{definition.code}» некорректно: {result.error}"
        )
    payload = result.payload or {}
    if _is_system_date(definition) and "date" in payload:
        formatted = _system_date_text(payload["date"])
        if formatted is not None:
            return formatted
    for key in ("text", "date", "time", "url", "number", "choice"):
        if key in payload:
            return str(payload[key])
    if "choices" in payload:
        choices = payload["choices"]
        if isinstance(choices, (list, tuple)):
            return ", ".join(str(item) for item in choices)
    return str(payload) if payload else ""


def _safe_url(value: Any, *, code: str = "") -> str:
    """
    Безопасный адрес гиперссылки из значения кирпичика.

    Проверяет схему http/https, отклоняет `javascript:`, `data:`, `file:`
    и произвольный текст. Некорректный адрес — ошибка, а не правдоподобный
    анонс с битой ссылкой.
    """
    text = str(value) if value is not None else ""
    normalized = normalize_url(text)
    if normalized is None:
        raise TemplateCompileError(
            f"Значение кирпичика «{code}» не является корректной "
            f"http(s)-ссылкой: {text!r}."
        )
    return normalized


_FORMAT_TAGS = {
    "**": "b",
    "*": "i",
    "__": "u",
    "~~": "s",
    "||": "tg-spoiler",
    "`": "code",
    "```": "pre",
}


def _escape_html(text: str, *, quote: bool = False) -> str:
    """Экранирует HTML-спецсимволы для безопасного вывода в Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        if quote
        else text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


@dataclass
class _Rendered:
    """
    Результат узла: HTML, наличие токена кирпичика и его пустота.

    `has_brick` — в узле есть хотя бы один токен кирпичика.
    `bricks_empty` — все токены кирпичиков узла отрендерились пустыми.
    """

    html: str = ""
    has_brick: bool = False
    bricks_empty: bool = True
    has_newline: bool = False


def _brick_value_empty(
    brick_id: int,
    values_by_id: Mapping[int, Any],
    definitions_by_id: Mapping[int, BrickDefinition],
) -> bool:
    """
    Пусто ли значение кирпичика для рендера и для условных частей (§41).

    Единственный источник правды: и `_render_node`, и вырезание фрагментов
    обязаны считать пустым одно и то же. Пусто — нет значения в
    `values_by_id`, `None`, строка из пробелов или значение, у которого
    после подстановки нет видимого текста (`""`, `<b></b>`).
    """
    if brick_id not in values_by_id:
        return True
    value = values_by_id[brick_id]
    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    if isinstance(value, SafeHtml):
        # U1b: значение уже прошло allow-list; видимость считаем по HTML.
        return not visible_announcement_text(str(value))
    definition = definitions_by_id.get(brick_id)
    if definition is None:
        raise TemplateCompileError(
            f"Определение кирпичика #{brick_id} не найдено: "
            "нельзя подменить тип значения на short_text."
        )
    return not visible_announcement_text(_escape_html(_brick_text(value, definition)))


def _render_node(
    node: Any,
    values_by_id: Mapping[int, Any],
    definitions_by_id: Mapping[int, BrickDefinition],
) -> _Rendered:
    """
    Визуализирует один узел, подставляя значения кирпичиков.

    Значения валидируются по типу кирпичика (`validate_brick_value`):
    дата/время/число/выбор/текст/ссылка — все семь типов. URL дополнительно
    нормализуется и проверяется на схему непосредственно перед созданием
    гиперссылки. Отсутствующее значение пропускает токен (необязательное
    поле); некорректное значение — диагностируемая ошибка, а не вывод
    `None`/`[]` в готовом анонсе.
    """
    if isinstance(node, TextNode):
        return _Rendered(
            html=_escape_html(node.text),
            has_newline="\n" in node.text,
        )
    if isinstance(node, BrickNode):
        if node.brick_id is None:
            raise TemplateCompileError(
                "Токен кирпичика без привязки brick_id: версия шаблона повреждена."
            )
        definition = definitions_by_id.get(node.brick_id)
        if definition is None:
            raise TemplateCompileError(
                f"Определение кирпичика #{node.brick_id} не найдено: "
                "нельзя подменить тип значения на short_text."
            )
        if node.brick_id not in values_by_id:
            return _Rendered(has_brick=True, bricks_empty=True)
        value = values_by_id[node.brick_id]
        if value is None or (isinstance(value, str) and not value.strip()):
            return _Rendered(has_brick=True, bricks_empty=True)
        if isinstance(value, SafeHtml):
            # U1b: значение уже прошло allow-list (разметка сохранена,
            # текст экранирован) — повторно не экранируем.
            text = str(value)
        else:
            text = _escape_html(_brick_text(value, definition))
        return _Rendered(
            html=text,
            has_brick=True,
            bricks_empty=not visible_announcement_text(text),
        )
    if isinstance(node, LinkNode):
        label = _render_children(node.label, values_by_id, definitions_by_id)
        has_brick = label.has_brick
        bricks_empty = label.bricks_empty if label.has_brick else True
        if not label.html:
            return _Rendered(
                has_brick=has_brick,
                bricks_empty=True,
                has_newline=False,
            )
        if isinstance(node.url, BrickNode):
            if node.url.brick_id is None:
                raise TemplateCompileError(
                    "URL ссылки без привязки brick_id: версия шаблона повреждена."
                )
            url_definition = definitions_by_id.get(node.url.brick_id)
            if url_definition is None:
                raise TemplateCompileError(
                    f"Определение кирпичика #{node.url.brick_id} не найдено."
                )
            has_brick = True
            if node.url.brick_id not in values_by_id:
                # URL-кирпичик отсутствует: пустых/битых ссылок не
                # выводим, но подпись остаётся обычным текстом —
                # ровно это обещает контракт стандартного шаблона.
                return _Rendered(
                    html=label.html,
                    has_brick=True,
                    bricks_empty=label.bricks_empty,
                    has_newline=label.has_newline,
                )
            url_value = values_by_id[node.url.brick_id]
            if not url_value or (isinstance(url_value, str) and not url_value.strip()):
                return _Rendered(
                    html=label.html,
                    has_brick=True,
                    bricks_empty=label.bricks_empty,
                    has_newline=label.has_newline,
                )
            url = _safe_url(url_value, code=url_definition.code)
        else:
            url = _normalize_render_url(node.url)
        if not url:
            return _Rendered(
                html=label.html,
                has_brick=has_brick,
                bricks_empty=bricks_empty,
                has_newline=label.has_newline,
            )
        # U1b-fix: значение с URL-entity уже содержит <a>; в подписи
        # ссылки оставляем внешнюю обёртку — вложенные ссылки Telegram
        # отклоняет. Текст подписи и прочая разметка сохраняются.
        return _Rendered(
            html=(
                f'<a href="{_escape_html(url, quote=True)}">'
                f"{strip_link_tags(label.html)}</a>"
            ),
            has_brick=has_brick,
            bricks_empty=label.bricks_empty,
            has_newline=label.has_newline,
        )
    if isinstance(node, FormatNode):
        inner = _render_children(node.children, values_by_id, definitions_by_id)
        tag = _FORMAT_TAGS.get(node.marker)
        html = f"<{tag}>{inner.html}</{tag}>" if tag else inner.html
        return _Rendered(
            html=html,
            has_brick=inner.has_brick,
            bricks_empty=inner.bricks_empty,
            has_newline=inner.has_newline,
        )
    if isinstance(node, str):
        return _Rendered(html=_escape_html(node), has_newline="\n" in node)
    return _Rendered()


def _render_children(
    children: Iterable[Any],
    values_by_id: Mapping[int, Any],
    definitions_by_id: Mapping[int, BrickDefinition],
) -> _Rendered:
    """Объединяет результаты дочерних узлов."""
    html: list[str] = []
    has_brick = False
    bricks_empty = True
    has_newline = False
    for child in children:
        rendered = _render_node(child, values_by_id, definitions_by_id)
        html.append(rendered.html)
        has_brick = has_brick or rendered.has_brick
        if rendered.has_brick:
            bricks_empty = bricks_empty and rendered.bricks_empty
        has_newline = has_newline or rendered.has_newline
    return _Rendered(
        html="".join(html),
        has_brick=has_brick,
        bricks_empty=bricks_empty,
        has_newline=has_newline,
    )


def _normalize_render_url(raw: str) -> str:
    """
    Нормализует литеральный URL гиперссылки перед созданием `<a href>`.

    Опасные схемы и произвольный текст отклоняются; корректные адреса
    приводятся к https://. Схема `tg://` разрешена наравне с http(s) —
    это внутренние ссылки Telegram, и парсер (`template_parser`) сохраняет
    их как есть.
    """
    value = str(raw).strip()
    lowered = value.lower()
    if lowered.startswith(("javascript:", "data:", "file:", "vbscript:")):
        raise TemplateCompileError(f"Схема ссылки запрещена: {value!r}.")
    if lowered.startswith(("http://", "https://", "tg://")):
        return value
    normalized = normalize_url(value)
    if normalized is None:
        raise TemplateCompileError(f"Некорректный адрес гиперссылки: {value!r}.")
    return normalized


@dataclass
class _Line:
    """Одна строка текста шаблона до решения о скрытии."""

    html: str = ""
    has_brick: bool = False
    bricks_empty: bool = True
    drops: bool = False


def _render_text_lines(
    tokens: Sequence[Any],
    values_by_id: Mapping[int, Any],
    definitions_by_id: Mapping[int, BrickDefinition],
) -> list[_Line]:
    """
    Рендерит текст построчно, сохраняя границы строк исходного шаблона.

    Строка, в которой есть токены кирпичиков, но все они пусты, помечается
    к скрытию: висячий литерал (`//`, `—`) и пустая разметка исчезают
    вместе с ней. Строки без токенов остаются буквальными.

    Форматирование, которое само содержит перевод строки, считается
    атомарным: такая строка не скрывается, чтобы не разорвать HTML-теги.
    """
    lines: list[_Line] = []
    current = _Line()

    def _flush() -> None:
        nonlocal current
        current.drops = current.has_brick and current.bricks_empty
        lines.append(current)
        current = _Line()

    for token in tokens:
        rendered = _render_node(token, values_by_id, definitions_by_id)
        if isinstance(token, TextNode):
            # Литеральный текст — единственный узел, который безопасно
            # делить на строки: вне разметки нет открытых тегов.
            chunks = rendered.html.split("\n")
            for index, chunk in enumerate(chunks):
                if index > 0:
                    _flush()
                current.html += chunk
        else:
            current.html += rendered.html
            if rendered.has_newline:
                # Перевод строки внутри форматирования/значения: скрывать
                # такую строку нельзя — это разорвало бы HTML-теги.
                current.bricks_empty = False
        if rendered.has_brick:
            current.has_brick = True
            current.bricks_empty = current.bricks_empty and rendered.bricks_empty
    _flush()
    return lines


def _assemble_text(lines: Sequence[_Line]) -> str:
    """
    Склеивает оставшиеся строки и убирает пустые края.

    Скрытая строка не оставляет «дыру»: вместе с ней поглощается одна
    соседняя пустая строка (предпочтительно следующая), чтобы блок не
    разъезжался на лишний интервал. Пустые строки внутри текста без
    скрытых соседей сохраняются буквально.
    """
    kept: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if line.drops:
            index += 1
            if index < total and not lines[index].drops and lines[index].html == "":
                index += 1
            elif kept and kept[-1] == "":
                kept.pop()
            continue
        kept.append(line.html)
        index += 1
    while kept and not kept[0]:
        kept.pop(0)
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept)


@dataclass(frozen=True)
class ConditionalRule:
    """
    Правило условной части, применяемое к ИСХОДНОМУ тексту (§41).

    `fragment` — точный фрагмент авторского текста, `occurrence` (1-based)
    различает одинаковые вхождения (§42-Е). `brick_id` — единственный
    управляющий кирпичик: фрагмент скрывается, только когда он пуст.
    """

    brick_id: int
    fragment: str
    occurrence: int = 1


def _rule_span(text: str, fragment: str, occurrence: int) -> tuple[int, int] | None:
    """Позиция `occurrence`-го вхождения; `None` — фрагмента нет."""
    start = -1
    for _ in range(max(1, occurrence)):
        start = text.find(fragment, start + 1)
        if start < 0:
            return None
    return start, start + len(fragment)


def _selected_rule_spans(
    text: str, rules: Iterable[ConditionalRule]
) -> list[tuple[int, int]]:
    """
    Отбирает непересекающиеся фрагменты в порядке позиции.

    Пересечения недопустимы: два правила не могут скрывать общий кусок
    текста. Более позднее правило игнорируется с предупреждением, а не
    расширяет/дробит первое. Пропавший фрагмент (текст изменился) тоже не
    применяется: правило не должно скрывать случайное похожее место §44.
    """
    spans: list[tuple[int, int]] = []
    for rule in rules:
        if not rule.fragment:
            logger.warning(
                "Условная часть кирпичика #%s пропущена: пустой фрагмент.",
                rule.brick_id,
            )
            continue
        span = _rule_span(text, rule.fragment, rule.occurrence)
        if span is None:
            logger.warning(
                "Условная часть кирпичика #%s пропущена: фрагмент не найден.",
                rule.brick_id,
            )
            continue
        if any(span[0] < end and start < span[1] for start, end in spans):
            logger.warning(
                "Условная часть кирпичика #%s пересекается с другой и "
                "проигнорирована.",
                rule.brick_id,
            )
            continue
        spans.append(span)
    spans.sort()
    return spans


def _whole_line_range(text: str, start: int, end: int) -> tuple[int, int] | None:
    """
    Диапазон удаления целой строки вместе с одним лишним переводом строки.

    Строка считается целой, если фрагмент занимает её целиком (допустимы
    только пробелы по краям). Вместе с ней поглощается одна соседняя
    пустая строка — иначе на месте абзаца остался бы лишний интервал.
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    if text[line_start:start].strip() or text[end:line_end].strip():
        return None
    deletion_start = line_start
    deletion_end = line_end
    if line_end < len(text):
        # Перевод строки самой строки входит в удаление.
        deletion_end += 1
        next_end = text.find("\n", deletion_end)
        if next_end < 0:
            next_end = len(text)
        if deletion_end == next_end:
            # Следующая строка пуста: она тоже лишняя (§41/§42-Д).
            deletion_end += 1 if next_end < len(text) else 0
    elif deletion_start > 0:
        # Последняя строка: забираем перевод строки у предыдущей границы.
        deletion_start -= 1
        previous_start = text.rfind("\n", 0, deletion_start) + 1
        if previous_start == deletion_start:
            deletion_start = previous_start
    return deletion_start, deletion_end


def apply_conditional_parts(
    text: str,
    rules: Iterable[ConditionalRule],
    *,
    is_empty: Callable[[int], bool],
) -> str:
    """
    Вырезает авторские фрагменты, чей управляющий кирпичик пуст (§38–45).

    Работает с ИСХОДНЫМ текстом до `parse_template`: фрагменты ищутся по
    логическому тексту, а не по экранированному HTML (§45). Заполненный
    кирпичик не меняет текст вообще — фрагмент остаётся и рендерится
    обычным путём. Пунктуация вне авторского фрагмента не чистится:
    эвристик нет (§38/§41).
    """
    active = [rule for rule in rules if is_empty(rule.brick_id)]
    if not active:
        return text
    deletions: list[tuple[int, int]] = []
    for start, end in _selected_rule_spans(text, active):
        whole_line = _whole_line_range(text, start, end)
        deletions.append(whole_line if whole_line is not None else (start, end))
    result = text
    for start, end in sorted(deletions, reverse=True):
        result = result[:start] + result[end:]
    return result


def render_template(
    tokens_by_text: Mapping[str, Sequence[Any]],
    values_by_id: Mapping[int, Any],
    definitions_by_id: Mapping[int, BrickDefinition],
    *,
    use_extended: bool = False,
) -> str:
    """
    Собирает итоговый HTML-анонс из уже связанных текстов шаблона.

    Не выполняет повторного разбора: привязки и структура уже сохранены.
    Если выбран расширенный текст (`use_extended=True`) и он есть в
    шаблоне, рендерится он; иначе — канонический `content`. Строки с
    токенами, отрендерившимися пустыми, скрываются целиком.
    """
    tokens = tokens_by_text.get(TEMPLATE_CONTENT)
    if use_extended:
        tokens = tokens_by_text.get(TEMPLATE_EXTENDED_CONTENT, tokens)
    if not tokens:
        return ""
    return _assemble_text(_render_text_lines(tokens, values_by_id, definitions_by_id))


# Техническая заглушка Telegram: пустое сообщение отправить нельзя.
PLACEHOLDER_TEXT = "."

# Лимиты Telegram на один анонс — по ВИДИМОМУ тексту после разбора
# разметки (S4 §5.1). Сырые HTML-теги символами сообщения не считаются.
ANNOUNCEMENT_TEXT_LIMIT = 4096
ANNOUNCEMENT_CAPTION_LIMIT = 1024


def visible_text_length(rendered_html: str) -> int:
    """
    Длина видимого текста в единицах Telegram (UTF-16 code units).

    Считается после разбора разметки: `visible_announcement_text` снимает
    теги и раскрывает HTML-сущности, поэтому `&amp;` — один символ, а не
    пять, и ограничение совпадает с фактическим сообщением.
    """
    return utf16_len(visible_announcement_text(rendered_html))


def announcement_text_limit(*, has_photos: bool) -> int:
    """Предел текста: без фото 4096, с фото (подпись) — 1024."""
    return ANNOUNCEMENT_CAPTION_LIMIT if has_photos else ANNOUNCEMENT_TEXT_LIMIT


@dataclass(frozen=True)
class AnnouncementLengthCheck:
    """
    Результат измерения готового текста (S4 §5.1).

    `length` — видимые символы после разбора entities; `limit` — предел
    Telegram для выбранного вида сообщения. Автосокращения нет: превышение
    показывается пользователю отдельным экраном, данные сохраняются.
    """

    length: int
    limit: int
    has_photos: bool

    @property
    def over(self) -> bool:
        return self.length > self.limit

    @property
    def excess(self) -> int:
        return max(0, self.length - self.limit)


def check_announcement_length(
    rendered_html: str, *, has_photos: bool
) -> AnnouncementLengthCheck:
    """Измеряет готовый анонс против лимита 4096/1024 без его изменения."""
    return AnnouncementLengthCheck(
        length=visible_text_length(rendered_html),
        limit=announcement_text_limit(has_photos=has_photos),
        has_photos=has_photos,
    )


def render_announcement(
    tokens_by_text: Mapping[str, Sequence[Any]],
    values_by_id: Mapping[int, Any],
    definitions_by_id: Mapping[int, BrickDefinition],
    *,
    has_photos: bool = False,
    use_extended: bool = False,
) -> str:
    """
    Единый рендер анонса для живого блока и итоговой отправки.

    Отличие от `render_template` — только техническая заглушка:

    - видимый текст непустой → рендер как есть (статика строк буквальна);
    - видимый текст пуст и фото нет → ровно ``.``;
    - видимый текст пуст, но есть фото → пустая строка: фото уходит без
      подписи, точка не нужна.

    Заглушка не является значением кирпичика: `values_by_id` не меняется,
    поэтому `.` не попадает в `announcement_values` и исчезает при правке.
    Анонс из одного литерального `//` в строке без токенов непустой и
    заглушку не получает.
    """
    html = render_template(
        tokens_by_text,
        values_by_id,
        definitions_by_id,
        use_extended=use_extended,
    )
    if visible_announcement_text(html):
        return html
    if has_photos:
        return ""
    return PLACEHOLDER_TEXT
