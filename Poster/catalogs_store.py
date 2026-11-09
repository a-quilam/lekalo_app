# catalogs_store.py
"""
Слой данных справочников кураторов и подвалов («Лекало» 2.6, этап B1).

Каждый справочник живёт в одной из двух областей:

- справочник площадки (`place_id`), доступный только действующим
  администраторам этой площадки (`permissions.can_manage_place`);
- личный справочник пользователя (`owner_user_id` — внутренний `users.id`);
  владелец определяется постоянным Telegram user ID, не username.

Права проверяются здесь, а не только в UI: каждая операция заново читает
роль из БД. Чужой личный справочник не раскрывается (`CatalogNotFoundError`),
недостаток полномочий на площадке — явный `CatalogPermissionError`.

Форматирование хранится по модели значений анонса (`announcement_store`):
plain-текст — источник истины, безопасный HTML (`utils.tg_html`) — то, что
уходит в Telegram. HTML без видимого текста и HTML, равный экранированному
plain-тексту, не сохраняется: двойного экранирования и «пустой разметки» нет.

Физического удаления нет: `archive_*` переводит запись в `archived`,
`restore_*` возвращает её одним действием. ID и содержимое при этом не
меняются, поэтому старые анонсы и callback'и остаются валидными.

UI-обработчиков и текстов интерфейса здесь нет — это следующий этап.
"""

from __future__ import annotations

import html as html_module
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from models import CuratorEntry, FooterEntry, Place, User, utcnow
from permissions import can_manage_place
from utils.pagination import PAGE_SIZE, PageSlice, page_items
from utils.tg_html import escape_html, sanitize_html

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"
CATALOG_STATUSES = (STATUS_ACTIVE, STATUS_ARCHIVED)

# Максимальная длина названия подвала: подпись кнопки списка. Не ограничиваем
# текст подстановки — это постоянное напоминание гостям.
MAX_FOOTER_NAME_LENGTH = 100

_TAG_RE = re.compile(r"<[^<>]*>")


class CatalogError(ValueError):
    """Базовая ошибка операций со справочниками."""


class CatalogPermissionError(PermissionError):
    """Область справочника недоступна вызывающему (чужая площадка/владелец)."""


class CatalogNotFoundError(CatalogError):
    """Запись не найдена или не видима вызывающему."""


class CatalogConflictError(CatalogError):
    """Название подвала занято внутри области владения."""


@dataclass(frozen=True)
class CuratorEntryView:
    """Снимок записи куратора для карточки, списка и подстановки."""

    id: int
    place_id: int | None
    owner_user_id: int | None
    value_plain: str
    value_html: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def is_archived(self) -> bool:
        return self.status == STATUS_ARCHIVED

    @property
    def html(self) -> str:
        """
        Готовый HTML контакта без технической разметки в тексте.

        Отсутствие сохранённого HTML означает «форматирования нет» — тогда
        plain-текст экранируется один раз.
        """
        return self.value_html or str(escape_html(self.value_plain))

    def label(self) -> str:
        """Короткая подпись кнопки: plain-текст одной строкой без разметки."""
        collapsed = " ".join(self.value_plain.split())
        return collapsed or self.value_plain


@dataclass(frozen=True)
class FooterEntryView:
    """Снимок записи подвала: название для списка и текст для анонса."""

    id: int
    place_id: int | None
    owner_user_id: int | None
    name: str
    text_plain: str
    text_html: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def is_archived(self) -> bool:
        return self.status == STATUS_ARCHIVED

    @property
    def html(self) -> str:
        """HTML текста подвала — именно он подставляется в анонс."""
        return self.text_html or str(escape_html(self.text_plain))


def _valid_telegram_user_id(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _actor_user(session: Session, telegram_user_id: int | None) -> User | None:
    if not _valid_telegram_user_id(telegram_user_id):
        return None
    return session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))


def _require_place_scope(
    session: Session, actor_telegram_user_id: int | None, place_id: int
) -> None:
    if isinstance(place_id, bool) or not isinstance(place_id, int) or place_id <= 0:
        raise CatalogNotFoundError("Некорректный идентификатор площадки.")
    if session.get(Place, place_id) is None:
        raise CatalogNotFoundError("Площадка не найдена.")
    if not can_manage_place(session, actor_telegram_user_id, place_id):
        raise CatalogPermissionError(
            "Управлять справочником площадки может только её администратор."
        )


def _scope_owner(
    session: Session,
    actor_telegram_user_id: int | None,
    place_id: int | None,
) -> tuple[User | None, int | None]:
    """
    Проверяет право на область и возвращает `(актор, owner_user_id)`.

    `place_id is None` — личный справочник: он доступен только владельцу,
    поэтому `owner_user_id` всегда внутренний ID самого вызывающего.
    """
    if place_id is not None:
        _require_place_scope(session, actor_telegram_user_id, place_id)
        return _actor_user(session, actor_telegram_user_id), None
    actor = _actor_user(session, actor_telegram_user_id)
    if actor is None:
        raise CatalogPermissionError("Личный справочник доступен только его владельцу.")
    return actor, actor.id


def _selection_owner(
    session: Session,
    actor_telegram_user_id: int | None,
    place_id: int | None,
) -> int | None:
    """
    Область справочника в режиме ВЫБОРА значения для анонса (§9–10).

    Читать список для подстановки в анонс вправе любой действующий
    пользователь Poster — не только администратор площадки: управление
    справочником (создание/правка/архив) остаётся за `_scope_owner`.
    Личный справочник по-прежнему виден только владельцу.
    """
    if place_id is not None:
        if isinstance(place_id, bool) or not isinstance(place_id, int) or place_id <= 0:
            raise CatalogNotFoundError("Некорректный идентификатор площадки.")
        if session.get(Place, place_id) is None:
            raise CatalogNotFoundError("Площадка не найдена.")
        if _actor_user(session, actor_telegram_user_id) is None:
            raise CatalogPermissionError(
                "Выбор из справочника доступен только зарегистрированному "
                "пользователю."
            )
        return None
    actor = _actor_user(session, actor_telegram_user_id)
    if actor is None:
        raise CatalogPermissionError("Личный справочник доступен только его владельцу.")
    return actor.id


def _ensure_status(status: str) -> str:
    if status not in CATALOG_STATUSES:
        raise CatalogError(f"Неизвестный статус записи: {status!r}.")
    return status


def _require_plain(raw: object, *, what: str) -> str:
    text = str(raw if raw is not None else "")
    if not text.strip():
        raise CatalogError(f"{what} не может быть пустым.")
    return text


def _visible_text(rendered_html: str) -> str:
    """Текст без тегов — проверка, что HTML не рисует пустоту."""
    without_tags = _TAG_RE.sub("", rendered_html or "")
    return html_module.unescape(without_tags).strip()


def _safe_html(raw: object, plain: str) -> str | None:
    """
    Безопасный HTML значения или `None`, если форматирования фактически нет.

    Совпадает с контрактом `announcement_store._encode_value`: HTML,
    равный экранированному plain-тексту, и разметка без видимого текста не
    хранятся, чтобы не появлялось двойного экранирования и пустых тегов.
    """
    html = getattr(raw, "html", None)
    if not isinstance(html, str) or not html:
        return None
    sanitized = str(sanitize_html(html))
    if sanitized == str(escape_html(plain)):
        return None
    if not _visible_text(sanitized):
        return None
    return sanitized


def _name_search(name: str) -> str:
    """Нормализованное название подвала: NFKC, casefold, сжатые пробелы."""
    normalized = unicodedata.normalize("NFKC", name).casefold().replace("ё", "е")
    return " ".join(normalized.split())


def _clean_footer_name(raw: object) -> str:
    name = " ".join(unicodedata.normalize("NFKC", str(raw or "")).split())
    if not name:
        raise CatalogError("Название подвала не может быть пустым.")
    if len(name) > MAX_FOOTER_NAME_LENGTH:
        raise CatalogError(
            "Название подвала слишком длинное: максимум "
            f"{MAX_FOOTER_NAME_LENGTH} символов, получено {len(name)}."
        )
    return name


def _curator_view(row: CuratorEntry) -> CuratorEntryView:
    return CuratorEntryView(
        id=row.id,
        place_id=row.place_id,
        owner_user_id=row.owner_user_id,
        value_plain=row.value_plain,
        value_html=row.value_html,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _footer_view(row: FooterEntry) -> FooterEntryView:
    return FooterEntryView(
        id=row.id,
        place_id=row.place_id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        text_plain=row.text_plain,
        text_html=row.text_html,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _commit(session: Session) -> None:
    try:
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise CatalogError(
            "Не удалось сохранить запись из-за одновременной записи. "
            "Повторите попытку."
        ) from exc


def _scope_conditions(model: Any, place_id: int | None, owner_user_id: int | None):
    if place_id is not None:
        return (model.place_id == place_id, model.owner_user_id.is_(None))
    return (model.owner_user_id == owner_user_id, model.place_id.is_(None))


def _load_curator(
    session: Session, entry_id: int, *, place_id: int | None, owner_user_id: int | None
) -> CuratorEntry:
    if isinstance(entry_id, bool) or not isinstance(entry_id, int) or entry_id <= 0:
        raise CatalogNotFoundError("Запись не найдена.")
    row = session.scalar(
        select(CuratorEntry).where(
            CuratorEntry.id == entry_id,
            *_scope_conditions(CuratorEntry, place_id, owner_user_id),
        )
    )
    if row is None:
        if place_id is None and session.get(CuratorEntry, entry_id) is not None:
            # Строка есть, но принадлежит другому пользователю: не раскрываем
            # содержимое, а сообщаем о нехватке прав (§18).
            raise CatalogPermissionError(
                "Личный справочник доступен только его владельцу."
            )
        raise CatalogNotFoundError("Куратор не найден.")
    return row


def _load_footer(
    session: Session, entry_id: int, *, place_id: int | None, owner_user_id: int | None
) -> FooterEntry:
    if isinstance(entry_id, bool) or not isinstance(entry_id, int) or entry_id <= 0:
        raise CatalogNotFoundError("Запись не найдена.")
    row = session.scalar(
        select(FooterEntry).where(
            FooterEntry.id == entry_id,
            *_scope_conditions(FooterEntry, place_id, owner_user_id),
        )
    )
    if row is None:
        if place_id is None and session.get(FooterEntry, entry_id) is not None:
            raise CatalogPermissionError(
                "Личный справочник доступен только его владельцу."
            )
        raise CatalogNotFoundError("Подвал не найден.")
    return row


def _list_entries(
    session: Session,
    model: Any,
    *,
    place_id: int | None,
    owner_user_id: int | None,
    status: str,
    page: int,
    page_size: int,
) -> PageSlice[Any]:
    _ensure_status(status)
    if page_size <= 0:
        raise CatalogError("Размер страницы должен быть положительным.")
    rows: list[Any] = list(
        session.scalars(
            select(model)
            .where(
                *_scope_conditions(model, place_id, owner_user_id),
                model.status == status,
            )
            .order_by(model.id)
        ).all()
    )
    return page_items(rows, page, page_size)


def _count(session: Session, model: Any, *, place_id, owner_user_id, status) -> int:
    _ensure_status(status)
    return int(
        session.scalar(
            select(func.count(model.id)).where(
                *_scope_conditions(model, place_id, owner_user_id),
                model.status == status,
            )
        )
        or 0
    )


# --- Кураторы ---------------------------------------------------------------


def create_curator(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    value: object,
) -> CuratorEntryView:
    """
    Добавляет контакт куратора в справочник площадки или личный.

    `value` — текст сообщения (или `FormattedText` из `capture_message_text`):
    plain-текст сохраняется дословно, разметка — только безопасным HTML.
    """
    actor, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    plain = _require_plain(value, what="Контакт куратора")
    row = CuratorEntry(
        place_id=place_id,
        owner_user_id=owner_user_id,
        value_plain=plain,
        value_html=_safe_html(value, plain),
        status=STATUS_ACTIVE,
        created_by_user_id=actor.id if actor is not None else None,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(row)
    try:
        session.flush()
        entry_id = row.id
        _commit(session)
    except Exception:
        session.rollback()
        raise
    stored = session.get(CuratorEntry, entry_id)
    assert stored is not None  # строка вставлена в этой транзакции
    return _curator_view(stored)


def get_curator(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    entry_id: int,
    include_archived: bool = True,
) -> CuratorEntryView:
    """Карточка куратора; чужая запись не раскрывается."""
    _, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    row = _load_curator(
        session, entry_id, place_id=place_id, owner_user_id=owner_user_id
    )
    if not include_archived and row.status != STATUS_ACTIVE:
        raise CatalogNotFoundError("Куратор не найден.")
    return _curator_view(row)


def list_curators(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    status: str = STATUS_ACTIVE,
    page: int = 0,
    page_size: int = PAGE_SIZE,
) -> PageSlice[CuratorEntryView]:
    """Страница кураторов области: активные — по умолчанию, архивные — явно."""
    _, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    slice_ = _list_entries(
        session,
        CuratorEntry,
        place_id=place_id,
        owner_user_id=owner_user_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    return PageSlice(
        items=[_curator_view(row) for row in slice_.items],
        page=slice_.page,
        total_pages=slice_.total_pages,
        total=slice_.total,
        page_size=slice_.page_size,
    )


def count_curators(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    status: str = STATUS_ACTIVE,
) -> int:
    """Количество записей области (для пустого списка и счётчиков)."""
    _, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    return _count(
        session,
        CuratorEntry,
        place_id=place_id,
        owner_user_id=owner_user_id,
        status=status,
    )


def update_curator(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    entry_id: int,
    value: object,
) -> CuratorEntryView:
    """Меняет контакт, сохраняя прежний ID и форматирование нового ввода."""
    _, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    row = _load_curator(
        session, entry_id, place_id=place_id, owner_user_id=owner_user_id
    )
    if row.status != STATUS_ACTIVE:
        raise CatalogError(
            "Архивного куратора нельзя редактировать. Сначала восстановите его."
        )
    plain = _require_plain(value, what="Контакт куратора")
    row.value_plain = plain
    row.value_html = _safe_html(value, plain)
    row.updated_at = utcnow()
    try:
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _curator_view(row)


def _set_curator_status(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    entry_id: int,
    expected: str,
    target: str,
) -> CuratorEntryView:
    _, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    row = _load_curator(
        session, entry_id, place_id=place_id, owner_user_id=owner_user_id
    )
    if row.status == target:
        raise CatalogError(
            "Куратор уже в архиве."
            if target == STATUS_ARCHIVED
            else "Куратор уже активен."
        )
    result = session.execute(
        update(CuratorEntry)
        .where(
            CuratorEntry.id == row.id,
            CuratorEntry.status == expected,
        )
        .values(status=target, updated_at=utcnow())
    )
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        session.rollback()
        raise CatalogError("Запись уже изменилась. Откройте её заново.")
    _commit(session)
    session.refresh(row)
    return _curator_view(row)


def archive_curator(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    entry_id: int,
) -> CuratorEntryView:
    """Логическое архивирование: строка и ID сохраняются."""
    return _set_curator_status(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        place_id=place_id,
        entry_id=entry_id,
        expected=STATUS_ACTIVE,
        target=STATUS_ARCHIVED,
    )


def restore_curator(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    entry_id: int,
) -> CuratorEntryView:
    """Восстановление архивного куратора одним действием, без подтверждения."""
    return _set_curator_status(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        place_id=place_id,
        entry_id=entry_id,
        expected=STATUS_ARCHIVED,
        target=STATUS_ACTIVE,
    )


# --- Подвалы ----------------------------------------------------------------


def _footer_name_taken(
    session: Session,
    *,
    place_id: int | None,
    owner_user_id: int | None,
    name_search: str,
    exclude_id: int | None = None,
) -> bool:
    statement = select(FooterEntry.id).where(
        *_scope_conditions(FooterEntry, place_id, owner_user_id),
        FooterEntry.name_search == name_search,
    )
    if exclude_id is not None:
        statement = statement.where(FooterEntry.id != exclude_id)
    return session.scalar(statement.limit(1)) is not None


def create_footer(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    name: object,
    text: object,
) -> FooterEntryView:
    """
    Создаёт подвал: название для списка и текст для подстановки в анонс.

    Название уникально внутри области владения без учёта регистра; архивные
    записи тоже занимают название (иначе восстановление дало бы дубль).
    """
    actor, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    clean_name = _clean_footer_name(name)
    plain = _require_plain(text, what="Текст подвала")
    search = _name_search(clean_name)
    if _footer_name_taken(
        session, place_id=place_id, owner_user_id=owner_user_id, name_search=search
    ):
        raise CatalogConflictError("Подвал с таким названием уже есть.")
    row = FooterEntry(
        place_id=place_id,
        owner_user_id=owner_user_id,
        name=clean_name,
        name_search=search,
        text_plain=plain,
        text_html=_safe_html(text, plain),
        status=STATUS_ACTIVE,
        created_by_user_id=actor.id if actor is not None else None,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(row)
    try:
        session.flush()
        entry_id = row.id
        _commit(session)
    except IntegrityError as exc:
        session.rollback()
        raise CatalogConflictError("Подвал с таким названием уже есть.") from exc
    except Exception:
        session.rollback()
        raise
    stored = session.get(FooterEntry, entry_id)
    assert stored is not None
    return _footer_view(stored)


def get_footer(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    entry_id: int,
    include_archived: bool = True,
) -> FooterEntryView:
    """Карточка подвала; чужая запись не раскрывается."""
    _, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    row = _load_footer(
        session, entry_id, place_id=place_id, owner_user_id=owner_user_id
    )
    if not include_archived and row.status != STATUS_ACTIVE:
        raise CatalogNotFoundError("Подвал не найден.")
    return _footer_view(row)


def list_footers(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    status: str = STATUS_ACTIVE,
    page: int = 0,
    page_size: int = PAGE_SIZE,
) -> PageSlice[FooterEntryView]:
    """Страница подвалов области: активные по умолчанию, архивные — явно."""
    _, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    slice_ = _list_entries(
        session,
        FooterEntry,
        place_id=place_id,
        owner_user_id=owner_user_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    return PageSlice(
        items=[_footer_view(row) for row in slice_.items],
        page=slice_.page,
        total_pages=slice_.total_pages,
        total=slice_.total,
        page_size=slice_.page_size,
    )


def count_footers(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    status: str = STATUS_ACTIVE,
) -> int:
    """Количество подвалов области."""
    _, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    return _count(
        session,
        FooterEntry,
        place_id=place_id,
        owner_user_id=owner_user_id,
        status=status,
    )


def update_footer_text(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    entry_id: int,
    text: object,
) -> FooterEntryView:
    """Меняет текст подвала (название и ID не меняются)."""
    _, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    row = _load_footer(
        session, entry_id, place_id=place_id, owner_user_id=owner_user_id
    )
    if row.status != STATUS_ACTIVE:
        raise CatalogError(
            "Архивный подвал нельзя редактировать. Сначала восстановите его."
        )
    plain = _require_plain(text, what="Текст подвала")
    row.text_plain = plain
    row.text_html = _safe_html(text, plain)
    row.updated_at = utcnow()
    try:
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _footer_view(row)


def update_footer_name(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    entry_id: int,
    name: object,
) -> FooterEntryView:
    """Меняет название подвала; уникальность проверяется в его области."""
    _, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    row = _load_footer(
        session, entry_id, place_id=place_id, owner_user_id=owner_user_id
    )
    if row.status != STATUS_ACTIVE:
        raise CatalogError(
            "Архивный подвал нельзя редактировать. Сначала восстановите его."
        )
    clean_name = _clean_footer_name(name)
    search = _name_search(clean_name)
    if _footer_name_taken(
        session,
        place_id=place_id,
        owner_user_id=owner_user_id,
        name_search=search,
        exclude_id=row.id,
    ):
        raise CatalogConflictError("Подвал с таким названием уже есть.")
    row.name = clean_name
    row.name_search = search
    row.updated_at = utcnow()
    try:
        _commit(session)
    except IntegrityError as exc:
        session.rollback()
        raise CatalogConflictError("Подвал с таким названием уже есть.") from exc
    except Exception:
        session.rollback()
        raise
    return _footer_view(row)


def _set_footer_status(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    entry_id: int,
    expected: str,
    target: str,
) -> FooterEntryView:
    _, owner_user_id = _scope_owner(session, actor_telegram_user_id, place_id)
    row = _load_footer(
        session, entry_id, place_id=place_id, owner_user_id=owner_user_id
    )
    if row.status == target:
        raise CatalogError(
            "Подвал уже в архиве."
            if target == STATUS_ARCHIVED
            else "Подвал уже активен."
        )
    result = session.execute(
        update(FooterEntry)
        .where(
            FooterEntry.id == row.id,
            FooterEntry.status == expected,
        )
        .values(status=target, updated_at=utcnow())
    )
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        session.rollback()
        raise CatalogError("Запись уже изменилась. Откройте её заново.")
    _commit(session)
    session.refresh(row)
    return _footer_view(row)


def archive_footer(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    entry_id: int,
) -> FooterEntryView:
    """Логическое архивирование подвала: строка и ID сохраняются."""
    return _set_footer_status(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        place_id=place_id,
        entry_id=entry_id,
        expected=STATUS_ACTIVE,
        target=STATUS_ARCHIVED,
    )


def restore_footer(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int | None,
    entry_id: int,
) -> FooterEntryView:
    """Восстановление архивного подвала одним действием."""
    return _set_footer_status(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        place_id=place_id,
        entry_id=entry_id,
        expected=STATUS_ARCHIVED,
        target=STATUS_ACTIVE,
    )


# --- Выбор значения для анонса (§9–10) -------------------------------------


def selectable_curators(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    place_id: int | None,
) -> tuple[CuratorEntryView, ...]:
    """
    Активные кураторы области для подстановки в анонс.

    В отличие от `list_curators`, чтение доступно любому действующему
    пользователю Poster: администратор площадки нужен только для
    РЕДАКТИРОВАНИЯ справочника. Личный список по-прежнему принадлежит
    владельцу. Возвращаются все активные записи в порядке ID.
    """
    owner_user_id = _selection_owner(session, actor_telegram_user_id, place_id)
    rows = session.scalars(
        select(CuratorEntry)
        .where(
            *_scope_conditions(CuratorEntry, place_id, owner_user_id),
            CuratorEntry.status == STATUS_ACTIVE,
        )
        .order_by(CuratorEntry.id)
    ).all()
    return tuple(_curator_view(row) for row in rows)


def get_selectable_curator(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    place_id: int | None,
    entry_id: int,
) -> CuratorEntryView:
    """Один активный куратор области для подстановки в анонс (иначе — 404)."""
    owner_user_id = _selection_owner(session, actor_telegram_user_id, place_id)
    row = _load_curator(
        session, entry_id, place_id=place_id, owner_user_id=owner_user_id
    )
    if row.status != STATUS_ACTIVE:
        raise CatalogNotFoundError("Куратор не найден.")
    return _curator_view(row)


def selectable_footers(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    place_id: int | None,
) -> tuple[FooterEntryView, ...]:
    """Активные подвалы области для подстановки в анонс (§10)."""
    owner_user_id = _selection_owner(session, actor_telegram_user_id, place_id)
    rows = session.scalars(
        select(FooterEntry)
        .where(
            *_scope_conditions(FooterEntry, place_id, owner_user_id),
            FooterEntry.status == STATUS_ACTIVE,
        )
        .order_by(FooterEntry.id)
    ).all()
    return tuple(_footer_view(row) for row in rows)


def get_selectable_footer(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    place_id: int | None,
    entry_id: int,
) -> FooterEntryView:
    """Один активный подвал области для подстановки в анонс (иначе — 404)."""
    owner_user_id = _selection_owner(session, actor_telegram_user_id, place_id)
    row = _load_footer(
        session, entry_id, place_id=place_id, owner_user_id=owner_user_id
    )
    if row.status != STATUS_ACTIVE:
        raise CatalogNotFoundError("Подвал не найден.")
    return _footer_view(row)


__all__ = [
    "CATALOG_STATUSES",
    "CatalogConflictError",
    "CatalogError",
    "CatalogNotFoundError",
    "CatalogPermissionError",
    "CuratorEntryView",
    "FooterEntryView",
    "MAX_FOOTER_NAME_LENGTH",
    "STATUS_ACTIVE",
    "STATUS_ARCHIVED",
    "archive_curator",
    "archive_footer",
    "count_curators",
    "count_footers",
    "create_curator",
    "create_footer",
    "get_curator",
    "get_footer",
    "get_selectable_curator",
    "get_selectable_footer",
    "list_curators",
    "list_footers",
    "restore_curator",
    "restore_footer",
    "selectable_curators",
    "selectable_footers",
    "update_curator",
    "update_footer_name",
    "update_footer_text",
]
