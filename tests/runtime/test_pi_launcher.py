import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.agent.launcher import resolve_pi_command


class PiLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pi launch space ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shim = self.root / 'pi.cmd'
        self.shim.write_text('not executed', encoding='utf-8')
        self.node = str(self.root / 'node-on-path.exe')

    def install(self, root=None, package='@earendil-works/pi-coding-agent'):
        directory = (root or self.root / 'node_modules') / package
        cli = directory / 'dist/bundle/cli.js'
        cli.parent.mkdir(parents=True)
        cli.write_text('// test fixture', encoding='utf-8')
        (directory / 'package.json').write_text(json.dumps({'bin': {'pi': 'dist/bundle/cli.js'}}))
        return cli

    def which(self, name):
        return self.node if name == 'node' else str(self.shim)

    def test_windows_npm_resolves_metadata_and_preserves_args(self):
        cli = self.install()
        with patch('runtime.agent.launcher.shutil.which', side_effect=self.which):
            result = resolve_pi_command(['pi', '--extra'], windows=True)
        self.assertEqual(result, (self.node, str(cli.resolve()), '--extra'))

    def test_local_node_preferred(self):
        cli = self.install()
        node = self.root / 'node.exe'
        node.touch()
        with patch('runtime.agent.launcher.shutil.which', side_effect=self.which):
            self.assertEqual(resolve_pi_command(['pi.cmd'], windows=True), (str(node), str(cli.resolve())))

    def test_local_npm_bin_and_legacy_package(self):
        modules = self.root / 'node_modules'
        self.shim = modules / '.bin/pi.cmd'
        self.shim.parent.mkdir(parents=True)
        self.shim.touch()
        cli = self.install(modules, '@mariozechner/pi-coding-agent')
        with patch('runtime.agent.launcher.shutil.which', side_effect=self.which):
            self.assertEqual(resolve_pi_command([str(self.shim)], windows=True)[1], str(cli.resolve()))

    def test_explicit_node_and_posix_are_unchanged(self):
        with patch('runtime.agent.launcher.shutil.which') as which:
            self.assertEqual(resolve_pi_command(['node', 'custom.js'], windows=True), ('node', 'custom.js'))
            self.assertEqual(resolve_pi_command(['pi'], windows=False), ('pi',))
            which.assert_not_called()

    def test_native_executable(self):
        with patch('runtime.agent.launcher.shutil.which', return_value=str(self.root / 'pi.exe')):
            self.assertEqual(resolve_pi_command(['pi'], windows=True), (str(self.root / 'pi.exe'),))

    def test_missing_pi_and_node(self):
        with patch('runtime.agent.launcher.shutil.which', return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, 'server PATH'):
                resolve_pi_command(['pi'], windows=True)
        self.install()
        with patch('runtime.agent.launcher.shutil.which', side_effect=lambda n: None if n == 'node' else str(self.shim)):
            with self.assertRaisesRegex(FileNotFoundError, 'node.exe'):
                resolve_pi_command(['pi'], windows=True)

    def test_unknown_shim_fails_without_shell(self):
        with patch('runtime.agent.launcher.shutil.which', side_effect=self.which):
            with self.assertRaisesRegex(FileNotFoundError, 'could not locate'):
                resolve_pi_command(['pi'], windows=True)

    def test_invalid_manifest_and_path_escape(self):
        cli = self.install()
        manifest = cli.parents[2] / 'package.json'
        with patch('runtime.agent.launcher.shutil.which', side_effect=self.which):
            for contents in ('{bad', json.dumps({'bin': {'pi': '../../../outside.js'}})):
                manifest.write_text(contents)
                with self.assertRaisesRegex(OSError, 'Invalid Pi installation'):
                    resolve_pi_command(['pi'], windows=True)
