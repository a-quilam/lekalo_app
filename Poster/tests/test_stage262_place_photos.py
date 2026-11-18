# tests/test_stage262_place_photos.py
"""
Poster 2.6.2 §3.3–3.4: фототека площадки и раздел «Фото площадки» в мастере.

Контракт:

1. загрузка идёт через `file_id` из Update — бинарных копий нет, сеть не
   нужна; принимаются фото и документы с MIME `image/*`;
2. не-изображения отклоняются понятной плашкой, библиотека не меняется;
3. дубль по `file_unique_id` не создаёт вторую строку;
4. список стабилен: номера идут по порядку добавления, повторный вход не
   пересортировывает библиотеку;
5. удаление — только с подтверждением; «Отмена» ничего не удаляет;
6. раздел «Фото площадки» на шаге фото виден лишь при НЕПУСТОЙ библиотеке
   ЭТОЙ площадки; выбор — тумблер без автодобавления;
7. свои снимки и библиотечные объединяются в общем лимите 10;
8. удаление строки библиотеки не ломает черновик: он хранит `file_id`;
9. библиотеки площадок изолированы, подделанный `place_id` отклоняется;
10. права проверяются по фактическим строкам БД на каждом действии;
11. выбор переживает рестарт службы;
12. «◀️ Назад» с каждого нового экрана ведёт на своего родителя.

Все сценарии идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (`tests.integration_harness`); рабочая база, `.env` и сеть
Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage262_place_photos -v
"""

from __future__ import annotations

import asyncio
import unittest

from sqlalchemy import select
from telegram.error import BadRequest

from announcement_store import MAX_ANNOUNCEMENT_PHOTOS
from models import PlacePhoto
from place_photo_store import (
    PLACE_PHOTO_MAX_BYTES,
    PhotoCandidate,
    add_place_photos,
    delete_place_photo,
    list_place_photos,
)
from tests import ui_texts_expect as cat
from tests.integration_harness import (
    DEFAULT_USER_ID,
    HARNESS_IMPORT_ERROR,
    Harness,
    scenario_test,
)

ADMIN_TG = 2_621_001
STRANGER_TG = 2_621_002
OTHER_ADMIN_TG = 2_621_003


# --- помощники ---------------------------------------------------------------


def _rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _callbacks(h: Harness) -> list[str | None]:
    return [data for _label, data in h.screen_buttons()]


def _last_alert(h: Harness) -> str:
    answers = h.answers()
    if not answers:
        return ""
    record = answers[-1]
    args = tuple(record.get("args") or ())
    if args and isinstance(args[0], str):
        return args[0]
    return str(record.get("text") or "")


def _labels(h: Harness) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _library(h: Harness, place_id: int) -> list[PlacePhoto]:
    with h.session_factory() as session:
        return list(
            session.scalars(
                select(PlacePhoto)
                .where(PlacePhoto.place_id == place_id)
                .order_by(PlacePhoto.id)
            ).all()
        )


def _seed_library(
    h: Harness,
    place_id: int,
    count: int,
    *,
    actor_tg: int = ADMIN_TG,
    prefix: str = "lib",
) -> list[str]:
    """Кладёт в фототеку `count` снимков напрямую через store (быстрый сев)."""
    file_ids = [f"{prefix}{index}" for index in range(1, count + 1)]
    with h.session_factory() as session:
        add_place_photos(
            session,
            actor_telegram_user_id=actor_tg,
            place_id=place_id,
            candidates=[
                PhotoCandidate(file_id=value, file_unique_id=f"u_{value}")
                for value in file_ids
            ],
        )
    return file_ids


def _make_place(h: Harness, name: str, *, admin_tg: int | None = ADMIN_TG) -> int:
    from models import PlaceAdmin, utcnow
    from place_store import create_place
    from user_store import get_or_create_user

    with h.session_factory() as session:
        user = None
        if admin_tg is not None:
            user = get_or_create_user(
                session, admin_tg, username=f"pp{admin_tg}", full_name="Админ места"
            )
        place = create_place(
            session,
            name,
            city="Город фототеки",
            created_by_user_id=user.id if user is not None else None,
        )
        if user is not None:
            session.add(
                PlaceAdmin(place_id=place.id, user_id=user.id, assigned_at=utcnow())
            )
        session.commit()
        return int(place.id)


async def _open_library(h: Harness, place_id: int, *, tg: int = ADMIN_TG) -> None:
    await h.text("/admin", user_id=tg)
    await h.callback(f"adm_place:{place_id}", user_id=tg)
    await h.callback(f"adm_pfotos:{place_id}", user_id=tg)


async def _to_photo_step(h: Harness, *, place_id: int, tg: int = DEFAULT_USER_ID):
    """Доводит мастер до шага фото для указанной площадки."""
    from tests.test_stage27b_announcement_ui import (
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _start_to_template,
    )

    template_id, _revision, _bricks = _seed_personal_template(h, content="[main]")
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text("Значение")
    if cat.text("wizard.photos.title") not in h.screen_text():
        raise AssertionError(f"Не шаг фото: {h.screen_text()[:300]!r}")
    return _latest_draft(h)


def _library_callback(h: Harness) -> str:
    for data in _callbacks(h):
        if (data or "").startswith("aw_photolib:"):
            return str(data)
    raise AssertionError(f"Нет кнопки фототеки: {h.screen_buttons()}")


def _toggle_callbacks(h: Harness) -> list[str]:
    return [
        str(data)
        for data in _callbacks(h)
        if (data or "").startswith("aw_phototoggle:")
    ]


# --- §3.3: загрузка ----------------------------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlacePhotoUploadTests(unittest.TestCase):
    """Пункты 1–5: приём файлов, дубли, тип и размер."""

    @scenario_test
    async def test_single_photo_is_stored_by_file_id_only(self, h: Harness) -> None:
        """Пункт 1: `file_id` записан; бинарных копий и скачивания нет."""
        place_id = _make_place(h, "Дом культуры")
        await _open_library(h, place_id)

        await h.photo("PHOTO_1", user_id=ADMIN_TG)

        photos = _library(h, place_id)
        self.assertEqual([row.file_id for row in photos], ["PHOTO_1"])
        self.assertEqual([row.file_unique_id for row in photos], ["u_PHOTO_1"])
        # Бинарных копий нет: продукт не запрашивает файл у Telegram.
        self.assertEqual(
            [call for call in h.calls if call["type"] == "get_file"],
            [],
            "фототека не должна скачивать вложение",
        )
        # Снимок пользователя удалён из чата: он уже в библиотеке.
        self.assertTrue(h.deleted_ids(), "сообщение с фото не убрано из чата")

    @scenario_test
    async def test_media_group_keeps_every_photo(self, h: Harness) -> None:
        """Пункт 2: альбом — все снимки в библиотеке, порядок сохранён."""
        place_id = _make_place(h, "Дом культуры")
        await _open_library(h, place_id)

        for index in range(1, 4):
            await h.photo(f"ALBUM_{index}", media_group_id="g-1", user_id=ADMIN_TG)

        self.assertEqual(
            [row.file_id for row in _library(h, place_id)],
            ["ALBUM_1", "ALBUM_2", "ALBUM_3"],
        )

    @scenario_test
    async def test_duplicate_unique_id_is_not_added_twice(self, h: Harness) -> None:
        """Пункт 3: тот же `file_unique_id` второй раз строку не создаёт."""
        place_id = _make_place(h, "Дом культуры")
        await _open_library(h, place_id)

        await h.photo("PHOTO_1", user_id=ADMIN_TG)
        await h.photo("PHOTO_1", user_id=ADMIN_TG)

        self.assertEqual(len(_library(h, place_id)), 1)
        cat.assert_shows(self, h.screen_text(), "admin.place_photos.uzhe_v_biblioteke")

    @scenario_test
    async def test_non_image_document_is_rejected(self, h: Harness) -> None:
        """Пункт 4: PDF отклонён понятным сообщением, библиотека пуста."""
        place_id = _make_place(h, "Дом культуры")
        await _open_library(h, place_id)

        await h.document("DOC_1", mime_type="application/pdf", user_id=ADMIN_TG)

        self.assertEqual(_library(h, place_id), [])
        self.assertIn("не изображение", h.screen_text())
        self.assertNotIn("Ошибка", h.screen_text())

    @scenario_test
    async def test_image_document_is_accepted(self, h: Harness) -> None:
        """Пункт 5: документ с MIME `image/*` принимается как снимок."""
        place_id = _make_place(h, "Дом культуры")
        await _open_library(h, place_id)

        await h.document(
            "DOC_IMG", mime_type="image/png", file_size=1024, user_id=ADMIN_TG
        )

        self.assertEqual([row.file_id for row in _library(h, place_id)], ["DOC_IMG"])

    @scenario_test
    async def test_oversized_file_is_rejected_by_single_limit(self, h: Harness) -> None:
        """Лимит размера — одна константа: больше неё файл не принимается."""
        place_id = _make_place(h, "Дом культуры")
        await _open_library(h, place_id)

        await h.document(
            "DOC_BIG",
            mime_type="image/jpeg",
            file_size=PLACE_PHOTO_MAX_BYTES + 1,
            user_id=ADMIN_TG,
        )

        self.assertEqual(_library(h, place_id), [])
        self.assertIn("МБ", h.screen_text())


# --- §3.3: список, удаление, изоляция ---------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlacePhotoListTests(unittest.TestCase):
    """Пункты 6, 7, 13: список с номерами, удаление, изоляция площадок."""

    @scenario_test
    async def test_list_numbers_are_stable_between_visits(self, h: Harness) -> None:
        """Пункт 6: номера идут по порядку добавления и не сдвигаются."""
        place_id = _make_place(h, "Дом культуры")
        _seed_library(h, place_id, 3)
        await _open_library(h, place_id)

        first = [label for label in _labels(h) if "Снимок" in label]
        self.assertEqual(len(first), 3)
        self.assertEqual(
            [
                data
                for _l, data in h.screen_buttons()
                if (data or "").startswith("adm_pfoto_del:")
            ],
            [f"adm_pfoto_del:{row.id}" for row in _library(h, place_id)],
        )

        await h.callback("adm_pfoto_back", user_id=ADMIN_TG)
        await h.callback(f"adm_pfotos:{place_id}", user_id=ADMIN_TG)
        self.assertEqual([label for label in _labels(h) if "Снимок" in label], first)

        # Новый снимок дописывается в конец: прежние номера не меняются.
        await h.photo("PHOTO_4", user_id=ADMIN_TG)
        self.assertEqual(
            [row.file_id for row in _library(h, place_id)],
            ["lib1", "lib2", "lib3", "PHOTO_4"],
        )

    @scenario_test
    async def test_delete_requires_confirmation(self, h: Harness) -> None:
        """Пункт 7: «Отмена» не удаляет, подтверждение удаляет ровно один."""
        place_id = _make_place(h, "Дом культуры")
        _seed_library(h, place_id, 2)
        await _open_library(h, place_id)
        photo_id = _library(h, place_id)[1].id

        await h.callback(f"adm_pfoto_del:{photo_id}", user_id=ADMIN_TG)
        cat.assert_shows(
            self, h.screen_text(), "admin.place_photos.udalit_foto_podtverzhdenie_html"
        )
        await h.callback("adm_pfoto_back", user_id=ADMIN_TG)
        self.assertEqual(len(_library(h, place_id)), 2)

        await h.callback(f"adm_pfoto_del:{photo_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_pfoto_del_yes:{photo_id}", user_id=ADMIN_TG)
        self.assertEqual([row.file_id for row in _library(h, place_id)], ["lib1"])

    @scenario_test
    async def test_libraries_of_different_places_are_isolated(self, h: Harness) -> None:
        """Пункт 13: чужая библиотека не видна и не смешивается."""
        first = _make_place(h, "Дом культуры")
        second = _make_place(h, "Клуб")
        _seed_library(h, first, 2, prefix="first")
        _seed_library(h, second, 1, prefix="second")

        await _open_library(h, first)
        self.assertEqual(
            [row.file_id for row in _library(h, first)], ["first1", "first2"]
        )
        self.assertIn("Снимков в фототеке: 2", h.screen_text())
        self.assertNotIn("second1", h.screen_text())

        await h.callback(f"adm_pfotos:{second}", user_id=ADMIN_TG)
        self.assertIn("Снимков в фототеке: 1", h.screen_text())


# --- §3.4: раздел «Фото площадки» в мастере ---------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PhotoLibraryInWizardTests(unittest.TestCase):
    """Пункты 8–12, 14: раздел на шаге фото, тумблеры, лимит, устойчивость."""

    @scenario_test
    async def test_section_is_hidden_for_empty_library(self, h: Harness) -> None:
        """Пункт 8: пустая библиотека — кнопки «Фото площадки» нет."""
        from tests.test_stage27b_announcement_ui import _seed_place

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        await _to_photo_step(h, place_id=place_id)

        self.assertFalse(_toggle_callbacks(h))
        self.assertFalse(
            [data for data in _callbacks(h) if (data or "").startswith("aw_photolib:")],
            f"раздел показан при пустой библиотеке: {h.screen_buttons()}",
        )

    @scenario_test
    async def test_toggle_selects_without_autoadd(self, h: Harness) -> None:
        """Пункт 9: раздел виден, выбор — тумблер, автодобавления нет."""
        from tests.test_stage27b_announcement_ui import _seed_place

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        _seed_library(h, place_id, 2, actor_tg=DEFAULT_USER_ID)
        draft = await _to_photo_step(h, place_id=place_id)
        self.assertEqual(_draft_photos(h, draft.id), [])

        cat.assert_shows(self, h.screen_text(), "wizard.photos.library_hint")
        await h.callback(_library_callback(h), user_id=DEFAULT_USER_ID)
        cat.assert_shows(self, h.screen_text(), "wizard.photo_library.title")
        # Открытие раздела ничего не выбрало.
        self.assertEqual(tuple(_draft_photos(h, draft.id)), ())

        await h.callback(_toggle_callbacks(h)[0], user_id=DEFAULT_USER_ID)
        self.assertEqual(tuple(_draft_photos(h, draft.id)), ("lib1",))
        cat.assert_shows(self, _last_alert(h), "wizard.photo_library.vybrano")

        # Повторное нажатие снимает выбор.
        await h.callback(_toggle_callbacks(h)[0], user_id=DEFAULT_USER_ID)
        self.assertEqual(tuple(_draft_photos(h, draft.id)), ())
        cat.assert_shows(self, _last_alert(h), "wizard.photo_library.snyato")

    @scenario_test
    async def test_own_and_library_photos_share_limit_of_ten(self, h: Harness) -> None:
        """Пункт 10: свои + библиотечные объединяются; 11-й снимок — отказ."""
        from tests.test_stage27b_announcement_ui import _seed_place

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        _seed_library(h, place_id, 3, actor_tg=DEFAULT_USER_ID)
        draft = await _to_photo_step(h, place_id=place_id)

        # Два библиотечных снимка выбраны тумблерами.
        await h.callback(_library_callback(h), user_id=DEFAULT_USER_ID)
        await h.callback(_toggle_callbacks(h)[0], user_id=DEFAULT_USER_ID)
        await h.callback(_toggle_callbacks(h)[1], user_id=DEFAULT_USER_ID)
        self.assertEqual(_draft_photos(h, draft.id), ["lib1", "lib2"])

        # Альбом своих снимков объединяется с выбранными: 2 + 8 = лимит.
        await h.callback(_back_from_library(h), user_id=DEFAULT_USER_ID)
        for index in range(
            len(_draft_photos(h, draft.id)), MAX_ANNOUNCEMENT_PHOTOS + 1
        ):
            await h.photo(
                f"OWN_{index}", media_group_id="mg-limit", user_id=DEFAULT_USER_ID
            )
        await asyncio.sleep(0.4)
        photos = _draft_photos(h, draft.id)
        self.assertEqual(len(photos), MAX_ANNOUNCEMENT_PHOTOS)
        self.assertEqual(photos[:2], ["lib1", "lib2"])
        self.assertIn("OWN_2", photos)
        cat.assert_shows(self, h.screen_text(), "wizard.photos.limit")

        # 11-й снимок — библиотечный: отказ, выбранные не теряются.
        await h.callback(_library_callback(h), user_id=DEFAULT_USER_ID)
        await h.callback(_toggle_callbacks(h)[2], user_id=DEFAULT_USER_ID)
        self.assertEqual(_draft_photos(h, draft.id), photos)
        cat.assert_shows(self, _last_alert(h), "wizard.photo_library.limit")

    @scenario_test
    async def test_back_without_choice_keeps_draft(self, h: Harness) -> None:
        """Пункт 11: выход из раздела без выбора не меняет черновик."""
        from tests.test_stage27b_announcement_ui import _seed_place

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        _seed_library(h, place_id, 2, actor_tg=DEFAULT_USER_ID)
        draft = await _to_photo_step(h, place_id=place_id)
        revision = draft.revision

        await h.callback(_library_callback(h), user_id=DEFAULT_USER_ID)
        await h.callback(_back_from_library(h), user_id=DEFAULT_USER_ID)

        cat.assert_shows(self, h.screen_text(), "wizard.photos.title")
        after = _draft_state(h, draft.id)
        self.assertEqual(tuple(after.photos), ())
        self.assertEqual(after.revision, revision)
        self.assertEqual(after.stage, draft.stage)

    @scenario_test
    async def test_library_deletion_does_not_break_draft(self, h: Harness) -> None:
        """Пункт 12: удаление строки библиотеки не трогает выбранное фото."""
        from tests.test_stage27b_announcement_ui import _seed_place

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        _seed_library(h, place_id, 1, actor_tg=DEFAULT_USER_ID)
        draft = await _to_photo_step(h, place_id=place_id)
        await h.callback(_library_callback(h), user_id=DEFAULT_USER_ID)
        await h.callback(_toggle_callbacks(h)[0], user_id=DEFAULT_USER_ID)
        self.assertEqual(_draft_photos(h, draft.id), ["lib1"])

        photo_id = _library(h, place_id)[0].id
        with h.session_factory() as session:
            delete_place_photo(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                place_id=place_id,
                photo_id=photo_id,
            )
        self.assertEqual(_library(h, place_id), [])

        await h.callback(_back_from_library(h), user_id=DEFAULT_USER_ID)
        cat.assert_shows(self, h.screen_text(), "wizard.photos.title")
        self.assertEqual(_draft_photos(h, draft.id), ["lib1"])

    @scenario_test
    async def test_selection_survives_restart(self, h: Harness) -> None:
        """Пункт 14: выбор хранится в черновике и переживает рестарт."""
        from tests.test_stage27b_announcement_ui import _seed_place

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        _seed_library(h, place_id, 2, actor_tg=DEFAULT_USER_ID)
        draft = await _to_photo_step(h, place_id=place_id)
        await h.callback(_library_callback(h), user_id=DEFAULT_USER_ID)
        await h.callback(_toggle_callbacks(h)[1], user_id=DEFAULT_USER_ID)
        self.assertEqual(_draft_photos(h, draft.id), ["lib2"])

        await h.restart()

        self.assertEqual(_draft_photos(h, draft.id), ["lib2"])
        self.assertEqual(
            [view.file_id for view in _views(h, place_id)], ["lib1", "lib2"]
        )


# --- права и устойчивость ----------------------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlacePhotoAccessTests(unittest.TestCase):
    """Пункты 15–17: права, подделанный callback, сбой Telegram."""

    @scenario_test
    async def test_stranger_cannot_open_or_upload(self, h: Harness) -> None:
        """Пункт 15: посторонний не открывает фототеку и не грузит снимки."""
        place_id = _make_place(h, "Дом культуры")
        from user_store import get_or_create_user

        with h.session_factory() as session:
            get_or_create_user(session, STRANGER_TG, username="stranger")
            session.commit()
        await h.text("/admin", user_id=STRANGER_TG)
        await h.callback(f"adm_pfotos:{place_id}", user_id=STRANGER_TG)

        self.assertIn("доступен только", h.screen_text())
        self.assertEqual(_library(h, place_id), [])

        await h.photo("PHOTO_X", user_id=STRANGER_TG)
        self.assertEqual(_library(h, place_id), [])

    @scenario_test
    async def test_forged_callback_with_foreign_place_is_rejected(
        self, h: Harness
    ) -> None:
        """Пункт 16: подменённый `place_id` и чужой `photo_id` отклоняются."""
        mine = _make_place(h, "Дом культуры")
        other = _make_place(h, "Клуб", admin_tg=OTHER_ADMIN_TG)
        _seed_library(h, mine, 1, prefix="mine")
        foreign_photo = _seed_library(
            h, other, 1, actor_tg=OTHER_ADMIN_TG, prefix="other"
        )
        await _open_library(h, mine)
        foreign_id = _library(h, other)[0].id

        await h.callback(f"adm_pfotos:{other}", user_id=ADMIN_TG)
        cat.assert_shows(
            self, _last_alert(h), "admin.module.dostup_administrativnomu_razdelu"
        )

        await h.callback(f"adm_pfoto_del:{foreign_id}", user_id=ADMIN_TG)
        self.assertEqual([row.file_id for row in _library(h, other)], foreign_photo)
        await h.callback(f"adm_pfoto_del_yes:{foreign_id}", user_id=ADMIN_TG)
        self.assertEqual(
            [row.file_id for row in _library(h, other)],
            foreign_photo,
            "чужой снимок удалён подделанным callback",
        )
        self.assertEqual(len(_library(h, mine)), 1)

    @scenario_test
    async def test_telegram_failure_keeps_photo_and_user(self, h: Harness) -> None:
        """Пункт 17: сбой редактирования экрана не теряет снимок."""
        place_id = _make_place(h, "Дом культуры")
        await _open_library(h, place_id)
        h.bot.errors_by_type["edit_message"] = BadRequest(
            "Bad Request: message to edit not found"
        )

        await h.photo("PHOTO_FAIL", user_id=ADMIN_TG)

        self.assertEqual([row.file_id for row in _library(h, place_id)], ["PHOTO_FAIL"])
        # Экран пересоздан: пользователь видит библиотеку, а не пустоту.
        self.assertIn("Снимков в фототеке: 1", h.screen_text())


# --- навигация ---------------------------------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlacePhotoBackTests(unittest.TestCase):
    """Пункт 18: «◀️ Назад» с каждого нового экрана — на своего родителя."""

    @scenario_test
    async def test_back_chain_from_every_screen(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры")
        _seed_library(h, place_id, 1)
        await _open_library(h, place_id)
        photo_id = _library(h, place_id)[0].id

        # Объяснение → список.
        await h.callback("adm_pfoto_upload", user_id=ADMIN_TG)
        cat.assert_shows(self, h.screen_text(), "admin.place_photos.obyasnenie_html")
        await h.callback("adm_pfoto_back", user_id=ADMIN_TG)
        cat.assert_shows(self, h.screen_text(), "admin.place_photos.spisok_html")

        # Подтверждение удаления → список (без удаления).
        await h.callback(f"adm_pfoto_del:{photo_id}", user_id=ADMIN_TG)
        await h.callback("adm_pfoto_back", user_id=ADMIN_TG)
        cat.assert_shows(self, h.screen_text(), "admin.place_photos.spisok_html")
        self.assertEqual(len(_library(h, place_id)), 1)

        # Список → карточка места (и это рабочая карточка, а не «устарело»).
        await h.callback("adm_pfoto_back", user_id=ADMIN_TG)
        cat.assert_not_stale(self, _last_alert(h))
        cat.assert_shows(self, h.screen_text(), "admin.place.mesto")
        self.assertIn(f"adm_pfotos:{place_id}", _callbacks(h))

    @scenario_test
    async def test_back_from_wizard_library_returns_to_photo_step(
        self, h: Harness
    ) -> None:
        from tests.test_stage27b_announcement_ui import _seed_place

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        _seed_library(h, place_id, 1, actor_tg=DEFAULT_USER_ID)
        await _to_photo_step(h, place_id=place_id)

        await h.callback(_library_callback(h), user_id=DEFAULT_USER_ID)
        await h.callback(_back_from_library(h), user_id=DEFAULT_USER_ID)

        cat.assert_shows(self, h.screen_text(), "wizard.photos.title")
        self.assertTrue(
            [
                data
                for data in _callbacks(h)
                if (data or "").startswith("aw_photo_none")
            ],
            "потерян шаг фото после возврата из раздела",
        )


# --- вспомогательные чтения состояния ---------------------------------------


def _draft_state(h: Harness, draft_id: int):
    """Доменное состояние черновика (ORM-строка хранит `photos` как JSON)."""
    from announcement_store import get_announcement_draft

    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _draft_photos(h: Harness, draft_id: int) -> list[str]:
    return list(_draft_state(h, draft_id).photos)


def _views(h: Harness, place_id: int):
    with h.session_factory() as session:
        return list_place_photos(session, place_id)


def _back_from_library(h: Harness) -> str:
    """«◀️ Назад» из раздела: родитель — шаг фото, а не список вложений."""
    for data in _callbacks(h):
        if (data or "").startswith("aw_photoback:"):
            return str(data)
    raise AssertionError(f"Нет возврата из раздела: {h.screen_buttons()}")
