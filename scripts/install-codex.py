#!/usr/bin/env python3
"""Install a tested release tarball through Codex's local marketplace CLI."""
import argparse
import filecmp
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


def install(archive, root, codex, monitor=None, smooth_pointer=False):
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.install-', dir=root) as temporary:
        staging = Path(temporary)
        with tarfile.open(archive, 'r:gz') as package:
            package.extractall(staging, filter='data')
        source = staging / 'package'
        manifest = json.loads((source / '.codex-plugin/plugin.json').read_text())
        version = manifest['version']
        if not isinstance(version, str) or not version or any(c not in '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-' for c in version):
            raise ValueError('invalid plugin version')
        destination = root / version
        server_path = source / '.mcp.json'
        config = json.loads(server_path.read_text())
        server = config['mcpServers']['computer_use']
        settings = server.setdefault('env', {})
        if monitor:
            settings['COMPUTER_USE_MCP_MONITOR'] = monitor
        if smooth_pointer:
            settings['COMPUTER_USE_MCP_SMOOTH_POINTER'] = '1'
        if not settings:
            server.pop('env', None)
        server_path.write_text(json.dumps(config, indent=2) + '\n')
        if destination.exists():
            # Registration can be retried after a CLI failure without replacing
            # a running installation or silently changing same-version content.
            for file in source.rglob('*'):
                if file.is_file() and not filecmp.cmp(file, destination / file.relative_to(source), shallow=False):
                    raise FileExistsError(f'{destination} has different contents; use a new release version')
        else:
            shutil.move(str(source), destination)
    listing = subprocess.run([codex, 'plugin', 'marketplace', 'list', '--json'],
                             check=True, capture_output=True, text=True)
    existing = next((item for item in json.loads(listing.stdout)['marketplaces']
                     if item['name'] == 'patlegrand-computer-use'), None)
    changed = existing is not None and Path(existing['root']).resolve() != destination.resolve()
    if changed:
        subprocess.run([codex, 'plugin', 'marketplace', 'remove', 'patlegrand-computer-use'], check=True)
    try:
        subprocess.run([codex, 'plugin', 'marketplace', 'add', str(destination)], check=True)
        subprocess.run([codex, 'plugin', 'add', 'computer-use@patlegrand-computer-use'], check=True)
    except subprocess.CalledProcessError:
        if changed:
            subprocess.run([codex, 'plugin', 'marketplace', 'remove', 'patlegrand-computer-use'], check=False)
            previous = existing['marketplaceSource']
            command = [codex, 'plugin', 'marketplace', 'add', previous['source']]
            if previous.get('ref'):
                command.extend(['--ref', previous['ref']])
            subprocess.run(command, check=True)
        raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--monitor', help='exact KDE output name; defaults to portal selection')
    parser.add_argument('--smooth-pointer', action='store_true')
    parser.add_argument('--codex', default='codex', help='Codex CLI executable')
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'patlegrand-computer-use')
    args = parser.parse_args()
    destination = install(args.archive, args.root, args.codex, args.monitor, args.smooth_pointer)
    print(f'Installed at {destination}. Restart the desktop app to load the updated plugin.')


if __name__ == '__main__':
    main()
