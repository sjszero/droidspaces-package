#!/usr/bin/env python3
"""Collect only native binary packages; keep CI records outside deliverables."""

import argparse
import datetime
import filecmp
import fnmatch
import hashlib
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile

FORMATS = {
    'deb': ('.deb',),
    'rpm': ('.rpm',),
    'arch': ('.pkg.tar.zst', '.pkg.tar.xz', '.pkg.tar.gz'),
}


def package_format(target):
    target = target.lower()
    if target in FORMATS:
        return target
    if target in ('ubuntu2404', 'ubuntu2510', 'ubuntu2604', 'debian13'):
        return 'deb'
    if target in ('fedora43', 'fedora44'):
        return 'rpm'
    raise ValueError(f'unsupported package target: {target}')


def is_package(name, target):
    return (not name.startswith('.') and not any(c in name for c in '\r\n')
            and name.endswith(FORMATS[package_format(target)])
            and not name.endswith(('.src.rpm', '.nosrc.rpm')))


def real_directory(path):
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f'not a real directory: {path}')


def package_files(directory, target):
    real_directory(directory)
    files = {}
    for path in sorted(directory.iterdir()):
        if (path.is_symlink() or not path.is_file() or path.stat().st_size == 0
                or not is_package(path.name, target)):
            raise ValueError(f'unexpected artifact (only native packages allowed): {path}')
        files[path.name] = path
    if not files:
        raise ValueError(f'empty package artifact: {directory}')
    return files


def collect(args):
    destination = args.destination
    if destination.is_symlink():
        raise ValueError(f'symlink destination: {destination}')
    if destination.exists():
        real_directory(destination)
        if any(destination.iterdir()):
            raise ValueError(f'refusing to mix existing artifacts: {destination}')
    if '/' in args.name_prefix or '\\' in args.name_prefix or any(c in args.name_prefix for c in '\r\n'):
        raise ValueError('unsafe package filename prefix')
    # Resolve and compare before creating the destination or copying anything.
    selected = {}
    for source in args.sources:
        if not source.exists():
            continue
        real_directory(source)
        if destination.resolve() == source.resolve() or source.resolve() in destination.resolve().parents:
            raise ValueError('destination must be outside the source tree')
        candidates = source.rglob('*') if args.recursive else source.iterdir()
        for path in sorted(candidates):
            if any(part.startswith('.') for part in path.relative_to(source).parts):
                continue
            if not is_package(path.name, args.target) or any(fnmatch.fnmatchcase(path.name, p) for p in args.exclude):
                continue
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f'invalid native package: {path}')
            # Do not enter symlinked source directories even if a caller supplied one.
            if any(parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents):
                raise ValueError(f'package reached through directory symlink: {path}')
            name = args.name_prefix + path.name
            previous = selected.get(name)
            if previous is not None and not filecmp.cmp(previous, path, shallow=False):
                raise ValueError(f'conflicting package basename: {name}')
            selected[name] = path
    if not selected:
        raise ValueError('no native binary packages produced')
    destination.mkdir(parents=True, exist_ok=True)
    for name, path in sorted(selected.items()):
        output = destination / name
        shutil.copyfile(path, output, follow_symlinks=False)
        shutil.copystat(path, output, follow_symlinks=False)
        output.chmod(0o644)
        with output.open("rb") as stream:
            print(f'{hashlib.file_digest(stream, "sha256").hexdigest()}  {name}')
    package_files(destination, args.target)
    print(f'PACKAGE_ARTIFACTS_OK count={len(selected)} target={args.target}')


def verify(args):
    original = package_files(args.original, args.target)
    downloaded = package_files(args.downloaded, args.target)
    if original.keys() != downloaded.keys():
        raise ValueError('uploaded/downloaded package sets differ')
    for name, path in original.items():
        if not filecmp.cmp(path, downloaded[name], shallow=False):
            raise ValueError(f'downloaded package bytes differ: {name}')
    print(f'PACKAGE_DOWNLOAD_VERIFIED count={len(original)} target={args.target}')


def archive_members(archive, target):
    files = []
    seen = set()
    with tarfile.open(archive, 'r:gz') as stream:
        for member in stream:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts:
                raise ValueError(f'unsafe archive path: {member.name}')
            if member.isdir():
                continue
            if (not member.isfile() or member.size == 0 or not is_package(name.name, target)
                    or any(p.startswith('.') for p in name.parts if p != '.')):
                raise ValueError(f'non-package archive member: {member.name}')
            if name.name in seen:
                raise ValueError(f'duplicate package in archive: {name.name}')
            seen.add(name.name)
            files.append(member)
    if not files:
        raise ValueError(f'empty package archive: {archive}')
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    cmd = commands.add_parser('collect')
    cmd.add_argument('--target', required=True)
    cmd.add_argument('--destination', type=Path, required=True)
    cmd.add_argument('--recursive', action='store_true')
    cmd.add_argument('--exclude', action='append', default=[])
    cmd.add_argument('--name-prefix', default='')
    cmd.add_argument('sources', type=Path, nargs='+')
    cmd.set_defaults(function=collect)
    cmd = commands.add_parser('verify')
    cmd.add_argument('--target', required=True)
    cmd.add_argument('original', type=Path)
    cmd.add_argument('downloaded', type=Path)
    cmd.set_defaults(function=verify)
    for action in ('verify-archive', 'archive-time'):
        cmd = commands.add_parser(action)
        cmd.add_argument('--target', required=True)
        cmd.add_argument('archive', type=Path)
    args = parser.parse_args()
    try:
        package_format(args.target)
        if args.command == 'archive-time':
            # Build time is derived from the packaged files, never a bundled sidecar.
            with tarfile.open(args.archive, 'r:gz') as stream:
                times = [m.mtime for m in stream if m.isfile() and is_package(PurePosixPath(m.name).name, args.target)]
            if not times:
                raise ValueError('archive contains no native packages')
            print(datetime.datetime.fromtimestamp(max(times), datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
        elif args.command == 'verify-archive':
            files = archive_members(args.archive, args.target)
            print(f'PACKAGE_ARCHIVE_OK count={len(files)} target={args.target}')
        else:
            args.function(args)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f'package-artifacts: {error}\n')


if __name__ == '__main__':
    main()
