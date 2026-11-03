# tests/test_stage25_conditional_parts.py
"""
Poster 2.5, этап D1 — условные части шаблонов (spec §38–45, миграция 27).

Контракт:

- правило — отдельная строка БД, без синтаксиса условий в тексте шаблона,
  привязанная к конкретной НЕИЗМЕНЯЕМОЙ версии и одной секции
  (`content`/`extended_content`), с одним управляющим кирпичиком;
- `fragment` — точный фрагмент ИСХОДНОГО текста; `occurrence` (1-based)
  различает одинаковые вхождения (§42-Е);
- фрагмент непустой, включает `[код]` кирпичика, существует, не
  пересекается с активными правилами секции; кирпичик привязан к версии;
- правка правил НЕ создаёт новую версию и не меняет `revision`; неверный
  `expected_revision` → `stale`;
- при изменении текста правило переносится только при однозначном
  совпадении, иначе `stale` и не удаляется молча (§44);
- рендер вырезает фрагменты из ИСХОДНОГО текста до разбора: целая строка
  исчезает вместе с одним лишним переводом строки, пунктуация вне
  авторского фрагмента не чистится, форматирование и ссылки сохраняются;
- копии шаблонов и снимки правила не переносят (§44).

Тесты работают только с временными файлами SQLite; реальная `post_bot.db`,
`.env` и Telegram не используются.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest

from sqlalchemy import text as sql_text

from announcement_store import (
    build_announcement_preview,
    save_announcement_value,
)
from brick_store import get_system_brick
from conditional_parts import (
    STATUS_ACTIVE,
    STATUS_STALE,
    active_rules_for_version,
    create_conditional_part,
    delete_conditional_part,
    get_conditional_part,
    list_conditional_parts,
    refresh_conditional_parts,
    update_conditional_part,
)
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from template_compiler import (
    ConditionalRule,
    apply_conditional_parts,
)
from template_parser import parse_template
from template_store import (
    create_personal_template,
    create_template_snapshot,
    render_template_snapshot,
    render_template_version,
    update_template_content,
)
from tests.stage19a_helpers import make_engine, temp_db_path
from tests.stage27_helpers import OWNER_TG, AnnouncementDatabaseTestCase

MIGRATION_27_NAME = "template_conditional_parts"


def _journal(path: str) -> list[tuple[int, str]]:
    connection = sqlite3.connect(path)
    try:
        return [
            (int(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        connection.close()


def _sql(path: str, statement: str, params: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        return connection.execute(statement, params).fetchall()
    finally:
        connection.close()


# --- миграция 27 ------------------------------------------------------------


class Migration27Tests(unittest.TestCase):
    """Чистая установка, апгрейд с v26 и ограничения таблицы."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-25-d1-migr-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)

    def migrate(self, migrations=DEFAULT_MIGRATIONS) -> int:
        engine = make_engine(self.path)
        try:
            return run_migrations(engine, migrations)
        finally:
            engine.dispose()

    def test_fresh_install_has_conditional_parts_table(self) -> None:
        self.assertEqual(self.migrate(), LATEST_VERSION)
        self.assertEqual(LATEST_VERSION, 27)
        self.assertEqual(
            _journal(self.path)[-2:],
            [(26, "announcement_instances"), (27, MIGRATION_27_NAME)],
        )
        columns = {
            str(row[1]): (str(row[2]).upper(), int(row[3]), row[4])
            for row in _sql(self.path, "PRAGMA table_info(template_conditional_parts)")
        }
        self.assertEqual(
            columns,
            {
                "id": ("INTEGER", 1, None),
                "template_id": ("INTEGER", 1, None),
                "version_id": ("INTEGER", 1, None),
                "section": ("TEXT", 1, None),
                "brick_id": ("INTEGER", 1, None),
                "fragment": ("TEXT", 1, None),
                "occurrence": ("INTEGER", 1, "1"),
                "status": ("TEXT", 1, "'active'"),
                "created_at": ("DATETIME", 1, None),
                "updated_at": ("DATETIME", 1, None),
            },
        )
        self.assertEqual(
            sorted(
                str(row[0])
                for row in _sql(
                    self.path,
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='template_conditional_parts' "
                    "AND name NOT LIKE 'sqlite_%'",
                )
            ),
            ["ix_template_conditional_parts_version_section"],
        )
        self.assertEqual(_sql(self.path, "PRAGMA foreign_key_check"), [], "FK целы")
        self.assertEqual(
            _sql(self.path, "PRAGMA integrity_check"), [("ok",)], "целостность"
        )

    def test_upgrade_from_v26_adds_only_conditional_parts(self) -> None:
        """Upgrade-путь: база v26 получает ровно одну новую таблицу."""
        self.assertEqual(self.migrate(DEFAULT_MIGRATIONS[:26]), 26)
        before = {
            str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")
        }
        self.assertNotIn("template_conditional_parts", before)
        self.assertEqual(self.migrate(), LATEST_VERSION)
        after = {
            str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")
        }
        self.assertEqual(
            after - before,
            {
                "template_conditional_parts",
                "ix_template_conditional_parts_version_section",
            },
            "миграция только добавочная",
        )
        self.assertEqual(_journal(self.path)[-1], (27, MIGRATION_27_NAME))
        self.assertEqual(self.migrate(), LATEST_VERSION)
        self.assertEqual(
            {str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")},
            after,
            "повторный запуск ничего не меняет",
        )

    def test_table_enforces_section_status_and_occurrence(self) -> None:
        """CHECK-и схемы: секция, статус, вхождение, непустой фрагмент."""
        self.assertEqual(self.migrate(), LATEST_VERSION)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 9927001, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO templates (id, kind, owner_user_id, revision, "
                "created_at, updated_at) VALUES ("
                "1, 'personal', 1, 1, '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO template_versions (id, template_id, version, content, "
                "created_at) VALUES (1, 1, 1, 'Цена: [цена]', "
                "'2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO bricks (id, code, title, type, scope, source, status, "
                "revision, owner_user_id, created_at) VALUES ("
                "900, 'цена', 'Цена', 'short_text', 'personal', 'ask', 'active', "
                "1, 1, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO template_version_bricks "
                "(template_version_id, brick_id, code) VALUES (1, 900, 'цена')"
            )

            def insert(**overrides) -> None:
                values = {
                    "id": "1",
                    "template_id": "1",
                    "version_id": "1",
                    "section": "'content'",
                    "brick_id": "900",
                    "fragment": "'Цена: [цена]'",
                    "occurrence": "1",
                    "status": "'active'",
                    "created_at": "'2026-01-01 00:00:00'",
                    "updated_at": "'2026-01-01 00:00:00'",
                }
                values.update(overrides)
                connection.execute(
                    "INSERT INTO template_conditional_parts ("
                    + ", ".join(values)
                    + ") VALUES ("
                    + ", ".join(values.values())
                    + ")"
                )

            insert()
            for bad in (
                {"id": "2", "section": "'header'"},
                {"id": "3", "status": "'broken'"},
                {"id": "4", "occurrence": "0"},
                {"id": "5", "fragment": "'   '"},
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    insert(**bad)
        finally:
            connection.close()


# --- store-слой -------------------------------------------------------------


class ConditionalPartStoreTests(AnnouncementDatabaseTestCase):
    """Правила на реальном сервисе шаблонов: права, проверки, stale, refresh."""

    TEMPLATE_CONTENT = "Начало\n💰 Цена: [цена]\nКонец"

    def setUp(self) -> None:
        super().setUp()
        self.price_brick = self.make_personal_brick(self.owner, "цена")
        self.template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="С ценой",
            content=self.TEMPLATE_CONTENT,
        )

    def create(self, **overrides):
        params = {
            "actor_telegram_user_id": OWNER_TG,
            "template_id": self.template.template_id,
            "expected_revision": self.template.revision,
            "section": "content",
            "brick_id": self.price_brick,
            "fragment": "💰 Цена: [цена]",
        }
        params.update(overrides)
        return create_conditional_part(self.session, **params)

    def test_create_rules_does_not_create_new_version(self) -> None:
        result = self.create()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.status, "created")
        assert result.part is not None
        self.assertEqual(result.part.fragment, "💰 Цена: [цена]")
        self.assertEqual(result.part.occurrence, 1)
        self.assertEqual(result.part.status, STATUS_ACTIVE)
        self.assertEqual(result.part.code, "цена")
        self.assertEqual(result.part.version, self.template.revision)
        # Версия шаблона и revision не изменились: правило живёт рядом.
        versions = self.session.execute(
            sql_text("SELECT count(*) FROM template_versions WHERE template_id = :id"),
            {"id": self.template.template_id},
        ).scalar()
        self.assertEqual(versions, 1)
        self.session.expire_all()
        parts = list_conditional_parts(
            self.session, OWNER_TG, self.template.template_id
        )
        self.assertEqual(len(parts), 1)

    def test_create_rejects_fragment_without_brick(self) -> None:
        result = self.create(fragment="💰 Цена:")
        self.assertEqual(result.status, "invalid")
        self.assertIn("[цена]", result.message)
        self.assertEqual(
            list_conditional_parts(self.session, OWNER_TG, self.template.template_id),
            [],
        )

    def test_create_rejects_missing_occurrence_and_foreign_brick(self) -> None:
        self.assertEqual(self.create(fragment="Нет такого [цена]").status, "invalid")
        self.assertEqual(self.create(occurrence=2).status, "invalid")
        # Кирпичик, не привязанный к версии, правилом управлять не может.
        unbound = self.make_personal_brick(self.owner, "другой")
        self.assertEqual(self.create(brick_id=unbound).status, "invalid")

    def test_create_rejects_overlapping_rules(self) -> None:
        self.assertTrue(self.create().ok)
        overlap = self.create(fragment="[цена]")
        self.assertEqual(overlap.status, "invalid")
        self.assertIn("пересекается", overlap.message)

    def test_stale_revision_is_reported(self) -> None:
        result = self.create(expected_revision=self.template.revision + 1)
        self.assertEqual(result.status, "stale")
        self.assertIsNone(result.part)

    def test_permissions_follow_template_edit_rights(self) -> None:
        # Чужой личный шаблон неотличим от несуществующего.
        self.assertEqual(
            create_conditional_part(
                self.session,
                actor_telegram_user_id=self.other.telegram_user_id,
                template_id=self.template.template_id,
                expected_revision=self.template.revision,
                section="content",
                brick_id=self.price_brick,
                fragment="💰 Цена: [цена]",
            ).status,
            "not_found",
        )
        self.assertEqual(
            list_conditional_parts(
                self.session, self.other.telegram_user_id, self.template.template_id
            ),
            [],
        )

    def test_update_and_delete_rule(self) -> None:
        created = self.create()
        assert created.part is not None
        updated = update_conditional_part(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            part_id=created.part.id,
            expected_revision=self.template.revision,
            fragment="Цена: [цена]",
        )
        self.assertTrue(updated.ok, updated.message)
        assert updated.part is not None
        self.assertEqual(updated.part.fragment, "Цена: [цена]")
        deleted = delete_conditional_part(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            part_id=created.part.id,
            expected_revision=self.template.revision,
        )
        self.assertEqual(deleted.status, "deleted")
        self.assertIsNone(get_conditional_part(self.session, OWNER_TG, created.part.id))

    def test_edit_text_keeps_rule_only_when_fragment_survives(self) -> None:
        created = self.create()
        assert created.part is not None
        # Текст изменился, но фрагмент остался однозначным → правило
        # переносится в новую версию, revision версии растёт отдельно.
        update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
            expected_revision=self.template.revision,
            content="Новое начало\n💰 Цена: [цена]\nКонец",
        )
        refreshed = refresh_conditional_parts(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
        )
        self.assertEqual(refreshed.status, "refreshed")
        self.assertEqual(refreshed.transferred, 1)
        self.assertEqual(refreshed.stale, 0)
        moved = get_conditional_part(self.session, OWNER_TG, created.part.id)
        assert moved is not None
        self.assertEqual(moved.status, STATUS_ACTIVE)
        self.assertEqual(moved.version, 2)

    def test_edit_text_marks_rule_stale_instead_of_deleting(self) -> None:
        created = self.create()
        assert created.part is not None
        update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
            expected_revision=self.template.revision,
            content="Новое начало\nСовсем другой текст\nКонец",
        )
        refreshed = refresh_conditional_parts(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
        )
        self.assertEqual(refreshed.transferred, 0)
        self.assertEqual(refreshed.stale, 1)
        kept = get_conditional_part(self.session, OWNER_TG, created.part.id)
        assert kept is not None, "правило не удаляется молча (§44)"
        self.assertEqual(kept.status, STATUS_STALE)
        # Stale-правило не применяется к случайному похожему месту.
        self.assertEqual(
            active_rules_for_version(self.session, kept.version_id, "content"),
            (),
        )

    def test_snapshot_and_copy_do_not_inherit_rules(self) -> None:
        created = self.create()
        assert created.part is not None
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
            expected_revision=self.template.revision,
        )
        self.assertEqual(snapshot.source_template_version, self.template.revision)
        copied = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Копия",
            content=self.TEMPLATE_CONTENT,
        )
        self.assertEqual(
            list_conditional_parts(self.session, OWNER_TG, copied.template_id),
            [],
        )
        self.assertEqual(
            self.session.execute(
                sql_text(
                    "SELECT count(*) FROM template_conditional_parts "
                    "WHERE template_id = :id"
                ),
                {"id": copied.template_id},
            ).scalar(),
            0,
        )

    def test_rules_survive_new_session(self) -> None:
        """Правило хранится в БД и читается после перезапуска (§44)."""
        created = self.create()
        assert created.part is not None
        self.session.close()
        self.session = self.Session()
        restored = get_conditional_part(self.session, OWNER_TG, created.part.id)
        assert restored is not None
        self.assertEqual(restored.fragment, "💰 Цена: [цена]")
        self.assertEqual(restored.status, STATUS_ACTIVE)


# --- рендер §42 А–Е, §45 ----------------------------------------------------


class ConditionalRenderTests(unittest.TestCase):
    """
    Примеры §42 и сохранение форматирования §45 на чистом компиляторе.

    Тексты и значения заданы литерально: тест не опирается на шаблоны БД,
    поэтому проверяет именно правило вырезания.
    """

    def test_end_time_example_a(self) -> None:
        """§42-А: `19:00–` при пустом времени окончания → `19:00`."""
        text = "[время_начала]–[время_окончания]"
        rules = (ConditionalRule(brick_id=4, fragment="–[время_окончания]"),)
        empty = apply_conditional_parts(
            text, rules, is_empty=lambda brick_id: brick_id == 4
        )
        self.assertEqual(empty, "[время_начала]")
        filled = apply_conditional_parts(text, rules, is_empty=lambda brick_id: False)
        self.assertEqual(filled, text)

    def test_price_example_b_removes_whole_line(self) -> None:
        """§42-Б/Д: строка целиком и без лишней пустой строки."""
        text = "Афиша\n\n💰 Цена: [цена]\n\nКонтакты"
        rules = (ConditionalRule(brick_id=900, fragment="💰 Цена: [цена]"),)
        result = apply_conditional_parts(text, rules, is_empty=lambda brick_id: True)
        self.assertEqual(result, "Афиша\n\nКонтакты")

    def test_address_example_v_keeps_neighbour_text(self) -> None:
        """§42-В: скрывается только ` ([адрес])`, скобки не чистятся."""
        text = "📍 [место] ([адрес])"
        rules = (ConditionalRule(brick_id=10, fragment=" ([адрес])"),)
        result = apply_conditional_parts(text, rules, is_empty=lambda brick_id: True)
        self.assertEqual(result, "📍 [место]")

    def test_dash_alone_is_not_heuristic(self) -> None:
        """§38: тире вне авторского фрагмента не удаляется."""
        text = "[время_начала]–[время_окончания]"
        rules = (ConditionalRule(brick_id=4, fragment="[время_окончания]"),)
        result = apply_conditional_parts(text, rules, is_empty=lambda brick_id: True)
        self.assertEqual(result, "[время_начала]–")

    def test_repeated_fragment_example_e(self) -> None:
        """§42-Е: одинаковые вхождения различаются номером вхождения."""
        text = "A [цена] B [цена] C"
        rules = (ConditionalRule(brick_id=900, fragment=" [цена]", occurrence=2),)
        result = apply_conditional_parts(text, rules, is_empty=lambda brick_id: True)
        self.assertEqual(result, "A [цена] B C")

    def test_overlapping_rules_ignore_later(self) -> None:
        text = "Цена: [цена] рублей"
        rules = (
            ConditionalRule(brick_id=901, fragment="Цена: [цена]"),
            ConditionalRule(brick_id=900, fragment="[цена]"),
        )
        result = apply_conditional_parts(text, rules, is_empty=lambda brick_id: True)
        # Первое (по позиции) правило выигрывает, пересекающееся игнорируется.
        self.assertEqual(result, " рублей")

    def test_formatting_and_links_survive_hiding(self) -> None:
        """§45: bold/italic/ссылки не рвутся, фрагмент ищется до HTML."""
        text = "**[название]**\n[ссылка](https://example.com)\n💰 [цена]"
        rules = (ConditionalRule(brick_id=900, fragment="💰 [цена]"),)
        hidden = apply_conditional_parts(text, rules, is_empty=lambda brick_id: True)
        self.assertEqual(hidden, "**[название]**\n[ссылка](https://example.com)")
        tokens = parse_template(hidden)
        from template_compiler import render_template as compile_render

        _assign_brick_ids(tokens, {"название": 1, "ссылка": 2})
        html = compile_render(
            {"content": tokens},
            {1: "Концерт", 2: "ссылка"},
            {1: _definition(1), 2: _definition(2)},
        )
        self.assertIn("<b>Концерт</b>", html)
        self.assertIn('<a href="https://example.com">ссылка</a>', html)


def _assign_brick_ids(tokens, bindings: dict[str, int]) -> None:
    """Проставляет brick_id там, где его сделал бы `template_store`."""
    from template_parser import BrickNode, FormatNode, LinkNode

    def visit(node) -> None:
        if isinstance(node, BrickNode):
            node.brick_id = bindings.get(node.code)
        elif isinstance(node, LinkNode):
            for child in node.label:
                visit(child)
            if isinstance(node.url, BrickNode):
                visit(node.url)
        elif isinstance(node, FormatNode):
            for child in node.children:
                visit(child)

    for token in tokens:
        visit(token)


def _definition(brick_id: int):
    """Минимальное определение кирпичика для рендера теста."""
    from brick_store import BrickDefinition
    from models import utcnow

    return BrickDefinition(
        id=brick_id,
        code=f"код{brick_id}",
        title="Тест",
        value_type="short_text",
        scope="personal",
        source="ask",
        place_id=None,
        owner_user_id=None,
        rules={},
        status="active",
        revision=1,
        created_by_user_id=None,
        created_at=utcnow(),
        updated_at=None,
    )


# --- сквозной путь: шаблон версии и анонс -----------------------------------


class ConditionalVersionRenderTests(AnnouncementDatabaseTestCase):
    """`render_template_version` вырезает правила версии ДО разбора текста."""

    def setUp(self) -> None:
        super().setUp()
        self.price_brick = self.make_personal_brick(self.owner, "цена")
        self.template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="С ценой",
            content="Афиша\n💰 Цена: [цена]\nКонец",
        )
        self.assertEqual(
            create_conditional_part(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=self.template.template_id,
                expected_revision=self.template.revision,
                section="content",
                brick_id=self.price_brick,
                fragment="💰 Цена: [цена]",
            ).status,
            "created",
        )

    def render(self, values: dict[int, object] | None = None) -> str:
        return render_template_version(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
            values_by_id=values or {},
        )

    def test_empty_brick_hides_the_fragment_and_line(self) -> None:
        self.assertEqual(self.render(), "Афиша\nКонец")

    def test_filled_brick_keeps_the_fragment(self) -> None:
        self.assertEqual(
            self.render({self.price_brick: "500 ₽"}),
            "Афиша\n💰 Цена: 500 ₽\nКонец",
        )

    def test_announcement_preview_uses_rules(self) -> None:
        draft = self.start_at_questions(template_id=self.template.template_id)
        preview = build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertEqual(preview.html, "Афиша\nКонец")
        saved = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=self.price_brick,
            value="500 ₽",
            expected_revision=draft.revision,
        )
        preview = build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=saved.draft_id,
        )
        self.assertEqual(preview.html, "Афиша\n💰 Цена: 500 ₽\nКонец")

    def test_snapshot_render_does_not_apply_rules(self) -> None:
        """§44: снимок правила не переносит и рендерится без вырезания."""
        title = get_system_brick(self.session, "название")
        assert title is not None
        template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="С ценой в строке",
            content="Афиша [название] 💰 [цена]",
        )
        self.assertEqual(
            create_conditional_part(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=template.template_id,
                expected_revision=template.revision,
                section="content",
                brick_id=self.price_brick,
                fragment=" 💰 [цена]",
            ).status,
            "created",
        )
        values = {title.id: "Концерт"}
        self.assertEqual(
            render_template_version(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=template.template_id,
                values_by_id=values,
            ),
            "Афиша Концерт",
            "версия вырезает авторский фрагмент",
        )

        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=template.template_id,
            expected_revision=template.revision,
        )
        self.assertEqual(snapshot.source_template_version, template.revision)
        rendered = render_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            snapshot_id=snapshot.snapshot_id,
            values_by_id=values,
        )
        self.assertIn("💰", rendered, "снимок правила не применяет")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
