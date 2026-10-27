"""Build contracts and production-code tests for the XNU xhci0 resource consumer."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CORE_INCLUDE = ROOT / "UniXNU/drivers/UsbCore/include"
if not CORE_INCLUDE.is_dir():
    CORE_INCLUDE = ROOT.parent / "UniXNU/drivers/UsbCore/include"
SPEC = importlib.util.spec_from_file_location(
    "dali_usb_xhci_resources_builder",
    ROOT / "scripts/build-dali-usb-xhci-resources.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class DaliUsbXhciResourcesBuilderTests(unittest.TestCase):
    def test_profile_force_loads_the_resource_consumer_only_when_opted_in(self):
        default = (ROOT / "configs/dali_defconfig").read_text()
        profile = (ROOT / "configs/dali_usb_defconfig").read_text()
        board = tomllib.loads((ROOT / "board.toml").read_text())
        archive = "{tree}/build/dali-usb-xhci-resources/libDaliUsbXhciResources.a"
        self.assertNotIn("libDaliUsbXhciResources.a", default)
        self.assertIn("CONFIG_XNU_NATIVE_USB_HOST=y", profile)
        self.assertIn("-Wl,-force_load," + archive, profile)
        self.assertIn(archive, profile)
        self.assertIn("build-dali-usb-xhci-resources.py", board["kernel"]["archives"])
        self.assertTrue((ROOT / "drivers/DaliUsbXhciOwner/DaliUsbXhciOwner.cpp").is_file())
        self.assertIn("dali-usb-xhci-resources-personality.patch",
                      [path.name for path in (ROOT / "patches/xnu").glob("*.patch")])

    def test_disabled_profile_skips_without_apple_tools(self):
        with patch.dict(os.environ, {"XNU_NATIVE_USB_HOST": "0"}), \
                patch.object(
                    BUILDER, "build", side_effect=AssertionError("disabled builder must not run")):
            BUILDER.main([])

    def test_builder_finds_usb_core_outside_the_source_only_tree(self):
        with tempfile.TemporaryDirectory(prefix="dali-usb-xhci-source-tree-") as temporary:
            checkout = Path(temporary) / "UniXNU"
            core_include = checkout / "drivers/UsbCore/include"
            core_include.mkdir(parents=True)
            source_tree = checkout / "out/dali/tree"
            source_tree.mkdir(parents=True)
            with patch.dict(os.environ, {"UNIXNU_OUT": str(checkout / "out")}):
                self.assertEqual(
                    BUILDER.generic_usb_core_include(source_tree), core_include)

    def test_builder_uses_kernel_cpp_compile_record_for_all_xhci_units(self):
        helper = BUILDER.helpers()
        with tempfile.TemporaryDirectory(prefix="dali-usb-xhci-compile-record-") as temporary:
            root = Path(temporary)
            original, replacement = root / "IOService.cpp", root / "DaliUsbXhciResources.cpp"
            original.touch()
            replacement.touch()
            record = {
                "file": str(original),
                "directory": str(root),
                "arguments": ["clang++", "-DKERNEL=1", "-MD", "-MT", "old.o",
                              "-MF", "old.d", "-o", "old.o", "-c", str(original)],
            }
            for source in BUILDER.SOURCES:
                command = helper.compile_command(
                    record, source, root / (source.stem + ".o"),
                    [root / "iokit", root / "driver-headers"])
                self.assertEqual(command[0:4], ["xcrun", "--sdk", "iphoneos", "clang++"])
                self.assertIn(str(source.resolve()), command)
                self.assertNotIn(str(original.resolve()), command)
                self.assertNotIn("old.o", command)
                self.assertNotIn("old.d", command)
                self.assertIn(str((root / "iokit").resolve()), command)
                output_option = command.index("-o")
                self.assertEqual(command[output_option + 1], str((root / (source.stem + ".o")).resolve()))

    def test_kernel_verifier_requires_the_service_and_description_api(self):
        symbols = (
            "00000000 S __ZN20DaliUsbXhciResources10gMetaClassE\n"
            "00000000 T __ZN20DaliUsbXhciResources5startEP9IOService\n"
            "00000000 T __ZNK20DaliUsbXhciResources24copyResourceDescriptionEP31dali_usb_xhci_resource_v1\n"
            "00000000 S __ZN16DaliUsbXhciOwner10gMetaClassE\n"
            "00000000 T __ZN16DaliUsbXhciOwner5startEP9IOService\n"
            "00000000 T __ZN16DaliUsbXhciOwner4stopEP9IOService\n"
            "00000000 T _dali_usb_xhci_dma_init\n"
            "00000000 T _dali_usb_xhci_dma_get_ops\n"
            "00000000 T _dali_usb_xhci_dma_stop\n"
        )
        BUILDER.require_symbols(symbols, BUILDER.SYMBOLS, "fixture")
        with self.assertRaisesRegex(ValueError, "DaliUsbXhciOwner"):
            BUILDER.require_symbols(symbols.replace("DaliUsbXhciOwner", "WrongOwner"),
                                    BUILDER.SYMBOLS, "fixture")

    def test_resource_decoder_and_iokit_service_execute_production_code(self):
        cc = shutil.which(os.environ.get("CC", "clang"))
        self.assertIsNotNone(cc)
        cpp = shutil.which("g++" if Path(cc).name == "gcc" else "clang++")
        self.assertIsNotNone(cpp)
        model = ROOT / "tests/usb_xhci_resources_model"
        with tempfile.TemporaryDirectory(prefix="dali-usb-xhci-resources-") as temporary:
            out = Path(temporary)
            parser = out / "resource-format"
            cflags = ["-O1", "-g", "-Wall", "-Wextra", "-Werror", "-Wconversion",
                      "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                      "-I", str(ROOT / "drivers/DaliUsbXhciResources")]
            built = subprocess.run(
                [cc, "-std=c11", *cflags,
                 str(ROOT / "tests/fixtures/usb_xhci_resource_v1_test.c"), "-o", str(parser)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(parser)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn("PASS: production Dali xHCI resources-v1 decoder", ran.stdout)

            service = out / "service-lifecycle"
            core_object = out / "usb-core.o"
            cxxflags = ["-O1", "-g", "-Wall", "-Wextra", "-Werror", "-Wconversion",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        "-I", str(model), "-I", str(ROOT / "drivers/DaliUsbXhciResources"),
                        "-I", str(ROOT / "drivers/DaliUsbXhciOwner"),
                        "-I", str(ROOT / "drivers/DaliUsbXhciDma"),
                        "-I", str(CORE_INCLUDE)]
            core_built = subprocess.run(
                [cc, "-std=c11", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-Wconversion",
                 "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                 "-I", str(CORE_INCLUDE), "-c", str(CORE_INCLUDE.parent / "UsbCore.c"),
                 "-o", str(core_object)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(core_built.returncode, 0, core_built.stderr)
            built = subprocess.run(
                [cpp, "-std=c++17", *cxxflags,
                 str(ROOT / "drivers/DaliUsbXhciResources/DaliUsbXhciResources.cpp"),
                 str(ROOT / "drivers/DaliUsbXhciOwner/DaliUsbXhciOwner.cpp"),
                 str(ROOT / "drivers/DaliUsbXhciDma/DaliUsbXhciDma.cpp"),
                 str(model / "lifecycle.cpp"), str(core_object), "-o", str(service)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(service)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn("PASS: production Dali xHCI MMIO mapping owner", ran.stdout)


if __name__ == "__main__":
    unittest.main()
