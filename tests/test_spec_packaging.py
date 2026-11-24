"""Spec asset packaging and asset-path reasoning, without Tk or a PyInstaller build.

Frozen runtime status: UNVERIFIED. ``dib.config.ASSETS`` derives from the
config module's ``__file__`` and ``dib.paths`` only manages writable runtime
directories; neither reads ``sys._MEIPASS`` or ``sys.frozen``. The simulation
below shows where the loader would look if the config module were unpacked into
an extraction-style base. It is not evidence that a frozen executable works.
"""
import ast
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from dib import config
from dib.commentary import COMMENTARY_ASSET_NAME, load_commentary_lines

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / 'DesktopIconBilliards.spec'
CONFIG_SOURCE = ROOT / 'dib' / 'config.py'


def spec_ast():
    return ast.parse(SPEC.read_text(encoding='utf-8'))


def assigned_joins(tree):
    joins = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Call) \
                and ast.unparse(node.value.func) == 'os.path.join':
            joins[target.id] = node.value
    return joins


def source_parts(node, joins):
    if isinstance(node, ast.Name):
        if node.id == 'base_dir':
            return ()  # the spec root is not part of the relative datas destination
        return source_parts(joins[node.id], joins)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, ast.Call) and ast.unparse(node.func) == 'os.path.join':
        return tuple(part for argument in node.args for part in source_parts(argument, joins))
    raise AssertionError(f'unsupported datas source expression: {ast.unparse(node)}')


def spec_datas():
    tree = spec_ast()
    joins = assigned_joins(tree)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if not any(isinstance(target, ast.Name) and target.id == 'a' for target in node.targets):
            continue
        for keyword in node.value.keywords:
            if keyword.arg != 'datas':
                continue
            if not isinstance(keyword.value, (ast.List, ast.Tuple)):
                raise AssertionError('Analysis datas is not a literal list')
            entries = []
            for element in keyword.value.elts:
                if not isinstance(element, ast.Tuple) or len(element.elts) != 2:
                    raise AssertionError('every datas entry must be a (source, destination) tuple')
                entries.append((source_parts(element.elts[0], joins),
                                ast.literal_eval(element.elts[1])))
            return entries
    raise AssertionError('Analysis(... datas=...) not found in the spec')


class SpecPackagingTests(unittest.TestCase):
    def test_commentary_json_is_packaged_into_the_assets_destination(self):
        entries = spec_datas()
        matches = [source for source, destination in entries
                   if source == ('assets', COMMENTARY_ASSET_NAME) and destination == 'assets']
        self.assertEqual(len(matches), 1, f'commentary datas entries: {entries}')
        for name in ('desktop-icon-billiards.ico', 'donation.jpg', 'desktop-icon-billiards.png'):
            self.assertIn((('assets', name), 'assets'), entries)


class FrozenAssetPathTests(unittest.TestCase):
    def test_asset_paths_come_from_the_module_file_not_a_frozen_override(self):
        self.assertNotIn('_MEIPASS', CONFIG_SOURCE.read_text(encoding='utf-8'))
        self.assertNotIn('sys.frozen', CONFIG_SOURCE.read_text(encoding='utf-8'))
        paths_source = (ROOT / 'dib' / 'paths.py').read_text(encoding='utf-8')
        self.assertNotIn('_MEIPASS', paths_source)
        self.assertNotIn('sys.frozen', paths_source)
        config_source = CONFIG_SOURCE.read_text(encoding='utf-8')
        self.assertIn("BASE = Path(__file__).resolve().parent.parent", config_source)
        self.assertIn("ASSETS = BASE / 'assets'", config_source)

    def test_simulated_extraction_base_resolves_the_loader_asset(self):
        with TemporaryDirectory() as folder:
            fake_base = Path(folder)
            spec = importlib.util.spec_from_file_location('dib.config_meipass_probe', CONFIG_SOURCE)
            probe = importlib.util.module_from_spec(spec)
            probe.__file__ = str(fake_base / 'dib' / 'config.py')
            spec.loader.exec_module(probe)
            self.assertEqual(probe.ASSETS, fake_base / 'assets')
            asset = probe.ASSETS / COMMENTARY_ASSET_NAME
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_text(json.dumps({'scratch': {'easy': ['simulated line']}}, ensure_ascii=False),
                             encoding='utf-8')
            with mock.patch.object(config, 'ASSETS', probe.ASSETS):
                bank = load_commentary_lines(use_cache=False)
            self.assertEqual(bank, {'scratch': {'easy': ['simulated line']}})


if __name__ == '__main__':
    unittest.main()
