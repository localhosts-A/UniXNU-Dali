import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dali_ipados_trust_cache", ROOT / "scripts/dali_ipados_trust_cache.py")
TRUST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRUST)


def der(tag, value):
    size = len(value)
    if size < 0x80:
        length = bytes([size])
    else:
        encoded = size.to_bytes((size.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(encoded)]) + encoded
    return bytes([tag]) + length + value


def module(hashes, hash_type=2):
    return (struct.pack("<I16sI", 2, bytes(range(16)), len(hashes)) +
            b"".join(value + bytes([hash_type, 0, 0, 0]) for value in hashes))


def im4p(component_type, body):
    fields = b"".join((der(0x16, b"IM4P"), der(0x16, component_type),
                       der(0x16, b"1"), der(0x04, body)))
    return der(0x30, fields)


class DaliIpadosTrustCacheTests(unittest.TestCase):
    def make_inputs(self, root, static_module, system_module):
        static_path = root / "static.trustcache"
        system_path = root / "system.trustcache"
        static_path.write_bytes(im4p(b"trst", static_module))
        system_path.write_bytes(im4p(b"trcs", system_module))
        for path, component in ((static_path, "StaticTrustCache"),
                                (system_path, "Cryptex1,SystemTrustCache")):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            Path(str(path) + ".json").write_text(json.dumps({
                "component": component,
                "member": f"Firmware/{path.name}",
                "size": path.stat().st_size,
                "sha256": digest,
                "source": "remote",
            }))
        bundle = struct.pack("<3I", 2, 12, 12 + len(static_module)) + static_module + system_module
        board = {
            "ipsw": {"sha256": {
                static_path.name: hashlib.sha256(static_path.read_bytes()).hexdigest(),
                system_path.name: hashlib.sha256(system_path.read_bytes()).hexdigest(),
            }},
            "payload": {"trust_cache_sha256": [hashlib.sha256(bundle).hexdigest()]},
        }
        return static_path, system_path, board, bundle

    def test_builds_reproducible_two_module_bundle_and_provenance_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static_path, system_path, board, expected = self.make_inputs(
                root, module([bytes([0x10]) * 20, bytes([0x20]) * 20]),
                module([bytes([0x30]) * 20]))
            bundle_path, report_path, report = TRUST.build_bundle(
                static_path, system_path, board, root / "out")

            self.assertEqual(bundle_path.read_bytes(), expected)
            self.assertEqual(hashlib.sha256(expected).hexdigest(), report["bundle_sha256"])
            self.assertEqual(report_path.read_text(), json.dumps(report, indent=2, sort_keys=True) + "\n")
            self.assertEqual([item["entries"] for item in report["modules"]], [2, 1])
            self.assertFalse(report["apple_trust_chain_verified"])
            self.assertFalse(report["trust_cache_runtime_consumed"])
            self.assertEqual(len(TRUST.parse_bundle(expected)), 2)

    def test_rejects_component_hash_or_fetch_record_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static_path, system_path, board, _ = self.make_inputs(
                root, module([bytes([0x10]) * 20]), module([bytes([0x20]) * 20]))
            board["ipsw"]["sha256"][static_path.name] = "0" * 64
            with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
                TRUST.build_bundle(static_path, system_path, board, root / "out")

            board["ipsw"]["sha256"][static_path.name] = hashlib.sha256(static_path.read_bytes()).hexdigest()
            record_path = Path(str(system_path) + ".json")
            record = json.loads(record_path.read_text())
            record["component"] = "StaticTrustCache"
            record_path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "provenance record"):
                TRUST.build_bundle(static_path, system_path, board, root / "out")

            record_path.write_text("[]\n")
            with self.assertRaisesRegex(ValueError, "provenance record is not an object"):
                TRUST.build_bundle(static_path, system_path, board, root / "out")

    def test_rejects_wrong_im4p_type_bad_module_and_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static_path, system_path, board, _ = self.make_inputs(
                root, module([bytes([0x10]) * 20]), module([bytes([0x20]) * 20]))
            static_path.write_bytes(im4p(b"trcs", module([bytes([0x10]) * 20])))
            board["ipsw"]["sha256"][static_path.name] = hashlib.sha256(static_path.read_bytes()).hexdigest()
            record_path = Path(str(static_path) + ".json")
            record = json.loads(record_path.read_text())
            record["sha256"] = hashlib.sha256(static_path.read_bytes()).hexdigest()
            record["size"] = static_path.stat().st_size
            record_path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "expected 21H461 IM4P trust-cache type"):
                TRUST.build_bundle(static_path, system_path, board, root / "out")

            invalid = struct.pack("<I16sI", 2, bytes(range(16)), 1) + bytes([0x10]) * 20 + bytes([0, 0, 0, 0])
            with self.assertRaisesRegex(ValueError, "supported types"):
                TRUST.module_info(invalid)

            second = root / "second"
            second.mkdir()
            static2, system2, valid_board, _ = self.make_inputs(
                second, module([bytes([0x10]) * 20]), module([bytes([0x20]) * 20]))
            output = root / "existing"
            output.mkdir()
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                TRUST.build_bundle(static2, system2, valid_board, output)


if __name__ == "__main__":
    unittest.main()
