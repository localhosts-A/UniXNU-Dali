#!/usr/bin/env python3
"""List every selected pointer-table relocation into a codeless source interface."""
import argparse
from bisect import bisect_right
from collections import defaultdict
import hashlib
import importlib
import json
from pathlib import Path
import struct
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE_KEYS = (
    'CFBundleExecutable', '_PrelinkExecutableLoadAddr', '_PrelinkExecutableSourceAddr',
    '_PrelinkExecutableSize', '_PrelinkKmodInfo', '_PrelinkExecutableRelativePath',
)


def scan_imports(kernelcache, roots, source_interfaces, source_interface_bindings, prelink):
    """Scan a pinned kernelcache without needing a built native kernel."""
    original = prelink.macho(kernelcache)
    if original['kind'] != 2:
        raise ValueError('Expected an MH_EXECUTE kernelcache')
    parent = original['segments']
    info = next((segment for segment in parent if segment['name'] == '__PRELINK_INFO'), None)
    if info is None:
        raise ValueError('Kernelcache has no __PRELINK_INFO segment')
    info_at = prelink.backed_offset(parent, info['va'], info['filesize'], len(kernelcache))
    metadata = prelink.unserialize_xml(kernelcache[info_at:info_at + info['filesize']])
    entries = {}
    for entry in metadata.get('_PrelinkInfoDictionary', []):
        identifier = entry.get('CFBundleIdentifier')
        if not identifier or identifier in entries:
            raise ValueError('Missing or duplicate prelink bundle identifier')
        entries[identifier] = entry

    selected, kernel_interfaces = set(), set()
    pending = list(roots)
    while pending:
        identifier = pending.pop()
        if identifier in selected or identifier in kernel_interfaces:
            continue
        entry = entries.get(identifier)
        if entry is None:
            raise ValueError(f'Missing declared dependency {identifier}')
        if entry.get('OSKernelResource') is True:
            kernel_interfaces.add(identifier)
            continue
        selected.add(identifier)
        pending.extend(entry.get('OSBundleLibraries') or {})

    interface_ids = set(source_interfaces)
    if len(interface_ids) != len(source_interfaces):
        raise ValueError('Duplicate source-interface bundle')
    if not interface_ids.issubset(selected):
        raise ValueError('Every source interface must be in the selected root closure')

    owned, modules = [], {}
    for identifier, entry in entries.items():
        if entry.get('OSKernelResource') is True:
            continue
        executable_va = entry.get('_PrelinkExecutableLoadAddr')
        executable_size = entry.get('_PrelinkExecutableSize')
        if executable_va is None or not executable_size:
            continue
        header_at = prelink.backed_offset(parent, executable_va, 32, len(kernelcache))
        module = prelink.macho(kernelcache, header_at)
        if module['kind'] != 11:
            raise ValueError(f'Expected MH_KEXT_BUNDLE for {identifier}')
        modules[identifier] = module
        for segment in module['segments']:
            if segment['size'] and segment['name'] != '__LINKEDIT':
                owned.append((segment['va'], segment['va'] + segment['size'], identifier, segment))

    for identifier in interface_ids:
        entry = entries[identifier]
        if not entry.get('IOKitPersonalities') or not isinstance(entry.get('OSBundleLibraries'), dict):
            raise ValueError(f'Source interface lacks personalities or dependencies: {identifier}')
        if identifier not in modules:
            raise ValueError(f'Source interface has no executable donor: {identifier}')

    codeless = selected - modules.keys()
    for identifier in codeless:
        entry = entries[identifier]
        personalities = entry.get('IOKitPersonalities')
        if any(key in entry for key in EXECUTABLE_KEYS) or not isinstance(personalities, dict) or not personalities:
            raise ValueError(f'Invalid existing codeless component: {identifier}')

    owned.sort(key=lambda item: item[:3])
    for left, right in zip(owned, owned[1:]):
        if left[1] > right[0]:
            raise ValueError(f'Overlapping component extents: {left[2]} and {right[2]}')
    starts = [item[0] for item in owned]

    def owner(address):
        index = bisect_right(starts, address) - 1
        return owned[index] if index >= 0 and address < owned[index][1] else None

    interface_segments = []
    for identifier in sorted(interface_ids):
        for segment in modules[identifier]['segments']:
            if segment['size'] and segment['name'] != '__LINKEDIT':
                interface_segments.append((segment['va'], segment['va'] + segment['size'], identifier, segment))

    bindings = {}
    for item in source_interface_bindings:
        if not isinstance(item, dict) or set(item) != {'bundle', 'donor_va', 'symbol'}:
            raise ValueError('Source-interface mapping must contain bundle, donor_va, and symbol')
        try:
            donor_va = int(item['donor_va'], 0) if isinstance(item['donor_va'], str) else int(item['donor_va'])
        except (TypeError, ValueError) as exc:
            raise ValueError('Source-interface donor_va must be an integer') from exc
        if item['bundle'] not in interface_ids or donor_va in bindings:
            raise ValueError('Source-interface mapping names an unknown bundle or duplicate donor address')
        matches = [segment for start, end, bundle, segment in interface_segments
                   if start <= donor_va < end and bundle == item['bundle']]
        if len(matches) != 1 or matches[0]['name'] != '__TEXT_EXEC' or not matches[0]['initprot'] & 4:
            raise ValueError(f'Configured donor {donor_va:#x} is not in executable __TEXT_EXEC')
        bindings[donor_va] = item['symbol']

    packed = metadata.get('_PrelinkLinkKASLROffsets')
    if not isinstance(packed, bytes) or len(packed) < 4:
        raise ValueError('Kernelcache has no valid _PrelinkLinkKASLROffsets table')
    count, = struct.unpack_from('<I', packed)
    if len(packed) != 4 + count * 4:
        raise ValueError('Invalid pointer-table extent')
    offsets = struct.unpack_from(f'<{count}I', packed, 4)
    if len(set(offsets)) != count:
        raise ValueError('Duplicate pointer relocation site')
    prelink_text = next((segment for segment in parent if segment['name'] == '__PRELINK_TEXT'), None)
    if prelink_text is None:
        raise ValueError('Kernelcache has no __PRELINK_TEXT segment')

    original_exports = prelink.exports(kernelcache, original)
    references = defaultdict(list)
    selected_sites = 0
    selected_interface_sources = 0
    for offset in offsets:
        site = prelink_text['va'] + offset
        source = owner(site)
        if source is None or source[2] not in selected:
            continue
        if source != owner(site + 7):
            raise ValueError('Pointer spans component extent')
        selected_sites += 1
        if source[2] in interface_ids:
            selected_interface_sources += 1
            continue
        file_offset = prelink.backed_offset(parent, site, 8, len(kernelcache))
        target, = struct.unpack_from('<Q', kernelcache, file_offset)
        destination = owner(target)
        if destination is not None and destination[2] not in selected:
            raise ValueError(f'Undeclared pointer dependency {destination[2]} from {source[2]}')
        donor_segments = [(bundle, segment) for start, end, bundle, segment in interface_segments
                          if start <= target < end]
        if len(donor_segments) > 1:
            raise ValueError(f'Ambiguous source-interface donor target {target:#x}')
        if not donor_segments:
            continue
        bundle, segment = donor_segments[0]
        references[(bundle, target, segment['name'])].append({
            'site_va': f'{site:#x}',
            'file_offset': f'0x{file_offset:x}',
            'consumer_bundle': source[2],
        })

    imports = []
    for (bundle, donor_va, segment_name), sites in sorted(references.items(), key=lambda item: (item[0][0], item[0][1])):
        symbol = bindings.get(donor_va)
        donor_segment = next(segment for _, _, owner_bundle, segment in interface_segments
                             if owner_bundle == bundle and segment['name'] == segment_name and
                             segment['va'] <= donor_va < segment['va'] + segment['size'])
        imports.append({
            'bundle': bundle,
            'donor_va': f'{donor_va:#x}',
            'donor_segment': segment_name,
            'executable': bool(donor_segment['initprot'] & 4),
            'kernelcache_exports': sorted(set(original_exports.get(donor_va, []))),
            'configured_symbol': symbol,
            'reference_count': len(sites),
            'references': sites,
        })

    observed = {(item['bundle'], int(item['donor_va'], 0)) for item in imports}
    configured = {(item['bundle'], int(item['donor_va'], 0)): item['symbol'] for item in source_interface_bindings}
    unresolved = [item for item in imports if item['configured_symbol'] is None or not item['executable']]
    unused = [
        {'bundle': bundle, 'donor_va': f'{donor_va:#x}', 'symbol': symbol}
        for (bundle, donor_va), symbol in sorted(configured.items()) if (bundle, donor_va) not in observed
    ]
    return {
        'schema_version': 1,
        'kernelcache_sha256': hashlib.sha256(kernelcache).hexdigest(),
        'roots': sorted(set(roots)),
        'selected_components': sorted(selected),
        'kernel_interfaces': sorted(kernel_interfaces),
        'source_interfaces': sorted(interface_ids),
        'pointer_table_count': count,
        'selected_pointer_site_count': selected_sites,
        'selected_source_interface_site_count': selected_interface_sources,
        'source_interface_imports': imports,
        'unresolved_donor_imports': unresolved,
        'unused_source_interface_bindings': unused,
        'complete': not unresolved and not unused,
    }


def load_native_prelink(root):
    root = Path(root).resolve()
    if not (root / 'native_prelink.py').is_file():
        raise ValueError(f'NativePrelink module not found under {root}')
    sys.path.insert(0, str(root))
    return importlib.import_module('native_prelink')


def audit_from_board(kernelcache_path, board_path, prelink):
    board = tomllib.loads(Path(board_path).read_text())
    profile = board.get('kernel', {}).get('prelink', {})
    expected = profile.get('kernelcache_sha256')
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError('Board must pin the exact decompressed kernelcache SHA-256')
    data = Path(kernelcache_path).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f'Kernelcache SHA-256 mismatch: expected {expected}, got {actual}')
    report = scan_imports(data, profile.get('roots', []), profile.get('source_interfaces', []),
                          profile.get('source_interface_bindings', []), prelink)
    report['board'] = board.get('board', {}).get('name')
    report['configured_kernelcache_sha256'] = expected
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernelcache', required=True, type=Path)
    parser.add_argument('--board', type=Path, default=ROOT / 'board.toml')
    parser.add_argument('--native-prelink-root', type=Path,
                        default=ROOT.parent / 'UniXNU/kits/NativePrelink')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--fail-on-unresolved', action='store_true')
    args = parser.parse_args()

    report = audit_from_board(args.kernelcache, args.board, load_native_prelink(args.native_prelink_root))
    encoded = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    else:
        print(encoded, end='')
    if args.fail_on_unresolved and not report['complete']:
        raise SystemExit(f"Found {len(report['unresolved_donor_imports'])} unmapped donor targets and "
                         f"{len(report['unused_source_interface_bindings'])} unused mappings")
    print(f"Audited {len(report['source_interface_imports'])} source-interface donor targets; "
          f"{len(report['unresolved_donor_imports'])} unmapped, "
          f"{len(report['unused_source_interface_bindings'])} unused mappings")


if __name__ == '__main__':
    main()
