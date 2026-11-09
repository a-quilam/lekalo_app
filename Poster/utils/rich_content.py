# utils/rich_content.py
"""
Загрузка и строгая проверка Rich-каталога `config/rich_content.yaml`.

Финальное задание 2026-11-08 §15.1: содержимое, заголовки и порядок разделов
«Помощи» и «О проекте» живут в ОДНОМ файле, а Python только загружает,
проверяет и отображает его. Вторая копия утверждённых текстов в Python или в
`ui_texts.yaml` запрещена.

Почему fail-fast, а не откат на старый текст: согласованная справка не
должна молча подменяться прежней редакцией (§15.1). Любая ошибка каталога —
`RichContentError`: вызывающий слой показывает короткую плашку сбоя и
пишет причину в лог, но не отправляет пользователю устаревший текст.

Белый список блоков (других ключей быть не может):

- `paragraph` — строка или список частей: строка, `{link: {text, url}}`,
  `{bold: "…"}`;
- `list` — маркированный список (пункт — строка или части абзаца);
- `table` — компактная Rich-таблица: `header`, `rows`, `compact`;
- `quote` — Rich-цитата: строка или `{text, credit}`;
- `details` — вложенный раскрываемый блок `{summary, blocks}`;
- `slideshow` — настоящее слайд-шоу: список `{id, file}`.

Проверки структурные и без поблажек: ровно `help` и `about`; уникальные
`id` разделов; непустые заголовки, разделы и блоки; одинаковое число колонок
таблицы; `id` медиа по шаблону `^[A-Za-z0-9_-]{1,64}$`; путь медиа
относительный, без `..`, внутри проекта; после всех подстановок в текстах не
остаётся `{…}`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

# Корень проекта (каталог Poster) — граница для путей медиа.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PATH = PROJECT_ROOT / "config" / "rich_content.yaml"

# Переменная окружения для тестов и нестандартных развёртываний — тот же
# приём, что `UI_TEXTS_PATH` в каталоге текстов интерфейса.
PATH_ENV_VARIABLE = "RICH_CONTENT_PATH"

PAGE_KEYS = frozenset({"help", "about"})
SECTION_KEYS = frozenset({"id", "title", "blocks"})
PAGE_FIELDS = frozenset({"title", "intro", "hint", "sections"})
BLOCK_KEYS = frozenset({"paragraph", "list", "table", "quote", "details", "slideshow"})
TABLE_KEYS = frozenset({"header", "rows", "compact"})
QUOTE_KEYS = frozenset({"text", "credit"})
DETAILS_KEYS = frozenset({"summary", "blocks"})
SLIDE_KEYS = frozenset({"id", "file"})
PART_KEYS = frozenset({"link", "bold"})
LINK_KEYS = frozenset({"text", "url"})

MEDIA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Остаток подстановки после подстановок/экранирования: в готовом каталоге
# `{…}` быть не должно (иначе пользователь увидит технический маркер).
LEFTOVER_MARKER_RE = re.compile(r"\{[^{}]*\}")


class RichContentError(ValueError):
    """Каталог Rich-контента не соответствует схеме (ошибка до отправки)."""


# —— модель каталога ——————————————————————————————————————————————————
#
# Неизменяемые dataclass'ы: готовый каталог не должен меняться задним числом
# между сборкой сообщения и отправкой.


@dataclass(frozen=True)
class LinkPart:
    """Часть абзаца — кликабельная ссылка с подписью (`RichTextUrl`)."""

    text: str
    url: str


@dataclass(frozen=True)
class BoldPart:
    """Часть абзаца — жирная фраза (`RichTextBold`)."""

    text: str


TextPart = str | LinkPart | BoldPart


@dataclass(frozen=True)
class ParagraphBlock:
    parts: tuple[TextPart, ...]


@dataclass(frozen=True)
class ListBlock:
    items: tuple[tuple[TextPart, ...], ...]


@dataclass(frozen=True)
class TableBlock:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    compact: bool = True


@dataclass(frozen=True)
class QuoteBlock:
    text: str
    credit: str | None = None


@dataclass(frozen=True)
class DetailsBlock:
    summary: str
    blocks: tuple["Block", ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Slide:
    id: str
    # Путь относительно корня проекта: `assets/rich/help_forward.png`.
    file: str


@dataclass(frozen=True)
class SlideshowBlock:
    slides: tuple[Slide, ...]


Block = (
    ParagraphBlock | ListBlock | TableBlock | QuoteBlock | DetailsBlock | SlideshowBlock
)


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class RichPage:
    title: str
    intro: str
    hint: str
    sections: tuple[Section, ...]


@dataclass(frozen=True)
class RichCatalog:
    help: RichPage
    about: RichPage


# —— загрузка ————————————————————————————————————————————————————————


def catalog_path(path: Path | str | None = None) -> Path:
    """Путь к каталогу: аргумент, `RICH_CONTENT_PATH` или файл проекта."""
    if path is not None:
        return Path(path)
    raw = os.getenv(PATH_ENV_VARIABLE)
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return DEFAULT_PATH


def load_catalog(path: Path | str | None = None) -> RichCatalog:
    """
    Читает и проверяет каталог. Любая ошибка — `RichContentError`.

    Кэша нет намеренно: файл небольшой, а правка YAML должна применяться
    сразу после перезапуска (и в тестах — без перезагрузки модуля).
    """
    target = catalog_path(path)
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RichContentError(f"Rich-каталог недоступен: {target}: {exc}") from exc
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise RichContentError(f"Rich-каталог: синтаксис YAML: {exc}") from exc
    return load_from_mapping(data, path=target)


def load_from_mapping(data: Any, *, path: Path | None = None) -> RichCatalog:
    """Проверяет уже разобранный YAML (используется тестами)."""
    where = str(path) if path is not None else "rich_content"
    if not isinstance(data, Mapping):
        raise RichContentError(f"{where}: корень файла должен быть словарём.")
    keys = set(data)
    missing = PAGE_KEYS - keys
    if missing:
        raise RichContentError(f"{where}: нет обязательных страниц {sorted(missing)}.")
    extra = keys - PAGE_KEYS
    if extra:
        raise RichContentError(f"{where}: неизвестные страницы {sorted(extra)}.")
    return RichCatalog(
        help=_parse_page(data["help"], where=f"{where}.help"),
        about=_parse_page(data["about"], where=f"{where}.about"),
    )


def _parse_page(value: Any, *, where: str) -> RichPage:
    if not isinstance(value, Mapping):
        raise RichContentError(f"{where}: страница должна быть словарём.")
    keys = set(value)
    extra = keys - PAGE_FIELDS
    if extra:
        raise RichContentError(f"{where}: неизвестные поля {sorted(extra)}.")
    title = _require_text(value.get("title"), f"{where}.title")
    intro = _optional_text(value.get("intro"), f"{where}.intro")
    hint = _optional_text(value.get("hint"), f"{where}.hint")
    sections_raw = value.get("sections")
    if not isinstance(sections_raw, Sequence) or isinstance(sections_raw, (str, bytes)):
        raise RichContentError(f"{where}.sections: ожидается список разделов.")
    if not sections_raw:
        raise RichContentError(f"{where}.sections: список разделов пуст.")
    sections: list[Section] = []
    seen: set[str] = set()
    for index, item in enumerate(sections_raw):
        section = _parse_section(item, where=f"{where}.sections[{index}]")
        if section.id in seen:
            raise RichContentError(
                f"{where}.sections[{index}].id: повторяется ({section.id})."
            )
        seen.add(section.id)
        sections.append(section)
    return RichPage(title=title, intro=intro, hint=hint, sections=tuple(sections))


def _parse_section(value: Any, *, where: str) -> Section:
    if not isinstance(value, Mapping):
        raise RichContentError(f"{where}: раздел должен быть словарём.")
    keys = set(value)
    extra = keys - SECTION_KEYS
    if extra:
        raise RichContentError(f"{where}: неизвестные поля {sorted(extra)}.")
    section_id = _require_text(value.get("id"), f"{where}.id")
    if not MEDIA_ID_RE.fullmatch(section_id):
        raise RichContentError(
            f"{where}.id: допустимы буквы, цифры, «_» и «-» (1–64 символа)."
        )
    title = _require_text(value.get("title"), f"{where}.title")
    blocks = _parse_blocks(value.get("blocks"), where=f"{where}.blocks")
    return Section(id=section_id, title=title, blocks=blocks)


def _parse_blocks(value: Any, *, where: str) -> tuple[Block, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RichContentError(f"{where}: ожидается список блоков.")
    if not value:
        raise RichContentError(f"{where}: список блоков пуст.")
    return tuple(
        _parse_block(item, where=f"{where}[{index}]")
        for index, item in enumerate(value)
    )


def _parse_block(value: Any, *, where: str) -> Block:
    if not isinstance(value, Mapping):
        raise RichContentError(f"{where}: блок должен быть словарём.")
    if len(value) != 1:
        raise RichContentError(
            f"{where}: у блока ровно один ключ из {sorted(BLOCK_KEYS)}."
        )
    kind = next(iter(value))
    if kind not in BLOCK_KEYS:
        raise RichContentError(
            f"{where}: неизвестный блок {kind!r} (допустимы {sorted(BLOCK_KEYS)})."
        )
    payload = value[kind]
    if kind == "paragraph":
        return ParagraphBlock(parts=_parse_parts(payload, where=f"{where}.paragraph"))
    if kind == "list":
        return ListBlock(items=_parse_list(payload, where=f"{where}.list"))
    if kind == "table":
        return _parse_table(payload, where=f"{where}.table")
    if kind == "quote":
        return _parse_quote(payload, where=f"{where}.quote")
    if kind == "details":
        return _parse_details(payload, where=f"{where}.details")
    return _parse_slideshow(payload, where=f"{where}.slideshow")


def _parse_parts(value: Any, *, where: str) -> tuple[TextPart, ...]:
    items = (
        value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes))
        else [value]
    )
    if not items:
        raise RichContentError(f"{where}: пустой абзац.")
    return tuple(
        _parse_part(item, where=f"{where}[{index}]") for index, item in enumerate(items)
    )


def _parse_part(value: Any, *, where: str) -> TextPart:
    if isinstance(value, str):
        return _require_text(value, where)
    if not isinstance(value, Mapping):
        raise RichContentError(f"{where}: ожидается строка или часть абзаца.")
    if len(value) != 1:
        raise RichContentError(
            f"{where}: у части ровно один ключ из {sorted(PART_KEYS)}."
        )
    kind = next(iter(value))
    if kind not in PART_KEYS:
        raise RichContentError(f"{where}: неизвестная часть {kind!r}.")
    if kind == "bold":
        return BoldPart(text=_require_text(value[kind], f"{where}.bold"))
    link = value[kind]
    if not isinstance(link, Mapping):
        raise RichContentError(f"{where}.link: ожидается словарь.")
    extra = set(link) - LINK_KEYS
    if extra:
        raise RichContentError(f"{where}.link: неизвестные поля {sorted(extra)}.")
    text = _require_text(link.get("text"), f"{where}.link.text")
    url = _require_text(link.get("url"), f"{where}.link.url")
    if not url.startswith(("http://", "https://")):
        raise RichContentError(f"{where}.link.url: ожидается ссылка http(s)://…")
    return LinkPart(text=text, url=url)


def _parse_list(value: Any, *, where: str) -> tuple[tuple[TextPart, ...], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RichContentError(f"{where}: ожидается список пунктов.")
    if not value:
        raise RichContentError(f"{where}: список пуст.")
    return tuple(
        _parse_parts(item, where=f"{where}[{index}]")
        for index, item in enumerate(value)
    )


def _parse_table(value: Any, *, where: str) -> TableBlock:
    if not isinstance(value, Mapping):
        raise RichContentError(f"{where}: ожидается словарь таблицы.")
    extra = set(value) - TABLE_KEYS
    if extra:
        raise RichContentError(f"{where}: неизвестные поля {sorted(extra)}.")
    header = _parse_row(value.get("header"), where=f"{where}.header")
    rows_raw = value.get("rows")
    if isinstance(rows_raw, (str, bytes)) or not isinstance(rows_raw, Sequence):
        raise RichContentError(f"{where}.rows: ожидается список строк.")
    if not rows_raw:
        raise RichContentError(f"{where}.rows: таблица без строк.")
    rows: list[tuple[str, ...]] = []
    for index, row in enumerate(rows_raw):
        cells = _parse_row(row, where=f"{where}.rows[{index}]")
        if len(cells) != len(header):
            raise RichContentError(
                f"{where}.rows[{index}]: {len(cells)} колонок вместо {len(header)}."
            )
        rows.append(cells)
    compact = value.get("compact", True)
    if not isinstance(compact, bool):
        raise RichContentError(f"{where}.compact: ожидается true/false.")
    return TableBlock(header=header, rows=tuple(rows), compact=compact)


def _parse_row(value: Any, *, where: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RichContentError(f"{where}: ожидается список ячеек.")
    if not value:
        raise RichContentError(f"{where}: строка таблицы пуста.")
    return tuple(
        _require_text(cell, f"{where}[{index}]") for index, cell in enumerate(value)
    )


def _parse_quote(value: Any, *, where: str) -> QuoteBlock:
    if isinstance(value, str):
        return QuoteBlock(text=_require_text(value, where))
    if not isinstance(value, Mapping):
        raise RichContentError(f"{where}: ожидается строка или словарь цитаты.")
    extra = set(value) - QUOTE_KEYS
    if extra:
        raise RichContentError(f"{where}: неизвестные поля {sorted(extra)}.")
    text = _require_text(value.get("text"), f"{where}.text")
    credit = _optional_text(value.get("credit"), f"{where}.credit") or None
    return QuoteBlock(text=text, credit=credit)


def _parse_details(value: Any, *, where: str) -> DetailsBlock:
    if not isinstance(value, Mapping):
        raise RichContentError(f"{where}: ожидается словарь раскрываемого блока.")
    extra = set(value) - DETAILS_KEYS
    if extra:
        raise RichContentError(f"{where}: неизвестные поля {sorted(extra)}.")
    summary = _require_text(value.get("summary"), f"{where}.summary")
    blocks = _parse_blocks(value.get("blocks"), where=f"{where}.blocks")
    return DetailsBlock(summary=summary, blocks=blocks)


def _parse_slideshow(value: Any, *, where: str) -> SlideshowBlock:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RichContentError(f"{where}: ожидается список слайдов.")
    if not value:
        raise RichContentError(f"{where}: слайд-шоу без слайдов.")
    slides: list[Slide] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_where = f"{where}[{index}]"
        if not isinstance(item, Mapping):
            raise RichContentError(f"{item_where}: ожидается словарь слайда.")
        extra = set(item) - SLIDE_KEYS
        if extra:
            raise RichContentError(f"{item_where}: неизвестные поля {sorted(extra)}.")
        slide_id = _require_text(item.get("id"), f"{item_where}.id")
        if not MEDIA_ID_RE.fullmatch(slide_id):
            raise RichContentError(
                f"{item_where}.id: допустимы буквы, цифры, «_» и «-» (1–64)."
            )
        if slide_id in seen:
            raise RichContentError(f"{item_where}.id: повторяется ({slide_id}).")
        seen.add(slide_id)
        file_name = _require_text(item.get("file"), f"{item_where}.file")
        _require_project_path(file_name, f"{item_where}.file")
        slides.append(Slide(id=slide_id, file=file_name))
    return SlideshowBlock(slides=tuple(slides))


def _require_project_path(relative: str, where: str) -> None:
    """
    Путь медиа: относительно корня проекта, без `..` и выхода наружу.

    Абсолютный путь или `..` позволили бы отправить произвольный файл
    сервера — этого каталог не разрешает.
    """
    candidate = Path(relative)
    if candidate.is_absolute():
        raise RichContentError(f"{where}: путь должен быть относительным.")
    if any(part == ".." for part in candidate.parts):
        raise RichContentError(f"{where}: путь не должен содержать «..».")
    resolved = (PROJECT_ROOT / candidate).resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise RichContentError(f"{where}: путь вне проекта.") from exc


def _require_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RichContentError(f"{where}: ожидается непустая строка.")
    _reject_markers(value, where)
    return value


def _optional_text(value: Any, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RichContentError(f"{where}: ожидается строка.")
    _reject_markers(value, where)
    return value


def _reject_markers(value: str, where: str) -> None:
    """После подстановок в готовом тексте не должно остаться `{…}`."""
    marker = LEFTOVER_MARKER_RE.search(value)
    if marker is not None:
        raise RichContentError(
            f"{where}: остался маркер подстановки {marker.group(0)!r}."
        )


def media_path(slide: Slide) -> Path:
    """Абсолютный путь файла слайда (существование проверяет отправитель)."""
    return PROJECT_ROOT / slide.file
