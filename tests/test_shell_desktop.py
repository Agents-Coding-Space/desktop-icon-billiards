"""Desktop discovery must not dereference an absent Shell object."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dib.shell import _find_desktop, FWF_NOICONS, ShellDesktop, atomic_json


class DesktopDiscoveryTests(unittest.TestCase):
    def test_transient_missing_object_or_window_retries_then_succeeds(self):
        dispatch = object()
        for missing in ((0, None), (100, None), (0, dispatch), (99, dispatch)):
            with self.subTest(missing=missing):
                lookup = Mock(side_effect=[missing, (100, dispatch)])
                with patch('win32gui.IsWindow', side_effect=lambda hwnd: hwnd == 100), \
                        patch('dib.shell.time.sleep') as sleep:
                    self.assertEqual(_find_desktop(lookup), (100, dispatch))
                self.assertEqual(lookup.call_count, 2)
                sleep.assert_called_once_with(.15)

    def test_persistent_missing_desktop_is_bounded_and_other_errors_propagate(self):
        lookup = Mock(return_value=(0, None))
        with patch('dib.shell.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'Cannot reach the Windows desktop right now'):
                _find_desktop(lookup)
        self.assertEqual(lookup.call_count, 5)
        self.assertEqual(sleep.call_count, 4)
        error = PermissionError('blocked')
        with self.assertRaises(PermissionError) as caught:
            _find_desktop(Mock(side_effect=error))
        self.assertIs(caught.exception, error)


class DesktopDisplayNameTests(unittest.TestCase):
    def desktop(self):
        desktop = object.__new__(ShellDesktop)
        desktop.folder = Mock()
        desktop.shellcon = Mock(SHGDN_NORMAL=0)
        desktop.cache = {r'C:\Users\example\Desktop\report.lnk': object(),
                         '::{645FF040-5081-101B-9F08-00AA002F954E}': object()}
        return desktop

    def test_screenshot_com_failure_keeps_item_and_next_label_readable(self):
        import pywintypes

        desktop = self.desktop()
        identities = list(desktop.cache)
        cached = desktop.cache.copy()
        desktop.folder.GetDisplayNameOf.side_effect = [
            pywintypes.com_error(-2147467259, 'Unspecified error', None, None), 'Recycle Bin']
        with patch('dib.diag.log') as log:
            names = [desktop.display_name(identity) for identity in identities]
        self.assertEqual(names, ['', 'Recycle Bin'])
        self.assertEqual(desktop.cache, cached)
        self.assertIn('80004005', log.call_args.args[0])
        self.assertNotIn(identities[0], log.call_args.args[0])

    def test_virtual_item_failure_or_empty_name_omits_label(self):
        import pywintypes

        desktop = self.desktop()
        identity = list(desktop.cache)[1]
        desktop.folder.GetDisplayNameOf.side_effect = [
            pywintypes.com_error(-2147467259, 'Unspecified error', None, None), '', '   ']
        with patch('dib.diag.log'):
            for _ in range(3):
                self.assertEqual(desktop.display_name(identity), '')


class ShowIconsTests(unittest.TestCase):
    def desktop(self):
        desktop = object.__new__(ShellDesktop)
        desktop.get_flags = Mock(return_value=0)
        desktop.set_flags = Mock()
        return desktop

    def test_visible_icons_are_a_read_only_no_op(self):
        desktop = self.desktop()
        desktop.get_flags.return_value = 0x1 | 0x4
        desktop.show_icons()
        desktop.get_flags.assert_called_once_with()
        desktop.set_flags.assert_not_called()

    def test_hidden_icons_are_shown_with_one_masked_write(self):
        desktop = self.desktop()
        desktop.get_flags.side_effect = [FWF_NOICONS | 0x1, 0x1]
        desktop.show_icons()
        self.assertEqual(desktop.get_flags.call_count, 2)
        desktop.set_flags.assert_called_once_with(FWF_NOICONS, 0)

    def test_com_errors_propagate_unchanged(self):
        import pywintypes

        error = pywintypes.com_error(-2147467259, 'Unspecified error', None, None)
        read_failure = self.desktop()
        read_failure.get_flags.side_effect = error
        with self.assertRaises(pywintypes.com_error) as caught:
            read_failure.show_icons()
        self.assertIs(caught.exception, error)
        read_failure.set_flags.assert_not_called()

        write_failure = self.desktop()
        write_failure.get_flags.side_effect = [FWF_NOICONS]
        write_failure.set_flags.side_effect = error
        with self.assertRaises(pywintypes.com_error) as caught:
            write_failure.show_icons()
        self.assertIs(caught.exception, error)
        self.assertEqual(write_failure.get_flags.call_count, 1)

    def test_still_hidden_after_write_raises(self):
        desktop = self.desktop()
        desktop.get_flags.side_effect = [FWF_NOICONS, FWF_NOICONS]
        with self.assertRaisesRegex(RuntimeError, 'hidden-icons setting is still active'):
            desktop.show_icons()
        desktop.set_flags.assert_called_once_with(FWF_NOICONS, 0)


class AtomicJournalTests(unittest.TestCase):
    def test_replace_retries_a_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'layout.json'
            target.write_text('{}', encoding='utf-8')
            original = Path.replace
            attempts = []

            def flaky(self, to):
                if '.tmp-' in self.name and len(attempts) < 2:
                    attempts.append(self)
                    raise PermissionError('reader still has the file open')
                return original(self, to)

            with patch('pathlib.Path.replace', new=flaky), patch('dib.shell.time.sleep') as sleep:
                atomic_json(target, {'active': False})
            self.assertEqual(len(attempts), 2)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'active': False})

    def test_persistent_permission_error_removes_tmp(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'layout.json'
            target.write_text('{}', encoding='utf-8')
            with patch('pathlib.Path.replace', side_effect=PermissionError('locked')),                  patch('dib.shell.time.sleep'):
                with self.assertRaises(PermissionError):
                    atomic_json(target, {'active': True})
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ['layout.json'])


