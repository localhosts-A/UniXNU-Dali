import importlib.util
from pathlib import Path
import tempfile
import unittest

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


class UserdataLogRunnerTests(unittest.TestCase):
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
