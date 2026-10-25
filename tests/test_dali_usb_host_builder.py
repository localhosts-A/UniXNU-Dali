import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_dali_usb_host", ROOT / "scripts/build-dali-usb-host.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class DaliUsbHostBuilderTests(unittest.TestCase):
    def test_replaces_cpo_inputs_without_reusing_output_or_dependencies(self):
        with tempfile.TemporaryDirectory(prefix="dali-usb-builder-") as temporary:
            root = Path(temporary)
            original = root / "original.c"
            replacement = root / "replacement.c"
            output = root / "usb-host.o"
            include = root / "include"
            original.touch()
            replacement.touch()
            record_path = root / "unit.o.json"
            record = {
                "file": str(original),
                "directory": str(root),
                "arguments": ["clang", "-DABI_TEST=1", "-MMD", "-MT", "old.o",
                              "-MF", "old.d", "-o", "old.o", "-c", str(original)],
            }
            command, cwd = BUILDER.compile_command(record_path, record, replacement, output, include)
            self.assertEqual(cwd, root.resolve())
            self.assertEqual(command[:4], ["xcrun", "--sdk", "iphoneos", "clang"])
            self.assertIn(str(replacement.resolve()), command)
            self.assertNotIn(str(original.resolve()), command)
            self.assertNotIn("old.o", command)
            self.assertNotIn("old.d", command)
            self.assertIn("-I", command)
            self.assertIn(str(include.resolve()), command)
            self.assertEqual(command[-2:], ["-o", str(output.resolve())])

    def test_requires_exactly_one_record_source(self):
        with tempfile.TemporaryDirectory(prefix="dali-usb-builder-") as temporary:
            root = Path(temporary)
            original = root / "original.c"
            replacement = root / "replacement.c"
            original.touch()
            replacement.touch()
            record = {"file": str(original), "directory": str(root),
                      "arguments": ["clang", "-c", str(root / "different.c")]}
            with self.assertRaisesRegex(ValueError, "exactly one original C source"):
                BUILDER.compile_command(root / "record.o.json", record, replacement,
                                        root / "out.o", root / "include")

    def test_default_profile_builder_skips_without_toolchain(self):
        output = io.StringIO()
        with patch.dict(BUILDER.os.environ, {"XNU_NATIVE_USB_HOST": "0"}), \
             patch.object(BUILDER, "build", side_effect=AssertionError("disabled builder must not run")), \
             redirect_stdout(output):
            BUILDER.main()
        self.assertEqual(output.getvalue().strip(), "SKIP: source-only USB host/storage core disabled")

    def test_usb_core_profile_is_opt_in_and_runs_its_builder(self):
        default = (ROOT / "configs/dali_defconfig").read_text()
        usb = (ROOT / "configs/dali_usb_defconfig").read_text()
        board = tomllib.loads((ROOT / "board.toml").read_text())
        self.assertNotIn("CONFIG_XNU_NATIVE_USB_HOST=", default)
        self.assertIn("CONFIG_XNU_NATIVE_USB_HOST=y", usb)
        self.assertIn("build/dali-usb-host/libDaliUsbHost.a", usb)
        self.assertIn("build-dali-usb-host.py", board["kernel"]["archives"])
        self.assertEqual([source.name for source in BUILDER.SOURCES],
                         ["UsbCore.c", "UsbHost.c", "UsbStorage.c"])
        self.assertTrue({"usb_host_hub_start", "usb_host_hub_poll",
                         "usb_host_hub_child_route", "usb_host_hub_stop",
                        "usb_bot_read_only", "usb_block_io_init", "usb_block_io_transfer",
                         "usb_block_io_sync", "usb_block_io_quiesce",
                         "usb_block_io_detach"} <=
                        set(BUILDER.REQUIRED_SYMBOLS))


if __name__ == "__main__":
    unittest.main()
