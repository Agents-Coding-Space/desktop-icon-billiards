"""Unit tests for the system-tray adapter (no real shell icon is installed)."""
import ctypes
import unittest
from unittest import mock

from dib import tray
from dib.tray import (CMD_MINIMIZE, CMD_PIN, CMD_QUIT, CMD_RESTART, CMD_SHOW, CMD_TOGGLE_WINDOW,
                      MENU_ITEMS, NIF_ICON, NIF_MESSAGE, NIF_TIP,
                      NIM_ADD, NIM_DELETE, NIM_MODIFY, SystemTrayIcon, build_icon_data, menu_label)


class StructTests(unittest.TestCase):
    def test_notify_data_fields(self):
        data = build_icon_data(123, 456, 'Desktop Icon Billiards')
        self.assertEqual(ctypes.sizeof(data), data.cbSize)
        self.assertEqual(data.hWnd, 123)
        self.assertEqual(data.hIcon, 456)
        self.assertEqual(data.uID, 1)
        self.assertEqual(data.uFlags, NIF_MESSAGE | NIF_ICON | NIF_TIP)
        self.assertEqual(data.szTip, 'Desktop Icon Billiards')

    def test_long_tooltip_is_truncated(self):
        data = build_icon_data(1, 2, 'x' * 300)
        self.assertEqual(len(data.szTip), 127)


class DispatchTests(unittest.TestCase):
    def test_menu_commands_call_the_matching_actions(self):
        calls = []
        icon = SystemTrayIcon.__new__(SystemTrayIcon)
        icon.actions = {name: (lambda name=name: calls.append(name))
                        for name in ('show', 'restart', 'quit', 'minimize', 'pin')}
        for command in (CMD_SHOW, CMD_RESTART, CMD_QUIT, CMD_MINIMIZE, CMD_PIN, 9999):
            icon.dispatch(command)
        self.assertEqual(calls, ['show', 'restart', 'quit', 'minimize', 'pin'])


class WindowToggleTests(unittest.TestCase):
    def test_single_toggle_entry_reflects_the_window_state(self):
        commands = [command for command, _label in MENU_ITEMS]
        self.assertEqual(commands.count(CMD_TOGGLE_WINDOW), 1)
        self.assertNotIn(CMD_MINIMIZE, commands)
        self.assertEqual(menu_label(CMD_TOGGLE_WINDOW, True), 'Hide game window')
        self.assertEqual(menu_label(CMD_TOGGLE_WINDOW, False), 'Show game window')
        self.assertEqual(menu_label(CMD_SHOW, False), 'Show toolbar')

    def test_toggle_command_dispatches_the_toggle_action(self):
        calls = []
        icon = SystemTrayIcon.__new__(SystemTrayIcon)
        icon.actions = {'toggle_window': lambda: calls.append('toggle')}
        icon.dispatch(CMD_TOGGLE_WINDOW)
        self.assertEqual(calls, ['toggle'])

    def test_menu_rebuilds_its_label_when_the_window_is_hidden(self):
        icon = SystemTrayIcon.__new__(SystemTrayIcon)
        icon.hwnd = 42
        icon.actions = {}
        icon.window_visible = lambda: False
        with mock.patch.object(tray.win32gui, 'CreatePopupMenu', return_value=3), \
             mock.patch.object(tray.win32gui, 'AppendMenu') as append, \
             mock.patch.object(tray.win32gui, 'GetCursorPos', return_value=(10, 10)), \
             mock.patch.object(tray.win32gui, 'SetForegroundWindow'), \
             mock.patch.object(tray.win32gui, 'TrackPopupMenu', return_value=0), \
             mock.patch.object(tray.win32gui, 'DestroyMenu'), \
             mock.patch.object(tray.win32gui, 'PostMessage'):
            icon._show_menu()
        labels = [call.args[3] for call in append.call_args_list]
        self.assertIn('Show game window', labels)
        self.assertNotIn('Hide game window', labels)


class LifecycleTests(unittest.TestCase):
    def test_install_modify_and_delete(self):
        calls = []

        def fake_notify(message, data):
            calls.append((message, data.contents.szTip))
            return 1

        with mock.patch.object(tray.win32gui, 'RegisterClass'), \
             mock.patch.object(tray.win32gui, 'CreateWindow', return_value=42), \
             mock.patch.object(tray.win32gui, 'LoadImage', return_value=7), \
             mock.patch.object(tray, 'win32api_module_handle', return_value=1), \
             mock.patch.object(tray.win32gui, 'DestroyWindow'), \
             mock.patch.object(tray.win32gui, 'UnregisterClass'), \
             mock.patch.object(tray.win32gui, 'DestroyIcon'):
            icon = SystemTrayIcon({'quit': lambda: None}, tip='Tip', notify=fake_notify)
            self.assertTrue(icon.installed)
            icon.refresh(tip='New tip')
            icon.close()

        self.assertEqual([message for message, _ in calls], [NIM_ADD, NIM_MODIFY, NIM_DELETE])
        self.assertEqual([tip for _, tip in calls], ['Tip', 'New tip', 'New tip'])
        self.assertFalse(icon.installed)
        self.assertIsNone(icon.hwnd)
        self.assertIsNone(icon.hicon)

    def test_failed_install_is_non_fatal(self):
        with mock.patch.object(tray.win32gui, 'RegisterClass'), \
             mock.patch.object(tray.win32gui, 'CreateWindow', return_value=42), \
             mock.patch.object(tray.win32gui, 'LoadImage', return_value=7), \
             mock.patch.object(tray, 'win32api_module_handle', return_value=1), \
             mock.patch.object(tray.win32gui, 'DestroyWindow'), \
             mock.patch.object(tray.win32gui, 'UnregisterClass'), \
             mock.patch.object(tray.win32gui, 'DestroyIcon'):
            icon = SystemTrayIcon({}, notify=lambda message, data: 0)
            self.assertFalse(icon.installed)
            icon.refresh(tip='ignored')
            icon.close()


if __name__ == '__main__':
    unittest.main()
