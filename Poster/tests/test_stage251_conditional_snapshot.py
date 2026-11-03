# tests/test_stage251_conditional_snapshot.py
"""
Poster 2.5.1 — независимость снимка условных частей от правки шаблона.

Tester, воспроизводящий тест (HIGH). Продукт не меняется.

КОНТРАКТ 2.5.1 (требование владельца): правила условных частей входят в
НЕЗАВИСИМЫЙ снимок анонса и замораживаются в момент создания анонса.

Сценарий целиком (НЕ через прямой вызов `apply_conditional_parts`):

1. Автор создаёт шаблон с условной частью (скрыть ` – [цена]`, если цена
   пуста) и начинает по нему анонс.
2. Правила применяются к ФАКТИЧЕСКОМУ тексту анонса: рендер черновика
   скрывает фрагмент при пустой цене и оставляет при заполненной.
3. Затем автор правит текст шаблона — новая версия; `template_store`
   сам вызывает `refresh_conditional_parts`.
4. Старый черновик сохраняет ИСХОДНЫЙ текст и ИСХОДНЫЕ правила: скрытый
   фрагмент остаётся скрытым, даже если правило в шаблоне перенесли в
   новую версию или пометили `stale`.
5. После рестарта (`Harness.restart` / новый движок и сессия на том же
   файле БД) черновик рендерится идентично.
6. Новый анонс использует новую актуальную версию текста и правил.
7. Форматирование, ссылки, кирпичики и доп. поля снимка сохраняются.

Почему сценарий именно такой. `template_compiler` исторически скрывает
строку, где ВСЕ кирпичики пусты (`_render_text_lines`). Если поставить
правило на целую строку `💰 Цена: [цена]`, дефект незаметен: старый
черновик спрячет строку и без правила. Поэтому фрагмент стоит в строке с
видимым литералом (`**[main] – [cost]**`): без правила рендер даёт
`<b>Концерт – </b>`, с правилом — `<b>Концерт</b>`. Разница видна только
если правило реально применилось.

Ожидаемое состояние: после реализации 2.5.1 (миграция 28 + заморозка правил
в снимке) все пункты проходят — тест остаётся регрессионным контрактом.
Историческая справка: до заморозки пункты 4, 5 и 7 падали, потому что
`announcement_store._conditional_snapshot_texts` брал правила через
`active_rules_for_version(session, version_id)` по версии ЧЕРНОВИКА на
каждом рендере, а `conditional_parts.refresh_conditional_parts` при правке
шаблона перезаписывал `version_id` той же строки правила (или ставил
`stale`) — правило «уезжало» из версии черновика.

Реальная `post_bot.db`, `.env` и сеть Telegram не используются: временный
SQLite-файл (доменная часть) и настоящий `Application.process_update`
поверх временной SQLite (`Harness`, рестарт-часть).

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \\
        -m unittest tests.test_stage251_conditional_snapshot -v
"""

from __future__ import annotations

import unittest

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from announcement_store import (
    ORIGIN_PERSONAL,
    add_announcement_extra,
    build_announcement_preview,
    save_announcement_value,
    start_announcement_wizard,
)
from conditional_parts import (
    STATUS_STALE,
    create_conditional_part,
    list_conditional_parts,
)
from models import Template
from template_store import create_personal_template, update_template_content
from tests.stage19a_helpers import make_engine
from tests.stage23_helpers import OWNER_TG, TemplateDatabaseTestCase

try:
    from brick_store import create_personal_brick
    from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# Фрагмент стоит в строке с видимым литералом: без правила строка НЕ
# скрывается компилятором, поэтому тест проверяет именно условную часть.
CONTENT_V1 = "**[main] – [cost]**\nКонец"
HIDDEN_FRAGMENT = " – [cost]"

# Ожидаемые рендеры черновика v1.
EMPTY_COST_V1 = "<b>Концерт</b>\nКонец"
FILLED_COST_V1 = "<b>Концерт – 500 ₽</b>\nКонец"

CONTENT_V2 = "**[main] – [cost]**\nНовый текст\nНовый конец"


class ConditionalSnapshotTestBase(TemplateDatabaseTestCase):
    """
    Временная SQLite и шаблон-заготовка БЕЗ условных правил.

    Правило добавляет подкласс — это принципиально: сценарий «правило
    появилось ПОСЛЕ создания анонса» обязан стартовать с шаблона без
    правил, иначе его предусловие недостижимо.
    """

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="snapshot-owner")
        self.main_brick = self.make_personal_brick(self.owner, "main")
        self.cost_brick = self.make_personal_brick(self.owner, "cost")
        self.template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="С ценой",
            content=CONTENT_V1,
        )

    # --- помощники ---------------------------------------------------------

    def _add_rule(self, *, fragment: str = HIDDEN_FRAGMENT, section: str = "content"):
        """Добавляет правило к ТЕКУЩЕЙ версии шаблона (версия не меняется)."""
        return create_conditional_part(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
            expected_revision=self._revision(),
            section=section,
            brick_id=self.cost_brick,
            fragment=fragment,
        )

    def _revision(self) -> int:
        return int(
            self.session.execute(
                select(Template.revision).where(
                    Template.id == self.template.template_id
                )
            ).scalar_one()
        )

    def _start_draft(self, template_id: int | None = None):
        """Пункт 1: автор начинает анонс по шаблону (снимок фиксируется)."""
        return start_announcement_wizard(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            origin=ORIGIN_PERSONAL,
            template_id=(
                self.template.template_id if template_id is None else template_id
            ),
        )

    def _save(self, draft, brick_id: int, value: object):
        return save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=brick_id,
            value=value,
            expected_revision=draft.revision,
        )

    def _preview(self, draft) -> str:
        """Пункт 2: рендер черновика — фактический текст анонса."""
        return build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        ).html

    def _edit_template(
        self,
        content: str,
        *,
        template_id: int | None = None,
        revision: int | None = None,
    ) -> None:
        """Пункт 3: правка шаблона → новая версия + refresh правил."""
        checked_id = self.template.template_id if template_id is None else template_id
        result = update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=checked_id,
            expected_revision=(self._revision() if revision is None else revision),
            content=content,
        )
        self.assertTrue(result.ok, result.message)
        self.session.expire_all()

    def _rules_state(self, template_id: int | None = None) -> str:
        """Диагностика в assert: где сейчас живут правила шаблона."""
        parts = list_conditional_parts(
            self.session,
            OWNER_TG,
            self.template.template_id if template_id is None else template_id,
            include_stale=True,
        )
        return "; ".join(
            f"id={part.id} version={part.version} status={part.status} "
            f"fragment={part.fragment!r}"
            for part in parts
        )

    def _restart(self) -> None:
        """Пункт 5: новый движок и новая сессия на том же файле БД."""
        self.session.close()
        self.engine.dispose()
        self.engine = make_engine(self.path)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.Session()


class ConditionalSnapshotContractTests(ConditionalSnapshotTestBase):
    """
    Пункты 1–7 контракта 2.5.1: правило создано ДО начала анонса.

    Так снимок обязан «увидеть» правило — это и есть предмет заморозки.
    """

    def setUp(self) -> None:
        super().setUp()
        created = self._add_rule()
        self.assertEqual(created.status, "created", created.message)

    # --- пункты 1–3: снимок и фактический рендер ----------------------------

    def test_draft_render_applies_rules_to_actual_text(self) -> None:
        """Пункты 1–2: правило скрывает фрагмент в реальном рендере черновика."""
        draft = self._start_draft()
        self.assertIsNotNone(draft.template_snapshot, "п.1: снимок зафиксирован")
        self.assertEqual(draft.template_version, 1)

        draft = self._save(draft, self.main_brick, "Концерт")
        self.assertEqual(
            self._preview(draft),
            EMPTY_COST_V1,
            "п.2: пустая цена скрывает авторский фрагмент ` – [cost]` "
            "(без правила было бы `<b>Концерт – </b>`)",
        )

        draft = self._save(draft, self.cost_brick, "500 ₽")
        self.assertEqual(
            self._preview(draft),
            FILLED_COST_V1,
            "п.2: заполненная цена сохраняет фрагмент и значение",
        )

    # --- пункт 4: старый черновик после правки шаблона ----------------------

    def test_old_draft_keeps_rules_when_fragment_moves_to_new_version(self) -> None:
        """
        Пункт 4 (ОСНОВНОЙ ДЕФЕКТ): правка текста переносит правило в v2 —
        старый черновик обязан сохранить исходный текст и правило снимка.
        """
        draft = self._start_draft()
        draft = self._save(draft, self.main_brick, "Концерт")
        before = self._preview(draft)
        self.assertEqual(before, EMPTY_COST_V1)

        self._edit_template(CONTENT_V2)
        # Подготовка: правило однозначно перенесено в новую версию шаблона.
        self.assertEqual(self._revision(), 2)
        self.assertIn(
            "version=2",
            self._rules_state(),
            "подготовка теста: правило уехало в новую версию",
        )

        self.session.expire_all()
        after = self._preview(draft)
        self.assertEqual(
            after,
            before,
            "КОНТРАКТ 2.5.1, п.4: старый черновик обязан рендериться как при "
            "создании — его текст и правила заморожены на момент создания. "
            f"Состояние правил: {self._rules_state()}",
        )

    def test_old_draft_keeps_rules_when_edit_marks_them_stale(self) -> None:
        """
        Пункт 4 (вторая сторона): правка убрала фрагмент из шаблона →
        правило стало stale, но снимок старого черновика не меняется.
        """
        draft = self._start_draft()
        draft = self._save(draft, self.main_brick, "Концерт")
        before = self._preview(draft)
        self.assertEqual(before, EMPTY_COST_V1)

        self._edit_template("**[main]**\nСовсем другой текст")
        self.assertIn(
            STATUS_STALE,
            self._rules_state(),
            "подготовка теста: правило помечено stale, но не удалено",
        )

        self.session.expire_all()
        self.assertEqual(
            self._preview(draft),
            before,
            "КОНТРАКТ 2.5.1, п.4: stale в живом шаблоне не отменяет правило "
            "снимка старого черновика",
        )

    # --- пункт 5: рестарт --------------------------------------------------

    def test_old_draft_renders_identically_after_restart(self) -> None:
        """Пункт 5: новый процесс бота (движок + сессия) не меняет рендер."""
        draft = self._start_draft()
        draft = self._save(draft, self.main_brick, "Концерт")
        draft = self._save(draft, self.cost_brick, "500 ₽")
        before = self._preview(draft)
        self.assertEqual(before, FILLED_COST_V1)

        self._restart()
        restored = build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        ).html
        self.assertEqual(restored, before, "п.5: рендер после рестарта идентичен")

        # Правка шаблона ПОСЛЕ рестарта не должна менять старый черновик.
        self._edit_template(CONTENT_V2)
        self._restart()
        after_edit = build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        ).html
        self.assertEqual(
            after_edit,
            before,
            "КОНТРАКТ 2.5.1, п.4+5: после рестарта и правки шаблона старый "
            f"черновик рендерится по своему снимку. {self._rules_state()}",
        )

    # --- пункт 6: новый анонс ----------------------------------------------

    def test_new_draft_uses_current_rules_and_text(self) -> None:
        """Пункт 6: анонс, созданный после правки, берёт новую версию."""
        self._edit_template(CONTENT_V2)
        self.assertEqual(self._revision(), 2)

        draft = self._start_draft()
        self.assertEqual(draft.template_version, 2, "новый анонс — новая версия")
        draft = self._save(draft, self.main_brick, "Концерт")
        self.assertEqual(
            self._preview(draft),
            "<b>Концерт</b>\nНовый текст\nНовый конец",
            "п.6: новый анонс использует актуальный текст и актуальное правило",
        )
        draft = self._save(draft, self.cost_brick, "500 ₽")
        self.assertEqual(
            self._preview(draft),
            "<b>Концерт – 500 ₽</b>\nНовый текст\nНовый конец",
        )

    # --- пункт 7: форматирование, ссылки, доп. поля ------------------------

    def test_old_draft_keeps_formatting_links_and_extras(self) -> None:
        """Пункт 7: форматирование, ссылка и доп. поле снимка не теряются."""
        venue_brick = self.make_personal_brick(self.owner, "venue")
        content = "**[main] – [cost]**\nБилеты(https://example.com/afisha)"
        extended = content + "\nМесто: [venue]"
        template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="С форматом",
            content=content,
            extended_content=extended,
        )
        created = create_conditional_part(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=template.template_id,
            expected_revision=template.revision,
            section="extended_content",
            brick_id=self.cost_brick,
            fragment=HIDDEN_FRAGMENT,
        )
        self.assertEqual(created.status, "created", created.message)

        draft = self._start_draft(template.template_id)
        draft = self._save(draft, self.main_brick, "Концерт")
        draft = add_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=venue_brick,
            value="Дом культуры",
        )
        before = self._preview(draft)
        # Содержимое снимка до правки шаблона: правило расширенной секции
        # применилось, форматирование, ссылка и доп. поле на месте.
        self.assertIn("<b>Концерт</b>", before, "п.7: жирный фрагмент сохранён")
        self.assertNotIn("[cost]", before, "п.7: пустая цена скрыта правилом")
        self.assertIn(
            '<a href="https://example.com/afisha">',
            before,
            "п.7: ссылка снимка не потеряна",
        )
        self.assertIn("Место: Дом культуры", before, "п.7: доп. поле на месте")

        self._edit_template(
            content + "\nНовая строка",
            template_id=template.template_id,
            revision=template.revision,
        )
        # extended_content не передан — остаётся прежним; правило переносится.
        self.session.expire_all()

        self.assertEqual(
            self._preview(draft),
            before,
            "КОНТРАКТ 2.5.1, п.7: форматирование, ссылка и доп. поле снимка "
            "сохраняются вместе с его условным правилом",
        )


class ConditionalSnapshotLateRuleTests(ConditionalSnapshotTestBase):
    """
    Пункт 4 (третья сторона): правило добавлено ПОСЛЕ создания анонса.

    Отдельный класс с шаблоном БЕЗ правил в `setUp`: только так «до» честно
    показывает непустой фрагмент. В `ConditionalSnapshotContractTests`
    правило создаётся в `setUp` до старта черновика, и снимок обязан его
    содержать (п.2) — совмещать оба предусловия в одном классе нельзя.
    """

    def test_old_draft_is_not_affected_by_rule_added_to_same_version(self) -> None:
        """Новое правило той же версии не применяется к уже созданному анонсу."""
        draft = self._start_draft()
        draft = self._save(draft, self.main_brick, "Концерт")
        after_creation = self._preview(draft)
        self.assertEqual(
            after_creation,
            "<b>Концерт – </b>\nКонец",
            "предусловие: на момент создания анонса правил в шаблоне не было, "
            "поэтому фрагмент ` – [cost]` не скрыт",
        )

        added = self._add_rule()
        self.assertEqual(added.status, "created", added.message)
        self.assertEqual(self._revision(), 1, "правило не создаёт новую версию")

        self.session.expire_all()
        self.assertEqual(
            self._preview(draft),
            after_creation,
            "КОНТРАКТ 2.5.1, п.4: правила снимка заморожены на момент "
            "создания анонса — новое правило того же шаблона не применяется "
            "к уже созданному черновику",
        )

    def test_new_draft_created_after_rule_uses_it(self) -> None:
        """Контроль к предыдущему: новый анонс то же правило применяет."""
        self.assertEqual(self._add_rule().status, "created")

        draft = self._start_draft()
        draft = self._save(draft, self.main_brick, "Концерт")
        self.assertEqual(
            self._preview(draft),
            "<b>Концерт</b>\nКонец",
            "новый снимок забирает правило, существующее на момент создания",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ConditionalSnapshotRestartTests(unittest.TestCase):
    """Пункты 1–5 через настоящий `Application` и `Harness.restart`."""

    def _seed(self, h) -> tuple[int, int]:
        with h.session_factory() as session:
            get_or_create_user(session, h.user_id, username="snapshot-ui")
            main = create_personal_brick(
                session,
                actor_telegram_user_id=h.user_id,
                code="main",
                title="Основное",
                value_type="short_text",
            )
            cost = create_personal_brick(
                session,
                actor_telegram_user_id=h.user_id,
                code="cost",
                title="Цена",
                value_type="short_text",
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=h.user_id,
                name="С ценой",
                content=CONTENT_V1,
            )
            created = create_conditional_part(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template.template_id,
                expected_revision=template.revision,
                section="content",
                brick_id=cost.id,
                fragment=HIDDEN_FRAGMENT,
            )
            self.assertEqual(created.status, "created", created.message)
            draft = start_announcement_wizard(
                session,
                actor_telegram_user_id=h.user_id,
                origin=ORIGIN_PERSONAL,
                template_id=template.template_id,
            )
            draft = save_announcement_value(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=draft.draft_id,
                brick_id=main.id,
                value="Концерт",
                expected_revision=draft.revision,
            )
            return draft.draft_id, template.template_id

    @scenario_test
    async def test_draft_survives_restart_and_template_edit(self, h) -> None:
        draft_id, template_id = self._seed(h)
        with h.session_factory() as session:
            before = build_announcement_preview(
                session, actor_telegram_user_id=h.user_id, draft_id=draft_id
            ).html
        self.assertEqual(before, EMPTY_COST_V1)

        await h.restart()
        self.assertEqual(h.user_data(), {}, "состояние диалога потеряно, как в жизни")
        with h.session_factory() as session:
            restored = build_announcement_preview(
                session, actor_telegram_user_id=h.user_id, draft_id=draft_id
            ).html
        self.assertEqual(restored, before, "п.5: после рестарта рендер тот же")

        with h.session_factory() as session:
            current = session.scalar(
                select(Template.revision).where(Template.id == template_id)
            )
            result = update_template_content(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=int(current),
                content=CONTENT_V2,
            )
            self.assertTrue(result.ok, result.message)

        await h.restart()
        with h.session_factory() as session:
            after = build_announcement_preview(
                session, actor_telegram_user_id=h.user_id, draft_id=draft_id
            ).html
        self.assertEqual(
            after,
            before,
            "КОНТРАКТ 2.5.1, п.4+5 через реальный рестарт: старый анонс "
            "рендерится по своему снимку, а не по переехавшему правилу",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
