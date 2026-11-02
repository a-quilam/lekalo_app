# tests/test_utils_pagination.py
"""
Тесты единой пагинации длинных списков Poster 2.5 (spec §14).

Проверяется чистая логика `utils/pagination.py`:

- 0–5 объектов: одна страница, навигации нет (`None` — ни стрелок, ни
  индикатора, ни служебных счётчиков);
- 6+: пять объектов на страницу, число страниц по фактическому количеству;
- первая страница без ◀️, последняя без ▶️, декоративных пустых кнопок нет;
- индикатор `2/4` — no-op callback `noop:<page>:<total>`;
- после удаления последней записи последней страницы страница приводится
  к новой последней (а не показывает пустоту);
- два протокола callback: смещение (`pl_page:5`) и номер страницы
  (`tp_page:1`).

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_utils_pagination -v
"""

import unittest

try:
    from utils import pagination

    IMPORT_ERROR = None
except Exception as e:  # причина попадает в сообщение пропуска
    IMPORT_ERROR = str(e)


def _labels(row):
    return [label for label, _data in row]


def _callbacks(row):
    return [data for _label, data in row]


@unittest.skipIf(IMPORT_ERROR, f"import: {IMPORT_ERROR}")
class PageCountTest(unittest.TestCase):
    """Число страниц — по реальному количеству объектов."""

    def test_empty_list_has_no_pages(self):
        self.assertEqual(pagination.page_count(0), 0)

    def test_page_size_boundaries(self):
        expected = {1: 1, 4: 1, 5: 1, 6: 2, 9: 2, 10: 2, 11: 3, 20: 4}
        for total, pages in expected.items():
            with self.subTest(total=total):
                self.assertEqual(pagination.page_count(total), pages)

    def test_negative_total_counts_as_empty(self):
        self.assertEqual(pagination.page_count(-3), 0)

    def test_non_positive_page_size_rejected(self):
        with self.assertRaises(ValueError):
            pagination.page_count(7, 0)


@unittest.skipIf(IMPORT_ERROR, f"import: {IMPORT_ERROR}")
class ClampPageTest(unittest.TestCase):
    """Приведение страницы к допустимой — сценарий удаления."""

    def test_empty_list_keeps_first_page(self):
        self.assertEqual(pagination.clamp_page(3, 0), 0)

    def test_negative_page_becomes_first(self):
        self.assertEqual(pagination.clamp_page(-2, 12), 0)

    def test_too_far_page_becomes_last(self):
        self.assertEqual(pagination.clamp_page(99, 12), 2)

    def test_last_page_disappears_after_deletion(self):
        # Было 11 объектов (3 страницы), удалили последний — 10 (2 страницы):
        # открытая 3-я страница не должна остаться пустой.
        self.assertEqual(pagination.clamp_page(2, 11), 2)
        self.assertEqual(pagination.clamp_page(2, 10), 1)
        # И дальше — до полного исчезновения пагинации.
        self.assertEqual(pagination.clamp_page(1, 5), 0)


@unittest.skipIf(IMPORT_ERROR, f"import: {IMPORT_ERROR}")
class PageItemsTest(unittest.TestCase):
    """Нарезка списка: пять объектов на страницу, без потерь и дублей."""

    def test_short_list_is_whole_and_unpaginated(self):
        for total in (0, 1, 5):
            with self.subTest(total=total):
                data = list(range(total))
                page = pagination.page_items(data, 0)
                self.assertEqual(page.items, data)
                self.assertEqual(page.total, total)
                self.assertEqual(page.total_pages, 1 if total else 0)
                self.assertFalse(page.has_pagination)
                self.assertFalse(page.has_prev)
                self.assertFalse(page.has_next)

    def test_six_items_split_into_two_pages(self):
        data = list(range(6))
        first = pagination.page_items(data, 0)
        second = pagination.page_items(data, 1)
        self.assertEqual(first.items, [0, 1, 2, 3, 4])
        self.assertEqual(second.items, [5])
        self.assertEqual(first.total_pages, 2)
        self.assertTrue(first.has_pagination)
        self.assertFalse(first.has_prev)
        self.assertTrue(first.has_next)
        self.assertTrue(second.has_prev)
        self.assertFalse(second.has_next)

    def test_pages_cover_list_exactly_once(self):
        data = list(range(11))
        collected = []
        for number in range(pagination.page_count(len(data))):
            collected.extend(pagination.page_items(data, number).items)
        self.assertEqual(collected, data)

    def test_requested_page_is_clamped_on_render(self):
        page = pagination.page_items(list(range(7)), 5)
        self.assertEqual(page.page, 1)
        self.assertEqual(page.items, [5, 6])

    def test_offset_matches_page(self):
        page = pagination.page_items(list(range(11)), 2)
        self.assertEqual(page.offset, 10)

    def test_tuple_unpacking_contract(self):
        items, total_pages, page = pagination.page_items(list(range(11)), 1)
        self.assertEqual(items, [5, 6, 7, 8, 9])
        self.assertEqual(total_pages, 3)
        self.assertEqual(page, 1)


@unittest.skipIf(IMPORT_ERROR, f"import: {IMPORT_ERROR}")
class NavButtonsTest(unittest.TestCase):
    """Одна строка `◀️ | 2/4 | ▶️` — только когда страниц больше одной."""

    def test_no_pagination_returns_none(self):
        self.assertIsNone(pagination.nav_buttons(0, 0, "pl_page"))
        self.assertIsNone(pagination.nav_buttons(0, 1, "pl_page"))

    def test_first_page_has_no_left_arrow(self):
        row = pagination.nav_buttons(0, 4, "pl_page")
        self.assertIsNotNone(row)
        self.assertEqual(_labels(row), ["1/4", "▶️"])

    def test_middle_page_has_both_arrows(self):
        row = pagination.nav_buttons(1, 4, "pl_page")
        self.assertEqual(_labels(row), ["◀️", "2/4", "▶️"])

    def test_last_page_has_no_right_arrow(self):
        row = pagination.nav_buttons(3, 4, "pl_page")
        self.assertEqual(_labels(row), ["◀️", "4/4"])

    def test_no_decorative_empty_buttons(self):
        for page in range(4):
            with self.subTest(page=page):
                row = pagination.nav_buttons(page, 4, "pl_page")
                self.assertTrue(all(label for label, _ in row))
                self.assertTrue(all(data for _, data in row))

    def test_indicator_is_noop_callback(self):
        row = pagination.nav_buttons(1, 4, "pl_page")
        indicator = row[1]
        self.assertEqual(indicator[0], "2/4")
        self.assertTrue(indicator[1].startswith(f"{pagination.NOOP_PREFIX}:"))
        self.assertEqual(indicator[1], "noop:2:4")

    def test_indicator_noop_matches_handler_pattern(self):
        # Регулярное выражение обработчика — единственный контракт no-op.
        import re

        from handlers.noop import NOOP_PATTERN

        for page in range(4):
            row = pagination.nav_buttons(page, 4, "pl_page")
            indicator = next(
                (data, label) for label, data in row if not data.startswith("pl_page:")
            )
            callback, label = indicator
            self.assertIn("/", label)
            self.assertRegex(callback, NOOP_PATTERN)
            self.assertIsNotNone(re.match(NOOP_PATTERN, callback))

    def test_offset_protocol_for_db_lists(self):
        row = pagination.nav_buttons(1, 4, "pl_page", as_offset=True)
        self.assertEqual(_callbacks(row), ["pl_page:0", "noop:2:4", "pl_page:10"])
        # Смещение вычисляется от размера страницы, а не от номера.
        row = pagination.nav_buttons(2, 3, "pl_page", page_size=2)
        self.assertEqual(_callbacks(row), ["pl_page:2", "noop:3:3"])

    def test_page_number_protocol_for_wizard_lists(self):
        row = pagination.nav_buttons(1, 4, "tp_page", as_offset=False)
        self.assertEqual(_callbacks(row), ["tp_page:0", "noop:2:4", "tp_page:2"])

    def test_last_page_offset_renders_both_sides(self):
        # Смещение 10 при 11 объектах — третья (последняя) страница.
        row = pagination.nav_buttons(10 // pagination.PAGE_SIZE, 3, "pl_page")
        self.assertEqual(_labels(row), ["◀️", "3/3"])
        self.assertEqual(_callbacks(row), ["pl_page:5", "noop:3:3"])

    def test_arrow_labels_come_from_catalog(self):
        """Стрелки — живые ключи каталога: правка YAML меняет подпись."""
        from unittest.mock import patch

        with patch("utils.ui_texts.tr", side_effect=lambda key: f"<{key}>"):
            row = pagination.nav_buttons(1, 4, "pl_page")
        self.assertEqual(
            _labels(row),
            [
                f"<{pagination.PREV_LABEL_KEY}>",
                "2/4",
                f"<{pagination.NEXT_LABEL_KEY}>",
            ],
        )

    def test_arrow_labels_fall_back_when_catalog_unavailable(self):
        """Пустой ответ или само имя ключа — показываем стрелку."""
        from unittest.mock import patch

        cases = (
            lambda _key: "",
            lambda key: key,  # tr вернул имя ключа вместо строки
        )
        for fake in cases:
            with self.subTest(fake=fake):
                with patch("utils.ui_texts.tr", side_effect=fake):
                    row = pagination.nav_buttons(1, 4, "pl_page")
                self.assertEqual(_labels(row), ["◀️", "2/4", "▶️"])


@unittest.skipIf(IMPORT_ERROR, f"import: {IMPORT_ERROR}")
class NavKeyboardTest(unittest.TestCase):
    """Реальная клавиатура: та же строка, кнопки PTB."""

    def test_keyboard_is_single_row_of_buttons(self):
        from telegram import InlineKeyboardButton

        row = pagination.nav_keyboard(1, 4, "pl_pickp")
        self.assertIsNotNone(row)
        self.assertTrue(all(isinstance(b, InlineKeyboardButton) for b in row))
        self.assertEqual(
            [(b.text, b.callback_data) for b in row],
            [("◀️", "pl_pickp:0"), ("2/4", "noop:2:4"), ("▶️", "pl_pickp:10")],
        )

    def test_keyboard_absent_for_short_list(self):
        self.assertIsNone(pagination.nav_keyboard(0, 1, "pl_pickp"))

    def test_offset_helper_matches_existing_offset_protocol(self):
        from telegram import InlineKeyboardButton

        row = pagination.nav_keyboard_for_offset(5, 11, "pl_pickp")
        self.assertIsNotNone(row)
        self.assertTrue(all(isinstance(b, InlineKeyboardButton) for b in row))
        self.assertEqual(
            [(b.text, b.callback_data) for b in row],
            [("◀️", "pl_pickp:0"), ("2/3", "noop:2:3"), ("▶️", "pl_pickp:10")],
        )

    def test_offset_helper_single_page(self):
        self.assertIsNone(pagination.nav_keyboard_for_offset(0, 5, "pl_pickp"))


if __name__ == "__main__":
    unittest.main()
