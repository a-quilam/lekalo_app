# handlers/admin.py
"""
Административный кабинет Stage 20A: роли, места и назначение администраторов.

Безопасность:
- актор всегда берётся из `update.effective_user.id`, а не из callback;
- суперадминистратор проверяется по `SUPERADMIN_USER_IDS` при каждом действии;
- администратор места проверяется по актуальной строке `place_admins`;
- callback передаёт только id места/получателя, но не подтверждает роль;
- назначение/замена/отзыв дополнительно проверяют права и ожидаемое состояние
  в слое `place_admin_store`, а инвариант «не более одного админа» обеспечивает
  частичный уникальный индекс SQLite;
- состояние `admin_sessions` хранит только навигацию и является подсказкой,
  а не источником полномочий.

Текстовый ввод принимается только в явных фазах кабинета: поиск получателя
назначения (Stage 20A) и поля редактирования места (Stage 20B). Обработчик
зарегистрирован после мастера поста и «Мест», поэтому не перехватывает их
поля; при входе в кабинет сценарий «Места» очищается.
"""

from __future__ import annotations

import logging
from datetime import datetime
from html import escape as html_escape
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import report_store
from admin_state import (
    clear_admin_session,
    load_admin_session,
    save_admin_session,
)
from admin_transfer_store import (
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    TransferUser,
    TransferView,
    approve_transfer_recipient,
    cancel_admin_transfer,
    confirm_admin_transfer,
    create_admin_transfer,
    find_known_recipients,
    get_pending_transfer_for_place,
    get_transfer_view,
    mark_transfer_notified,
    normalize_transfer_username,
    open_transfer_invitation,
    reject_admin_transfer,
    transfer_status_label,
)
from brick_proposal_store import (
    PROPOSAL_SCOPE_SYSTEM,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_REJECTED,
    PROPOSAL_STATUS_WITHDRAWN,
    BrickProposalSnapshot,
    ProposalActorError,
    ProposalPermissionError,
    approve_brick_proposal,
    count_pending_for_place,
    count_pending_place_proposals,
    count_pending_system_proposals,
    get_admin_proposal,
    list_pending_for_place,
    list_pending_place_proposals,
    list_pending_system_proposals,
    reject_brick_proposal,
)
from brick_state import clear_brick_session
from brick_store import (
    BrickConflictError,
    BrickConflictView,
    BrickNotFoundError,
    BrickPermissionError,
    ConflictResolutionResult,
    count_open_conflicts_for_place,
    get_conflict,
    get_place_name_for_display,
    list_pending_conflict_notifications,
    list_place_bricks,
    list_place_conflicts,
    list_system_bricks,
    mark_conflict_notification,
    resolve_conflict_by_rename,
    resolve_conflict_by_switch,
    update_brick_description,
)
from brick_types import (
    BRICK_SCOPE_PLACE,
    BRICK_SCOPE_SYSTEM,
    BRICK_STATUS_ACTIVE,
    BRICK_TYPE_CHOICE,
    BRICK_TYPE_NUMBER,
    BRICK_TYPE_TITLES,
    brick_title_display,
)
from database import SessionLocal
from handlers.catalogs import (
    KIND_CURATOR,
    KIND_FOOTER,
    open_callback,
    place_catalog_visibility,
)
from handlers.place_invite_screen import invite_content
from models import Brick, User
from permissions import (
    can_access_admin_panel,
    can_manage_place,
    has_any_place_admin_role,
    is_place_admin,
    is_superadmin,
)
from place_admin_store import (
    AdminMutationResult,
    AdminPermissionError,
    AdminUser,
    UserSearchError,
    assign_place_admin,
    get_active_place_admin,
    list_places_for_administration,
    replace_place_admin,
    revoke_place_admin,
    search_known_users,
)
from place_edit_store import (
    AliasRow,
    EditMutationResult,
    PlaceEditPermissionError,
    PlaceEditValueError,
    add_place_alias,
    clean_address_text,
    delete_place_address,
    delete_place_alias,
    delete_place_url,
    list_place_aliases,
    normalize_place_url,
    rename_canonical_place_name,
    rename_place_alias,
    set_place_address,
    set_place_city,
    set_place_url,
)
from place_state import clear_place_session
from place_store import (
    PlaceDetails,
    PlaceNameError,
    find_name_conflicts,
    get_place_details,
    normalize_place_name,
    validate_place_city,
    validate_place_name,
)
from template_state import clear_template_session
from user_store import get_or_create_user
from utils import current_value, pagination, screen
from utils import tg_context as ctx
from utils.tg_html import split_html, utf16_len
from utils.ui_texts import join_blocks, tr, tr_plain, tr_safe
from utils.user_events import event_log

logger = logging.getLogger(__name__)

BUTTON_TEXT_LIMIT = 60
# Poster 2.5 §14: единый размер страницы для всех длинных списков.
ADMIN_PLACES_PAGE_SIZE = pagination.PAGE_SIZE
PROPOSAL_PAGE_SIZE = pagination.PAGE_SIZE
STALE_TEXT = tr("errors.stale.admin")
DENIED_TEXT = tr("admin.module.razdel_dostupen_tolko")
DENIED_ALERT = tr("admin.module.dostup_administrativnomu_razdelu")

PHASES = {
    "places",
    "place",
    "assign_search",
    "assign_results",
    "confirm_assign",
    "confirm_replace",
    "confirm_revoke",
    "edit_menu",
    "edit_name_input",
    "edit_name_confirm",
    "edit_aliases",
    "edit_alias_add_input",
    "edit_alias_add_confirm",
    "edit_alias_rename_input",
    "edit_alias_rename_confirm",
    "edit_alias_delete_confirm",
    "edit_address_input",
    "edit_address_confirm",
    "edit_address_delete_confirm",
    "edit_city_input",
    "edit_city_confirm",
    "edit_url_input",
    "edit_url_confirm",
    "edit_url_delete_confirm",
    "transfer_username_input",
    "transfer_candidates",
    "transfer_confirm_known",
    "transfer_confirm_invite",
    "transfer_outgoing",
    "proposal_global_list",
    "proposal_places_list",
    "proposal_card",
    "proposal_approve_confirm",
    "proposal_reject_confirm",
    "conflicts_list",
    "conflict_card",
    "conflict_rename_input",
    "conflict_switch_confirm",
    # Poster 2.4 P1: административный редактор ОПИСАНИЯ кирпичика.
    # Метаданные (название/код/тип/область) здесь не меняются.
    "brick_list",
    "brick_card",
    "brick_description_input",
    # Poster 2.4.1 T2: ссылка для создания анонса — экран полной карточки.
    "invite",
    # Poster 2.5 A3: служебный подраздел управления администратором места.
    "place_admins",
    # Poster 2.5.5 D: раздел «Репорты» — список, карточка, ответ, архив.
    "reports_list",
    "report_card",
    "report_reply_input",
    "report_reply_preview",
    "reports_archive",
    "report_archive_card",
}

# Фазы раздела «Репорты»: доступны только суперадминистраторам.
REPORTS_PHASES = frozenset(
    {
        "reports_list",
        "report_card",
        "report_reply_input",
        "report_reply_preview",
        "reports_archive",
        "report_archive_card",
    }
)

# Предел ответа разработчика: сообщение пользователю добавляет к ответу
# префикс каталога, поэтому 3500 символов оставляют запас до лимита Telegram
# (4096). Форма репорта использует такой же предел (`handlers/report.py`).
MAX_REPORT_REPLY = 3500

# Poster 2.5 A3: действия администратора доступны и с карточки места, и из
# подраздела «Управление администратором» — это один и тот же механизм.
PLACE_ACTION_PHASES = frozenset({"place", "place_admins"})

_SUPER_ONLY_PHASES = {
    "assign_search",
    "assign_results",
    "confirm_assign",
    "confirm_replace",
    "confirm_revoke",
    "proposal_global_list",
} | REPORTS_PHASES
SUPER_ONLY_PHASES = _SUPER_ONLY_PHASES


def _h(value: object) -> str:
    return html_escape(str(value))


def _short(value: object, limit: int = 100) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


# Экран Telegram ограничен 4096 символами. Длинное значение хранится
# полностью; на экране показывается начало с ЯВНОЙ пометкой, а не молчаливое
# «…» (H3: единый компонент «Текущее значение»).
#
# Бюджет значения здесь не константа: он считается от фактической обвязки
# экрана общим механизмом `utils.current_value` (`value_limits`/`fit_screen`),
# чтобы два длинных поля (адрес и ссылка, «текущее» и «новое») делили остаток
# лимита и не выносили сообщение за 4096.

# U1b: личные уведомления не разворачивают превью ссылок (PTB 22.8:
# `LinkPreviewOptions` вместо устаревшего `disable_web_page_preview`).
# Отдельный экземпляр (не тот, что у экрана): по идентичности объекта
# тестовый каркас отличает уведомление от экрана управления, хотя для
# Telegram значения равнозначны.
NOTIFICATION_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _long_value(value: object, *, limit: int | None = None) -> str:
    """
    HTML-безопасное значение без косметической обрезки (H3, требование D).

    Раньше адрес резался до 1500 символов «для красоты», и автор не видел
    полный текст. Теперь ограничение только физическое — предел сообщения
    Telegram — и о нём сказано явно; в базе значение всегда полное.
    """
    return current_value.clipped_text(
        value, limit=current_value.VALUE_LIMIT if limit is None else limit
    )


# Ссылка остаётся кликабельной, пока сама укладывается в бюджет экрана.
# Реальные ссылки короче сотни символов; экстремально длинный URL (например,
# подписанная ссылка на несколько тысяч символов) раньше попадал в экран
# ДВАЖДЫ — в href и в подписи — и раздувал сообщение далеко за лимит
# Telegram, так что экран вообще не отправлялся.
LINK_HREF_LIMIT = 1200


def _place_value_limits(build, blocks: int) -> tuple[list[int], str]:
    """
    Бюджет значений места, посчитанный от фактической обвязки экрана.

    H3 (требование D): единый механизм `utils.current_value`. Экран сначала
    собирается с пустыми значениями (`limit <= 0`) и измеряется, затем остаток
    лимита Telegram делится между длинными полями. Так два «полных» значения
    по отдельности корректны, но вместе не выносят сообщение за 4096, и у
    каждого сокращённого есть честная пометка.

    Возвращает лимиты значений и уже собранный текст экрана.
    """
    count = max(int(blocks), 1)
    limits = current_value.value_limits(build([0] * count), count)
    return limits, current_value.clip_screen(build(limits))


def _safe_link(url: str | None, *, limit: int | None = None) -> str:
    """
    Ссылка — только http(s); иначе показываем как обычный текст.

    H3 (требование D): ограничение физическое, а не косметическое. Пока URL
    короткий, он кликабелен; если не влезает в экран, показывается начало с
    явной пометкой, что полное значение сохранено (utils/current_value), а не
    обрезанный href — битая ссылка хуже, чем честный текст.

    Отсутствующая ссылка показывается текстом «Не указана».
    """
    if not url:
        return tr("admin.safe_link.ne_ukazana")
    if url.lower().startswith(("http://", "https://")) and len(url) <= LINK_HREF_LIMIT:
        return f'<a href="{_h(url)}">{_h(url)}</a>'
    return current_value.clipped_text(
        url, limit=current_value.VALUE_LIMIT if limit is None else limit
    )


def _button_name(prefix: str, text: str) -> str:
    available = max(BUTTON_TEXT_LIMIT - len(prefix), 1)
    if len(text) <= available:
        return prefix + text
    return prefix + text[: max(available - 1, 1)] + "…"


def _user_label(user: AdminUser, *, with_id: bool = False) -> str:
    if user.full_name:
        base = user.full_name
        if user.username:
            base += f" (@{user.username})"
    elif user.username:
        base = f"@{user.username}"
    else:
        base = f"ID {user.telegram_user_id}"
    if with_id:
        base += tr(
            "admin.user_label.postoyannyy_telegram_user_id",
            telegram_user_id=user.telegram_user_id,
        )
    return base


def _date_label(value: datetime | None) -> str:
    if value is None:
        return "—"
    try:
        return value.strftime("%d.%m.%Y %H:%M UTC")
    except (AttributeError, ValueError):  # pragma: no cover — неожиданный тип
        return "—"


def _transfer_user_label(user: TransferUser, *, with_id: bool = False) -> str:
    """Подпись получателя в экранах передачи (username — не идентификатор)."""
    if user.full_name:
        base = user.full_name
        if user.username:
            base += f" (@{user.username})"
    elif user.username:
        base = f"@{user.username}"
    else:
        base = f"ID {user.telegram_user_id}"
    if with_id:
        base += tr(
            "admin.user_label.postoyannyy_telegram_user_id",
            telegram_user_id=user.telegram_user_id,
        )
    return base


def _transfer_username_content(
    details: PlaceDetails, active_admin: AdminUser | None
) -> tuple[str, InlineKeyboardMarkup]:
    initiator = (
        _h(_user_label(active_admin, with_id=True))
        if active_admin is not None
        else tr("admin.transfer_username.tekuschiy_administrator")
    )
    text = tr_safe(
        "admin.transfer_username.peredacha_administracii_mesta",
        canonical_name=_h(details.canonical_name),
        initiator=initiator,
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(tr("buttons.nav.back"), callback_data="adm_cancel")]]
    )
    return text, keyboard


def _transfer_candidates_content(
    details: PlaceDetails, candidates: tuple[TransferUser, ...], state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    username = _h(str(state.get("transfer_username") or ""))
    text = tr_safe(
        "admin.transfer_candidates.naydeno_neskolko_izvestnyh",
        canonical_name=_h(details.canonical_name),
        username=username,
    )
    rows: list[list[InlineKeyboardButton]] = []
    for index, user in enumerate(candidates, start=1):
        text += (
            f"\n{index}. {_h(_short(_transfer_user_label(user), 100))}\n"
            f"   Telegram user ID: {user.telegram_user_id}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    _button_name(f"{index}. ", _transfer_user_label(user)),
                    callback_data=f"adm_transfer_pick:{user.telegram_user_id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="adm_cancel")]
    )
    return text, InlineKeyboardMarkup(rows)


def _transfer_known_confirm_content(
    session: Session, details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup] | None:
    target_tg = state.get("transfer_target_telegram_user_id")
    if not isinstance(target_tg, int):
        return None
    target_row = session.scalar(select(User).where(User.telegram_user_id == target_tg))
    if target_row is None:
        return None
    target = TransferUser(
        user_id=target_row.id,
        telegram_user_id=target_row.telegram_user_id,
        username=target_row.username,
        full_name=target_row.full_name,
    )
    active = get_active_place_admin(session, details.place_id)
    text = tr_safe(
        "admin.transfer_known_confirm.podtverdite_sozdanie_predlozheniy",
        canonical_name=_h(details.canonical_name),
        user_label=_h(_user_label(active.user, with_id=True)) if active else "—",
        transfer_user_label=_h(_transfer_user_label(target, with_id=True)),
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.sozdat_predlozhenie"),
                    callback_data="adm_transfer_confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.otmena"), callback_data="adm_cancel"
                )
            ],
        ]
    )


def _transfer_invite_confirm_content(
    details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    username = _h(str(state.get("transfer_username") or ""))
    text = tr_safe(
        "admin.transfer_invite_confirm.poluchatel_poka_izvesten_botu",
        canonical_name=_h(details.canonical_name),
        username=username,
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.sozdat_ssylku_priglashenie"),
                    callback_data="adm_transfer_confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.otmena"), callback_data="adm_cancel"
                )
            ],
        ]
    )


def _transfer_expiry_label(view: TransferView) -> str:
    return _date_label(view.expires_at)


def _transfer_outgoing_content(
    session: Session,
    details: PlaceDetails,
    state: dict,
    *,
    invite_url: str | None = None,
) -> tuple[str, InlineKeyboardMarkup] | None:
    view = get_pending_transfer_for_place(session, details.place_id)
    if view is None:
        return None

    text = tr_safe(
        "admin.transfer_outgoing.peredacha_administracii_status",
        canonical_name=_h(details.canonical_name),
        transfer_status_label=_h(transfer_status_label(view.status)),
        transfer_expiry_label=_h(_transfer_expiry_label(view)),
        transfer_user_label=_h(_transfer_user_label(view.from_user)),
    )
    if view.to_user is not None:
        text += tr_safe(
            "admin.transfer_outgoing.poluchatel",
            transfer_user_label=_h(_transfer_user_label(view.to_user, with_id=True)),
        )
    else:
        text += tr_safe(
            "admin.transfer_outgoing.username_priglashenii",
            h=_h(view.to_username or "—"),
        )
        if view.opened_by is not None:
            text += tr_safe(
                "admin.transfer_outgoing.ssylku_otkryl_peredacha_prav",
                transfer_user_label=_h(
                    _transfer_user_label(view.opened_by, with_id=True)
                ),
            )
        else:
            text += tr("admin.transfer_outgoing.ssylku_poka_nikto_otkryl")

    if invite_url:
        text += tr_safe(
            "admin.transfer_outgoing.odnorazovaya_ssylka_priglashenie",
            invite_url=_safe_link(invite_url),
        )
    elif view.has_invite_token and view.to_user is None:
        text += tr("admin.transfer_outgoing.ssylka_priglashenie_byla_pokazana")

    if view.awaiting_recipient:
        text += tr("admin.transfer_outgoing.poluchatel_izvesten_no_podtverdil")
        if view.notified_at is None:
            text += tr("admin.transfer_outgoing.dostavka_uvedomleniya_poluchatelyu")
    elif view.awaiting_initiator_approval:
        text += tr("admin.transfer_outgoing.ozhidaetsya_vashe_utverzhdenie")
    else:
        text += tr("admin.transfer_outgoing.prava_ostayutsya_u_podtverzhdeniya")

    rows: list[list[InlineKeyboardButton]] = []
    if view.awaiting_initiator_approval:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.admin.utverdit_poluchatelya"),
                    callback_data=f"adm_transfer_approve:{view.transfer_id}",
                )
            ]
        )
    if view.awaiting_recipient:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.admin.uvedomit_poluchatelya_raz"),
                    callback_data=f"adm_transfer_notify:{view.transfer_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.obnovit_status"),
                callback_data=f"adm_transfer_refresh:{details.place_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.otmenit_peredachu"),
                callback_data=f"adm_transfer_cancel:{view.transfer_id}",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="adm_cancel")]
    )
    return text, InlineKeyboardMarkup(rows)


def _transfer_offer_text(view: TransferView) -> str:
    return tr_safe(
        "admin.transfer_offer.vam_predlagayut_administrirovanie",
        place_name=_h(view.place_name),
        transfer_user_label=_h(_transfer_user_label(view.from_user, with_id=True)),
        transfer_expiry_label=_h(_transfer_expiry_label(view)),
    )


def _transfer_offer_keyboard(view: TransferView) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.podtverzhdayu"),
                    callback_data=f"adm_transfer_accept:{view.transfer_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.otklonit"),
                    callback_data=f"adm_transfer_decline:{view.transfer_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
                )
            ],
        ]
    )


def _incoming_transfer_content(
    status: str, view: TransferView | None, viewer_telegram_user_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран получателя после открытия ссылки или нажатия кнопки."""
    menu = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
                )
            ]
        ]
    )
    if view is None or status == "invalid_invite":
        return (
            tr("admin.incoming_transfer.ssylka_priglashenie_nedeystvitelna"),
            menu,
        )
    if status == "self":
        return (
            tr("admin.incoming_transfer.eto_sobstvennaya_ssylka"),
            menu,
        )
    if status == "opened_by_other":
        return (
            tr("admin.incoming_transfer.ssylka_otkryta_drugim_akkauntom"),
            menu,
        )
    if status == "not_recipient":
        return (
            tr("admin.incoming_transfer.predlozhenie_adresovano_drugomu"),
            menu,
        )
    if status == "approved" or (
        view.status == STATUS_PENDING
        and view.to_user is not None
        and view.to_user.telegram_user_id == viewer_telegram_user_id
    ):
        return _transfer_offer_text(view), _transfer_offer_keyboard(view)
    if view.status == STATUS_PENDING and view.opened_by is not None:
        if view.opened_by.telegram_user_id == viewer_telegram_user_id:
            return (
                tr_safe(
                    "admin.incoming_transfer.priglashenie_otkryto_peredacha",
                    place_name=_h(view.place_name),
                    transfer_user_label=_h(
                        _transfer_user_label(view.from_user, with_id=True)
                    ),
                ),
                menu,
            )
        return (
            tr("admin.incoming_transfer.ssylka_otkryta_drugim_akkauntom_2"),
            menu,
        )
    if view.status == STATUS_CONFIRMED:
        return (
            tr_safe(
                "admin.incoming_transfer.peredacha_zavershena_predlozhenie",
                place_name=_h(view.place_name),
            ),
            menu,
        )
    if view.status == STATUS_EXPIRED:
        return (
            tr_safe(
                "admin.incoming_transfer.srok_priglasheniya_istek_peredacha",
                place_name=_h(view.place_name),
            ),
            menu,
        )
    if view.status == STATUS_CANCELLED:
        return (
            tr_safe(
                "admin.incoming_transfer.peredacha_otmenena_iniciatorom",
                place_name=_h(view.place_name),
            ),
            menu,
        )
    if view.status == STATUS_REJECTED:
        return (
            tr_safe(
                "admin.incoming_transfer.predlozhenie_otkloneno",
                place_name=_h(view.place_name),
            ),
            menu,
        )
    if view.status == STATUS_SUPERSEDED:
        return (
            tr_safe(
                "admin.incoming_transfer.predlozhenie_nedeystvitelno",
                place_name=_h(view.place_name),
            ),
            menu,
        )
    return (
        tr_safe(
            "admin.incoming_transfer.predlozhenie_peredachi_seychas",
            place_name=_h(view.place_name),
        ),
        menu,
    )


async def _deep_link(context: ContextTypes.DEFAULT_TYPE, payload: str) -> str | None:
    """Ссылка t.me/<bot>?start=...; username бота не секрет (utils.deep_links)."""
    from utils.deep_links import deep_link

    return await deep_link(context, payload)


async def _send_user_notification(
    context: ContextTypes.DEFAULT_TYPE,
    telegram_user_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> bool:
    """
    Best-effort личное уведомление. True — Telegram подтвердил доставку.

    Токен приглашения в уведомлениях не передаётся. Ошибка доставки не
    отменяет предложение и не выдаёт/не отнимает прав.
    """
    try:
        await context.bot.send_message(
            chat_id=telegram_user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            link_preview_options=NOTIFICATION_LINK_PREVIEW,
        )
        return True
    except Exception as e:  # noqa: BLE001 — сбой Telegram не ломает операцию
        logger.warning(
            "Не удалось отправить уведомление пользователю %s: %s",
            telegram_user_id,
            e,
        )
        return False


async def _notify_recipient(
    context: ContextTypes.DEFAULT_TYPE, view: TransferView
) -> bool:
    if view.to_user is None:
        return False
    return await _send_user_notification(
        context,
        view.to_user.telegram_user_id,
        _transfer_offer_text(view),
        _transfer_offer_keyboard(view),
    )


async def _notify_initiator(
    context: ContextTypes.DEFAULT_TYPE,
    view: TransferView,
    text: str,
) -> None:
    await _send_user_notification(context, view.from_user.telegram_user_id, text)


async def _edit_notification_message(
    query: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None = None
) -> None:
    """Обновляет сообщение, на кнопке которого нажали; при сбое — без падения."""
    try:
        await query.edit_message_text(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            link_preview_options=NOTIFICATION_LINK_PREVIEW,
        )
    except Exception as e:  # noqa: BLE001 — устаревшее сообщение не должно падать
        logger.info("Не удалось обновить сообщение передачи: %s", e)


def _new_state() -> dict:
    return {"phase": "places", "page": 0}


def _page_value(state: dict, key: str = "page") -> int:
    try:
        value = int(state.get(key) or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _clamp_offset(offset: int, total: int, page_size: int) -> int:
    if total <= 0:
        return 0
    max_offset = ((total - 1) // page_size) * page_size
    return min(max(offset, 0), max_offset)


def _failed_add(state: dict, update: Update) -> None:
    message = getattr(update, "message", None)
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return
    ids = state.setdefault("failed_inputs", [])
    if message_id not in ids:
        ids.append(int(message_id))


def _failed_take(state: dict) -> list[int]:
    raw = state.pop("failed_inputs", None) or []
    out: list[int] = []
    for value in raw:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def _ensure_user(session: Session, update: Update) -> None:
    telegram_user = ctx.user(update)
    get_or_create_user(
        session,
        telegram_user.id,
        username=getattr(telegram_user, "username", None),
        full_name=getattr(telegram_user, "full_name", None),
    )


def _access_kind(session: Session, telegram_user_id: int) -> str | None:
    if is_superadmin(telegram_user_id):
        return "superadmin"
    if has_any_place_admin_role(session, telegram_user_id):
        return "place_admin"
    return None


async def _show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    text: str,
    keyboard: InlineKeyboardMarkup,
    *,
    notice: str | None = None,
    cleanup_ids: list[int] | None = None,
    drop_input: bool = False,
) -> bool:
    """Сохраняет состояние, рисует экран и только потом чистит ввод."""
    user_id = ctx.user(update).id
    body = f"{notice}\n\n{text}" if notice else text
    saved = save_admin_session(user_id, state)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(
        context, user_data, chat_id, body, keyboard, user_id=user_id
    )
    screen.save_screen_ref(user_id, user_data, chat_id)
    if saved:
        if cleanup_ids:
            await screen.cleanup_messages(
                context.bot, chat_id, sorted(set(cleanup_ids))
            )
        if drop_input:
            await screen.drop_user_input(update, context)
    return saved


async def _render_denied(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Закрывает кабинет; выдаёт только главное меню, без административных кнопок."""
    user_id = ctx.user(update).id
    clear_admin_session(user_id)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    await screen.render_screen(
        context,
        user_data,
        ctx.chat(update).id,
        DENIED_TEXT,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
                    )
                ]
            ]
        ),
        user_id=user_id,
    )
    screen.save_screen_ref(user_id, user_data, ctx.chat(update).id)


def _brick_scope_label(scope: str, place_name: str | None) -> str:
    """Человекочитаемая область кирпичика для административной карточки."""
    if scope == BRICK_SCOPE_SYSTEM:
        return tr("admin.brick_card.oblast_sistema")
    if scope == BRICK_SCOPE_PLACE:
        return tr(
            "admin.brick_card.oblast_mesto",
            place=place_name or tr("admin.brick_card.mesto_bez_nazvaniya"),
        )
    return tr("admin.brick_card.oblast_lichno")


def _brick_status_label(status: str) -> str:
    return (
        tr("admin.brick_card.status_aktivnyy")
        if status == BRICK_STATUS_ACTIVE
        else tr("admin.brick_card.status_arxiv")
    )


def _brick_description_block(value: object) -> str:
    """Описание в административной карточке: экранировано, «—» при отсутствии."""
    text = " ".join(str(value or "").split())
    if not text:
        return tr("admin.brick_card.opisaniya_net")
    return f"<code>{_h(text)}</code>"


def _brick_card_content(
    brick: Brick,
    *,
    place_name: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Карточка СУЩЕСТВУЮЩЕГО кирпичика в административном кабинете.

    Poster 2.4 P1: здесь только просмотр метаданных и одна правка — описание.
    Кнопок «изменить название/код/тип» нет и не должно быть: эти поля
    задаются один раз (созданием, предложением или автором личного
    кирпичика), а администратор меняет лишь инструкцию автору анонса.
    """
    text = tr_safe(
        "admin.brick_card.kartochka_kirpichika_html",
        title=_h(brick_title_display(brick.title, brick.code)),
        code=_h(brick.code),
        type_title=_h(BRICK_TYPE_TITLES.get(brick.value_type, brick.value_type)),
        scope_label=_h(_brick_scope_label(brick.scope, place_name)),
        status_label=_h(_brick_status_label(brick.status)),
        description_block=_brick_description_block(brick.description),
    )
    rows: list[list[InlineKeyboardButton]] = []
    if brick.status == BRICK_STATUS_ACTIVE:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.admin.opisanie"),
                    callback_data=f"adm_bedit_description:{brick.id}",
                )
            ]
        )
    back_data = "adm_bedit_back"
    rows.append([InlineKeyboardButton(tr("buttons.nav.back"), callback_data=back_data)])
    return text, InlineKeyboardMarkup(rows)


def _brick_description_input_content(
    brick: Brick,
) -> tuple[str, InlineKeyboardMarkup]:
    """Фаза ввода нового описания: запись — только после отправки текста."""
    text = tr_safe(
        "admin.brick_description_input.opisanie_kirpichika_html",
        code=_h(brick.code),
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.otmena"),
                    callback_data=f"adm_bedit:{brick.id}",
                )
            ]
        ]
    )


def _brick_list_content(
    session: Session,
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Список кирпичиков административного кабинета (Poster 2.4 P1).

    Суперадминистратор видит системный каталог, администратор места — кирпичики
    своей площадки. Открывается только карточка описания: метаданные здесь не
    меняются, поэтому и кнопок правки названия/кода/типа нет.
    """
    scope = state.get("brick_scope")
    place_id = state.get("brick_place_id")
    if (
        scope == "place"
        and isinstance(place_id, int)
        and not isinstance(place_id, bool)
    ):
        place_name = get_place_name_for_display(session, place_id)
        scope_line = tr(
            "admin.brick_list.mesto_line",
            place=place_name or tr("admin.brick_card.mesto_bez_nazvaniya"),
        )
        bricks = list_place_bricks(session, place_id, include_archived=True)
    else:
        scope = "system"
        scope_line = tr("admin.brick_list.sistema_line")
        bricks = list_system_bricks(session, include_archived=True)
    state["brick_scope"] = scope

    # Poster 2.5 §14: системный каталог и кирпичики площадки — длинные
    # списки, пять записей на страницу. Смещение приводится к допустимому.
    offset = _clamp_offset(
        _page_value(state, "brick_page"), len(bricks), pagination.PAGE_SIZE
    )
    state["brick_page"] = offset
    page_bricks = bricks[offset : offset + pagination.PAGE_SIZE]

    text = tr_safe("admin.brick_list.kirpichiki_html", scope_line=scope_line)
    rows: list[list[InlineKeyboardButton]] = []
    for brick in page_bricks:
        label = f"[{brick.code}] {brick_title_display(brick.title, brick.code)}"
        if brick.status != BRICK_STATUS_ACTIVE:
            label += " · " + tr("admin.brick_card.status_arxiv")
        rows.append(
            [
                InlineKeyboardButton(
                    _button_name("", label),
                    callback_data=f"adm_bedit:{brick.id}",
                )
            ]
        )
    if not bricks:
        text += "\n\n" + tr("admin.brick_list.net_kirpichikov")
    nav = pagination.nav_keyboard_for_offset(offset, len(bricks), "adm_bricks_page")
    if nav:
        rows.append(nav)
    place_scoped = scope == "place" and isinstance(place_id, int)
    if place_scoped:
        # Poster 2.5 A4 (§13): локальный кирпичик создаётся тем же
        # конструктором, что и личный, но сразу в области площадки.
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.admin.sozdat_kirpichik"),
                    callback_data=f"adm_brick_new:{place_id}",
                )
            ]
        )
    back_data = f"adm_place:{place_id}" if place_scoped else "adm_places"
    rows.append([InlineKeyboardButton(tr("buttons.nav.back"), callback_data=back_data)])
    return text, InlineKeyboardMarkup(rows)


def _brick_edit_allowed(
    session: Session, brick: Brick, *, access_kind: str | None, user_id: int
) -> bool:
    """
    Может ли этот административный экран менять описание именно этого кирпичика.

    Права решает ФАКТИЧЕСКАЯ строка БД, а не состояние навигации: подменённый
    `brick_id` не должен открывать чужую карточку (IDOR). Личные кирпичики в
    административном кабинете недоступны вовсе — их правит автор в «Моих
    кирпичиках».
    """
    if access_kind is None:
        return False
    if brick.scope == BRICK_SCOPE_SYSTEM:
        return access_kind == "superadmin"
    if brick.scope == BRICK_SCOPE_PLACE:
        return brick.place_id is not None and can_manage_place(
            session, user_id, brick.place_id
        )
    return False


def _brick_phase_content(
    session: Session,
    state: dict,
    phase: str,
    *,
    access_kind: str,
    user_id: int,
) -> tuple[str | None, InlineKeyboardMarkup | None]:
    """
    Экран фазы административного редактора описания кирпичика.

    Права проверяются заново на каждой отрисовке, по фактической строке БД:
    системный каталог — только суперадминистратор, кирпичики площадки — только
    тот, кто сейчас управляет этим местом.

    `(None, None)` — доступ не подтверждён; вызывающий слой возвращает
    человека в список мест с понятной плашкой.
    """
    if phase == "brick_list":
        scope = str(state.get("brick_scope") or "")
        place_id = state.get("brick_place_id")
        if scope == "place" and not (
            isinstance(place_id, int)
            and not isinstance(place_id, bool)
            and can_manage_place(session, user_id, place_id)
        ):
            return None, None
        if scope != "place" and access_kind != "superadmin":
            return None, None
        return _brick_list_content(session, state)

    brick_id = state.get("brick_id")
    brick = (
        session.get(Brick, brick_id)
        if isinstance(brick_id, int) and not isinstance(brick_id, bool)
        else None
    )
    if brick is None or not _brick_edit_allowed(
        session, brick, access_kind=access_kind, user_id=user_id
    ):
        # Кирпичик исчез, подменён или недоступен роли: возвращаем список
        # области, а не чужую карточку.
        state.pop("brick_id", None)
        if access_kind == "superadmin" and state.get("brick_scope") != "place":
            state["phase"] = "brick_list"
            state["brick_scope"] = "system"
            return _brick_list_content(session, state)
        return None, None

    place_name = (
        get_place_name_for_display(session, brick.place_id)
        if brick.place_id is not None
        else None
    )
    if phase == "brick_description_input":
        if brick.status != BRICK_STATUS_ACTIVE:
            state["phase"] = "brick_card"
            return _brick_card_content(brick, place_name=place_name)
        return _brick_description_input_content(brick)
    return _brick_card_content(brick, place_name=place_name)


def _places_content(
    session: Session,
    state: dict,
    access_kind: str,
    telegram_user_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    offset = max(_page_value(state), 0)
    items, total = list_places_for_administration(
        session,
        telegram_user_id,
        offset=offset,
        limit=ADMIN_PLACES_PAGE_SIZE,
    )
    offset = _clamp_offset(offset, total, ADMIN_PLACES_PAGE_SIZE)
    if offset != _page_value(state):
        items, total = list_places_for_administration(
            session,
            telegram_user_id,
            offset=offset,
            limit=ADMIN_PLACES_PAGE_SIZE,
        )
    state["page"] = offset

    if access_kind == "superadmin":
        heading = tr("admin.places.vse_mesta")
        intro = tr("admin.places.vyberite_mesto_chtoby_naznachit")
    else:
        heading = tr("admin.places.moi_mesta")
        intro = tr("admin.places.zdes_pokazany_tolko_mesta")

    if total == 0:
        text = f"{heading}\n\n{intro}\n\n" + (
            tr("admin.places.v_spravochnike_poka_net")
            if access_kind == "superadmin"
            else tr("admin.places.deystvuyuschih_naznacheniy_poka")
        )
        empty_rows: list[list[InlineKeyboardButton]] = []
        if access_kind == "superadmin":
            try:
                system_pending = count_pending_system_proposals(
                    session, telegram_user_id
                )
                places_pending = count_pending_place_proposals(
                    session, telegram_user_id
                )
            except ProposalPermissionError:
                system_pending = 0
                places_pending = 0
            empty_rows.append(
                [
                    InlineKeyboardButton(
                        tr(
                            "buttons.admin.globalnye_predlozheniya",
                            system_pending=system_pending,
                        ),
                        callback_data="adm_bprop_global",
                    )
                ]
            )
            empty_rows.append(
                [
                    InlineKeyboardButton(
                        tr(
                            "buttons.admin.predlozheniya_mestam",
                            places_pending=places_pending,
                        ),
                        callback_data="adm_bprop_places",
                    )
                ]
            )
            empty_rows.append(
                [
                    InlineKeyboardButton(
                        tr("buttons.admin.kirpichiki"),
                        callback_data="adm_bricks_global",
                    )
                ]
            )
            # Poster 2.5.5 D: вход в раздел «Репорты» — отдельная строка
            # со счётчиком действующих жалоб (только суперадмин).
            empty_rows.append(_reports_entry_row(session))
        empty_rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
                )
            ]
        )
        return text, InlineKeyboardMarkup(empty_rows)

    text = f"{heading} — {total}\n\n{intro}\n\n"
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(items, start=offset + 1):
        admin_line = (
            _h(_short(_user_label(item.admin), 90))
            if item.admin is not None
            else tr("admin.places.ne_naznachen")
        )
        text += tr_safe(
            "admin.places.administrator",
            index=index,
            canonical_name=_h(_short(item.canonical_name, 100)),
            admin_line=admin_line,
        )
        rows.append(
            [
                InlineKeyboardButton(
                    _button_name(f"{index}. ", item.canonical_name),
                    callback_data=f"adm_place:{item.place_id}",
                )
            ]
        )

    nav = pagination.nav_keyboard_for_offset(
        offset, total, "adm_page", page_size=ADMIN_PLACES_PAGE_SIZE
    )
    if nav:
        rows.append(nav)
    if access_kind == "superadmin":
        try:
            system_pending = count_pending_system_proposals(session, telegram_user_id)
            places_pending = count_pending_place_proposals(session, telegram_user_id)
        except ProposalPermissionError:
            system_pending = 0
            places_pending = 0
        rows.append(
            [
                InlineKeyboardButton(
                    tr(
                        "buttons.admin.globalnye_predlozheniya",
                        system_pending=system_pending,
                    ),
                    callback_data="adm_bprop_global",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    tr(
                        "buttons.admin.predlozheniya_mestam",
                        places_pending=places_pending,
                    ),
                    callback_data="adm_bprop_places",
                )
            ]
        )
        # Poster 2.4 P1: системный каталог кирпичиков — точка входа в
        # административный редактор описания.
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.admin.kirpichiki"),
                    callback_data="adm_bricks_global",
                )
            ]
        )
        # Poster 2.5.5 D: раздел «Репорты» — отдельная строка со счётчиком.
        rows.append(_reports_entry_row(session))
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
            )
        ]
    )
    return text, InlineKeyboardMarkup(rows)


def _report_author_label(report: report_store.Report) -> str:
    """@username, иначе имя, иначе ID (подпись автора в списке и карточке)."""
    return report.author_label()


def _report_item_label(report: report_store.Report) -> str:
    """
    Подпись кнопки репорта: «Раздел — автор», не длиннее лимита кнопки.

    Подпись — plain-текст (`tr_plain`): разметку Telegram в кнопке не
    разбирает, а название раздела приходит из каталога без тегов.
    """
    label = tr_plain(
        "buttons.reports.item",
        screen_label=report.screen_label,
        author_label=_report_author_label(report),
    )
    if len(label) <= BUTTON_TEXT_LIMIT:
        return label
    return label[: max(BUTTON_TEXT_LIMIT - 1, 1)] + "…"


def _reports_page(
    session: Session,
    state: dict,
    *,
    archived: bool,
) -> tuple[list[report_store.Report], int, int]:
    """
    Страница репортов и выровненное смещение.

    Хранилище само приводит номер страницы к допустимому; здесь смещение
    нужно для строки навигации, поэтому после запроса оно пересчитывается
    от общего числа — как у остальных списков кабинета.
    """
    offset_key = "reports_archive_offset" if archived else "reports_offset"
    offset = max(_page_value(state, offset_key), 0)
    page = offset // report_store.REPORTS_PAGE_SIZE
    loader = report_store.archived_reports if archived else report_store.active_reports
    items, total = loader(session, page=page)
    offset = _clamp_offset(offset, total, report_store.REPORTS_PAGE_SIZE)
    if offset != _page_value(state, offset_key):
        items, total = loader(session, page=offset // report_store.REPORTS_PAGE_SIZE)
    state[offset_key] = offset
    return items, total, offset


def _reports_list_content(
    session: Session, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    """Список действующих репортов: новые к старым, по пять на страницу."""
    items, total, offset = _reports_page(session, state, archived=False)
    heading = tr("admin.reports.title")
    if total == 0:
        text = join_blocks([heading, tr("admin.reports.empty")])
    else:
        text = join_blocks(
            [
                heading,
                tr("admin.reports.active_total", total=total),
                tr("admin.reports.intro"),
            ]
        )
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                _report_item_label(report), callback_data=f"rpts_card:{report.id}"
            )
        ]
        for report in items
    ]
    nav = pagination.nav_keyboard_for_offset(
        offset, total, "rpts_page", page_size=report_store.REPORTS_PAGE_SIZE
    )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.reports.archive"), callback_data="rpts_archive"
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="rpts_cabinet")]
    )
    return text, InlineKeyboardMarkup(rows)


def _reports_archive_content(
    session: Session, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    """Архив репортов: те же правила списка, но записи только для чтения."""
    items, total, offset = _reports_page(session, state, archived=True)
    heading = tr("admin.reports.archive_title")
    if total == 0:
        text = join_blocks([heading, tr("admin.reports.archive_empty")])
    else:
        text = join_blocks(
            [
                heading,
                tr("admin.reports.archive_total", total=total),
                tr("admin.reports.intro"),
            ]
        )
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                _report_item_label(report), callback_data=f"rpts_acard:{report.id}"
            )
        ]
        for report in items
    ]
    nav = pagination.nav_keyboard_for_offset(
        offset, total, "rpts_archive_page", page_size=report_store.REPORTS_PAGE_SIZE
    )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(tr("buttons.nav.back"), callback_data="rpts_back"),
            InlineKeyboardButton(
                tr("buttons.reports.cabinet"), callback_data="rpts_cabinet"
            ),
        ]
    )
    return text, InlineKeyboardMarkup(rows)


def _report_reply_parts(reply: str) -> list[str]:
    """
    Части сообщения автору: префикс каталога + ответ, без потери данных.

    Ответ ограничен `MAX_REPORT_REPLY`, но экранирование HTML может удлинить
    его (например, `<` → `&lt;`). Поэтому сообщение режется штатным
    `split_html` по границам тегов/сущностей: каждая часть — валидный HTML.
    """
    # Ответ — пользовательский текст; `tr` экранирует его сам, поэтому
    # `<` и `&` в ответе не ломают HTML-разметку сообщения.
    text = tr("admin.reports.user_reply", reply=reply)
    if utf16_len(text) <= 4000:
        return [text]
    return split_html(text, 4000)


def _report_reply_delivered_parts(state: dict, reply: str) -> int:
    """
    Сколько частей ответа уже подтверждено Telegram (0 — ни одной).

    Прогресс хранится в состоянии вместе с самим текстом: новый ответ,
    введённый пользователем, никогда не считается частично доставленным.
    """
    progress = state.get("report_reply_delivered")
    if not isinstance(progress, dict) or progress.get("text") != reply:
        return 0
    count = progress.get("parts")
    if not isinstance(count, int) or isinstance(count, bool):
        return 0
    return max(count, 0)


def _report_body_block(report: report_store.Report, *, limit: int) -> str:
    """Текст репорта с бюджетом экрана: сокращение честно помечено."""
    body = _h(report.body)
    return tr_safe(
        "admin.reports.body",
        body=current_value.clip_html(body, limit=limit),
    )


def _report_card_text(
    report: report_store.Report, *, reply: str | None = None, closed: bool = False
) -> str:
    """Карточка репорта: автор, раздел, дата, полный текст (и ответ в архиве)."""
    head = [
        tr("admin.reports.card_title", report_id=report.id),
        tr(
            "admin.reports.author",
            author_label=_report_author_label(report),
            author_user_id=report.author_user_id,
        ),
        tr("admin.reports.section", screen_label=report.screen_label),
        tr("admin.reports.created", created_at=_date_label(report.created_at)),
    ]
    tail: list[str] = []
    if closed:
        tail.append(
            tr(
                "admin.reports.reply_label",
                # `tr` экранирует ответ — это пользовательский текст.
                reply=reply if reply else tr("admin.reports.reply_missing"),
            )
        )
        tail.append(tr("admin.reports.closed", closed_at=_date_label(report.closed_at)))

    def build(limits: list[int]) -> str:
        return join_blocks([*head, _report_body_block(report, limit=limits[0]), *tail])

    # H3: бюджет считается от фактической обвязки, чтобы длинный текст
    # репорта не вынес карточку за лимит Telegram.
    limits = current_value.value_limits(build([0]), 1)
    return current_value.clip_screen(build(limits))


def _report_card_content(
    report: report_store.Report,
) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка действующего репорта: «Решено» и возврат к списку."""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.reports.resolved"),
                    callback_data=f"rpts_reply:{report.id}",
                )
            ],
            [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="rpts_back")],
        ]
    )
    return _report_card_text(report), keyboard


def _report_archive_card_content(
    report: report_store.Report,
) -> tuple[str, InlineKeyboardMarkup]:
    """Архивная карточка: ответ разработчика и дата закрытия, без действий."""
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(tr("buttons.nav.back"), callback_data="rpts_archive")]]
    )
    return (
        _report_card_text(report, reply=report.developer_reply, closed=True),
        keyboard,
    )


def _report_reply_input_content(*, changing: bool) -> tuple[str, InlineKeyboardMarkup]:
    """Шаг 1 ответа: приглашение ввести текст и «Отмена»."""
    prompt = (
        tr("admin.reports.reply_prompt_change")
        if changing
        else tr("admin.reports.reply_prompt")
    )
    return prompt, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.reports.reply_cancel"),
                    callback_data="rpts_reply_cancel",
                )
            ]
        ]
    )


def _report_reply_preview_content(
    reply: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Шаг 2 ответа: предпросмотр и три действия."""
    text = join_blocks(
        [
            # `tr` экранирует ответ: `<` и `&` не ломают HTML-разметку экрана.
            tr("admin.reports.reply_preview", reply=reply),
            tr("admin.reports.reply_preview_hint"),
        ]
    )
    return current_value.clip_screen(text), InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.reports.reply_cancel"),
                    callback_data="rpts_reply_cancel",
                ),
                InlineKeyboardButton(
                    tr("buttons.reports.reply_change"),
                    callback_data="rpts_reply_change",
                ),
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.reports.reply_send"),
                    callback_data="rpts_reply_send",
                )
            ],
        ]
    )


def _report_file_name(report: report_store.Report) -> str:
    """Имя .txt-снимка: то же, что у этапа C (`report_ГГГГММДД_ЧЧММСС.txt`)."""
    return f"report_{report.created_at:%Y%m%d_%H%M%S}.txt"


async def _send_report_snapshot(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    report: report_store.Report,
) -> None:
    """Отправляет .txt-снимок событий; ошибка Telegram не ломает экран."""
    try:
        await context.bot.send_document(
            chat_id=chat_id,
            document=BytesIO((report.events_snapshot or "").encode("utf-8")),
            filename=_report_file_name(report),
        )
    except Exception as exc:  # noqa: BLE001 — текст карточки уже показан
        logger.warning("Снимок репорта %s не отправлен: %s", report.id, exc)


def _reports_event(
    user_id: int,
    action: str,
    *,
    report_id: int | None = None,
    result: str | None = None,
    detail: str | None = None,
) -> None:
    """
    Техническое событие раздела «Репорты».

    `detail` — только идентификатор и технические параметры: текст жалобы
    и ответа разработчика в журнал не попадают (контракт `user_events`).
    """
    parts: list[str] = []
    if report_id is not None:
        parts.append(f"report_id={report_id}")
    if detail:
        parts.append(detail)
    event_log(
        user_id,
        action,
        screen="admin_reports",
        result=result,
        detail="; ".join(parts) or None,
        session_factory=SessionLocal,
    )


def _reports_entry_row(session: Session) -> list[InlineKeyboardButton]:
    """
    Строка входа в раздел «Репорты» для меню кабинета (только суперадмин).

    Счётчик — отдельная неактивная кнопка с no-op callback'ом (PTB 22.8 не
    знает `disabled`). Сбой БД не ломает кабинет: счётчик просто не
    показывается, а кнопка входа остаётся рабочей.
    """
    row = [InlineKeyboardButton(tr("buttons.reports.open"), callback_data="rpts_list")]
    try:
        total = report_store.count_active(session)
    except Exception as exc:  # noqa: BLE001 — кабинет важнее счётчика
        logger.warning("Счётчик активных репортов недоступен: %s", exc)
        return row
    row.append(
        InlineKeyboardButton(
            tr("buttons.reports.counter", count=total),
            callback_data=f"noop:0:{max(total, 0)}",
        )
    )
    return row


def _place_text(
    details: PlaceDetails,
    state: dict,
    session: Session,
    *,
    viewer_telegram_user_id: int | None = None,
) -> str:
    active = get_active_place_admin(session, details.place_id)
    head = tr_safe("admin.place.mesto", canonical_name=_h(details.canonical_name))
    alternatives = [item for item in details.names if not item.is_canonical]
    if alternatives:
        head += tr("admin.place.alternativnye_nazvaniya") + "\n".join(
            f"— {_h(_short(item.name, 120))}" for item in alternatives[:8]
        )
        if len(alternatives) > 8:
            head += tr("admin.place.esche", value=len(alternatives) - 8)

    tail = ""
    open_conflicts = count_open_conflicts_for_place(session, details.place_id)
    if open_conflicts:
        tail += tr(
            "admin.place.aktivnye_zadachi_konfliktov_kodov",
            open_conflicts=open_conflicts,
        )
    if active is None:
        tail += tr("admin.place.administrator_naznachen")
    else:
        tail += tr_safe(
            "admin.place.administrator_naznachen_2",
            user_label=_h(_user_label(active.user, with_id=True)),
            date_label=_h(_date_label(active.assigned_at)),
        )
    if active is not None and viewer_telegram_user_id == active.user.telegram_user_id:
        pending = get_pending_transfer_for_place(session, details.place_id)
        if pending is not None:
            tail += tr_safe(
                "admin.place.peredacha_administracii",
                transfer_status_label=_h(transfer_status_label(pending.status)),
                transfer_expiry_label=_h(_transfer_expiry_label(pending)),
            )
    tail += tr("admin.place.redaktirovanie_nazvaniya")

    # H3 (требование D): на одном экране два длинных значения (адрес и
    # ссылка), поэтому у них общий бюджет от фактической обвязки экрана —
    # иначе два «полных» текста не влезают в 4096.
    blocks = int(bool(details.address)) + int(bool(details.url))

    def build(limits: list[int]) -> str:
        text = head
        position = 0
        if details.address:
            text += tr_safe(
                "admin.build.adres",
                address=_long_value(details.address, limit=limits[position]),
            )
            position += 1
        # Poster 2.0 U1a: город показывается и у старых мест (NULL → «—»).
        text += tr_safe(
            "admin.build.gorod", city=_h(details.city) if details.city else "—"
        )
        if details.url:
            text += tr_safe(
                "admin.build.ssylka",
                url=_safe_link(details.url, limit=limits[position]),
            )
        return text + tail

    return _place_value_limits(build, max(blocks, 1))[1]


def _place_keyboard(
    details: PlaceDetails,
    session: Session,
    *,
    access_kind: str,
    viewer_telegram_user_id: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Poster 2.3.1 E4: действия карточки собираются парами по смыслу —
    # правка места рядом с его шаблоном. Тексты кнопок не сокращаются:
    # длинную строку Telegram переносит сам.
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.redaktirovat_mesto"),
                callback_data=f"adm_edit_place:{details.place_id}",
            ),
            InlineKeyboardButton(
                tr("buttons.admin.standartnyy_shablon"),
                callback_data=f"tp_std:{details.place_id}",
            ),
        ]
    )
    active = get_active_place_admin(session, details.place_id)
    if viewer_telegram_user_id is not None:
        try:
            pending_proposals = count_pending_for_place(
                session, viewer_telegram_user_id, details.place_id
            )
        except ProposalPermissionError:
            pending_proposals = 0
        open_conflicts = count_open_conflicts_for_place(session, details.place_id)
        # Poster 2.5 A3 (§12): динамическая строка без пустых разделов.
        # Кнопка показывается только при актуальных записях, счётчик на
        # кнопке не выводится: (0) — это пустой раздел.
        dynamic_row: list[InlineKeyboardButton] = []
        if pending_proposals > 0:
            dynamic_row.append(
                InlineKeyboardButton(
                    tr("buttons.admin.predlozheniya_kirpichikov"),
                    callback_data=f"adm_bprop_place:{details.place_id}",
                )
            )
        if open_conflicts > 0:
            dynamic_row.append(
                InlineKeyboardButton(
                    tr("buttons.admin.konflikty_kodov"),
                    callback_data=f"adm_bconflicts:{details.place_id}",
                )
            )
        if dynamic_row:
            rows.append(dynamic_row)
        # Poster 2.4 P1: кирпичики площадки — отдельная строка сразу после
        # действий места. Вставка не сдвигает первую строку, навигацию и
        # последнюю административную строку (контракт E4).
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.admin.kirpichiki"),
                    callback_data=f"adm_bricks:{details.place_id}",
                )
            ]
        )
        # «Лекало» 2.6 §6: справочники кураторов и подвалов — одна строка.
        # Кнопка видна, если кирпичик есть в простом варианте ИЛИ (включён
        # расширенный И кирпичик есть в расширенном); наличие определяет
        # штатный разбор шаблона. Скрытие не удаляет записи.
        curators_visible, footers_visible = place_catalog_visibility(
            session, viewer_telegram_user_id, details.place_id
        )
        catalogs_row: list[InlineKeyboardButton] = []
        if curators_visible:
            catalogs_row.append(
                InlineKeyboardButton(
                    tr("buttons.catalogs.open_curators"),
                    callback_data=open_callback(KIND_CURATOR, details.place_id),
                )
            )
        if footers_visible:
            catalogs_row.append(
                InlineKeyboardButton(
                    tr("buttons.catalogs.open_footers"),
                    callback_data=open_callback(KIND_FOOTER, details.place_id),
                )
            )
        if catalogs_row:
            rows.append(catalogs_row)
    # Poster 2.5 A3: админ-действия карточки сведены к одной точке входа.
    # Суперадмин без администратора сразу назначает; замена, снятие и
    # передача прав живут в подразделе «Управление администратором».
    manages_admin = access_kind == "superadmin" or (
        active is not None and viewer_telegram_user_id == active.user.telegram_user_id
    )
    if manages_admin:
        if access_kind == "superadmin" and active is None:
            rows.append(
                [
                    InlineKeyboardButton(
                        tr("buttons.admin.naznachit_administratora"),
                        callback_data=f"adm_assign:{details.place_id}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        tr("buttons.admin.upravlyat_administratorom"),
                        callback_data=f"adm_place_admins:{details.place_id}",
                    )
                ]
            )
    # Poster 2.4.1 T2: ссылка на создание анонса переехала из справочника в
    # полную карточку. Права не расширяются: кнопка видна управляющему
    # местом (и суперадмину), а выдача ссылки перепроверяет права по БД.
    if viewer_telegram_user_id is not None and can_manage_place(
        session, viewer_telegram_user_id, details.place_id
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.places.invite"),
                    callback_data=f"adm_invite:{details.place_id}",
                )
            ]
        )
    # E4: навигация — тоже пара.
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.k_spisku_mest"), callback_data="adm_places"
            ),
            InlineKeyboardButton(
                tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _place_admins_content(
    session: Session,
    details: PlaceDetails,
    *,
    access_kind: str,
    viewer_telegram_user_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Poster 2.5 A3 (§12): служебный подраздел «Управление администратором».

    Второго механизма назначения нет: кнопки ведут в существующие
    `adm_assign` / `adm_replace` / `adm_revoke` / `adm_transfer`.
    """
    active = get_active_place_admin(session, details.place_id)
    text = tr_safe(
        "admin.place_admins.upravlenie_administratorom",
        canonical_name=_h(details.canonical_name),
    )
    if active is None:
        text += tr("admin.place.administrator_naznachen")
    else:
        text += tr_safe(
            "admin.place.administrator_naznachen_2",
            user_label=_h(_user_label(active.user, with_id=True)),
            date_label=_h(_date_label(active.assigned_at)),
        )
    rows: list[list[InlineKeyboardButton]] = []
    if access_kind == "superadmin":
        if active is None:
            rows.append(
                [
                    InlineKeyboardButton(
                        tr("buttons.admin.naznachit_administratora"),
                        callback_data=f"adm_assign:{details.place_id}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        tr("buttons.admin.zamenit_administratora"),
                        callback_data=f"adm_replace:{details.place_id}",
                    ),
                    InlineKeyboardButton(
                        tr("buttons.admin.snyat_administratora"),
                        callback_data=f"adm_revoke:{details.place_id}",
                    ),
                ]
            )
    if active is not None and viewer_telegram_user_id == active.user.telegram_user_id:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.admin.peredat_adminku_2"),
                    callback_data=f"adm_transfer:{details.place_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.k_kartochke_mesta"),
                callback_data="adm_place_admins_back",
            )
        ]
    )
    return text, InlineKeyboardMarkup(rows)


_PROPOSAL_STATUS_UI = {
    PROPOSAL_STATUS_PENDING: tr("admin.module.ozhidaet_resheniya"),
    PROPOSAL_STATUS_APPROVED: tr("admin.module.odobreno"),
    PROPOSAL_STATUS_REJECTED: tr("admin.module.otkloneno"),
    PROPOSAL_STATUS_WITHDRAWN: tr("admin.module.otozvano_avtorom"),
}


def _proposal_status_label(status: str) -> str:
    return _PROPOSAL_STATUS_UI.get(status, status)


def _proposal_settings_text(proposal: BrickProposalSnapshot) -> str:
    rules = proposal.rules
    if proposal.value_type == BRICK_TYPE_NUMBER:
        parts: list[str] = []
        if rules.get("integer"):
            parts.append(tr("admin.proposal_settings.tolko_celye_chisla"))
        if "min" in rules:
            parts.append(tr("admin.proposal_settings.minimum", rules_min=rules["min"]))
        if "max" in rules:
            parts.append(tr("admin.proposal_settings.maksimum", rules_max=rules["max"]))
        if "suffix" in rules:
            parts.append(
                tr("admin.proposal_settings.podpis", rules_suffix=rules["suffix"])
            )
        return (
            "; ".join(parts) + "."
            if parts
            else tr("admin.proposal_settings.bez_ogranicheniy")
        )
    if proposal.value_type == BRICK_TYPE_CHOICE:
        raw_choices = rules.get("choices")
        choices = raw_choices if isinstance(raw_choices, list) else []
        labels = ", ".join(str(item) for item in choices) or tr(
            "admin.proposal_settings.spisok_pust"
        )
        multiple = (
            tr("admin.proposal_settings.mozhno_vybrat_neskolko")
            if rules.get("multiple")
            else tr("admin.proposal_settings.odin_variant")
        )
        return f"{labels}; {multiple}."
    return tr("admin.proposal_settings.bez_dopolnitelnyh_nastroek")


def _proposal_target_label(proposal: BrickProposalSnapshot) -> str:
    if proposal.scope == PROPOSAL_SCOPE_SYSTEM:
        return tr("admin.proposal_target_label.vsya_sistema")
    return tr(
        "admin.proposal_target_label.mesto",
        value=proposal.place_name or f"#{proposal.place_id}",
    )


def _proposal_admin_list_content(
    session: Session,
    telegram_user_id: int,
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    phase = state.get("phase")
    offset = _page_value(state, "proposal_offset")
    place_filter = state.get("proposal_place_filter")
    if phase == "proposal_global_list":
        heading = tr("admin.proposal_admin_list.globalnye_predlozheniya")
        intro = tr("admin.proposal_admin_list.predlozheniya_novyh_kirpichikov")
        proposals, total = list_pending_system_proposals(
            session,
            telegram_user_id,
            offset=offset,
            limit=PROPOSAL_PAGE_SIZE,
        )
        if total and offset >= total:
            offset = ((total - 1) // PROPOSAL_PAGE_SIZE) * PROPOSAL_PAGE_SIZE
            proposals, total = list_pending_system_proposals(
                session,
                telegram_user_id,
                offset=offset,
                limit=PROPOSAL_PAGE_SIZE,
            )
        page_prefix = "adm_bprop_global_page"
        back_label = tr("buttons.admin.k_spisku_mest")
    else:
        if isinstance(place_filter, int) and not isinstance(place_filter, bool):
            details = get_place_details(session, place_filter)
            if details is None:
                state.clear()
                state.update(_new_state())
                raise ProposalPermissionError(
                    tr("admin.render_phase.mesto_suschestvuet_bylo_udaleno")
                )
            heading = tr("admin.proposal_admin_list.predlozheniya_kirpichikov")
            intro = tr_safe(
                "admin.proposal_admin_list.ozhidayuschie_predlozheniya_mesta",
                canonical_name=_h(details.canonical_name),
            )
            proposals, total = list_pending_for_place(
                session,
                telegram_user_id,
                details.place_id,
                offset=offset,
                limit=PROPOSAL_PAGE_SIZE,
            )
            if total and offset >= total:
                offset = ((total - 1) // PROPOSAL_PAGE_SIZE) * PROPOSAL_PAGE_SIZE
                proposals, total = list_pending_for_place(
                    session,
                    telegram_user_id,
                    details.place_id,
                    offset=offset,
                    limit=PROPOSAL_PAGE_SIZE,
                )
            back_label = tr("admin.proposal_admin_list.k_mestu")
        else:
            heading = tr("admin.proposal_admin_list.predlozheniya_mestam")
            intro = tr("admin.proposal_admin_list.vse_ozhidayuschie_predlozheniya")
            proposals, total = list_pending_place_proposals(
                session,
                telegram_user_id,
                offset=offset,
                limit=PROPOSAL_PAGE_SIZE,
            )
            if total and offset >= total:
                offset = ((total - 1) // PROPOSAL_PAGE_SIZE) * PROPOSAL_PAGE_SIZE
                proposals, total = list_pending_place_proposals(
                    session,
                    telegram_user_id,
                    offset=offset,
                    limit=PROPOSAL_PAGE_SIZE,
                )
            back_label = tr("buttons.admin.k_spisku_mest")
        page_prefix = "adm_bprop_places_page"
    state["proposal_offset"] = offset
    text = f"{heading} — {total}\n\n{intro}"
    if not total:
        text += tr("admin.proposal_admin_list.ozhidayuschih_predlozheniy_net")
    else:
        text += "\n"
        for index, proposal in enumerate(proposals, start=offset + 1):
            text += tr_safe(
                "admin.proposal_admin_list.avtor_oblast_otpravleno_html",
                index=index,
                title=_h(brick_title_display(proposal.title, proposal.code)),
                code=_h(proposal.code),
                author_label=_h(_short(proposal.author_label, 100)),
                proposal_target_label=_h(_proposal_target_label(proposal)),
                date_label=_h(_date_label(proposal.created_at)),
            )
    rows = [
        [
            InlineKeyboardButton(
                _button_name(
                    f"{index}. ",
                    f"{brick_title_display(proposal.title, proposal.code)} "
                    f"— [{proposal.code}]",
                ),
                callback_data=f"adm_bprop_open:{proposal.id}",
            )
        ]
        for index, proposal in enumerate(proposals, start=offset + 1)
    ]
    navigation = pagination.nav_keyboard_for_offset(
        offset, total, page_prefix, page_size=PROPOSAL_PAGE_SIZE
    )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(back_label, callback_data="adm_bprop_back")])
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
            )
        ]
    )
    return text, InlineKeyboardMarkup(rows)


def _proposal_description_html(value: object) -> str:
    """
    Описание в карточке предложения (R4 №29): моноширинно, «—» при отсутствии.

    Текст задаёт человек, поэтому он экранируется перед обёрткой в `<code>`.
    Старые pending-предложения без описания показывают «—».
    """
    text = " ".join(str(value or "").split())
    if not text:
        return "—"
    return f"<code>{_h(text)}</code>"


def _proposal_admin_card_content(
    proposal: BrickProposalSnapshot,
) -> tuple[str, InlineKeyboardMarkup]:
    parts = [
        tr_safe("admin.proposal_admin_card.predlozhenie_kirpichika_html"),
        "",
        # R4 №29: все семь полей карточки — название, код, тип, описание,
        # автор, цель и статус — в одном месте и в одном порядке.
        tr_safe(
            "admin.proposal_admin_card.nazvanie_html",
            title=_h(brick_title_display(proposal.title, proposal.code)),
        ),
        tr_safe("admin.proposal_admin_card.oboznachenie_html", code=_h(proposal.code)),
        tr_safe(
            "admin.proposal_admin_card.tip",
            get=_h(BRICK_TYPE_TITLES.get(proposal.value_type, proposal.value_type)),
        ),
        tr_safe(
            "admin.proposal_admin_card.opisanie",
            description=_proposal_description_html(proposal.description),
        ),
        tr_safe(
            "admin.proposal_admin_card.avtor", author_label=_h(proposal.author_label)
        ),
        tr_safe(
            "admin.proposal_admin_card.cel",
            proposal_target_label=_h(_proposal_target_label(proposal)),
        ),
        tr_safe(
            "admin.proposal_admin_card.status",
            proposal_status_label=_h(_proposal_status_label(proposal.status)),
        ),
        tr_safe(
            "admin.proposal_admin_card.nastroyki",
            proposal_settings_text=_h(_proposal_settings_text(proposal)),
        ),
        tr_safe(
            "admin.proposal_admin_card.otpravleno",
            date_label=_h(_date_label(proposal.created_at)),
        ),
    ]
    if proposal.decided_at is not None:
        parts.append(
            tr_safe(
                "admin.proposal_admin_card.reshenie",
                date_label=_h(_date_label(proposal.decided_at)),
            )
        )
    if proposal.status == PROPOSAL_STATUS_APPROVED:
        parts.append(
            tr(
                "admin.proposal_admin_card.sozdan_otdelnyy_kirpichik_ishodnyy",
                approved_brick_id=proposal.approved_brick_id,
            )
        )
    elif proposal.status == PROPOSAL_STATUS_REJECTED:
        if proposal.decision_note:
            parts.append(
                tr_safe(
                    "admin.proposal_admin_card.prichina",
                    decision_note=_h(proposal.decision_note),
                )
            )
        else:
            parts.append(tr("admin.proposal_admin_card.predlozhenie_otkloneno_bez"))
    elif proposal.status == PROPOSAL_STATUS_WITHDRAWN:
        parts.append(tr("admin.proposal_admin_card.predlozhenie_otozvano_avtorom"))
    else:
        parts.extend(
            [
                "",
                tr("admin.proposal_admin_card.snimok_nazvaniya_koda_tipa"),
            ]
        )
    rows: list[list[InlineKeyboardButton]] = []
    if proposal.status == PROPOSAL_STATUS_PENDING:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.admin.odobrit"),
                    callback_data=f"adm_bprop_approve:{proposal.id}",
                ),
                InlineKeyboardButton(
                    tr("buttons.admin.otklonit"),
                    callback_data=f"adm_bprop_reject:{proposal.id}",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.k_ocheredi"), callback_data="adm_bprop_back"
            )
        ]
    )
    return "\n".join(parts), InlineKeyboardMarkup(rows)


def _proposal_approve_confirm_content(
    proposal: BrickProposalSnapshot,
) -> tuple[str, InlineKeyboardMarkup]:
    text = tr_safe(
        "admin.proposal_approve_confirm.odobrit_predlozhenie_kirpi_html",
        title=_h(brick_title_display(proposal.title, proposal.code)),
        code=_h(proposal.code),
        proposal_target_label=_h(_proposal_target_label(proposal)),
        author_label=_h(proposal.author_label),
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.da_odobrit"),
                    callback_data=f"adm_bprop_approve_yes:{proposal.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.net_nazad"), callback_data="adm_bprop_back"
                )
            ],
        ]
    )


def _proposal_reject_confirm_content(
    proposal: BrickProposalSnapshot,
) -> tuple[str, InlineKeyboardMarkup]:
    text = tr_safe(
        "admin.proposal_reject_confirm.otklonit_predlozhenie_kirpi_html",
        title=_h(brick_title_display(proposal.title, proposal.code)),
        code=_h(proposal.code),
        proposal_target_label=_h(_proposal_target_label(proposal)),
        author_label=_h(proposal.author_label),
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.da_otklonit"),
                    callback_data=f"adm_bprop_reject_yes:{proposal.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.net_nazad"), callback_data="adm_bprop_back"
                )
            ],
        ]
    )


# --- Конфликты кодов: задачи разрешения (Poster 2.1 R3) --------------------


def _conflict_short_label(view: BrickConflictView) -> str:
    place = view.place_name or tr(
        "admin.conflict_short_label.mesto", place_id=view.place_id
    )
    return f"[{view.code}] — {place}"


def _conflicts_list_content(
    session: Session,
    telegram_user_id: int,
    view: BrickConflictView | None,
    place_id: int,
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    details = get_place_details(session, place_id)
    place_label = details.canonical_name if details is not None else f"#{place_id}"
    conflicts = list_place_conflicts(session, telegram_user_id, place_id)
    # Poster 2.5 §14: конфликты — тоже длинный список, пять записей на
    # страницу. Смещение хранится в состоянии кабинета и приводится к
    # допустимому при рендере.
    offset = (
        pagination.clamp_page(_page_value(state, "conflict_page"), len(conflicts))
        * pagination.PAGE_SIZE
    )
    state["conflict_page"] = offset
    page_conflicts = conflicts[offset : offset + pagination.PAGE_SIZE]
    text = tr_safe(
        "admin.conflicts_list.konflikty_kodov_globalnyy", place_label=_h(place_label)
    )
    if not conflicts:
        text += tr("admin.conflicts_list.aktivnyh_zadach_net")
    else:
        text += tr(
            "admin.conflicts_list.aktivnyh_zadach", count_conflicts=len(conflicts)
        )
        for index, item in enumerate(page_conflicts, start=offset + 1):
            text += (
                f"\n{index}. <b>{_h(item.local_brick_title)}</b> "
                f"<code>[{_h(item.code)}]</code>"
            )
    rows: list[list[InlineKeyboardButton]] = []
    for item in page_conflicts:
        rows.append(
            [
                InlineKeyboardButton(
                    _button_name(f"⚔️ {item.code}: ", item.local_brick_title),
                    callback_data=f"adm_bconflict:{item.id}",
                )
            ]
        )
    nav = pagination.nav_keyboard_for_offset(
        offset, len(conflicts), "adm_bconflicts_page"
    )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                tr("admin.proposal_admin_list.k_mestu"),
                callback_data=f"adm_place:{place_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
            )
        ]
    )
    return text, InlineKeyboardMarkup(rows)


def _conflict_card_content(
    view: BrickConflictView,
) -> tuple[str, InlineKeyboardMarkup]:
    place = view.place_name or tr(
        "admin.conflict_short_label.mesto", place_id=view.place_id
    )
    text = tr_safe(
        "admin.conflict_card.zadacha_razresheniya_konflikta_kod_html",
        code=_h(view.code),
        place=_h(place),
        local_brick_title=_h(view.local_brick_title),
        local_brick_id=view.local_brick_id,
        system_brick_id=view.system_brick_id,
    )
    rows: list[list[InlineKeyboardButton]] = []
    if view.is_open:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.admin.pereimenovat_mestnyy"),
                    callback_data=f"adm_bconflict_rename:{view.id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.admin.pereklyuchitsya_globalnyy"),
                    callback_data=f"adm_bconflict_switch:{view.id}",
                )
            ]
        )
    else:
        text += tr("admin.conflict_card.zadacha_reshena_istoriya_sohranena")
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.k_konfliktam"),
                callback_data=f"adm_bconflicts:{view.place_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
            )
        ]
    )
    return text, InlineKeyboardMarkup(rows)


def _conflict_rename_input_content(
    view: BrickConflictView,
) -> tuple[str, InlineKeyboardMarkup]:
    text = tr_safe(
        "admin.conflict_rename_input.pereimenovanie_mestnogo_kirpi_html",
        code=_h(view.code),
        local_brick_title=_h(view.local_brick_title),
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.nav.back"),
                    callback_data=f"adm_bconflict:{view.id}",
                )
            ]
        ]
    )


def _conflict_switch_confirm_content(
    view: BrickConflictView,
) -> tuple[str, InlineKeyboardMarkup]:
    text = tr_safe(
        "admin.conflict_switch_confirm.pereklyuchitsya_globalnyy_html",
        code=_h(view.code),
        h=_h(view.place_name or f"#{view.place_id}"),
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.da_pereklyuchit"),
                    callback_data=f"adm_bconflict_switch_yes:{view.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.net_nazad"),
                    callback_data=f"adm_bconflict:{view.id}",
                )
            ],
        ]
    )


def _assign_search_text(details: PlaceDetails, state: dict) -> str:
    mode = str(state.get("mode") or "assign")
    action = (
        tr("admin.assign_search.zameny_administratora")
        if mode == "replace"
        else tr("admin.assign_search.naznacheniya")
    )
    return tr_safe(
        "admin.assign_search.poisk_poluchatelya_vvedite",
        action=action,
        canonical_name=_h(details.canonical_name),
    )


# R1, дефект №21: исход поиска получателя всегда явный. Вид запроса хранится
# в состоянии, чтобы «не найдено» объясняло, что именно не нашлось.
ASSIGN_QUERY_INVALID_TEXT = tr("admin.module.nevernyy_format_zaprosa_vvedite")

ASSIGN_QUERY_USERNAME = "username"
ASSIGN_QUERY_ID = "id"
ASSIGN_QUERY_NAME = "name"


def _normalize_assign_query(raw: str) -> tuple[str, str] | None:
    """
    Разбирает ввод в фазе `assign_search`.

    Возвращает (вид, значение) или None для пустого/несимвольного ввода —
    вызывающий показывает явную ошибку формата, а не молчит.
    """
    cleaned = " ".join(str(raw).split())
    if not cleaned:
        return None
    if cleaned.startswith("@"):
        username = cleaned[1:].strip()
        # Один «@» без имени — это не поиск, а ошибка формата.
        return (ASSIGN_QUERY_USERNAME, username) if username else None
    if cleaned.isascii() and cleaned.isdecimal():
        try:
            telegram_user_id = int(cleaned)
        except ValueError:  # pragma: no cover — isdecimal уже отсеял
            return None
        return (
            (ASSIGN_QUERY_ID, str(telegram_user_id)) if telegram_user_id > 0 else None
        )
    if not any(character.isalnum() for character in cleaned):
        return None
    return (ASSIGN_QUERY_NAME, cleaned)


def _assign_not_found_text(query: str, kind: str) -> str:
    """Явное «не найдено» с конкретным следующим шагом (R1, дефект №21)."""
    if kind == ASSIGN_QUERY_ID:
        return tr_safe(
            "admin.assign_not_found.polzovatel_nayden_telegram_user",
            query=_h(_short(query, 40)),
        )
    if kind == ASSIGN_QUERY_USERNAME:
        return tr_safe(
            "admin.assign_not_found.polzovatel_nayden_izvesten_botu",
            query=_h(_short(query, 100)),
        )
    return tr_safe(
        "admin.assign_not_found.polzovatel_nayden_zaprosu_esli",
        query=_h(_short(query, 100)),
    )


def _assign_results_content(
    session: Session, details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    query = str(state.get("query") or "")
    results = search_known_users(session, query)
    text = tr_safe(
        "admin.assign_results.poluchatel_zapros",
        canonical_name=_h(details.canonical_name),
        query=_h(_short(query, 100)),
    )
    keyboard: list[list[InlineKeyboardButton]] = []
    if not results.users:
        # R1, дефект №21: у «не найдено» должен быть ЯВНЫЙ следующий шаг —
        # человек может быть просто не знаком боту (не нажимал /start).
        parsed = _normalize_assign_query(query)
        kind = str(state.get("query_kind") or (parsed[0] if parsed else ""))
        text += _assign_not_found_text(query, kind)
    else:
        if results.total == len(results.users):
            text += tr("admin.assign_results.naydeno", total=results.total)
        else:
            text += tr(
                "admin.assign_results.naydeno_pokazany_pervye_utochnite",
                total=results.total,
                count_users=len(results.users),
            )
        for index, user in enumerate(results.users, start=1):
            label = _short(_user_label(user), 100)
            text += (
                f"{index}. {_h(label)}\n"
                f"   Telegram user ID: {user.telegram_user_id}\n"
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        _button_name(f"{index}. ", _user_label(user)),
                        callback_data=f"adm_pick:{user.telegram_user_id}",
                    )
                ]
            )
    keyboard.append(
        [
            InlineKeyboardButton(
                tr("buttons.nav.retry_search"), callback_data="adm_search_again"
            )
        ]
    )
    keyboard.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="adm_cancel")]
    )
    return text, InlineKeyboardMarkup(keyboard)


ALIAS_PAGE_SIZE = pagination.PAGE_SIZE


def _edit_menu_content(
    session: Session, details: PlaceDetails
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Меню редактирования данных места (Stage 20B, H3 требование C).

    Правка и удаление одного поля стоят парой «✏️ … | 🗑», чтобы действие и
    его опасная альтернатива были рядом, но не сливались с подтверждением.
    У названия и города удаления нет: каноническое название — якорь места, а
    город задаётся только изменением (сброс города из UI убран, H3-C).
    """
    aliases = list_place_aliases(session, details.place_id)
    # H3 (требование D): на одном экране два длинных значения, поэтому у них
    # общий бюджет (utils/current_value), а не фиксированные константы.
    blocks = int(bool(details.address)) + int(bool(details.url))

    def build(limits: list[int]) -> str:
        position = 0
        if details.address:
            address_text = _long_value(details.address, limit=limits[position])
            position += 1
        else:
            address_text = tr("admin.build.ne_ukazan")
        if details.url:
            url_text = _safe_link(details.url, limit=limits[position])
        else:
            url_text = tr("admin.safe_link.ne_ukazana")
        return tr_safe(
            "admin.build.redaktirovanie_mesta_nazvanie",
            canonical_name=_h(details.canonical_name),
            count_aliases=len(aliases),
            city=_h(details.city) if details.city else tr("admin.build.ne_ukazan"),
            address_text=address_text,
            url_text=url_text,
        )

    text = _place_value_limits(build, max(blocks, 1))[1]
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                tr("buttons.admin.izmenit_nazvanie"),
                callback_data=f"adm_edit_name:{details.place_id}",
            )
        ],
        [
            InlineKeyboardButton(
                tr("buttons.admin.alternativnye_nazvaniya"),
                callback_data=f"adm_aliases:{details.place_id}",
            )
        ],
        [
            InlineKeyboardButton(
                tr("buttons.admin.izmenit_gorod"),
                callback_data=f"adm_edit_city:{details.place_id}",
            )
        ],
        [
            InlineKeyboardButton(
                tr("buttons.admin.adres"),
                callback_data=f"adm_edit_address:{details.place_id}",
            )
        ],
        [
            InlineKeyboardButton(
                tr("buttons.admin.ssylka"),
                callback_data=f"adm_edit_url:{details.place_id}",
            )
        ],
    ]
    if details.address:
        # Корзина рядом с правкой того же поля, но отдельной кнопкой:
        # удаление всё равно требует подтверждения.
        rows[3].append(
            InlineKeyboardButton(
                "🗑", callback_data=f"adm_edit_address_delete:{details.place_id}"
            )
        )
    if details.url:
        rows[4].append(
            InlineKeyboardButton(
                "🗑", callback_data=f"adm_edit_url_delete:{details.place_id}"
            )
        )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="adm_cancel")]
    )
    return text, InlineKeyboardMarkup(rows)


def _edit_input_content(
    details: PlaceDetails, phase: str, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран ввода нового значения для редактируемого поля."""
    if phase == "edit_name_input":
        text = tr_safe(
            "admin.edit_input.izmenenie_nazvaniya_tekuschee",
            canonical_name=_h(details.canonical_name),
        )
    elif phase == "edit_alias_add_input":
        text = tr_safe(
            "admin.edit_input.dobavlenie_alternativnogo",
            canonical_name=_h(details.canonical_name),
        )
    elif phase == "edit_alias_rename_input":
        target_name = _h(str(state.get("edit_target_name") or ""))
        text = tr_safe(
            "admin.edit_input.pereimenovanie_alternativnogo",
            canonical_name=_h(details.canonical_name),
            target_name=target_name,
        )
    elif phase == "edit_address_input":

        def build(limits: list[int]) -> str:
            current = (
                _long_value(details.address, limit=limits[0])
                if details.address
                else tr("admin.build.ne_ukazan")
            )
            return tr_safe(
                "admin.build.izmenenie_adresa_tekuschiy_adres",
                canonical_name=_h(details.canonical_name),
                current=current,
            )

        # H3 (требование D): даже один длинный адрес ограничен бюджетом
        # экрана, иначе ввод не отправится вовсе.
        text = _place_value_limits(build, 1)[1]
    elif phase == "edit_city_input":
        current = _h(details.city) if details.city else tr("admin.build.ne_ukazan")
        text = tr_safe(
            "admin.edit_input.izmenenie_goroda_tekuschiy_gorod",
            canonical_name=_h(details.canonical_name),
            current=current,
        )
    elif phase == "edit_url_input":

        def build(limits: list[int]) -> str:
            return tr_safe(
                "admin.build.izmenenie_ssylki_tekuschaya_ssylka",
                canonical_name=_h(details.canonical_name),
                url=(
                    _safe_link(details.url, limit=limits[0])
                    if details.url
                    else tr("admin.safe_link.ne_ukazana")
                ),
            )

        text = _place_value_limits(build, 1)[1]
    else:
        text = tr("admin.edit_input.redaktirovanie_mesta_vvedite_novoe")
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.otmena_2"), callback_data="adm_cancel"
                )
            ]
        ]
    )


def _edit_confirm_content(
    session: Session, details: PlaceDetails, phase: str, state: dict
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Экран подтверждения изменения данных места."""
    warning = str(state.get("edit_warning") or "")
    warning_block = f"⚠️ {warning}\n\n" if warning else ""

    if phase == "edit_address_delete_confirm":
        if not details.address:
            return None

        def build(limits: list[int]) -> str:
            return tr_safe(
                "admin.build.podtverdite_udalenie_adresa",
                canonical_name=_h(details.canonical_name),
                address=_long_value(details.address, limit=limits[0]),
            )

        text = _place_value_limits(build, 1)[1]
    elif phase == "edit_url_delete_confirm":
        if not details.url:
            return None

        def build(limits: list[int]) -> str:
            return tr_safe(
                "admin.build.podtverdite_udalenie_ssylki",
                canonical_name=_h(details.canonical_name),
                url=_safe_link(details.url, limit=limits[0]),
            )

        text = _place_value_limits(build, 1)[1]
    elif phase == "edit_alias_delete_confirm":
        target_name = _h(str(state.get("edit_target_name") or ""))
        text = tr_safe(
            "admin.edit_confirm.podtverdite_udalenie",
            warning_block=warning_block,
            canonical_name=_h(details.canonical_name),
            target_name=target_name,
        )
    else:
        value = state.get("edit_value")
        if not isinstance(value, str) or not value:
            return None

        if phase == "edit_name_confirm":
            if value == details.canonical_name:
                return None
            text = tr_safe(
                "admin.edit_confirm.podtverdite_izmenenie_nazvaniya",
                warning_block=warning_block,
                canonical_name=_h(details.canonical_name),
                h=_h(value),
            )
        elif phase == "edit_alias_add_confirm":
            text = tr_safe(
                "admin.edit_confirm.podtverdite_dobavlenie",
                warning_block=warning_block,
                canonical_name=_h(details.canonical_name),
                h=_h(value),
            )
        elif phase == "edit_alias_rename_confirm":
            target_name = _h(str(state.get("edit_target_name") or ""))
            text = tr_safe(
                "admin.edit_confirm.podtverdite_pereimenovanie",
                warning_block=warning_block,
                canonical_name=_h(details.canonical_name),
                target_name=target_name,
                h=_h(value),
            )
        elif phase == "edit_address_confirm":

            def build(limits: list[int]) -> str:
                current = (
                    _long_value(details.address, limit=limits[0])
                    if details.address
                    else tr("admin.build.ne_ukazan")
                )
                return tr_safe(
                    "admin.build.podtverdite_izmenenie_adresa",
                    warning_block=warning_block,
                    canonical_name=_h(details.canonical_name),
                    current=current,
                    long_value=_long_value(value, limit=limits[1]),
                )

            # Два значения на одном экране делят остаток лимита Telegram.
            text = _place_value_limits(build, 2)[1]
        elif phase == "edit_city_confirm":
            current = _h(details.city) if details.city else tr("admin.build.ne_ukazan")
            text = tr_safe(
                "admin.edit_confirm.podtverdite_izmenenie_goroda",
                warning_block=warning_block,
                canonical_name=_h(details.canonical_name),
                current=current,
                h=_h(value),
            )
        elif phase == "edit_url_confirm":
            current = details.url if details.url else tr("admin.safe_link.ne_ukazana")

            def build(limits: list[int]) -> str:
                return tr_safe(
                    "admin.build.podtverdite_izmenenie_ssylki",
                    warning_block=warning_block,
                    canonical_name=_h(details.canonical_name),
                    current=_safe_link(current, limit=limits[0]),
                    safe_link=_safe_link(value, limit=limits[1]),
                )

            text = _place_value_limits(build, 2)[1]
        else:
            return None
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.podtverdit"), callback_data="adm_edit_confirm"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.otmena"), callback_data="adm_cancel"
                )
            ],
        ]
    )


def _edit_aliases_content(
    session: Session, details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    """Список альтернативных названий с пагинацией и действиями."""
    aliases = list_place_aliases(session, details.place_id)
    total = len(aliases)
    # Poster 2.5 §14: страница приводится к допустимой и сохраняется в
    # состоянии — после удаления последней записи пустой страницы не остаётся.
    offset = _clamp_offset(
        max(_page_value(state, "alias_page"), 0), total, ALIAS_PAGE_SIZE
    )
    state["alias_page"] = offset
    page_items = aliases[offset : offset + ALIAS_PAGE_SIZE]

    if total == 0:
        text = tr_safe(
            "admin.edit_aliases.alternativnye_nazvaniya_poka_net",
            canonical_name=_h(details.canonical_name),
        )
    else:
        text = tr_safe(
            "admin.edit_aliases.alternativnye_nazvaniya_naydeno",
            canonical_name=_h(details.canonical_name),
            total=total,
        )
        text += "\n".join(
            f"{index}. {_h(_short(alias.name, 120))}"
            for index, alias in enumerate(page_items, start=offset + 1)
        )

    keyboard: list[list[InlineKeyboardButton]] = []
    for alias in page_items:
        keyboard.append(
            [
                InlineKeyboardButton(
                    _button_name("✏️", alias.name),
                    callback_data=f"adm_alias_rename:{alias.name_id}",
                ),
                InlineKeyboardButton(
                    tr("buttons.admin.udalit"),
                    callback_data=f"adm_alias_delete:{alias.name_id}",
                ),
            ]
        )
    # Poster 2.5 §14: тот же компонент пагинации, callback прежний.
    nav = pagination.nav_keyboard_for_offset(
        offset, total, "adm_alias_page", page_size=ALIAS_PAGE_SIZE
    )
    if nav:
        keyboard.append(nav)
    keyboard.append(
        [
            InlineKeyboardButton(
                tr("buttons.admin.dobavit_nazvanie"),
                callback_data=f"adm_alias_add:{details.place_id}",
            )
        ]
    )
    keyboard.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="adm_cancel")]
    )
    return text, InlineKeyboardMarkup(keyboard)


def _confirmation_content(
    session: Session, details: PlaceDetails, state: dict, phase: str
) -> tuple[str, InlineKeyboardMarkup] | None:
    active = get_active_place_admin(session, details.place_id)
    target: AdminUser | None = None
    if phase != "confirm_revoke":
        target_value = state.get("target_telegram_user_id")
        if not isinstance(target_value, int):
            return None
        target_row = search_known_users(session, str(target_value))
        if not target_row.users:
            return None
        target = target_row.users[0]

    if phase == "confirm_assign":
        if active is not None or target is None:
            return None
        title = tr("admin.confirmation.podtverdite_naznachenie")
        action_line = tr("admin.confirmation.naznachit_etogo_polzovatelya")
        text = tr_safe(
            "admin.confirmation.poluchatel",
            title=title,
            canonical_name=_h(details.canonical_name),
            user_label=_h(_user_label(target, with_id=True)),
            action_line=action_line,
        )
    elif phase == "confirm_replace":
        expected = state.get("expected_admin_telegram_user_id")
        if active is None or active.user.telegram_user_id != expected:
            return None
        if target is None:
            return None
        title = tr("admin.confirmation.podtverdite_zamenu_administratora")
        text = tr_safe(
            "admin.confirmation.seychas_novyy_administrator",
            title=title,
            canonical_name=_h(details.canonical_name),
            user_label=_h(_user_label(active.user, with_id=True)),
            user_label_2=_h(_user_label(target, with_id=True)),
        )
    else:
        expected = state.get("expected_admin_telegram_user_id")
        if active is None or active.user.telegram_user_id != expected:
            return None
        title = tr("admin.confirmation.podtverdite_snyatie_administratora")
        text = tr_safe(
            "admin.confirmation.administrator_posle_snyatiya_u",
            title=title,
            canonical_name=_h(details.canonical_name),
            user_label=_h(_user_label(active.user, with_id=True)),
        )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.podtverdit"), callback_data="adm_confirm"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.otmena"), callback_data="adm_cancel_confirm"
                )
            ],
        ]
    )
    return text, keyboard


def _reports_phase_content(
    session: Session, state: dict, phase: str
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Экран фаз раздела «Репорты».

    Репорт каждый раз читается из БД: статус мог измениться в другом окне,
    и карточка обязана показывать актуальные данные. Отсутствующий репорт
    — «Репорт не найден.» с возвратом к списку, а не исключение.
    """

    def _list_fallback() -> tuple[str, InlineKeyboardMarkup]:
        # Возврат к списку: собственное состояние раздела сбрасывается.
        state.pop("report_reply", None)
        state.pop("report_reply_delivered", None)
        if phase in {"reports_archive", "report_archive_card"}:
            state["phase"] = "reports_archive"
            return _reports_archive_content(session, state)
        state["phase"] = "reports_list"
        return _reports_list_content(session, state)

    if phase == "reports_list":
        return _reports_list_content(session, state)
    if phase == "reports_archive":
        return _reports_archive_content(session, state)

    if phase in {"report_reply_input", "report_reply_preview"}:
        reply = state.get("report_reply")
        if phase == "report_reply_preview":
            # Предпросмотр без текста бессмыслен: возвращаемся к вводу,
            # репорт при этом остаётся открытым.
            if not isinstance(reply, str) or not reply:
                state["phase"] = "report_reply_input"
                return _report_reply_input_content(changing=True)
            return _report_reply_preview_content(reply)
        return _report_reply_input_content(
            changing=bool(state.get("report_reply_changing"))
        )

    if phase == "report_archive_card":
        report_id = state.get("report_archive_id")
        report = (
            report_store.get_report(session, report_id)
            if isinstance(report_id, int) and not isinstance(report_id, bool)
            else None
        )
        if report is None or not report.is_archived:
            state.pop("report_archive_id", None)
            state.pop("report_reply", None)
            state.pop("report_reply_delivered", None)
            return _list_fallback()
        return _report_archive_card_content(report)

    report_id = state.get("report_id")
    report = (
        report_store.get_report(session, report_id)
        if isinstance(report_id, int) and not isinstance(report_id, bool)
        else None
    )
    if report is None:
        state.pop("report_id", None)
        state.pop("report_reply", None)
        state.pop("report_reply_delivered", None)
        return _list_fallback()
    if not report.is_active:
        # Другой суперадмин уже закрыл репорт: показываем архивную карточку,
        # действия недоступны. Фаза переключается, чтобы ввод текста ответа
        # больше не принимался, и состояние ответа сбрасывается.
        state.pop("report_reply", None)
        state.pop("report_reply_delivered", None)
        state.pop("report_reply_changing", None)
        state["phase"] = "report_archive_card"
        state["report_archive_id"] = report.id
        return _report_archive_card_content(report)
    return _report_card_content(report)


async def _render_phase(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    *,
    notice: str | None = None,
    cleanup_ids: list[int] | None = None,
    drop_input: bool = False,
    invite_url: str | None = None,
) -> bool:
    """Единая отрисовка фазы; каждый раз проверяет актуальные права."""
    user_id = ctx.user(update).id
    phase = str(state.get("phase") or "")
    session = SessionLocal()
    try:
        access_kind = _access_kind(session, user_id)
        if access_kind is None:
            session.close()
            await _render_denied(update, context)
            return False

        if phase not in PHASES:
            state.clear()
            state.update(_new_state())
            phase = "places"
        if phase in SUPER_ONLY_PHASES and access_kind != "superadmin":
            state.clear()
            state.update(_new_state())
            phase = "places"
            notice = notice or tr("admin.render_phase.deystvie_dostupno_tolko")

        place_id = state.get("place_id")
        details = (
            get_place_details(session, int(place_id))
            if isinstance(place_id, int)
            else None
        )

        if phase == "places":
            text, keyboard = _places_content(session, state, access_kind, user_id)
        elif phase in REPORTS_PHASES:
            text, keyboard = _reports_phase_content(session, state, phase)
        elif phase == "place":
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.mesto_suschestvuet_bylo_udaleno"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not can_manage_place(session, user_id, details.place_id):
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.dostup_etomu_mestu_podtverzhden"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                text = _place_text(
                    details,
                    state,
                    session,
                    viewer_telegram_user_id=user_id,
                )
                keyboard = _place_keyboard(
                    details,
                    session,
                    access_kind=access_kind,
                    viewer_telegram_user_id=user_id,
                )
        elif phase == "place_admins":
            # Poster 2.5 A3: подраздел управления администратором места.
            # Права проверяются по БД: управляющий местом или суперадмин.
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.mesto_suschestvuet_bylo_udaleno"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not can_manage_place(session, user_id, details.place_id):
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.dostup_etomu_mestu_podtverzhden"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                text, keyboard = _place_admins_content(
                    session,
                    details,
                    access_kind=access_kind,
                    viewer_telegram_user_id=user_id,
                )
        elif phase == "invite":
            # Poster 2.4.1 T2: экран ссылки на создание анонса — часть полной
            # карточки. Права проверяются по БД, а не по факту прихода сюда.
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.mesto_suschestvuet_bylo_udaleno"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not can_manage_place(session, user_id, details.place_id):
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.dostup_etomu_mestu_podtverzhden"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                text, keyboard = await invite_content(
                    context,
                    session,
                    details,
                    telegram_user_id=user_id,
                    regenerated=bool(state.get("invite_regenerated")),
                )
        elif phase == "assign_search":
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.mesto_suschestvuet_bylo_udaleno"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                text = _assign_search_text(details, state)
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                tr("buttons.nav.back"), callback_data="adm_cancel"
                            )
                        ]
                    ]
                )
        elif phase == "assign_results":
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.mesto_suschestvuet_bylo_udaleno"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not state.get("query"):
                state["phase"] = "assign_search"
                text = _assign_search_text(details, state)
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                tr("buttons.nav.back"), callback_data="adm_cancel"
                            )
                        ]
                    ]
                )
            else:
                text, keyboard = _assign_results_content(session, details, state)
        elif phase == "edit_menu":
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.mesto_suschestvuet_bylo_udaleno"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not can_manage_place(session, user_id, details.place_id):
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.dostup_etomu_mestu_podtverzhden"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                text, keyboard = _edit_menu_content(session, details)
        elif phase in {
            "edit_name_input",
            "edit_alias_add_input",
            "edit_alias_rename_input",
            "edit_address_input",
            "edit_city_input",
            "edit_url_input",
        }:
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.mesto_suschestvuet_bylo_udaleno"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not can_manage_place(session, user_id, details.place_id):
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.dostup_etomu_mestu_podtverzhden"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                text, keyboard = _edit_input_content(details, phase, state)
        elif phase in {
            "edit_name_confirm",
            "edit_alias_add_confirm",
            "edit_alias_rename_confirm",
            "edit_alias_delete_confirm",
            "edit_address_confirm",
            "edit_address_delete_confirm",
            "edit_city_confirm",
            "edit_url_confirm",
            "edit_url_delete_confirm",
        }:
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.mesto_suschestvuet_bylo_udaleno"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not can_manage_place(session, user_id, details.place_id):
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.dostup_etomu_mestu_podtverzhden"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                content = _edit_confirm_content(session, details, phase, state)
                if content is None:
                    if phase.startswith("edit_alias"):
                        state["phase"] = "edit_aliases"
                        text, keyboard = _edit_aliases_content(session, details, state)
                    else:
                        state["phase"] = "edit_menu"
                        text, keyboard = _edit_menu_content(session, details)
                    notice = notice or (
                        tr("admin.render_phase.situaciya_izmenilas_obnovite")
                    )
                else:
                    text, keyboard = content
        elif phase == "edit_aliases":
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.mesto_suschestvuet_bylo_udaleno"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not can_manage_place(session, user_id, details.place_id):
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.dostup_etomu_mestu_podtverzhden"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                text, keyboard = _edit_aliases_content(session, details, state)
        elif phase in {
            "transfer_username_input",
            "transfer_candidates",
            "transfer_confirm_known",
            "transfer_confirm_invite",
            "transfer_outgoing",
        }:
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.mesto_suschestvuet_bylo_udaleno"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not is_place_admin(session, user_id, details.place_id):
                state.clear()
                state.update(_new_state())
                notice = notice or (
                    tr("admin.render_phase.peredat_adminku_mozhet_tolko")
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                active = get_active_place_admin(session, details.place_id)
                active_user = active.user if active is not None else None
                if phase == "transfer_username_input":
                    text, keyboard = _transfer_username_content(details, active_user)
                elif phase == "transfer_candidates":
                    raw_ids = state.get("transfer_candidate_ids")
                    ids = (
                        [
                            int(value)
                            for value in raw_ids
                            if isinstance(value, int) and not isinstance(value, bool)
                        ]
                        if isinstance(raw_ids, list)
                        else []
                    )
                    users = (
                        {
                            user.telegram_user_id: user
                            for user in session.scalars(
                                select(User).where(User.telegram_user_id.in_(ids))
                            ).all()
                        }
                        if ids
                        else {}
                    )
                    candidates = tuple(
                        TransferUser(
                            user_id=users[tg].id,
                            telegram_user_id=users[tg].telegram_user_id,
                            username=users[tg].username,
                            full_name=users[tg].full_name,
                        )
                        for tg in ids
                        if tg in users
                    )
                    if not candidates:
                        state["phase"] = "transfer_username_input"
                        notice = notice or tr(
                            "admin.render_phase.vyberite_poluchatelya_zanovo"
                        )
                        text, keyboard = _transfer_username_content(
                            details, active_user
                        )
                    else:
                        text, keyboard = _transfer_candidates_content(
                            details, candidates, state
                        )
                elif phase == "transfer_confirm_known":
                    content = _transfer_known_confirm_content(session, details, state)
                    if content is None:
                        state["phase"] = "transfer_username_input"
                        notice = notice or tr(
                            "admin.render_phase.poluchatel_bolshe_izvesten_botu"
                        )
                        text, keyboard = _transfer_username_content(
                            details, active_user
                        )
                    else:
                        text, keyboard = content
                elif phase == "transfer_confirm_invite":
                    if not state.get("transfer_username"):
                        state["phase"] = "transfer_username_input"
                        notice = notice or tr(
                            "admin.render_phase.vvedite_username_poluchatelya"
                        )
                        text, keyboard = _transfer_username_content(
                            details, active_user
                        )
                    else:
                        text, keyboard = _transfer_invite_confirm_content(
                            details, state
                        )
                else:
                    content = _transfer_outgoing_content(
                        session, details, state, invite_url=invite_url
                    )
                    if content is None:
                        state["phase"] = "transfer_username_input"
                        notice = notice or (
                            tr("admin.render_phase.aktivnoy_peredachi_net_vvedite")
                        )
                        text, keyboard = _transfer_username_content(
                            details, active_user
                        )
                    else:
                        text, keyboard = content
        elif phase in {"proposal_global_list", "proposal_places_list"}:
            place_filter = state.get("proposal_place_filter")
            if (
                phase == "proposal_places_list"
                and not isinstance(place_filter, int)
                and access_kind != "superadmin"
            ):
                state.clear()
                state.update(_new_state())
                notice = notice or (
                    tr("admin.render_phase.svodnaya_ochered_predlozheniy_mest")
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                try:
                    text, keyboard = _proposal_admin_list_content(
                        session, user_id, state
                    )
                except ProposalPermissionError as exc:
                    state.clear()
                    state.update(_new_state())
                    notice = notice or str(exc)
                    text, keyboard = _places_content(
                        session, state, access_kind, user_id
                    )
        elif phase in {
            "conflicts_list",
            "conflict_card",
            "conflict_rename_input",
            "conflict_switch_confirm",
        }:
            place_for_conflicts = state.get("place_id")
            conflict_id = state.get("conflict_id")
            view = (
                get_conflict(session, user_id, conflict_id)
                if isinstance(conflict_id, int) and not isinstance(conflict_id, bool)
                else None
            )
            if not isinstance(place_for_conflicts, int) or not can_manage_place(
                session, user_id, place_for_conflicts
            ):
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.dostup_etomu_mestu_podtverzhden"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif phase == "conflicts_list":
                text, keyboard = _conflicts_list_content(
                    session, user_id, view, place_for_conflicts, state
                )
            elif view is None:
                state["phase"] = "conflicts_list"
                notice = notice or tr("admin.render_phase.zadacha_konflikta_nedostupna")
                text, keyboard = _conflicts_list_content(
                    session, user_id, None, place_for_conflicts, state
                )
            elif phase == "conflict_card":
                text, keyboard = _conflict_card_content(view)
            elif phase == "conflict_rename_input":
                if not view.is_open:
                    state["phase"] = "conflict_card"
                    notice = notice or tr("admin.render_phase.zadacha_reshena")
                    text, keyboard = _conflict_card_content(view)
                else:
                    text, keyboard = _conflict_rename_input_content(view)
            else:
                if not view.is_open:
                    state["phase"] = "conflict_card"
                    notice = notice or tr("admin.render_phase.zadacha_reshena")
                    text, keyboard = _conflict_card_content(view)
                else:
                    text, keyboard = _conflict_switch_confirm_content(view)
        elif phase in {
            "proposal_card",
            "proposal_approve_confirm",
            "proposal_reject_confirm",
        }:
            try:
                proposal_id = int(state.get("proposal_id") or 0)
            except (TypeError, ValueError):
                proposal_id = 0
            proposal = (
                get_admin_proposal(session, user_id, proposal_id)
                if proposal_id > 0
                else None
            )
            if proposal is None:
                state.clear()
                state.update(_new_state())
                notice = notice or (
                    tr("admin.render_phase.predlozhenie_nedostupno_ono")
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif phase == "proposal_card":
                text, keyboard = _proposal_admin_card_content(proposal)
            elif proposal.status != PROPOSAL_STATUS_PENDING:
                state["phase"] = "proposal_card"
                notice = notice or tr(
                    "admin.render_phase.predlozhenie_ozhidaet_resheniya"
                )
                text, keyboard = _proposal_admin_card_content(proposal)
            elif phase == "proposal_approve_confirm":
                text, keyboard = _proposal_approve_confirm_content(proposal)
            else:
                text, keyboard = _proposal_reject_confirm_content(proposal)
        elif phase in {"brick_list", "brick_card", "brick_description_input"}:
            brick_text, brick_keyboard = _brick_phase_content(
                session,
                state,
                phase,
                access_kind=access_kind,
                user_id=user_id,
            )
            # Контент возвращается парой: доступ не подтверждён — оба None.
            if brick_text is None or brick_keyboard is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr("admin.render_phase.kirpichik_nedostupen_roley")
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                text, keyboard = brick_text, brick_keyboard
        else:  # confirm_*
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or tr(
                    "admin.render_phase.mesto_suschestvuet_bylo_udaleno"
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                confirmation = _confirmation_content(session, details, state, phase)
                if confirmation is None:
                    state["phase"] = "place"
                    notice = notice or (
                        tr("admin.render_phase.situaciya_izmenilas_obnovite_vybor")
                    )
                    text = _place_text(
                        details,
                        state,
                        session,
                        viewer_telegram_user_id=user_id,
                    )
                    keyboard = _place_keyboard(
                        details,
                        session,
                        access_kind=access_kind,
                        viewer_telegram_user_id=user_id,
                    )
                else:
                    text, keyboard = confirmation
    finally:
        session.close()

    return await _show(
        update,
        context,
        state,
        text,
        keyboard,
        notice=notice,
        cleanup_ids=cleanup_ids,
        drop_input=drop_input,
    )


async def _stale(update: Update, text: str = STALE_TEXT) -> None:
    await ctx.query(update).answer(text, show_alert=True)


async def open_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вход в кабинет: /admin, кнопка меню или восстановление после рестарта."""
    query = getattr(update, "callback_query", None)
    await screen.reanchor_screen(update, context)
    # R1, дефект №23: кабинет — вход в актуальный UI; снимаем reply-клавиатуру
    # старых версий, если она «прилипла» у клиента.
    await screen.drop_legacy_reply_keyboard(update, context)
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        allowed = can_access_admin_panel(session, user_id)
    except Exception as e:  # noqa: BLE001 — отказ безопаснее открытия
        logger.error("Не удалось проверить доступ в кабинет: %s", e)
        allowed = False
    finally:
        session.close()

    if not allowed:
        clear_admin_session(user_id)
        if query is not None:
            await query.answer(DENIED_ALERT, show_alert=True)
            return
        await _render_denied(update, context)
        await screen.drop_user_input(update, context)
        return

    if query is not None:
        await query.answer()
    clear_place_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)
    state = load_admin_session(user_id)
    if not isinstance(state, dict) or state.get("phase") not in PHASES:
        state = _new_state()
    # Явный вход в кабинет — снова «список кабинета»: запомненный возврат в
    # справочник от прежней карточки больше не действует.
    state.pop("return_to", None)
    state.pop("manage_page", None)
    state.pop("manage_phase", None)
    state.pop("manage_query", None)
    await _render_phase(update, context, state, drop_input=True)


async def open_place_admin_card_from_manage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    place_id: int,
    *,
    manage_page: int = 0,
    manage_phase: str = "manage_list",
    manage_query: str | None = None,
) -> None:
    """
    «Управление → Площадки» → тап по площадке: сразу полная карточка места.

    Poster 2.4.1 T2: промежуточная карточка справочника для управляющего
    больше не показывается. Допуск — управляющий ИМЕННО этого места
    (`can_manage_place`) или суперадминистратор; права перепроверяются по БД
    здесь, а не по факту нажатия кнопки (IDOR).

    `return_to`/`manage_page` в состоянии кабинета помнят, что пользователь
    пришёл из справочника: «◀️ К списку мест» вернёт его туда же, на ту же
    страницу, а не в список кабинета.

    Poster 2.5 §15: `manage_phase`/`manage_query` дополнительно помнят, что
    карточку открыли из результатов поиска, — возврат восстанавливает тот же
    поиск и ту же страницу. Состояние кабинета переживает рестарт.
    """
    query = ctx.query(update)
    user_id = ctx.user(update).id
    allowed = False
    session = SessionLocal()
    try:
        allowed = can_manage_place(session, user_id, place_id)
        exists = get_place_details(session, place_id) is not None
    except Exception as e:  # noqa: BLE001 — отказ безопаснее открытия
        logger.error("Не удалось проверить доступ к карточке места: %s", e)
        allowed = False
        exists = False
    finally:
        session.close()
    if not exists:
        await query.answer(
            tr("alerts.admin.mesto_suschestvuet_udaleno"), show_alert=True
        )
        return
    if not allowed:
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    await query.answer()
    clear_place_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)
    state = _new_state()
    state["phase"] = "place"
    state["place_id"] = place_id
    state["return_to"] = "manage"
    state["manage_page"] = max(int(manage_page), 0)
    if manage_phase == "manage_search_results" and isinstance(manage_query, str):
        if manage_query:
            state["manage_phase"] = "manage_search_results"
            state["manage_query"] = manage_query
    save_admin_session(user_id, state)
    await _render_phase(update, context, state, drop_input=True)


async def handle_invite_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    «🔗 Ссылка для создания анонса» в полной карточке места.

    Права проверяются по БД заново: подделанная кнопка по чужой площадке
    получает отказ, и ссылка не выдаётся. Повторное открытие не создаёт
    дублей — `ensure_invite` возвращает действующую ссылку.
    """
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "place":
        await _stale(update)
        return
    place_id = _proposal_callback_id(str(query.data or ""))
    if place_id is None or state.get("place_id") != place_id:
        await _stale(update)
        return
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        details = get_place_details(session, place_id)
        allowed = details is not None and can_manage_place(session, user_id, place_id)
    finally:
        session.close()
    if details is None:
        await query.answer(
            tr("alerts.admin.mesto_suschestvuet_udaleno"), show_alert=True
        )
        return
    if not allowed:
        await query.answer(tr("places.invite.no_rights"), show_alert=True)
        return
    await query.answer()
    state["phase"] = "invite"
    state.pop("invite_regenerated", None)
    await _render_phase(update, context, state)


async def handle_invite_regenerate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«🔁 Перевыпустить»: старый токен отзывается, выдаётся новый."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "invite":
        await _stale(update)
        return
    place_id = _proposal_callback_id(str(query.data or ""))
    if place_id is None or state.get("place_id") != place_id:
        await _stale(update)
        return
    user_id = ctx.user(update).id
    from place_invite_store import PlaceInvitePermissionError, regenerate_invite

    session = SessionLocal()
    details = None
    refused = True
    try:
        details = get_place_details(session, place_id)
        if details is not None and can_manage_place(session, user_id, place_id):
            regenerate_invite(
                session, actor_telegram_user_id=user_id, place_id=place_id
            )
            refused = False
    except PlaceInvitePermissionError:
        refused = True
    finally:
        session.close()
    if refused:
        await query.answer(
            (
                tr("places.invite.no_rights")
                if details is not None
                else tr("alerts.admin.mesto_suschestvuet_udaleno")
            ),
            show_alert=True,
        )
        return
    await query.answer(tr("alerts.places.ssylka_perevypuschena"))
    state["invite_regenerated"] = True
    await _render_phase(update, context, state)


async def handle_invite_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«◀️ К карточке» с экрана ссылки: возврат в полную карточку места."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "invite":
        await _stale(update)
        return
    await query.answer()
    state.pop("invite_regenerated", None)
    state["phase"] = "place"
    await _render_phase(update, context, state)


async def handle_place_admins_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Poster 2.5 A3: «👤 Управлять администратором» — служебный подраздел.

    Второго механизма нет: внутри живут существующие `adm_replace`,
    `adm_revoke` (суперадмин) и `adm_transfer` (админ места).
    """
    query = ctx.query(update)
    user_id = ctx.user(update).id
    state = load_admin_session(user_id)
    if state is None or state.get("phase") not in PLACE_ACTION_PHASES:
        await _stale(update)
        return
    place_id = _proposal_callback_id(str(query.data or ""))
    state_place_id = state.get("place_id")
    if (
        place_id is None
        or not isinstance(state_place_id, int)
        or state_place_id != place_id
    ):
        await _stale(update)
        return
    session = SessionLocal()
    try:
        allowed = can_manage_place(session, user_id, place_id)
    finally:
        session.close()
    if not allowed:
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    await query.answer()
    state["phase"] = "place_admins"
    await _render_phase(update, context, state)


async def handle_place_admins_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«◀️ К карточке места»: возврат из подраздела администратора."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "place_admins":
        await _stale(update)
        return
    await query.answer()
    state["phase"] = "place"
    await _render_phase(update, context, state)


async def handle_places_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "places":
        await _stale(update)
        return
    data = query.data or ""
    try:
        offset = int(data[len("adm_page:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    await query.answer()
    state["page"] = max(offset, 0)
    await _render_phase(update, context, state)


async def handle_open_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "places",
        # U3b: «◀️ К месту» из списка конфликтов кодов — живой возврат.
        # Раньше эта кнопка получала «Кнопка устарела»: фаза `conflicts_list`
        # не входила в allow-list, хотя экран предлагает такой переход.
        "conflicts_list",
        # Poster 2.5 A4 (§13): «◀️ Назад» из списка кирпичиков площадки ведёт
        # в административную карточку ЭТОЙ площадки. Раньше фаза `brick_list`
        # отсутствовала в allow-list, и рабочая кнопка отвечала «Кнопка
        # устарела»/«Откройте раздел администрирования заново».
        "brick_list",
    }:
        await _stale(update)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("adm_place:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        allowed = can_access_admin_panel(session, ctx.user(update).id)
        manages = allowed and can_manage_place(session, ctx.user(update).id, place_id)
        details = get_place_details(session, place_id) if manages else None
    finally:
        session.close()
    if details is None:
        await query.answer(tr("alerts.admin.mesto_nedostupno"), show_alert=True)
        return
    await query.answer()
    state["place_id"] = place_id
    state["phase"] = "place"
    # Вход из списка кабинета: «◀️ К списку мест» вернёт в кабинет, а не в
    # справочник «Управление → Площадки» (Poster 2.4.1 T2). Возврат из
    # подраздела кирпичиков тоже идёт в кабинетную карточку, поэтому
    # контекст справочника сбрасывается в любом случае.
    state.pop("return_to", None)
    state.pop("manage_page", None)
    state.pop("manage_phase", None)
    state.pop("manage_query", None)
    # Область кирпичиков привязана к площадке: после возврата в карточку
    # она не должна переживать переход в другой подраздел.
    state.pop("brick_scope", None)
    state.pop("brick_place_id", None)
    state.pop("brick_id", None)
    await _render_phase(update, context, state)


async def handle_assign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in PLACE_ACTION_PHASES:
        await _stale(update)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("adm_assign:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    if state.get("place_id") != place_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        if get_place_details(session, place_id) is None:
            await query.answer(tr("alerts.admin.mesto_naydeno"), show_alert=True)
            return
        if get_active_place_admin(session, place_id) is not None:
            await query.answer(
                tr("alerts.admin.u_mesta_est_administrator"),
                show_alert=True,
            )
            return
    finally:
        session.close()
    await query.answer()
    state["mode"] = "assign"
    state.pop("expected_admin_telegram_user_id", None)
    state["phase"] = "assign_search"
    await _render_phase(update, context, state)


async def handle_replace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in PLACE_ACTION_PHASES:
        await _stale(update)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("adm_replace:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    if state.get("place_id") != place_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        details = get_place_details(session, place_id)
        active = get_active_place_admin(session, place_id)
    finally:
        session.close()
    if details is None or active is None:
        await query.answer(
            tr("alerts.admin.administrator_izmenilsya_obnovite"), show_alert=True
        )
        return
    await query.answer()
    state["mode"] = "replace"
    state["expected_admin_telegram_user_id"] = active.user.telegram_user_id
    state["phase"] = "assign_search"
    await _render_phase(update, context, state)


async def handle_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in PLACE_ACTION_PHASES:
        await _stale(update)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("adm_revoke:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    if state.get("place_id") != place_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        details = get_place_details(session, place_id)
        active = get_active_place_admin(session, place_id)
    finally:
        session.close()
    if details is None or active is None:
        await query.answer(
            tr("alerts.admin.administrator_izmenilsya_obnovite"), show_alert=True
        )
        return
    await query.answer()
    state["expected_admin_telegram_user_id"] = active.user.telegram_user_id
    state["phase"] = "confirm_revoke"
    await _render_phase(update, context, state)


async def handle_search_again(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {"assign_results", "assign_search"}:
        await _stale(update)
        return
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    await query.answer()
    state["phase"] = "assign_search"
    state.pop("query", None)
    state.pop("query_kind", None)
    await _render_phase(update, context, state)


async def handle_pick_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "assign_results":
        await _stale(update)
        return
    data = query.data or ""
    try:
        telegram_user_id = int(data[len("adm_pick:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    query_text = state.get("query")
    place_id = state.get("place_id")
    if not isinstance(query_text, str) or not isinstance(place_id, int):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        results = search_known_users(session, query_text)
        visible_ids = {user.telegram_user_id for user in results.users}
        if telegram_user_id not in visible_ids:
            await _stale(update)
            return
        details = get_place_details(session, place_id)
        active = get_active_place_admin(session, place_id)
    finally:
        session.close()
    if details is None:
        await query.answer(tr("alerts.admin.mesto_suschestvuet"), show_alert=True)
        return

    mode = state.get("mode") or "assign"
    if mode == "replace":
        expected = state.get("expected_admin_telegram_user_id")
        if active is None or active.user.telegram_user_id != expected:
            await query.answer(
                tr("alerts.admin.administrator_izmenilsya_vyberite"),
                show_alert=True,
            )
            state["phase"] = "place"
            await _render_phase(update, context, state)
            return
    elif active is not None:
        await query.answer(
            tr("alerts.admin.u_mesta_poyavilsya_administrator"),
            show_alert=True,
        )
        state["phase"] = "place"
        await _render_phase(update, context, state)
        return

    await query.answer()
    state["target_telegram_user_id"] = telegram_user_id
    state["phase"] = "confirm_replace" if mode == "replace" else "confirm_assign"
    await _render_phase(update, context, state)


def _mutation_notice(result: AdminMutationResult) -> str:
    if result.status == "assigned":
        return tr("admin.mutation_notice.administrator_naznachen")
    if result.status == "replaced":
        return tr("admin.mutation_notice.administrator_zamenen")
    if result.status == "revoked":
        return tr("admin.mutation_notice.administrator_snyat")
    if result.status == "already_admin":
        return tr("admin.mutation_notice.etot_polzovatel_upravlyaet_etim")
    if result.status == "has_admin":
        return tr("admin.mutation_notice.naznachenie_vypolneno_u_mesta")
    if result.status == "user_missing":
        return tr("admin.mutation_notice.poluchatel_bolshe_izvesten_botu")
    if result.status == "place_missing":
        return tr("admin.render_phase.mesto_suschestvuet_bylo_udaleno")
    return tr("admin.mutation_notice.dannye_izmenilis_vremya")


async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    user = ctx.user(update)
    if not is_superadmin(user.id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(user.id)
    if state is None or state.get("phase") not in {
        "confirm_assign",
        "confirm_replace",
        "confirm_revoke",
    }:
        await _stale(update)
        return
    place_id = state.get("place_id")
    if not isinstance(place_id, int):
        await _stale(update)
        return
    await query.answer()

    phase = state.get("phase")
    target = state.get("target_telegram_user_id")
    expected = state.get("expected_admin_telegram_user_id")
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        if phase == "confirm_revoke":
            if not isinstance(expected, int):
                await _stale(update)
                return
            result = revoke_place_admin(
                session,
                actor_telegram_user_id=user.id,
                place_id=place_id,
                expected_admin_telegram_user_id=expected,
            )
        elif phase == "confirm_replace":
            if not isinstance(target, int) or not isinstance(expected, int):
                await _stale(update)
                return
            result = replace_place_admin(
                session,
                actor_telegram_user_id=user.id,
                place_id=place_id,
                target_telegram_user_id=target,
                expected_admin_telegram_user_id=expected,
            )
        else:
            if not isinstance(target, int):
                await _stale(update)
                return
            result = assign_place_admin(
                session,
                actor_telegram_user_id=user.id,
                place_id=place_id,
                target_telegram_user_id=target,
            )
    except AdminPermissionError:
        clear_admin_session(user.id)
        await _render_denied(update, context)
        return
    except Exception as e:  # noqa: BLE001 — экран остаётся рабочим
        logger.error("Не удалось изменить администратора места: %s", e)
        state["phase"] = "place"
        await _render_phase(
            update,
            context,
            state,
            notice=tr("admin.handle_confirm.ne_udalos_izmenit_administratora"),
        )
        return
    finally:
        session.close()

    if result.status == "place_missing":
        state.clear()
        state.update(_new_state())
    else:
        state["phase"] = "place"
    for key in (
        "target_telegram_user_id",
        "expected_admin_telegram_user_id",
        "mode",
        "query",
        "query_kind",
    ):
        state.pop(key, None)
    await _render_phase(update, context, state, notice=_mutation_notice(result))


# --- Редактирование данных места (Stage 20B) ----------------------------------


EDIT_STATE_KEYS = (
    "edit_value",
    "edit_warning",
    "edit_expected",
    "edit_target_name_id",
    "edit_target_name",
)

EDIT_INPUT_PHASES = frozenset(
    {
        "edit_name_input",
        "edit_alias_add_input",
        "edit_alias_rename_input",
        "edit_address_input",
        "edit_city_input",
        "edit_url_input",
    }
)

EDIT_CANCEL_TO_MENU_PHASES = frozenset(
    {
        "edit_name_input",
        "edit_name_confirm",
        "edit_address_input",
        "edit_address_confirm",
        "edit_address_delete_confirm",
        "edit_city_input",
        "edit_city_confirm",
        "edit_url_input",
        "edit_url_confirm",
        "edit_url_delete_confirm",
    }
)

EDIT_CANCEL_TO_ALIASES_PHASES = frozenset(
    {
        "edit_alias_add_input",
        "edit_alias_add_confirm",
        "edit_alias_rename_input",
        "edit_alias_rename_confirm",
        "edit_alias_delete_confirm",
    }
)


# --- Передача администратора места (Stage 21) --------------------------------

TRANSFER_STATE_KEYS = (
    "transfer_username",
    "transfer_candidate_ids",
    "transfer_target_telegram_user_id",
)

TRANSFER_INPUT_PHASES = frozenset({"transfer_username_input"})

TRANSFER_CANCEL_PHASES = frozenset(
    {
        "transfer_username_input",
        "transfer_candidates",
        "transfer_confirm_known",
        "transfer_confirm_invite",
        "transfer_outgoing",
    }
)


def _clear_transfer_keys(state: dict) -> None:
    """Убирает подсказки формы передачи; на права и предложение не влияет."""
    for key in TRANSFER_STATE_KEYS:
        state.pop(key, None)


def _clear_edit_keys(state: dict) -> None:
    """Сбрасывает промежуточные значения редактирования (не роль и не место)."""
    for key in EDIT_STATE_KEYS:
        state.pop(key, None)


def _find_alias(session: Session, place_id: int, name_id: int) -> AliasRow | None:
    """Актуальная строка альтернативного названия места по id."""
    for alias in list_place_aliases(session, place_id):
        if alias.name_id == name_id:
            return alias
    return None


async def _edit_transition(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    allowed_phase: str,
) -> tuple[CallbackQuery, dict] | None:
    """
    Общий префикс обработчиков редактирования: фаза + свежая проверка прав.

    Роль проверяется по effective_user: суперадминистратор или действующий
    администратор ИМЕННО этого места. Callback не передаёт роль.
    """
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != allowed_phase:
        await _stale(update)
        return None
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        allowed = isinstance(place_id, int) and can_manage_place(
            session, ctx.user(update).id, place_id
        )
    finally:
        session.close()
    if not allowed:
        await query.answer(DENIED_ALERT, show_alert=True)
        return None
    return query, state


async def handle_edit_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «✏️ Редактировать место» в карточке места."""
    result = await _edit_transition(update, context, allowed_phase="place")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["phase"] = "edit_menu"
    await _render_phase(update, context, state)


async def handle_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["phase"] = "edit_name_input"
    await _render_phase(update, context, state)


async def handle_edit_address(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["phase"] = "edit_address_input"
    await _render_phase(update, context, state)


async def handle_edit_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«🏙 Изменить город»: переход к вводу города (Poster 2.0 U1a)."""
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["phase"] = "edit_city_input"
    await _render_phase(update, context, state)


async def handle_edit_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["phase"] = "edit_url_input"
    await _render_phase(update, context, state)


async def handle_edit_address_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        details = (
            get_place_details(session, int(place_id))
            if isinstance(place_id, int)
            else None
        )
    finally:
        session.close()
    if details is None or details.address is None:
        # Адрес уже удалён или отсутствует: предлагаем обновить меню.
        await query.answer(
            tr("alerts.admin.adres_otsutstvuet_obnovite_menyu"),
            show_alert=True,
        )
        return
    # Фиксируем текущий адрес: подтверждение сработает только если
    # другой администратор не изменил его в процессе.
    state["edit_expected"] = details.address
    state["phase"] = "edit_address_delete_confirm"
    await _render_phase(update, context, state)


async def handle_edit_url_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        details = (
            get_place_details(session, int(place_id))
            if isinstance(place_id, int)
            else None
        )
    finally:
        session.close()
    if details is None or details.url is None:
        # Ссылка уже удалена или отсутствует: предлагаем обновить меню.
        await query.answer(
            tr("alerts.admin.ssylka_otsutstvuet_obnovite_menyu"),
            show_alert=True,
        )
        return
    # Фиксируем текущую ссылку: подтверждение сработает только если
    # другой администратор не изменил её в процессе.
    state["edit_expected"] = details.url
    state["phase"] = "edit_url_delete_confirm"
    await _render_phase(update, context, state)


async def handle_aliases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «📝 Альтернативные названия» в меню редактирования."""
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["alias_page"] = 0
    state["phase"] = "edit_aliases"
    await _render_phase(update, context, state)


async def handle_alias_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_aliases")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["phase"] = "edit_alias_add_input"
    await _render_phase(update, context, state)


async def handle_alias_rename(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    result = await _edit_transition(update, context, allowed_phase="edit_aliases")
    if result is None:
        return
    state = result[1]
    data = query.data or ""
    try:
        name_id = int(data[len("adm_alias_rename:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        alias = (
            _find_alias(session, int(place_id), name_id)
            if isinstance(place_id, int)
            else None
        )
    finally:
        session.close()
    if alias is None:
        await query.answer(
            tr("alerts.admin.nazvanie_izmeneno_udaleno"), show_alert=True
        )
        return
    await query.answer()
    _clear_edit_keys(state)
    state["edit_target_name_id"] = alias.name_id
    state["edit_target_name"] = alias.name
    state["phase"] = "edit_alias_rename_input"
    await _render_phase(update, context, state)


async def handle_alias_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    result = await _edit_transition(update, context, allowed_phase="edit_aliases")
    if result is None:
        return
    state = result[1]
    data = query.data or ""
    try:
        name_id = int(data[len("adm_alias_delete:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        alias = (
            _find_alias(session, int(place_id), name_id)
            if isinstance(place_id, int)
            else None
        )
    finally:
        session.close()
    if alias is None:
        await query.answer(
            tr("alerts.admin.nazvanie_izmeneno_udaleno"), show_alert=True
        )
        return
    await query.answer()
    _clear_edit_keys(state)
    state["edit_target_name_id"] = alias.name_id
    state["edit_target_name"] = alias.name
    state["phase"] = "edit_alias_delete_confirm"
    await _render_phase(update, context, state)


async def handle_alias_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "edit_aliases":
        await _stale(update)
        return
    data = query.data or ""
    try:
        offset = int(data[len("adm_alias_page:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    await query.answer()
    state["alias_page"] = max(offset, 0)
    await _render_phase(update, context, state)


def _validate_edit_input(
    session: Session,
    details: PlaceDetails,
    phase: str,
    raw: str,
    *,
    rename_name_id: int | None = None,
) -> tuple[str, str, str]:
    """
    Валидирует текстовый ввод фаз редактирования места.

    Возвращает (next_phase, value, warning). Ошибки PlaceNameError и
    PlaceEditValueError несут понятное сообщение. Похожие названия других
    мест возвращаются предупреждением — на экране подтверждения.
    """
    if phase == "edit_address_input":
        cleaned = clean_address_text(raw)
        if cleaned == details.address:
            raise PlaceEditValueError(tr("admin.validate_edit_input.adres_uzhe_takoy"))
        return "edit_address_confirm", cleaned, ""
    if phase == "edit_city_input":
        cleaned_city = validate_place_city(raw)
        if cleaned_city == details.city:
            raise PlaceEditValueError(tr("admin.validate_edit_input.gorod_uzhe_takoy"))
        return "edit_city_confirm", cleaned_city, ""
    if phase == "edit_url_input":
        normalized = normalize_place_url(raw)
        if normalized == details.url:
            raise PlaceEditValueError(
                tr("admin.validate_edit_input.ssylka_uzhe_takaya")
            )
        return "edit_url_confirm", normalized, ""

    what = {
        "edit_name_input": tr("admin.validate_edit_input.nazvanie_mesta"),
        "edit_alias_add_input": tr("places.handle_place.alternativnoe_nazvanie"),
        "edit_alias_rename_input": tr("places.handle_place.alternativnoe_nazvanie"),
    }[phase]
    display = validate_place_name(raw, what=what)
    needle = normalize_place_name(display)

    aliases = list_place_aliases(session, details.place_id)
    if phase == "edit_name_input":
        if needle == normalize_place_name(details.canonical_name):
            raise PlaceEditValueError(
                tr("admin.validate_edit_input.nazvanie_uzhe_kanonicheskoe")
            )
        existing_names = [alias.name for alias in aliases]
    elif phase == "edit_alias_add_input":
        existing_names = [details.canonical_name] + [alias.name for alias in aliases]
    else:
        target = None
        for alias in aliases:
            if alias.name_id == rename_name_id:
                target = alias
                break
        if target is None:
            raise PlaceEditValueError(
                tr("admin.validate_edit_input.nazvanie_uzhe_izmeneno_udaleno")
            )
        existing_names = [details.canonical_name] + [
            alias.name for alias in aliases if alias.name_id != rename_name_id
        ]
    for name in existing_names:
        if normalize_place_name(name) == needle:
            raise PlaceEditValueError(
                tr("admin.validate_edit_input.takoe_nazvanie_uzhe_est_u", name=name)
            )

    conflicts = find_name_conflicts(session, display, exclude_place_id=details.place_id)
    warning = ""
    if conflicts:
        other = conflicts[0]
        warning = tr(
            "admin.validate_edit_input.pohozhee_nazvanie_u_drugogo",
            matched_name=other.matched_name,
            canonical_name=other.canonical_name,
        )
        if len(conflicts) > 1:
            warning += tr(
                "admin.validate_edit_input.pohozhih_mest",
                count_conflicts=len(conflicts),
            )

    next_phase = {
        "edit_name_input": "edit_name_confirm",
        "edit_alias_add_input": "edit_alias_add_confirm",
        "edit_alias_rename_input": "edit_alias_rename_confirm",
    }[phase]
    return next_phase, display, warning


async def handle_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текст в фазах редактирования места: валидация и переход к подтверждению."""
    user = ctx.user(update)
    state = load_admin_session(user.id)
    if state is None or state.get("phase") not in EDIT_INPUT_PHASES:
        return
    phase = str(state.get("phase"))
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        if not isinstance(place_id, int) or not can_manage_place(
            session, user.id, place_id
        ):
            clear_admin_session(user.id)
            await _render_denied(update, context)
            return
        details = get_place_details(session, place_id)
        if details is None:
            state.clear()
            state.update(_new_state())
            await _render_phase(
                update,
                context,
                state,
                notice=tr("admin.render_phase.mesto_suschestvuet_bylo_udaleno"),
                drop_input=True,
            )
            return
        raw = ctx.message(update).text or ""
        try:
            next_phase, value, warning = _validate_edit_input(
                session,
                details,
                phase,
                raw,
                rename_name_id=(
                    state.get("edit_target_name_id")
                    if isinstance(state.get("edit_target_name_id"), int)
                    else None
                ),
            )
        except (PlaceNameError, PlaceEditValueError) as e:
            cleanup = []
            message = getattr(update, "message", None)
            if message is not None and message.message_id is not None:
                cleanup.append(int(message.message_id))
            await _render_phase(
                update,
                context,
                state,
                notice=str(e),
                cleanup_ids=cleanup,
                drop_input=True,
            )
            return
    finally:
        session.close()

    state["edit_value"] = value
    if warning:
        state["edit_warning"] = warning
    else:
        state.pop("edit_warning", None)

    # Фиксируем старое значение поля на момент ввода: подтверждение пройдёт
    # только если другой администратор не изменил поле в процессе (гонка
    # обнаруживается хранилищем по ожидаемому значению).
    expected_map = {
        "edit_name_confirm": details.canonical_name,
        "edit_address_confirm": details.address,
        "edit_city_confirm": details.city,
        "edit_url_confirm": details.url,
    }
    if next_phase in expected_map:
        state["edit_expected"] = expected_map[next_phase]
    else:
        state.pop("edit_expected", None)

    state["phase"] = next_phase
    cleanup = _failed_take(state)
    message = getattr(update, "message", None)
    if message is not None and message.message_id is not None:
        cleanup.append(int(message.message_id))
    await _render_phase(update, context, state, cleanup_ids=cleanup, drop_input=True)


def _edit_result_notice(
    result: EditMutationResult, phase: str
) -> tuple[str, str | None]:
    """Сообщение о результате мутации и фаза, куда вернуться."""
    alias_phase = phase.startswith("edit_alias")
    ok_phase = "edit_aliases" if alias_phase else "edit_menu"
    if result.status == "ok":
        if phase == "edit_name_confirm":
            return tr("admin.edit_result_notice.nazvanie_mesta_izmeneno"), "place"
        if phase == "edit_alias_add_confirm":
            return (
                tr("admin.edit_result_notice.alternativnoe_nazvanie_dobavleno"),
                "edit_aliases",
            )
        if phase == "edit_alias_rename_confirm":
            return tr("admin.edit_result_notice.alternativnoe_nazvanie"), "edit_aliases"
        if phase == "edit_alias_delete_confirm":
            return (
                tr("admin.edit_result_notice.alternativnoe_nazvanie_udaleno"),
                "edit_aliases",
            )
        if phase == "edit_address_confirm":
            return tr("admin.edit_result_notice.adres_mesta_obnovlen"), "edit_menu"
        if phase == "edit_address_delete_confirm":
            return tr("admin.edit_result_notice.adres_mesta_udalen"), "edit_menu"
        if phase == "edit_city_confirm":
            return tr("admin.edit_result_notice.gorod_mesta_obnovlen"), "edit_menu"
        if phase == "edit_url_confirm":
            return tr("admin.edit_result_notice.ssylka_mesta_obnovlena"), "edit_menu"
        return tr("admin.edit_result_notice.ssylka_mesta_udalena"), "edit_menu"
    if result.status == "unchanged":
        return tr("admin.edit_result_notice.znachenie_bylo_takim_izmeneniy"), ok_phase
    if result.status == "changed":
        return (tr("admin.edit_result_notice.znachenie_izmenilos_vremya")), ok_phase
    if result.status == "duplicate":
        return tr("admin.edit_result_notice.takoe_nazvanie_est_u"), ok_phase
    if result.status == "not_found":
        return (
            tr("admin.edit_result_notice.nazvanie_izmeneno_udaleno_drugim")
        ), "edit_aliases"
    if result.status == "canonical":
        return (tr("admin.edit_result_notice.kanonicheskoe_nazvanie")), ok_phase
    if result.status == "conflict":
        return (
            tr("admin.edit_result_notice.nazvanie_pohozhe_nazvanie_drugogo")
        ), ok_phase
    return tr("admin.render_phase.mesto_suschestvuet_bylo_udaleno"), None


def _expected_field_from_state(state: dict, fresh_value: str | None) -> str | None:
    """
    Ожидаемое старое значение поля из состояния кабинета.

    Значение захватывается при входе в фазу подтверждения, поэтому гонка
    с другим администратором обнаруживается хранилищем (status "changed").
    Отсутствие ключа — состояние старой сессии: резерв на свежее значение.
    """
    if "edit_expected" not in state:
        return fresh_value
    value = state.get("edit_expected")
    if value is not None and not isinstance(value, str):
        return fresh_value
    return value


async def handle_edit_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Подтверждение изменения данных места: мутация атомарна с аудитом."""
    query = ctx.query(update)
    user = ctx.user(update)
    state = load_admin_session(user.id)
    if state is None or state.get("phase") not in {
        "edit_name_confirm",
        "edit_alias_add_confirm",
        "edit_alias_rename_confirm",
        "edit_alias_delete_confirm",
        "edit_address_confirm",
        "edit_address_delete_confirm",
        "edit_city_confirm",
        "edit_url_confirm",
        "edit_url_delete_confirm",
    }:
        await _stale(update)
        return
    place_id = state.get("place_id")
    if not isinstance(place_id, int):
        await _stale(update)
        return
    phase = str(state.get("phase"))
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        details = get_place_details(session, place_id)
        if details is None:
            state.clear()
            state.update(_new_state())
            await query.answer()
            await _render_phase(
                update,
                context,
                state,
                notice=tr("admin.render_phase.mesto_suschestvuet_bylo_udaleno"),
            )
            return
        actor_telegram_user_id = user.id
        try:
            if phase == "edit_name_confirm":
                expected_name = _expected_field_from_state(
                    state, details.canonical_name
                )
                if expected_name is None:
                    # каноническое название всегда существует; отсутствие
                    # захвата означает старое состояние сессии — берём свежее.
                    expected_name = details.canonical_name
                result = rename_canonical_place_name(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    new_name=str(state.get("edit_value") or ""),
                    expected_name=expected_name,
                    force=True,
                )
            elif phase == "edit_alias_add_confirm":
                result = add_place_alias(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    name=str(state.get("edit_value") or ""),
                    force=True,
                )
            elif phase == "edit_alias_rename_confirm":
                name_id = state.get("edit_target_name_id")
                result = rename_place_alias(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    name_id=name_id if isinstance(name_id, int) else -1,
                    new_name=str(state.get("edit_value") or ""),
                    expected_name=str(state.get("edit_target_name") or ""),
                    force=True,
                )
            elif phase == "edit_alias_delete_confirm":
                name_id = state.get("edit_target_name_id")
                result = delete_place_alias(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    name_id=name_id if isinstance(name_id, int) else -1,
                    expected_name=str(state.get("edit_target_name") or ""),
                )
            elif phase == "edit_address_confirm":
                result = set_place_address(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    address=str(state.get("edit_value") or ""),
                    expected_address=_expected_field_from_state(state, details.address),
                )
            elif phase == "edit_address_delete_confirm":
                result = delete_place_address(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    expected_address=_expected_field_from_state(state, details.address),
                )
            elif phase == "edit_city_confirm":
                result = set_place_city(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    city=str(state.get("edit_value") or ""),
                    expected_city=_expected_field_from_state(state, details.city),
                )
            elif phase == "edit_url_confirm":
                result = set_place_url(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    url=str(state.get("edit_value") or ""),
                    expected_url=_expected_field_from_state(state, details.url),
                )
            else:  # edit_url_delete_confirm
                result = delete_place_url(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    expected_url=_expected_field_from_state(state, details.url),
                )
        except PlaceEditPermissionError:
            clear_admin_session(user.id)
            await _render_denied(update, context)
            return
        except Exception as e:  # noqa: BLE001 — экран остаётся рабочим
            logger.error("Не удалось изменить данные места: %s", e)
            state["phase"] = "edit_menu"
            await query.answer()
            await _render_phase(
                update,
                context,
                state,
                notice=tr("admin.handle_edit_confirm.ne_udalos_izmenit_dannye"),
            )
            return
    finally:
        session.close()

    await query.answer()
    notice, next_phase = _edit_result_notice(result, phase)
    _clear_edit_keys(state)
    if result.status == "place_missing":
        state.clear()
        state.update(_new_state())
    elif next_phase is not None:
        state["phase"] = next_phase
    else:
        state["phase"] = "edit_menu"
    await _render_phase(update, context, state, notice=notice)


async def handle_places_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Возврат из карточки/поиска к списку мест."""
    query = ctx.query(update)
    user_id = ctx.user(update).id
    state = load_admin_session(user_id)
    if state is None or state.get("phase") not in PHASES - {"places"}:
        await _stale(update)
        return
    await query.answer()
    return_to_manage = state.get("return_to") == "manage"
    manage_page = state.get("manage_page")
    manage_phase = state.get("manage_phase")
    manage_query = state.get("manage_query")
    if return_to_manage:
        # Poster 2.4.1 T2: карточка открыта из справочника «Управление →
        # Площадки» — «К списку мест» возвращает туда же, на ту же страницу.
        # Poster 2.5 §15: если открывали из поиска — в тот же поиск.
        clear_admin_session(user_id)
        from handlers.places import open_manage_list

        await open_manage_list(
            update,
            context,
            page=manage_page if isinstance(manage_page, int) else 0,
            query=(
                manage_query
                if manage_phase == "manage_search_results"
                and isinstance(manage_query, str)
                else None
            ),
        )
        return
    state.clear()
    state.update(_new_state())
    await _render_phase(update, context, state)


async def handle_cancel_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Отмена поиска/подтверждения/редактирования: возврат на шаг назад."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    phase = state.get("phase") if state else None
    if state is None or phase not in (
        {
            "assign_search",
            "assign_results",
            "confirm_assign",
            "confirm_replace",
            "confirm_revoke",
            "edit_menu",
            "edit_aliases",
        }
        | EDIT_CANCEL_TO_MENU_PHASES
        | EDIT_CANCEL_TO_ALIASES_PHASES
        | TRANSFER_CANCEL_PHASES
    ):
        await _stale(update)
        return
    await query.answer()
    for key in (
        "target_telegram_user_id",
        "expected_admin_telegram_user_id",
        "mode",
        "query",
        "query_kind",
    ):
        state.pop(key, None)
    if phase in TRANSFER_CANCEL_PHASES:
        # Это именно выход с экрана, а не отмена активного предложения:
        # для отмены есть отдельная кнопка, которая меняет статус в БД.
        _clear_transfer_keys(state)
        if isinstance(state.get("place_id"), int):
            state["phase"] = "place"
        else:
            state.clear()
            state.update(_new_state())
    elif phase == "edit_menu":
        # Назад из меню редактирования к карточке места.
        _clear_edit_keys(state)
        if isinstance(state.get("place_id"), int):
            state["phase"] = "place"
        else:
            state.clear()
            state.update(_new_state())
    elif phase == "edit_aliases":
        # R1, дефект №22: кнопка «Назад» на списке альтернативных названий
        # ведёт к карточке места. Раньше фаза не входила в allow-list, и
        # кнопка выглядела устаревшей (плюс падение при отрисовке).
        _clear_edit_keys(state)
        state.pop("alias_page", None)
        if isinstance(state.get("place_id"), int):
            state["phase"] = "place"
        else:
            state.clear()
            state.update(_new_state())
    elif phase in EDIT_CANCEL_TO_MENU_PHASES:
        # Возврат из ввода/подтверждения к меню редактирования.
        _clear_edit_keys(state)
        state["phase"] = "edit_menu"
    elif phase in EDIT_CANCEL_TO_ALIASES_PHASES:
        # Возврат из алиасных фаз к списку альтернативных названий.
        _clear_edit_keys(state)
        state["phase"] = "edit_aliases"
    elif isinstance(state.get("place_id"), int):
        state["phase"] = "place"
    else:
        state.clear()
        state.update(_new_state())
    await _render_phase(update, context, state)


async def handle_cancel_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Отмена на экране подтверждения без изменения прав."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "confirm_assign",
        "confirm_replace",
        "confirm_revoke",
    }:
        await _stale(update)
        return
    await query.answer()
    for key in (
        "target_telegram_user_id",
        "expected_admin_telegram_user_id",
        "mode",
        "query",
        "query_kind",
    ):
        state.pop(key, None)
    state["phase"] = "place"
    await _render_phase(update, context, state)


async def handle_search_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Текст в фазе поиска получателя (только суперадминистратор)."""
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "assign_search":
        return
    if not is_superadmin(ctx.user(update).id):
        clear_admin_session(ctx.user(update).id)
        await _render_denied(update, context)
        return

    await screen.reanchor_screen(update, context)
    raw = ctx.message(update).text or ""
    _failed_add(state, update)
    parsed = _normalize_assign_query(raw)
    if parsed is None:
        # R1, дефект №21: пустой/несимвольный ввод — явная ошибка формата.
        # Фаза остаётся assign_search: пользователь сразу повторяет ввод.
        state.pop("query", None)
        state.pop("query_kind", None)
        await _render_phase(
            update,
            context,
            state,
            notice=ASSIGN_QUERY_INVALID_TEXT,
            cleanup_ids=_failed_take(state),
            drop_input=True,
        )
        return
    kind, value = parsed
    session = SessionLocal()
    try:
        search_known_users(session, value)
    except UserSearchError as e:
        state.pop("query", None)
        state.pop("query_kind", None)
        await _render_phase(
            update,
            context,
            state,
            notice=str(e),
            cleanup_ids=_failed_take(state),
            drop_input=True,
        )
        return
    finally:
        session.close()

    state["query"] = value
    state["query_kind"] = kind
    state["phase"] = "assign_results"
    cleanup = _failed_take(state)
    current = getattr(update, "message", None)
    current_id = getattr(current, "message_id", None)
    if current_id is not None:
        cleanup.append(int(current_id))
    await _render_phase(
        update,
        context,
        state,
        cleanup_ids=cleanup,
        drop_input=True,
    )


async def _transfer_state_place(
    update: Update, *, allowed_phases: frozenset[str]
) -> tuple[dict, int, str] | None:
    """
    Общий guard кнопок передачи для инициатора.

    Callback подтверждает только id; фазу и место берём из сохранённого
    состояния, а действующую роль перепроверяем по БД в обработчике.
    """
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in allowed_phases:
        await _stale(update)
        return None
    place_id = state.get("place_id")
    if not isinstance(place_id, int):
        await _stale(update)
        return None
    return state, place_id, str(state.get("phase"))


async def handle_transfer_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Кнопка «Передать админку»: активное предложение или ввод username."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in PLACE_ACTION_PHASES:
        await _stale(update)
        return
    state_place_id = state.get("place_id")
    data = str(query.data or "")
    try:
        callback_place_id = int(data.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return
    if not isinstance(state_place_id, int) or callback_place_id != state_place_id:
        await _stale(update)
        return

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        details = get_place_details(session, callback_place_id)
        if details is None or not is_place_admin(
            session, ctx.user(update).id, callback_place_id
        ):
            await query.answer(DENIED_ALERT, show_alert=True)
            return
        pending = get_pending_transfer_for_place(session, callback_place_id)
        _clear_transfer_keys(state)
        state["phase"] = (
            "transfer_outgoing" if pending is not None else "transfer_username_input"
        )
    finally:
        session.close()
    await query.answer()
    await _render_phase(update, context, state)


async def handle_transfer_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Username получателя: точный поиск известного или приглашение."""
    user = ctx.user(update)
    state = load_admin_session(user.id)
    if state is None or state.get("phase") not in TRANSFER_INPUT_PHASES:
        return
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        if not isinstance(place_id, int) or not is_place_admin(
            session, user.id, place_id
        ):
            clear_admin_session(user.id)
            await _render_denied(update, context)
            return
        details = get_place_details(session, place_id)
        if details is None:
            state.clear()
            state.update(_new_state())
            await _render_phase(
                update,
                context,
                state,
                notice=tr("admin.render_phase.mesto_suschestvuet_bylo_udaleno"),
                drop_input=True,
            )
            return
        raw = ctx.message(update).text or ""
        try:
            username = normalize_transfer_username(raw)
        except ValueError as e:
            cleanup = []
            message = getattr(update, "message", None)
            if message is not None and message.message_id is not None:
                cleanup.append(int(message.message_id))
            await _render_phase(
                update,
                context,
                state,
                notice=str(e),
                cleanup_ids=cleanup,
                drop_input=True,
            )
            return

        state["transfer_username"] = username
        _clear_transfer_keys_for_username(state)
        found = find_known_recipients(session, username)
        # Себя нельзя предлагать даже в списке кандидатов: кнопка вела бы
        # только к отказу store. Если других совпадений нет — явная ошибка,
        # а не приглашение на собственный username.
        candidates = tuple(
            candidate for candidate in found if candidate.telegram_user_id != user.id
        )
        if not candidates:
            if found:
                cleanup = []
                message = getattr(update, "message", None)
                if message is not None and message.message_id is not None:
                    cleanup.append(int(message.message_id))
                await _render_phase(
                    update,
                    context,
                    state,
                    notice=(tr("admin.handle_transfer.nelzya_peredat_adminku_samomu")),
                    cleanup_ids=cleanup,
                    drop_input=True,
                )
                return
            state["phase"] = "transfer_confirm_invite"
        elif len(candidates) == 1:
            state["transfer_target_telegram_user_id"] = candidates[0].telegram_user_id
            state["phase"] = "transfer_confirm_known"
        else:
            state["transfer_candidate_ids"] = [
                candidate.telegram_user_id for candidate in candidates
            ]
            state["phase"] = "transfer_candidates"
    finally:
        session.close()

    cleanup = []
    message = getattr(update, "message", None)
    if message is not None and message.message_id is not None:
        cleanup.append(int(message.message_id))
    await _render_phase(update, context, state, cleanup_ids=cleanup, drop_input=True)


def _clear_transfer_keys_for_username(state: dict) -> None:
    """Сбрасывает выбранного получателя, сохраняя введённый username."""
    state.pop("transfer_candidate_ids", None)
    state.pop("transfer_target_telegram_user_id", None)


async def handle_transfer_pick(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Выбор конкретного известного пользователя при совпадении username."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "transfer_candidates":
        await _stale(update)
        return
    raw_ids = state.get("transfer_candidate_ids")
    allowed = set(raw_ids) if isinstance(raw_ids, list) else set()
    try:
        target_tg = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return
    if target_tg not in allowed:
        await _stale(update)
        return
    state["transfer_target_telegram_user_id"] = target_tg
    state["phase"] = "transfer_confirm_known"
    await query.answer()
    await _render_phase(update, context, state)


async def handle_transfer_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Подтверждение создания предложения (известный или неизвестный получатель)."""
    query = ctx.query(update)
    user = ctx.user(update)
    state = load_admin_session(user.id)
    if state is None or state.get("phase") not in {
        "transfer_confirm_known",
        "transfer_confirm_invite",
    }:
        await _stale(update)
        return
    place_id = state.get("place_id")
    if not isinstance(place_id, int):
        await _stale(update)
        return
    phase = str(state.get("phase"))
    username = str(state.get("transfer_username") or "")
    target_tg = state.get("transfer_target_telegram_user_id")
    if phase == "transfer_confirm_known" and not isinstance(target_tg, int):
        state["phase"] = "transfer_username_input"
        await query.answer()
        await _render_phase(
            update,
            context,
            state,
            notice=tr("admin.render_phase.vyberite_poluchatelya_zanovo"),
        )
        return
    if not username:
        state["phase"] = "transfer_username_input"
        await query.answer()
        await _render_phase(
            update,
            context,
            state,
            notice=tr("admin.render_phase.vvedite_username_poluchatelya"),
        )
        return

    result = None
    try:
        session = SessionLocal()
        try:
            _ensure_user(session, update)
            if not is_place_admin(session, user.id, place_id):
                await query.answer(DENIED_ALERT, show_alert=True)
                return
            result = create_admin_transfer(
                session,
                actor_telegram_user_id=user.id,
                place_id=place_id,
                username=username,
                recipient_telegram_user_id=(
                    target_tg if phase == "transfer_confirm_known" else None
                ),
            )
        finally:
            session.close()
    except Exception as e:  # noqa: BLE001 — безопасный экран вместо падения
        logger.error("Не удалось создать предложение передачи: %s", e)
        result = None
        notice = tr("admin.handle_transfer_confirm.ne_udalos_sozdat_predlozhenie")
    else:
        notice = ""

    if result is None:
        await query.answer()
        await _render_phase(update, context, state, notice=notice)
        return

    invite_url: str | None = None
    if result.status == "created_known":
        delivered = False
        if result.transfer is not None:
            delivered = await _notify_recipient(context, result.transfer)
            if delivered:
                session = SessionLocal()
                try:
                    mark_transfer_notified(session, result.transfer.transfer_id)
                except Exception as e:  # noqa: BLE001 — статус доставки не критичен
                    logger.error("Не удалось отметить доставку уведомления: %s", e)
                finally:
                    session.close()
        notice = (
            tr("admin.handle_transfer_confirm.predlozhenie_sozdano_poluchatel")
            if delivered
            else (tr("admin.handle_transfer_confirm.predlozhenie_sozdano_no_lichnoe"))
        )
        _clear_transfer_keys(state)
        state["phase"] = "transfer_outgoing"
    elif result.status == "created_invite":
        token = result.invite_token or ""
        transfer_id = result.transfer.transfer_id if result.transfer is not None else 0
        invite_url = (
            await _deep_link(context, f"tf_{transfer_id}_{token}")
            if token and transfer_id
            else None
        )
        if invite_url is None:
            session = SessionLocal()
            try:
                if result.transfer is not None:
                    cancel_admin_transfer(
                        session,
                        actor_telegram_user_id=user.id,
                        transfer_id=result.transfer.transfer_id,
                    )
            except Exception as e:  # noqa: BLE001
                logger.error("Не удалось отменить неполное приглашение: %s", e)
            finally:
                session.close()
            _clear_transfer_keys(state)
            state["phase"] = "transfer_username_input"
            notice = tr("admin.handle_transfer_confirm.ne_udalos_sformirovat_ssylku")
        else:
            _clear_transfer_keys(state)
            state["phase"] = "transfer_outgoing"
            notice = tr("admin.handle_transfer_confirm.ssylka_priglashenie_sozdana_24")
    elif result.status == "active_exists":
        _clear_transfer_keys(state)
        state["phase"] = "transfer_outgoing"
        notice = tr("admin.handle_transfer_confirm.u_etogo_mesta_est")
    elif result.status == "self":
        notice = tr("admin.handle_transfer_confirm.nelzya_peredat_adminku_samomu")
    elif result.status == "user_missing":
        notice = tr("admin.handle_transfer_confirm.poluchatel_bolshe_izvesten_botu")
        state["phase"] = "transfer_username_input"
    elif result.status == "place_missing":
        state.clear()
        state.update(_new_state())
        notice = tr("admin.render_phase.mesto_suschestvuet_bylo_udaleno")
    else:
        notice = tr("admin.handle_transfer_confirm.predlozhenie_sozdano_sostoyanie")

    await query.answer()
    await _render_phase(update, context, state, notice=notice, invite_url=invite_url)


async def handle_transfer_refresh(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Перечитывает статус предложения и лениво применяет срок."""
    query = ctx.query(update)
    guard = await _transfer_state_place(
        update, allowed_phases=frozenset({"transfer_outgoing"})
    )
    if guard is None:
        return
    state, place_id, _phase = guard
    session = SessionLocal()
    try:
        if not is_place_admin(session, ctx.user(update).id, place_id):
            await query.answer(DENIED_ALERT, show_alert=True)
            return
        get_pending_transfer_for_place(session, place_id)
    finally:
        session.close()
    await query.answer()
    await _render_phase(update, context, state)


async def handle_transfer_approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Инициатор утверждает кандидата, открывшего одноразовую ссылку."""
    query = ctx.query(update)
    guard = await _transfer_state_place(
        update, allowed_phases=frozenset({"transfer_outgoing"})
    )
    if guard is None:
        return
    state, place_id, _phase = guard
    try:
        transfer_id = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        view = get_transfer_view(session, transfer_id)
        if (
            view is None
            or view.place_id != place_id
            or view.from_user.telegram_user_id != ctx.user(update).id
        ):
            await query.answer(STALE_TEXT, show_alert=True)
            return
        result = approve_transfer_recipient(
            session,
            actor_telegram_user_id=ctx.user(update).id,
            transfer_id=transfer_id,
        )
    finally:
        session.close()

    if result.status != "approved":
        await query.answer()
        notice = {
            "not_opened": tr("admin.handle_transfer_approve.ssylku_nikto_otkryl"),
            "already_approved": tr(
                "admin.handle_transfer_approve.poluchatel_utverzhden_2"
            ),
            "not_initiator": tr(
                "admin.handle_transfer_approve.utverdit_poluchatelya_mozhet_tol"
            ),
            "superseded": tr(
                "admin.handle_transfer_approve.predlozhenie_nedeystvitelno"
            ),
            "expired": tr("admin.handle_transfer_approve.srok_predlozheniya_istek"),
            "self": tr("admin.handle_transfer_approve.nelzya_utverdit_samogo_sebya"),
        }.get(
            result.status,
            tr("admin.handle_transfer_approve.sostoyanie_izmenilos_obnovite"),
        )
        await _render_phase(update, context, state, notice=notice)
        return

    delivered = False
    if result.transfer is not None:
        delivered = await _notify_recipient(context, result.transfer)
        if delivered:
            session = SessionLocal()
            try:
                mark_transfer_notified(session, transfer_id)
            except Exception as e:  # noqa: BLE001
                logger.error("Не удалось отметить доставку уведомления: %s", e)
            finally:
                session.close()
    await query.answer()
    notice = tr("admin.handle_transfer_approve.poluchatel_utverzhden")
    if not delivered:
        notice += tr("admin.handle_transfer_approve.lichnoe_uvedomlenie_dostavleno")
    await _render_phase(update, context, state, notice=notice)


async def handle_transfer_notify(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Повторная best-effort доставка уведомления уже утверждённому получателю."""
    query = ctx.query(update)
    guard = await _transfer_state_place(
        update, allowed_phases=frozenset({"transfer_outgoing"})
    )
    if guard is None:
        return
    state, place_id, _phase = guard
    try:
        transfer_id = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        view = get_transfer_view(session, transfer_id)
        if (
            view is None
            or view.place_id != place_id
            or not is_place_admin(session, ctx.user(update).id, place_id)
        ):
            await query.answer(STALE_TEXT, show_alert=True)
            return
        if not view.awaiting_recipient:
            await query.answer(
                tr("alerts.admin.uvedomlenie_bolshe_trebuetsya"),
                show_alert=True,
            )
            await _render_phase(update, context, state)
            return
    finally:
        session.close()

    delivered = await _notify_recipient(context, view)
    if delivered:
        session = SessionLocal()
        try:
            mark_transfer_notified(session, transfer_id)
        except Exception as e:  # noqa: BLE001
            logger.error("Не удалось отметить доставку уведомления: %s", e)
        finally:
            session.close()
    await query.answer()
    notice = (
        tr("admin.handle_transfer_notify.uvedomlenie_dostavleno")
        if delivered
        else tr("admin.handle_transfer_notify.ne_udalos_dostavit_uvedomlenie")
    )
    await _render_phase(update, context, state, notice=notice)


async def handle_transfer_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Инициатор отменяет предложение до подтверждения; права не меняются."""
    query = ctx.query(update)
    guard = await _transfer_state_place(
        update, allowed_phases=frozenset({"transfer_outgoing"})
    )
    if guard is None:
        return
    state, place_id, _phase = guard
    try:
        transfer_id = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return

    result = None
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        view = get_transfer_view(session, transfer_id)
        if (
            view is None
            or view.place_id != place_id
            or view.from_user.telegram_user_id != ctx.user(update).id
        ):
            await query.answer(STALE_TEXT, show_alert=True)
            return
        result = cancel_admin_transfer(
            session,
            actor_telegram_user_id=ctx.user(update).id,
            transfer_id=transfer_id,
        )
    finally:
        session.close()

    if result.status == "cancelled":
        recipient = None
        if result.transfer is not None:
            recipient = result.transfer.to_user or result.transfer.opened_by
        if recipient is not None:
            await _send_user_notification(
                context,
                recipient.telegram_user_id,
                (
                    tr_safe(
                        "admin.handle_transfer_cancel.peredacha_administracii_otmenena",
                        place_name=_h(
                            result.transfer.place_name if result.transfer else ""
                        ),
                    )
                ),
            )
        _clear_transfer_keys(state)
        state["phase"] = "place"
        notice = tr("admin.handle_transfer_cancel.peredacha_otmenena_prava_ostalis")
    else:
        notice = {
            "not_initiator": tr(
                "admin.handle_transfer_cancel.otmenit_peredachu_mozhet_tolko"
            ),
            "expired": tr("admin.handle_transfer_cancel.srok_predlozheniya_istek"),
            "superseded": tr(
                "admin.handle_transfer_cancel.predlozhenie_nedeystvitelno"
            ),
            "cancelled": tr("admin.handle_transfer_cancel.peredacha_otmenena"),
            "confirmed": tr(
                "admin.handle_transfer_cancel.peredacha_podtverzhdena_mozhet_by"
            ),
            "rejected": tr(
                "admin.handle_transfer_cancel.poluchatel_otklonil_predlozhenie"
            ),
        }.get(
            result.status,
            tr("admin.handle_transfer_approve.sostoyanie_izmenilos_obnovite"),
        )
        if result.transfer is None:
            state["phase"] = "place"
        else:
            state["phase"] = "transfer_outgoing"
    await query.answer()
    await _render_phase(update, context, state, notice=notice)


async def handle_transfer_accept(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Получатель подтверждает передачу в своём Telegram-аккаунте."""
    query = ctx.query(update)
    user = ctx.user(update)
    try:
        transfer_id = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        result = confirm_admin_transfer(
            session,
            actor_telegram_user_id=user.id,
            transfer_id=transfer_id,
        )
    except Exception as e:  # noqa: BLE001 — отказ безопаснее ложного подтверждения
        logger.error("Не удалось подтвердить передачу: %s", e)
        await query.answer(
            tr("alerts.admin.ne_udalos_podtverdit_peredachu"), show_alert=True
        )
        return
    finally:
        session.close()

    if result.status == "confirmed":
        await query.answer(tr("alerts.admin.peredacha_podtverzhdena"))
        view = result.transfer
        if view is not None:
            text = tr_safe(
                "admin.handle_transfer_accept.vy_teper_administrator_mesta",
                place_name=_h(view.place_name),
            )
            await _edit_notification_message(
                query,
                text,
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                tr("buttons.admin.otkryt_kabinet"),
                                callback_data="admin_panel",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                tr("buttons.admin.glavnoe_menyu"),
                                callback_data="main_menu",
                            )
                        ],
                    ]
                ),
            )
            await _notify_initiator(
                context,
                view,
                (
                    tr_safe(
                        "admin.handle_transfer_accept.peredacha_administracii_zavershen",
                        place_name=_h(view.place_name),
                        transfer_user_label=(
                            _h(_transfer_user_label(view.to_user))
                            if view.to_user
                            else "—"
                        ),
                    )
                ),
            )
        return

    if result.status == "already_confirmed":
        await query.answer(tr("alerts.admin.peredacha_podtverzhdena_2"))
        await _edit_notification_message(
            query,
            tr("admin.handle_transfer_accept.eto_predlozhenie_podtverzhdeno"),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
                        )
                    ]
                ]
            ),
        )
        return

    if result.status == "not_recipient":
        await query.answer(
            tr("alerts.admin.predlozhenie_adresovano_drugomu"), show_alert=True
        )
        return

    await query.answer(tr("alerts.admin.peredacha_mozhet_byt_podtverzhdena"))
    text, keyboard = _incoming_transfer_content(result.status, result.transfer, user.id)
    await _edit_notification_message(query, text, keyboard)


async def handle_transfer_decline(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Получатель отклоняет предложение; права не меняются."""
    query = ctx.query(update)
    user = ctx.user(update)
    try:
        transfer_id = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        result = reject_admin_transfer(
            session,
            actor_telegram_user_id=user.id,
            transfer_id=transfer_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось отклонить передачу: %s", e)
        await query.answer(
            tr("alerts.admin.ne_udalos_otklonit_peredachu"), show_alert=True
        )
        return
    finally:
        session.close()

    if result.status == "rejected":
        await query.answer(tr("alerts.admin.predlozhenie_otkloneno"))
        view = result.transfer
        await _edit_notification_message(
            query,
            (
                tr_safe(
                    "admin.handle_transfer_decline.vy_otklonili_peredachu",
                    place_name=_h(view.place_name) if view else "",
                )
            ),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
                        )
                    ]
                ]
            ),
        )
        if view is not None:
            await _notify_initiator(
                context,
                view,
                (
                    tr_safe(
                        "admin.handle_transfer_decline.poluchatel_otklonil_peredachu",
                        place_name=_h(view.place_name),
                    )
                ),
            )
        return

    if result.status == "not_recipient":
        await query.answer(
            tr("alerts.admin.predlozhenie_adresovano_drugomu"), show_alert=True
        )
        return

    await query.answer(tr("alerts.admin.predlozhenie_nelzya_otklonit"))
    text, keyboard = _incoming_transfer_content(result.status, result.transfer, user.id)
    await _edit_notification_message(query, text, keyboard)


async def handle_transfer_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    Deep-link `/start tf_<id>_<token>`: экран получателя без прав.

    Возвращает True, если payload относится к передаче (включая некорректный):
    обычный /start тогда не должен перерисовывать главное меню.
    """
    args = getattr(context, "args", None) or []
    payload = args[0] if args else ""
    if not payload.startswith("tf_"):
        return False

    await screen.reanchor_screen(update, context)
    user = ctx.user(update)
    clear_place_session(user.id)
    clear_admin_session(user.id)

    body = payload[3:]
    transfer_id_raw, separator, token = body.partition("_")
    valid_shape = (
        bool(separator)
        and transfer_id_raw.isdigit()
        and len(transfer_id_raw) <= 18
        and 20 <= len(token) <= 128
        and all(char.isalnum() or char in {"_", "-"} for char in token)
    )
    status = "invalid_invite"
    view: TransferView | None = None
    if valid_shape:
        session = SessionLocal()
        try:
            _ensure_user(session, update)
            result = open_transfer_invitation(
                session,
                transfer_id=int(transfer_id_raw),
                token=token,
                actor_telegram_user_id=user.id,
            )
            status = result.status
            view = result.transfer
        except Exception as e:  # noqa: BLE001 — ссылка не должна ронять бота
            logger.error("Не удалось открыть приглашение передачи: %s", e)
        finally:
            session.close()

    text, keyboard = _incoming_transfer_content(status, view, user.id)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user.id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(
        context, user_data, chat_id, text, keyboard, user_id=user.id
    )
    screen.save_screen_ref(user.id, user_data, chat_id)
    return True


class AdminTextHandler(MessageHandler):
    """Текст принимается только в явных фазах кабинета, заданных allowed_phases."""

    def __init__(self, filters_arg, callback, *, allowed_phases: frozenset[str]):
        super().__init__(filters_arg, callback)
        self.allowed_phases = frozenset(allowed_phases)

    def check_update(self, update):
        matched = super().check_update(update)
        if not matched:
            return None
        user = getattr(update, "effective_user", None)
        if user is None:
            return None
        state = load_admin_session(user.id)
        if not isinstance(state, dict):
            return None
        if state.get("phase") not in self.allowed_phases:
            return None
        return matched


# --- Предложения кирпичиков: очереди и решения (Stage 26) ------------------


def _proposal_callback_id(data: str) -> int | None:
    try:
        value = int(data.rsplit(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _proposal_callback_offset(data: str) -> int | None:
    try:
        value = int(data.rsplit(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        return None
    return max(value, 0)


async def handle_brick_proposals_global(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "places":
        await _stale(update)
        return
    await query.answer()
    state = _new_state()
    state.update(
        {
            "phase": "proposal_global_list",
            "proposal_offset": 0,
        }
    )
    await _render_phase(update, context, state)


async def handle_brick_proposals_places(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "places":
        await _stale(update)
        return
    await query.answer()
    state = _new_state()
    state.update(
        {
            "phase": "proposal_places_list",
            "proposal_offset": 0,
        }
    )
    state.pop("proposal_place_filter", None)
    await _render_phase(update, context, state)


async def handle_brick_proposals_place(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "place":
        await _stale(update)
        return
    place_id = _proposal_callback_id(str(query.data or ""))
    state_place_id = state.get("place_id")
    if (
        place_id is None
        or not isinstance(state_place_id, int)
        or state_place_id != place_id
    ):
        await _stale(update)
        return
    session = SessionLocal()
    try:
        allowed = can_manage_place(session, ctx.user(update).id, place_id)
    finally:
        session.close()
    if not allowed:
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    await query.answer()
    state["phase"] = "proposal_places_list"
    state["proposal_offset"] = 0
    state["proposal_place_filter"] = place_id
    await _render_phase(update, context, state)


async def handle_brick_proposals_global_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_global_list":
        await _stale(update)
        return
    offset = _proposal_callback_offset(str(query.data or ""))
    if offset is None:
        await _stale(update)
        return
    await query.answer()
    state["proposal_offset"] = offset
    await _render_phase(update, context, state)


async def handle_brick_proposals_places_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_places_list":
        await _stale(update)
        return
    offset = _proposal_callback_offset(str(query.data or ""))
    if offset is None:
        await _stale(update)
        return
    await query.answer()
    state["proposal_offset"] = offset
    await _render_phase(update, context, state)


async def handle_brick_proposals_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "proposal_global_list",
        "proposal_places_list",
    }:
        await _stale(update)
        return
    proposal_id = _proposal_callback_id(str(query.data or ""))
    if proposal_id is None:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        proposal = get_admin_proposal(session, ctx.user(update).id, proposal_id)
    finally:
        session.close()
    if proposal is None:
        await query.answer(tr("alerts.admin.predlozhenie_nedostupno"), show_alert=True)
        await _render_phase(
            update,
            context,
            state,
            notice=tr("admin.handle_brick_proposals.predlozhenie_naydeno_nedostupno"),
        )
        return
    await query.answer()
    state["proposal_return_phase"] = state.get("phase")
    state["proposal_id"] = proposal.id
    state["phase"] = "proposal_card"
    await _render_phase(update, context, state)


async def handle_brick_proposal_approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_card":
        await _stale(update)
        return
    proposal_id = _proposal_callback_id(str(query.data or ""))
    if proposal_id is None or state.get("proposal_id") != proposal_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        proposal = get_admin_proposal(session, ctx.user(update).id, proposal_id)
    finally:
        session.close()
    if proposal is None:
        await query.answer(
            tr("alerts.admin.predlozhenie_nedostupno_2"),
            show_alert=True,
        )
        await _render_phase(
            update,
            context,
            state,
            notice=(tr("admin.handle_brick_proposal.predlozhenie_nedostupno")),
        )
        return
    await query.answer()
    if proposal.status != PROPOSAL_STATUS_PENDING:
        state["phase"] = "proposal_card"
        await _render_phase(
            update,
            context,
            state,
            notice=tr("admin.render_phase.predlozhenie_ozhidaet_resheniya"),
        )
        return
    state["phase"] = "proposal_approve_confirm"
    await _render_phase(update, context, state)


async def handle_brick_proposal_reject(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_card":
        await _stale(update)
        return
    proposal_id = _proposal_callback_id(str(query.data or ""))
    if proposal_id is None or state.get("proposal_id") != proposal_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        proposal = get_admin_proposal(session, ctx.user(update).id, proposal_id)
    finally:
        session.close()
    if proposal is None:
        await query.answer(
            tr("alerts.admin.predlozhenie_nedostupno_2"),
            show_alert=True,
        )
        await _render_phase(
            update,
            context,
            state,
            notice=(tr("admin.handle_brick_proposal.predlozhenie_nedostupno")),
        )
        return
    await query.answer()
    if proposal.status != PROPOSAL_STATUS_PENDING:
        state["phase"] = "proposal_card"
        await _render_phase(
            update,
            context,
            state,
            notice=tr("admin.render_phase.predlozhenie_ozhidaet_resheniya"),
        )
        return
    state["phase"] = "proposal_reject_confirm"
    await _render_phase(update, context, state)


async def _notify_proposal_author(
    context: ContextTypes.DEFAULT_TYPE,
    proposal: BrickProposalSnapshot,
    *,
    approved: bool,
    brick_id: int | None = None,
) -> bool:
    """Best-effort уведомление после уже зафиксированного решения."""
    if approved:
        text = tr_safe(
            "admin.notify_proposal_author.vashe_predlozhenie_kirpichik_html",
            title=_h(brick_title_display(proposal.title, proposal.code)),
            code=_h(proposal.code),
            proposal_target_label=_h(_proposal_target_label(proposal)),
        )
        if brick_id is not None:
            text += tr(
                "admin.notify_proposal_author.novyy_kirpichik", brick_id=brick_id
            )
    else:
        text = tr_safe(
            "admin.notify_proposal_author.vashe_predlozhenie_kirpichik_2_html",
            title=_h(brick_title_display(proposal.title, proposal.code)),
            code=_h(proposal.code),
            proposal_target_label=_h(_proposal_target_label(proposal)),
        )
        if proposal.decision_note:
            text += tr_safe(
                "admin.notify_proposal_author.prichina",
                decision_note=_h(proposal.decision_note),
            )
    text += tr("admin.notify_proposal_author.status_dostupen_razdele_moi")
    return await _send_user_notification(
        context, proposal.author_telegram_user_id, text
    )


async def _handle_brick_proposal_decision(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    approved: bool,
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    expected_phase = (
        "proposal_approve_confirm" if approved else "proposal_reject_confirm"
    )
    if state is None or state.get("phase") != expected_phase:
        await _stale(update)
        return
    proposal_id = _proposal_callback_id(str(query.data or ""))
    if proposal_id is None or state.get("proposal_id") != proposal_id:
        await _stale(update)
        return
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        result = (
            approve_brick_proposal(
                session,
                actor_telegram_user_id=user_id,
                proposal_id=proposal_id,
            )
            if approved
            else reject_brick_proposal(
                session,
                actor_telegram_user_id=user_id,
                proposal_id=proposal_id,
            )
        )
    except (ProposalPermissionError, ProposalActorError, ValueError) as exc:
        await query.answer(str(exc), show_alert=True)
        state.clear()
        state.update(_new_state())
        await _render_phase(
            update,
            context,
            state,
            notice=(tr("admin.handle_brick_proposal.polnomochiya_izmenilis")),
        )
        return
    finally:
        session.close()

    await query.answer()
    if result.status == "not_found":
        state.clear()
        state.update(_new_state())
        await _render_phase(
            update,
            context,
            state,
            notice=result.message
            or tr("admin.handle_brick_proposal.predlozhenie_naydeno"),
        )
        return
    if result.status in {"already_decided", "stale"}:
        state["phase"] = "proposal_card"
        await _render_phase(
            update,
            context,
            state,
            notice=result.message
            or tr("admin.handle_brick_proposal.predlozhenie_rassmotreno"),
        )
        return
    if result.status in {"conflict", "error", "invalid"}:
        state["phase"] = "proposal_card"
        await _render_phase(
            update,
            context,
            state,
            notice=result.message
            or tr("admin.handle_brick_proposal.reshenie_prinyato"),
        )
        return

    delivered = False
    if result.proposal is not None:
        delivered = await _notify_proposal_author(
            context,
            result.proposal,
            approved=approved,
            brick_id=result.brick.id if result.brick is not None else None,
        )
    conflict_ids = [view.id for view in result.conflicts]
    if approved and conflict_ids:
        notified, undelivered = await _notify_conflict_admins(context, conflict_ids)
        if notified and undelivered:
            notice_extra = tr(
                "admin.conflict.admins_both",
                notified=notified,
                undelivered=undelivered,
            )
        elif notified:
            notice_extra = tr("admin.conflict.admins_notified", notified=notified)
        elif undelivered:
            notice_extra = tr(
                "admin.conflict.admins_undelivered", undelivered=undelivered
            )
        else:
            notice_extra = tr("admin.conflict.admins_queued")
    else:
        notice_extra = ""
    state["phase"] = "proposal_card"
    if result.proposal is not None:
        state["proposal_id"] = result.proposal.id
    notice = result.message or (
        tr("admin.handle_brick_proposal.predlozhenie_odobreno")
        if approved
        else tr("admin.handle_brick_proposal.predlozhenie_otkloneno")
    )
    notice += notice_extra
    if delivered:
        notice += tr("admin.conflict.author_delivered")
    else:
        notice += tr("admin.conflict.author_undelivered")
    await _render_phase(update, context, state, notice=notice)


async def handle_brick_proposal_approve_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _handle_brick_proposal_decision(update, context, approved=True)


async def handle_brick_proposal_reject_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _handle_brick_proposal_decision(update, context, approved=False)


async def handle_brick_proposals_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "proposal_global_list",
        "proposal_places_list",
        "proposal_card",
        "proposal_approve_confirm",
        "proposal_reject_confirm",
    }:
        await _stale(update)
        return
    await query.answer()
    phase = state.get("phase")
    if phase in {"proposal_global_list", "proposal_places_list"}:
        if isinstance(state.get("proposal_place_filter"), int) and isinstance(
            state.get("place_id"), int
        ):
            state["phase"] = "place"
        else:
            state.clear()
            state.update(_new_state())
    elif phase in {"proposal_approve_confirm", "proposal_reject_confirm"}:
        state["phase"] = "proposal_card"
    else:
        return_phase = state.get("proposal_return_phase")
        if return_phase not in {
            "proposal_global_list",
            "proposal_places_list",
        }:
            return_phase = (
                "proposal_places_list"
                if isinstance(state.get("proposal_place_filter"), int)
                else "proposal_global_list"
            )
        state.pop("proposal_id", None)
        state.pop("proposal_return_phase", None)
        state["phase"] = return_phase
    await _render_phase(update, context, state)


# --- Редактор описания кирпичика (Poster 2.4 P1) ---------------------------


def _brick_callback_id(data: str) -> int | None:
    return _proposal_callback_id(data)


async def handle_admin_bricks(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    «🧱 Кирпичики»: список области административного редактора описания.

    Суперадминистратор открывает системный каталог из общего списка мест,
    администратор места — кирпичики своей площадки из её карточки. Права
    проверяются по фактической роли, а не по callback.
    """
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {"place", "places"}:
        await _stale(update)
        return
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        access_kind = _access_kind(session, user_id)
        if access_kind is None:
            allowed = False
        elif query.data == "adm_bricks_global" or state.get("phase") == "places":
            allowed = access_kind == "superadmin"
        else:
            place_id = _brick_callback_id(str(query.data or ""))
            allowed = available = False
            if place_id is not None:
                allowed = can_manage_place(session, user_id, place_id)
                available = get_place_details(session, place_id) is not None
            if not available:
                await query.answer(
                    tr("alerts.admin.mesto_suschestvuet_udaleno"), show_alert=True
                )
                return
    finally:
        session.close()
    if not allowed:
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    await query.answer()
    if state.get("phase") == "places":
        state["phase"] = "brick_list"
        state["brick_scope"] = "system"
        state.pop("brick_place_id", None)
        state.pop("brick_id", None)
    else:
        place_id = _brick_callback_id(str(query.data or ""))
        state["phase"] = "brick_list"
        state["brick_scope"] = "place"
        state["brick_place_id"] = place_id
        state.pop("brick_id", None)
    await _render_phase(update, context, state)


async def handle_brick_create(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    `adm_brick_new:<place_id>`: «➕ Создать кирпичик» площадки (Poster 2.5 §13).

    Открывает СУЩЕСТВУЮЩИЙ конструктор кирпичиков с предустановкой области:
    кирпичик сразу принадлежит площадке. Права проверяются здесь по БД, а не
    по факту нажатия: посторонний не попадает в мастер даже с подделанным
    callback.
    """
    query = ctx.query(update)
    user_id = ctx.user(update).id
    state = load_admin_session(user_id)
    if state is None or state.get("phase") not in {"place", "brick_list"}:
        await _stale(update)
        return
    place_id = _brick_callback_id(str(query.data or ""))
    if place_id is None:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        allowed = can_manage_place(session, user_id, place_id)
        available = get_place_details(session, place_id) is not None
    except Exception as e:  # noqa: BLE001 — отказ безопаснее открытия
        logger.error("Не удалось проверить права создания кирпичика: %s", e)
        allowed = False
        available = False
    finally:
        session.close()
    if not available:
        await query.answer(
            tr("alerts.admin.mesto_suschestvuet_udaleno"), show_alert=True
        )
        return
    if not allowed:
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    if state.get("brick_scope") == "place" and state.get("brick_place_id") != place_id:
        await _stale(update)
        return
    await query.answer()
    # Мастер живёт в `brick_sessions`; административное состояние остаётся
    # подсказкой о том, откуда человек пришёл, а не источником полномочий.
    from handlers.bricks import start_place_brick_create

    await start_place_brick_create(update, context, place_id)


async def handle_brick_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`adm_bedit:<brick_id>`: карточка кирпичика с единственной правкой — описанием."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "places",
        "place",
        "brick_list",
        "brick_card",
        "brick_description_input",
    }:
        await _stale(update)
        return
    brick_id = _brick_callback_id(str(query.data or ""))
    if brick_id is None:
        await _stale(update)
        return
    user_id = ctx.user(update).id
    scope = "system"
    place_id: int | None = None
    session = SessionLocal()
    try:
        access_kind = _access_kind(session, user_id)
        brick = session.get(Brick, brick_id)
        exists = brick is not None
        allowed = False
        if brick is not None:
            # Проверка прав требует непустую строку — сужаем тип здесь же.
            allowed = _brick_edit_allowed(
                session, brick, access_kind=access_kind, user_id=user_id
            )
            scope = "place" if brick.scope == BRICK_SCOPE_PLACE else "system"
            place_id = brick.place_id
    finally:
        session.close()
    if not allowed:
        await query.answer(
            DENIED_ALERT if exists else tr("admin.brick_list.kirpichik_nedostupen"),
            show_alert=True,
        )
        return
    await query.answer()
    state.pop("brick_description_expected", None)
    state["brick_scope"] = scope
    if scope == "place" and isinstance(place_id, int):
        state["brick_place_id"] = place_id
    else:
        state.pop("brick_place_id", None)
    state["brick_id"] = brick_id
    state["phase"] = "brick_card"
    await _render_phase(update, context, state)


async def handle_brick_description(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«✏️ Описание»: переход к вводу нового текста описания."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "brick_card":
        await _stale(update)
        return
    brick_id = _brick_callback_id(str(query.data or ""))
    if brick_id is None or state.get("brick_id") != brick_id:
        await _stale(update)
        return
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        access_kind = _access_kind(session, user_id)
        brick = session.get(Brick, brick_id)
        allowed = (
            brick is not None
            and brick.status == BRICK_STATUS_ACTIVE
            and _brick_edit_allowed(
                session, brick, access_kind=access_kind, user_id=user_id
            )
        )
        revision = brick.revision if brick is not None else None
    finally:
        session.close()
    if not allowed:
        await query.answer(
            tr("admin.brick_description_input.arhivnyy_kirpichik"),
            show_alert=True,
        )
        return
    await query.answer()
    # Ревизия экрана: сохранение пройдёт, только если кирпичик не изменился,
    # пока человек печатал описание.
    state["brick_description_expected"] = revision
    state["phase"] = "brick_description_input"
    await _render_phase(update, context, state, drop_input=True)


async def handle_admin_bricks_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Пагинация списка кирпичиков административного кабинета."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "brick_list":
        await _stale(update)
        return
    try:
        offset = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        await _stale(update)
        return
    await query.answer()
    state["brick_page"] = max(offset, 0)
    await _render_phase(update, context, state)


async def handle_brick_card_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«◀️ Назад»: из карточки — в список области, из списка — в карточку места."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "brick_list",
        "brick_card",
        "brick_description_input",
    }:
        await _stale(update)
        return
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        access_kind = _access_kind(session, user_id)
    finally:
        session.close()
    if access_kind is None:
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    await query.answer()
    phase = state.get("phase")
    state.pop("brick_description_expected", None)
    if phase == "brick_list":
        if state.get("brick_scope") == "place" and isinstance(
            state.get("brick_place_id"), int
        ):
            state["phase"] = "place"
            state["place_id"] = state["brick_place_id"]
        else:
            state.clear()
            state.update(_new_state())
    else:
        # Из карточки — в список области редактора: человек не теряет место,
        # из которого пришёл.
        state.pop("brick_id", None)
        state["phase"] = "brick_list"
    await _render_phase(update, context, state)


async def handle_brick_description_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Новый текст описания в фазе `brick_description_input`.

    Пишет только описание: название, код и тип кирпичика не являются
    параметрами доменного вызова. Ожидаемая ревизия берётся из состояния
    экрана, поэтому чужая правка в другом окне даёт честный отказ, а не
    молчаливую перезапись.
    """
    user = ctx.user(update)
    state = load_admin_session(user.id)
    if state is None or state.get("phase") != "brick_description_input":
        return
    brick_id = state.get("brick_id")
    session = SessionLocal()
    try:
        access_kind = _access_kind(session, user.id)
        brick = (
            session.get(Brick, brick_id)
            if isinstance(brick_id, int) and not isinstance(brick_id, bool)
            else None
        )
        allowed = brick is not None and _brick_edit_allowed(
            session, brick, access_kind=access_kind, user_id=user.id
        )
        if not allowed:
            clear_admin_session(user.id)
            await _render_denied(update, context)
            return
        text = ctx.message(update).text or ""
        expected = state.get("brick_description_expected")
        try:
            result = update_brick_description(
                session,
                actor_telegram_user_id=user.id,
                brick_id=int(brick_id or 0),
                description=text,
                expected_revision=(
                    expected
                    if isinstance(expected, int) and not isinstance(expected, bool)
                    else None
                ),
            )
        except (BrickPermissionError, BrickNotFoundError) as e:
            session.rollback()
            notice = str(e)
            failed = True
        except Exception:  # noqa: BLE001 — экран не должен падать
            session.rollback()
            logger.exception("Не удалось сохранить описание кирпичика.")
            notice = tr("admin.brick_description_input.ne_udalos_sohranit")
            failed = True
        else:
            notice = result.message or (
                tr("admin.brick_description_input.sohraneno")
                if result.status in {"updated", "unchanged"}
                else tr("admin.brick_description_input.ne_udalos_sohranit")
            )
            failed = result.status not in {"updated", "unchanged"}
    finally:
        session.close()

    state.pop("brick_description_expected", None)
    state["phase"] = "brick_card"
    cleanup = _failed_take(state)
    message = getattr(update, "message", None)
    if message is not None and message.message_id is not None and not failed:
        cleanup.append(int(message.message_id))
    await _render_phase(
        update,
        context,
        state,
        notice=notice,
        cleanup_ids=cleanup,
        drop_input=True,
    )


# --- Конфликты кодов: обработчики (Poster 2.1 R3) --------------------------


def _conflict_callback_id(data: str) -> int | None:
    return _proposal_callback_id(data)


async def handle_conflicts_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Кнопка «Конфликты кодов» в карточке места и «◀️ К конфликтам» в задаче."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "place",
        # U3b: «◀️ К конфликтам» из карточки задачи конфликта — живой возврат.
        # Без этой фазы кнопка отвечала «Кнопка устарела», хотя список задач
        # остаётся на месте и доступ не менялся.
        "conflict_card",
    }:
        await _stale(update)
        return
    place_id = _conflict_callback_id(str(query.data or ""))
    state_place_id = state.get("place_id")
    if (
        place_id is None
        or not isinstance(state_place_id, int)
        or state_place_id != place_id
    ):
        await _stale(update)
        return
    session = SessionLocal()
    try:
        allowed = can_manage_place(session, ctx.user(update).id, place_id)
    finally:
        session.close()
    if not allowed:
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    await query.answer()
    state["phase"] = "conflicts_list"
    state["conflict_page"] = 0
    await _render_phase(update, context, state)


async def handle_conflicts_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Пагинация списка конфликтов кодов (Poster 2.5 §14)."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "conflicts_list":
        await _stale(update)
        return
    try:
        offset = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        await _stale(update)
        return
    await query.answer()
    state["conflict_page"] = max(offset, 0)
    await _render_phase(update, context, state)


async def handle_conflict_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "conflicts_list",
        "conflict_card",
        "conflict_rename_input",
        "conflict_switch_confirm",
    }:
        await _stale(update)
        return
    conflict_id = _conflict_callback_id(str(query.data or ""))
    if conflict_id is None:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        view = get_conflict(session, ctx.user(update).id, conflict_id)
    finally:
        session.close()
    if view is None:
        await query.answer(tr("alerts.admin.zadacha_nedostupna"), show_alert=True)
        await _render_phase(
            update,
            context,
            state,
            notice=tr("admin.handle_conflict_open.zadacha_konflikta_naydena"),
        )
        return
    await query.answer()
    state["place_id"] = view.place_id
    state["conflict_id"] = view.id
    state["phase"] = "conflict_card"
    await _render_phase(update, context, state)


async def handle_conflict_rename(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "conflict_card":
        await _stale(update)
        return
    conflict_id = _conflict_callback_id(str(query.data or ""))
    if conflict_id is None or state.get("conflict_id") != conflict_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        view = get_conflict(session, ctx.user(update).id, conflict_id)
    finally:
        session.close()
    if view is None:
        await query.answer(tr("alerts.admin.zadacha_nedostupna"), show_alert=True)
        await _render_phase(
            update,
            context,
            state,
            notice=tr("admin.handle_conflict_open.zadacha_konflikta_naydena"),
        )
        return
    await query.answer()
    if not view.is_open:
        await _render_phase(
            update,
            context,
            state,
            notice=tr("admin.render_phase.zadacha_reshena"),
        )
        return
    state["phase"] = "conflict_rename_input"
    await _render_phase(update, context, state)


async def handle_conflict_switch(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "conflict_card":
        await _stale(update)
        return
    conflict_id = _conflict_callback_id(str(query.data or ""))
    if conflict_id is None or state.get("conflict_id") != conflict_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        view = get_conflict(session, ctx.user(update).id, conflict_id)
    finally:
        session.close()
    if view is None:
        await query.answer(tr("alerts.admin.zadacha_nedostupna"), show_alert=True)
        await _render_phase(
            update,
            context,
            state,
            notice=tr("admin.handle_conflict_open.zadacha_konflikta_naydena"),
        )
        return
    await query.answer()
    if not view.is_open:
        await _render_phase(
            update, context, state, notice=tr("admin.render_phase.zadacha_reshena")
        )
        return
    state["phase"] = "conflict_switch_confirm"
    await _render_phase(update, context, state)


def _conflict_result_notice(result: ConflictResolutionResult) -> str:
    if result.ok:
        return (
            f"✅ {result.message}"
            if result.message
            else tr("admin.conflict_result_notice.zadacha_reshena")
        )
    if result.status == "already_resolved":
        return tr("admin.conflict_result_notice.zadachu_reshili_drugom_okne")
    if result.status == "superseded":
        return tr("admin.conflict_result_notice.mestnyy_kirpichik_derzhit_etot")
    return result.message or tr("admin.handle_brick_proposal.reshenie_prinyato")


async def handle_conflict_switch_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "conflict_switch_confirm":
        await _stale(update)
        return
    conflict_id = _conflict_callback_id(str(query.data or ""))
    if conflict_id is None or state.get("conflict_id") != conflict_id:
        await _stale(update)
        return
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        try:
            result = resolve_conflict_by_switch(
                session,
                actor_telegram_user_id=user_id,
                conflict_id=conflict_id,
            )
        except (BrickPermissionError, ValueError) as exc:
            await query.answer(str(exc), show_alert=True)
            state["phase"] = "conflicts_list"
            await _render_phase(
                update,
                context,
                state,
                notice=tr(
                    "admin.handle_conflict_switch.polnomochiya_izmenilis_reshenie"
                ),
            )
            return
    finally:
        session.close()
    await query.answer()
    state["phase"] = "conflict_card"
    await _render_phase(
        update,
        context,
        state,
        notice=_conflict_result_notice(result),
    )


async def handle_conflict_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Новый код местного кирпичика в фазе переименования."""
    user = ctx.user(update)
    state = load_admin_session(user.id)
    if state is None or state.get("phase") != "conflict_rename_input":
        return
    conflict_id = state.get("conflict_id")
    if not isinstance(conflict_id, int) or isinstance(conflict_id, bool):
        await _stale(update)
        return
    raw = (ctx.message(update).text or "").strip()
    cleanup: list[int] = []
    message = getattr(update, "message", None)
    if message is not None and message.message_id is not None:
        cleanup.append(int(message.message_id))

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        try:
            result = resolve_conflict_by_rename(
                session,
                actor_telegram_user_id=user.id,
                conflict_id=conflict_id,
                new_code=raw,
            )
        except (BrickPermissionError, BrickConflictError) as exc:
            await _render_phase(
                update,
                context,
                state,
                notice=tr("admin.handle_conflict.kod_prinyat", exc=exc),
                cleanup_ids=cleanup,
                drop_input=True,
            )
            return
        except ValueError as exc:
            await _render_phase(
                update,
                context,
                state,
                notice=tr("admin.handle_conflict.kod_prinyat", exc=exc),
                cleanup_ids=cleanup,
                drop_input=True,
            )
            return
    finally:
        session.close()

    if result.ok:
        state["phase"] = "conflict_card"
        await _render_phase(
            update,
            context,
            state,
            notice=_conflict_result_notice(result),
            cleanup_ids=cleanup,
            drop_input=True,
        )
        return
    if result.status in {"conflict", "unchanged"}:
        await _render_phase(
            update,
            context,
            state,
            notice=_conflict_result_notice(result)
            + tr("admin.handle_conflict.vvedite_drugoy_kod"),
            cleanup_ids=cleanup,
            drop_input=True,
        )
        return
    state["phase"] = "conflict_card"
    await _render_phase(
        update,
        context,
        state,
        notice=_conflict_result_notice(result),
        cleanup_ids=cleanup,
        drop_input=True,
    )


def _conflict_notification_text(notification) -> str:
    """Плашка-уведомление администратору места (шаблон с разметкой в каталоге)."""
    place = notification.place_name or tr(
        "admin.conflict_short_label.mesto", place_id=notification.place_id
    )
    return tr(
        "admin.conflict.notification_html",
        place=place,
        code=notification.code,
        brick=notification.local_brick_title,
    )


async def _notify_conflict_admins(
    context: ContextTypes.DEFAULT_TYPE,
    conflict_ids: list[int] | None = None,
) -> tuple[int, int]:
    """
    Best-effort рассылка уведомлений из очереди `brick_conflict_notifications`.

    Возвращает `(доставлено, не доставлено)`: счётчик доставки растёт только
    после подтверждённой отправки, сбой отправки учитывается отдельно, чтобы
    итоговая плашка не сообщала «уведомлено» без доставки.

    Дубли невозможны: `mark_conflict_notification` переводит только `pending`,
    повторный прогон находит пустую очередь.
    """
    session = SessionLocal()
    delivered = 0
    undelivered = 0
    try:
        notifications = list_pending_conflict_notifications(
            session, conflict_ids=conflict_ids
        )
    finally:
        session.close()
    for notification in notifications:
        ok = await _send_user_notification(
            context,
            notification.telegram_user_id,
            _conflict_notification_text(notification),
        )
        session = SessionLocal()
        try:
            if mark_conflict_notification(
                session, notification.notification_id, delivered=ok
            ):
                if ok:
                    delivered += 1
                else:
                    undelivered += 1
        finally:
            session.close()
    return delivered, undelivered


async def _reports_denied(update: Update) -> bool:
    """
    Отказ не-суперадмину на ЛЮБОМ callback раздела «Репорты».

    Возвращает True, если доступ запрещён: обработчик обязан выйти. Роль
    берётся из `update.effective_user`, а не из callback-данных.
    """
    if is_superadmin(ctx.user(update).id):
        return False
    await ctx.query(update).answer(DENIED_ALERT, show_alert=True)
    return True


def _reports_state(update: Update) -> dict | None:
    """Состояние кабинета, если сейчас открыт раздел «Репорты»."""
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in REPORTS_PHASES:
        return None
    return state


def _reports_open_list(state: dict) -> None:
    """Сброс к списку действующих репортов."""
    state.clear()
    state.update(_new_state())
    state["phase"] = "reports_list"
    state["reports_offset"] = 0


async def handle_reports_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Вход в раздел «Репорты» из меню кабинета."""
    if await _reports_denied(update):
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "places":
        await _stale(update)
        return
    await ctx.query(update).answer()
    _reports_open_list(state)
    _reports_event(ctx.user(update).id, "admin_reports.list_opened")
    await _render_phase(update, context, state)


async def handle_reports_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Страница списка действующих репортов (смещение в callback)."""
    if await _reports_denied(update):
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "reports_list":
        await _stale(update)
        return
    try:
        offset = int((ctx.query(update).data or "")[len("rpts_page:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["reports_offset"] = max(offset, 0)
    await _render_phase(update, context, state)


async def handle_reports_archive(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Архив репортов (вход из списка и возврат из архивной карточки)."""
    if await _reports_denied(update):
        return
    state = _reports_state(update)
    if state is None or state.get("phase") not in {
        "reports_list",
        "report_archive_card",
        "reports_archive",
    }:
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["phase"] = "reports_archive"
    if not isinstance(state.get("reports_archive_offset"), int):
        state["reports_archive_offset"] = 0
    state.pop("report_archive_id", None)
    _reports_event(ctx.user(update).id, "admin_reports.archive_opened")
    await _render_phase(update, context, state)


async def handle_reports_archive_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Страница архива репортов."""
    if await _reports_denied(update):
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "reports_archive":
        await _stale(update)
        return
    try:
        offset = int((ctx.query(update).data or "")[len("rpts_archive_page:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["reports_archive_offset"] = max(offset, 0)
    await _render_phase(update, context, state)


async def handle_reports_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Карточка действующего репорта: полный текст и .txt-снимок."""
    if await _reports_denied(update):
        return
    state = _reports_state(update)
    if state is None or state.get("phase") not in {"reports_list", "report_card"}:
        await _stale(update)
        return
    report_id = _proposal_callback_id(str(ctx.query(update).data or ""))
    if report_id is None:
        await _stale(update)
        return
    with SessionLocal() as session:
        report = report_store.get_report(session, report_id)
    if report is None:
        await ctx.query(update).answer(tr("admin.reports.missing"), show_alert=True)
        return
    await ctx.query(update).answer()
    state["phase"] = "report_card"
    state["report_id"] = report_id
    state.pop("report_reply", None)
    state.pop("report_reply_delivered", None)
    state.pop("report_reply_changing", None)
    _reports_event(
        ctx.user(update).id, "admin_reports.report_opened", report_id=report_id
    )
    await _render_phase(update, context, state)
    # .txt-снимок идёт отдельным сообщением вместе с карточкой: он нужен и
    # для действующего репорта, и для уже закрытого (гонка двух окон).
    await _send_report_snapshot(context, ctx.chat(update).id, report)


async def handle_reports_archive_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Архивная карточка: ответ разработчика, дата закрытия, без действий."""
    if await _reports_denied(update):
        return
    state = _reports_state(update)
    if state is None or state.get("phase") not in {
        "reports_archive",
        "report_archive_card",
    }:
        await _stale(update)
        return
    report_id = _proposal_callback_id(str(ctx.query(update).data or ""))
    if report_id is None:
        await _stale(update)
        return
    with SessionLocal() as session:
        report = report_store.get_report(session, report_id)
    if report is None or not report.is_archived:
        await ctx.query(update).answer(tr("admin.reports.missing"), show_alert=True)
        return
    await ctx.query(update).answer()
    state["phase"] = "report_archive_card"
    state["report_archive_id"] = report_id
    state.pop("report_id", None)
    state.pop("report_reply", None)
    state.pop("report_reply_delivered", None)
    state.pop("report_reply_changing", None)
    _reports_event(
        ctx.user(update).id, "admin_reports.archive_opened", report_id=report_id
    )
    await _render_phase(update, context, state)
    await _send_report_snapshot(context, ctx.chat(update).id, report)


async def handle_reports_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«◀️ Назад» из карточки — к списку действующих репортов."""
    if await _reports_denied(update):
        return
    state = _reports_state(update)
    if state is None:
        await _stale(update)
        return
    await ctx.query(update).answer()
    state.pop("report_id", None)
    state.pop("report_reply", None)
    state.pop("report_reply_delivered", None)
    state.pop("report_reply_changing", None)
    state.pop("report_archive_id", None)
    state["phase"] = "reports_list"
    _reports_event(ctx.user(update).id, "admin_reports.list_opened")
    await _render_phase(update, context, state)


async def handle_reports_cabinet(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«🏠 В меню кабинета»: возврат в список мест/меню кабинета."""
    if await _reports_denied(update):
        return
    state = _reports_state(update)
    if state is None:
        await _stale(update)
        return
    await ctx.query(update).answer()
    state.clear()
    state.update(_new_state())
    await _render_phase(update, context, state)


async def handle_report_reply_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«Решено»: шаг 1 — приглашение написать ответ пользователю."""
    if await _reports_denied(update):
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "report_card":
        await _stale(update)
        return
    report_id = _proposal_callback_id(str(ctx.query(update).data or ""))
    if report_id is None or state.get("report_id") != report_id:
        await _stale(update)
        return
    with SessionLocal() as session:
        report = report_store.get_report(session, report_id)
    if report is None or not report.is_active:
        # Репорт закрыли в другом окне до начала ответа.
        await ctx.query(update).answer(
            tr("admin.reports.already_closed"), show_alert=True
        )
        state.pop("report_reply", None)
        state.pop("report_reply_delivered", None)
        await _render_phase(update, context, state)
        return
    await ctx.query(update).answer()
    state["phase"] = "report_reply_input"
    state.pop("report_reply", None)
    state.pop("report_reply_delivered", None)
    state.pop("report_reply_changing", None)
    _reports_event(
        ctx.user(update).id, "admin_reports.reply_started", report_id=report_id
    )
    await _render_phase(update, context, state)


async def handle_report_reply_change(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«Изменить» в предпросмотре: вернуться к вводу, текст сохранён."""
    if await _reports_denied(update):
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "report_reply_preview":
        await _stale(update)
        return
    if not isinstance(state.get("report_reply"), str):
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["phase"] = "report_reply_input"
    state["report_reply_changing"] = True
    await _render_phase(update, context, state)


async def handle_report_reply_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«Отмена» ответа: возврат к карточке, репорт остаётся активным."""
    if await _reports_denied(update):
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "report_reply_input",
        "report_reply_preview",
    }:
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["phase"] = "report_card"
    state.pop("report_reply", None)
    state.pop("report_reply_delivered", None)
    state.pop("report_reply_changing", None)
    _reports_event(
        ctx.user(update).id,
        "admin_reports.reply_cancelled",
        report_id=(
            state.get("report_id") if isinstance(state.get("report_id"), int) else None
        ),
    )
    await _render_phase(update, context, state)


async def handle_report_reply_send(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    «Отправить»: сообщение автору репорта, затем закрытие репорта.

    Порядок жёсткий: сначала проверка актуального статуса, затем доставка
    на `reports.chat_id`, и только после подтверждённой доставки — запись
    ответа и атомарное закрытие (`archive_report`, `WHERE status='active'`).
    Сбой доставки оставляет репорт активным и даёт возможность повтора;
    повторное нажатие после успеха ничего не дублирует.

    Длинный ответ уходит несколькими частями. Прогресс фиксируется в
    состоянии после КАЖДОЙ подтверждённой части, поэтому сбой посередине и
    повтор отправляют только недостающие части: заголовок «Спасибо за
    репорт!» (он в первой части) и уже доставленный текст не дублируются.
    """
    if await _reports_denied(update):
        return
    user_id = ctx.user(update).id
    state = load_admin_session(user_id)
    if state is None or state.get("phase") not in {
        "report_reply_input",
        "report_reply_preview",
        "report_card",
    }:
        await _stale(update)
        return
    report_id = state.get("report_id")
    reply = state.get("report_reply")
    if not isinstance(report_id, int) or isinstance(report_id, bool):
        await _stale(update)
        return

    with SessionLocal() as session:
        report = report_store.get_report(session, report_id)
    if report is None:
        await ctx.query(update).answer(tr("admin.reports.missing"), show_alert=True)
        return
    if not report.is_active:
        # Другой суперадмин уже закрыл репорт — второй ответ невозможен.
        # Экран переводится в архивную карточку: администратор видит ответ
        # первого и дату закрытия, а не приглашение писать в закрытый репорт
        # (иначе повторный ввод снова упирается в отказ).
        await ctx.query(update).answer(
            tr("admin.reports.already_closed"), show_alert=True
        )
        state["phase"] = "report_archive_card"
        state["report_archive_id"] = report.id
        state.pop("report_id", None)
        state.pop("report_reply", None)
        state.pop("report_reply_delivered", None)
        state.pop("report_reply_changing", None)
        await _render_phase(update, context, state)
        return
    if not isinstance(reply, str) or not reply.strip():
        await _stale(update)
        return

    # Доставка автору репорта: адрес — сохранённый chat_id репорта, а не
    # чат администратора. Длинный ответ (после экранирования HTML) уходит
    # несколькими частями без потери данных.
    #
    # Повтор после сбоя не дублирует сообщение: в состоянии запоминается
    # текст и число подтверждённых частей, поэтому дошлются только
    # недостающие. Совпадение текста — единственный признак; новый ответ
    # пользователь вводит заново, и он отличается.
    parts = _report_reply_parts(reply)
    delivered_parts = _report_reply_delivered_parts(state, reply)
    if delivered_parts < len(parts):
        for index in range(delivered_parts, len(parts)):
            try:
                await context.bot.send_message(
                    chat_id=report.chat_id,
                    text=parts[index],
                    parse_mode="HTML",
                )
            except Exception as exc:  # noqa: BLE001 — репорт остаётся активным
                logger.warning("Ответ на репорт %s не доставлен: %s", report_id, exc)
                _reports_event(
                    user_id,
                    "admin_reports.reply_failed",
                    report_id=report_id,
                    result="error",
                    detail=f"error={type(exc).__name__}",
                )
                await ctx.query(update).answer(
                    tr("admin.reports.reply_failed"), show_alert=True
                )
                return
            # Фиксируем прогресс ПОСЛЕ подтверждённой части: при сбое
            # посередине повтор дошлёт только недостающее и не продублирует
            # ни заголовок, ни уже ушедший текст.
            state["report_reply_delivered"] = {"text": reply, "parts": index + 1}
            if not save_admin_session(user_id, state):
                logger.warning(
                    "Прогресс доставки ответа на репорт %s не сохранён.", report_id
                )

    # Доставка подтверждена: сохраняем ответ и закрываем репорт ОДНИМ
    # условным UPDATE (`status='active'`) — ответ и статус не расходятся,
    # а гонка двух закрытий безопасна.
    closed = False
    with SessionLocal() as session:
        try:
            closed = bool(report_store.archive_report(session, report_id, reply=reply))
        except Exception as exc:  # noqa: BLE001 — доставлено, но не сохранено
            logger.error("Ответ на репорт %s не сохранён: %s", report_id, exc)
            _reports_event(
                user_id,
                "admin_reports.reply_failed",
                report_id=report_id,
                result="error",
                detail=f"error={type(exc).__name__}",
            )
            await ctx.query(update).answer(
                tr("admin.reports.reply_failed"), show_alert=True
            )
            return

    state["phase"] = "report_card"
    state.pop("report_reply", None)
    state.pop("report_reply_delivered", None)
    state.pop("report_reply_changing", None)
    if not closed:
        # Гонка двух закрытий: сообщение ушло, но репорт закрыл другой
        # администратор — повторно ничего не отправляем.
        await ctx.query(update).answer(
            tr("admin.reports.already_closed"), show_alert=True
        )
        _reports_event(
            user_id,
            "admin_reports.reply_failed",
            report_id=report_id,
            result="error",
            detail="reason=already_closed",
        )
        await _render_phase(update, context, state)
        return
    await ctx.query(update).answer(tr("admin.reports.reply_sent"))
    _reports_event(user_id, "admin_reports.reply_sent", report_id=report_id)
    await _render_phase(update, context, state, notice=tr("admin.reports.reply_sent"))


async def handle_report_reply_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Шаг 2: текст ответа обычным сообщением — только в своей фазе."""
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "report_reply_input":
        return
    if not is_superadmin(ctx.user(update).id):
        clear_admin_session(ctx.user(update).id)
        await _render_denied(update, context)
        return
    raw = ctx.message(update).text or ""
    cleanup: list[int] = []
    message = getattr(update, "message", None)
    if message is not None and message.message_id is not None:
        cleanup.append(int(message.message_id))
    if not raw.strip():
        await _render_phase(
            update,
            context,
            state,
            notice=tr("admin.reports.reply_empty"),
            cleanup_ids=cleanup,
            drop_input=True,
        )
        return
    if len(raw) > MAX_REPORT_REPLY:
        await _render_phase(
            update,
            context,
            state,
            notice=tr("report.screen.too_long", limit=MAX_REPORT_REPLY),
            cleanup_ids=cleanup,
            drop_input=True,
        )
        return
    state["report_reply"] = raw
    state["phase"] = "report_reply_preview"
    await _render_phase(update, context, state, cleanup_ids=cleanup, drop_input=True)


async def handle_admin_stale(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Любая необработанная кнопка adm_* — устаревшая, прав не меняет."""
    await _stale(update)


def admin_handlers() -> list[BaseHandler]:
    """
    Обработчики кабинета Stage 20A/20B.

    Регистрируются после мастера поста и «Мест», но до общего возврата в меню:
    callback-и админки не пересекаются с их шаблонами. Текстовые обработчики
    активны только в явных фазах кабинета: поиск получателя и поля
    редактирования места.
    """
    return [
        CommandHandler("admin", open_admin),
        CallbackQueryHandler(open_admin, pattern=r"^admin_panel$"),
        CallbackQueryHandler(handle_places_page, pattern=r"^adm_page:\d+$"),
        CallbackQueryHandler(handle_open_place, pattern=r"^adm_place:\d+$"),
        CallbackQueryHandler(handle_assign, pattern=r"^adm_assign:\d+$"),
        CallbackQueryHandler(handle_replace, pattern=r"^adm_replace:\d+$"),
        CallbackQueryHandler(handle_revoke, pattern=r"^adm_revoke:\d+$"),
        CallbackQueryHandler(handle_pick_user, pattern=r"^adm_pick:\d+$"),
        CallbackQueryHandler(handle_search_again, pattern=r"^adm_search_again$"),
        CallbackQueryHandler(handle_cancel_confirm, pattern=r"^adm_cancel_confirm$"),
        CallbackQueryHandler(handle_cancel_flow, pattern=r"^adm_cancel$"),
        CallbackQueryHandler(handle_places_back, pattern=r"^adm_places$"),
        # Poster 2.4.1 T2: ссылка на создание анонса — экран полной карточки.
        # Регистрируется до финального `^adm_`: иначе кнопки получали бы
        # «устаревшую» заглушку.
        CallbackQueryHandler(handle_invite_open, pattern=r"^adm_invite:\d+$"),
        CallbackQueryHandler(
            handle_invite_regenerate, pattern=r"^adm_invite_regen:\d+$"
        ),
        CallbackQueryHandler(handle_invite_back, pattern=r"^adm_invite_back$"),
        # Poster 2.5 A3: подраздел «Управление администратором» (вход/возврат).
        CallbackQueryHandler(
            handle_place_admins_open, pattern=r"^adm_place_admins:\d+$"
        ),
        CallbackQueryHandler(
            handle_place_admins_back, pattern=r"^adm_place_admins_back$"
        ),
        CallbackQueryHandler(handle_confirm, pattern=r"^adm_confirm$"),
        # Редактирование данных места (Stage 20B).
        CallbackQueryHandler(handle_edit_open, pattern=r"^adm_edit_place:\d+$"),
        CallbackQueryHandler(handle_edit_name, pattern=r"^adm_edit_name:\d+$"),
        CallbackQueryHandler(handle_aliases, pattern=r"^adm_aliases:\d+$"),
        CallbackQueryHandler(handle_alias_add, pattern=r"^adm_alias_add:\d+$"),
        CallbackQueryHandler(handle_alias_rename, pattern=r"^adm_alias_rename:\d+$"),
        CallbackQueryHandler(handle_alias_delete, pattern=r"^adm_alias_delete:\d+$"),
        CallbackQueryHandler(handle_alias_page, pattern=r"^adm_alias_page:\d+$"),
        CallbackQueryHandler(handle_edit_address, pattern=r"^adm_edit_address:\d+$"),
        CallbackQueryHandler(
            handle_edit_address_delete,
            pattern=r"^adm_edit_address_delete:\d+$",
        ),
        CallbackQueryHandler(handle_edit_city, pattern=r"^adm_edit_city:\d+$"),
        CallbackQueryHandler(handle_edit_url, pattern=r"^adm_edit_url:\d+$"),
        CallbackQueryHandler(
            handle_edit_url_delete, pattern=r"^adm_edit_url_delete:\d+$"
        ),
        CallbackQueryHandler(handle_edit_confirm, pattern=r"^adm_edit_confirm$"),
        # Передача администратора места (Stage 21).
        CallbackQueryHandler(handle_transfer_open, pattern=r"^adm_transfer:\d+$"),
        CallbackQueryHandler(handle_transfer_pick, pattern=r"^adm_transfer_pick:\d+$"),
        CallbackQueryHandler(
            handle_transfer_confirm, pattern=r"^adm_transfer_confirm$"
        ),
        CallbackQueryHandler(
            handle_transfer_approve, pattern=r"^adm_transfer_approve:\d+$"
        ),
        CallbackQueryHandler(
            handle_transfer_notify, pattern=r"^adm_transfer_notify:\d+$"
        ),
        CallbackQueryHandler(
            handle_transfer_refresh, pattern=r"^adm_transfer_refresh:\d+$"
        ),
        CallbackQueryHandler(
            handle_transfer_cancel, pattern=r"^adm_transfer_cancel:\d+$"
        ),
        CallbackQueryHandler(
            handle_transfer_accept, pattern=r"^adm_transfer_accept:\d+$"
        ),
        CallbackQueryHandler(
            handle_transfer_decline, pattern=r"^adm_transfer_decline:\d+$"
        ),
        # Предложения кирпичиков (Stage 26).
        CallbackQueryHandler(
            handle_brick_proposals_global, pattern=r"^adm_bprop_global$"
        ),
        CallbackQueryHandler(
            handle_brick_proposals_global_page,
            pattern=r"^adm_bprop_global_page:\d+$",
        ),
        CallbackQueryHandler(
            handle_brick_proposals_places, pattern=r"^adm_bprop_places$"
        ),
        CallbackQueryHandler(
            handle_brick_proposals_places_page,
            pattern=r"^adm_bprop_places_page:\d+$",
        ),
        CallbackQueryHandler(
            handle_brick_proposals_place, pattern=r"^adm_bprop_place:\d+$"
        ),
        CallbackQueryHandler(
            handle_brick_proposals_open, pattern=r"^adm_bprop_open:\d+$"
        ),
        CallbackQueryHandler(
            handle_brick_proposal_approve, pattern=r"^adm_bprop_approve:\d+$"
        ),
        CallbackQueryHandler(
            handle_brick_proposal_reject, pattern=r"^adm_bprop_reject:\d+$"
        ),
        CallbackQueryHandler(
            handle_brick_proposal_approve_confirm,
            pattern=r"^adm_bprop_approve_yes:\d+$",
        ),
        CallbackQueryHandler(
            handle_brick_proposal_reject_confirm,
            pattern=r"^adm_bprop_reject_yes:\d+$",
        ),
        CallbackQueryHandler(handle_brick_proposals_back, pattern=r"^adm_bprop_back$"),
        # Poster 2.4 P1: административный редактор ОПИСАНИЯ кирпичика.
        # Регистрируется до финального `^adm_`: иначе кнопки получали бы
        # «устаревшую» заглушку.
        CallbackQueryHandler(
            handle_admin_bricks, pattern=r"^adm_bricks(?:_global|:\d+)$"
        ),
        CallbackQueryHandler(
            handle_admin_bricks_page, pattern=r"^adm_bricks_page:\d+$"
        ),
        # Poster 2.5 A4 (§13): локальный кирпичик из списка площадки.
        CallbackQueryHandler(handle_brick_create, pattern=r"^adm_brick_new:\d+$"),
        CallbackQueryHandler(handle_brick_card, pattern=r"^adm_bedit:\d+$"),
        CallbackQueryHandler(
            handle_brick_description, pattern=r"^adm_bedit_description:\d+$"
        ),
        CallbackQueryHandler(
            handle_brick_card_back, pattern=r"^adm_bedit_back(?::\d+)?$"
        ),
        AdminTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_brick_description_text,
            allowed_phases=frozenset({"brick_description_input"}),
        ),
        # Конфликты кодов (Poster 2.1 R3).
        CallbackQueryHandler(handle_conflicts_open, pattern=r"^adm_bconflicts:\d+$"),
        CallbackQueryHandler(
            handle_conflicts_page, pattern=r"^adm_bconflicts_page:\d+$"
        ),
        CallbackQueryHandler(handle_conflict_open, pattern=r"^adm_bconflict:\d+$"),
        CallbackQueryHandler(
            handle_conflict_rename, pattern=r"^adm_bconflict_rename:\d+$"
        ),
        CallbackQueryHandler(
            handle_conflict_switch, pattern=r"^adm_bconflict_switch:\d+$"
        ),
        CallbackQueryHandler(
            handle_conflict_switch_confirm,
            pattern=r"^adm_bconflict_switch_yes:\d+$",
        ),
        AdminTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_conflict_text,
            allowed_phases=frozenset({"conflict_rename_input"}),
        ),
        AdminTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_search_text,
            allowed_phases=frozenset({"assign_search"}),
        ),
        AdminTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_edit_text,
            allowed_phases=EDIT_INPUT_PHASES,
        ),
        AdminTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_transfer_text,
            allowed_phases=TRANSFER_INPUT_PHASES,
        ),
        # Poster 2.5.5 D: раздел «Репорты» (суперадмин). Префикс `rpts_` —
        # вне `adm_`, иначе финальный stale-обработчик `^adm_` перехватывал
        # бы неизвестные (и будущие) кнопки раздела.
        CallbackQueryHandler(handle_reports_open, pattern=r"^rpts_list$"),
        CallbackQueryHandler(handle_reports_page, pattern=r"^rpts_page:\d+$"),
        CallbackQueryHandler(handle_reports_archive, pattern=r"^rpts_archive$"),
        CallbackQueryHandler(
            handle_reports_archive_page, pattern=r"^rpts_archive_page:\d+$"
        ),
        CallbackQueryHandler(handle_reports_card, pattern=r"^rpts_card:\d+$"),
        CallbackQueryHandler(handle_reports_archive_card, pattern=r"^rpts_acard:\d+$"),
        CallbackQueryHandler(handle_reports_back, pattern=r"^rpts_back$"),
        CallbackQueryHandler(handle_reports_cabinet, pattern=r"^rpts_cabinet$"),
        CallbackQueryHandler(handle_report_reply_start, pattern=r"^rpts_reply:\d+$"),
        CallbackQueryHandler(
            handle_report_reply_change, pattern=r"^rpts_reply_change$"
        ),
        CallbackQueryHandler(
            handle_report_reply_cancel, pattern=r"^rpts_reply_cancel$"
        ),
        CallbackQueryHandler(handle_report_reply_send, pattern=r"^rpts_reply_send$"),
        AdminTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_report_reply_text,
            allowed_phases=frozenset({"report_reply_input"}),
        ),
        CallbackQueryHandler(handle_admin_stale, pattern=r"^adm_"),
    ]
