import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import struct
import tempfile
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_dali_usb_storage", ROOT / "scripts/build-dali-usb-storage.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class DaliUsbStorageBuilderTests(unittest.TestCase):
    def test_replaces_exact_cpp_compile_record_and_drops_output_side_effects(self):
        with tempfile.TemporaryDirectory(prefix="dali-usb-storage-builder-") as temporary:
            root = Path(temporary)
            original, replacement = root / "original.cpp", root / "replacement.cpp"
            original.touch()
            replacement.touch()
            record_path = root / "IOService.cpo.json"
            record = {
                "file": str(original),
                "directory": str(root),
                "arguments": ["clang++", "-DKERNEL=1", "-MD", "-MT", "old.o",
                              "-MF", "old.d", "-o", "old.o", "-c", str(original)],
            }
            command, cwd = BUILDER.compile_command(
                record_path, record, replacement, root / "new.o", [root / "pinned-storage"])
            self.assertEqual(cwd, root.resolve())
            self.assertEqual(command[:4], ["xcrun", "--sdk", "iphoneos", "clang++"])
            self.assertIn(str(replacement), command)
            self.assertNotIn(str(original), command)
            self.assertNotIn("old.o", command)
            self.assertNotIn("old.d", command)
            self.assertIn("-iquote", command)
            self.assertIn(str((root / "pinned-storage").resolve()), command)
            self.assertEqual(command[-2:], ["-o", str((root / "new.o").resolve())])

    def test_requires_one_cpp_source_in_compile_record(self):
        with tempfile.TemporaryDirectory(prefix="dali-usb-storage-record-") as temporary:
            root = Path(temporary)
            original, replacement = root / "original.cpp", root / "replacement.cpp"
            original.touch()
            replacement.touch()
            record = {"file": str(original), "directory": str(root),
                      "arguments": ["clang++", "-c", str(root / "other.cpp")]}
            with self.assertRaisesRegex(ValueError, "exactly one original C\\+\\+ source"):
                BUILDER.compile_command(root / "record.cpo.json", record, replacement,
                                        root / "out.o", [root / "headers"])

    def test_validates_arm64_iOS17_object(self):
        with tempfile.TemporaryDirectory(prefix="dali-usb-storage-object-") as temporary:
            path = Path(temporary) / "service.o"
            version = struct.pack("<6I", 0x32, 24, 2, 0x110000, 0x110000, 0)
            header = struct.pack("<8I", 0xfeedfacf, 0x100000c, 0, 1, 1, 24, 0, 0)
            path.write_bytes(header + version)
            BUILDER.validate_object(path)
            bad = struct.pack("<6I", 0x32, 24, 2, 0x120000, 0x120000, 0)
            path.write_bytes(header + bad)
            with self.assertRaisesRegex(ValueError, "iOS 17 ABI"):
                BUILDER.validate_object(path)

    def test_symbol_check_accepts_mangled_iokit_metaclass_and_exact_c_exports(self):
        nm = "0000000000000000 S __ZN14DaliUsbStorage10gMetaClassE\n0000000000000000 T _dali_usb_storage_publish\n"
        self.assertEqual(BUILDER.missing_symbols(nm, BUILDER.REQUIRED_SYMBOLS[0:2]), [])
        self.assertEqual(BUILDER.missing_symbols(nm, ["dali_usb_storage_detach"]),
                         ["dali_usb_storage_detach"])

    def test_default_off_profile_skips_without_xcrun(self):
        output = io.StringIO()
        with patch.dict(BUILDER.os.environ, {"XNU_NATIVE_USB_HOST": "0"}), \
             patch.object(BUILDER, "build", side_effect=AssertionError("disabled builder must not run")), \
             redirect_stdout(output):
            BUILDER.main([])
        self.assertEqual(output.getvalue().strip(), "SKIP: source-only USB BOT block service disabled")

    def test_enabled_profile_uses_the_source_builder(self):
        with patch.dict(BUILDER.os.environ, {"XNU_NATIVE_USB_HOST": "1"}), \
             patch.object(BUILDER, "build") as build:
            BUILDER.main([])
        build.assert_called_once_with()

    def test_profile_force_loads_the_archive_and_runs_its_builder(self):
        default = (ROOT / "configs/dali_defconfig").read_text()
        usb = (ROOT / "configs/dali_usb_defconfig").read_text()
        board = tomllib.loads((ROOT / "board.toml").read_text())
        self.assertNotIn("CONFIG_XNU_NATIVE_USB_HOST=", default)
        self.assertIn("CONFIG_XNU_NATIVE_USB_HOST=y", usb)
        self.assertIn("-force_load,{tree}/build/dali-usb-storage/libDaliUsbStorage.a", usb)
        self.assertIn("{tree}/build/dali-usb-storage/libDaliUsbStorage.a", usb)
        self.assertIn("build-dali-usb-storage.py", board["kernel"]["archives"])
        self.assertTrue({"dali_usb_storage_publish", "dali_usb_storage_quiesce",
                         "dali_usb_storage_detach", "dali_usb_storage_terminate"} <=
                        set(BUILDER.REQUIRED_SYMBOLS))


if __name__ == "__main__":
    unittest.main()
