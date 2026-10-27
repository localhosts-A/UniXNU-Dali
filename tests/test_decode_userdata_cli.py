import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "decode-userdata-logs.py"
PARTITION_BYTES = 32 * 1024 * 1024


class DecodeCliTests(unittest.TestCase):
    def test_empty_partition_export_is_read_only_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image, output = root / "logs.img", root / "export"
            with image.open("wb") as stream:
                stream.truncate(PARTITION_BYTES)
            result = subprocess.run([sys.executable, str(SCRIPT), str(image), str(output)],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["records"], 0)
            self.assertEqual((output / "unixnu_logs.img").stat().st_size, PARTITION_BYTES)
            self.assertEqual(image.stat().st_size, PARTITION_BYTES)
            again = subprocess.run([sys.executable, str(SCRIPT), str(image), str(output)],
                                   capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)

    def test_truncated_capture_is_rejected_before_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image, output = root / "short.img", root / "export"
            image.write_bytes(b"incomplete")
            result = subprocess.run([sys.executable, str(SCRIPT), str(image), str(output)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
