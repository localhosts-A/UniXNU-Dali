"""Build contracts for the opt-in source-XNU MTU3 gadget core."""
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_dali_usb_gadget", ROOT / "scripts/build-dali-usb-gadget.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class DaliUsbGadgetBuilderTests(unittest.TestCase):
    def test_core_and_gadget_archive_are_opt_in_and_registered(self):
        default = (ROOT / "configs/dali_defconfig").read_text()
        profile = (ROOT / "configs/dali_usb_defconfig").read_text()
        board = tomllib.loads((ROOT / "board.toml").read_text())
        archive = "build/dali-usb-gadget/libDaliUsbGadget.a"
        self.assertNotIn("libDaliUsbGadget.a", default)
        self.assertIn("CONFIG_XNU_NATIVE_USB_HOST=y", profile)
        self.assertIn("-Wl,-force_load,{tree}/" + archive, profile)
        self.assertIn("{tree}/" + archive, profile)
        self.assertIn("build-dali-usb-gadget.py", board["kernel"]["archives"])
        self.assertEqual([source.name for source in BUILDER.SOURCES], [
            "UsbMtu3Qmu.c", "UsbMtu3Ep0.c", "UsbMtu3Endpoints.c", "UsbMtu3Device.c"])
        self.assertIn("usb_mtu3_device_log", BUILDER.REQUIRED_SYMBOLS)
        self.assertTrue({"usb_qmu_init", "usb_mtu3_ep0_start", "usb_mtu3_endpoints_configure",
                         "usb_mtu3_device_start", "usb_mtu3_device_stop"} <=
                        set(BUILDER.REQUIRED_SYMBOLS))
        workflow = (ROOT / ".github/workflows/dali-usb-xnu.yml").read_text()
        self.assertIn('build-dali-usb-gadget.py" --verify-kernel "$kernel"', workflow)
        self.assertIn("build/dali-usb-gadget/*.a", workflow)

    def test_kernel_symbol_check_requires_each_gadget_unit(self):
        defined = "\n".join("00000000 T _" + name for name in BUILDER.REQUIRED_SYMBOLS)
        self.assertEqual(BUILDER.check_kernel_symbols(defined, ""), list(BUILDER.REQUIRED_SYMBOLS))
        missing = "usb_mtu3_device_log"
        with self.assertRaisesRegex(ValueError, "missing=" + missing):
            BUILDER.check_kernel_symbols("\n".join(
                "00000000 T _" + name for name in BUILDER.REQUIRED_SYMBOLS if name != missing), "")
        with self.assertRaisesRegex(ValueError, "unresolved=" + missing):
            BUILDER.check_kernel_symbols(defined, "                 U _" + missing)

    def test_disabled_profile_skips_without_apple_tools(self):
        output = io.StringIO()
        with patch.dict(BUILDER.os.environ, {"XNU_NATIVE_USB_HOST": "0"}), \
                patch.object(BUILDER, "build", side_effect=AssertionError("disabled builder must not run")), \
                redirect_stdout(output):
            BUILDER.main([])
        self.assertEqual(output.getvalue().strip(), "SKIP: source-only USB profile disabled")

    def test_enabled_profile_dispatches_kernel_verification(self):
        kernel = Path("/tmp/dali-usb-kernel")
        with patch.dict(BUILDER.os.environ, {"XNU_NATIVE_USB_HOST": "1"}), \
                patch.object(BUILDER, "verify_kernel") as verify:
            BUILDER.main(["--verify-kernel", str(kernel)])
        verify.assert_called_once_with(kernel)

    def test_kernel_link_report_requires_distinct_gadget_symbols(self):
        self.assertEqual(len(BUILDER.SOURCES), 4)
        self.assertEqual(len(set(BUILDER.REQUIRED_SYMBOLS)), len(BUILDER.REQUIRED_SYMBOLS))


if __name__ == "__main__":
    unittest.main()
