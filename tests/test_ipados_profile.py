from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_MEMBER_HASHES = {
    "kernelcache.release.ipad7": "13b97fa2c5ce97f04408dcc28c41fb6b0d60b9440065197879acf44b2572bf7b",
    "044-76040-038.dmg": "3f99b859c9728379110b66657c7b15285bcb1925ad8b5726542e2cdcf8b1028d",
    "044-76034-038.dmg": "b4908cde41d23e6bc05e6af51a2c9eeb00cf0f863a6a18b1600c96a3fd70414f",
    "044-76040-038.dmg.trustcache": "4563bdbf6ed8f72237d45dbba3ccde432634338f95c10d79d5d4631678a97c7c",
    "044-76034-038.dmg.trustcache": "9742f3d566369e6c48f63b3dc0740b06c243d13c6650e25bbf44616e13d6d097",
}
TRUST_BUNDLE = "7416ca4f8377146626bb284e2133c76bee6cb4e3ee0af5d648b186235a7323a7"


class IpadosProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT / "board.toml").open("rb") as source:
            cls.board = tomllib.load(source)

    def test_donor_selector_is_pinned_without_changing_dali_identity(self):
        board, ipsw = self.board["board"], self.board["ipsw"]
        self.assertEqual((board["name"], board["soc"]), ("dali", "MT6991"))
        self.assertEqual((ipsw["product"], ipsw["device_class"], ipsw["version"], ipsw["build"]),
                         ("iPad7,1", "j120ap", "17.7.11", "21H461"))
        self.assertEqual((ipsw["chip_id"], ipsw["board_id"]), (0x8011, 12))
        self.assertEqual(ipsw["sha256"], EXPECTED_MEMBER_HASHES)
        self.assertEqual(self.board["payload"]["trust_cache_sha256"], [TRUST_BUNDLE])
        self.assertEqual(self.board["payload"]["skip_setup_tool"], "scripts/seed-data-skip-setup.py")
        self.assertEqual(self.board["payload"]["skip_setup_args"], ["--no-auto-dim"])
        self.assertEqual(self.board["payload"]["adt"]["tool"], "scripts/make-dali-adt.py")
        self.assertTrue(self.board["payload"]["adt"]["packed_data_ramdisk"])

    def test_unixnu_data_geometry_and_windows_are_explicit(self):
        spec = self.board["userdata"]
        self.assertEqual(spec["partition_name"], "unixnu_data")
        self.assertEqual(spec["lun"], 2)
        self.assertEqual((spec["first_lba"], spec["last_lba"], spec["lba_bytes"]),
                         (7_023_103, 15_403_518, 4096))
        self.assertEqual((spec["last_lba"] - spec["first_lba"] + 1) * spec["lba_bytes"],
                         34_326_183_936)
        self.assertEqual(spec["windows"], [
            {"name": "OS", "component": "OS", "volume_uuid": "04769D26-2CA0-4825-B443-E2E5F88300F2"},
            {"name": "SystemOS", "component": "Cryptex1,SystemOS", "volume_uuid": "8846FE78-2910-4297-AF97-D59BB6F8FC16"},
        ])
        # Read-only Android sysfs maps /dev/block/sdc to target 0:0:0:2; the XNU root reader is still unimplemented.
        self.assertEqual(spec["lun"], 2)

    def test_root_volume_uuids_match_the_exact_21h461_apfs_components(self):
        windows = {entry["name"]: entry["volume_uuid"] for entry in self.board["userdata"]["windows"]}
        self.assertEqual(windows["OS"], self.board["payload"]["adt"]["root_match_volume_uuid"])
        self.assertEqual(windows["SystemOS"], "8846FE78-2910-4297-AF97-D59BB6F8FC16")

    def test_unqualified_prelink_and_runtime_root_values_remain_unset(self):
        self.assertNotIn("prelink", self.board["kernel"])
        adt = self.board["payload"]["adt"]
        self.assertEqual(adt["root_match_volume_uuid"], "04769D26-2CA0-4825-B443-E2E5F88300F2")
        for key in ("unique_chip_id", "ipad_identity", "chip_id"):
            self.assertNotIn(key, adt)
        config = (ROOT / "configs/dali_ipados_defconfig").read_text()
        self.assertIn("CONFIG_XNU_SOURCE_MOCKFS=y", config)
        self.assertIn("CONFIG_XNU_SOURCE_STORAGE_FAMILY=y", config)
        self.assertNotIn("CONFIG_XNU_NATIVE_UFS=y", config)
        self.assertNotIn("CONFIG_XNU_TRUST_CACHE_RUNTIME=y", config)


if __name__ == "__main__":
    unittest.main()
