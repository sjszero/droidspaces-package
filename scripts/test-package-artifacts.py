#!/usr/bin/env python3
"""Behavioral regression tests for package-only cloud deliverables."""

import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().with_name('package-artifacts.py')
spec = importlib.util.spec_from_file_location('package_artifacts', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PackageArtifactTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / '.cache/package-artifact-tests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.destination = self.root / 'deliverables'

    def run_tool(self, *arguments, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, arguments)],
                                capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def add(self, name, data=b'package payload'):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def collect(self, target, *extra, success=True):
        return self.run_tool('collect', '--target', target, '--destination', self.destination,
                             *extra, self.source, success=success)

    def test_exact_allowlist_all_distros(self):
        packages = {'Fedora43': ['wm.aarch64.rpm'], 'Fedora44': ['wm.aarch64.rpm', 'data.noarch.rpm'],
                    'Debian13': ['wm_arm64.deb'], 'ubuntu2604': ['wm_arm64.deb'],
                    'ubuntu2404': ['wm_arm64.deb'], 'ubuntu2510': ['wm_arm64.deb'],
                    'Arch': ['wm-aarch64.pkg.tar.zst', 'other-aarch64.pkg.tar.xz', 'old-aarch64.pkg.tar.gz']}
        for target, names in packages.items():
            with self.subTest(target=target):
                for path in list(self.source.iterdir()):
                    path.unlink()
                if self.destination.exists():
                    shutil.rmtree(self.destination)
                for name in names:
                    self.add(name)
                    for suffix in ('.sig', '.sha256', '.asc', '.part', '.log'):
                        self.add(name + suffix)
                for name in ('SHA256SUMS', 'CI_SHA256SUMS', 'packages.list', 'manifest.env',
                             'build-inputs.json', '.hidden.rpm', 'source.src.rpm', 'source.nosrc.rpm',
                             'build.log', 'dependencies.lock', 'private-key.asc', 'raw.tar.gz'):
                    self.add(name)
                self.collect(target)
                self.assertEqual(sorted(p.name for p in self.destination.iterdir()), sorted(names))

    def test_recursive_collection_ignores_hidden_cache_packages(self):
        self.add('current/wm.rpm')
        self.add('.cache/old.rpm')
        self.add('current/.hidden/nested.rpm')
        self.collect('Fedora44', '--recursive')
        self.assertEqual([p.name for p in self.destination.iterdir()], ['wm.rpm'])
    def test_empty_source_rejected(self):
        self.add('SHA256SUMS')
        self.collect('Fedora44', success=False)
        self.assertFalse(self.destination.exists())

    def test_nonempty_destination_rejected_without_removing_files(self):
        self.add('wm.rpm')
        self.destination.mkdir()
        original = self.destination / 'kept.txt'
        original.write_text('preserve')
        self.collect('Fedora44', success=False)
        self.assertEqual(original.read_text(), 'preserve')

    def test_symlinks_and_empty_package_rejected(self):
        self.add('target.data')
        link = self.source / 'wm.rpm'
        link.symlink_to('target.data')
        self.collect('Fedora44', success=False)
        link.unlink()
        self.add('wm.rpm', b'')
        self.collect('Fedora44', success=False)

    def test_symlink_destination_and_nested_destination_rejected(self):
        self.add('wm.rpm')
        self.destination.symlink_to(self.source, target_is_directory=True)
        self.collect('Fedora44', success=False)
        self.destination.unlink()
        self.run_tool('collect', '--target', 'Fedora44', '--destination', self.source / 'out',
                      self.source, success=False)

    def test_identical_duplicates_deduplicated_conflicts_rejected(self):
        self.add('one/wm.rpm')
        self.add('two/wm.rpm')
        self.collect('Fedora44', '--recursive')
        self.assertEqual([p.name for p in self.destination.iterdir()], ['wm.rpm'])
        shutil.rmtree(self.destination)
        self.add('two/wm.rpm', b'conflict')
        self.collect('Fedora44', '--recursive', success=False)
        self.assertFalse(self.destination.exists())

    def test_runtime_exclusions_preserved(self):
        self.add('kwin.rpm')
        self.add('kwin-debug.rpm')
        self.add('kwin-devel.rpm')
        self.collect('Fedora44', '--exclude', '*debug*', '--exclude', '*devel*')
        self.assertEqual([p.name for p in self.destination.iterdir()], ['kwin.rpm'])

    def test_download_bytes_names_and_sidecars_verified(self):
        self.add('wm.rpm')
        self.collect('Fedora44')
        downloaded = self.root / 'downloaded'
        shutil.copytree(self.destination, downloaded)
        self.run_tool('verify', '--target', 'Fedora44', self.destination, downloaded)
        (downloaded / 'wm.rpm').write_bytes(b'corrupt')
        self.run_tool('verify', '--target', 'Fedora44', self.destination, downloaded, success=False)
        shutil.copyfile(self.destination / 'wm.rpm', downloaded / 'wm.rpm')
        (downloaded / 'SHA256SUMS').write_text('unexpected')
        self.run_tool('verify', '--target', 'Fedora44', self.destination, downloaded, success=False)

    def test_package_archive_excludes_metadata_and_links(self):
        self.add('wm.rpm')
        self.collect('Fedora44')
        archive = self.root / 'packages.tar.gz'
        with tarfile.open(archive, 'w:gz') as stream:
            stream.add(self.destination, arcname='anland-kde-packages/fedora44')
        self.run_tool('verify-archive', '--target', 'Fedora44', archive)
        result = self.run_tool('archive-time', '--target', 'Fedora44', archive)
        self.assertRegex(result.stdout, r'^\d{4}-\d{2}-\d{2}T')
        for name in ('SHA256SUMS', '.anland-kde-build-info', 'wm.rpm.sig', '../escape.rpm'):
            with self.subTest(name=name):
                with tarfile.open(archive, 'w:gz') as stream:
                    data = b'not a binary package'
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    stream.addfile(info, io.BytesIO(data))
                self.run_tool('verify-archive', '--target', 'Fedora44', archive, success=False)

    def test_session_target_filename_prefix(self):
        self.add('anland-session_0.1.0_arm64.deb')
        self.collect('ubuntu2604', '--name-prefix', 'anland-session-ubuntu2604-')
        self.assertEqual([p.name for p in self.destination.iterdir()],
                         ['anland-session-ubuntu2604-anland-session_0.1.0_arm64.deb'])

    def test_prefix_must_not_escape_destination(self):
        self.add('wm.rpm')
        self.collect('Fedora44', '--name-prefix', '../', success=False)


if __name__ == '__main__':
    unittest.main(verbosity=2)