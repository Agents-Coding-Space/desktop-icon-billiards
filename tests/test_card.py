"""The shareable result card."""
import struct
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from dib import card

BASE = {'time': '06:12.34', 'average': '10.63', 'balls': '35', 'shots': '41', 'note': ''}
WHEN = time.strptime('2026-11-11', '%Y-%m-%d')


def view(**rank):
    return {**BASE, 'rank': rank}


def ok(**changes):
    return view(phase='ok', rank=1204, percentile=87, best_rank=860, new_best=False, **changes)


def render(v):
    return card.render_card(v, when=WHEN, credit='Sample label', site='example.invalid')


class TierTests(unittest.TestCase):
    def test_tiers_follow_the_percentile(self):
        expected = {100: 'Billiards Legend', 99: 'Billiards Legend', 98: 'Desktop King', 95: 'Desktop King', 94: 'Master', 85: 'Master',
                    84: 'Expert', 65: 'Expert', 64: 'Skilled', 40: 'Skilled', 39: 'Rookie', 0: 'Rookie'}
        for percentile, title in expected.items():
            with self.subTest(percentile=percentile):
                self.assertEqual(card.tier_for(percentile)[0], title)

    def test_every_tier_has_its_own_colour(self):
        colours = {colour for _, _, colour in card.TIERS}
        self.assertEqual(len(colours), len(card.TIERS))


class RenderTests(unittest.TestCase):
    def test_the_card_is_a_4_by_5_rgb_poster(self):
        image = render(ok())
        self.assertEqual((image.size, image.mode), ((1080, 1350), 'RGB'))

    def test_every_ranking_phase_renders(self):
        for rank in ({'phase': 'ok', 'rank': 1, 'percentile': 100, 'best_rank': 1, 'new_best': True},
                     {'phase': 'ok', 'rank': 1234567, 'percentile': 0, 'best_rank': 9999999, 'new_best': False},
                     {'phase': 'off'}, {'phase': 'pending'}, {'phase': 'unavailable'},
                     {'phase': 'skipped', 'message': 'Ranking was enabled after this round started; counted from the next round.'},
                     {'phase': 'error', 'message': 'Score failed validation; not ranked'}):
            with self.subTest(phase=rank['phase'], rank=rank.get('rank')):
                self.assertEqual(render({**BASE, 'rank': rank}).size, (1080, 1350))

    def test_a_huge_rank_still_fits_inside_the_card(self):
        image = render(view(phase='ok', rank=12_345_678, percentile=50, best_rank=1, new_best=False))
        # Nothing but the background may touch the extreme left and right columns around the hero.
        for x in (0, 1079):
            for y in range(380, 660, 40):
                r, g, b = image.getpixel((x, y))
                self.assertLess(max(r, g, b), 120)

    def test_tiers_change_the_look(self):
        low = render(view(phase='ok', rank=900, percentile=20, best_rank=900, new_best=False))
        top = render(view(phase='ok', rank=2, percentile=99, best_rank=2, new_best=False))
        self.assertNotEqual(low.tobytes(), top.tobytes())

    def test_the_tier_badge_is_translucent_not_a_solid_slab(self):
        image = render(ok())  # percentile 87 -> Master, purple
        _, colour = card.tier_for(87)
        # A point inside the badge, left of the text: tinted, but far darker than the solid colour.
        r, g, b = image.getpixel((470, 277))
        self.assertLess(r + g + b, (sum(colour)) * .75)
        self.assertGreater(r + g + b, 3 * 40)

    def test_winner_is_drawn_in_the_footer(self):
        plain = render(ok())
        won = render({**ok(), 'winner': 'You win 3-2'})
        footer = (0, 1270, 1080, 1330)
        self.assertNotEqual(plain.crop(footer).tobytes(), won.crop(footer).tobytes())

    def test_credit_and_site_are_drawn_in_the_footer(self):
        footer = (0, 1270, 1080, 1330)
        plain = card.render_card(ok(), when=WHEN).crop(footer).tobytes()
        credited = card.render_card(ok(), when=WHEN, credit='Sample label').crop(footer).tobytes()
        sited = card.render_card(ok(), when=WHEN, site='example.invalid').crop(footer).tobytes()
        self.assertNotEqual(plain, credited)
        self.assertNotEqual(plain, sited)
        self.assertNotEqual(credited, sited)

    def test_new_record_badge_only_when_it_is_one(self):
        plain = render(ok())
        record = render(view(phase='ok', rank=1204, percentile=87, best_rank=860, new_best=True))
        area = (800, 1150, 1010, 1225)
        self.assertNotEqual(plain.crop(area).tobytes(), record.crop(area).tobytes())

    def test_unranked_rounds_lead_with_the_average_time(self):
        off = render(view(phase='off'))
        self.assertNotEqual(off.tobytes(), render(ok()).tobytes())

    def test_the_card_never_contains_the_desktop(self):
        """Only the app's own logo is used; no desktop icon artwork or names are drawn."""
        source = Path(card.__file__).read_text(encoding='utf-8')
        imports = [line for line in source.splitlines() if line.startswith(('import ', 'from '))]
        for line in imports:
            self.assertNotRegex(line, r'overlay|shell|\.ui|wallpaper', 'the card must not reach into the desktop')
        self.assertEqual(card.LOGO.name, 'desktop-icon-billiards.png')


class SharingTests(unittest.TestCase):
    def test_save_writes_a_png_with_a_timestamped_name(self):
        image = Image.new('RGB', (40, 50), 'white')
        with tempfile.TemporaryDirectory() as folder:
            path = card.save_card(image, Path(folder) / 'sub', when=time.strptime('2026-11-11 08:09:10', '%Y-%m-%d %H:%M:%S'))
            self.assertEqual(path.parent, Path(folder) / 'sub')
            self.assertEqual(path.name, 'DesktopIconBilliards-Result-20261111-080910.png')
            with Image.open(path) as saved:
                self.assertEqual((saved.format, saved.size), ('PNG', (40, 50)))

    def test_clipboard_bitmap_is_a_dib_without_the_file_header(self):
        data = card.dib_bytes(Image.new('RGB', (4, 3), (10, 20, 30)))
        header_size, width, height = struct.unpack('<Iii', data[:12])
        self.assertEqual((header_size, width, abs(height)), (40, 4, 3))
        self.assertFalse(data.startswith(b'BM'))

    def test_pictures_directory_is_inside_the_users_pictures_or_home(self):
        directory = card.pictures_directory()
        self.assertEqual(directory.name, 'DesktopIconBilliards')


if __name__ == '__main__':
    unittest.main()
