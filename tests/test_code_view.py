import unittest

from frontend.code_view import side_by_side_rows


class SideBySideRowsTests(unittest.TestCase):
    def test_changed_line_sits_opposite_its_replacement_and_added_lines_leave_a_gap(self):
        rows = side_by_side_rows("a\nold\nc", "a\nnew\nc\nd")
        self.assertEqual(rows[0], (1, "a", "same", 1, "a", "same"))
        self.assertEqual(rows[1], (2, "old", "del", 2, "new", "add"))
        self.assertEqual(rows[2], (3, "c", "same", 3, "c", "same"))
        self.assertEqual(rows[3], (None, "", "empty", 4, "d", "add"))
