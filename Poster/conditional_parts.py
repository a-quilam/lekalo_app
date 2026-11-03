# conditional_parts.py
"""
Условные части шаблонов анонсов (Poster 2.5, spec §38–45, миграция 27).

Автор сам определяет, какую часть текста скрывать, если конкретный
управляющий кирпичик пуст. Синтаксиса условий в тексте нет: правило —
отдельная строка БД, привязанная к конкретной НЕИЗМЕНЯЕМОЙ версии шаблона
и ровно к одной секции (`content` или `extended_content`).

Контракт, который держит этот модуль:

- правило указывает на ОДИН кирпичик (`brick_id`), привязанный к той же
  версии; AND/OR и вложенных условий нет (§41);
- `fragment` — точный фрагмент ИСХОДНОГО авторского текста (не HTML);
  `occurrence` (1-based) различает одинаковые вхождения (§42-Е);
- фрагмент обязан включать токен управляющего кирпичика и не пересекаться
  с активными правилами той же секции;
- версия шаблона и `revision` от правки правил НЕ меняются: правила живут
  рядом с версией, а не внутри её текста;
- при изменении текста шаблона правило переносится в новую версию только
  при ОДНОЗНАЧНОМ совпадении фрагмента (`refresh_conditional_parts`),
  иначе помечается `stale` и не удаляется молча (§44);
- копии шаблонов и снимки правила не переносят (§44): у снимка нет своей
  таблицы правил, поэтому рендер снимка идёт без них.

Таблица создаётся миграцией 27 и намеренно не добавлена в `models.py`
(как `announcement_instances`): `create_all` не должен создавать её в
обход журнала миграций. Здесь описана только Core-таблица для запросов.

Модуль не зависит от Telegram. Права — те же, что у
`template_store.update_template_content`: стандарт меняет администратор
места/суперадмин, личный шаблон — только его владелец.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    select,
)
from sqlalchemy import (
    delete as sqlalchemy_delete,
)
from sqlalchemy import (
    update as sqlalchemy_update,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from models import Brick, TemplateVersion, TemplateVersionBrick, utcnow
from template_compiler import ConditionalRule
from template_store import (
    TemplateActorError,
    TemplateError,
    TemplateNotFoundError,
    _can_edit_template,
    _readable_template_row,
    _require_actor_user,
    _valid_positive_id,
    _version_row,
)
from template_types import TEMPLATE_CONTENT, TEMPLATE_EXTENDED_CONTENT

STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
CONDITIONAL_STATUSES = (STATUS_ACTIVE, STATUS_STALE)

SECTION_CONTENT = TEMPLATE_CONTENT
SECTION_EXTENDED_CONTENT = TEMPLATE_EXTENDED_CONTENT
CONDITIONAL_SECTIONS = (SECTION_CONTENT, SECTION_EXTENDED_CONTENT)

# Таблица уже существует (миграция 27). Metadata локальная: к Base она не
# подключена, `create_all` её не создаёт.
_METADATA = MetaData()

template_conditional_parts = Table(
    "template_conditional_parts",
    _METADATA,
    Column("id", Integer, primary_key=True),
    Column(
        "template_id",
        Integer,
        ForeignKey("templates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "version_id",
        Integer,
        ForeignKey("template_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("section", Text, nullable=False),
    Column(
        "brick_id",
        Integer,
        ForeignKey("bricks.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("fragment", Text, nullable=False),
    Column("occurrence", Integer, nullable=False, default=1, server_default="1"),
    Column(
        "status", Text, nullable=False, default=STATUS_ACTIVE, server_default="'active'"
    ),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)


@dataclass(frozen=True)
class ConditionalPartDetails:
    """Одно правило условной части для редактора и рендера."""

    id: int
    template_id: int
    version_id: int
    version: int
    section: str
    brick_id: int
    code: str
    fragment: str
    occurrence: int
    status: str
    created_at: datetime
    updated_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def is_stale(self) -> bool:
        return self.status == STATUS_STALE


@dataclass(frozen=True)
class ConditionalPartMutationResult:
    """Результат правки правила; `template`-состояние здесь не меняется."""

    status: str
    part: ConditionalPartDetails | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"created", "updated", "deleted", "unchanged"}


@dataclass(frozen=True)
class ConditionalRefreshResult:
    """Итог переноса правил в актуальную версию (§44)."""

    status: str
    parts: tuple[ConditionalPartDetails, ...] = ()
    transferred: int = 0
    stale: int = 0
    message: str = ""


def _validate_section(section: object) -> str:
    if section not in CONDITIONAL_SECTIONS:
        raise TemplateError(
            "Секция условной части должна быть 'content' или " "'extended_content'."
        )
    return str(section)


def _validate_occurrence(occurrence: object) -> int:
    if isinstance(occurrence, bool) or not isinstance(occurrence, int):
        raise TemplateError("occurrence должен быть положительным целым числом.")
    if occurrence < 1:
        raise TemplateError("occurrence должен быть положительным целым числом.")
    return occurrence


def _section_text(version_row: TemplateVersion, section: str) -> str:
    raw = (
        version_row.content
        if section == SECTION_CONTENT
        else version_row.extended_content
    )
    if raw is None or not raw.strip():
        raise TemplateError(
            "В этой версии шаблона нет такого текста: условную часть "
            "не к чему привязать."
        )
    return raw


def _binding_code(session: Session, version_id: int, brick_id: int) -> str | None:
    """Код кирпичика в КОНКРЕТНОЙ версии (а не текущий код `Brick`)."""
    return session.scalar(
        select(TemplateVersionBrick.code).where(
            TemplateVersionBrick.template_version_id == version_id,
            TemplateVersionBrick.brick_id == brick_id,
        )
    )


def _fragment_span(text: str, fragment: str, occurrence: int) -> tuple[int, int] | None:
    """Позиция `occurrence`-го (1-based) вхождения фрагмента или `None`."""
    start = -1
    for _ in range(occurrence):
        start = text.find(fragment, start + 1)
        if start < 0:
            return None
    return start, start + len(fragment)


def _validate_fragment(
    *,
    text: str,
    fragment: object,
    occurrence: int,
    code: str,
    other_spans: Sequence[tuple[int, int]] = (),
) -> str:
    """
    Проверяет авторский фрагмент до сохранения правила.

    Фрагмент непустой, включает `[код]` управляющего кирпичика, встречается
    в тексте ровно `occurrence` раз (то есть такое вхождение существует) и
    не пересекается с уже принятыми активными правилами. Смещения и
    индексы в БД не сохраняются: только точный текст и номер вхождения.
    """
    if not isinstance(fragment, str):
        raise TemplateError("Фрагмент условной части должен быть строкой.")
    if not fragment.strip():
        raise TemplateError("Фрагмент условной части не должен быть пустым.")
    token = f"[{code}]"
    if token not in fragment:
        raise TemplateError(f"Фрагмент должен включать управляющий кирпичик {token}.")
    span = _fragment_span(text, fragment, occurrence)
    if span is None:
        raise TemplateError(
            f"Фрагмент не найден в тексте шаблона (вхождение {occurrence})."
        )
    for other_start, other_end in other_spans:
        if span[0] < other_end and other_start < span[1]:
            raise TemplateError(
                "Фрагмент пересекается с другим правилом этой секции. "
                "Выберите другой фрагмент."
            )
    return fragment


def _active_spans(
    session: Session,
    *,
    version_row: TemplateVersion,
    section: str,
    exclude_id: int | None = None,
) -> list[tuple[int, int]]:
    """Позиции активных правил секции; для проверки пересечения."""
    text = _section_text(version_row, section)
    rows = session.execute(
        select(
            template_conditional_parts.c.id,
            template_conditional_parts.c.fragment,
            template_conditional_parts.c.occurrence,
        ).where(
            template_conditional_parts.c.version_id == version_row.id,
            template_conditional_parts.c.section == section,
            template_conditional_parts.c.status == STATUS_ACTIVE,
        )
    ).all()
    spans: list[tuple[int, int]] = []
    for row in rows:
        if exclude_id is not None and int(row.id) == exclude_id:
            continue
        span = _fragment_span(text, str(row.fragment), int(row.occurrence))
        if span is not None:
            spans.append(span)
    return spans


def _view(session: Session, row: object) -> ConditionalPartDetails:
    version_number = session.scalar(
        select(TemplateVersion.version).where(
            TemplateVersion.id == int(getattr(row, "version_id"))
        )
    )
    code = session.scalar(
        select(Brick.code).where(Brick.id == int(getattr(row, "brick_id")))
    )
    return ConditionalPartDetails(
        id=int(getattr(row, "id")),
        template_id=int(getattr(row, "template_id")),
        version_id=int(getattr(row, "version_id")),
        version=int(version_number) if version_number is not None else 0,
        section=str(getattr(row, "section")),
        brick_id=int(getattr(row, "brick_id")),
        code=str(code) if code is not None else "",
        fragment=str(getattr(row, "fragment")),
        occurrence=int(getattr(row, "occurrence")),
        status=str(getattr(row, "status")),
        created_at=getattr(row, "created_at"),
        updated_at=getattr(row, "updated_at"),
    )


def _load_part(session: Session, part_id: int) -> object | None:
    if not _valid_positive_id(part_id):
        return None
    return session.execute(
        select(template_conditional_parts).where(
            template_conditional_parts.c.id == part_id
        )
    ).first()


def _mutation(
    status: str,
    part: ConditionalPartDetails | None = None,
    message: str = "",
) -> ConditionalPartMutationResult:
    return ConditionalPartMutationResult(status=status, part=part, message=message)


def _forbidden_message(template: object) -> str:
    if getattr(template, "kind") == "standard":
        return (
            "Изменять условные части этого шаблона может только "
            "администратор места или суперадминистратор."
        )
    return "Изменять условные части личного шаблона может только его владелец."


def _editable_template(
    session: Session, actor_telegram_user_id: int | None, template_id: int
):
    """
    Доступный шаблон для правки правил.

    Возвращает `(actor, row)`, а вызывающий сам решает, вернуть ли
    `not_found`/`forbidden`: так права одинаковы у всех операций.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    try:
        row = _readable_template_row(session, actor, template_id)
    except TemplateNotFoundError:
        return actor, None
    return actor, row


def _check_revision(
    row: Any, expected_revision: object
) -> ConditionalPartMutationResult | None:
    """`expected_revision` — ревизия ШАБЛОНА, не правила (версию не меняем)."""
    if not _valid_positive_id(expected_revision):
        raise TemplateError("expected_revision должен быть положительным числом.")
    if not isinstance(expected_revision, int):  # pragma: no cover — проверено выше
        raise TemplateError("expected_revision должен быть положительным числом.")
    if int(getattr(row, "revision")) != expected_revision:
        return _mutation(
            "stale",
            None,
            "Шаблон уже изменился в другом окне. Откройте его заново.",
        )
    return None


# --- Чтение -----------------------------------------------------------------


def list_conditional_parts(
    session: Session,
    actor_telegram_user_id: int | None,
    template_id: int,
    *,
    version: int | None = None,
    section: str | None = None,
    include_stale: bool = False,
) -> list[ConditionalPartDetails]:
    """
    Правила доступного шаблона: по умолчанию текущей версии.

    `version` позволяет открыть историю, `section` — одну секцию. Правила
    сортируются по секции и позиции первого вхождения, чтобы редактор
    показывал их в порядке текста.

    `include_stale=True` добавляет правила прежних версий, помеченные
    `stale` (§44): после правки текста они остаются в БД, и редактор обязан
    показать их автору, иначе правило «исчезло» бы из интерфейса молча.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    try:
        template = _readable_template_row(session, actor, template_id)
    except TemplateNotFoundError:
        return []
    version_row = _version_row(session, template, version)
    if include_stale:
        # Правила текущей версии + устаревшие ЛЮБОЙ прежней версии: после
        # правки текста правило остаётся в БД и должно быть видно автору.
        statement = select(template_conditional_parts).where(
            template_conditional_parts.c.template_id == template.id,
            (
                (template_conditional_parts.c.version_id == version_row.id)
                | (template_conditional_parts.c.status == STATUS_STALE)
            ),
        )
    else:
        statement = select(template_conditional_parts).where(
            template_conditional_parts.c.version_id == version_row.id
        )
    if section is not None:
        statement = statement.where(
            template_conditional_parts.c.section == _validate_section(section)
        )
    rows = session.execute(statement).all()
    parts = [_view(session, row) for row in rows]

    def _sort_key(part: ConditionalPartDetails) -> tuple[int, int, int]:
        text = (
            version_row.content
            if part.section == SECTION_CONTENT
            else (version_row.extended_content or "")
        )
        span = _fragment_span(text, part.fragment, part.occurrence)
        # Активные правила текущей версии — выше устаревших: сначала то, что
        # действительно работает.
        return (
            CONDITIONAL_SECTIONS.index(part.section),
            0 if part.status == STATUS_ACTIVE else 1,
            span[0] if span else 0,
        )

    return sorted(parts, key=_sort_key)


def get_conditional_part(
    session: Session,
    actor_telegram_user_id: int | None,
    part_id: int,
) -> ConditionalPartDetails | None:
    """Правило по id; чужой личный шаблон неотличим от несуществующего."""
    actor = _require_actor_user(session, actor_telegram_user_id)
    row = _load_part(session, part_id)
    if row is None:
        return None
    try:
        template = _readable_template_row(
            session, actor, int(getattr(row, "template_id"))
        )
    except TemplateNotFoundError:
        return None
    if template.id != int(getattr(row, "template_id")):
        return None
    return _view(session, row)


def active_rules_for_version(
    session: Session, version_id: int, section: str
) -> tuple[ConditionalRule, ...]:
    """
    Активные правила версии для рендера (без проверки прав).

    Вызывается из уже авторизованного рендера (`template_store`,
    `announcement_store`): значения и определения там загружены по
    сохранённым привязкам, поэтому правило — только текст и кирпичик.
    `stale`-правила не применяются: они не должны скрывать случайное
    похожее место (§44).
    """
    checked = _validate_section(section)
    if not _valid_positive_id(version_id):
        return ()
    rows = session.execute(
        select(
            template_conditional_parts.c.brick_id,
            template_conditional_parts.c.fragment,
            template_conditional_parts.c.occurrence,
        )
        .where(
            template_conditional_parts.c.version_id == version_id,
            template_conditional_parts.c.section == checked,
            template_conditional_parts.c.status == STATUS_ACTIVE,
        )
        .order_by(template_conditional_parts.c.id)
    ).all()
    return tuple(
        ConditionalRule(
            brick_id=int(row.brick_id),
            fragment=str(row.fragment),
            occurrence=int(row.occurrence),
        )
        for row in rows
    )


# --- Создание и правка ------------------------------------------------------


def create_conditional_part(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    expected_revision: int,
    section: str,
    brick_id: int,
    fragment: object,
    occurrence: int = 1,
) -> ConditionalPartMutationResult:
    """
    Добавляет правило к ТЕКУЩЕЙ версии шаблона.

    Новая версия не создаётся и `revision` не меняется: правило живёт
    рядом с неизменяемым текстом версии. Все проверки §40 (фрагмент
    существует, включает кирпичик, нужное вхождение, нет пересечения)
    выполняются до вставки; некорректное правило не сохраняется.
    """
    actor, template = _editable_template(session, actor_telegram_user_id, template_id)
    if template is None:
        return _mutation("not_found", None, "Шаблон не найден.")
    if not _can_edit_template(session, actor, template):
        return _mutation("forbidden", None, _forbidden_message(template))
    stale = _check_revision(template, expected_revision)
    if stale is not None:
        return stale
    checked_section = _validate_section(section)
    checked_occurrence = _validate_occurrence(occurrence)
    if not _valid_positive_id(brick_id):
        raise TemplateError("brick_id должен быть положительным целым числом.")

    version_row = _version_row(session, template)
    try:
        text = _section_text(version_row, checked_section)
    except TemplateError as exc:
        return _mutation("invalid", None, str(exc))
    code = _binding_code(session, version_row.id, brick_id)
    if code is None:
        return _mutation(
            "invalid",
            None,
            "Кирпичик не привязан к этой версии шаблона: правило невозможно.",
        )
    try:
        checked_fragment = _validate_fragment(
            text=text,
            fragment=fragment,
            occurrence=checked_occurrence,
            code=code,
            other_spans=_active_spans(
                session, version_row=version_row, section=checked_section
            ),
        )
    except TemplateError as exc:
        return _mutation("invalid", None, str(exc))

    now = utcnow()
    try:
        result = session.execute(
            template_conditional_parts.insert()
            .values(
                template_id=template.id,
                version_id=version_row.id,
                section=checked_section,
                brick_id=brick_id,
                fragment=checked_fragment,
                occurrence=checked_occurrence,
                status=STATUS_ACTIVE,
                created_at=now,
                updated_at=now,
            )
            .returning(template_conditional_parts.c.id)
        )
        new_id = int(result.scalar_one())
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return _mutation(
            "conflict",
            None,
            "Не удалось сохранить правило из-за одновременной записи. "
            "Повторите попытку.",
        )
    except Exception:
        session.rollback()
        raise
    row = _load_part(session, new_id)
    assert row is not None  # строка только что вставлена
    return _mutation(
        "created",
        _view(session, row),
        "Условная часть сохранена.",
    )


def update_conditional_part(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    part_id: int,
    expected_revision: int,
    fragment: object = None,
    occurrence: int | None = None,
    brick_id: int | None = None,
    repair: bool = False,
) -> ConditionalPartMutationResult:
    """
    Меняет фрагмент/вхождение/кирпичик существующего правила.

    Проверки те же, что при создании; собственное правило исключается из
    проверки пересечений. Версия шаблона и `revision` не меняются.

    `repair=True` — явное исправление правила, помеченного `stale` после
    правки текста (§44): фрагмент проверяется по ТЕКУЩЕЙ версии и правило
    переносится в неё. Без этого флага правило прежней версии не правится
    «наугад» и возвращается `stale`.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    row = _load_part(session, part_id)
    if row is None:
        return _mutation("not_found", None, "Условная часть не найдена.")
    try:
        template = _readable_template_row(
            session, actor, int(getattr(row, "template_id"))
        )
    except TemplateNotFoundError:
        return _mutation("not_found", None, "Условная часть не найдена.")
    if not _can_edit_template(session, actor, template):
        return _mutation("forbidden", None, _forbidden_message(template))
    stale = _check_revision(template, expected_revision)
    if stale is not None:
        return stale

    version_row = _version_row(session, template)
    if version_row.id != int(getattr(row, "version_id")) and not repair:
        return _mutation(
            "stale",
            _view(session, row),
            "Правило относится к прежней версии шаблона. "
            "Сначала перенесите правила в текущую версию.",
        )
    section = str(getattr(row, "section"))
    checked_brick = int(getattr(row, "brick_id")) if brick_id is None else brick_id
    if not _valid_positive_id(checked_brick):
        raise TemplateError("brick_id должен быть положительным целым числом.")
    checked_occurrence = (
        int(getattr(row, "occurrence"))
        if occurrence is None
        else _validate_occurrence(occurrence)
    )
    checked_fragment = getattr(row, "fragment") if fragment is None else fragment

    text = _section_text(version_row, section)
    code = _binding_code(session, version_row.id, checked_brick)
    if code is None:
        return _mutation(
            "invalid",
            _view(session, row),
            "Кирпичик не привязан к этой версии шаблона: правило невозможно.",
        )
    try:
        checked_fragment = _validate_fragment(
            text=text,
            fragment=checked_fragment,
            occurrence=checked_occurrence,
            code=code,
            other_spans=_active_spans(
                session,
                version_row=version_row,
                section=section,
                exclude_id=int(getattr(row, "id")),
            ),
        )
    except TemplateError as exc:
        return _mutation("invalid", _view(session, row), str(exc))

    changed = (
        checked_fragment != getattr(row, "fragment")
        or checked_occurrence != int(getattr(row, "occurrence"))
        or checked_brick != int(getattr(row, "brick_id"))
        or version_row.id != int(getattr(row, "version_id"))
    )
    if not changed:
        return _mutation(
            "unchanged",
            _view(session, row),
            "Правило не изменилось.",
        )
    try:
        values: dict[str, object] = {
            "brick_id": checked_brick,
            "fragment": checked_fragment,
            "occurrence": checked_occurrence,
            "status": STATUS_ACTIVE,
            "updated_at": utcnow(),
        }
        if version_row.id != int(getattr(row, "version_id")):
            # Явное исправление устаревшего правила: оно становится
            # правилом ТЕКУЩЕЙ версии (§44).
            values["version_id"] = version_row.id
        session.execute(
            sqlalchemy_update(template_conditional_parts)
            .where(template_conditional_parts.c.id == int(getattr(row, "id")))
            .values(**values)
        )
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return _mutation(
            "conflict",
            None,
            "Не удалось сохранить правило из-за одновременной записи. "
            "Повторите попытку.",
        )
    except Exception:
        session.rollback()
        raise
    updated = _load_part(session, part_id)
    assert updated is not None
    return _mutation("updated", _view(session, updated), "Правило изменено.")


def delete_conditional_part(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    part_id: int,
    expected_revision: int | None = None,
) -> ConditionalPartMutationResult:
    """
    Удаляет правило. Пользовательское правило удаляется только по явному
    действию автора (`stale`-правила нельзя терять молча, §44).
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    row = _load_part(session, part_id)
    if row is None:
        return _mutation("not_found", None, "Условная часть не найдена.")
    try:
        template = _readable_template_row(
            session, actor, int(getattr(row, "template_id"))
        )
    except TemplateNotFoundError:
        return _mutation("not_found", None, "Условная часть не найдена.")
    if not _can_edit_template(session, actor, template):
        return _mutation("forbidden", None, _forbidden_message(template))
    if expected_revision is not None:
        stale = _check_revision(template, expected_revision)
        if stale is not None:
            return stale
    part = _view(session, row)
    try:
        session.execute(
            sqlalchemy_delete(template_conditional_parts).where(
                template_conditional_parts.c.id == part.id
            )
        )
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return _mutation(
            "conflict",
            part,
            "Не удалось удалить правило из-за одновременной записи. "
            "Повторите попытку.",
        )
    except Exception:
        session.rollback()
        raise
    return _mutation("deleted", part, "Условная часть удалена.")


# --- Актуальность после изменения шаблона (§44) -----------------------------


def refresh_conditional_parts(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    expected_revision: int | None = None,
    section: str | None = None,
) -> ConditionalRefreshResult:
    """
    Переносит правила прежних версий в текущую версию — только однозначно.

    «Однозначно» = фрагмент встречается в новом тексте ровно один раз, а
    управляющий кирпичик по-прежнему привязан к версии. Иначе правило
    помечается `stale` и ОСТАЁТСЯ в БД: автор увидит проблему и сможет
    перенастроить её. Правило никогда не применяется к случайному
    похожему месту и не удаляется молча.

    `revision` шаблона не меняется: перенос правила — не новая версия.
    """
    actor, template = _editable_template(session, actor_telegram_user_id, template_id)
    if template is None:
        return ConditionalRefreshResult("not_found", message="Шаблон не найден.")
    if not _can_edit_template(session, actor, template):
        return ConditionalRefreshResult(
            "forbidden", message=_forbidden_message(template)
        )
    if expected_revision is not None:
        if not _valid_positive_id(expected_revision):
            raise TemplateError("expected_revision должен быть положительным числом.")
        if int(template.revision) != int(expected_revision):
            return ConditionalRefreshResult(
                "stale",
                message="Шаблон уже изменился в другом окне. Откройте его заново.",
            )

    current = _version_row(session, template)
    statement = select(template_conditional_parts).where(
        template_conditional_parts.c.template_id == template.id,
        template_conditional_parts.c.version_id != current.id,
        template_conditional_parts.c.status == STATUS_ACTIVE,
    )
    if section is not None:
        statement = statement.where(
            template_conditional_parts.c.section == _validate_section(section)
        )
    rows = session.execute(statement.order_by(template_conditional_parts.c.id)).all()

    transferred = 0
    marked_stale = 0
    try:
        for row in rows:
            part_id = int(getattr(row, "id"))
            part_section = str(getattr(row, "section"))
            brick_id = int(getattr(row, "brick_id"))
            text = (
                current.content
                if part_section == SECTION_CONTENT
                else (current.extended_content or "")
            )
            code = _binding_code(session, current.id, brick_id)
            fragment = str(getattr(row, "fragment"))
            span = _fragment_span(text, fragment, 1) if text and code else None
            unambiguous = (
                span is not None
                and text.count(fragment) == 1
                and _binding_code(session, current.id, brick_id) is not None
            )
            if unambiguous:
                session.execute(
                    sqlalchemy_update(template_conditional_parts)
                    .where(template_conditional_parts.c.id == part_id)
                    .values(
                        version_id=current.id,
                        occurrence=1,
                        status=STATUS_ACTIVE,
                        updated_at=utcnow(),
                    )
                )
                transferred += 1
            else:
                session.execute(
                    sqlalchemy_update(template_conditional_parts)
                    .where(template_conditional_parts.c.id == part_id)
                    .values(status=STATUS_STALE, updated_at=utcnow())
                )
                marked_stale += 1
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return ConditionalRefreshResult(
            "conflict",
            message="Не удалось перенести правила из-за одновременной записи. "
            "Повторите попытку.",
        )
    except Exception:
        session.rollback()
        raise

    parts = tuple(
        _view(session, row)
        for row in session.execute(
            select(template_conditional_parts)
            .where(template_conditional_parts.c.version_id == current.id)
            .order_by(template_conditional_parts.c.id)
        ).all()
    )
    return ConditionalRefreshResult(
        "refreshed",
        parts=parts,
        transferred=transferred,
        stale=marked_stale,
        message=(
            f"Перенесено правил: {transferred}; "
            f"требуют перенастройки: {marked_stale}."
        ),
    )


__all__ = [
    "CONDITIONAL_SECTIONS",
    "CONDITIONAL_STATUSES",
    "SECTION_CONTENT",
    "SECTION_EXTENDED_CONTENT",
    "STATUS_ACTIVE",
    "STATUS_STALE",
    "ConditionalPartDetails",
    "ConditionalPartMutationResult",
    "ConditionalRefreshResult",
    "TemplateActorError",
    "active_rules_for_version",
    "create_conditional_part",
    "delete_conditional_part",
    "get_conditional_part",
    "list_conditional_parts",
    "refresh_conditional_parts",
    "template_conditional_parts",
    "update_conditional_part",
]
