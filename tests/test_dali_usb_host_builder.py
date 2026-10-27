import importlib.util
import io
from contextlib import redirect_stdout
import json
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

    def test_kernel_symbol_check_requires_each_resolved_host_implementation(self):
        defined = "\n".join("00000000 T _" + name for name in BUILDER.REQUIRED_SYMBOLS)
        self.assertEqual(BUILDER.check_kernel_symbols(defined, ""), list(BUILDER.REQUIRED_SYMBOLS))
        missing = BUILDER.REQUIRED_SYMBOLS[0]
        with self.assertRaisesRegex(ValueError, "missing=" + missing):
            BUILDER.check_kernel_symbols("\n".join(
                "00000000 T _" + name for name in BUILDER.REQUIRED_SYMBOLS if name != missing), "")
        with self.assertRaisesRegex(ValueError, "unresolved=" + missing):
            BUILDER.check_kernel_symbols(defined, "                 U _" + missing)

    def test_kernel_verifier_writes_a_hash_bound_link_report(self):
        with tempfile.TemporaryDirectory(prefix="dali-usb-host-link-") as temporary:
            root = Path(temporary)
            out = root / "out"
            out.mkdir()
            archive = out / "libDaliUsbHost.a"
            archive.write_bytes(b"fixed host archive")
            kernel = root / "kernel.macho"
            kernel.write_bytes(b"linked kernel")
            (out / "build-report.json").write_text(json.dumps({
                "archive_sha256": BUILDER.digest(archive),
                "required_symbols": list(BUILDER.REQUIRED_SYMBOLS),
                "source_archive_built": True,
                "kernel_link_verified": False,
                "host_runtime_qualified": False,
                "hardware_qualified": False,
            }))
            defined = "\n".join("00000000 T _" + name for name in BUILDER.REQUIRED_SYMBOLS)
            with patch.object(BUILDER, "OUT", out), patch.object(BUILDER, "ARCHIVE", archive), \
                    patch.object(BUILDER.subprocess, "check_output", side_effect=[defined, ""]):
                BUILDER.verify_kernel(kernel)
            report = json.loads((out / "kernel-link-report.json").read_text())
            self.assertTrue(report["kernel_link_verified"])
            self.assertEqual(report["kernel_sha256"], BUILDER.digest(kernel))
            self.assertEqual(report["kernel_symbol_checks"], list(BUILDER.REQUIRED_SYMBOLS))
            self.assertFalse(report["host_runtime_qualified"])

    def test_default_profile_builder_skips_without_toolchain(self):
        output = io.StringIO()
        with patch.dict(BUILDER.os.environ, {"XNU_NATIVE_USB_HOST": "0"}), \
             patch.object(BUILDER, "build", side_effect=AssertionError("disabled builder must not run")), \
             redirect_stdout(output):
            BUILDER.main([])
        self.assertEqual(output.getvalue().strip(), "SKIP: source-only USB host/storage core disabled")

    def test_enabled_profile_dispatches_kernel_verification(self):
        kernel = Path("/tmp/dali-usb-kernel")
        with patch.dict(BUILDER.os.environ, {"XNU_NATIVE_USB_HOST": "1"}), \
                patch.object(BUILDER, "verify_kernel") as verify:
            BUILDER.main(["--verify-kernel", str(kernel)])
        verify.assert_called_once_with(kernel)

    def test_usb_core_profile_is_opt_in_and_runs_its_builder(self):
        default = (ROOT / "configs/dali_defconfig").read_text()
        usb = (ROOT / "configs/dali_usb_defconfig").read_text()
        board = tomllib.loads((ROOT / "board.toml").read_text())
        self.assertNotIn("CONFIG_XNU_NATIVE_USB_HOST=", default)
        self.assertIn("CONFIG_XNU_NATIVE_USB_HOST=y", usb)
        self.assertIn("build/dali-usb-host/libDaliUsbHost.a", usb)
        self.assertIn("build-dali-usb-host.py", board["kernel"]["archives"])
        self.assertEqual([source.name for source in BUILDER.SOURCES],
                         ["UsbCore.c", "UsbHost.c", "UsbStorage.c", "UsbXhci.c",
                          "UsbXhciController.c"])
        workflow = (ROOT / ".github/workflows/dali-usb-xnu.yml").read_text()
        self.assertIn('build-dali-usb-host.py" --verify-kernel "$kernel"', workflow)
        self.assertIn("'tests/test_dali_usb_host_builder.py'", workflow)
        self.assertTrue({"usb_host_hub_start", "usb_host_hub_poll",
                         "usb_host_hub_child_route", "usb_host_hub_stop",
                         "usb_host_stop_device", "usb_host_release_stopped_device",
                        "usb_bot_read_only", "usb_block_io_init", "usb_block_io_transfer",
                         "usb_block_io_sync", "usb_block_io_quiesce",
                         "usb_block_io_detach", "usb_xhci_read_capabilities",
                         "usb_xhci_transfer_ring_init", "usb_xhci_transfer_ring_enqueue",
                         "usb_xhci_transfer_ring_complete_event", "usb_xhci_event_decode",
                         "usb_xhci_event_ring_init", "usb_xhci_event_ring_read",
                         "usb_xhci_controller_start", "usb_xhci_controller_stop"} <=
                        set(BUILDER.REQUIRED_SYMBOLS))


if __name__ == "__main__":
    unittest.main()
