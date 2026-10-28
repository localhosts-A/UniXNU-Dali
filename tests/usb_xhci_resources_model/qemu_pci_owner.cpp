#include "IOKitModel.h"
#include "DaliUsbQemuPciOwner.hpp"
#include "DaliUsbQemuXhciRuntime.hpp"

#include <cassert>
#include <cstdio>
#include <memory>
#include <string>

static constexpr uint64_t kEcamBase = 0x4010000000ull;
static constexpr uint64_t kEcamLength = 0x10000000ull;
static constexpr uint64_t kBarBase = 0x8000000000ull;
static constexpr uint64_t kBusBytes = 0x100000ull;

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

static void put_config16(uint8_t *bytes, size_t offset, uint16_t value)
{
    bytes[offset] = static_cast<uint8_t>(value);
    bytes[offset + 1u] = static_cast<uint8_t>(value >> 8);
}

struct HostFixture {
    IOService host;
    IORegistryEntry pciHost;
    std::vector<std::unique_ptr<OSData>> property_storage;
    std::vector<uint8_t> compatible = {
        'q', 'e', 'm', 'u', ',', 'v', 'i', 'r', 't', '-', 'p', 'c', 'i', 'e', '-',
        'h', 'o', 's', 't', 0, 'p', 'c', 'i', '-', 'h', 'o', 's', 't', '-', 'e',
        'c', 'a', 'm', '-', 'g', 'e', 'n', 'e', 'r', 'i', 'c', 0};
    std::vector<uint8_t> device_type = {'p', 'c', 'i', 0};
    std::vector<uint8_t> address_cells = {3, 0, 0, 0};
    std::vector<uint8_t> size_cells = {2, 0, 0, 0};
    std::vector<uint8_t> reg = std::vector<uint8_t>(16);
    std::vector<uint8_t> bus_range = std::vector<uint8_t>(8);
    std::vector<uint8_t> ranges = std::vector<uint8_t>(21u * sizeof(uint32_t));
    std::vector<uint8_t> coherent;
    std::vector<uint8_t> arm_io_device_type = {'q', 'e', 'm', 'u', '-', 'v', 'i', 'r', 't', 0};

    HostFixture()
    {
        put_le64(reg.data(), kEcamBase);
        put_le64(reg.data() + 8, kEcamLength);
        put_le32(bus_range.data(), 0);
        put_le32(bus_range.data() + 4, 255);
        const uint32_t cells[] = {
            0x01000000u, 0, 0, 0, 0x3eff0000u, 0, 0x00010000u,
            0x02000000u, 0, 0x10000000u, 0, 0x10000000u, 0, 0x2eff0000u,
            0x03000000u, 0x80u, 0, 0x80u, 0, 0x80u, 0,
        };
        for (size_t index = 0; index < 21; ++index)
            put_le32(ranges.data() + index * sizeof(uint32_t), cells[index]);

        addArmIoProperty("device_type", arm_io_device_type);
        addProperty("compatible", compatible);
        addProperty("device_type", device_type);
        addProperty("#address-cells", address_cells);
        addProperty("#size-cells", size_cells);
        addProperty("reg", reg);
        addProperty("bus-range", bus_range);
        addProperty("ranges", ranges);
        addProperty("dma-coherent", coherent);
        IORegistryEntry::setTestPath("/arm-io/qemu-pcie-host", &pciHost);

        auto &ecam = gIOKitMemoryModel.window(kEcamBase, kBusBytes);
        std::fill(ecam.begin(), ecam.end(), 0xffu);
        uint8_t *config = ecam.data() + 2u * 0x8000u;
        put_config16(config, 0x00u, 0x1b36u);
        put_config16(config, 0x02u, 0x000du);
        put_config16(config, 0x04u, (1u << 1) | (1u << 2));
        config[0x09u] = 0x30u;
        config[0x0au] = 0x03u;
        config[0x0bu] = 0x0cu;
        config[0x0eu] = 0;
        put_le32(config + 0x10u, 0x00000004u);
        put_le32(config + 0x14u, 0x00000080u);
    }

    void addProperty(const char *key, const std::vector<uint8_t> &value)
    {
        auto property = std::make_unique<OSData>(value.data(), value.size());
        pciHost.setTestProperty(key, property.get());
        property_storage.push_back(std::move(property));
    }

    void addArmIoProperty(const char *key, const std::vector<uint8_t> &value)
    {
        auto property = std::make_unique<OSData>(value.data(), value.size());
        host.setTestProperty(key, property.get());
        property_storage.push_back(std::move(property));
    }

    ~HostFixture()
    {
        IORegistryEntry::removeTestPath("/arm-io/qemu-pcie-host", &pciHost);
    }

    uint8_t *xhciConfig()
    {
        auto found = gIOKitMemoryModel.mapped_windows.find(kEcamBase);
        assert(found != gIOKitMemoryModel.mapped_windows.end());
        return found->second.data() + 2u * 0x8000u;
    }
};

struct GuardState {
    int result = USB_OK;
    unsigned calls = 0;
};

static int register_guard(void *opaque)
{
    auto *state = static_cast<GuardState *>(opaque);
    if (!state) return USB_INVALID;
    ++state->calls;
    return state->result;
}

struct CoreRuntimeModel {
    int capability_result = USB_OK;
    int start_result = USB_OK;
    int acknowledge_result = USB_OK;
    unsigned stop_failures = 0;
    unsigned capability_calls = 0;
    unsigned start_calls = 0;
    unsigned stop_calls = 0;
    unsigned status_calls = 0;
    unsigned acknowledge_calls = 0;
    unsigned reset_calls = 0;
    unsigned event_reads = 0;
    unsigned enumerate_calls = 0;
    unsigned storage_start_calls = 0;
    unsigned hid_publish_calls = 0;
    unsigned storage_publish_calls = 0;
    unsigned hub_start_calls = 0;
    unsigned hub_poll_calls = 0;
    unsigned hub_stop_calls = 0;
    unsigned host_stop_calls = 0;
    unsigned host_release_calls = 0;
    unsigned hid_quiesce_calls = 0;
    unsigned hid_detach_calls = 0;
    unsigned hid_terminate_calls = 0;
    unsigned storage_quiesce_calls = 0;
    unsigned storage_detach_calls = 0;
    unsigned storage_terminate_calls = 0;
    bool root_device_is_hub = false;
    bool hub_child_attached = false;
    unsigned enumeration_fail_call = 0;
    int storage_quiesce_result = USB_OK;
    struct usb_xhci_host_adapter *adapter = nullptr;
    struct usb_host_device *last_enumerated[8] = {};
    struct usb_xhci_event pending_events[16] = {};
    size_t pending_event_count = 0;
    uint8_t max_slots = 8;
    uint8_t max_ports = 4;
    uint8_t usb2_port_count = 2;
    uint8_t usb2_ports[USB_XHCI_PORT_BITMAP_BYTES] = {0x03};
    struct usb_xhci_root_port_status ports[256] = {};
    struct usb_dma_buffer schedule_buffer = {};
};

static CoreRuntimeModel gCoreRuntimeModel;

extern "C" int usb_xhci_read_capabilities(
    const struct usb_mmio *mmio, struct usb_xhci_capabilities *output)
{
    ++gCoreRuntimeModel.capability_calls;
    uint32_t capability = 0;
    if (!mmio || !output || !mmio->read32 || !mmio->context ||
        mmio->read32(mmio->context, 0, &capability) != USB_OK)
        return USB_INVALID;
    if (gCoreRuntimeModel.capability_result != USB_OK)
        return gCoreRuntimeModel.capability_result;
    struct usb_xhci_capabilities candidate = {};
    candidate.max_slots = gCoreRuntimeModel.max_slots;
    candidate.max_ports = gCoreRuntimeModel.max_ports;
    candidate.usb2_port_count = gCoreRuntimeModel.usb2_port_count;
    for (size_t index = 0; index < USB_XHCI_PORT_BITMAP_BYTES; ++index)
        candidate.usb2_ports[index] = gCoreRuntimeModel.usb2_ports[index];
    *output = candidate;
    return USB_OK;
}

extern "C" int usb_xhci_controller_start(
    struct usb_xhci_controller *controller, const struct usb_mmio *mmio,
    const struct usb_dma_ops *dma, int (*guard)(void *), void *guard_context,
    uint8_t slots, uint32_t, uint32_t)
{
    ++gCoreRuntimeModel.start_calls;
    if (!controller || !mmio || !dma || !guard || !slots ||
        guard(guard_context) != USB_OK) return USB_INVALID;
    *controller = {};
    if (gCoreRuntimeModel.start_result != USB_OK) {
        controller->state = USB_XHCI_CONTROLLER_FAULTED;
        controller->guard = guard;
        controller->guard_context = guard_context;
        controller->mmio = *mmio;
        controller->dma = *dma;
        return gCoreRuntimeModel.start_result;
    }
    controller->guard = guard;
    controller->guard_context = guard_context;
    controller->mmio = *mmio;
    controller->dma = *dma;
    controller->slot_limit = slots;
    const int allocated = usb_dma_alloc32(&controller->dma, 256, 64,
                                           &gCoreRuntimeModel.schedule_buffer);
    if (allocated != USB_OK) {
        controller->state = USB_XHCI_CONTROLLER_FAULTED;
        return allocated;
    }
    controller->state = USB_XHCI_CONTROLLER_RUNNING;
    return USB_OK;
}

extern "C" int usb_xhci_controller_stop(struct usb_xhci_controller *controller)
{
    ++gCoreRuntimeModel.stop_calls;
    if (!controller) return USB_INVALID;
    if (gCoreRuntimeModel.stop_failures) {
        --gCoreRuntimeModel.stop_failures;
        return USB_TIMEOUT;
    }
    if (gCoreRuntimeModel.schedule_buffer.cookie) {
        usb_dma_free(&controller->dma, &gCoreRuntimeModel.schedule_buffer);
    }
    *controller = {};
    return USB_OK;
}

extern "C" int usb_xhci_root_port_get_status(
    struct usb_xhci_controller *controller, uint8_t port,
    struct usb_xhci_root_port_status *output)
{
    ++gCoreRuntimeModel.status_calls;
    if (!controller || controller->state != USB_XHCI_CONTROLLER_RUNNING ||
        !port || port > gCoreRuntimeModel.max_ports || !output) return USB_RANGE;
    const uint16_t index = static_cast<uint16_t>(port - 1u);
    if (!(gCoreRuntimeModel.usb2_ports[index / 8u] & (1u << (index % 8u))))
        return USB_UNSUPPORTED;
    *output = gCoreRuntimeModel.ports[port];
    return USB_OK;
}

extern "C" int usb_xhci_root_port_ack_changes(
    struct usb_xhci_controller *controller, uint8_t port, uint32_t changes)
{
    ++gCoreRuntimeModel.acknowledge_calls;
    if (!controller || controller->state != USB_XHCI_CONTROLLER_RUNNING ||
        !port || port > gCoreRuntimeModel.max_ports ||
        (changes & ~USB_XHCI_ROOT_PORT_CHANGE_MASK)) return USB_INVALID;
    if (gCoreRuntimeModel.acknowledge_result != USB_OK)
        return gCoreRuntimeModel.acknowledge_result;
    gCoreRuntimeModel.ports[port].changes &= ~changes;
    return USB_OK;
}

extern "C" int usb_xhci_host_adapter_init(
    struct usb_xhci_host_adapter *adapter, struct usb_xhci_controller *controller,
    struct usb_xhci_host_device *devices, size_t capacity, int (*lock)(void *),
    void (*unlock)(void *), void *lock_context, uint32_t (*now_ms)(void *),
    void *clock_context, uint32_t timeout_ms)
{
    if (!adapter || !controller || !devices || !capacity || !lock || !unlock ||
        !now_ms || !timeout_ms) return USB_INVALID;
    *adapter = {};
    adapter->controller = controller;
    adapter->devices = devices;
    adapter->device_capacity = capacity;
    adapter->lock = lock;
    adapter->unlock = unlock;
    adapter->lock_context = lock_context;
    adapter->now_ms = now_ms;
    adapter->clock_context = clock_context;
    adapter->command_timeout_ms = timeout_ms;
    gCoreRuntimeModel.adapter = adapter;
    return USB_OK;
}

extern "C" int usb_xhci_host_adapter_get_ops(
    struct usb_xhci_host_adapter *adapter, struct usb_host_ops *ops)
{
    if (!adapter || !ops) return USB_INVALID;
    *ops = {};
    ops->context = adapter;
    return USB_OK;
}

extern "C" int usb_xhci_host_adapter_reset_root_port(
    struct usb_xhci_host_adapter *adapter, uint8_t port, uint32_t timeout_ms,
    enum usb_host_speed *speed)
{
    if (!adapter || !speed || !timeout_ms || !port || port > gCoreRuntimeModel.max_ports)
        return USB_INVALID;
    const uint16_t index = static_cast<uint16_t>(port - 1u);
    if (!(gCoreRuntimeModel.usb2_ports[index / 8u] & (1u << (index % 8u))))
        return USB_UNSUPPORTED;
    ++gCoreRuntimeModel.reset_calls;
    struct usb_xhci_root_port_status *status = &gCoreRuntimeModel.ports[port];
    if (!status->powered) return USB_BUSY;
    if (!status->connected) return USB_DISCONNECTED;
    if (status->overcurrent || status->resetting) return USB_BUSY;
    status->enabled = true;
    status->resetting = false;
    status->changes |= USB_XHCI_ROOT_PORT_CHANGE_RESET;
    *speed = status->speed_id == USB_XHCI_SPEED_HIGH ? USB_HOST_HIGH_SPEED :
             status->speed_id == USB_XHCI_SPEED_LOW ? USB_HOST_LOW_SPEED : USB_HOST_FULL_SPEED;
    if (gCoreRuntimeModel.pending_event_count >=
        sizeof(gCoreRuntimeModel.pending_events) / sizeof(gCoreRuntimeModel.pending_events[0]))
        return USB_RANGE;
    struct usb_xhci_event event = {};
    event.kind = USB_XHCI_EVENT_PORT_STATUS_CHANGE;
    event.data.port_status.port_id = port;
    gCoreRuntimeModel.pending_events[gCoreRuntimeModel.pending_event_count++] = event;
    return USB_OK;
}

extern "C" int usb_xhci_host_adapter_root_port_get_status(
    struct usb_xhci_host_adapter *adapter, uint8_t port,
    struct usb_xhci_root_port_status *status)
{
    if (!adapter) return USB_INVALID;
    return usb_xhci_root_port_get_status(adapter->controller, port, status);
}

extern "C" int usb_xhci_host_adapter_root_port_ack_changes(
    struct usb_xhci_host_adapter *adapter, uint8_t port, uint32_t changes)
{
    if (!adapter) return USB_INVALID;
    return usb_xhci_root_port_ack_changes(adapter->controller, port, changes);
}

extern "C" int usb_xhci_host_adapter_next_event(
    struct usb_xhci_host_adapter *adapter, struct usb_xhci_event *event)
{
    if (!adapter || !event) return USB_INVALID;
    ++gCoreRuntimeModel.event_reads;
    if (!gCoreRuntimeModel.pending_event_count) return USB_AGAIN;
    *event = gCoreRuntimeModel.pending_events[0];
    for (size_t i = 1; i < gCoreRuntimeModel.pending_event_count; ++i)
        gCoreRuntimeModel.pending_events[i - 1] = gCoreRuntimeModel.pending_events[i];
    --gCoreRuntimeModel.pending_event_count;
    return USB_OK;
}

extern "C" int usb_xhci_host_adapter_release_stopped(
    struct usb_xhci_host_adapter *adapter)
{
    if (!adapter || !adapter->controller ||
        adapter->controller->state != USB_XHCI_CONTROLLER_EMPTY) return USB_BUSY;
    for (size_t i = 0; i < adapter->device_capacity; ++i) adapter->devices[i] = {};
    gCoreRuntimeModel.pending_event_count = 0;
    return USB_OK;
}

extern "C" int usb_host_enumerate(
    struct usb_host_device *device, const struct usb_host_ops *ops,
    const struct usb_host_route *route, enum usb_host_speed speed,
    uint8_t preferred_address)
{
    if (!device || !ops || !route || !preferred_address || preferred_address > 127u)
        return USB_INVALID;
    ++gCoreRuntimeModel.enumerate_calls;
    if (gCoreRuntimeModel.enumerate_calls <=
        sizeof(gCoreRuntimeModel.last_enumerated) / sizeof(gCoreRuntimeModel.last_enumerated[0]))
        gCoreRuntimeModel.last_enumerated[gCoreRuntimeModel.enumerate_calls - 1u] = device;
    *device = {};
    if (gCoreRuntimeModel.enumeration_fail_call == gCoreRuntimeModel.enumerate_calls)
        return USB_NO_MEMORY;
    device->state = USB_HOST_CONFIGURED;
    device->ops = *ops;
    device->route = *route;
    device->speed = speed;
    device->address = preferred_address;
    if (gCoreRuntimeModel.root_device_is_hub && route->depth == 0) {
        device->hub_interface.present = true;
        device->hub_interface.interface_number = 0;
        device->hub_interface.endpoint = 0x81u;
        device->hub_interface.interval = 12u;
        device->hub_interface.protocol = 0;
        device->hub_interface.max_packet = 1u;
    } else {
        device->has_bot = true;
        device->bot.blocks = 4096u;
        device->bot.block_size = 512u;
    }
    device->hid_count = 1u;
    device->hid[0].kind = USB_HOST_HID_KEYBOARD;
    device->hid[0].interface_number = 1u;
    device->hid[0].endpoint = 0x82u;
    return USB_OK;
}

extern "C" int usb_host_start_storage(struct usb_host_device *device)
{
    if (!device || !device->has_bot) return USB_INVALID;
    ++gCoreRuntimeModel.storage_start_calls;
    return USB_OK;
}

extern "C" IOService *dali_usb_hid_publish(
    IOService *, const struct usb_host_device *device, uint8_t hid_index)
{
    if (!device || hid_index >= device->hid_count) return nullptr;
    ++gCoreRuntimeModel.hid_publish_calls;
    return new (std::nothrow) IOService();
}

extern "C" int dali_usb_hid_quiesce(IOService *service)
{
    if (!service) return USB_INVALID;
    ++gCoreRuntimeModel.hid_quiesce_calls;
    return USB_OK;
}

extern "C" int dali_usb_hid_detach(IOService *service, bool stopped)
{
    if (!service || !stopped) return USB_BUSY;
    ++gCoreRuntimeModel.hid_detach_calls;
    return USB_OK;
}

extern "C" int dali_usb_hid_terminate(IOService *service)
{
    if (!service) return USB_INVALID;
    ++gCoreRuntimeModel.hid_terminate_calls;
    return USB_OK;
}

extern "C" IOService *dali_usb_storage_publish(IOService *, struct usb_bot *bot)
{
    if (!bot) return nullptr;
    ++gCoreRuntimeModel.storage_publish_calls;
    return new (std::nothrow) IOService();
}

extern "C" int dali_usb_storage_quiesce(IOService *service)
{
    if (!service) return USB_INVALID;
    ++gCoreRuntimeModel.storage_quiesce_calls;
    return gCoreRuntimeModel.storage_quiesce_result;
}

extern "C" int dali_usb_storage_detach(IOService *service, bool stopped)
{
    if (!service || !stopped) return USB_BUSY;
    ++gCoreRuntimeModel.storage_detach_calls;
    return USB_OK;
}

extern "C" int dali_usb_storage_terminate(IOService *service)
{
    if (!service) return USB_INVALID;
    ++gCoreRuntimeModel.storage_terminate_calls;
    return USB_OK;
}

extern "C" int usb_host_hub_start(struct usb_host_device *device, struct usb_host_hub *hub)
{
    if (!device || !hub || !device->hub_interface.present) return USB_INVALID;
    ++gCoreRuntimeModel.hub_start_calls;
    *hub = {};
    hub->initialized = true;
    hub->scan_all_ports = true;
    hub->port_count = 1u;
    hub->bitmap_bytes = 1u;
    return USB_OK;
}

extern "C" int usb_host_hub_child_route(
    const struct usb_host_device *device, const struct usb_host_hub *hub, uint8_t port,
    enum usb_host_speed speed, struct usb_host_route *route)
{
    if (!device || !hub || !route || port == 0 || port > hub->port_count ||
        device->route.depth >= USB_HOST_MAX_ROUTE_DEPTH) return USB_INVALID;
    *route = device->route;
    route->hub_address[route->depth] = device->address;
    route->hub_port[route->depth] = port;
    ++route->depth;
    if (device->speed == USB_HOST_HIGH_SPEED && speed != USB_HOST_HIGH_SPEED) {
        route->tt_hub_address = device->address;
        route->tt_port = port;
        route->tt_think_time_ns = 666u;
    }
    return USB_OK;
}

extern "C" int usb_host_hub_poll(
    struct usb_host_device *device, struct usb_host_hub *hub, uint32_t timeout_ms,
    usb_host_hub_event callback, void *context)
{
    if (!device || !hub || !callback || !timeout_ms || !hub->initialized) return USB_INVALID;
    ++gCoreRuntimeModel.hub_poll_calls;
    if (gCoreRuntimeModel.root_device_is_hub && !gCoreRuntimeModel.hub_child_attached &&
        hub->scan_all_ports) {
        hub->ports[0].attach_pending = true;
        struct usb_host_route route = {};
        const int routed = usb_host_hub_child_route(
            device, hub, 1u, USB_HOST_FULL_SPEED, &route);
        if (routed != USB_OK) return routed;
        const int result = callback(context, USB_HOST_HUB_ATTACH, 1u, &route,
                                    USB_HOST_FULL_SPEED);
        if (result != USB_OK) return result;
        hub->ports[0].attach_pending = false;
        gCoreRuntimeModel.hub_child_attached = true;
    }
    return USB_OK;
}

extern "C" int usb_host_hub_stop(
    struct usb_host_device *device, struct usb_host_hub *hub,
    usb_host_hub_event callback, void *context)
{
    if (!device || !hub) return USB_INVALID;
    ++gCoreRuntimeModel.hub_stop_calls;
    if (gCoreRuntimeModel.hub_child_attached) {
        struct usb_host_route route = {};
        const int routed = usb_host_hub_child_route(
            device, hub, 1u, USB_HOST_FULL_SPEED, &route);
        if (routed != USB_OK) return routed;
        const int result = callback(context, USB_HOST_HUB_DETACH, 1u, &route,
                                    USB_HOST_FULL_SPEED);
        if (result != USB_OK) return result;
        gCoreRuntimeModel.hub_child_attached = false;
    }
    *hub = {};
    return USB_OK;
}

extern "C" int usb_host_stop_device(struct usb_host_device *device)
{
    if (!device || device->state == USB_HOST_EMPTY) return USB_INVALID;
    ++gCoreRuntimeModel.host_stop_calls;
    device->stop_proven = true;
    return USB_OK;
}

extern "C" int usb_host_confirm_controller_stopped(struct usb_host_device *device)
{
    if (!device || device->state == USB_HOST_EMPTY) return USB_INVALID;
    device->stop_proven = true;
    return USB_OK;
}

extern "C" int usb_host_release_stopped_device(
    struct usb_host_device *device, usb_key_event, usb_pointer_event, void *)
{
    if (!device || !device->stop_proven) return USB_BUSY;
    ++gCoreRuntimeModel.host_release_calls;
    *device = {};
    return USB_OK;
}

static void prepare_qemu_runtime_model()
{
    gIOKitMemoryModel = {};
    gIOKitDmaModel = {};
    gIOKitDmaModel.no_mapper = true;
    gIOKitTimerModel = {};
    gIOKitTimerEventSource = nullptr;
    gIOKitTestUptimeNanoseconds = 0;
    gCoreRuntimeModel = {};
    gCoreRuntimeModel.ports[1].connected = true;
    gCoreRuntimeModel.ports[1].powered = true;
    gCoreRuntimeModel.ports[1].speed_id = USB_XHCI_SPEED_HIGH;
    gCoreRuntimeModel.ports[1].changes = USB_XHCI_ROOT_PORT_CHANGE_CONNECT;
    gCoreRuntimeModel.ports[2].powered = true;
}

static void test_qemu_xhci_runtime_starts_polls_and_stops_in_order()
{
    prepare_qemu_runtime_model();
    HostFixture fixture;
    DaliUsbQemuPciOwner pciOwner;
    assert(pciOwner.start(&fixture.host));
    DaliUsbQemuXhciRuntime runtime;
    assert(runtime.start(&pciOwner));
    assert(runtime.started && runtime.registered && pciOwner.hasMappedBar());
    assert(gCoreRuntimeModel.capability_calls == 1 && gCoreRuntimeModel.start_calls == 1);
    assert(gIOKitTimerEventSource && gIOKitTimerEventSource->timeout_ms == 5);

    gIOKitTimerEventSource->fire();
    assert(gCoreRuntimeModel.status_calls == 3 && gCoreRuntimeModel.acknowledge_calls == 2);
    assert(gCoreRuntimeModel.reset_calls == 1 && gCoreRuntimeModel.enumerate_calls == 1);
    assert(gCoreRuntimeModel.hid_publish_calls == 1 &&
           gCoreRuntimeModel.storage_start_calls == 1 &&
           gCoreRuntimeModel.storage_publish_calls == 1);
    struct dali_qemu_xhci_runtime_snapshot snapshot = {};
    assert(runtime.copyRuntimeSnapshot(&snapshot));
    assert(snapshot.version == DALI_QEMU_XHCI_RUNTIME_SNAPSHOT_VERSION);
    assert(snapshot.controller_running && !snapshot.faulted && snapshot.max_ports == 4);
    assert(snapshot.ports[1].connected && snapshot.ports[1].powered);
    assert(snapshot.ports[1].speed_id == USB_XHCI_SPEED_HIGH);
    assert(snapshot.valid_ports[0] == 3u);
    assert(gIOKitTimerEventSource->timeout_ms == 5);

    runtime.stop(&pciOwner);
    assert(!runtime.started && pciOwner.hasMappedBar());
    assert(gCoreRuntimeModel.stop_calls == 1);
    assert(gCoreRuntimeModel.host_stop_calls == 1 &&
           gCoreRuntimeModel.host_release_calls == 1);
    assert(gCoreRuntimeModel.hid_quiesce_calls == 1 &&
           gCoreRuntimeModel.hid_detach_calls == 1 &&
           gCoreRuntimeModel.hid_terminate_calls == 1);
    assert(gCoreRuntimeModel.storage_quiesce_calls == 1 &&
           gCoreRuntimeModel.storage_detach_calls == 1 &&
           gCoreRuntimeModel.storage_terminate_calls == 1);
    assert(gIOKitTimerModel.cancelled == 1 && gIOKitTimerModel.removed == 1);
    assert(gIOKitDmaModel.memory_releases == 1 && gIOKitDmaModel.command_releases == 2);
    runtime.free();
    pciOwner.stop(&fixture.host);
    assert(!pciOwner.hasMappedBar());
    pciOwner.free();
    assert(gIOKitLockCount == 0);
    gIOKitTimerEventSource = nullptr;
}

static void test_qemu_runtime_enumerates_hub_child_and_publishes_storage()
{
    prepare_qemu_runtime_model();
    gCoreRuntimeModel.root_device_is_hub = true;
    HostFixture fixture;
    DaliUsbQemuPciOwner pciOwner;
    assert(pciOwner.start(&fixture.host));
    DaliUsbQemuXhciRuntime runtime;
    assert(runtime.start(&pciOwner));

    gIOKitTimerEventSource->fire();
    assert(gCoreRuntimeModel.reset_calls == 1);
    assert(gCoreRuntimeModel.enumerate_calls == 2);
    assert(gCoreRuntimeModel.hub_start_calls == 1 && gCoreRuntimeModel.hub_poll_calls == 1);
    assert(gCoreRuntimeModel.hid_publish_calls == 2);
    assert(gCoreRuntimeModel.storage_publish_calls == 1);
    assert(gCoreRuntimeModel.hub_child_attached);
    assert(gCoreRuntimeModel.last_enumerated[0] != nullptr &&
           gCoreRuntimeModel.last_enumerated[1] != nullptr);
    assert(gCoreRuntimeModel.last_enumerated[0]->route.depth == 0);
    assert(gCoreRuntimeModel.last_enumerated[1]->route.depth == 1);

    runtime.stop(&pciOwner);
    assert(!runtime.started && gCoreRuntimeModel.hub_stop_calls == 1);
    assert(!gCoreRuntimeModel.hub_child_attached);
    assert(gCoreRuntimeModel.host_stop_calls == 2 &&
           gCoreRuntimeModel.host_release_calls == 2);
    assert(gCoreRuntimeModel.hid_detach_calls == 2 &&
           gCoreRuntimeModel.storage_detach_calls == 1);
    runtime.free();
    pciOwner.stop(&fixture.host);
    pciOwner.free();
    assert(gIOKitLockCount == 0);
    gIOKitTimerEventSource = nullptr;
}

static void test_qemu_runtime_retries_transient_root_enumeration()
{
    prepare_qemu_runtime_model();
    gIOKitTestUptimeNanoseconds =
        (static_cast<uint64_t>(UINT32_MAX) - 5u) * 1000000u;
    gCoreRuntimeModel.enumeration_fail_call = 1;
    HostFixture fixture;
    DaliUsbQemuPciOwner pciOwner;
    assert(pciOwner.start(&fixture.host));
    DaliUsbQemuXhciRuntime runtime;
    assert(runtime.start(&pciOwner));

    gIOKitTimerEventSource->fire();
    assert(gCoreRuntimeModel.reset_calls == 1 && gCoreRuntimeModel.enumerate_calls == 1);
    struct dali_qemu_xhci_runtime_snapshot snapshot = {};
    assert(runtime.copyRuntimeSnapshot(&snapshot) && !snapshot.faulted);
    for (unsigned tick = 0; tick < 99u; ++tick) gIOKitTimerEventSource->fire();
    assert(gCoreRuntimeModel.reset_calls == 1 && gCoreRuntimeModel.enumerate_calls == 1);

    gIOKitTimerEventSource->fire();
    assert(gCoreRuntimeModel.reset_calls == 2 && gCoreRuntimeModel.enumerate_calls == 2);
    assert(gCoreRuntimeModel.hid_publish_calls == 1 &&
           gCoreRuntimeModel.storage_publish_calls == 1);
    assert(runtime.copyRuntimeSnapshot(&snapshot) && !snapshot.faulted);

    runtime.stop(&pciOwner);
    runtime.free();
    pciOwner.stop(&fixture.host);
    pciOwner.free();
    assert(gIOKitLockCount == 0);
    gIOKitTimerEventSource = nullptr;
}

static void test_qemu_runtime_retries_hub_attach_without_faulting_owner()
{
    prepare_qemu_runtime_model();
    gCoreRuntimeModel.root_device_is_hub = true;
    gCoreRuntimeModel.enumeration_fail_call = 2;
    HostFixture fixture;
    DaliUsbQemuPciOwner pciOwner;
    assert(pciOwner.start(&fixture.host));
    DaliUsbQemuXhciRuntime runtime;
    assert(runtime.start(&pciOwner));

    gIOKitTimerEventSource->fire();
    assert(gCoreRuntimeModel.enumerate_calls == 2 &&
           gCoreRuntimeModel.hub_poll_calls == 1 && !gCoreRuntimeModel.hub_child_attached);
    struct dali_qemu_xhci_runtime_snapshot snapshot = {};
    assert(runtime.copyRuntimeSnapshot(&snapshot) && !snapshot.faulted);
    for (unsigned tick = 0; tick < 99u; ++tick) gIOKitTimerEventSource->fire();
    assert(gCoreRuntimeModel.hub_poll_calls == 1 && gCoreRuntimeModel.enumerate_calls == 2);

    gIOKitTimerEventSource->fire();
    assert(gCoreRuntimeModel.hub_poll_calls == 2 && gCoreRuntimeModel.enumerate_calls == 3);
    assert(gCoreRuntimeModel.hub_child_attached);
    assert(runtime.copyRuntimeSnapshot(&snapshot) && !snapshot.faulted);

    runtime.stop(&pciOwner);
    runtime.free();
    pciOwner.stop(&fixture.host);
    pciOwner.free();
    assert(gIOKitLockCount == 0);
    gIOKitTimerEventSource = nullptr;
}

static void test_qemu_runtime_fails_closed_on_usb3_event_without_usb2_access()
{
    prepare_qemu_runtime_model();
    gCoreRuntimeModel.ports[1].connected = false;
    gCoreRuntimeModel.ports[1].changes = 0;
    gCoreRuntimeModel.usb2_ports[0] = 0x01u;
    gCoreRuntimeModel.usb2_port_count = 1;
    gCoreRuntimeModel.pending_events[0] = {};
    gCoreRuntimeModel.pending_events[0].kind = USB_XHCI_EVENT_PORT_STATUS_CHANGE;
    gCoreRuntimeModel.pending_events[0].data.port_status.port_id = 3u;
    gCoreRuntimeModel.pending_event_count = 1;
    HostFixture fixture;
    DaliUsbQemuPciOwner pciOwner;
    assert(pciOwner.start(&fixture.host));
    DaliUsbQemuXhciRuntime runtime;
    assert(runtime.start(&pciOwner));

    gIOKitTimerEventSource->fire();
    struct dali_qemu_xhci_runtime_snapshot snapshot = {};
    assert(runtime.copyRuntimeSnapshot(&snapshot));
    assert(snapshot.faulted && snapshot.controller_running);
    assert(gCoreRuntimeModel.status_calls == 1 && gCoreRuntimeModel.acknowledge_calls == 0);
    assert(gCoreRuntimeModel.reset_calls == 0 && gCoreRuntimeModel.enumerate_calls == 0);
    assert(gCoreRuntimeModel.pending_event_count == 0);
    assert(gIOKitTimerEventSource->timeout_ms == 0);

    runtime.stop(&pciOwner);
    runtime.free();
    pciOwner.stop(&fixture.host);
    pciOwner.free();
    assert(gIOKitLockCount == 0);
    gIOKitTimerEventSource = nullptr;
}

static void test_qemu_runtime_retains_storage_until_sync_retry_on_disconnect()
{
    prepare_qemu_runtime_model();
    HostFixture fixture;
    DaliUsbQemuPciOwner pciOwner;
    assert(pciOwner.start(&fixture.host));
    DaliUsbQemuXhciRuntime runtime;
    assert(runtime.start(&pciOwner));
    gIOKitTimerEventSource->fire();
    const unsigned acknowledgements = gCoreRuntimeModel.acknowledge_calls;
    gCoreRuntimeModel.storage_quiesce_result = USB_BUSY;
    gCoreRuntimeModel.ports[1].connected = false;
    gCoreRuntimeModel.ports[1].changes |= USB_XHCI_ROOT_PORT_CHANGE_CONNECT;
    gIOKitTimerEventSource->fire();
    assert(gCoreRuntimeModel.host_stop_calls == 0);
    assert(gCoreRuntimeModel.storage_detach_calls == 0);
    assert(gCoreRuntimeModel.acknowledge_calls == acknowledgements);
    assert(gCoreRuntimeModel.hid_detach_calls == 0);

    gCoreRuntimeModel.storage_quiesce_result = USB_OK;
    for (unsigned tick = 0; tick < 99u; ++tick) gIOKitTimerEventSource->fire();
    assert(gCoreRuntimeModel.host_stop_calls == 0);
    assert(gCoreRuntimeModel.acknowledge_calls == acknowledgements);
    gIOKitTimerEventSource->fire();
    assert(gCoreRuntimeModel.host_stop_calls == 1);
    assert(gCoreRuntimeModel.storage_detach_calls == 1);
    assert(gCoreRuntimeModel.hid_detach_calls == 1);
    assert(gCoreRuntimeModel.host_release_calls == 1);
    assert(gCoreRuntimeModel.acknowledge_calls == acknowledgements + 1u);

    runtime.stop(&pciOwner);
    runtime.free();
    pciOwner.stop(&fixture.host);
    pciOwner.free();
    assert(gIOKitLockCount == 0);
    gIOKitTimerEventSource = nullptr;
}

static void test_qemu_xhci_runtime_retains_all_leases_after_failed_stop()
{
    prepare_qemu_runtime_model();
    HostFixture fixture;
    DaliUsbQemuPciOwner pciOwner;
    assert(pciOwner.start(&fixture.host));
    DaliUsbQemuXhciRuntime runtime;
    assert(runtime.start(&pciOwner));
    gCoreRuntimeModel.stop_failures = 1;
    runtime.stop(&pciOwner);
    assert(runtime.started && pciOwner.hasMappedBar());
    assert(gIOKitDmaModel.memory_releases == 0 && gIOKitDmaModel.command_releases == 1);
    assert(gIOKitTimerModel.cancelled == 1 && gIOKitTimerModel.removed == 1);
    runtime.stop(&pciOwner);
    assert(!runtime.started && pciOwner.hasMappedBar());
    assert(gCoreRuntimeModel.stop_calls == 2);
    assert(gIOKitDmaModel.memory_releases == 1 && gIOKitDmaModel.command_releases == 2);
    runtime.free();
    pciOwner.stop(&fixture.host);
    assert(!pciOwner.hasMappedBar());
    pciOwner.free();
    assert(gIOKitLockCount == 0);
    gIOKitTimerEventSource = nullptr;
}

static void test_qemu_xhci_runtime_releases_cleanly_after_capability_refusal()
{
    prepare_qemu_runtime_model();
    gCoreRuntimeModel.capability_result = USB_RANGE;
    HostFixture fixture;
    DaliUsbQemuPciOwner pciOwner;
    assert(pciOwner.start(&fixture.host));
    DaliUsbQemuXhciRuntime runtime;
    assert(!runtime.start(&pciOwner));
    assert(!runtime.started && pciOwner.hasMappedBar());
    assert(gCoreRuntimeModel.start_calls == 0 && gCoreRuntimeModel.stop_calls == 0);
    assert(gIOKitTimerModel.removed == 1);
    assert(gIOKitDmaModel.mapper_requests == 1);
    runtime.free();
    pciOwner.stop(&fixture.host);
    assert(!pciOwner.hasMappedBar());
    pciOwner.free();
    assert(gIOKitLockCount == 0);
    gIOKitTimerEventSource = nullptr;
}

static void test_qemu_pci_owner_success_and_guarded_bar()
{
    gIOKitMemoryModel = {};
    HostFixture fixture;
    DaliUsbQemuPciOwner owner;
    assert(owner.start(&fixture.host));
    assert(owner.started && owner.registered && owner.hasMappedBar());
    assert(gIOKitMemoryModel.range_calls == 2);
    assert(gIOKitMemoryModel.ranges[0] == std::make_pair(kEcamBase, kBusBytes));
    assert(gIOKitMemoryModel.ranges[1] ==
           std::make_pair(kBarBase, static_cast<uint64_t>(DALI_QEMU_XHCI_BAR0_BYTES)));
    assert(gIOKitMemoryModel.map_calls == 2 && gIOKitMemoryModel.map_releases == 1);
    assert(gIOKitMemoryModel.map_options_history[0] ==
           (kIOMapAnywhere | kIOMapInhibitCache));
    assert(gIOKitMemoryModel.map_options_history[1] ==
           (kIOMapAnywhere | kIOMapInhibitCache));

    struct dali_qemu_pci_xhci description = {};
    assert(owner.copyXhciDescription(&description));
    assert(description.bus == 0 && description.device == 2 && description.function == 0);
    assert(description.vendor_id == 0x1b36u && description.device_id == 0x000du);
    assert(description.class_code == 0x0c0330u);
    assert(description.command == ((1u << 1) | (1u << 2)));
    assert(description.bar0_pci_base == kBarBase);
    assert(description.bar0_cpu_base == kBarBase);
    assert(description.bar0_length == DALI_QEMU_XHCI_BAR0_BYTES);

    GuardState guard = {};
    guard.result = USB_BUSY;
    struct usb_mmio mmio = {};
    mmio.context = reinterpret_cast<void *>(1);
    mmio.size = 7;
    assert(!owner.bindGuardedMmio(register_guard, &guard, &mmio));
    assert(mmio.context == reinterpret_cast<void *>(1) && mmio.size == 7);
    guard.result = USB_OK;
    assert(owner.bindGuardedMmio(register_guard, &guard, &mmio));
    assert(mmio.size == DALI_QEMU_XHCI_BAR0_BYTES);

    assert(!owner.releaseGuardedMmio(false));
    owner.stop(&fixture.host);
    assert(owner.started && owner.hasMappedBar() && gIOKitMemoryModel.map_releases == 1);

    const uint32_t value = 0x87654321u;
    assert(mmio.write32(mmio.context, 0x20u, value) == USB_OK);
    uint32_t observed = 0;
    assert(mmio.read32(mmio.context, 0x20u, &observed) == USB_OK);
    assert(observed == value);
    observed = 0x12345678u;
    guard.result = USB_BUSY;
    assert(mmio.read32(mmio.context, 0x20u, &observed) == USB_BUSY);
    assert(observed == 0x12345678u);
    guard.result = USB_OK;
    assert(mmio.read32(mmio.context, mmio.size, &observed) == USB_RANGE);
    assert(owner.releaseGuardedMmio(true));
    owner.stop(&fixture.host);
    assert(!owner.started && !owner.hasMappedBar());
    assert(gIOKitMemoryModel.map_releases == 2);
    owner.free();
    assert(gIOKitLockCount == 0);
}

static void test_qemu_pci_owner_refuses_unassigned_or_disabled_bar()
{
    for (unsigned mode = 0; mode < 3; ++mode) {
        gIOKitMemoryModel = {};
        HostFixture fixture;
        uint8_t *config = fixture.xhciConfig();
        if (mode == 0) {
            put_le32(config + 0x10u, 0x00000004u);
            put_le32(config + 0x14u, 0);
        } else if (mode == 1) {
            put_config16(config, 0x04u, 1u << 2); // Bus master without memory decode.
        } else {
            put_config16(config, 0x04u, 1u << 1); // Memory decode without bus mastering.
        }
        DaliUsbQemuPciOwner owner;
        assert(!owner.start(&fixture.host));
        assert(!owner.started && !owner.registered && !owner.hasMappedBar());
        assert(gIOKitMemoryModel.range_calls == 1);
        assert(gIOKitMemoryModel.map_calls == 1 && gIOKitMemoryModel.map_releases == 1);
        owner.free();
        assert(gIOKitLockCount == 0);
    }
}

static void test_qemu_pci_owner_rejects_unqualified_host_properties()
{
    gIOKitMemoryModel = {};
    HostFixture wrongDeviceType;
    std::vector<uint8_t> invalidDeviceType = {'n', 'o', 't', '-', 'p', 'c', 'i', 0};
    wrongDeviceType.addProperty("device_type", invalidDeviceType);
    DaliUsbQemuPciOwner badTypeOwner;
    assert(!badTypeOwner.start(&wrongDeviceType.host));
    assert(gIOKitMemoryModel.range_calls == 0);
    badTypeOwner.free();

    gIOKitMemoryModel = {};
    HostFixture wrongArmIoType;
    std::vector<uint8_t> invalidArmIoType = {'n', 'o', 't', '-', 'q', 'e', 'm', 'u', '-', 'v', 'i', 'r', 't', 0};
    wrongArmIoType.addArmIoProperty("device_type", invalidArmIoType);
    DaliUsbQemuPciOwner badArmIoOwner;
    assert(!badArmIoOwner.start(&wrongArmIoType.host));
    assert(gIOKitMemoryModel.range_calls == 0);
    badArmIoOwner.free();

    gIOKitMemoryModel = {};
    HostFixture wrongCells;
    std::vector<uint8_t> invalidAddressCells = {2, 0, 0, 0};
    wrongCells.addProperty("#address-cells", invalidAddressCells);
    DaliUsbQemuPciOwner badCellsOwner;
    assert(!badCellsOwner.start(&wrongCells.host));
    assert(gIOKitMemoryModel.range_calls == 0);
    badCellsOwner.free();

    gIOKitMemoryModel = {};
    HostFixture missingCoherency;
    missingCoherency.pciHost.removeTestProperty("dma-coherent");
    DaliUsbQemuPciOwner noCoherencyOwner;
    assert(!noCoherencyOwner.start(&missingCoherency.host));
    assert(gIOKitMemoryModel.range_calls == 0);
    noCoherencyOwner.free();

    gIOKitMemoryModel = {};
    HostFixture translatedDma;
    std::vector<uint8_t> iommuMap = {0, 0, 0, 0};
    translatedDma.addProperty("iommu-map", iommuMap);
    DaliUsbQemuPciOwner iommuOwner;
    assert(!iommuOwner.start(&translatedDma.host));
    assert(gIOKitMemoryModel.range_calls == 0);
    iommuOwner.free();
    assert(gIOKitLockCount == 0);
}

static void test_qemu_pci_owner_requires_the_nested_host_node()
{
    gIOKitMemoryModel = {};
    HostFixture missingHostNode;
    IORegistryEntry::removeTestPath("/arm-io/qemu-pcie-host", &missingHostNode.pciHost);
    DaliUsbQemuPciOwner owner;
    assert(!owner.start(&missingHostNode.host));
    assert(!owner.started && !owner.hasMappedBar());
    assert(gIOKitMemoryModel.range_calls == 0);
    owner.free();
    assert(gIOKitLockCount == 0);
}

static void test_qemu_pci_owner_refuses_bad_host_and_bar_mapping_failure()
{
    gIOKitMemoryModel = {};
    HostFixture wrongHost;
    wrongHost.compatible[0] = 'x';
    wrongHost.addProperty("compatible", wrongHost.compatible);
    DaliUsbQemuPciOwner incompatible;
    assert(!incompatible.start(&wrongHost.host));
    assert(gIOKitMemoryModel.range_calls == 0);
    incompatible.free();

    gIOKitMemoryModel = {};
    HostFixture mappingFailure;
    gIOKitMemoryModel.fail_map_at = 2;
    DaliUsbQemuPciOwner owner;
    assert(!owner.start(&mappingFailure.host));
    assert(!owner.started && !owner.registered && !owner.hasMappedBar());
    assert(gIOKitMemoryModel.range_calls == 2 && gIOKitMemoryModel.map_calls == 2);
    assert(gIOKitMemoryModel.memory_releases == 2 && gIOKitMemoryModel.map_releases == 1);
    owner.free();
    assert(gIOKitLockCount == 0);
}

int main()
{
    test_qemu_xhci_runtime_starts_polls_and_stops_in_order();
    test_qemu_runtime_enumerates_hub_child_and_publishes_storage();
    test_qemu_runtime_retries_transient_root_enumeration();
    test_qemu_runtime_retries_hub_attach_without_faulting_owner();
    test_qemu_runtime_fails_closed_on_usb3_event_without_usb2_access();
    test_qemu_runtime_retains_storage_until_sync_retry_on_disconnect();
    test_qemu_xhci_runtime_retains_all_leases_after_failed_stop();
    test_qemu_xhci_runtime_releases_cleanly_after_capability_refusal();
    test_qemu_pci_owner_success_and_guarded_bar();
    test_qemu_pci_owner_refuses_unassigned_or_disabled_bar();
    test_qemu_pci_owner_rejects_unqualified_host_properties();
    test_qemu_pci_owner_refuses_bad_host_and_bar_mapping_failure();
    test_qemu_pci_owner_requires_the_nested_host_node();
    std::puts("PASS: production QEMU PCI owner and guarded xHCI runtime lifecycle/root-port polling");
    return 0;
}
