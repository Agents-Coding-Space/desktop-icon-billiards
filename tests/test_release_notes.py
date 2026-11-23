"""Display-only formatting and the native update dialog's interaction contract."""
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from dib.panel import Pill, show_update_dialog
from dib.release_notes import note_blocks


class ReleaseNoteTests(unittest.TestCase):
    def test_supported_markup_and_untrusted_text_are_display_only(self):
        blocks = note_blocks('# Highlights\n- **Table clear fix**, keeps `Ctrl+Alt+X`\n1. Second item\n> Older versions still work\n\n<script>alert(1)</script>\n**unclosed')
        self.assertEqual([kind for kind, _ in blocks], ['heading', 'list', 'list', 'quote', 'space', 'body', 'body'])
        self.assertIn(('Table clear fix', 'bold'), blocks[1][1])
        self.assertIn(('Ctrl+Alt+X', 'code'), blocks[1][1])
        self.assertEqual(blocks[-2][1], [('<script>alert(1)</script>', '')])
        self.assertEqual(blocks[-1][1], [('**unclosed', '')])
        self.assertEqual(note_blocks('Plain notes')[0], ('body', [('Plain notes', '')]))
        self.assertEqual(note_blocks('`**literal code**`')[0][1], [('**literal code**', 'code')])

    def test_dialog_formats_scrolls_shows_all_history_and_preserves_download_callback(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        entries = [{'version': '0.2.3', 'notes': '# Fix notes\n- **Result window**: consecutive restarts display normally.\n> Tip\nUse `Shift`.\n' + 'Long notes\n' * 40},
                   {'version': '0.2.2', 'notes': 'Historical release notes'}]
        download = Mock()
        entries[0]['date'] = '2026-11-15 16:30'
        with patch('dib.panel.win32api.GetMonitorInfo', return_value={'Work': (0, 0, 640, 440)}):
            dialog = show_update_dialog(root, '0.2.2', entries[0], entries, download)
        root.update()

        def widgets(parent):
            for child in parent.winfo_children():
                yield child
                yield from widgets(child)

        children = list(widgets(dialog))
        notes = next(w for w in children if isinstance(w, tk.Text))
        pills = [w for w in children if isinstance(w, Pill)]
        self.assertEqual(str(notes.cget('state')), 'disabled')
        self.assertTrue(notes.tag_ranges('bold'))
        self.assertTrue(notes.tag_ranges('code'))
        self.assertTrue(notes.tag_ranges('heading'))
        self.assertNotIn('**', notes.get('1.0', 'end'))
        self.assertIn('Historical release notes', notes.get('1.0', 'end'))
        self.assertTrue(notes.tag_ranges('version'))
        self.assertTrue(notes.tag_ranges('date'))
        self.assertIn('2026-11-15 16:30', notes.get('1.0', 'end'))
        self.assertLess(notes.get('1.0', 'end').index('v0.2.3'), notes.get('1.0', 'end').index('v0.2.2'))
        self.assertEqual({w.cget('text') for w in pills}, {'Download Update', 'Later'})
        self.assertLess(notes.yview()[1], 1.)
        self.assertLessEqual(dialog.winfo_width(), 640)
        self.assertLessEqual(dialog.winfo_height(), 440)
        next(w for w in pills if w.cget('text') == 'Download Update').command()
        download.assert_called_once_with(dialog)
        next(w for w in pills if w.cget('text') == 'Later').command()
        self.assertFalse(dialog.winfo_exists())
