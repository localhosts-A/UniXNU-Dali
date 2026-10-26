// Production DaliUsbStorage and USB host code with a test-only IOKit/BOT model.
#include "IOKitModel.h"
#include "DaliUsbStorage.h"
#include "UsbHost.h"
#include <cstdio>

static const uint8_t deviceDescriptor[18] = {
    18, 1, 0, 2, 0, 0, 0, 64, 0x34, 0x12, 0x78, 0x56, 0, 1, 0, 0, 0, 1
};
static const uint8_t configurationDescriptor[32] = {
    9, 2, 32, 0, 1, 1, 0, 0x80, 50,
    9, 4, 0, 0, 2, 8, 6, 0x50, 0,
    7, 5, 0x81, 2, 64, 0, 0, 7, 5, 0x02, 2, 64, 0, 0
};

struct Controller {
    uint8_t media[4096] = {};
    uint32_t tag = 0, transferSize = 0, lba = 0;
    unsigned phase = 0, stopCalls = 0, syncCalls = 0;
    uint8_t opcode = 0;
    bool slotOwned = false, failStop = false, failSyncOnce = false;
};
struct Completion {
    unsigned calls = 0;
    IOReturn result = -1;
    UInt64 bytes = 0;
};

static uint32_t le32(const uint8_t *bytes)
{
    return static_cast<uint32_t>(bytes[0]) | static_cast<uint32_t>(bytes[1]) << 8 |
           static_cast<uint32_t>(bytes[2]) << 16 | static_cast<uint32_t>(bytes[3]) << 24;
}
static uint32_t be32(const uint8_t *bytes)
{
    return static_cast<uint32_t>(bytes[0]) << 24 | static_cast<uint32_t>(bytes[1]) << 16 |
           static_cast<uint32_t>(bytes[2]) << 8 | static_cast<uint32_t>(bytes[3]);
}
static void putLe32(uint8_t *bytes, uint32_t value)
{
    for (unsigned i = 0; i < 4; ++i) bytes[i] = static_cast<uint8_t>(value >> (i * 8));
}
static void putBe32(uint8_t *bytes, uint32_t value)
{
    for (unsigned i = 0; i < 4; ++i) bytes[i] = static_cast<uint8_t>(value >> ((3 - i) * 8));
}

static int control(void *context, const usb_host_route *route, uint8_t address,
                   const usb_setup *setup, void *data, size_t capacity,
                   size_t *actual, uint32_t timeout)
{
    auto *controller = static_cast<Controller *>(context);
    assert(controller && route && route->root_port == 1 && setup && actual && timeout);
    *actual = 0;
    if (setup->type == 0x80 && setup->request == 6) {
        const uint8_t *source = nullptr;
        size_t available = 0;
        if ((setup->value >> 8) == 1 && (address == 0 || address == 5)) {
            source = deviceDescriptor;
            available = sizeof(deviceDescriptor);
        } else if ((setup->value >> 8) == 2 && address == 5) {
            source = configurationDescriptor;
            available = sizeof(configurationDescriptor);
        }
        assert(source && data && setup->length <= available && setup->length <= capacity);
        std::memcpy(data, source, setup->length);
        *actual = setup->length;
        return USB_OK;
    }
    assert(setup->type == 0 && setup->request == 9 && address == 5 &&
           setup->value == 1 && !setup->length);
    return USB_OK;
}

static int addressDevice(void *context, const usb_host_route *route, usb_host_speed speed,
                         uint8_t ep0, uint8_t preferred, uint8_t *assigned)
{
    auto *controller = static_cast<Controller *>(context);
    assert(route && route->root_port == 1 && speed == USB_HOST_FULL_SPEED &&
           ep0 == 64 && preferred == 5 && assigned && !controller->slotOwned);
    controller->slotOwned = true;
    *assigned = preferred;
    return USB_OK;
}

static int configureEndpoint(void *context, const usb_host_route *route, uint8_t address,
                             usb_host_speed speed, const usb_endpoint *endpoint)
{
    auto *controller = static_cast<Controller *>(context);
    assert(controller->slotOwned && route && address == 5 &&
           speed == USB_HOST_FULL_SPEED && endpoint && endpoint->type == 2);
    return USB_OK;
}

static int bulk(void *context, const usb_host_route *route, uint8_t address,
                uint8_t endpoint, void *buffer, size_t length, size_t *actual,
                uint32_t timeout)
{
    auto *controller = static_cast<Controller *>(context);
    auto *bytes = static_cast<uint8_t *>(buffer);
    assert(controller->slotOwned && route && address == 5 && actual && timeout && bytes);
    *actual = 0;
    if (controller->phase == 0) {
        assert(endpoint == 0x02 && length == 31 && le32(bytes) == 0x43425355);
        controller->tag = le32(bytes + 4);
        controller->transferSize = le32(bytes + 8);
        controller->opcode = bytes[15];
        if (controller->opcode == 0x28 || controller->opcode == 0x2a) {
            controller->lba = be32(bytes + 17);
            assert(controller->lba < 8 && controller->transferSize == 512 &&
                   bytes[22] == 0 && bytes[23] == 1);
        } else if (controller->opcode == 0x35) {
            assert(!controller->transferSize);
            ++controller->syncCalls;
        } else {
            assert(controller->opcode == 0x25 || controller->opcode == 0x1a);
        }
        controller->phase = controller->transferSize ? 1 : 2;
    } else if (controller->phase == 1) {
        assert(length == controller->transferSize);
        if (controller->opcode == 0x25) {
            assert(endpoint == 0x81 && length == 8);
            putBe32(bytes, 7);
            putBe32(bytes + 4, 512);
        } else if (controller->opcode == 0x1a) {
            assert(endpoint == 0x81 && length == 4);
            std::memset(bytes, 0, length);
            bytes[0] = 3;
        } else if (controller->opcode == 0x28) {
            assert(endpoint == 0x81);
            std::memcpy(bytes, controller->media + controller->lba * 512, length);
        } else {
            assert(controller->opcode == 0x2a && endpoint == 0x02);
            std::memcpy(controller->media + controller->lba * 512, bytes, length);
        }
        controller->phase = 2;
    } else {
        assert(controller->phase == 2 && endpoint == 0x81 && length == 13);
        std::memset(bytes, 0, length);
        putLe32(bytes, 0x53425355);
        putLe32(bytes + 4, controller->tag);
        if (controller->opcode == 0x35 && controller->failSyncOnce) {
            bytes[12] = 1;
            controller->failSyncOnce = false;
        }
        controller->phase = 0;
    }
    *actual = length;
    return USB_OK;
}

static int disableDevice(void *context, const usb_host_route *route, uint8_t address)
{
    auto *controller = static_cast<Controller *>(context);
    assert(route && address == 5 && controller->slotOwned);
    ++controller->stopCalls;
    if (controller->failStop) return USB_TIMEOUT;
    controller->slotOwned = false;
    return USB_OK;
}

static usb_host_ops hostOps(Controller *controller)
{
    usb_host_ops ops = {};
    ops.context = controller;
    ops.control = control;
    ops.address_device = addressDevice;
    ops.configure_endpoint = configureEndpoint;
    ops.bulk = bulk;
    ops.disable_device = disableDevice;
    return ops;
}

static void complete(void *target, void *, IOReturn result, UInt64 bytes)
{
    auto *capture = static_cast<Completion *>(target);
    ++capture->calls;
    capture->result = result;
    capture->bytes = bytes;
}

static void enumerate(Controller &controller, usb_host_device &device)
{
    usb_host_ops ops = hostOps(&controller);
    usb_host_route route = {};
    route.root_port = 1;
    assert(usb_host_enumerate(&device, &ops, &route, USB_HOST_FULL_SPEED, 5) == USB_OK);
    assert(usb_host_start_storage(&device) == USB_OK && device.bot.present);
}

static void testStorageLifecycle()
{
    IOService provider;
    Controller controller;
    usb_host_device device = {};
    enumerate(controller, device);
    IOService *service = dali_usb_storage_publish(&provider, &device.bot);
    auto *storage = dynamic_cast<IOBlockStorageDevice *>(service);
    assert(storage && storage->registered && modelLocks == 1 && modelBuffers == 1);

    UInt64 blockSize = 0, lastBlock = 0;
    bool present = false, changed = true, readOnly = true;
    assert(storage->reportBlockSize(&blockSize) == kIOReturnSuccess && blockSize == 512);
    assert(storage->reportMaxValidBlock(&lastBlock) == kIOReturnSuccess && lastBlock == 7);
    assert(storage->reportMediaState(&present, &changed) == kIOReturnSuccess && present && !changed);
    assert(storage->reportWriteProtection(&readOnly) == kIOReturnSuccess && !readOnly);

    IOMemoryDescriptor output(kIODirectionOut, 512), input(kIODirectionIn, 512);
    for (size_t i = 0; i < output.bytes.size(); ++i) output.bytes[i] = static_cast<uint8_t>(i ^ 0x5a);
    Completion completion;
    IOStorageCompletion ioCompletion = {&completion, nullptr, complete};
    assert(storage->doAsyncReadWrite(&output, 2, 1, nullptr, &ioCompletion) == kIOReturnSuccess);
    assert(completion.calls == 1 && completion.result == kIOReturnSuccess && completion.bytes == 512);
    assert(storage->doAsyncReadWrite(&input, 2, 1, nullptr, &ioCompletion) == kIOReturnSuccess);
    assert(completion.calls == 2 && completion.result == kIOReturnSuccess &&
           completion.bytes == 512 && input.bytes == output.bytes);

    controller.failSyncOnce = true;
    assert(dali_usb_storage_quiesce(service) == USB_COMMAND_FAILED);
    assert(storage->doAsyncReadWrite(&input, 2, 1, nullptr, &ioCompletion) == kIOReturnSuccess);
    assert(completion.calls == 3 && completion.result == kIOReturnNotReady && completion.bytes == 0);
    assert(dali_usb_storage_quiesce(service) == USB_OK && controller.syncCalls == 2);
    assert(dali_usb_storage_detach(service, false) == USB_BUSY);
    assert(dali_usb_storage_terminate(service) == USB_BUSY);
    assert(usb_host_release_stopped_device(&device, nullptr, nullptr, nullptr) == USB_BUSY);

    controller.failStop = true;
    assert(usb_host_stop_device(&device) == USB_TIMEOUT);
    assert(device.state == USB_HOST_FAULTED && !device.stop_proven &&
           device.bot.present && controller.slotOwned);
    assert(dali_usb_storage_detach(service, false) == USB_BUSY);
    assert(dali_usb_storage_terminate(service) == USB_BUSY);
    controller.failStop = false;
    assert(usb_host_stop_device(&device) == USB_OK && device.stop_proven);
    assert(device.bot.present && !controller.slotOwned && controller.stopCalls == 2);
    assert(dali_usb_storage_detach(service, device.stop_proven) == USB_OK);
    assert(!device.bot.present && dali_usb_storage_detach(service, true) == USB_OK);
    assert(usb_host_release_stopped_device(&device, nullptr, nullptr, nullptr) == USB_OK);
    assert(device.state == USB_HOST_EMPTY);
    assert(storage->reportMediaState(&present, &changed) == kIOReturnSuccess && !present);
    assert(storage->doAsyncReadWrite(&input, 2, 1, nullptr, &ioCompletion) == kIOReturnSuccess);
    assert(completion.calls == 4 && completion.result == kIOReturnNotReady && completion.bytes == 0);
    assert(storage->doSynchronize(0, 0, 0) == kIOReturnNotReady);
    assert(dali_usb_storage_terminate(service) == USB_OK);
    service->release();
    assert(!modelLocks && !modelBuffers);
}

static void testAllocationFailure()
{
    IOService provider;
    Controller controller;
    usb_host_device device = {};
    enumerate(controller, device);
    modelFailBufferAllocation = true;
    assert(!dali_usb_storage_publish(&provider, &device.bot));
    assert(!modelFailBufferAllocation && !modelLocks && !modelBuffers && device.bot.present);
    assert(usb_host_stop_device(&device) == USB_OK);
    assert(usb_host_release_stopped_device(&device, nullptr, nullptr, nullptr) == USB_OK);
}

int main()
{
    const int baseline = modelLiveObjects;
    testStorageLifecycle();
    testAllocationFailure();
    assert(modelLiveObjects == baseline && !modelLocks && !modelBuffers);
    std::puts("PASS: production USB storage child lifecycle with in-memory BOT/IOKit model; runtime unqualified");
}
