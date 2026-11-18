# place_photo_store.py
"""
Фототека площадки (Poster 2.6.2, требование 3.3).

Зачем отдельный слой: фототека — библиотека САМОЙ площадки, из которой автор
анонса выбирает снимки. Бинарных копий нет: хранится только Telegram
`file_id`, поэтому библиотека не занимает диск и не требует доступа к
файловому хранилищу. `file_unique_id` — ключ дедупликации: тот же снимок не
попадает в одну площадку дважды (уникальный индекс `ux_place_photos_unique`).

Правила:

- читать и менять библиотеку может только управляющий ЭТИМ местом (или
  суперадминистратор) — права проверяются по фактическим строкам БД на
  каждом действии, а не по факту прихода в экран;
- принимаются только фотографии и документы с MIME `image/*`; прочие файлы
  отклоняются понятным сообщением, а не ошибкой 500;
- размер ограничен одной константой (`PLACE_PHOTO_MAX_BYTES`) — ответ
  Telegram приходит до скачивания, поэтому лимит проверяется по метаданным;
- удаление строки библиотеки НЕ ломает черновик/анонс: те хранят свой
  `file_id`-снимок, а не ссылку на строку.

Схема `place_photos` создана миграцией `place_photos_2_6_2`; этот модуль её
не меняет.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, TypeGuard

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from models import Place, PlacePhoto, User, utcnow
from permissions import can_manage_place

# Один лимит на всё: Bot API отдаёт боту файлы до 20 МБ, поэтому больший
# снимок площадка всё равно не сможет переиспользовать. Проверяется по
# метаданным Update — скачивания нет.
PLACE_PHOTO_MAX_BYTES = 20 * 1024 * 1024

# Ограничение длины `file_id`/`file_unique_id`: защита от мусора из подделки.
PLACE_PHOTO_MAX_ID_LENGTH = 4096

# Сколько снимков показывает одна страница списка фототеки.
PLACE_PHOTO_PAGE_SIZE = 5


class PlacePhotoError(ValueError):
    """Снимок нельзя принять в фототеку (тип, размер, пустые идентификаторы)."""


class PlacePhotoPermissionError(PlacePhotoError):
    """Нет прав управлять фототекой этой площадки."""


class PlacePhotoNotFoundError(PlacePhotoError):
    """Место или снимок не найдены (в том числе чужой снимок площадки)."""


@dataclass(frozen=True)
class PhotoCandidate:
    """
    Кандидат из Update: то, что реально пришло в сообщении.

    `kind` — `"photo"` (сжатый снимок Telegram) или `"document"` (файл).
    У документа обязателен `mime_type`: без него тип не подтверждён и файл
    отклоняется — «на глаз» формат не угадывается.
    """

    file_id: str
    file_unique_id: str
    kind: str = "photo"
    mime_type: str | None = None
    file_size: int | None = None


@dataclass(frozen=True)
class PlacePhotoView:
    """Строка библиотеки глазами экрана: номер порядка считается вызывающим."""

    photo_id: int
    file_id: str
    file_unique_id: str

    @property
    def telegram_file_id(self) -> str:
        """Псевдоним для ясности: в анонс уходит именно `file_id`."""
        return self.file_id


@dataclass(frozen=True)
class PhotoRejection:
    """Отклонённый кандидат: причина уже сформулирована для пользователя."""

    file_id: str
    reason: str


@dataclass(frozen=True)
class PlacePhotoBatch:
    """
    Итог приёма партии (одно фото или альбом).

    `accepted` — `file_id` новых строк; `duplicate` — уже бывшие в библиотеке
    (повторная отправка, тихий пропуск); `rejected` — причины отказа. Дубли не
    считаются ошибкой: снимок в библиотеке уже есть, второй строки нет.
    """

    photos: tuple[PlacePhotoView, ...] = ()
    accepted: tuple[str, ...] = ()
    duplicate: tuple[str, ...] = ()
    rejected: tuple[PhotoRejection, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return bool(self.accepted)


@dataclass(frozen=True)
class PlacePhotoMutation:
    """Результат удаления: статус и текст для экрана, без исключений."""

    status: str
    message: str | None = None


# --- проверки ---------------------------------------------------------------


def _valid_place_id(place_id: object) -> TypeGuard[int]:
    return not isinstance(place_id, bool) and isinstance(place_id, int) and place_id > 0


def _valid_photo_id(photo_id: object) -> TypeGuard[int]:
    return not isinstance(photo_id, bool) and isinstance(photo_id, int) and photo_id > 0


def _require_place(session: Session, place_id: object) -> Place:
    if not _valid_place_id(place_id):
        raise PlacePhotoNotFoundError("Некорректный идентификатор площадки.")
    place = session.get(Place, place_id)
    if place is None:
        raise PlacePhotoNotFoundError("Площадка не найдена.")
    return place


def _require_actor(session: Session, telegram_user_id: object) -> User:
    if (
        isinstance(telegram_user_id, bool)
        or not isinstance(telegram_user_id, int)
        or telegram_user_id <= 0
    ):
        raise PlacePhotoPermissionError("Пользователь не определён.")
    actor = session.scalar(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )
    if actor is None:
        raise PlacePhotoPermissionError(
            "Пользователь ещё не зарегистрирован в таблице пользователей."
        )
    return actor


def _require_managed_place(session: Session, actor: User, place_id: object) -> Place:
    place = _require_place(session, place_id)
    if not can_manage_place(session, actor.telegram_user_id, place.id):
        raise PlacePhotoPermissionError(
            "Фототеку этой площадки меняет только её администратор "
            "или суперадминистратор."
        )
    return place


def _clean_id(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise PlacePhotoError(f"{label} должен быть строкой.")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > PLACE_PHOTO_MAX_ID_LENGTH:
        raise PlacePhotoError(f"Некорректный {label}.")
    return cleaned


def _has_image_mime(mime_type: object) -> bool:
    if not isinstance(mime_type, str):
        return False
    return mime_type.strip().lower().startswith("image/")


def validate_candidate(candidate: PhotoCandidate) -> PhotoCandidate:
    """
    Проверяет один кандидат и возвращает очищенное значение.

    Отказ — `PlacePhotoError` с текстом для пользователя: этот слой не
    бросает технических исключений, экран просто показывает причину.
    """
    if candidate.kind == "photo":
        file_id = _clean_id(candidate.file_id, label="file_id фотографии")
        unique = _clean_id(candidate.file_unique_id, label="file_unique_id")
    elif candidate.kind == "document":
        file_id = _clean_id(candidate.file_id, label="file_id файла")
        unique = _clean_id(candidate.file_unique_id, label="file_unique_id")
        if not _has_image_mime(candidate.mime_type):
            raise PlacePhotoError(
                "Это не изображение: приложите фото или файл-картинку "
                "(JPEG, PNG, WebP и т.п.)."
            )
    else:  # неизвестный вид вложения — не принимаем молча
        raise PlacePhotoError("Приложите фотографию или файл-изображение.")
    size = candidate.file_size
    if isinstance(size, bool):
        size = None
    if isinstance(size, int) and size > PLACE_PHOTO_MAX_BYTES:
        raise PlacePhotoError(
            "Файл больше "
            f"{PLACE_PHOTO_MAX_BYTES // (1024 * 1024)} МБ: "
            "пришлите изображение меньшего размера."
        )
    return PhotoCandidate(
        file_id=file_id,
        file_unique_id=unique,
        kind=candidate.kind,
        mime_type=candidate.mime_type,
        file_size=size if isinstance(size, int) else None,
    )


# --- чтение -----------------------------------------------------------------


def _view(row: PlacePhoto) -> PlacePhotoView:
    return PlacePhotoView(
        photo_id=int(row.id),
        file_id=row.file_id,
        file_unique_id=row.file_unique_id,
    )


def list_place_photos(session: Session, place_id: int) -> list[PlacePhotoView]:
    """
    Библиотека площадки в стабильном порядке добавления (`id`).

    Порядок не зависит от времени и не пересортировывается: номер строки на
    экране остаётся тем же между открытиями.
    """
    if not _valid_place_id(place_id):
        return []
    rows = session.scalars(
        select(PlacePhoto)
        .where(PlacePhoto.place_id == place_id)
        .order_by(PlacePhoto.id)
    ).all()
    return [_view(row) for row in rows]


def count_place_photos(session: Session, place_id: object) -> int:
    """Сколько снимков в библиотеке площадки (0 — пусто/нет площадки)."""
    return len(list_place_photos(session, place_id))  # type: ignore[arg-type]


def place_photo_ids(session: Session, place_id: object) -> tuple[str, ...]:
    """
    `file_id` библиотеки для конструктора анонса (только это место).

    Возвращаются снапшоты-строки: мастер копирует их в черновик, поэтому
    последующее удаление из библиотеки анонс не ломает.
    """
    if not _valid_place_id(place_id):
        return ()
    return tuple(view.file_id for view in list_place_photos(session, place_id))


# --- запись -----------------------------------------------------------------


def _row_for_unique(
    session: Session, place_id: int, file_unique_id: str
) -> PlacePhoto | None:
    return session.scalar(
        select(PlacePhoto).where(
            PlacePhoto.place_id == place_id,
            PlacePhoto.file_unique_id == file_unique_id,
        )
    )


def add_place_photos(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    candidates: Iterable[PhotoCandidate],
) -> PlacePhotoBatch:
    """
    Принимает партию снимков (одно фото или альбом) в фототеку площадки.

    Каждый кандидат проверяется отдельно: один не-образ отклоняется, остальные
    из партии принимаются. Дубль по `file_unique_id` — тихий пропуск
    (`duplicate`), второй строки нет. Ошибка БД откатывает ВСЮ партию и
    возвращает `conflict`, а не оставляет половину записей.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    place = _require_managed_place(session, actor, place_id)
    accepted: list[str] = []
    duplicate: list[str] = []
    rejected: list[PhotoRejection] = []
    pending_unique: set[str] = set()
    for candidate in candidates:
        try:
            checked = validate_candidate(candidate)
        except PlacePhotoError as exc:
            rejected.append(
                PhotoRejection(
                    file_id=str(getattr(candidate, "file_id", "")), reason=str(exc)
                )
            )
            continue
        if checked.file_unique_id in pending_unique or _row_for_unique(
            session, place.id, checked.file_unique_id
        ):
            duplicate.append(checked.file_id)
            continue
        pending_unique.add(checked.file_unique_id)
        session.add(
            PlacePhoto(
                place_id=place.id,
                file_id=checked.file_id,
                file_unique_id=checked.file_unique_id,
                added_at=utcnow(),
            )
        )
        accepted.append(checked.file_id)
    if accepted:
        try:
            session.commit()
        except IntegrityError:
            # Гонка двух окон: уникальный индекс сработал — партия не
            # сохраняется целиком, повторная отправка покажет дубли.
            session.rollback()
            return PlacePhotoBatch(
                photos=tuple(list_place_photos(session, place.id)),
                duplicate=tuple(accepted),
                rejected=tuple(rejected),
            )
        except OperationalError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
    return PlacePhotoBatch(
        photos=tuple(list_place_photos(session, place.id)),
        accepted=tuple(accepted),
        duplicate=tuple(duplicate),
        rejected=tuple(rejected),
    )


def delete_place_photo(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    photo_id: int,
) -> PlacePhotoMutation:
    """
    Удаляет снимок из библиотеки площадки.

    Чужой или уже удалённый id — `not_found` без изменения данных: подделанный
    callback не удаляет строку другой площадки.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    place = _require_managed_place(session, actor, place_id)
    if not _valid_photo_id(photo_id):
        return PlacePhotoMutation("not_found", "Снимок уже удалён.")
    row = session.scalar(
        select(PlacePhoto).where(
            PlacePhoto.id == photo_id,
            PlacePhoto.place_id == place.id,
        )
    )
    if row is None:
        return PlacePhotoMutation("not_found", "Снимок уже удалён.")
    try:
        session.execute(delete(PlacePhoto).where(PlacePhoto.id == row.id))
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return PlacePhotoMutation(
            "conflict",
            "Не удалось удалить снимок из-за ошибки базы. Повторите попытку.",
        )
    except Exception:
        session.rollback()
        raise
    return PlacePhotoMutation("deleted", "Снимок удалён из фототеки площадки.")


def place_photo_view(
    session: Session, place_id: Any, photo_id: Any
) -> PlacePhotoView | None:
    """Строка библиотеки этой площадки; `None` — чужой/несуществующий id."""
    if not _valid_place_id(place_id) or not _valid_photo_id(photo_id):
        return None
    row = session.scalar(
        select(PlacePhoto).where(
            PlacePhoto.id == photo_id,
            PlacePhoto.place_id == place_id,
        )
    )
    return None if row is None else _view(row)


__all__ = [
    "PLACE_PHOTO_MAX_BYTES",
    "PLACE_PHOTO_MAX_ID_LENGTH",
    "PLACE_PHOTO_PAGE_SIZE",
    "PhotoCandidate",
    "PhotoRejection",
    "PlacePhotoBatch",
    "PlacePhotoError",
    "PlacePhotoMutation",
    "PlacePhotoNotFoundError",
    "PlacePhotoPermissionError",
    "PlacePhotoView",
    "add_place_photos",
    "count_place_photos",
    "delete_place_photo",
    "list_place_photos",
    "place_photo_ids",
    "place_photo_view",
    "validate_candidate",
]
