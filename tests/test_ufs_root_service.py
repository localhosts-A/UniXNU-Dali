from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
ROOT_SOURCE = ROOT / "drivers/DaliUfs/DaliUfsRoot.cpp"
ROOT_FALLBACK = ROOT / "drivers/DaliUfs/VerifiedRootUnavailable.cpp"
DIAGNOSTIC_SOURCE = ROOT / "drivers/DaliUfs/DaliUfs.cpp"
BUILDER = ROOT / "scripts/build-dali-xnu-platform.py"
POLICY_BUILDER = ROOT / "scripts/build-dali-verified-root.py"
PERSONALITY = ROOT / "patches/xnu/dali-bootlog-personality.patch"
WORKFLOW = ROOT / ".github/workflows/dali-xnu.yml"


class UfsRootServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT_SOURCE.read_text()
        cls.fallback = ROOT_FALLBACK.read_text()
        cls.diagnostic = DIAGNOSTIC_SOURCE.read_text()
        cls.builder = BUILDER.read_text()
        cls.policy_builder = POLICY_BUILDER.read_text()
        cls.personality = PERSONALITY.read_text()
        cls.workflow = WORKFLOW.read_text()
        with (ROOT / "board.toml").open("rb") as source:
            cls.board = tomllib.load(source)

    def test_root_personality_is_separate_and_requires_explicit_pid1_mode(self):
        start = self.root[self.root.index("bool DaliUfsRoot::start("):]
        self.assertIn('PE_parse_boot_argn("dali-ipados-root"', start)
        self.assertIn('PE_parse_boot_argn("dali-pid1"', start)
        self.assertIn('PE_parse_boot_argn("dali-earlyboot"', start)
        self.assertIn('PE_parse_boot_argn("dali-run"', start)
        self.assertIn('"dali-auto-fastboot"', start)
        self.assertIn('"dali-reset-method"', start)
        self.assertNotIn("dali-ufs-writable", start)
        self.assertIn("IOClass'         = DaliUfsRoot", self.personality)
        self.assertIn("IOMatchCategory' = DaliUfsRoot", self.personality)

    def test_owner_root_profile_is_separate_and_does_not_enable_virtio_or_mockfs(self):
        profile = (ROOT / "configs/dali_ipados_root_defconfig").read_text()
        self.assertIn("CONFIG_XNU_VERIFIED_ROOT_PROVIDER=y", profile)
        self.assertIn("CONFIG_XNU_OWNER_ROOT_AUDIT=y", profile)
        self.assertIn("CONFIG_XNU_OWNER_ROOT_TRUST=y", profile)
        self.assertNotIn("CONFIG_XNU_NATIVE_VIRTIO_BLOCK=y", profile)
        self.assertNotIn("CONFIG_XNU_SOURCE_MOCKFS=y", profile)
        self.assertNotIn("CONFIG_XNU_TRUST_CACHE_RUNTIME=y", profile)
        self.assertNotIn("dali-ipados-root", (ROOT / "board.toml").read_text())

    def test_diagnostic_service_refuses_to_compete_for_root_controller(self):
        start = self.diagnostic[self.diagnostic.index("bool DaliUfs::start("):]
        guard = start.index('PE_parse_boot_argn("dali-ipados-root"')
        register = start.index("registerService()")
        self.assertLess(guard, register)
        self.assertIn("root == 1", start[guard:register])

    def test_only_verified_read_only_windows_can_be_published(self):
        self.assertIn('findRootLayout(&layout_, &why, rootLun_, kDaliRootContainer)', self.root)
        self.assertIn('constexpr char kDaliRootContainer[] = "unixnu_data"', self.root)
        self.assertIn("static constexpr uint8_t kDaliRootLun = 2", self.root)
        self.assertIn(self.board["payload"]["adt"]["root_match_volume_uuid"], self.root)
        systemos_uuid = next(window["volume_uuid"] for window in self.board["userdata"]["windows"]
                             if window["name"] == "SystemOS")
        self.assertIn(systemos_uuid, self.root)
        self.assertIn(f"kDaliContainerFirstLba = {self.board['userdata']['first_lba']}", self.root)
        self.assertIn(f"kDaliContainerLastLba = {self.board['userdata']['last_lba']}", self.root)
        self.assertIn("ufsBuildRootPlan(names, layout_.windows, plan_)", self.root)
        self.assertIn("vr_open(manifest, manifestBytes", self.root)
        self.assertIn("memcmp(verified_[index].image_sha256, window.sha256", self.root)
        self.assertIn("vr_verify_block(&block.verified_", self.root)
        self.assertIn("ufsReadWindow(ops, block->absolute_, block->bytes_", self.root)
        self.assertIn('"DaliUfsRootLayout.h"', self.root)
        self.assertIn("*out = true; return kIOReturnSuccess", self.root)
        self.assertIn("kIOReturnLockedWrite", self.root)
        self.assertIn("unixnu_ufs::Port port{this, portRead, portWrite, cache, micros, delay, exclusive, nullptr}", self.root)
        self.assertNotIn("transport_.write(", self.root)
        self.assertNotIn("dali_ufs_wait_for_manual_recovery", self.root)
        self.assertNotIn("scratchWrite", self.root)

    def test_owner_authorization_is_bound_to_the_os_window_and_verified_media(self):
        self.assertIn('index_ != owner_->osWindow_', self.root)
        self.assertIn("native_verified_block_root_authorized(dev_t device)", self.root)
        self.assertIn("block->authenticatesVolume(uuid->getCStringNoCopy())", self.root)
        self.assertIn("__atomic_store_n(&rootAuthorized_, true, __ATOMIC_RELEASE)", self.root)
        self.assertIn("rootAuthorized() const", self.root)

    def test_archive_records_root_service_and_shared_verification_inputs(self):
        self.assertIn("ROOT / \"drivers/DaliUfs/DaliUfsRoot.cpp\"", self.builder)
        for header in ("RootWindowRead.h", "RootPublication.h",
                       "VerifiedBlockPolicy.h", "verified_root.h"):
            self.assertIn(header, self.builder)
        self.assertNotIn('ROOT.parent / "UniXNU"', self.builder)
        self.assertIn('"root_service_default_off": True', self.builder)
        self.assertIn('"boot_root_authenticated": False', self.builder)
        self.assertIn('ROOT / "drivers/DaliUfs/VerifiedRootUnavailable.cpp"', self.builder)

    def test_missing_verified_root_inputs_are_weak_and_fail_closed(self):
        self.assertIn("__attribute__((weak)) const uint64_t verified_root_policy_count = 0", self.fallback)
        self.assertIn("__attribute__((weak)) int vr_open", self.fallback)
        self.assertIn("return VR_KEY", self.fallback)
        self.assertIn("__attribute__((weak)) int vr_verify_block", self.fallback)
        self.assertIn("return VR_BLOCK", self.fallback)
        self.assertIn("strong replacements", self.fallback)

    def test_production_policy_builder_binds_raw_windows_and_never_archives_private_key(self):
        self.assertIn('"OS", "SystemOS"', self.policy_builder)
        self.assertIn("verify_built_image(image, entries, expected_capacity)", self.policy_builder)
        self.assertIn("metadata[\"image_sha256\"] != expected_digest.hex()", self.policy_builder)
        self.assertIn("OS and SystemOS policies must use the same owner anchor", self.policy_builder)
        self.assertIn('"private_key_in_archive": False', self.policy_builder)
        self.assertIn("libVerifiedRootDali.a", self.policy_builder)

    def test_device_root_defconfig_links_production_policy_archive_separately(self):
        compile_profile = (ROOT / "configs/dali_ipados_root_defconfig").read_text()
        boot_profile = (ROOT / "configs/dali_ipados_root_boot_defconfig").read_text()
        self.assertNotIn("libVerifiedRootDali.a", compile_profile)
        self.assertIn("libVerifiedRootDali.a", boot_profile)
        self.assertIn("CONFIG_XNU_DALI_OWNER_PRIVATE_KEY=", boot_profile)
        self.assertIn("CONFIG_XNU_DALI_OWNER_PUBLIC_KEY=", boot_profile)

    def test_root_compile_action_uses_owner_profile_without_qemu_or_device_artifact(self):
        self.assertIn("options: [reset-qualification, pid1, ipados-root-compile, ipados-root-policy-compile]", self.workflow)
        self.assertIn('profile == "ipados-root-compile"', self.workflow)
        self.assertIn('profile == "ipados-root-policy-compile"', self.workflow)
        self.assertIn('defconfig = "dali_ipados_root_boot_defconfig"', self.workflow)
        self.assertIn("Build pinned OS and SystemOS root windows", self.workflow)
        self.assertIn("Build metadata-preserving skip-setup Data seed candidate", self.workflow)
        self.assertIn('sudo env PYTHONDONTWRITEBYTECODE=1', self.workflow)
        self.assertGreaterEqual(self.workflow.count("env.VALIDATION_PROFILE != 'ipados-root-compile'"), 5)


if __name__ == "__main__":
    unittest.main()
