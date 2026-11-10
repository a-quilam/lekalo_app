# tests/test_stage261_e_dates.py
"""
Poster 2.6.1, этап E — единая контролируемая дата в цепочке рендера (§9).

Проверяется ТРЕБОВАНИЯ ВЛАДЕЛЬЦА (`docs/agent_team/task_261_20261110.md`,
§9.1–9.4), а не устройство реализации: даты сохраняются и рендерятся через
штатный путь анонса (`save_announcement_value` →
`build_announcement_preview` → `render_template` → `validate_brick_value`)
на временной SQLite в /tmp. Рабочая `post_bot.db`, `.env` и Telegram не
используются.

Что закрепляется.

§9.2 — одно значение `today` проходит всю цепочку. Пока компилятор брал
`app_today()` сам, явный `today` доходил только до вопросов и полей, а
проверка правил даты при рендере шла по фактическому «сегодня». Поэтому
сохранённое значение, валидное на момент сохранения, на следующей день
могло стать «датой в прошлом» и рендер падал (`TemplateCompileError`) при
каждой отправке/перерендере. Теперь `today` операции доходит и до
`_brick_text`/`_brick_value_empty`.

§9.3 — часовой пояс площадки моделью данных не определяется (`Place` —
произвольная строка города), поэтому используется единый `APP_TZ`; в
тестах время замораживается штатным `frozen_today()`.

Важно, что именно проверяется на границе суток. «Переход через полночь» —
это смена фактических часов при ОДНОЙ дате операции: анонс начат вчера,
его `today` уже зафиксирован, а рендер идёт после полуночи. Тест передаёт
этот `today` явно (как рабочие обработчики), а часы заморожены на
следующем дне — до исправления компилятор брал из часов новый день и
отклонял сохранённую дату. Проверка «дата в прошлом» при ВВОДЕ не
отключается: она по-прежнему отклоняет вчерашнюю дату (отдельный тест).

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage261_e_dates -v
"""

from __future__ import annotations

import os
import shutil
import unittest
from datetime import date

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session, sessionmaker

from announcement_store import (
    AnnouncementValueError,
    build_announcement_preview,
    choose_announcement_template,
    confirm_announcement_place,
    get_announcement_draft,
    save_announcement_value,
    start_announcement_wizard,
)
from brick_store import get_system_brick
from migrations import run_migrations
from models import PlaceAdmin, utcnow
from place_store import create_place
from template_compiler import TemplateCompileError
from tests.integration_harness import frozen_today
from tests.stage19a_helpers import make_engine, temp_db_path
from user_store import get_or_create_user

OWNER_TG = 26_011_101
PLACE_NAME = "Дом культуры"
PLACE_CITY = "Москва"
PLACE_ADDRESS = "ул. Ленина, 1"
PLACE_URL = "https://example.com/place"

NAME_CODE = "название"
DATE_CODE = "дата"


class _DatesCase(unittest.TestCase):
    """Временная база, площадка с админом и анонс по стандартному шаблону."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-261e-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)
        self.user = get_or_create_user(self.session, OWNER_TG, username="owner")
        self.place_id = self._make_place()
        self.draft_id, self.revision = self._start_draft()

    # --- подготовка ------------------------------------------------------

    def _make_place(self) -> int:
        place = create_place(
            self.session,
            PLACE_NAME,
            city=PLACE_CITY,
            created_by_user_id=self.user.id,
            address=PLACE_ADDRESS,
            url=PLACE_URL,
        )
        self.session.add(
            PlaceAdmin(
                place_id=place.id,
                user_id=self.user.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
            )
        )
        self.session.commit()
        return place.id

    def _start_draft(self) -> tuple[int, int]:
        draft = start_announcement_wizard(
            self.session, actor_telegram_user_id=OWNER_TG, place_id=self.place_id
        )
        confirmed = confirm_announcement_place(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )
        template_id: int = int(
            self.session.execute(
                sql_text(
                    "SELECT id FROM templates "
                    "WHERE place_id = :place AND kind = 'standard'"
                ),
                {"place": self.place_id},
            ).scalar_one()
        )
        chosen = choose_announcement_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=confirmed.draft_id,
            template_id=template_id,
            expected_revision=confirmed.revision,
        )
        return chosen.draft_id, chosen.revision

    # --- штатные операции ------------------------------------------------

    def _save(self, code: str, value: str, *, today: date) -> None:
        brick = get_system_brick(self.session, code)
        self.assertIsNotNone(brick, f"системный кирпичик «{code}» не найден")
        assert brick is not None
        saved = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=self.draft_id,
            brick_id=brick.id,
            value=value,
            expected_revision=self.revision,
            today=today,
        )
        self.revision = saved.revision

    def _preview(self, *, today: date | None) -> str:
        return build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=self.draft_id,
            today=today,
        ).html

    def _stored_date(self) -> str | None:
        details = get_announcement_draft(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=self.draft_id,
        )
        brick = get_system_brick(self.session, DATE_CODE)
        assert brick is not None
        value = details.value_for(brick.id)
        if value is None or value.payload is None:
            return None
        stored = value.payload.get("date")
        return stored if isinstance(stored, str) else None


class SaveRulesTests(_DatesCase):
    """Проверки дат при сохранении не ослаблены (§9.2, §9.4)."""

    def test_today_is_valid(self) -> None:
        today = date(2026, 11, 10)
        with frozen_today(today.year, today.month, today.day):
            self._save(DATE_CODE, today.strftime("%d.%m.%Y"), today=today)
            self.assertEqual(self._stored_date(), "10.11.2026")
            self.assertIn("10.11", self._preview(today=today))

    def test_tomorrow_is_valid(self) -> None:
        today = date(2026, 11, 10)
        with frozen_today(today.year, today.month, today.day):
            self._save(DATE_CODE, "11.11.2026", today=today)
            self.assertEqual(self._stored_date(), "11.11.2026")
            self.assertIn("11.11", self._preview(today=today))

    def test_past_date_is_rejected_on_save(self) -> None:
        today = date(2026, 11, 10)
        with frozen_today(today.year, today.month, today.day):
            with self.assertRaises(AnnouncementValueError) as caught:
                self._save(DATE_CODE, "09.11.2026", today=today)
        self.assertIn("не раньше 10.11.2026", str(caught.exception))
        self.assertIsNone(self._stored_date(), "отклонённое значение не хранится")


class MidOperationDateTests(_DatesCase):
    """
    Одна дата операции на сохранение и рендер (§9.2, §9.4).

    Часы на рендере заморожены на СЛЕДУЮЩЕМ дне: до исправления компилятор
    брал их как `app_today()` и отклонял сохранённое значение, хотя операция
    несёт свой `today`.
    """

    def test_render_survives_midnight_with_same_today(self) -> None:
        op_today = date(2026, 11, 10)
        with frozen_today(op_today.year, op_today.month, op_today.day):
            self._save(DATE_CODE, "10.11.2026", today=op_today)
            expected = self._preview(today=op_today)
            self.assertIn("10.11", expected)
        # 00:05 следующего дня: рабочие часы уже новый день, дата операции —
        # прежняя. Рендер не падает и совпадает с сохранённым текстом.
        with frozen_today(2026, 11, 11):
            self.assertEqual(self._preview(today=op_today), expected)

    def test_render_of_stored_past_date_without_today_is_rejected(self) -> None:
        """Проверка «в прошлом» не отключена: без `today` — актуальный день."""
        op_today = date(2026, 11, 10)
        with frozen_today(op_today.year, op_today.month, op_today.day):
            self._save(DATE_CODE, "10.11.2026", today=op_today)
        with frozen_today(2026, 11, 11):
            with self.assertRaises(TemplateCompileError):
                self._preview(today=None)

    def test_explicit_today_matches_default_when_day_is_same(self) -> None:
        """Явный `today` и `None` (→ `app_today()`) дают один текст."""
        op_today = date(2026, 11, 10)
        with frozen_today(op_today.year, op_today.month, op_today.day):
            self._save(DATE_CODE, "15.11.2026", today=op_today)
            explicit = self._preview(today=op_today)
            repeated = self._preview(today=op_today)
            implicit = self._preview(today=None)
        self.assertEqual(explicit, repeated, "рендер с явным today нестабилен")
        self.assertEqual(explicit, implicit)
        self.assertIn("15.11", explicit)


class CalendarBoundaryTests(_DatesCase):
    """Границы месяца и года при сохранении и рендере (§9.4)."""

    def test_last_day_of_month_to_first_day_of_next(self) -> None:
        save_day = date(2027, 1, 31)
        next_day = date(2027, 2, 1)
        with frozen_today(save_day.year, save_day.month, save_day.day):
            self._save(DATE_CODE, "31.01.2027", today=save_day)
            expected = self._preview(today=save_day)
            self.assertIn("31.01", expected)
        with frozen_today(next_day.year, next_day.month, next_day.day):
            self.assertEqual(self._preview(today=save_day), expected)

    def test_new_year_eve_to_new_year(self) -> None:
        save_day = date(2026, 12, 31)
        next_day = date(2027, 1, 1)
        with frozen_today(save_day.year, save_day.month, save_day.day):
            self._save(DATE_CODE, "31.12.2026", today=save_day)
            expected = self._preview(today=save_day)
            self.assertIn("31.12", expected)
        # Год в подписи системной даты не выводится (§17): проверяется, что
        # валидация горизонта не отклоняет сохранённое значение после смены
        # года и текст совпадает.
        with frozen_today(next_day.year, next_day.month, next_day.day):
            self.assertEqual(self._preview(today=save_day), expected)


class FlexibleDateTests(_DatesCase):
    """Разбор дат без года не менялся; год подставляется от `today`."""

    def test_date_without_year_uses_operation_today(self) -> None:
        today = date(2026, 11, 5)
        with frozen_today(today.year, today.month, today.day):
            self._save(DATE_CODE, "09.11", today=today)
            self.assertEqual(self._stored_date(), "09.11.2026")
            self.assertIn("09.11", self._preview(today=today))

    def test_spoken_date_without_year(self) -> None:
        today = date(2026, 11, 5)
        with frozen_today(today.year, today.month, today.day):
            self._save(DATE_CODE, "9 ноября", today=today)
            self.assertEqual(self._stored_date(), "09.11.2026")
            self.assertIn("09.11", self._preview(today=today))


class EditAndPersistenceTests(_DatesCase):
    """Правка даты и повторный рендер (§9.4)."""

    def test_date_change_is_reflected_in_new_render(self) -> None:
        today = date(2026, 11, 10)
        with frozen_today(today.year, today.month, today.day):
            self._save(DATE_CODE, "15.11.2026", today=today)
            first = self._preview(today=today)
            self.assertIn("15.11", first)
            self._save(DATE_CODE, "20.11.2026", today=today)
            second = self._preview(today=today)
        self.assertIn("20.11", second)
        self.assertNotIn("15.11", second)
        self.assertEqual(self._stored_date(), "20.11.2026")

    def test_render_matches_stored_value(self) -> None:
        today = date(2026, 11, 10)
        with frozen_today(today.year, today.month, today.day):
            self._save(NAME_CODE, "Летний концерт", today=today)
            self._save(DATE_CODE, "17.11.2026", today=today)
            html = self._preview(today=today)
        stored = self._stored_date()
        assert stored is not None
        day_month = ".".join(stored.split(".")[:2])
        self.assertIn(day_month, html, "рендер обязан показать сохранённую дату")

    def test_two_calls_with_explicit_today_are_identical(self) -> None:
        """Два рендера с одним явным `today` дают одинаковый текст."""
        today = date(2026, 11, 10)
        with frozen_today(today.year, today.month, today.day):
            self._save(DATE_CODE, "18.11.2026", today=today)
            first = self._preview(today=today)
            second = self._preview(today=today)
        self.assertEqual(first, second)
        self.assertIn("18.11", first)

    def test_restart_render_is_stable(self) -> None:
        """Новая сессия (как после рестарта) — тот же текст при том же today."""
        today = date(2026, 11, 10)
        with frozen_today(today.year, today.month, today.day):
            self._save(DATE_CODE, "25.11.2026", today=today)
            first = self._preview(today=today)
        self.session.close()
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.addCleanup(self.session.close)
        with frozen_today(2026, 11, 11):
            second = self._preview(today=today)
        self.assertEqual(first, second)
        self.assertIn("25.11", second)


if __name__ == "__main__":  # pragma: no cover — запуск модуля напрямую
    unittest.main()
