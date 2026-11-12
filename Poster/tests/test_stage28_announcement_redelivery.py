# tests/test_stage28_announcement_redelivery.py
"""
Stage 28: проверяемая замена доставленного анонса.

Проверяется только временная SQLite. Схема допускает ровно один переход
`sent -> sending` с явным переносом адресов прошлой версии; всё остальное
по-прежнему запрещено. Контракт живёт в базовой версии 1.0.0 и закреплён
триггером `trg_announcement_drafts_published_immutable`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import unittest

from migrations import run_migrations
from tests.stage19a_helpers import make_engine, temp_db_path

SENT_PROGRESS = {"fingerprint": "fp-old", "step": 1, "ids": [501, 502], "total": 1}


class AnnouncementRedeliveryTriggerTests(unittest.TestCase):
    """Триггер базовой схемы: ровно один проверяемый переход из `sent`."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-28-trigger-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        run_migrations(self.engine)
        self.connection = sqlite3.connect(self.path)
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA foreign_keys=OFF")
        self.connection.execute(
            "INSERT INTO users (id, telegram_user_id, created_at) "
            "VALUES (1, 1001, '2026-01-01 00:00:00')"
        )
        self.connection.execute(
            "INSERT INTO places (id, created_by_user_id, created_at) "
            "VALUES (1, 1, '2026-01-01 00:00:00')"
        )
        self.connection.commit()
        self.draft_id = self._sent_draft()

    def _sent_draft(self) -> int:
        self.connection.execute(
            """
            INSERT INTO announcement_drafts (
                author_user_id, place_id, place_display_name, stage, revision,
                created_at, updated_at, publication_status, publication_text,
                publication_photos, publication_progress,
                publication_started_at, published_at
            ) VALUES (1, 1, 'Дом культуры', 'completed', 5,
                '2026-01-01 00:00:00', '2026-01-01 00:00:00', 'sent',
                'Готовый текст', '[]', ?, '2026-01-01 00:00:00',
                '2026-01-01 00:00:00')
            """,
            (json.dumps(SENT_PROGRESS),),
        )
        self.connection.commit()
        return int(
            self.connection.execute(
                "SELECT max(id) FROM announcement_drafts"
            ).fetchone()[0]
        )

    def _rebuild(self, *, previous: object | None, status: str = "sending") -> None:
        progress: dict[str, object] = {}
        if previous is not None:
            progress["previous_ids"] = previous
        self.connection.execute(
            """
            UPDATE announcement_drafts SET
                publication_status = ?,
                publication_progress = ?,
                published_at = NULL,
                publication_started_at = '2026-02-01 00:00:00',
                publication_error = NULL,
                revision = revision + 1
            WHERE id = ?
            """,
            (status, json.dumps(progress), self.draft_id),
        )
        self.connection.commit()

    def test_sent_can_rebuild_only_with_old_ids_in_previous_ids(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self._rebuild(previous=None)
        with self.assertRaises(sqlite3.IntegrityError):
            self._rebuild(previous=[999])
        with self.assertRaises(sqlite3.IntegrityError):
            # Старые id нельзя выдать за id текущей отправки.
            self.connection.execute(
                "UPDATE announcement_drafts SET publication_progress = ? WHERE id = ?",
                (
                    json.dumps({**SENT_PROGRESS, "previous_ids": [501, 502]}),
                    self.draft_id,
                ),
            )
            self.connection.commit()

        self._rebuild(previous=[501, 502])
        row = self.connection.execute(
            "SELECT publication_status, published_at, revision, "
            "publication_progress FROM announcement_drafts WHERE id = ?",
            (self.draft_id,),
        ).fetchone()
        self.assertEqual(row[0], "sending")
        self.assertIsNone(row[1])
        self.assertEqual(row[2], 6)
        self.assertEqual(json.loads(row[3])["previous_ids"], [501, 502])
        self.assertNotIn("ids", json.loads(row[3]))

    def test_other_transitions_from_sent_stay_forbidden(self) -> None:
        for status in ("failed", "partial", "sent"):
            with self.subTest(status=status):
                with self.assertRaises(sqlite3.IntegrityError):
                    self._rebuild(previous=[501, 502], status=status)

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE announcement_drafts SET publication_text = 'Другое' "
                "WHERE id = ?",
                (self.draft_id,),
            )
            self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE announcement_drafts SET published_at = "
                "'2026-03-01 00:00:00' WHERE id = ?",
                (self.draft_id,),
            )
            self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE announcement_drafts SET publication_progress = '{}' "
                "WHERE id = ?",
                (self.draft_id,),
            )
            self.connection.commit()


if __name__ == "__main__":
    unittest.main()
