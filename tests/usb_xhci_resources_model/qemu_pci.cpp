#include "DaliUsbQemuPci.h"

#include <cassert>
#include <cstdio>
#include <cstring>

static void put_le32(uint8_t *bytes, uint32_t value)
{
    bytes[0] = static_cast<uint8_t>(value);
    bytes[1] = static_cast<uint8_t>(value >> 8);
    bytes[2] = static_cast<uint8_t>(value >> 16);
    bytes[3] = static_cast<uint8_t>(value >> 24);
}

static void put_le64(uint8_t *bytes, uint64_t value)
{
    put_le32(bytes, static_cast<uint32_t>(value));
    put_le32(bytes + 4, static_cast<uint32_t>(value >> 32));
}

struct HostBytes {
    uint8_t reg[16] = {};
    uint8_t bus_range[8] = {};
    uint8_t ranges[DALI_QEMU_PCI_WINDOW_COUNT * 7u * sizeof(uint32_t)] = {};
};

static HostBytes valid_host_bytes()
{
    HostBytes bytes = {};
    put_le64(bytes.reg, 0x4010000000ULL);
    put_le64(bytes.reg + 8, 0x10000000ULL);
    put_le32(bytes.bus_range, 0);
    put_le32(bytes.bus_range + 4, 255);
    const uint32_t cells[21] = {
        0x01000000u, 0, 0, 0, 0x3eff0000u, 0, 0x10000u,
        0x02000000u, 0, 0x10000000u, 0, 0x10000000u, 0, 0x2eff0000u,
        0x03000000u, 0x80u, 0, 0x80u, 0, 0x80u, 0,
    };
    for (size_t i = 0; i < 21; ++i) put_le32(bytes.ranges + i * 4u, cells[i]);
    return bytes;
}

struct ConfigModel {
    uint8_t xhci_device = 2;
    uint8_t xhci_function = 0;
    bool multi_function = false;
    bool duplicate = false;
    bool fail_device_read = false;
    uint8_t config[256] = {};
    uint8_t second_config[256] = {};
    uint32_t read_count = 0;
};

static void make_xhci_config(uint8_t *config, uint64_t bar_base)
{
    std::memset(config, 0, 256);
    config[0] = 0x36;
    config[1] = 0x1b;
    config[2] = 0x0d;
    config[3] = 0x00;
    config[4] = 0x06;
    config[9] = 0x30;
    config[10] = 0x03;
    config[11] = 0x0c;
    const uint32_t low = static_cast<uint32_t>(bar_base) | 0x4u;
    put_le32(config + 0x10, low);
    put_le32(config + 0x14, static_cast<uint32_t>(bar_base >> 32));
}

static bool read_function(void *opaque, uint8_t bus, uint8_t device,
                          uint8_t function, uint8_t config[256])
{
    auto *model = static_cast<ConfigModel *>(opaque);
    ++model->read_count;
    if (model->fail_device_read && device == 7) return false;
    std::memset(config, 0xff, 256);
    if (bus != 0) return true;
    if (model->multi_function && device == model->xhci_device && function == 0) {
        std::memset(config, 0, 256);
        config[0] = 0x34;
        config[1] = 0x12;
        config[0x0e] = 0x80;
        return true;
    }
    if (device == model->xhci_device && function == model->xhci_function) {
        std::memcpy(config, model->config, 256);
        return true;
    }
    if (model->duplicate && device == 5 && function == 0) {
        std::memcpy(config, model->second_config, 256);
        return true;
    }
    return true;
}

static dali_qemu_pci_host decode_valid_host(const HostBytes &bytes)
{
    dali_qemu_pci_host host = {};
    assert(dali_qemu_pci_decode_host(bytes.reg, sizeof(bytes.reg),
        bytes.bus_range, sizeof(bytes.bus_range), bytes.ranges, sizeof(bytes.ranges),
        &host) == DALI_QEMU_PCI_OK);
    assert(host.ecam_base == 0x4010000000ULL && host.ecam_length == 0x10000000ULL);
    assert(host.bus_first == 0 && host.bus_last == 255);
    assert(host.windows[DALI_QEMU_PCI_MEM32_WINDOW - 1].pci_base == 0x10000000ULL);
    assert(host.windows[DALI_QEMU_PCI_MEM32_WINDOW - 1].cpu_base == 0x10000000ULL);
    return host;
}

static void test_host_decode()
{
    const HostBytes bytes = valid_host_bytes();
    const auto host = decode_valid_host(bytes);

    dali_qemu_pci_host output = {};
    output.ecam_base = 0xfeedfaceu;
    assert(dali_qemu_pci_decode_host(bytes.reg, sizeof(bytes.reg) - 1u,
        bytes.bus_range, sizeof(bytes.bus_range), bytes.ranges, sizeof(bytes.ranges),
        &output) == DALI_QEMU_PCI_INVALID);
    assert(output.ecam_base == 0xfeedfaceu);

    HostBytes shortEcam = bytes;
    put_le64(shortEcam.reg + 8, 0x100000ULL);
    assert(dali_qemu_pci_decode_host(shortEcam.reg, sizeof(shortEcam.reg),
        shortEcam.bus_range, sizeof(shortEcam.bus_range), shortEcam.ranges,
        sizeof(shortEcam.ranges), &output) == DALI_QEMU_PCI_RANGE);

    HostBytes overlapCpu = bytes;
    put_le32(overlapCpu.ranges + 7u * 4u + 16u, 0x3eff0000u);
    assert(dali_qemu_pci_decode_host(overlapCpu.reg, sizeof(overlapCpu.reg),
        overlapCpu.bus_range, sizeof(overlapCpu.bus_range), overlapCpu.ranges,
        sizeof(overlapCpu.ranges), &output) == DALI_QEMU_PCI_RANGE);

    HostBytes overlapPci = bytes;
    put_le32(overlapPci.ranges + 7u * 4u + 8u, 0);
    assert(dali_qemu_pci_decode_host(overlapPci.reg, sizeof(overlapPci.reg),
        overlapPci.bus_range, sizeof(overlapPci.bus_range), overlapPci.ranges,
        sizeof(overlapPci.ranges), &output) == DALI_QEMU_PCI_RANGE);
    (void)host;
}

static void test_function_discovery()
{
    const auto host = decode_valid_host(valid_host_bytes());
    ConfigModel model = {};
    model.xhci_device = 1;
    make_xhci_config(model.config, 0x10000000ULL);
    dali_qemu_pci_xhci function = {};
    function.device = 0xff;
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_OK);
    assert(function.bus == 0 && function.device == 1 && function.function == 0);
    assert(function.vendor_id == 0x1b36u && function.device_id == 0x000du);
    assert(function.class_code == 0x0c0330u && function.command == 6u);
    assert(function.bar0_pci_base == 0x10000000ULL);
    assert(function.bar0_cpu_base == 0x10000000ULL);
    assert(function.bar0_length == 0x4000u);
    assert(function.device == 1 && model.read_count == 32u);

    model = {};
    model.xhci_device = 2;
    make_xhci_config(model.config, 0x10010000ULL);
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_OK);
    assert(function.device == 2 && function.bar0_pci_base == 0x10010000ULL);

    model = {};
    model.xhci_device = 2;
    make_xhci_config(model.config, 0x8000004000ULL);
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_OK);
    assert(function.bar0_pci_base == 0x8000004000ULL);
    assert(function.bar0_cpu_base == 0x8000004000ULL);

    model = {};
    model.xhci_device = 2;
    make_xhci_config(model.config, 0xffffffc000ULL);
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_OK);
    assert(function.bar0_pci_base == 0xffffffc000ULL);

    model = {};
    model.xhci_device = 2;
    make_xhci_config(model.config, 0x10000000000ULL);
    function.device = 0xee;
    function.bar0_cpu_base = 0xfeedfaceULL;
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_RANGE);
    assert(function.device == 0xee && function.bar0_cpu_base == 0xfeedfaceULL);

    model = {};
    model.xhci_device = 2;
    model.xhci_function = 1;
    model.multi_function = true;
    make_xhci_config(model.config, 0x10010000ULL);
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_OK);
    assert(function.device == 2 && function.function == 1);

    model = {};
    model.xhci_device = 31;
    model.xhci_function = 7;
    model.multi_function = true;
    make_xhci_config(model.config, 0x8000004000ULL);
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_OK);
    assert(function.device == 31 && function.function == 7);

    model = {};
    model.xhci_device = 2;
    make_xhci_config(model.config, 0x10010000ULL);
    make_xhci_config(model.second_config, 0x10014000ULL);
    model.duplicate = true;
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_AMBIGUOUS);

    model = {};
    model.xhci_device = 2;
    model.fail_device_read = true;
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_IO);
}

static void test_bar_refusal()
{
    const auto host = decode_valid_host(valid_host_bytes());
    ConfigModel model = {};
    model.xhci_device = 2;
    make_xhci_config(model.config, 0x10010000ULL);
    dali_qemu_pci_xhci function = {};

    make_xhci_config(model.config, 0);
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_RANGE);

    make_xhci_config(model.config, 0x10011000ULL);
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_RANGE);

    make_xhci_config(model.config, 0x50000000ULL);
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_RANGE);

    make_xhci_config(model.config, 0x10010000ULL);
    put_le32(model.config + 0x10, 0x10010000u);
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_UNSUPPORTED);

    make_xhci_config(model.config, 0x10010000ULL);
    put_le32(model.config + 0x10, 0x1001000cu);
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_UNSUPPORTED);

    make_xhci_config(model.config, 0x10010000ULL);
    model.config[0x0b] = 0x01;
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_UNSUPPORTED);

    model = {};
    assert(dali_qemu_pci_find_xhci(&host, read_function, &model, &function) ==
        DALI_QEMU_PCI_NOT_FOUND);
}

int main()
{
    test_host_decode();
    test_function_discovery();
    test_bar_refusal();
    std::puts("PASS: QEMU PCI ECAM decode, read-only xHCI discovery and assigned BAR translation");
    return 0;
}
