"""Real FDT parsing / resource serialization and opt-in Apple DT generation."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from dali_ufs_resources import encode_scratch_grant, resource_contract, RESOURCE_WORDS, CLOCK_NAMES, HCI, PHY, AO, CLK, RESET
spec = importlib.util.spec_from_file_location('ufs_adt', SCRIPTS/'make-dali-adt.py')
adt = importlib.util.module_from_spec(spec); spec.loader.exec_module(adt)


def cells(*v):
    return struct.pack('>'+str(len(v))+'I', *v)


def fixture():
    nodes = {'/': {'#address-cells':cells(2),'#size-cells':cells(2),'compatible':b'mediatek,MT6991\0'},
             '/soc': {'#address-cells':cells(2),'#size-cells':cells(2),'ranges':b'','dma-ranges':cells(0,0,0,0,0x10,0)}}
    for p,c,b,s in ((HCI,'mediatek,mt8183-ufshci',0x16810000,0x2a00),(PHY,'mediatek,mt8183-ufsphy',0x16800000,0x10000),
                    (AO,'mediatek,mt6991-ufscfg_ao',0x168a0000,0x1000),(CLK,'mediatek,mt6991-cksys',0x10000000,0x800)):
        nodes[p]={'compatible':c.encode()+b'\0','reg':cells(0,b,0,s)}
    nodes[PHY].update(phandle=cells(1), **{'#phy-cells':cells(0),'mphy-ver':cells(1)})
    nodes[AO].update(phandle=cells(2), **{'#clock-cells':cells(1)})
    nodes[CLK].update(phandle=cells(3), **{'#clock-cells':cells(1)})
    nodes[RESET]={'compatible':b'ti,syscon-reset\0','phandle':cells(4),'#reset-cells':cells(1),
                  'ti,reset-bits':cells(*([0]*7+[x for n in range(3) for x in (0x148,n,0x14c,n,0,0,0x1c)]))}
    nodes['/vcc']={'phandle':cells(5),'regulator-name':b'mt6363_vemc\0','regulator-always-on':b''}
    nodes['/core']={'phandle':cells(6),'regulator-name':b'dvfsrc-vcore\0','regulator-always-on':b''}
    nodes[HCI].update({'phys':cells(1),'clocks':cells(*[x for i,n in enumerate((42,97,115,41,118,96,1,3,5,7,9,11,13,15)) for x in (3 if i<6 else 2,n)]),
                      'clock-names':b''.join(n.encode()+b'\0' for n in CLOCK_NAMES),'resets':cells(4,1,4,2,4,3),
                      'reset-names':b'unipro_rst\0crypto_rst\0hci_rst\0','mediatek,ufs-rtff-mtcmos':b'',
                      'vcc-supply':cells(5),'dvfsrc-vcore-supply':cells(6),
                      'interrupts':cells(*[v for n in (320,*range(329,337)) for v in (0,n,4,0)])})
    nodes.update({'/memory':{'reg':cells(0,0x80000000,3,0)},'/cpus':{'#address-cells':cells(1)},'/cpus/cpu@0':{'reg':cells(0)},
                  '/timer':{'clock-frequency':cells(13000000)},'/interrupt-controller':{'compatible':b'arm,gic-v3\0','reg':cells(0,0xc400000,0,0x40000,0,0xc440000,0,0x200000)},
                  '/psci':{'compatible':b'arm,psci-1.0\0','method':b'smc\0'},'/watchdog@1c010000':{'reg':cells(0,0x1c010000,0,0x1000),'compatible':b'mediatek,mt6991-wdt\0'},
                  '/watchdog@1c010000/reboot-mode':{'compatible':b'syscon-reboot-mode\0','offset':cells(0x24),'mask':cells(15),'mode-bootloader':cells(3)},
                  '/soc/pinctrl@1002d000':{'reg':cells(0,0x1002d000,0,0x1000),'phandle':cells(10)},'/gpio-keys':{},
                  '/gpio-keys/volumedown':{'gpios':cells(10,60,1)},'/chosen':{'aee,enable':b'mini\0','mrdump,lk':b'MRDUMP11\0','mrdump,cblock':bytes(8)}})
    return nodes


def scratch_grant(**updates):
    value = {
        'profile': 'boot-b-scratch-write-test-v1', 'write_authorized': True, 'media_writes': True,
        'token': '0123456789abcdef0123456789abcdef',
        'device_binding_sha256': '11' * 32, 'candidate_sha256': '22' * 32, 'source_sha256': '33' * 32,
        'partition_guid': '00000046-39c2-4488-9bb0-00cb43c9ccd4',
        'lun': 2, 'block_bytes': 4096, 'capacity_blocks': 64577536,
        'boot_start_lba': 418560, 'boot_blocks': 16384, 'payload_end_bytes': 5062656,
        'scratch_start_lba': 434432, 'scratch_blocks': 256, 'tail_guard_blocks': 256, 'sequence': 1,
    }
    value.update(updates)
    return value


def dtb(nodes):
    strings=bytearray(); offsets={}
    for props in nodes.values():
        for name in props:
            if name not in offsets: offsets[name]=len(strings);strings+=name.encode()+b'\0'
    def node(path):
        name=b'' if path=='/' else path.rsplit('/',1)[-1].encode()
        data=cells(1)+name+b'\0';data+=bytes(-len(data)%4)
        for k,v in nodes[path].items(): data+=cells(3,len(v),offsets[k])+v+bytes(-len(v)%4)
        for child in nodes:
            if child!='/' and (child.rsplit('/',1)[0] or '/')==path:data+=node(child)
        return data+cells(2)
    body=node('/')+cells(9);string_offset=56+len(body)
    return cells(0xd00dfeed,string_offset+len(strings),56,string_offset,40,17,16,0,len(strings),len(body))+bytes(16)+body+strings


class ResourceTests(unittest.TestCase):
    def contract(self, nodes):
        raw=dtb(nodes);return resource_contract(adt.Fdt(raw),raw)

    def test_resource_words_and_raw_fdt_digest(self):
        raw=dtb(fixture());result=resource_contract(adt.Fdt(raw),raw)
        self.assertEqual(result,struct.pack('<16Q',*RESOURCE_WORDS)+hashlib.sha256(raw).digest())
        self.assertEqual(len(result),160)

    def test_wrong_reg_status_phy_or_dma_binding_is_refused(self):
        for path,key,value in ((HCI,'reg',cells(0,0x1d84000,0,0x3000)),(HCI,'status',b'disabled\0'),(PHY,'mphy-ver',cells(0)),
                               (HCI,'phys',cells(2)),('/soc','dma-ranges',cells(0,0,0,0,0x20,0)),(HCI,'iommus',b''),('/', 'dma-coherent',b'')):
            with self.subTest(path=path,key=key):
                n=fixture();n[path][key]=value
                with self.assertRaises(ValueError):self.contract(n)

    def test_every_clock_reference_and_reset_recipe_is_checked(self):
        for index in range(28):
            n=fixture();v=list(struct.unpack('>28I',n[HCI]['clocks']));v[index]^=32;n[HCI]['clocks']=cells(*v)
            with self.subTest(clock_cell=index),self.assertRaises(ValueError):self.contract(n)
        for p,k,v in ((HCI,'reset-names',b'hci_rst\0'),(RESET,'ti,reset-bits',cells(*([0]*28))),(HCI,'resets',cells(4,3,4,2,4,1))):
            n=fixture();n[p][k]=v
            with self.assertRaises(ValueError):self.contract(n)

    def test_duplicate_phandles_supply_irq_and_unsupported_power_recipe_fail(self):
        for p,k,v in (('/vcc','phandle',cells(1)),('/vcc','regulator-name',b'other\0'),('/core','regulator-always-on',b'x'),
                      (HCI,'interrupts',cells(0,320,4)),(HCI,'mediatek,ufs-support-va09',b''),(HCI,'mediatek,ufs-rtff-mtcmos',b'x')):
            n=fixture();n[p][k]=v
            with self.subTest(p=p,k=k),self.assertRaises(ValueError):self.contract(n)

    def test_default_ram_tree_does_not_require_or_publish_ufs(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'fdt';output=root/'adt';n=fixture();n[HCI]['phys']=cells(999);source.write_bytes(dtb(n))
            command=[sys.executable,str(SCRIPTS/'make-dali-adt.py'),'--fdt',str(source),'--dram-size','0x300000000','--output',str(output)]
            env=dict(os.environ,DALI_XNU_UFS_READ_ONLY='false')
            subprocess.run(command,env=env,check=True,capture_output=True)
            result = output.read_bytes()
            self.assertNotIn(b'dali-ufs\0',result)
            self.assertNotIn(b'darwinboot-kernel-sha256',result)
            self.assertEqual(result.count(b'crypto-hash-method'), 1)
            self.assertEqual(result.count(b'sha2-384\0'), 1)
            self.assertEqual(result.count(b'amfi-only-platform-code'), 1)
            self.assertIn(b'amfi-only-platform-code' + bytes(32 - len(b'amfi-only-platform-code')) +
                          struct.pack('<II', 4, 1), result)
            result=subprocess.run(command+['--ufs-readonly'],env=env,capture_output=True)
            self.assertNotEqual(result.returncode,0)

    def test_opt_in_generates_real_resource_nub(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'fdt';output=root/'adt';raw=dtb(fixture());source.write_bytes(raw)
            command=[sys.executable,str(SCRIPTS/'make-dali-adt.py'),'--fdt',str(source),'--dram-size','0x300000000','--output',str(output),'--ufs-readonly']
            subprocess.run(command,env=dict(os.environ,DALI_XNU_UFS_READ_ONLY='false'),check=True,capture_output=True)
            self.assertIn(b'dali-ufs\0',output.read_bytes())
            self.assertIn(resource_contract(adt.Fdt(raw),raw),output.read_bytes())
            self.assertNotIn(b'darwinboot-kernel-sha256',output.read_bytes())

    def test_os_root_match_adds_ufs_resources_and_data_ramdisk_slot(self):
        root_uuid = '04769D26-2CA0-4825-B443-E2E5F88300F2'
        xml = (b'<dict><key>IOProviderClass</key><string>IOMedia</string>'
               b'<key>IOPropertyMatch</key><dict><key>UUID</key><string>' + root_uuid.encode() +
               b'</string></dict></dict>\0')
        for flag in ('--data-ramdisk', '--packed-data-ramdisk'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temp:
                root=Path(temp);source=root/'fdt';output=root/'adt';raw=dtb(fixture());source.write_bytes(raw)
                command=[sys.executable,str(SCRIPTS/'make-dali-adt.py'),'--fdt',str(source),'--dram-size',
                         '0x300000000','--output',str(output),'--root-match-volume-uuid',root_uuid,flag]
                subprocess.run(command,env=dict(os.environ,DALI_XNU_UFS_READ_ONLY='false'),check=True,capture_output=True)
                result=output.read_bytes()
                self.assertIn(xml,result)
                self.assertIn(b'RAMDisk',result)
                self.assertIn(b'dali-ufs\0',result)
                self.assertIn(resource_contract(adt.Fdt(raw),raw),result)

    def test_invalid_os_root_uuid_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'fdt';output=root/'adt';source.write_bytes(dtb(fixture()))
            command=[sys.executable,str(SCRIPTS/'make-dali-adt.py'),'--fdt',str(source),'--dram-size',
                     '0x300000000','--output',str(output),'--root-match-volume-uuid','not-a-uuid']
            result=subprocess.run(command,capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(output.exists())

    def test_explicit_scratch_grant_wire_and_profile_validation(self):
        value=scratch_grant();wire=encode_scratch_grant(value)
        self.assertEqual(len(wire),232)
        self.assertEqual(wire[:16],b'DALIUFSW'+struct.pack('<II',2,1))
        self.assertEqual(wire[16:48],value['token'].encode())
        self.assertEqual(wire[48:80],bytes.fromhex('11'*32))
        self.assertEqual(wire[144:160],uuid.UUID(value['partition_guid']).bytes)
        self.assertEqual(struct.unpack_from('<IIQQQQQQQQ',wire,160),
                         (2,4096,64577536,418560,16384,5062656,434432,256,256,1))
        invalid=(
            {'write_authorized':False}, {'media_writes':False}, {'token':'bad'},
            {'lun':8}, {'block_bytes':512}, {'scratch_blocks':255},
            {'tail_guard_blocks':1}, {'scratch_start_lba':434431}, {'sequence':0}, {'sequence':129},
            {'device_binding_sha256':'00'*32}, {'partition_guid':'00000000-0000-0000-0000-000000000000'},
        )
        for change in invalid:
            with self.subTest(change=change),self.assertRaises(ValueError):encode_scratch_grant(scratch_grant(**change))

    def test_scratch_grant_is_emitted_only_by_explicit_write_profile_with_same_token(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'fdt';output=root/'adt';grant=root/'grant.json';raw=dtb(fixture());source.write_bytes(raw)
            value=scratch_grant();grant.write_text(json.dumps(value))
            command=[sys.executable,str(SCRIPTS/'make-dali-adt.py'),'--fdt',str(source),'--dram-size','0x300000000',
                     '--output',str(output),'--ufs-scratch-grant',str(grant),'--run-token',value['token']]
            env=dict(os.environ,DALI_XNU_UFS_READ_ONLY='true',DALI_XNU_UFS_SCRATCH_WRITE='true')
            subprocess.run(command,env=env,check=True,capture_output=True)
            self.assertIn(encode_scratch_grant(value),output.read_bytes())
            # The host supplies only a unique zero slot, never a computed hash.
            slot=struct.pack('<32sI',b'darwinboot-kernel-sha256',32)+bytes(32)
            self.assertEqual(output.read_bytes().count(slot),1)
            for bad_env in (dict(env,DALI_XNU_UFS_SCRATCH_WRITE='false'),dict(env,DALI_XNU_UFS_READ_ONLY='false')):
                self.assertNotEqual(subprocess.run(command,env=bad_env,capture_output=True).returncode,0)
            wrong=command.copy();wrong[-1]='ffffffffffffffffffffffffffffffff'
            self.assertNotEqual(subprocess.run(wrong,env=env,capture_output=True).returncode,0)
