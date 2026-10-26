"""Small real-Git source tree for cache/provenance integration tests."""
import subprocess


def tree_fixture(base):
    def git(repo, *args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    def repo(path, files):
        path.mkdir(parents=True)
        git(path, 'init', '-q')
        for name, value in files.items():
            target = path/name; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(value)
        git(path, 'add', '-A')
        git(path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'source')
        return path
    tree = base/'tree'
    xnu = repo(tree/'.cache/xnu-work', {'kernel.c': 'fixed source'})
    generic = repo(base/'generic', {'drivers/UfsTransport/UfsTransport.cpp': 'transport',
        'feeds.toml': '[[source]]\nname="xnu"\ncommit="'+git(xnu, 'rev-parse', 'HEAD')+'"\n'+
            'driverkit_tag="xnu-1.0"\ntrustcache_tag="xnu-2.0"\n'})
    device = repo(base/'device', {'drivers/DaliUfs/DaliUfs.cpp': 'platform',
        'board.toml': '[firmware]\nunixnu_commit="'+git(generic, 'rev-parse', 'HEAD')+'"\nuefi_commit="'+'c'*40+'"\n'})
    (tree/'drivers').mkdir()
    for name, source in [('DaliUfs', device), ('UfsTransport', generic)]:
        (tree/'drivers'/name).symlink_to(source/'drivers'/name, target_is_directory=True)
    cache = tree/'.cache/sources'; cache.mkdir()
    for name in ['xnu-driverkit-1.0.json', 'xnu-driverkit-crypto-1.0.json', 'xnu-trustcache-2.0.json']:
        (cache/name).write_text('[]')
    return tree, device, generic, xnu
