// SPDX-License-Identifier: BSD-2-Clause-Patent
/* The pinned XNU headers expose OSPtr as a raw pointer unless a translation
 * unit opts into the shared-pointer API. This adapter relies on the owning
 * factory results to release temporary DMA objects on every early return. */
#define IOKIT_ENABLE_EXPERIMENTAL_SHARED_PTR_IN_API
#define IOKIT_ENABLE_SHARED_PTR

#include <IOKit/IOLib.h>
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IODMACommand.h>
#include <IOKit/IOMapper.h>

#include "DaliUsbXhciDma.hpp"

struct dali_usb_xhci_dma_allocation {
    struct dali_usb_xhci_dma_allocation *next;
    IOBufferMemoryDescriptor *memory;
    IODMACommand *command;
    void *cpu;
    uint64_t address;
    size_t size;
    bool descriptor_attached;
};

static bool power_of_two(size_t value)
{
    return value && !(value & (value - 1));
}

static bool listed(struct dali_usb_xhci_dma_context *context,
                   struct dali_usb_xhci_dma_allocation *allocation)
{
    for (auto *item = context->allocations; item != nullptr; item = item->next) {
        if (item == allocation) return true;
    }
    return false;
}

static bool overlaps(uint64_t left, size_t left_size, uint64_t right, size_t right_size)
{
    if (!left_size || !right_size || left > UINT64_MAX - (left_size - 1) ||
        right > UINT64_MAX - (right_size - 1)) return true;
    return left <= right + (right_size - 1) && right <= left + (left_size - 1);
}

static void retain_allocation(struct dali_usb_xhci_dma_context *context,
                              struct dali_usb_xhci_dma_allocation *allocation)
{
    allocation->next = context->allocations;
    context->allocations = allocation;
}

static bool unlink_allocation(struct dali_usb_xhci_dma_context *context,
                              struct dali_usb_xhci_dma_allocation *allocation)
{
    auto **cursor = &context->allocations;
    while (*cursor != nullptr) {
        if (*cursor == allocation) {
            *cursor = allocation->next;
            allocation->next = nullptr;
            return true;
        }
        cursor = &(*cursor)->next;
    }
    return false;
}

static int release_allocation(struct dali_usb_xhci_dma_context *context,
                              struct dali_usb_xhci_dma_allocation *allocation)
{
    if (!context || !allocation || !listed(context, allocation)) return USB_INVALID;
    if (allocation->descriptor_attached) {
        if (allocation->command->clearMemoryDescriptor(true) != kIOReturnSuccess) {
            context->faulted = true;
            IOLog("DaliUsbXhciDma: DMA mapping release failed; allocation retained\n");
            return USB_IO;
        }
        allocation->descriptor_attached = false;
    }
    if (!unlink_allocation(context, allocation)) {
        context->faulted = true;
        return USB_IO;
    }
    allocation->command->release();
    allocation->memory->release();
    IOFree(allocation, sizeof(*allocation));
    return USB_OK;
}

static int allocate_dma(void *opaque, size_t size, size_t alignment, uint64_t limit,
                        struct usb_dma_buffer *out)
{
    auto *context = static_cast<struct dali_usb_xhci_dma_context *>(opaque);
    if (!out) return USB_INVALID;
    if (!context || !context->active || context->faulted || !context->mapper) return USB_UNSUPPORTED;
    if (!size || size > UINT32_MAX || !power_of_two(alignment) || alignment > UINT32_MAX ||
        limit != UINT32_MAX) return USB_RANGE;

    const IOOptionBits memory_options = kIODirectionInOut | kIOMemoryPhysicallyContiguous |
                                       kIOMapInhibitCache;
    /* CPU virtual alignment and the controller's I/O bus address width are
     * separate constraints. Request CPU alignment here; the mapped DMA command
     * below independently enforces the 32-bit IOVA and bus alignment. */
    auto memory = IOBufferMemoryDescriptor::withOptions(
        memory_options, static_cast<vm_size_t>(size), static_cast<vm_offset_t>(alignment));
    if (!memory) return USB_NO_MEMORY;
    auto command = IODMACommand::withSpecification(
        kIODMACommandOutputHost32, 32, static_cast<UInt64>(size), IODMACommand::kMapped,
        static_cast<UInt64>(size), static_cast<UInt32>(alignment), context->mapper);
    if (!command) return USB_NO_MEMORY;

    auto *allocation = static_cast<struct dali_usb_xhci_dma_allocation *>(
        IOMallocZero(sizeof(struct dali_usb_xhci_dma_allocation)));
    if (!allocation) return USB_NO_MEMORY;
    allocation->memory = memory.get();
    allocation->memory->retain();
    allocation->command = command.get();
    allocation->command->retain();
    allocation->cpu = memory->getBytesNoCopy();
    allocation->size = size;

    /* usb_dma_alloc32 can route a failed allocation through release() only
     * after it receives a CPU pointer. Keep this pre-mapping failure local so
     * a descriptor with no kernel mapping cannot become an unreleaseable list
     * entry. */
    if (!allocation->cpu || (reinterpret_cast<uintptr_t>(allocation->cpu) & (alignment - 1))) {
        allocation->command->release();
        allocation->memory->release();
        IOFree(allocation, sizeof(*allocation));
        return USB_RANGE;
    }

    retain_allocation(context, allocation);

    /* Publish the cookie before any operation that may acquire mapping
     * resources, so usb_dma_alloc32 can route failures through release(). */
    out->cpu = allocation->cpu;
    out->size = size;
    out->cookie = allocation;
    out->coherent = true; /* CPU mapping is cache-inhibited. */

    if (allocation->command->setMemoryDescriptor(allocation->memory, false) != kIOReturnSuccess)
        return USB_IO;
    allocation->descriptor_attached = true;
    if (allocation->command->prepare(0, static_cast<UInt64>(size), false, false) != kIOReturnSuccess)
        return USB_IO;

    IODMACommand::Segment32 segment = {};
    UInt64 offset = 0;
    UInt32 segment_count = 1;
    if (allocation->command->gen32IOVMSegments(&offset, &segment, &segment_count) != kIOReturnSuccess ||
        segment_count != 1 || segment.fLength < size || segment.fIOVMAddr > limit ||
        size - 1 > limit - segment.fIOVMAddr || (segment.fIOVMAddr & (alignment - 1)))
        return USB_RANGE;

    for (auto *item = context->allocations; item != nullptr; item = item->next) {
        if (item == allocation) continue;
        if (overlaps(reinterpret_cast<uintptr_t>(item->cpu), item->size,
                     reinterpret_cast<uintptr_t>(allocation->cpu), allocation->size) ||
            overlaps(item->address, item->size, segment.fIOVMAddr, size)) {
            context->faulted = true;
            return USB_RANGE;
        }
    }
    allocation->address = segment.fIOVMAddr;
    out->address = allocation->address;
    return USB_OK;
}

static void release_dma(void *opaque, struct usb_dma_buffer *buffer)
{
    auto *context = static_cast<struct dali_usb_xhci_dma_context *>(opaque);
    if (!context || !buffer || !buffer->cookie) {
        if (context) context->faulted = true;
        return;
    }
    auto *allocation = static_cast<struct dali_usb_xhci_dma_allocation *>(buffer->cookie);
    if (allocation->cpu != buffer->cpu || allocation->address != buffer->address ||
        allocation->size != buffer->size || release_allocation(context, allocation) != USB_OK)
        context->faulted = true;
}

static int sync_dma(void *opaque, struct usb_dma_buffer *buffer, size_t offset,
                    size_t length, enum usb_dma_direction direction, bool for_device)
{
    auto *context = static_cast<struct dali_usb_xhci_dma_context *>(opaque);
    auto *allocation = buffer ?
        static_cast<struct dali_usb_xhci_dma_allocation *>(buffer->cookie) : nullptr;
    if (!context || !context->active || context->faulted || !buffer || !allocation ||
        !listed(context, allocation) || allocation->cpu != buffer->cpu ||
        allocation->address != buffer->address || allocation->size != buffer->size ||
        direction < USB_DMA_TO_DEVICE || direction > USB_DMA_BIDIRECTIONAL ||
        offset > buffer->size || length > buffer->size - offset) return USB_RANGE;
    /* The backing CPU mapping is cache-inhibited. Keep the DMA visibility
     * contract with a full ordering barrier in both directions. */
    OSMemoryBarrier();
    (void)for_device;
    return USB_OK;
}

extern "C" bool
dali_usb_xhci_dma_init(struct dali_usb_xhci_dma_context *context, IOService *device)
{
    if (!context || !device || context->active || context->mapper || context->allocations || context->faulted)
        return false;
    auto mapper = IOMapper::copyMapperForDevice(device);
    if (!mapper) return false;
    context->mapper = mapper.get();
    context->mapper->retain();
    context->active = true;
    return true;
}

extern "C" bool
dali_usb_xhci_dma_get_ops(struct dali_usb_xhci_dma_context *context, struct usb_dma_ops *out)
{
    if (!context || !out || !context->active || context->faulted || !context->mapper) return false;
    struct usb_dma_ops ops = {};
    ops.context = context;
    ops.qualified = true;
    ops.allocate = allocate_dma;
    ops.release = release_dma;
    ops.sync = sync_dma;
    *out = ops;
    return true;
}

extern "C" bool
dali_usb_xhci_dma_stop(struct dali_usb_xhci_dma_context *context)
{
    if (!context || !context->active) return false;
    while (context->allocations != nullptr) {
        auto *allocation = context->allocations;
        if (release_allocation(context, allocation) != USB_OK) return false;
    }
    if (context->mapper) context->mapper->release();
    context->mapper = nullptr;
    context->active = false;
    context->faulted = false;
    return true;
}
