# handlers/place_invite_screen.py
"""
Экран «🔗 Ссылка для создания анонса» (Poster 2.2 U4b → 2.4.1 T2).

Экран один на всё приложение: он открывается из полной административной
карточки места (`handlers.admin`, фаза `invite`) и показывает действующую
ссылку площадки. Формат ссылки и права не меняются: токен выдаёт
`place_invite_store.ensure_invite`, адрес — `utils.deep_links.deep_link`.

Вынесено в отдельный модуль, чтобы справочник площадок и кабинет не
импортировали друг друга и не дублировали генерацию ссылки.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from place_store import PlaceDetails
from utils.deep_links import deep_link
from utils.ui_texts import tr

logger = logging.getLogger(__name__)


async def invite_content(
    context: ContextTypes.DEFAULT_TYPE,
    session: Session,
    details: PlaceDetails,
    *,
    telegram_user_id: int,
    regenerated: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Текст и клавиатура экрана ссылки на создание анонса площадки.

    Ссылка показывается текстом; превью ссылки отключает сам экран
    (`render_screen` передаёт `LinkPreviewOptions(is_disabled=True)`).
    Перевыпуск отзывает прежний токен — об этом говорит плашка, новая ссылка
    уже в тексте.
    """
    lines: list[str] = []
    if regenerated:
        lines.append(tr("places.invite.regenerated"))
        lines.append("")
    lines.append(tr("places.invite.title", place=details.canonical_name))
    lines.append("")

    from place_invite_store import PlaceInviteError, ensure_invite

    token: str | None = None
    try:
        token = ensure_invite(
            session,
            actor_telegram_user_id=telegram_user_id,
            place_id=details.place_id,
        ).token
    except PlaceInviteError as exc:
        logger.info("Ссылка для площадки %s не выдана: %s", details.place_id, exc)
    link = await deep_link(context, token) if token else None
    lines.append(
        tr("places.invite.link", link=link) if link else tr("places.invite.no_link")
    )
    rows = [
        [
            InlineKeyboardButton(
                tr("buttons.places.invite_regenerate"),
                callback_data=f"adm_invite_regen:{details.place_id}",
            )
        ],
        [
            InlineKeyboardButton(
                tr("buttons.bricks.k_kartochke"),
                callback_data="adm_invite_back",
            )
        ],
        [InlineKeyboardButton(tr("buttons.nav.main_menu"), callback_data="main_menu")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)
