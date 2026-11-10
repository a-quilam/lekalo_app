# handlers/place_invite_screen.py
"""
Экран «🔗 Ссылка для создания анонса» (Poster 2.2 U4b → 2.6.1 §8).

Экран один на всё приложение: он открывается из полной административной
карточки места (`handlers.admin`, фаза `invite`) и показывает действующую
ссылку площадки. Формат ссылки и права не меняются: токен выдаёт
`place_invite_store.ensure_invite`, адрес — `utils.deep_links.deep_link`.

Poster 2.6.1 §8: кнопка «📋 Скопировать» — настоящая `CopyTextButton`
Telegram (ссылка сразу уходит в буфер обмена, без нового сообщения и без
перевыпуска). Пользовательского перевыпуска ссылки на экране больше нет:
`place_invite_store.regenerate_invite` остаётся только внутренним API.

Вынесено в отдельный модуль, чтобы справочник площадок и кабинет не
импортировали друг друга и не дублировали генерацию ссылки.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import InlineKeyboardButtonLimit
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
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Текст и клавиатура экрана ссылки на создание анонса площадки.

    Ссылка показывается отдельным абзацем обычным текстом (кликабельность
    даёт автолинк Telegram); превью ссылки отключает сам экран
    (`render_screen` передаёт `LinkPreviewOptions(is_disabled=True)`).
    """
    blocks: list[str] = [tr("places.invite.title", place=details.canonical_name)]

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
    if link:
        blocks.append(tr("places.invite.instruction"))
        blocks.append(tr("places.invite.link", link=link))
    else:
        blocks.append(tr("places.invite.no_link"))

    rows: list[list[InlineKeyboardButton]] = []
    # §8.3: CopyTextButton принимает 1–256 символов. Ссылка короче в разы
    # (username бота + 22-символьный payload), но при неожиданно длинном
    # username кнопка не показывается целиком — обрезать URL нельзя.
    if link and len(link) <= InlineKeyboardButtonLimit.MAX_COPY_TEXT:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.places.invite_copy"),
                    copy_text=CopyTextButton(text=link),
                )
            ]
        )
    elif link:
        logger.warning(
            "Ссылка площадки %s длиннее %s символов — кнопка копирования скрыта",
            details.place_id,
            InlineKeyboardButtonLimit.MAX_COPY_TEXT,
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.bricks.k_kartochke"),
                callback_data="adm_invite_back",
            )
        ]
    )
    return "\n\n".join(blocks), InlineKeyboardMarkup(rows)
