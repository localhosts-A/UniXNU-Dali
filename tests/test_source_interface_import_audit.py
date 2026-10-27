import base64
import importlib.util
from pathlib import Path
import struct
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'source_interface_import_audit', ROOT / 'scripts/audit-source-interface-imports.py')
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)
PRELINK = AUDIT.load_native_prelink(ROOT.parent / 'UniXNU/kits/NativePrelink')


def segment(name, va, size, off, length, initprot=5):
    return struct.pack('<II16s4Q4I', 0x19, 72, name.encode(), va, size, off, length,
                       7, initprot, 0, 0)


def header(kind, commands):
    return struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, kind, len(commands),
                       sum(map(len, commands)), 0, 0) + b''.join(commands)


def plist_value(value):
    if isinstance(value, dict):
        element = ET.Element('dict')
        for key, child in value.items():
            ET.SubElement(element, 'key').text = key
            element.append(plist_value(child))
    elif isinstance(value, list):
        element = ET.Element('array')
        element.extend(plist_value(child) for child in value)
    elif isinstance(value, bool):
        element = ET.Element('true' if value else 'false')
    elif isinstance(value, int):
        element = ET.Element('integer')
        element.text = hex(value)
    elif isinstance(value, bytes):
        element = ET.Element('data')
        element.text = base64.b64encode(value).decode()
    else:
        element = ET.Element('string')
        element.text = value
    return element


def fixture(*, dependency=True):
    storage_id = 'com.apple.iokit.IOStorageFamily'
    entries = [
        {
            'CFBundleIdentifier': 'test.consumer',
            'CFBundleExecutable': 'consumer',
            '_PrelinkExecutableLoadAddr': 0x2000,
            '_PrelinkExecutableSize': 0x100,
            'OSBundleLibraries': ({storage_id: '1.0'} if dependency else {}),
        },
        {
            'CFBundleIdentifier': storage_id,
            'CFBundleExecutable': 'IOStorageFamily',
            '_PrelinkExecutableLoadAddr': 0x2100,
            '_PrelinkExecutableSize': 0x100,
            'OSBundleLibraries': {'com.apple.kpi.iokit': '1.0'},
            'IOKitPersonalities': {'StorageFixture': {}},
        },
        {
            'CFBundleIdentifier': 'test.unselected',
            'CFBundleExecutable': 'unselected',
            '_PrelinkExecutableLoadAddr': 0x2200,
            '_PrelinkExecutableSize': 0x100,
            'OSBundleLibraries': {},
        },
        {'CFBundleIdentifier': 'com.apple.kpi.iokit', 'OSKernelResource': True},
    ]
    offsets = [0x80, 0x88, 0xa0, 0x1030, 0x2080]
    metadata = {
        '_PrelinkInfoDictionary': entries,
        '_PrelinkLinkKASLROffsets': struct.pack(f'<{len(offsets) + 1}I', len(offsets), *offsets),
    }
    xml = ET.tostring(plist_value(metadata)) + b'\0'

    original = bytearray(0x9000)
    original_header = header(2, [
        segment('__PRELINK_TEXT', 0x2000, 0x4000, 0x1000, 0x4000),
        segment('__PRELINK_INFO', 0x8000, 0x2000, 0x6000, len(xml)),
        struct.pack('<6I', 2, 24, 0x8500, 0, 0x8500, 1),
    ])
    original[:len(original_header)] = original_header

    for module_va, module_commands in [
        (0x2000, [segment('__TEXT', 0x2000, 0x100, 0, 0x100)]),
        (0x2100, [segment('__TEXT_EXEC', 0x3000, 0x100, 0, 0x100)]),
        (0x2200, [segment('__TEXT', 0x4000, 0x100, 0, 0x100)]),
    ]:
        module = header(11, module_commands)
        module_offset = 0x1000 + module_va - 0x2000
        original[module_offset:module_offset + len(module)] = module

    for site, target in [
        (0x2080, 0x3040),
        (0x2088, 0x3040),
        (0x20a0, 0x3050),
        (0x3030, 0x3040),
        (0x4080, 0x3040),
    ]:
        offset = 0x1000 + site - 0x2000
        struct.pack_into('<Q', original, offset, target)
    original[0x6000:0x6000 + len(xml)] = xml
    original[0x8500] = 0
    return bytes(original), storage_id


class SourceInterfaceImportAuditTests(unittest.TestCase):
    def test_batches_sites_for_all_unique_donors_in_selected_closure(self):
        original, storage_id = fixture()
        report = AUDIT.scan_imports(
            original,
            ['test.consumer'],
            [storage_id],
            [{'bundle': storage_id, 'donor_va': '0x3040', 'symbol': '__Zfixture'}],
            PRELINK,
        )

        imports = {int(item['donor_va'], 0): item for item in report['source_interface_imports']}
        self.assertEqual(set(imports), {0x3040, 0x3050})
        self.assertEqual(imports[0x3040]['reference_count'], 2)
        self.assertEqual(imports[0x3040]['configured_symbol'], '__Zfixture')
        self.assertEqual(imports[0x3050]['reference_count'], 1)
        self.assertEqual([item['consumer_bundle'] for item in imports[0x3040]['references']],
                         ['test.consumer', 'test.consumer'])
        self.assertEqual([item['donor_va'] for item in report['unresolved_donor_imports']], ['0x3050'])
        self.assertEqual(report['unused_source_interface_bindings'], [])
        self.assertEqual(report['selected_source_interface_site_count'], 1)
        self.assertFalse(report['complete'])

    def test_source_interface_must_be_selected_by_dependency_closure(self):
        original, storage_id = fixture(dependency=False)
        with self.assertRaisesRegex(ValueError, 'selected root closure'):
            AUDIT.scan_imports(original, ['test.consumer'], [storage_id], [], PRELINK)


if __name__ == '__main__':
    unittest.main()
