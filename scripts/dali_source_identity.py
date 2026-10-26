"""Compiled source-pin identity; the kernel digest is measured separately at boot."""
from pathlib import Path
import re
import subprocess
import tomllib
from dali_xnu_source import verify_xnu_source

FIELDS = ('device_commit', 'unixnu_commit', 'uefi_commit', 'xnu_commit')
MAGIC = b'DALIUFSSRC1' + bytes(5)
COMMIT = re.compile(r'[0-9a-f]{40}\Z')


def source_record(pins):
    if not isinstance(pins, dict) or any(not isinstance(pins.get(k), str) or not COMMIT.fullmatch(pins[k]) for k in FIELDS):
        raise ValueError('compiled Dali source identity requires four exact commits')
    return MAGIC + b''.join(pins[k].encode('ascii') + b'\0' for k in FIELDS)


def verify_compiled_record(kernel, pins):
    expected = source_record(pins)
    records, offset = [], 0
    while True:
        offset = kernel.find(MAGIC, offset)
        if offset < 0:
            break
        raw = kernel[offset:offset + len(expected)]
        values = [raw[16 + i*41:16 + (i+1)*41] for i in range(4)]
        if all(len(v) == 41 and v[-1] == 0 and re.fullmatch(b'[0-9a-f]{40}', v[:40]) for v in values):
            records.append(raw)
        offset += 1
    if records != [expected]:
        raise ValueError('kernel compiled source identity is missing, duplicate or differs from the source manifest')


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def build_source_identity(tree):
    # The generic build copies scripts but symlinks driver modules to their
    # actual repositories. Resolve the module sources, never git-discover tree/.
    tree = Path(tree).resolve()
    device = Path(git((tree/'drivers/DaliUfs/DaliUfs.cpp').resolve().parent, 'rev-parse', '--show-toplevel'))
    unixnu = Path(git((tree/'drivers/UfsTransport/UfsTransport.cpp').resolve().parent, 'rev-parse', '--show-toplevel'))
    for repo in (device, unixnu):
        if git(repo, 'status', '--porcelain', '--untracked-files=normal'):
            raise ValueError('compiled source identity requires a clean source checkout: ' + str(repo))
    board = tomllib.loads((device/'board.toml').read_text())
    firmware = board['firmware']
    feeds = tomllib.loads((unixnu/'feeds.toml').read_text())
    xnu_feed = next(entry for entry in feeds['source'] if entry['name'] == 'xnu')
    xnu = xnu_feed['commit']
    pins = dict(device_commit=git(device, 'rev-parse', 'HEAD'), unixnu_commit=git(unixnu, 'rev-parse', 'HEAD'),
                uefi_commit=firmware['uefi_commit'], xnu_commit=git(tree/'.cache/xnu-work', 'rev-parse', 'HEAD'))
    source_record(pins)
    if pins['unixnu_commit'] != firmware['unixnu_commit'] or pins['xnu_commit'] != xnu:
        raise ValueError('compiled Dali source pins differ from board/feeds revisions')
    return pins, verify_xnu_source(tree, device, unixnu, xnu_feed)


def build_source_pins(tree):
    return build_source_identity(tree)[0]
