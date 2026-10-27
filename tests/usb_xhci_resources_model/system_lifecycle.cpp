#include "IOKitModel.h"
#include "DaliUsbXhciResources.hpp"
#include "DaliUsbXhciOwner.hpp"
#include "DaliUsbXhciDma.hpp"
#include "DaliUsbHid.h"
#include "DaliUsbStorage.h"
#include "usb_xhci_controller_test_api.h"

#include <array>
#include <cassert>
#include <cstring>

namespace {
constexpr uint32_t kFatTotalSectors = 66584;
constexpr uint32_t kFatReservedSectors = 32;
constexpr uint32_t kFatSectorsPerFat = 513;
constexpr uint32_t kFatCount = 2;
constexpr uint32_t kFatDataStart = kFatReservedSectors + kFatCount * kFatSectorsPerFat;
constexpr uint32_t kFatClusterCount = kFatTotalSectors - kFatDataStart;

static void put_le16(uint8_t *bytes, uint16_t value)
{
    bytes[0] = static_cast<uint8_t>(value);
    bytes[1] = static_cast<uint8_t>(value >> 8);
}

static void put_le32(uint8_t *bytes, uint32_t value)
{
    put_le16(bytes, static_cast<uint16_t>(value));
    put_le16(bytes + 2, static_cast<uint16_t>(value >> 16));
}

static uint16_t get_le16(const uint8_t *bytes)
{
    return static_cast<uint16_t>(bytes[0] | (static_cast<uint16_t>(bytes[1]) << 8));
}

static uint32_t get_le32(const uint8_t *bytes)
{
    return static_cast<uint32_t>(bytes[0]) |
        (static_cast<uint32_t>(bytes[1]) << 8) |
        (static_cast<uint32_t>(bytes[2]) << 16) |
        (static_cast<uint32_t>(bytes[3]) << 24);
}

static std::vector<uint8_t> valid_wire()
{
    std::vector<uint8_t> bytes(DALI_USB_XHCI_RESOURCE_V1_WIRE_BYTES);
    put_le32(bytes.data(), 1);
    put_le32(bytes.data() + 4, static_cast<uint32_t>(DALI_USB_XHCI_RESOURCE_V1_BASE));
    put_le32(bytes.data() + 8, static_cast<uint32_t>(DALI_USB_XHCI_RESOURCE_V1_BASE >> 32));
    put_le32(bytes.data() + 12, static_cast<uint32_t>(DALI_USB_XHCI_RESOURCE_V1_LENGTH));
    put_le32(bytes.data() + 16, static_cast<uint32_t>(DALI_USB_XHCI_RESOURCE_V1_LENGTH >> 32));
    put_le32(bytes.data() + 20, 0);
    put_le32(bytes.data() + 24, 600);
    put_le32(bytes.data() + 28, 4);
    put_le32(bytes.data() + 32, 0);
    return bytes;
}

static void initialize_fat32(std::vector<uint8_t> &media)
{
    media.assign(static_cast<size_t>(kFatTotalSectors) * 512, 0);
    auto *boot = media.data();
    boot[0] = 0xeb; boot[1] = 0x58; boot[2] = 0x90;
    std::memcpy(boot + 3, "DALIUSB ", 8);
    put_le16(boot + 11, 512);
    boot[13] = 1;
    put_le16(boot + 14, static_cast<uint16_t>(kFatReservedSectors));
    boot[16] = static_cast<uint8_t>(kFatCount);
    put_le16(boot + 17, 0);
    put_le16(boot + 19, 0);
    boot[21] = 0xf8;
    put_le16(boot + 22, 0);
    put_le16(boot + 24, 63);
    put_le16(boot + 26, 255);
    put_le32(boot + 28, 0);
    put_le32(boot + 32, kFatTotalSectors);
    put_le32(boot + 36, kFatSectorsPerFat);
    put_le16(boot + 40, 0);
    put_le16(boot + 42, 0);
    put_le32(boot + 44, 2);
    put_le16(boot + 48, 1);
    put_le16(boot + 50, 6);
    boot[64] = 0x80;
    boot[66] = 0x29;
    put_le32(boot + 67, 0x20261028);
    std::memcpy(boot + 71, "RAM FAT32 ", 11);
    std::memcpy(boot + 82, "FAT32   ", 8);
    put_le16(boot + 510, 0xaa55);

    uint8_t *fsInfo = media.data() + 512;
    put_le32(fsInfo, 0x41615252);
    put_le32(fsInfo + 484, 0x61417272);
    put_le32(fsInfo + 488, kFatClusterCount - 2);
    put_le32(fsInfo + 492, 4);
    put_le32(fsInfo + 508, 0xaa550000);
    std::memcpy(media.data() + static_cast<size_t>(6) * 512, boot, 512);

    for (uint32_t fatIndex = 0; fatIndex < kFatCount; ++fatIndex) {
        size_t fatOffset = static_cast<size_t>(kFatReservedSectors + fatIndex * kFatSectorsPerFat) * 512;
        put_le32(media.data() + fatOffset, 0x0ffffff8);
        put_le32(media.data() + fatOffset + 4, 0x0fffffff);
        put_le32(media.data() + fatOffset + 8, 0x0fffffff);  // root directory cluster 2
        put_le32(media.data() + fatOffset + 12, 0x0fffffff); // README.TXT cluster 3
    }

    size_t rootOffset = static_cast<size_t>(kFatDataStart) * 512;
    std::memcpy(media.data() + rootOffset, "README  TXT", 11);
    media[rootOffset + 11] = 0x20;
    put_le16(media.data() + rootOffset + 20, 0);
    put_le16(media.data() + rootOffset + 26, 3);
    constexpr char contents[] = "Dali USB BOT FAT32 RAM read-only\n";
    put_le32(media.data() + rootOffset + 28, static_cast<uint32_t>(sizeof(contents) - 1));
    size_t fileOffset = static_cast<size_t>(kFatDataStart + 1) * 512;
    std::memcpy(media.data() + fileOffset, contents, sizeof(contents) - 1);
}

struct Controller {
    std::vector<uint8_t> media;
    uint8_t keyboardReport[8] = {0, 0, 4, 0, 0, 0, 0, 0};
};

struct CompletionResult { IOReturn result = kIOReturnError; UInt64 bytes = 0; unsigned calls = 0; };
static void complete(void *opaque, void *, IOReturn result, UInt64 bytes)
{
    auto *completion = static_cast<CompletionResult *>(opaque);
    completion->result = result;
    completion->bytes = bytes;
    ++completion->calls;
}

static bool read_sector(IOBlockStorageDevice *storage, uint64_t block, std::array<uint8_t, 512> *bytes)
{
    IOMemoryDescriptor buffer(kIODirectionIn, bytes->size());
    CompletionResult result;
    IOStorageCompletion completion = {&result, nullptr, complete};
    if (storage->doAsyncReadWrite(&buffer, block, 1, nullptr, &completion) != kIOReturnSuccess ||
        result.calls != 1 || result.result != kIOReturnSuccess || result.bytes != bytes->size()) return false;
    return buffer.readBytes(0, bytes->data(), bytes->size()) == bytes->size();
}

static bool read_fat32_file(IOBlockStorageDevice *storage, std::vector<uint8_t> *contents)
{
    // Parse sectors in the test; production msdosfs/VFS is not part of this fixture.
    if (!contents) return false;
    std::array<uint8_t, 512> sector = {};
    if (!read_sector(storage, 0, &sector) || get_le16(sector.data() + 11) != 512 ||
        sector[13] != 1 || sector[16] != 2 || get_le16(sector.data() + 14) == 0 ||
        get_le16(sector.data() + 510) != 0xaa55) return false;
    uint32_t reserved = get_le16(sector.data() + 14);
    uint32_t fats = sector[16];
    uint32_t sectorsPerFat = get_le32(sector.data() + 36);
    uint32_t rootCluster = get_le32(sector.data() + 44);
    uint32_t totalSectors = get_le32(sector.data() + 32);
    uint32_t dataStart = reserved + fats * sectorsPerFat;
    uint32_t clusters = totalSectors - dataStart;
    if (clusters < 65525 || rootCluster < 2 || rootCluster >= clusters + 2 || !sectorsPerFat) return false;

    uint64_t rootSector = static_cast<uint64_t>(dataStart) + (rootCluster - 2);
    if (!read_sector(storage, rootSector, &sector)) return false;
    static constexpr uint8_t fileName[11] = {'R','E','A','D','M','E',' ',' ','T','X','T'};
    uint32_t fileCluster = 0;
    uint32_t fileSize = 0;
    for (size_t offset = 0; offset + 32 <= sector.size(); offset += 32) {
        const uint8_t *entry = sector.data() + offset;
        if (entry[0] == 0) break;
        if (entry[0] == 0xe5 || entry[11] == 0x0f || (entry[11] & 0x18)) continue;
        if (std::memcmp(entry, fileName, sizeof(fileName)) == 0) {
            fileCluster = (static_cast<uint32_t>(get_le16(entry + 20)) << 16) | get_le16(entry + 26);
            fileSize = get_le32(entry + 28);
            break;
        }
    }
    if (fileCluster < 2 || fileCluster >= clusters + 2 || !fileSize) return false;

    contents->clear();
    contents->reserve(fileSize);
    uint32_t cluster = fileCluster;
    for (uint32_t visited = 0; visited < clusters && contents->size() < fileSize; ++visited) {
        uint64_t firstSector = static_cast<uint64_t>(dataStart) + (cluster - 2);
        if (!read_sector(storage, firstSector, &sector)) return false;
        size_t remaining = static_cast<size_t>(fileSize) - contents->size();
        size_t count = std::min(remaining, sector.size());
        contents->insert(contents->end(), sector.begin(), sector.begin() + static_cast<ptrdiff_t>(count));
        if (contents->size() == fileSize) return true;
        uint64_t fatEntryOffset = static_cast<uint64_t>(cluster) * 4;
        uint64_t fatSector = reserved + fatEntryOffset / 512;
        if (!read_sector(storage, fatSector, &sector)) return false;
        uint32_t next = get_le32(sector.data() + static_cast<size_t>(fatEntryOffset % 512)) & 0x0fffffff;
        if (next < 2 || next >= 0x0ffffff8) return false;
        cluster = next;
    }
    return false;
}

static void test_owner_host_children_and_readonly_fat32()
{
    IOService fdtProvider;
    auto wire = valid_wire();
    OSData resourceProperty(wire.data(), wire.size());
    static constexpr char compatible[] = "mediatek,mtk-xhci";
    OSData compatibleProperty(compatible, sizeof(compatible));
    fdtProvider.setTestProperty("resources-v1", &resourceProperty);
    fdtProvider.setTestProperty("compatible", &compatibleProperty);

    DaliUsbXhciResources resources;
    assert(resources.start(&fdtProvider));
    DaliUsbXhciOwner owner;
    assert(owner.start(&resources));
    assert(owner.hasMappedWindow()); // production owner maps resources without touching registers

    dali_usb_xhci_dma_context dmaContext = {};
    assert(dali_usb_xhci_dma_init(&dmaContext, &owner));
    usb_dma_ops dmaOps = {};
    assert(dali_usb_xhci_dma_get_ops(&dmaContext, &dmaOps));

    Controller controller;
    initialize_fat32(controller.media);
    auto *xhci = usb_xhci_test_host_create(controller.media.data(), controller.media.size(),
                                            controller.keyboardReport, &dmaOps);
    assert(xhci);
    usb_host_ops ops = {};
    assert(usb_xhci_test_host_get_ops(xhci, &ops) == USB_OK);
    usb_host_route route = {};
    route.root_port = 1;
    usb_host_device device = {};
    int enumeration = usb_host_enumerate(&device, &ops, &route, USB_HOST_FULL_SPEED, 5);
    if (enumeration != USB_OK) {
        usb_xhci_test_host_counters failedCounters = {};
        (void)usb_xhci_test_host_get_counters(xhci, &failedCounters);
        std::fprintf(stderr, "xHCI composite enumeration failed: result=%d state=%d control=%u "
                     "protocol=%u capacity=%u\n", enumeration, static_cast<int>(device.state),
                     failedCounters.host_control_requests, failedCounters.hid_protocol_selections,
                     failedCounters.bot_capacity_commands);
    }
    assert(enumeration == USB_OK);
    assert(device.hid_count == 1 && device.hid[0].kind == USB_HOST_HID_KEYBOARD);
    assert(device.has_bot && device.bot.present);
    assert(usb_host_start_storage(&device) == USB_OK);

    IOService *hid = dali_usb_hid_publish(&owner, &device, 0);
    IOService *storageService = dali_usb_storage_publish(&owner, &device.bot);
    auto *storage = dynamic_cast<IOBlockStorageDevice *>(storageService);
    assert(hid && storage && storage->registered);
    bool readOnly = false;
    assert(storage->reportWriteProtection(&readOnly) == kIOReturnSuccess && readOnly);

    uint8_t report[8] = {};
    size_t reportBytes = 0;
    assert(usb_host_read_boot_report(&device, 0, report, sizeof(report), &reportBytes, 1000) == USB_OK);
    assert(reportBytes == sizeof(report));
    assert(dali_usb_hid_submit(hid, report, reportBytes) == USB_OK);
    assert(!gIOKitHidReports.empty() && gIOKitHidReports.back() ==
           std::vector<uint8_t>(controller.keyboardReport, controller.keyboardReport + sizeof(report)));

    std::vector<uint8_t> file;
    assert(read_fat32_file(storage, &file));
    static constexpr char expected[] = "Dali USB BOT FAT32 RAM read-only\n";
    assert(file.size() == sizeof(expected) - 1);
    assert(std::memcmp(file.data(), expected, sizeof(expected) - 1) == 0);
    usb_xhci_test_host_counters counters = {};
    assert(usb_xhci_test_host_get_counters(xhci, &counters) == USB_OK);
    assert(counters.hid_protocol_selections == 1 && counters.hid_interrupts == 1);
    assert(counters.bot_capacity_commands == 1 && counters.bot_read_only_commands == 1);
    assert(counters.bot_read_commands >= 3 && counters.bot_write_commands == 0);

    IOMemoryDescriptor attemptedWrite(kIODirectionOut, 512);
    CompletionResult writeCompletion;
    IOStorageCompletion completion = {&writeCompletion, nullptr, complete};
    assert(storage->doAsyncReadWrite(&attemptedWrite, 1058, 1, nullptr, &completion) == kIOReturnNotWritable);
    assert(usb_xhci_test_host_get_counters(xhci, &counters) == USB_OK &&
           counters.bot_write_commands == 0 && writeCompletion.calls == 0);

    assert(dali_usb_hid_quiesce(hid) == USB_OK);
    assert(gIOKitHidReports.back() == std::vector<uint8_t>(8, 0));
    assert(dali_usb_storage_quiesce(storageService) == USB_OK);
    assert(usb_xhci_test_host_get_counters(xhci, &counters) == USB_OK &&
           counters.bot_sync_commands == 1);
    assert(dali_usb_hid_detach(hid, false) == USB_BUSY);
    assert(dali_usb_storage_detach(storageService, false) == USB_BUSY);
    assert(usb_host_stop_device(&device) == USB_OK && device.stop_proven);
    assert(dali_usb_hid_detach(hid, device.stop_proven) == USB_OK);
    assert(dali_usb_storage_detach(storageService, device.stop_proven) == USB_OK);
    assert(usb_host_release_stopped_device(&device, nullptr, nullptr, nullptr) == USB_OK);
    assert(device.state == USB_HOST_EMPTY);
    assert(dali_usb_hid_terminate(hid) == USB_OK);
    assert(dali_usb_storage_terminate(storageService) == USB_OK);
    hid->release();
    storageService->release();

    assert(usb_xhci_test_host_stop(xhci) == USB_OK);
    assert(usb_xhci_test_host_destroy(xhci) == USB_OK);
    assert(dali_usb_xhci_dma_stop(&dmaContext));
    assert(gIOKitDmaModel.memory_allocations == gIOKitDmaModel.memory_releases);
    assert(gIOKitDmaModel.command_allocations == gIOKitDmaModel.command_releases);
    assert(gIOKitDmaModel.active_prepares == 0);
    owner.stop(&resources);
    assert(!owner.hasMappedWindow() && gIOKitLockCount == 0);
}
} // namespace

int main()
{
    test_owner_host_children_and_readonly_fat32();
    std::puts("PASS: production xHCI controller/host adapter with synthetic MMIO, Dali DMA owner, HID/BOT children, and RAM FAT32-image read-only sector path; msdosfs runtime and hardware qualification unverified");
    return 0;
}
