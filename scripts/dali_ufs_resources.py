"""Validate fixed MT6991 UFS FDT resources before emitting an opt-in Apple-DT nub.

This is a resource contract, not DMA/coherency or write authorization. Keep the
on-device HCI/IP/queue checks in DaliUfsPlatform as well. No device is contacted.
"""
import hashlib
import struct

HCI = '/soc/ufshci@16810000'
PHY = '/soc/ufsphy@16800000'
AO = '/syscon@168a0000'
CLK = '/syscon@10000000'
RESET = AO + '/reset-controller'
CLOCK_NAMES = ('ufs_sel', 'ufs_sel_min_src', 'ufs_sel_max_src', 'ufs_fde',
               'ufs_fde_min_src', 'ufs_fde_max_src', 'unipro_tx_sym', 'unipro_rx_sym0',
               'unipro_rx_sym1', 'unipro_sys', 'unipro_sap', 'ufs_phy_sap', 'ufshci_ufs', 'ufshci_aes')
RESOURCE_WORDS = (0x31534655494c4144, 1, 0x16810000, 0x2a00, 0x168a0000, 0x1000,
                  0x16800000, 0x10000, 0x10000000, 0x800, 0, 0x1000000000, 1, 7, 14, 7)


def resource_contract(tree, raw_fdt):
    def require(ok, reason):
        if not ok:
            raise ValueError('UFS resource contract: ' + reason)

    def cells(path, name, expected):
        require(tree.cells(path, name) == tuple(expected), path + ':' + name)

    def status(path):
        require(tree.nodes[path].get('status', b'okay\0') in (b'okay\0', b'ok\0'), path + ':status')

    def compatible(path, value):
        require(value.encode() in tree.string_list(path, 'compatible'), path + ':compatible')

    # Resolve identities, not hardcoded phandle numbers. Ambiguous aliases fail.
    handles = {}
    for path, props in tree.nodes.items():
        for key in ('phandle', 'linux,phandle'):
            if key not in props:
                continue
            value = props[key]
            require(len(value) == 4 and int.from_bytes(value, 'big') not in (0, 0xffffffff), path + ':' + key)
            number = int.from_bytes(value, 'big')
            require(number not in handles or handles[number] == path, 'duplicate phandle')
            handles[number] = path
        if 'phandle' in props and 'linux,phandle' in props:
            require(props['phandle'] == props['linux,phandle'], 'phandle aliases differ')

    def target(path, key):
        values = tree.cells(path, key)
        require(len(values) == 1 and values[0] in handles, path + ':' + key)
        return handles[values[0]]

    require(40 <= len(raw_fdt) <= 0x100000, 'FDT size')
    compatible('/', 'mediatek,MT6991')
    require('/soc' in tree.nodes and tree.nodes['/soc'].get('ranges') == b'', 'soc ranges')
    for parent in ('/', '/soc'):
        cells(parent, '#address-cells', (2,)); cells(parent, '#size-cells', (2,))
    specs = ((HCI, 'mediatek,mt8183-ufshci', 0x16810000, 0x2a00),
             (PHY, 'mediatek,mt8183-ufsphy', 0x16800000, 0x10000),
             (AO, 'mediatek,mt6991-ufscfg_ao', 0x168a0000, 0x1000),
             (CLK, 'mediatek,mt6991-cksys', 0x10000000, 0x800))
    for path, compat, base, size in specs:
        require(path in tree.nodes, path)
        status(path); compatible(path, compat); cells(path, 'reg', (0, base, 0, size))
    require(target(HCI, 'phys') == PHY, 'PHY reference')
    cells(PHY, '#phy-cells', (0,)); cells(PHY, 'mphy-ver', (1,))
    cells(AO, '#clock-cells', (1,)); cells(CLK, '#clock-cells', (1,))
    require(tree.nodes[HCI].get('clock-names') == b''.join(s.encode() + b'\0' for s in CLOCK_NAMES), 'clock names')
    clocks = tree.cells(HCI, 'clocks')
    ids = (42, 97, 115, 41, 118, 96, 1, 3, 5, 7, 9, 11, 13, 15)
    require(len(clocks) == 28, 'clock count')
    for i, clock_id in enumerate(ids):
        require(handles.get(clocks[2*i]) == (CLK if i < 6 else AO) and clocks[2*i+1] == clock_id, 'clock provider/ID')
    cells('/soc', 'dma-ranges', (0, 0, 0, 0, 0x10, 0))
    for path in ('/', '/soc', HCI):
        require(not any(k in tree.nodes[path] for k in ('dma-coherent', 'dma-noncoherent')), 'unqualified coherency property')
    require('iommus' not in tree.nodes[HCI], 'IOMMU binding')
    require(tree.nodes[HCI].get('mediatek,ufs-rtff-mtcmos') == b'' and
            'mediatek,ufs-support-va09' not in tree.nodes[HCI], 'power recipe')
    require(RESET in tree.nodes, 'reset node'); compatible(RESET, 'ti,syscon-reset'); status(RESET)
    cells(RESET, '#reset-cells', (1,))
    require(tree.nodes[HCI].get('reset-names') == b'unipro_rst\0crypto_rst\0hci_rst\0', 'reset names')
    resets = tree.cells(HCI, 'resets')
    require(len(resets) == 6, 'reset count')
    for i in range(3):
        require(handles.get(resets[i*2]) == RESET and resets[i*2+1] == i+1, 'reset provider/ID')
    bits = tree.cells(RESET, 'ti,reset-bits')
    require(len(bits) == 28, 'reset bits length')
    for i in range(1, 4):
        require(bits[7*i:7*i+7] == (0x148, i-1, 0x14c, i-1, 0, 0, 0x1c), 'reset bit recipe')
    # Preserve the verified always-on rails; this port never programs regulators.
    for key, expected in (('vcc-supply', b'mt6363_vemc\0'), ('dvfsrc-vcore-supply', b'dvfsrc-vcore\0')):
        props = tree.nodes[target(HCI, key)]
        require(props.get('regulator-name') == expected and props.get('regulator-always-on') == b'', key)
    irq = tuple(value for n in (320, *range(329, 337)) for value in (0, n, 4, 0))
    cells(HCI, 'interrupts', irq)  # Inventory only. The initial driver polls, IE=0.
    return struct.pack('<16Q', *RESOURCE_WORDS) + hashlib.sha256(raw_fdt).digest()
