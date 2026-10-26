"""Check effective XNU source against HEAD, declared patches and restored public headers.

No network or worktree/index edits. Alternate indexes and unreachable Git objects
are used to compare the entire source, including ignored/untracked files.
Public header indexes are the fixed-tag API/blob records used by the builder;
this checks their consistency, not their independent network provenance.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


def git(repo, *args, env=None, data=None):
    return subprocess.check_output(['git', '-C', str(repo), *args], env=env, input=data, stderr=subprocess.PIPE)


def declared_patches(device, unixnu):
    # Same first-source-wins overlay and lexical order as tree.py + Bash glob.
    patches = {p.name: p for p in (unixnu/'patches/xnu').glob('*.patch')}
    patches.update({p.name: p for p in (device/'patches/xnu').glob('*.patch')})
    return [patches[n] for n in sorted(patches)] + sorted((unixnu/'kits/NativePrelink/patches').glob('*.patch'))


def restored_headers(tree, xnu_feed):
    output, indexes = {}, {}
    driver, trust = xnu_feed.get('driverkit_tag'), xnu_feed.get('trustcache_tag')
    if any(not isinstance(t, str) or not re.fullmatch(r'xnu-[0-9.]+', t) for t in (driver, trust)):
        raise ValueError('fixed public-header source tags are required')
    groups = [
        (f'xnu-driverkit-{driver.removeprefix("xnu-")}.json', driver, 'iokit/DriverKit'),
        (f'xnu-driverkit-crypto-{driver.removeprefix("xnu-")}.json', driver, 'iokit/DriverKit/crypto'),
        (f'xnu-trustcache-{trust.removeprefix("xnu-")}.json', trust, 'EXTERNAL_HEADERS/TrustCache'),
    ]
    for name, tag, prefix in groups:
        raw = (tree/'.cache/sources'/name).read_bytes()
        indexes[name] = hashlib.sha256(raw).hexdigest()
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise ValueError('public-header index must be an array')
        for row in entries:
            if not isinstance(row, dict):
                raise ValueError('invalid public-header index row')
            if row.get('type') != 'file' or not str(row.get('name', '')).endswith('.h'):
                continue
            leaf, blob = row['name'], row.get('sha')
            if not isinstance(leaf, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.h', leaf):
                raise ValueError('public-header filename must be a single header basename')
            path = prefix+'/'+leaf
            url = f'https://raw.githubusercontent.com/apple-oss-distributions/xnu/{tag}/{path}'
            if row.get('download_url') != url or not isinstance(blob, str) or not re.fullmatch(r'[0-9a-f]{40}', blob):
                raise ValueError('public-header fixed tag/URL/blob record mismatch')
            if path in output:
                raise ValueError('duplicate public-header record')
            output[path] = blob
    return output, indexes


def verify_source_tree(repo, patches, headers):
    """Compare source bytes/modes to a clean HEAD plus the declared exact inputs."""
    repo = Path(repo).resolve()
    with tempfile.TemporaryDirectory(prefix='dali-xnu-identity-') as temporary:
        expected_env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary)/'expected'))
        git(repo, 'read-tree', 'HEAD', env=expected_env)
        for patch in patches:
            git(repo, 'apply', '--cached', '--whitespace=nowarn', str(patch.resolve()), env=expected_env)
        for name, blob in sorted(headers.items()):
            if Path(name).is_absolute() or '..' in Path(name).parts or not re.fullmatch(r'[0-9a-f]{40}', blob):
                raise ValueError('invalid restored public-header path/blob')
            # The restoration script leaves upstream/patched tracked files intact.
            tracked = git(repo, 'ls-files', '--stage', '--', name, env=expected_env)
            if tracked:
                continue
            path = repo/name
            try:
                regular = stat.S_ISREG(path.lstat().st_mode)
            except FileNotFoundError:
                raise ValueError('missing restored public header: '+name) from None
            if not regular:
                raise ValueError('restored public header must be a regular file: '+name)
            data = path.read_bytes()
            if hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest() != blob:
                raise ValueError('restored public header differs from fixed blob: '+name)
            actual_blob = git(repo, 'hash-object', '-w', '--stdin', data=data).decode().strip()
            if actual_blob != blob:
                raise ValueError('restored public header Git object mismatch')
            git(repo, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{name}', env=expected_env)
        expected = git(repo, 'write-tree', env=expected_env).decode().strip()
        actual_env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary)/'actual'))
        git(repo, 'read-tree', 'HEAD', env=actual_env)
        git(repo, 'add', '-A', '-f', '--', '.', env=actual_env)
        actual = git(repo, 'write-tree', env=actual_env).decode().strip()
        if actual != expected:
            names = git(repo, 'diff', '--name-only', expected, actual).decode().splitlines()
            raise ValueError('XNU source differs from HEAD + declared patches/headers: '+', '.join(names[:20]))
        return {'git_tree': expected, 'tree_records_sha256': hashlib.sha256(git(repo, 'ls-tree', '-rz', expected)).hexdigest(),
                'untracked_and_ignored_files_checked': True}


def verify_xnu_source(tree, device, unixnu, xnu_feed):
    headers, indexes = restored_headers(tree, xnu_feed)
    patches = declared_patches(device, unixnu)
    result = verify_source_tree(tree/'.cache/xnu-work', patches, headers)
    result['patches_sha256'] = [hashlib.sha256(p.read_bytes()).hexdigest() for p in patches]
    result['public_header_indexes_sha256'] = indexes
    result['public_header_network_provenance_verified_here'] = False
    return result
