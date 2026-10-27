"""Execute the XNU USB owner/host/child stack against a RAM-backed BOT disk model."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GENERIC = ROOT.parent / "UniXNU"
CORE = GENERIC / "drivers/UsbCore"
MODEL = ROOT / "tests/usb_xhci_resources_model"


class DaliUsbXhciIntegrationTests(unittest.TestCase):
    def test_production_owner_dma_host_hid_storage_and_fat32_read_path(self):
        compiler = shutil.which(os.environ.get("CC", "clang"))
        self.assertIsNotNone(compiler, "C compiler is required")
        cpp = shutil.which("g++" if Path(compiler).name.startswith("gcc") else "clang++")
        self.assertIsNotNone(cpp, "C++ compiler is required")
        self.assertTrue((CORE / "UsbHost.c").is_file(), "pinned generic UniXNU source is required")

        sanitizer = ["-O1", "-g", "-Wall", "-Wextra", "-Werror", "-Wconversion",
                     "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-pthread"]
        c_includes = ["-I", str(CORE / "include")]
        cxx_includes = [
            "-I", str(MODEL),
            "-I", str(GENERIC / "tests/usb"),
            "-I", str(ROOT / "drivers/DaliUsbXhciResources"),
            "-I", str(ROOT / "drivers/DaliUsbXhciOwner"),
            "-I", str(ROOT / "drivers/DaliUsbXhciDma"),
            "-I", str(ROOT / "drivers/DaliUsbHid"),
            "-I", str(ROOT / "drivers/DaliUsbStorage"),
            "-I", str(CORE / "include"),
        ]

        with tempfile.TemporaryDirectory(prefix="dali-usb-xhci-integration-") as temporary:
            out = Path(temporary)
            objects = []
            for name in ("UsbCore", "UsbXhci", "UsbXhciController", "UsbXhciHost",
                         "UsbHost", "UsbStorage"):
                obj = out / f"{name}.o"
                result = subprocess.run(
                    [compiler, "-std=c11", *sanitizer, *c_includes, "-c",
                     str(CORE / f"{name}.c"), "-o", str(obj)],
                    text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                objects.append(str(obj))

            model_object = out / "xhci-controller-model.o"
            result = subprocess.run(
                [compiler, "-std=c11", *sanitizer, "-DUSB_XHCI_TEST_MODEL_ONLY",
                 *c_includes, "-c", str(GENERIC / "tests/usb/usb_xhci_controller_test.c"),
                 "-o", str(model_object)],
                text=True, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            objects.append(str(model_object))

            binary = out / "integration"
            sources = [
                ROOT / "drivers/DaliUsbXhciResources/DaliUsbXhciResources.cpp",
                ROOT / "drivers/DaliUsbXhciOwner/DaliUsbXhciOwner.cpp",
                ROOT / "drivers/DaliUsbXhciDma/DaliUsbXhciDma.cpp",
                ROOT / "drivers/DaliUsbHid/DaliUsbHid.cpp",
                ROOT / "drivers/DaliUsbStorage/DaliUsbStorage.cpp",
                MODEL / "system_lifecycle.cpp",
            ]
            result = subprocess.run(
                [cpp, "-std=c++17", *sanitizer, *cxx_includes,
                 *map(str, sources), *objects, "-o", str(binary)],
                text=True, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)

            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("production xHCI controller/host adapter with synthetic MMIO, "
                          "Dali DMA owner, HID/BOT children, and RAM FAT32-image read-only sector path",
                          result.stdout)
            self.assertIn("msdosfs runtime and hardware qualification unverified",
                          result.stdout)


if __name__ == "__main__":
    unittest.main()
