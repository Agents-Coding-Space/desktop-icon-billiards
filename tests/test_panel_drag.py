"""Mock-only regression tests for toolbar drag offsets."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from dib.config import fmt_pos
from dib.panel import MainPanel


class PanelDragTests(unittest.TestCase):
    def make_panel(self):
        panel = object.__new__(MainPanel)
        panel.root = Mock()
        panel.root.winfo_x.return_value = 100
        panel.root.winfo_y.return_value = 80
        panel.root.winfo_height.return_value = 40
        panel.minimized = False
        panel.restore_y = None
        panel.drag_offset = None
        return panel

    def test_motion_before_press_does_not_move(self):
        panel = self.make_panel()
        panel._drag_move(SimpleNamespace(x_root=250, y_root=200))
        panel.root.geometry.assert_not_called()

    def test_press_then_motion_uses_fresh_offset(self):
        panel = self.make_panel()
        panel._drag_start(SimpleNamespace(x_root=150, y_root=130))
        self.assertEqual(panel.drag_offset, (50, 50))
        panel.root.geometry.reset_mock()
        panel._drag_move(SimpleNamespace(x_root=250, y_root=200))
        panel.root.geometry.assert_called_once_with('+200+150')

    def test_restore_clears_offset_and_ignores_motion_until_new_press(self):
        panel = self.make_panel()
        panel._drag_start(SimpleNamespace(x_root=150, y_root=130))
        panel.minimize()
        self.assertTrue(panel.minimized)
        panel.restore()
        self.assertFalse(panel.minimized)
        self.assertIsNone(panel.drag_offset)
        panel.root.geometry.reset_mock()
        panel._drag_move(SimpleNamespace(x_root=250, y_root=200))
        panel.root.geometry.assert_not_called()

    def test_zero_and_negative_coordinates_keep_their_sign(self):
        panel = self.make_panel()
        panel.drag_offset = (150, 130)
        panel._drag_move(SimpleNamespace(x_root=150, y_root=100))
        panel.root.geometry.assert_called_once_with('+0+-30')
        self.assertEqual(fmt_pos(0, 0), '+0+0')
        self.assertEqual(fmt_pos(-40, 0), '+-40+0')

    def test_minimize_keeps_a_negative_y_absolute(self):
        panel = self.make_panel()
        panel.root.winfo_height.return_value = 40
        panel.minimize()
        panel.root.geometry.assert_called_once_with('+100+-20')

    def test_press_while_minimized_restores_without_storing_offset(self):
        panel = self.make_panel()
        panel.minimize()
        panel.root.geometry.reset_mock()
        panel._drag_start(SimpleNamespace(x_root=150, y_root=130))
        self.assertFalse(panel.minimized)
        self.assertIsNone(panel.drag_offset)
        panel.root.geometry.assert_called_once_with('+100+80')
        panel._drag_move(SimpleNamespace(x_root=250, y_root=200))
        panel.root.geometry.assert_called_once_with('+100+80')


if __name__ == '__main__':
    unittest.main()
