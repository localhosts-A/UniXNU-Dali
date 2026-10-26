import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("userdata_log_runner", ROOT / "scripts/run-userdata-log.py")
runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
probe_spec = importlib.util.spec_from_file_location("userdata_probe", ROOT / "scripts/run-ufs-probe.py")
probe = importlib.util.module_from_spec(probe_spec); probe_spec.loader.exec_module(probe)

TOKEN = "1234567890abcdef" * 2
SHA = "a" * 64


def transcript(*, complete=True):
    lines = []
    lines += [f"DALI-UFS-LOG: run={TOKEN} mode=1 target=2 begin sequence=1 owner=1",
              f"DALI-UFS-LOG: run={TOKEN} mode=1 result=Success attempted=1 submitted=1 synced=1 verified=1 bytes=42 sequence=1 sha256={SHA}"]
    if complete:
        for _ in range(2):
            lines += [f"DALI-UFS-LOG: run={TOKEN} mode=2 target=2 begin sequence=1 owner=1",
                      f"DALI-UFS-LOG: run={TOKEN} mode=2 result=Success attempted=1 submitted=0 synced=0 verified=1 bytes=42 sequence=1 sha256={SHA}"]
        lines += [f"DALI-XNU-UFS: run={TOKEN} stage=userdata-log-attempt-submitted a=1 b=0 c=0",
                  f"DALI-XNU-UFS: run={TOKEN} stage=userdata-log-verified a=1 b=2a c=0",
                  f"DALI-XNU-UFS: run={TOKEN} stage=userdata-log-finish a=1 b=1 c=6",
                  f"DALI-XNU-UFS: run={TOKEN} stage=userdata-log-existing-record a=1 b=2a c=0",
                  f"DALI-XNU-UFS: run={TOKEN} stage=userdata-log-verified a=1 b=2a c=0",
                  f"DALI-XNU-UFS: run={TOKEN} stage=userdata-log-finish a=1 b=1 c=9"]
    return "\n".join(lines)


def apple_node(name, props=(), children=()):
    values = [("name", name.encode("ascii") + b"\0"), *props]
    data = bytearray(struct.pack("<II", len(values), len(children)))
    for key, value in values:
        data.extend(struct.pack("<32sI", key.encode("ascii"), len(value)))
        data.extend(value); data.extend(bytes(-len(value) % 4))
    for child in children: data.extend(child)
    return bytes(data)


def userdata_wire(token):
    return runner.encode_scratch_grant(dict(
        profile="userdata-logs-diagnostic-v1", target_partition="unixnu_logs",
        write_authorized=True, media_writes=True, token=token,
        device_binding_sha256="11" * 32, candidate_sha256="22" * 32,
        source_sha256="33" * 32, partition_guid="cde9ab32-18d8-4777-8ced-691e7cd5c636",
        lun=2, block_bytes=4096, capacity_blocks=64577536, boot_start_lba=64565240,
        boot_blocks=8192, payload_end_bytes=0, scratch_start_lba=64565240,
        scratch_blocks=1024, tail_guard_blocks=7168, sequence=1))


class UserdataLogRunnerTests(unittest.TestCase):
    def test_exact_v4_grant_is_bound_into_the_candidate_adt_with_pinned_encoder(self):
        adt = apple_node("device-tree", children=[apple_node("arm-io", children=[
            apple_node("dali-ufs", [("compatible", b"localhosts,dali-ufs\0")])])])
        wire = userdata_wire(TOKEN)
        # Developer workspaces name the pinned checkout after its branch,
        # while CI checks it out under the stable workflow path `UEFI-Dali`.
        # Ignore unrelated sibling clones that do not contain this wire API.
        uefi = next((candidate for candidate in (
            ROOT.parent / "UEFI-Dali-ufs-scratch", ROOT.parent / "UEFI-Dali")
            if (candidate / "tools/make_payload.py").is_file() and
            "validate_userdata_log_wire" in
            (candidate / "tools/make_payload.py").read_text(encoding="utf-8") and
            "append_apple_dt_property" in
            (candidate / "tools/make_payload.py").read_text(encoding="utf-8")), None)
        self.assertIsNotNone(uefi, "pinned UEFI ADT encoder checkout is unavailable")
        bound = runner.bind_userdata_grant_to_adt(uefi, adt, wire, TOKEN)
        self.assertNotEqual(bound, adt)
        self.assertEqual(runner.bind_userdata_grant_to_adt(uefi, bound, wire, TOKEN), bound)
        with self.assertRaisesRegex(runner.cycle.CycleError, "authorized userdata grant"):
            runner.bind_userdata_grant_to_adt(uefi, adt, wire, "f" * 32)

    def test_final_candidate_verifier_supplies_userdata_wire_to_payload_validator(self):
        wire = userdata_wire(TOKEN)

        class PayloadTools:
            def validate_xnu_ufs_bundle(self, *args):
                self.args = args

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "grant.bin").write_bytes(wire)
            tools = PayloadTools()
            probe.validate_candidate_xnu_bundle(
                tools, directory, "userdata-log", "payload", TOKEN,
                b"adt", b"live-fdt", b"config", b"recovery", b"kernel")
            self.assertEqual(tools.args[-1], wire)
            probe.validate_candidate_xnu_bundle(
                tools, directory, "xnu-ufs-read-only", "payload", TOKEN,
                b"adt", b"live-fdt", b"config", b"recovery", b"kernel")
            self.assertIsNone(tools.args[-1])

    def test_transcript_requires_write_sync_and_same_candidate_readback_for_both_owners(self):
        complete = runner.score_userdata_transcript(transcript(), TOKEN, 1)
        self.assertTrue(complete["uefi_write_sync_pass"])
        self.assertTrue(complete["uefi_same_candidate_readback_pass"])
        self.assertTrue(complete["xnu_write_sync_immediate_readback_pass"])
        self.assertTrue(complete["xnu_same_candidate_reboot_readback_pass"])
        self.assertTrue(complete["persistence_qualified"])
        partial = runner.score_userdata_transcript(transcript(complete=False), TOKEN, 1)
        self.assertTrue(partial["uefi_write_sync_pass"])
        self.assertFalse(partial["persistence_qualified"])
        self.assertFalse(partial["xnu_write_sync_immediate_readback_pass"])

    def test_other_token_and_wrong_sequence_do_not_qualify(self):
        text = transcript().replace(TOKEN, "f" * 32)
        score = runner.score_userdata_transcript(text, TOKEN, 1)
        self.assertFalse(score["uefi_write_sync_pass"])
        score = runner.score_userdata_transcript(transcript(), TOKEN, 2)
        self.assertFalse(score["uefi_write_sync_pass"])

    def test_readback_records_must_match_the_committed_write_bytes_and_hash(self):
        changed = transcript().replace("sha256=" + SHA, "sha256=" + "b" * 64, 1)
        score = runner.score_userdata_transcript(changed, TOKEN, 1)
        self.assertTrue(score["uefi_write_sync_pass"])
        self.assertFalse(score["uefi_same_candidate_readback_pass"])
        self.assertFalse(score["persistence_qualified"])

    def test_pending_userdata_log_lease_cannot_clear_before_android_export(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"; run.mkdir()
            token = "b" * 32
            state = dict(token=token, userdata_logs_may_be_modified=True)
            lease = probe.DeviceLease("device", Path(temp) / "ledger")
            with lease:
                lease.arm(run, state)
                self.assertTrue(lease.data["pending_userdata_logs"])
                with self.assertRaisesRegex(probe.cycle.CycleError, "capture/export"):
                    lease.restored(run, state)
                state.update(userdata_logs_capture_verified=True,
                             userdata_logs_android_export_verified=True,
                             userdata_logs_may_be_modified=False)
                lease.restored(run, state)
                self.assertIsNone(lease.data["pending_run"])
                self.assertFalse(lease.data["pending_userdata_logs"])

    def test_malformed_or_orphaned_userdata_pending_flag_refuses_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); device = root / __import__("hashlib").sha256(b"device").hexdigest()
            device.mkdir(); (device / "state.json").write_text('{"pending_run":null,"pending_userdata_logs":true,"used_tokens":[]}')
            with self.assertRaisesRegex(probe.cycle.CycleError, "invalid userdata logs"):
                with probe.DeviceLease("device", root): pass

    def test_userdata_trial_restore_refuses_a_lost_canonical_pending_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); run = root / "run"; run.mkdir()
            device = root / __import__("hashlib").sha256(b"device").hexdigest()
            device.mkdir()
            (device / "state.json").write_text(__import__("json").dumps({
                "pending_run": str(run), "pending_userdata_logs": False,
                "pending_expdb": False, "used_tokens": ["b" * 32]}))
            state = dict(kind="dali-userdata-logs-trial", userdata_logs_may_be_modified=False,
                         userdata_logs_capture_verified=True, userdata_logs_android_export_verified=True)
            with probe.DeviceLease("device", root) as lease:
                with self.assertRaisesRegex(probe.cycle.CycleError, "lost its canonical"):
                    lease.restored(run, state)


if __name__ == "__main__":
    unittest.main()
