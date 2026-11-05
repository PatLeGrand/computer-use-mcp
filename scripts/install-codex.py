#!/usr/bin/env python3
"""Install a tested release tarball through Codex's local marketplace CLI."""
import argparse
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
        if destination.exists():
            raise FileExistsError(f'{destination} already exists; use a new release version')
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
        shutil.move(str(source), destination)
    subprocess.run([codex, 'plugin', 'marketplace', 'add', str(destination)], check=True)
    subprocess.run([codex, 'plugin', 'add', 'computer-use@patlegrand-computer-use'], check=True)
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
