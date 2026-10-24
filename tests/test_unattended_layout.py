"""Safety refusals for the actual offline boot_b reservation planner."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/unattended_layout.py"
spec = importlib.util.spec_from_file_location("unattended_layout", SCRIPT)
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)


def boot_image(kernel_bytes=4096):
    image = bytearray(4096 + layout.aligned(kernel_bytes))
    image[:8] = b"ANDROID!"
    struct.pack_into("<II", image, 8, kernel_bytes, 0)
    struct.pack_into("<I", image, 20, 1584)
    struct.pack_into("<I", image, 40, 4)
    image[4096:4100] = layout.LEGACY_LZ4
    return image


class UnattendedLayoutTests(unittest.TestCase):
    def test_fitting_image_proposes_bytes_but_never_hardware_qualification(self):
        image = boot_image()
        result = layout.plan_scratch(image, 64 * layout.MIB)
        self.assertEqual(result["scratch"]["offset_bytes"], 62 * layout.MIB)
        self.assertEqual(result["scratch"]["bytes"], layout.MIB)
        self.assertEqual(result["candidate_sha256"], hashlib.sha256(image).hexdigest())
        self.assertIsNone(result["logical_block_bytes"])
        for key in ("reader_switch_qualified", "media_writes_qualified", "device_access_performed"):
            self.assertIs(result[key], False)

    def test_full_zero_padding_does_not_imply_fastboot_preservation(self):
        image = boot_image()
        image.extend(bytes(4 * layout.MIB - len(image)))
        self.assertFalse(layout.plan_scratch(image, len(image))["reader_switch_qualified"])

    def test_payload_collision_and_bad_partition_geometry_are_refused(self):
        image = boot_image()
        for capacity in (0, True, 4097, len(image) - 4096, 2 * layout.MIB):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                layout.plan_scratch(image, capacity)
        large = boot_image(2 * layout.MIB)
        with self.assertRaisesRegex(ValueError, "overlaps"):
            layout.plan_scratch(large, 4 * layout.MIB)

    def test_truncated_or_different_containers_are_refused(self):
        variants = [bytearray(), boot_image()[:4096], bytearray(b"sparse" + bytes(8192))]
        for offset, value in ((40, 3), (20, 1580), (8, 1 << 30), (8, 0), (12, 4096), (1580, 4096)):
            image = boot_image()
            struct.pack_into("<I", image, offset, value)
            variants.append(image)
        bad_lz4 = boot_image()
        bad_lz4[4096:4100] = b"gzip"
        variants.append(bad_lz4)
        for image in variants:
            with self.subTest(length=len(image), header=image[:44]), self.assertRaises(ValueError):
                layout.plan_scratch(image, 64 * layout.MIB)

    def test_unknown_padding_and_avb_coverage_are_not_free_space(self):
        variants = []
        image = boot_image()
        image[2000] = 1
        variants.append(image)
        image = boot_image(4100)
        image[-1] = 1
        variants.append(image)
        image = boot_image()
        image.extend(b"unexpected-metadata")
        variants.append(image)
        image = boot_image()
        image.extend(bytes(4 * layout.MIB - len(image)))
        image[-64:-60] = b"AVBf"
        variants.append(image)
        for image in variants:
            with self.subTest(length=len(image)), self.assertRaises(ValueError):
                layout.plan_scratch(image, 64 * layout.MIB)

    def test_backup_identity_and_hash_are_required(self):
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp)
            data = boot_image()
            (backup / "boot_b.img").write_bytes(data)
            manifest = {"complete": True, "product": "dali", "slot_suffix": "_b",
                        "files": {"boot_b.img": {"bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()}}}
            target = backup / "manifest.json"
            target.write_text(json.dumps(manifest))
            self.assertEqual(layout.verified_partition_size(backup)[0], len(data))
            for key, value in (("complete", False), ("product", "sheng"), ("slot_suffix", "_a")):
                bad = dict(manifest)
                bad[key] = value
                target.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    layout.verified_partition_size(backup)
            target.write_text(json.dumps(manifest))
            (backup / "boot_b.img").write_bytes(bytes(len(data)))
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                layout.verified_partition_size(backup)


if __name__ == "__main__":
    unittest.main()
