"""Bind the exact passive MT6991 GPU resource graph and CSF image to an ADT nub.

The descriptor inventories resources. It does not authorize inherited GPUEB RAM,
qualify power/MMU handoff, or assert a coherent DMA route. No device is accessed.
"""
import hashlib
import struct

GPU = '/soc/mali@48000000'
FREQUENCY = '/soc/gpufreq@48500000'
GPUEB = '/soc/gpueb@4b000000'
GHPM = '/soc/ghpm@4b800000'
SPIS = (606, 605, 604, 607, 608, 612)
IRQ_NAMES = ('JOB', 'MMU', 'GPU', 'EVENT', 'PWR', 'GPUEB_MBOX1')
MAGIC = 0x494c414d494c4144
IPI_NAMES = ('IPI_ID_FAST_DVFS_EVENT', 'IPI_ID_GPUFREQ', 'IPI_ID_SLEEP', 'IPI_ID_TIMER',
             'IPI_ID_FHCTL', 'IPI_ID_CCF', 'IPI_ID_GPUMPU', 'IPI_ID_FAST_DVFS',
             'CH_IPIR_C_MET', 'CH_IPIS_C_MET', 'IPI_ID_BRISKET', 'IPI_ID_PPB')
IPI_SEND_WORDS = (4, 8, 3, 6, 9, 4, 6, 6, 1, 4, 4, 1)
IPI_RECEIVE = ((4, 0, 0), (8, 1, 1), (1, 0, 1), (1, 0, 1), (1, 1, 1), (4, 1, 1),
               (1, 1, 1), (6, 1, 1), (4, 0, 1), (1, 1, 1), (4, 1, 1), (1, 1, 1))
# tag -> (node, reg-name, physical address, length). Tags match NativeMaliResources.h.
WINDOWS = {
    2: (FREQUENCY, 'mfg_top_config', 0x48500000, 0x1000),
    3: (FREQUENCY, 'mfg_smmu', 0x48600000, 0x200000),
    4: (FREQUENCY, 'mfg_rpc', 0x4b800000, 0x10000),
    5: (FREQUENCY, 'mfg_pll', 0x4b810000, 0x400),
    6: (FREQUENCY, 'mfg_pll_sc0', 0x4b810400, 0x400),
    7: (FREQUENCY, 'mfg_pll_sc1', 0x4b810800, 0x400),
    8: (FREQUENCY, 'sleep', 0x1c004000, 0x10000),
    9: (FREQUENCY, 'mfg_vcore_ao_cfg', 0x4b860000, 0x1000),
    10: (FREQUENCY, 'mfg_hbvc', 0x4b840000, 0x1000),
    11: (FREQUENCY, 'mcusys_par_wrap', 0x0c000000, 0x1000),
    12: (FREQUENCY, 'cksys', 0x10000000, 0x1000),
    16: (GPUEB, 'gpueb_base', 0x4b000000, 0xa0000),
    17: (GPUEB, 'gpueb_gpr_base', 0x4b09fd00, 0x80),
    18: (GPUEB, 'gpueb_cfgreg_base', 0x4b140100, 0x200),
    19: (GPUEB, 'gpueb_intc_base', 0x4b120000, 0x100),
    20: (GPUEB, 'gpueb_dma_base', 0x4b130000, 0x200),
    21: (GPUEB, 'mbox0_base', 0x4b09fd80, 0x280),
    22: (GPUEB, 'mbox0_init', 0x4b170008, 4),
    23: (GPUEB, 'mbox0_set', 0x4b170004, 4),
    24: (GPUEB, 'mbox0_clr', 0x4b170074, 4),
    25: (GPUEB, 'mbox0_send', 0x4b170000, 4),
    26: (GPUEB, 'mbox0_recv', 0x4b170078, 4),
    27: (GPUEB, 'mfg0_pwr_con', 0x4b800504, 4),
    32: (GHPM, 'mfg_mt6991_e2_id_con', 0x4b860128, 4),
    33: (GHPM, 'spm_mfg0_pwr_con', 0x1c004ea8, 4),
    34: (GHPM, 'clk_cfg_6', 0x1c000070, 4),
    35: (GHPM, 'clk_cfg_6_set', 0x1c000074, 4),
    36: (GHPM, 'clk_cfg_6_clr', 0x1c000078, 4),
}


def resource_contract(tree, raw_fdt, firmware):
    def require(ok, reason):
        if not ok:
            raise ValueError('Mali resource contract: ' + reason)

    require(40 <= len(raw_fdt) <= 1024 * 1024 and raw_fdt[:4] == b'\xd0\x0d\xfe\xed', 'FDT size/header')
    require(struct.unpack_from('>I', raw_fdt, 4)[0] == len(raw_fdt), 'FDT total size')
    require(20 <= len(firmware) <= 16 * 1024 * 1024 and firmware[:6] == b'n:\xf1\xc3\x03\x00', 'CSF image header')
    require(b'mediatek,MT6991' in tree.string_list('/', 'compatible'), 'SoC')
    for path in ('/', '/soc'):
        require(tree.cells(path, '#address-cells') == (2,) and tree.cells(path, '#size-cells') == (2,), path + ':cells')
    require(tree.nodes['/soc'].get('ranges') == b'', 'soc translation')
    for path, compatible in ((GPU, b'arm,mali-valhall'), (FREQUENCY, b'mediatek,gpufreq'),
                             (GPUEB, b'mediatek,gpueb'), (GHPM, b'mediatek,ghpm')):
        require(path in tree.nodes and compatible in tree.string_list(path, 'compatible'), path + ':compatible')
        require(tree.nodes[path].get('status', b'okay\0') in (b'okay\0', b'ok\0'), path + ':status')
    require(tree.reg(GPU) == [(0x48000000, 0x480000)], 'GPU aperture')
    require(tree.cells('/gpufreq-wrapper', 'gpueb-support') == (1,) and
            tree.cells(GPUEB, 'gpueb-support') == (1,) and tree.cells(GPUEB, 'ghpm-support') == (1,), 'GPUEB route')
    for name, expected in (('mbox-count', (1,)), ('mbox-size', (160,)), ('slot-size', (4,))):
        require(tree.cells(GPUEB, name) == expected, 'GPUEB ' + name)
    for name, expected in (('gpueb_mem_size', (0, 0x180000)), ('gpueb_mem_addr', (0, 0xfc810000)),
                           ('gpueb-mem-table', (0, 0x4000, 1, 0x180000))):
        require(tree.cells(GPUEB, name) == expected, 'GPUEB ' + name)
    require(tree.string_list(GPUEB, 'gpueb-mem-name-table') == [b'MEM_ID_GPUFREQ', b'MEM_ID_LOG'],
            'GPUEB partition names')
    for name in ('send-name-table', 'recv-name-table'):
        require(tree.string_list(GPUEB, name) == [s.encode() for s in IPI_NAMES], 'GPUEB ' + name)
    require(tree.cells(GPUEB, 'send-table') == tuple(x for i, n in enumerate(IPI_SEND_WORDS) for x in (i, 0, n)),
            'GPUEB send channel layout')
    require(tree.cells(GPUEB, 'recv-table') == tuple(x for i, fields in enumerate(IPI_RECEIVE) for x in (i, 0, *fields)),
            'GPUEB receive channel layout')
    require(tree.nodes[GPU].get('interrupt-names') == b''.join(s.encode() + b'\0' for s in IRQ_NAMES), 'IRQ names')
    require(tree.cells(GPU, 'interrupts') == tuple(x for spi in SPIS for x in (0, spi, 4, 0)), 'IRQ specs')
    require(not any(k in tree.nodes[GPU] for k in ('iommus', 'dma-coherent', 'dma-noncoherent')), 'GPU DMA/coherency binding')
    records = [(1, 1, 0x48000000, 0x480000)]
    for tag, (path, name, base, size) in WINDOWS.items():
        names = tree.string_list(path, 'reg-names')
        regions = tree.reg(path)
        require(len(names) == len(regions) and len(set(names)) == len(names), path + ':reg names')
        require(name.encode() in names, path + ':' + name)
        require(regions[names.index(name.encode())] == (base, size), path + ':' + name + ' aperture')
        records.append((tag, 1, base, size))
    # GHPM reuses RPC/VCORE apertures with a narrower named RPC window.
    for name, expected in (('mfg_rpc', (0x4b800000, 0x1000)),
                           ('mfg_vcore_ao_config', (0x4b860000, 0x1000))):
        names = tree.string_list(GHPM, 'reg-names')
        require(name.encode() in names and tree.reg(GHPM)[names.index(name.encode())] == expected,
                'GHPM ' + name)
    shared = [path for path in tree.nodes if b'mediatek,me_GPUEB_SHARED' in tree.string_list(path, 'compatible')]
    require(len(shared) == 1 and shared[0].startswith('/reserved-memory/'), 'GPUEB reservation identity')
    require(tree.reg(shared[0]) == [(0xfc810000, 0x180000)], 'GPUEB reservation range')
    records.append((128, 2, 0xfc810000, 0x180000))
    header = struct.pack('<8Q', MAGIC, 1, len(records), len(SPIS), 4096, len(firmware), 13, 1)
    return (header + hashlib.sha256(raw_fdt).digest() + hashlib.sha256(firmware).digest() +
            struct.pack('<6Q', *SPIS) + b''.join(struct.pack('<IIQQ', *record) for record in records))
