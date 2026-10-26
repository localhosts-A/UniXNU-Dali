"""Collect expdb before restoring only its original one-MiB diagnostic window.

This is recovery, not a test-write or grant issuer. Callers hold DeviceLease and
keep pending_expdb set until this report and boot_b/Android/18 hashes pass. There
is no automatic write retry. A later explicit recovery can inspect an uncertain
previous result and restore the same original bytes again if needed.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess

import ufs_geometry as geometry
import ufs_identity
from ufs_expdb import MIB, _read, validate_expdb_backup
from ufs_expdb_log import decode_log


def sha(data):
    return hashlib.sha256(data).hexdigest()


def durable_json(path, value):
    temporary = path.with_suffix('.partial')
    with temporary.open('w') as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, indent=2, sort_keys=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def prepare_restore(*, serial, baseline, baseline_dir, expdb_baseline, expdb_dir):
    """Reopen the full original image, reciprocal GPT and identity captures."""
    if baseline.get('serial') != serial:
        raise ValueError('expdb restoration serial differs from UFS baseline')
    identity = ufs_identity.validate_captured_identity(baseline, baseline_dir)
    shape = validate_expdb_backup(expdb_baseline, expdb_dir, serial=serial,
                                 geometry=baseline['geometry'], reference_gpt=baseline['gpt'],
                                 original_boot_sha=baseline['original_boot_b_sha256'])
    original = _read(Path(expdb_dir), 'expdb.img', shape['partition_bytes'])
    if sha(original) != shape['original_partition_sha256']:
        raise ValueError('expdb original image changed during recovery preparation')
    return dict(shape, serial=serial, device_binding=identity,
                block_bytes=baseline['geometry']['logical_block_bytes'],
                capacity_bytes=baseline['geometry']['capacity_bytes'],
                lun=baseline['geometry']['lun'], gpt=baseline['gpt'], original=original)


def recover_window(image, io, output, *, expected_log):
    """Export immutable pre-restore bytes, restore once, verify all original bytes.

    io.observe repeats live identity/GPT/WP and original Android/18-hash checks.
    Failed or absent log decoding does not prevent recovery, but never qualifies
    a trial. Guard differences prevent any restore write and remain pending.
    """
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = dict(schema=1, serial=image['serial'], expdb_restore_verified=False,
                  write_attempted=False, device_sync_completed=False,
                  record_integrity_verified=False, physical_write_qualified=False,
                  persistence_qualified=False)
    path = output / 'recovery.json'
    durable_json(path, report)
    try:
        io.observe(image)
        digest = hashlib.sha256()
        head, after = hashlib.sha256(), hashlib.sha256()
        window = None
        with (output / 'before.img').open('xb') as capture:
            os.chmod(capture.name, 0o400)
            for offset in range(0, image['partition_bytes'], MIB):
                count = min(MIB, image['partition_bytes'] - offset)
                data = io.read(offset, count)
                if not isinstance(data, bytes) or len(data) != count:
                    raise ValueError('truncated expdb pre-restore capture')
                capture.write(data)
                digest.update(data)
                if offset == 0:
                    head.update(data)
                elif offset == MIB:
                    window = data
                else:
                    after.update(data)
            capture.flush()
            os.fsync(capture.fileno())
        report.update(before_sha256=digest.hexdigest(), window_sha256=sha(window),
                      head_sha256=head.hexdigest(), after_sha256=after.hexdigest(),
                      pre_restore_capture_complete=True)
        try:
            body, decoded = decode_log(window, **expected_log)
            with (output / 'expdb-console.bin').open('xb') as target:
                os.chmod(target.name, 0o400)
                target.write(body)
                target.flush()
                os.fsync(target.fileno())
            report.update(record_integrity_verified=True, log=decoded)
        except (ValueError, TypeError) as error:
            report['log_error'] = str(error)
        durable_json(path, report)
        if (head.hexdigest() != image['original_head_sha256'] or
                after.hexdigest() != image['original_after_sha256']):
            raise ValueError('expdb bytes outside the authorized window changed; preserve pending recovery')
        original = image['original'][MIB:2*MIB]
        if window != original:
            # Recheck after export and before the only write. Persist intent
            # first, so a host failure cannot erase the uncertain operation.
            io.observe(image)
            report['write_attempted'] = True
            durable_json(path, report)
            io.write_original_once(image, original, output)
            report['device_sync_completed'] = True
            durable_json(path, report)
        restored = hashlib.sha256()
        for offset in range(0, image['partition_bytes'], MIB):
            count = min(MIB, image['partition_bytes'] - offset)
            data = io.read(offset, count)
            if not isinstance(data, bytes) or len(data) != count:
                raise ValueError('truncated expdb restoration readback')
            restored.update(data)
        report['restored_sha256'] = restored.hexdigest()
        if restored.hexdigest() != image['original_partition_sha256']:
            raise ValueError('complete expdb restoration hash differs from original backup')
        io.observe(image)
        report['expdb_restore_verified'] = True
        durable_json(path, report)
        return report
    except Exception as error:
        report['recovery_error'] = str(error)
        durable_json(path, report)
        raise


class AndroidExpdb:
    """Actual ADB backend. Its sole block write is a fixed original window."""
    def __init__(self, cycle, serial, records):
        self.cycle, self.serial, self.records = cycle, serial, records
        self.node = None
        self.block = None

    def text(self, command):
        return self.cycle.adb_output(self.serial, command).decode('ascii').strip()

    def integer(self, path):
        value = self.text('cat ' + shlex.quote(path))
        if not re.fullmatch('[0-9]+', value):
            raise ValueError('invalid expdb sysfs integer: ' + path)
        return int(value)

    def observe(self, image):
        self.cycle.current_android(self.serial)
        self.cycle.compare_partition_hashes(self.serial, self.records)
        if image['serial'] != self.serial:
            raise ValueError('expdb recovery device mismatch')
        node = self.text('readlink -f /dev/block/by-name/expdb')
        match = re.fullmatch(r'/dev/block/(sd[a-z]+)([1-9][0-9]*)', node)
        if not match:
            raise ValueError('expdb is not a direct SCSI partition')
        disk, number = match.group(1), int(match.group(2))
        parent, part = '/sys/class/block/' + disk, '/sys/class/block/' + disk + str(number)
        scsi = self.text('readlink -f ' + parent + '/device')
        address = re.fullmatch(r'(.*/([0-9]+):([0-9]+):([0-9]+):([0-9]+))', scsi)
        block = self.integer(parent + '/queue/logical_block_size')
        capacity = self.integer(parent + '/size') * 512
        if (not address or '/16810000.ufshci/' not in scsi or int(address.group(5)) != image['lun'] or
                block != image['block_bytes'] or capacity != image['capacity_bytes'] or
                self.integer(part + '/ro') != 0 or self.integer(parent + '/ro') != 0 or
                self.integer(part + '/start') * 512 != image['partition']['first_lba'] * block or
                self.integer(part + '/size') * 512 != image['partition_bytes'] or
                number != image['partition']['number']):
            raise ValueError('live expdb topology/geometry/write-protection differs')
        events = dict(line.split('=', 1) for line in self.text('cat ' + part + '/uevent').splitlines() if '=' in line)
        if events.get('PARTUUID', '').lower() != image['partition']['guid']:
            raise ValueError('live expdb partition GUID differs')
        controller = scsi.split('/host', 1)[0]
        _, identity, _ = ufs_identity.read_identity(controller, lambda command: self.cycle.adb_output(self.serial, command))
        if identity != image['device_binding']:
            raise ValueError('live UFS identity differs from original expdb backup')
        descriptors = {key: self.text('cat ' + parent + '/device/unit_descriptor/' + key)
                       for key in ('lu_enable', 'lun_write_protect', 'logical_block_size', 'logical_block_count')}
        unit = geometry.unit_descriptor(dict(logical_block_bytes=block, capacity_blocks=capacity//block), descriptors)
        if unit['unit_write_protected']:
            raise ValueError('live UFS LUN is write protected')
        raw = []
        for lba in (1, capacity//block-1):
            header = self.cycle.adb_output(self.serial, f'dd if=/dev/block/{disk} bs={block} skip={lba} count=1 status=none')
            meta = geometry.header(header, block, capacity, lba)
            count = (meta['entries_bytes'] + block-1)//block
            table = self.cycle.adb_output(self.serial, f'dd if=/dev/block/{disk} bs={block} skip={meta["entries_lba"]} count={count} status=none')
            if len(table) != count*block:
                raise ValueError('truncated live expdb GPT entries')
            raw.extend((header, table[:meta['entries_bytes']]))
        if geometry.validate_gpt(*raw, block, capacity) != image['gpt']:
            raise ValueError('live expdb reciprocal GPT differs from backup')
        if self.node is not None and (self.node != node or self.block != block):
            raise ValueError('expdb device path changed during recovery')
        self.node, self.block, self.size = node, block, image['partition_bytes']

    def read(self, offset, count):
        if (self.node is None or type(offset) is not int or type(count) is not int or
                offset < 0 or not 0 < count <= MIB or offset % self.block or count % self.block or
                offset + count > self.size):
            raise ValueError('expdb recovery read exceeds verified partition')
        data = self.cycle.adb_output(self.serial, f'dd if={self.node} bs={self.block} skip={offset//self.block} count={count//self.block} status=none')
        if len(data) != count:
            raise ValueError('truncated Android expdb read')
        return data

    def write_original_once(self, image, data, output):
        if (self.node is None or self.block != image['block_bytes'] or len(data) != MIB or
                data != image['original'][MIB:2*MIB] or sha(data) != image['original_scratch_sha256']):
            raise ValueError('expdb restore write is not the exact original window')
        # Android toybox 0.8.13 lacks iflag=fullblock. Stage a complete regular
        # file and verify it remotely instead of dd from potentially short pipe
        # reads. The actual block write uses neither a caller path nor payload.
        local = Path(output) / 'original-window.bin'
        with local.open('xb') as target:
            os.chmod(local, 0o400)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        remote = '/data/local/tmp/dali-expdb-restore-' + secrets.token_hex(16)
        self.cycle.adb_output(self.serial, 'mkdir -m 700 ' + remote)
        staged = remote + '/original.bin'
        try:
            try:
                transfer = subprocess.run(['adb', '-s', self.serial, 'exec-out',
                    'su -c ' + shlex.quote('cat > ' + staged)], input=data,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise self.cycle.CycleError('original expdb staging failed: ' + str(error)) from None
            if transfer.returncode != 0 or transfer.stdout:
                raise self.cycle.CycleError('original expdb staging did not complete')
            size = self.text('stat -c %s ' + staged)
            digest = self.text('sha256sum ' + staged).split()
            if size != str(MIB) or len(digest) != 2 or digest[0] != sha(data) or digest[1] != staged:
                raise ValueError('staged Android original expdb bytes differ')
            self.cycle.adb_output(self.serial,
                f'dd if={staged} of={self.node} bs={self.block} seek={MIB//self.block} count={MIB//self.block} conv=notrunc,fsync status=none', timeout=60)
            self.cycle.adb_output(self.serial, 'blockdev --flushbufs ' + self.node, timeout=60)
        finally:
            # A failed cleanup cannot erase the recovery failure or trigger a
            # second block write. Keep the private staging path in the report.
            try:
                self.cycle.adb_output(self.serial, 'rm -f ' + staged)
                self.cycle.adb_output(self.serial, 'rmdir ' + remote)
            except self.cycle.CycleError:
                pass
