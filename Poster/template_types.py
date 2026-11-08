# template_types.py
"""
Чистые контракты шаблонов анонсов (Stage 23 → Poster 2.3 H2).

Здесь нет SQLAlchemy и Telegram: только закрытый каталог видов шаблонов,
каноническое содержимое шаблона (одна строка `content`), опциональное
расширенное содержимое, контролируемое встроенное содержимое Poster и
стабильные ID системных кирпичиков, на которые оно ссылается.

Границы:

- шаблон хранит авторский текст без разбора и нормализации;
- `[код]` остаётся частью текста до разбора компилятором (Stage 24);
- встроенный стандарт имеет собственную версию, чтобы будущий сброс не
  зависел от случайного изменения констант.

Poster 2.3 H2: три секции (`header`/`body`/`footer`) заменены одной строкой
`content`. Пустые строки между блоками теперь часть самого текста, поэтому
переносы сохраняются ровно так, как их видит автор. `extended_content` —
необязательный расширенный вариант того же текста: он рендерится, когда в
анонсе выбраны дополнительные кирпичики (H2), и отсутствует (`None`) у
шаблонов, которым расширение не нужно.
"""

from __future__ import annotations

from dataclasses import dataclass

from brick_types import SYSTEM_BRICKS_BY_CODE

TEMPLATE_KIND_STANDARD = "standard"
TEMPLATE_KIND_PERSONAL = "personal"
TEMPLATE_KINDS = (TEMPLATE_KIND_STANDARD, TEMPLATE_KIND_PERSONAL)

# Ключи текстов шаблона. Используются в компиляторе, снимках и представлении
# мастера вместо прежних имён секций.
TEMPLATE_CONTENT = "content"
TEMPLATE_EXTENDED_CONTENT = "extended_content"
TEMPLATE_TEXTS = (TEMPLATE_CONTENT, TEMPLATE_EXTENDED_CONTENT)

STANDARD_TEMPLATE_DISPLAY_NAME = "Стандартный шаблон места"

# Имя первого личного шаблона пользователя (Poster 2.0). Отдельная константа,
# чтобы UI и сервис не расходились в тексте. Poster 2.1 R2: стартовая копия
# называется «Дефолтный шаблон»; прежнее имя «Мой шаблон» осталось только у
# уже выданных копий до миграции 18.
INITIAL_PERSONAL_TEMPLATE_NAME = "Дефолтный шаблон"

# Контролируемая версия встроенного стандарта. Меняется только вместе с
# осознанным изменением содержимого и документацией; существующие шаблоны
# при обновлении приложения автоматически не перезаписываются — их обновляет
# только явная миграция нетронутых копий (миграции 18 и 22) или сброс из
# карточки.
#
# Версия 3 (Poster 2.1 R2) — эталон владельца (Часть III): жирный заголовок,
# пустая строка, жирное `дата // время`, пустая строка, жирная кликабельная
# площадка, нежирная строка `[адрес]`, пустая строка, `[описание]`, пустая
# строка, `[контакт]`.
#
# Версия 4 (Poster 2.0 U1a) — уточнение владельца по живому анонсу:
# `[дата] // [время_начала]` идут отдельной жирной строкой прямо под
# заголовком (без пустой строки между ними), площадка — отдельным блоком
# через пустую строку, тело содержит ТОЛЬКО описание, подвал — ТОЛЬКО контакт.
#
# Poster 2.3 H2: версия остаётся 4, но текст хранится одной строкой. Значение
# `BUILTIN_TEMPLATE_CONTENT` побайтово совпадает с прежней склейкой секций
# `header + "\n" + body + "\n" + footer`, поэтому миграция 24 и рендер
# заполненного анонса не меняют ни одного символа эталона.
#
# Версия 5 (этап A1, финальное задание владельца §1–2): у стандартного
# шаблона площадки теперь ДВА рабочих варианта одного текста.
#
# Простой вариант (§1.1): дата и время начала разделены запятой и пробелом
# (без `//`), контакт — строкой с подписью «Мероприятие ведёт:».
#
# Расширенный вариант (§1.2): условие входа отдельной строкой под заголовком,
# время окончания через символ `–` (U+2013) без пробелов, `[город], [адрес]`,
# куратор с подписью и блок `[подвал]`.
#
# Оба варианта создаются сразу с автоматическими условными частями
# (`BUILTIN_TEMPLATE_CONDITIONAL_RULES`): это не настройка администратора,
# а часть стандарта (§2). Флаг активности расширенного варианта живёт в
# строке шаблона (`templates.extended_active`, миграция 30) и по умолчанию
# выключен.
BUILTIN_TEMPLATE_VERSION = 5

BUILTIN_TEMPLATE_CONTENT = (
    "**[название]**\n"
    "\n"
    "**[дата], [время_начала]**\n"
    "**[место]([ссылка_места])**\n"
    "[адрес]\n"
    "\n"
    "[описание]\n"
    "\n"
    "Мероприятие ведёт: [контакт]"
)

BUILTIN_TEMPLATE_EXTENDED_CONTENT: str | None = (
    "**[название]**\n"
    "[условие_входа]\n"
    "\n"
    "**[дата], [время_начала]–[время_окончания]**\n"
    "**[место]([ссылка_места])**\n"
    "[город], [адрес]\n"
    "\n"
    "[описание]\n"
    "\n"
    "Мероприятие ведёт: [контакт]\n"
    "Куратор от площадки: [куратор]\n"
    "\n"
    "[подвал]"
)


@dataclass(frozen=True)
class BuiltinBrickBinding:
    """Постоянная связь встроенного текста с системным brick_id."""

    brick_id: int
    code: str


# Явные пары «ID → код», а не результат поиска по имени во время выполнения.
# Это те же стабильные определения, которые вставлены миграциями 6, 15, 21
# и 30. Порядок повторяет первое появление кода в текстах стандарта.
BUILTIN_TEMPLATE_BINDINGS: tuple[BuiltinBrickBinding, ...] = (
    BuiltinBrickBinding(1, "название"),
    BuiltinBrickBinding(12, "условие_входа"),
    BuiltinBrickBinding(2, "дата"),
    BuiltinBrickBinding(3, "время_начала"),
    BuiltinBrickBinding(4, "время_окончания"),
    BuiltinBrickBinding(5, "место"),
    BuiltinBrickBinding(7, "ссылка_места"),
    BuiltinBrickBinding(11, "город"),
    BuiltinBrickBinding(10, "адрес"),
    BuiltinBrickBinding(8, "описание"),
    BuiltinBrickBinding(9, "контакт"),
    BuiltinBrickBinding(13, "куратор"),
    BuiltinBrickBinding(14, "подвал"),
)
BUILTIN_TEMPLATE_BRICK_IDS: tuple[int, ...] = tuple(
    binding.brick_id for binding in BUILTIN_TEMPLATE_BINDINGS
)


@dataclass(frozen=True)
class BuiltinTemplateDefinition:
    """Неизменяемый встроенный стандарт и его контролируемая версия."""

    version: int
    content: str
    extended_content: str | None = None
    bindings: tuple[BuiltinBrickBinding, ...] = BUILTIN_TEMPLATE_BINDINGS

    @property
    def brick_ids(self) -> tuple[int, ...]:
        return tuple(binding.brick_id for binding in self.bindings)

    @property
    def texts(self) -> dict[str, str]:
        """Тексты стандарта в виде, который принимает компилятор."""
        return {
            TEMPLATE_CONTENT: self.content,
            **(
                {TEMPLATE_EXTENDED_CONTENT: self.extended_content}
                if self.extended_content is not None
                else {}
            ),
        }


BUILTIN_TEMPLATE = BuiltinTemplateDefinition(
    version=BUILTIN_TEMPLATE_VERSION,
    content=BUILTIN_TEMPLATE_CONTENT,
    extended_content=BUILTIN_TEMPLATE_EXTENDED_CONTENT,
)


# --- Автоматические условные части стандарта (этап A1, §2) ------------------
#
# Правила заданы ДАННЫМИ, а не отдельным рендерером: та же таблица
# `template_conditional_parts`, тот же движок `apply_conditional_parts`.
# Поэтому они переживают перезапуск, правку текста и переключение флага,
# как и любые авторские правила.
#
# Фрагменты выбраны так, чтобы скрытие не оставляло висящей пунктуации:
#
# - простой текст: строка контакта и строка адреса скрываются целиком
#   (у адреса — вместе с переводом строки перед ним, чтобы абзац описания
#   остался отдельным); абзац описания исчезает без пустой строки;
# - расширенный текст: условие входа скрывается с переводом строки перед
#   ним (заголовок и дата остаются разделены пустой строкой), тире уходит
#   вместе с временем окончания, город и адрес — каждый со своей запятой.
#   Правила города и адреса пересекаются только разделителем `, `: когда
#   пусты оба, движок объединяет их и скрывает строку целиком.
#
# `[ссылка_места]` правила не требует: пустая ссылка — это просто жирная
# подпись без `<a>` (поведение компилятора Stage 24).
@dataclass(frozen=True)
class BuiltinConditionalRule:
    """Одно правило автоматической условной части стандартного шаблона."""

    section: str
    code: str
    fragment: str
    occurrence: int = 1

    @property
    def key(self) -> tuple[str, str]:
        return self.section, self.code


BUILTIN_TEMPLATE_CONDITIONAL_RULES: tuple[BuiltinConditionalRule, ...] = (
    BuiltinConditionalRule(TEMPLATE_CONTENT, "контакт", "Мероприятие ведёт: [контакт]"),
    BuiltinConditionalRule(TEMPLATE_CONTENT, "адрес", "\n[адрес]"),
    BuiltinConditionalRule(TEMPLATE_CONTENT, "описание", "[описание]"),
    BuiltinConditionalRule(
        TEMPLATE_EXTENDED_CONTENT, "условие_входа", "\n[условие_входа]"
    ),
    BuiltinConditionalRule(
        TEMPLATE_EXTENDED_CONTENT, "время_окончания", "–[время_окончания]"
    ),
    BuiltinConditionalRule(TEMPLATE_EXTENDED_CONTENT, "город", "[город], "),
    BuiltinConditionalRule(TEMPLATE_EXTENDED_CONTENT, "адрес", ", [адрес]"),
    BuiltinConditionalRule(TEMPLATE_EXTENDED_CONTENT, "описание", "[описание]"),
    BuiltinConditionalRule(
        TEMPLATE_EXTENDED_CONTENT, "контакт", "Мероприятие ведёт: [контакт]"
    ),
    BuiltinConditionalRule(
        TEMPLATE_EXTENDED_CONTENT, "куратор", "Куратор от площадки: [куратор]"
    ),
    BuiltinConditionalRule(TEMPLATE_EXTENDED_CONTENT, "подвал", "[подвал]"),
)


class TemplateValidationError(ValueError):
    """Некорректное название или текст шаблона."""


def _validate_catalog() -> None:
    """
    Проверяет, что пары встроенного стандарта совпадают с каталогом Stage 22A.

    Это не разбор пользовательского текста: только защита от случайного
    рассинхронизирования стабильных ID и кодов при правке кода.
    """
    for binding in BUILTIN_TEMPLATE_BINDINGS:
        definition = SYSTEM_BRICKS_BY_CODE.get(binding.code)
        if definition is None or definition.id != binding.brick_id:
            raise RuntimeError(
                "Встроенный шаблон ссылается на неизвестный системный "
                f"кирпичик: {binding.brick_id}:{binding.code!r}."
            )


_validate_catalog()


def validate_template_name(name: object) -> str:
    """
    Название личного шаблона.

    Значимые символы не переписываются и не обрезаются; проверяется только
    тип и наличие непустого человекочитаемого содержимого. Уникальность
    названий не требуется: разные пользователи и один владелец могут иметь
    несколько шаблонов с одинаковым названием.
    """
    if not isinstance(name, str):
        raise TemplateValidationError("Название шаблона должно быть строкой.")
    if not name.strip():
        raise TemplateValidationError("Название шаблона не должно быть пустым.")
    return name


def validate_template_content(
    content: object,
    extended_content: object = None,
) -> tuple[str, str | None]:
    """
    Проверяет каноническое и (необязательное) расширенное содержимое.

    Символы сохраняются один в один: `.strip()` используется ТОЛЬКО для
    проверки и никогда не меняет сохраняемое значение. Пустое каноническое
    содержимое недопустимо; расширенное — либо `None`, либо непустое.
    """
    if not isinstance(content, str):
        raise TemplateValidationError("Содержимое шаблона должно быть строкой.")
    if not content.strip():
        raise TemplateValidationError(
            "Шаблон не должен состоять только из пустых строк."
        )
    if extended_content is None:
        return content, None
    if not isinstance(extended_content, str):
        raise TemplateValidationError(
            "Расширенное содержимое шаблона должно быть строкой."
        )
    if not extended_content.strip():
        raise TemplateValidationError(
            "Расширенное содержимое шаблона не должно быть пустым; "
            "отсутствие расширения — это None."
        )
    return content, extended_content
