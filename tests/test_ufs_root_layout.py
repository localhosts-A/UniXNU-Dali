from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GENERIC = ROOT.parent / "UniXNU"


class UfsRootLayoutTests(unittest.TestCase):
    def test_production_parser_with_synthetic_dali_gpt(self):
        compiler = shutil.which("clang++") or shutil.which("c++")
        if not compiler:
            self.skipTest("native C++ compiler required")
        openssl = Path("/usr")
        brew = shutil.which("brew")
        if brew:
            result = subprocess.run([brew, "--prefix", "openssl@3"], capture_output=True,
                                    text=True, timeout=20)
            if result.returncode == 0:
                openssl = Path(result.stdout.strip())
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "dali-root-layout"
            subprocess.run([
                compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-fsanitize=address,undefined", "-g",
                str(ROOT / "tests/ufs_root_layout_harness.cpp"),
                "-I", str(GENERIC / "drivers/NativeUfs"),
                "-I", str(openssl / "include"),
                "-L", str(openssl / "lib"), "-lcrypto", "-o", str(binary),
            ], check=True, timeout=60)
            result = subprocess.run([str(binary)], check=True, capture_output=True,
                                    text=True, timeout=20)
            self.assertIn("PASS: Dali LUN 2 unixnu_data GPT and SHNGROOT parser", result.stdout)


if __name__ == "__main__":
    unittest.main()
