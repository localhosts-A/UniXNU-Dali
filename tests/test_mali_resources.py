"""Owned FDT fixtures exercise the real Mali resource serializer and ADT CLI."""
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from test_ufs_resources import adt, cells, dtb, fixture as base_fixture, SCRIPTS
from dali_mali_resources import (GPU, GPUEB, GHPM, FREQUENCY, IPI_NAMES, IPI_SEND_WORDS,
                                IPI_RECEIVE, IRQ_NAMES, MAGIC, SPIS, WINDOWS, resource_contract)


def firmware():
    image = bytearray(128)
    struct.pack_into('<I', image, 0, 0xc3f13a6e)
    image[4] = 3
    return bytes(image)


def fixture():
    nodes = base_fixture()
    nodes['/reserved-memory'] = {'#address-cells': cells(2), '#size-cells': cells(2)}
    nodes['/reserved-memory/gpueb'] = {'compatible': b'mediatek,me_GPUEB_SHARED\0', 'reg': cells(0, 0xfc810000, 0, 0x180000)}
    nodes['/gpufreq-wrapper'] = {'gpueb-support': cells(1)}
    nodes[GPU] = {'compatible': b'mediatek,mali\0arm,mali-valhall\0', 'reg': cells(0, 0x48000000, 0, 0x480000),
                  'interrupts': cells(*(x for spi in SPIS for x in (0, spi, 4, 0))),
                  'interrupt-names': b''.join(s.encode() + b'\0' for s in IRQ_NAMES)}
    nodes[FREQUENCY] = {'compatible': b'mediatek,gpufreq\0'}
    nodes[GPUEB] = {'compatible': b'mediatek,gpueb\0', 'gpueb-support': cells(1), 'ghpm-support': cells(1),
                    'gpueb_mem_size': cells(0,0x180000), 'gpueb_mem_addr': cells(0,0xfc810000),
                    'gpueb-mem-table': cells(0,0x4000,1,0x180000),
                    'gpueb-mem-name-table': b'MEM_ID_GPUFREQ\0MEM_ID_LOG\0',
                    'mbox-count': cells(1), 'mbox-size': cells(160), 'slot-size': cells(4),
                    'send-name-table': b''.join(s.encode() + b'\0' for s in IPI_NAMES),
                    'recv-name-table': b''.join(s.encode() + b'\0' for s in IPI_NAMES),
                    'send-table': cells(*(x for i,n in enumerate(IPI_SEND_WORDS) for x in (i,0,n))),
                    'recv-table': cells(*(x for i,fields in enumerate(IPI_RECEIVE) for x in (i,0,*fields)))}
    nodes[GHPM] = {'compatible': b'mediatek,ghpm\0'}
    for path in (FREQUENCY, GPUEB, GHPM):
        windows = [w for w in WINDOWS.values() if w[0] == path]
        nodes[path]['reg-names'] = b''.join(w[1].encode() + b'\0' for w in windows)
        nodes[path]['reg'] = cells(*(x for _, _, base, length in windows for x in (0, base, 0, length)))
    nodes[GHPM]['reg-names'] += b'mfg_rpc\0mfg_vcore_ao_config\0'
    nodes[GHPM]['reg'] += cells(0,0x4b800000,0,0x1000,0,0x4b860000,0,0x1000)
    return nodes


class MaliResourceTests(unittest.TestCase):
    def contract(self, nodes, image=None):
        raw = dtb(nodes)
        return resource_contract(adt.Fdt(raw), raw, firmware() if image is None else image)

    def test_wire_header_and_both_digests(self):
        raw = dtb(fixture()); image = firmware()
        blob = resource_contract(adt.Fdt(raw), raw, image)
        self.assertEqual(struct.unpack_from('<8Q', blob), (MAGIC, 1, 30, 6, 4096, len(image), 13, 1))
        self.assertEqual(blob[64:96], hashlib.sha256(raw).digest())
        self.assertEqual(blob[96:128], hashlib.sha256(image).digest())
        self.assertEqual(struct.unpack_from('<6Q', blob, 128), SPIS)
        self.assertEqual(len(blob), 128 + 48 + 30 * 24)
        self.assertEqual(struct.unpack_from('<IIQQ', blob, len(blob) - 24), (128, 2, 0xfc810000, 0x180000))

    def test_wrong_aperture_irq_route_and_coherency_fail(self):
        for path, key, value in ((GPU, 'reg', cells(0, 0x48000000, 0, 0x490000)),
                                 (GPU, 'interrupts', cells(0, 604, 4)),
                                 (GPU, 'iommus', cells(1)), (GPU, 'dma-coherent', b''),
                                 (GPU, 'status', b'disabled\0'), (GPUEB, 'gpueb-support', cells(0)),
                                 ('/soc', 'ranges', cells(0)),
                                 ('/reserved-memory/gpueb', 'reg', cells(0, 0xfc800000, 0, 0x180000))):
            with self.subTest(path=path, key=key):
                nodes = fixture(); nodes[path][key] = value
                with self.assertRaises(ValueError): self.contract(nodes)

    def test_every_named_window_is_bound(self):
        for path in (FREQUENCY, GPUEB, GHPM):
            for offset in range(len(fixture()[path]['reg']) // 16):
                nodes = fixture(); value = bytearray(nodes[path]['reg'])
                value[offset * 16 + 7] ^= 4; nodes[path]['reg'] = bytes(value)
                with self.subTest(path=path, window=offset), self.assertRaises(ValueError): self.contract(nodes)

    def test_changed_mailbox_layout_fails(self):
        for key, value in (('slot-size', cells(8)), ('mbox-size', cells(128)),
                           ('send-table', cells(1,0,7)), ('recv-table', cells(1,0,8,0,0)),
                           ('send-name-table', b'GPUFREQ\0'), ('ghpm-support', cells(0))):
            nodes = fixture(); nodes[GPUEB][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.contract(nodes)
        nodes = fixture(); nodes[GPUEB]['gpueb-mem-table'] = cells(0,0x8000,1,0x180000)
        with self.assertRaises(ValueError): self.contract(nodes)

    def test_firmware_header_and_duplicate_names_fail(self):
        for image in (b'', bytes(128), firmware()[:19]):
            with self.assertRaises(ValueError): self.contract(fixture(), image)
        nodes = fixture(); names = nodes[GPUEB]['reg-names'].split(b'\0'); names[1] = names[0]
        nodes[GPUEB]['reg-names'] = b'\0'.join(names)
        with self.assertRaises(ValueError): self.contract(nodes)

    def test_nub_and_firmware_require_explicit_cli_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); raw = dtb(fixture())
            source = root/'fdt'; source.write_bytes(raw)
            image = root/'firmware'; image.write_bytes(firmware())
            output = root/'adt'
            command = [sys.executable, str(SCRIPTS/'make-dali-adt.py'), '--fdt', str(source),
                       '--dram-size', '0x300000000', '--output', str(output)]
            env = dict(os.environ, DALI_XNU_UFS_READ_ONLY='false')
            subprocess.run(command, env=env, capture_output=True, check=True)
            self.assertNotIn(b'dali-mali\0', output.read_bytes())
            subprocess.run(command + ['--mali-firmware', str(image)], env=env, capture_output=True, check=True)
            self.assertIn(b'dali-mali\0', output.read_bytes())
            self.assertIn(resource_contract(adt.Fdt(raw), raw, firmware()), output.read_bytes())


if __name__ == '__main__':
    unittest.main()
