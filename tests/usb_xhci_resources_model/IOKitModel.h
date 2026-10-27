/* Test-only IOKit seam; this is not a kernel ABI or hardware qualification. */
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <atomic>
#include <algorithm>
#include <functional>
#include <mutex>
#include <new>
#include <string>
#include <unordered_map>
#include <vector>

using IOOptionBits = uint32_t;
using vm_size_t = size_t;
using vm_offset_t = uintptr_t;
static constexpr IOOptionBits kIOMapAnywhere = 1u;
static constexpr IOOptionBits kIOMapInhibitCache = 2u;
static constexpr IOOptionBits kIOMemoryPhysicallyContiguous = 0x10u;

struct IOKitMemoryModel {
    bool fail_range = false;
    bool fail_map = false;
    uint64_t base = 0;
    uint64_t length = 0;
    IOOptionBits map_options = 0;
    unsigned range_calls = 0;
    unsigned memory_releases = 0;
    unsigned map_calls = 0;
    unsigned map_releases = 0;
};

inline IOKitMemoryModel gIOKitMemoryModel;

class OSObject {
    unsigned mReferences = 1;

public:
    virtual ~OSObject() = default;
    virtual void free() {}
    virtual void retain() { ++mReferences; }
    virtual void release()
    {
        if (--mReferences == 0) { free(); delete this; }
    }
};

template <typename T>
class OSPtr {
    T *mPointer = nullptr;

public:
    OSPtr() = default;
    explicit OSPtr(T *pointer) : mPointer(pointer) {}
    OSPtr(const OSPtr &) = delete;
    OSPtr &operator=(const OSPtr &) = delete;
    OSPtr(OSPtr &&other) noexcept : mPointer(other.mPointer) { other.mPointer = nullptr; }
    OSPtr &operator=(OSPtr &&other) noexcept
    {
        if (mPointer) mPointer->release();
        mPointer = other.mPointer;
        other.mPointer = nullptr;
        return *this;
    }
    ~OSPtr() { if (mPointer) mPointer->release(); }
    T *get() const { return mPointer; }
    T *operator->() const { return mPointer; }
    explicit operator bool() const { return mPointer != nullptr; }
};

using IOReturn = int;
using UInt8 = uint8_t;
using UInt32 = uint32_t;
using UInt64 = uint64_t;
using mach_vm_size_t = uint64_t;
static constexpr IOReturn kIOReturnSuccess = 0;
static constexpr IOReturn kIOReturnError = -1;
static constexpr IOReturn kIOReturnBadArgument = -2;
static constexpr IOReturn kIOReturnNoMemory = -3;
static constexpr IOReturn kIOReturnOffline = -4;
static constexpr IOReturn kIOReturnNotReady = -5;
static constexpr IOReturn kIOReturnTimeout = -6;
static constexpr IOReturn kIOReturnUnsupported = -7;
static constexpr IOReturn kIOReturnIOError = -8;
static constexpr IOReturn kIOReturnNotWritable = -9;
static constexpr IOOptionBits kIODirectionInOut = 3u;
using IODirection = IOOptionBits;
static constexpr IODirection kIODirectionNone = 0u;
static constexpr IODirection kIODirectionIn = 1u;
static constexpr IODirection kIODirectionOut = 2u;

struct IOKitDmaModel {
    bool no_mapper = false;
    bool no_cpu_mapping = false;
    bool fail_memory = false;
    bool fail_command = false;
    bool fail_set_descriptor = false;
    bool fail_prepare = false;
    bool fail_segments = false;
    bool fail_clear = false;
    bool fail_sync = false;
    uint32_t segment_count = 1;
    uint64_t dma_address = 0x00100000u;
    uint64_t next_dma_address = 0;
    uint64_t segment_length = 0;
    bool ignore_iova_alignment = false;
    size_t capacity = 0;
    size_t cpu_alignment = 0;
    IOOptionBits memory_options = 0;
    UInt8 address_bits = 0;
    UInt32 alignment = 0;
    uint32_t mapping_options = 0;
    unsigned mapper_requests = 0;
    unsigned mapper_releases = 0;
    unsigned memory_allocations = 0;
    unsigned memory_releases = 0;
    unsigned command_allocations = 0;
    unsigned command_releases = 0;
    unsigned prepare_calls = 0;
    unsigned active_prepares = 0;
    unsigned complete_calls = 0;
    unsigned clear_calls = 0;
    unsigned sync_calls = 0;
};

inline IOKitDmaModel gIOKitDmaModel;

class IOMemoryDescriptor : public OSObject {
protected:
    std::vector<uint8_t> mStorage;
    IODirection mDirection;

public:
    IOMemoryDescriptor(IODirection direction, size_t length) : mStorage(length), mDirection(direction) {}
    IOMemoryDescriptor(IODirection direction, const void *bytes, size_t length)
        : mStorage(length), mDirection(direction)
    {
        if (bytes && length) std::memcpy(mStorage.data(), bytes, length);
    }
    UInt64 getLength() const { return static_cast<UInt64>(mStorage.size()); }
    IODirection getDirection() const { return mDirection; }
    virtual UInt64 readBytes(UInt64 offset, void *destination, UInt64 length)
    {
        if (!destination || offset > mStorage.size() || length > mStorage.size() - offset) return 0;
        std::memcpy(destination, mStorage.data() + static_cast<size_t>(offset), static_cast<size_t>(length));
        return length;
    }
    virtual UInt64 writeBytes(UInt64 offset, const void *source, UInt64 length)
    {
        if (!source || offset > mStorage.size() || length > mStorage.size() - offset) return 0;
        std::memcpy(mStorage.data() + static_cast<size_t>(offset), source, static_cast<size_t>(length));
        return length;
    }
};

inline void *IOMallocZero(size_t size) { return std::calloc(1, size); }
inline void *IOMalloc(size_t size) { return std::malloc(size); }
inline void IOFree(void *memory, size_t) { std::free(memory); }
inline void OSMemoryBarrier() { std::atomic_thread_fence(std::memory_order_seq_cst); }

class IOService;

class IOMapper : public OSObject {
public:
    static OSPtr<IOMapper> copyMapperForDevice(IOService *)
    {
        ++gIOKitDmaModel.mapper_requests;
        return gIOKitDmaModel.no_mapper ? OSPtr<IOMapper>() : OSPtr<IOMapper>(new IOMapper());
    }
    ~IOMapper() override { ++gIOKitDmaModel.mapper_releases; }
};

class IOBufferMemoryDescriptor : public IOMemoryDescriptor {
    void *mBytes = nullptr;
    bool mPhysicalAllocation = true;
    bool mNoCpuMapping = false;

public:
    explicit IOBufferMemoryDescriptor(size_t size, bool no_cpu_mapping, size_t alignment)
        : IOMemoryDescriptor(kIODirectionInOut, size), mNoCpuMapping(no_cpu_mapping)
    {
        const size_t allocationAlignment = std::max(alignment, sizeof(void *));
        if (!no_cpu_mapping && posix_memalign(&mBytes, allocationAlignment, size) == 0)
            std::memset(mBytes, 0, size);
    }
    IOBufferMemoryDescriptor(const void *bytes, size_t size, IODirection direction)
        : IOMemoryDescriptor(direction, bytes, size), mPhysicalAllocation(false) {}
    ~IOBufferMemoryDescriptor() override
    {
        if (mPhysicalAllocation) {
            ++gIOKitDmaModel.memory_releases;
            std::free(mBytes);
        }
    }
    static OSPtr<IOBufferMemoryDescriptor> withOptions(
        IOOptionBits options, vm_size_t capacity, vm_offset_t alignment = 1)
    {
        ++gIOKitDmaModel.memory_allocations;
        gIOKitDmaModel.memory_options = options;
        gIOKitDmaModel.capacity = static_cast<size_t>(capacity);
        gIOKitDmaModel.cpu_alignment = static_cast<size_t>(alignment);
        if (gIOKitDmaModel.fail_memory) return OSPtr<IOBufferMemoryDescriptor>();
        auto *memory = new (std::nothrow) IOBufferMemoryDescriptor(
            static_cast<size_t>(capacity), gIOKitDmaModel.no_cpu_mapping,
            static_cast<size_t>(alignment));
        if (!memory || (!gIOKitDmaModel.no_cpu_mapping && !memory->getBytesNoCopy())) {
            if (memory) memory->release();
            return OSPtr<IOBufferMemoryDescriptor>();
        }
        return OSPtr<IOBufferMemoryDescriptor>(memory);
    }
    static IOBufferMemoryDescriptor *withBytes(const void *bytes, size_t size, IODirection direction)
    {
        return bytes && size ? new (std::nothrow) IOBufferMemoryDescriptor(bytes, size, direction) : nullptr;
    }
    void *getBytesNoCopy() const
    {
        if (mPhysicalAllocation) return mNoCpuMapping ? nullptr : mBytes;
        return const_cast<uint8_t *>(mStorage.data());
    }
};

class IODMACommand : public OSObject {
public:
    ~IODMACommand() override { ++gIOKitDmaModel.command_releases; }
    struct Segment32 { UInt32 fIOVMAddr = 0; UInt32 fLength = 0; };
    struct Segment64 { UInt64 fIOVMAddr = 0; UInt64 fLength = 0; };
    enum MappingOptions { kMapped = 1 };
    using SegmentFunction = bool (*)(IODMACommand *, Segment64, void *, UInt32);

private:
    IOBufferMemoryDescriptor *mMemory = nullptr;
    UInt64 mDmaAddress = 0;
    UInt64 mSegmentLength = 0;
    UInt32 mAlignment = 1;
    bool mActive = false;

public:
    static bool OutputHost32(IODMACommand *, Segment64, void *, UInt32) { return true; }
    static OSPtr<IODMACommand> withSpecification(SegmentFunction, UInt8 address_bits,
        UInt64, MappingOptions mapping_options, UInt64, UInt32 alignment, IOMapper *mapper)
    {
        if (gIOKitDmaModel.fail_command || !mapper) return OSPtr<IODMACommand>();
        ++gIOKitDmaModel.command_allocations;
        gIOKitDmaModel.address_bits = address_bits;
        gIOKitDmaModel.alignment = alignment;
        gIOKitDmaModel.mapping_options = static_cast<uint32_t>(mapping_options);
        auto *command = new (std::nothrow) IODMACommand();
        if (command) command->mAlignment = alignment ? alignment : 1u;
        return OSPtr<IODMACommand>(command);
    }
    IOReturn setMemoryDescriptor(IOBufferMemoryDescriptor *memory, bool)
    {
        if (gIOKitDmaModel.fail_set_descriptor) return kIOReturnError;
        mMemory = memory;
        return kIOReturnSuccess;
    }
    IOReturn prepare(UInt64, UInt64, bool, bool)
    {
        ++gIOKitDmaModel.prepare_calls;
        if (!mMemory) return kIOReturnError;
        if (!mDmaAddress) {
            UInt64 next = gIOKitDmaModel.next_dma_address ?
                          gIOKitDmaModel.next_dma_address : gIOKitDmaModel.dma_address;
            UInt64 alignment = mAlignment;
            UInt64 mask = alignment - 1u;
            if (gIOKitDmaModel.ignore_iova_alignment) {
                mDmaAddress = next;
            } else {
                if (next > UINT64_MAX - mask) return kIOReturnError;
                mDmaAddress = (next + mask) & ~mask;
            }
            mSegmentLength = gIOKitDmaModel.segment_length ?
                             gIOKitDmaModel.segment_length : mMemory->getLength();
            if (mDmaAddress > UINT64_MAX - alignment ||
                mSegmentLength > UINT64_MAX - mDmaAddress - alignment)
                return kIOReturnError;
            gIOKitDmaModel.next_dma_address = mDmaAddress + mSegmentLength + alignment;
        }
        if (!mActive) {
            mActive = true;
            ++gIOKitDmaModel.active_prepares;
        }
        return gIOKitDmaModel.fail_prepare ? kIOReturnError : kIOReturnSuccess;
    }
    IOReturn gen32IOVMSegments(UInt64 *offset, Segment32 *segments, UInt32 *count)
    {
        if (gIOKitDmaModel.fail_segments || !offset || !segments || !count || !mMemory)
            return kIOReturnError;
        if (mDmaAddress > UINT32_MAX || !mSegmentLength ||
            mSegmentLength - 1u > static_cast<UInt64>(UINT32_MAX) - mDmaAddress)
            return kIOReturnError;
        *count = gIOKitDmaModel.segment_count;
        segments[0].fIOVMAddr = static_cast<UInt32>(mDmaAddress);
        segments[0].fLength = static_cast<UInt32>(mSegmentLength);
        return kIOReturnSuccess;
    }
    IOReturn clearMemoryDescriptor(bool auto_complete)
    {
        ++gIOKitDmaModel.clear_calls;
        if (gIOKitDmaModel.fail_clear) return kIOReturnError;
        if (mActive && !auto_complete) return kIOReturnError;
        if (mActive) {
            if (gIOKitDmaModel.active_prepares) --gIOKitDmaModel.active_prepares;
            ++gIOKitDmaModel.complete_calls;
            mActive = false;
        }
        mMemory = nullptr;
        return kIOReturnSuccess;
    }
    IOReturn synchronize(IOOptionBits)
    {
        ++gIOKitDmaModel.sync_calls;
        return gIOKitDmaModel.fail_sync ? kIOReturnError : kIOReturnSuccess;
    }
};

#define kIODMACommandOutputHost32 (IODMACommand::OutputHost32)

class IOMemoryMap : public OSObject {
    IOKitMemoryModel *mModel;

public:
    explicit IOMemoryMap(IOKitMemoryModel *model) : mModel(model) {}
    uint64_t getVirtualAddress() const { return 0xfeed0000u; }
    void release() override { ++mModel->map_releases; OSObject::release(); }
};

class IODeviceMemory : public OSObject {
    IOKitMemoryModel *mModel;

public:
    explicit IODeviceMemory(IOKitMemoryModel *model) : mModel(model) {}
    static IODeviceMemory *withRange(uint64_t base, uint64_t length)
    {
        ++gIOKitMemoryModel.range_calls;
        gIOKitMemoryModel.base = base;
        gIOKitMemoryModel.length = length;
        return gIOKitMemoryModel.fail_range ? nullptr : new IODeviceMemory(&gIOKitMemoryModel);
    }
    IOMemoryMap *map(IOOptionBits options)
    {
        ++mModel->map_calls;
        mModel->map_options = options;
        return mModel->fail_map ? nullptr : new IOMemoryMap(mModel);
    }
    void release() override { ++mModel->memory_releases; OSObject::release(); }
};

class OSData : public OSObject {
    std::vector<uint8_t> mBytes;

public:
    OSData(const void *bytes, size_t length)
    {
        const auto *source = static_cast<const uint8_t *>(bytes);
        mBytes.assign(source, source + length);
    }

    size_t getLength() const { return mBytes.size(); }
    const void *getBytesNoCopy() const { return mBytes.data(); }
};

class OSDictionary;
class IOService : public OSObject {
    std::unordered_map<std::string, OSObject *> mProperties;
    IOService *mProvider = nullptr;
    bool mInactive = false;

public:
    bool started = false;
    bool registered = false;

    virtual bool init(OSDictionary * = nullptr) { return true; }
    bool attach(IOService *provider)
    {
        if (!provider || mProvider) return false;
        mProvider = provider;
        return true;
    }
    bool detach(IOService *provider)
    {
        if (!provider || provider != mProvider) return false;
        mProvider = nullptr;
        return true;
    }
    virtual bool start(IOService *)
    {
        started = true;
        return true;
    }

    virtual void stop(IOService *) { started = false; }
    virtual void free() {}
    bool terminate(IOOptionBits)
    {
        if (mProvider) stop(mProvider);
        mInactive = true;
        if (mProvider) mProvider = nullptr;
        return true;
    }
    bool isInactive() const { return mInactive; }
    OSObject *getProperty(const char *key)
    {
        auto found = mProperties.find(key);
        return found == mProperties.end() ? nullptr : found->second;
    }
    bool setProperty(const char *key, UInt64, unsigned)
    {
        return key != nullptr;
    }
    void setTestProperty(const char *key, OSObject *value) { mProperties[key] = value; }
    void registerService() { registered = true; }
};

class OSDictionary : public OSObject {};
static constexpr IOOptionBits kIOServiceRequired = 1u;

struct IOLock { std::mutex mutex; };
inline unsigned gIOKitLockCount = 0;
inline IOLock *IOLockAlloc()
{
    auto *lock = new (std::nothrow) IOLock();
    if (lock) ++gIOKitLockCount;
    return lock;
}
inline void IOLockFree(IOLock *lock)
{
    if (lock) { --gIOKitLockCount; delete lock; }
}
inline void IOLockLock(IOLock *lock) { if (lock) lock->mutex.lock(); }
inline void IOLockUnlock(IOLock *lock) { if (lock) lock->mutex.unlock(); }
inline void panic(const char *) { std::abort(); }

class OSString : public OSObject {
public:
    static OSString *withCString(const char *value)
    { return value ? new (std::nothrow) OSString() : nullptr; }
};
class OSNumber : public OSObject {
public:
    static OSNumber *withNumber(UInt64, unsigned)
    { return new (std::nothrow) OSNumber(); }
};

class IOHIDDevice;
inline std::vector<std::vector<uint8_t>> gIOKitHidReports;
inline std::function<IOReturn(IOHIDDevice *, const std::vector<uint8_t> &)> gIOKitHidReportCallback;

class IOHIDDevice : public IOService {
public:
    virtual IOReturn newReportDescriptor(IOMemoryDescriptor **) const { return kIOReturnUnsupported; }
    virtual OSString *newTransportString() const { return nullptr; }
    virtual OSString *newProductString() const { return nullptr; }
    virtual OSNumber *newVendorIDNumber() const { return nullptr; }
    virtual OSNumber *newProductIDNumber() const { return nullptr; }
    virtual OSNumber *newVersionNumber() const { return nullptr; }
    virtual OSNumber *newPrimaryUsagePageNumber() const { return nullptr; }
    virtual OSNumber *newPrimaryUsageNumber() const { return nullptr; }
    bool start(IOService *provider) override
    {
        IOMemoryDescriptor *descriptor = nullptr;
        if (newReportDescriptor(&descriptor) != kIOReturnSuccess || !descriptor) {
            if (descriptor) descriptor->release();
            stop(provider);
            return false;
        }
        descriptor->release();
        return IOService::start(provider);
    }
    void stop(IOService *provider) override { IOService::stop(provider); }
    virtual IOReturn handleReport(IOMemoryDescriptor *report, UInt32)
    {
        if (!report || report->getLength() > SIZE_MAX) return kIOReturnError;
        std::vector<uint8_t> bytes(static_cast<size_t>(report->getLength()));
        if (report->readBytes(0, bytes.data(), bytes.size()) != bytes.size()) return kIOReturnError;
        gIOKitHidReports.push_back(bytes);
        return gIOKitHidReportCallback ? gIOKitHidReportCallback(this, bytes) : kIOReturnSuccess;
    }
};
static constexpr UInt32 kIOHIDReportTypeInput = 0u;

struct IOStorageCompletion {
    void *target;
    void *parameter;
    void (*action)(void *, void *, IOReturn, UInt64);
};
using IOStorageAttributes = UInt32;
using IOStorageSynchronizeOptions = UInt32;
class IOStorage {
public:
    static void complete(IOStorageCompletion *completion, IOReturn result, UInt64 bytes)
    {
        if (completion && completion->action)
            completion->action(completion->target, completion->parameter, result, bytes);
    }
};

class IOBlockStorageDevice : public IOService {
public:
    virtual IOReturn doEjectMedia() { return kIOReturnError; }
    virtual IOReturn doFormatMedia(UInt64) { return kIOReturnError; }
    virtual UInt32 doGetFormatCapacities(UInt64 *, UInt32) const { return 0; }
    virtual char *getVendorString() { return nullptr; }
    virtual char *getProductString() { return nullptr; }
    virtual char *getRevisionString() { return nullptr; }
    virtual char *getAdditionalDeviceInfoString() { return nullptr; }
    virtual IOReturn reportBlockSize(UInt64 *) { return kIOReturnError; }
    virtual IOReturn reportMaxValidBlock(UInt64 *) { return kIOReturnError; }
    virtual IOReturn reportMediaState(bool *, bool *) { return kIOReturnError; }
    virtual IOReturn reportEjectability(bool *) { return kIOReturnError; }
    virtual IOReturn reportRemovability(bool *) { return kIOReturnError; }
    virtual IOReturn reportWriteProtection(bool *) { return kIOReturnError; }
    virtual IOReturn doAsyncReadWrite(IOMemoryDescriptor *, UInt64, UInt64,
                                      IOStorageAttributes *, IOStorageCompletion *)
    { return kIOReturnError; }
    virtual IOReturn doSynchronize(UInt64, UInt64, IOStorageSynchronizeOptions)
    { return kIOReturnError; }
};

#define OSDeclareDefaultStructors(type)
#define OSDefineMetaClassAndStructors(type, base)
#define OSDynamicCast(type, value) dynamic_cast<type *>(value)
#define IOLog(...) ((void)0)
