// Test-only IOKit seam: this does not establish a kernel ABI or runtime result.
#pragma once
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <vector>

using UInt32 = uint32_t;
using UInt64 = uint64_t;
using IOReturn = int;
using IOStorageSynchronizeOptions = uint32_t;
using IOLock = std::mutex;
enum IODirection { kIODirectionNone, kIODirectionIn, kIODirectionOut };
constexpr IOReturn kIOReturnSuccess = 0, kIOReturnBadArgument = 1;
constexpr IOReturn kIOReturnTimeout = 2, kIOReturnNotReady = 3;
constexpr IOReturn kIOReturnNoMemory = 4, kIOReturnUnsupported = 5;
constexpr IOReturn kIOReturnIOError = 6, kIOReturnNotWritable = 7;
constexpr int kIOServiceRequired = 1;

inline int modelLiveObjects = 0, modelLocks = 0, modelBuffers = 0, modelBaseStops = 0;
inline bool modelFailBufferAllocation = false;
inline IOLock *IOLockAlloc() { ++modelLocks; return new IOLock; }
inline void IOLockFree(IOLock *lock) { assert(lock && modelLocks); --modelLocks; delete lock; }
inline void IOLockLock(IOLock *lock) { lock->lock(); }
inline void IOLockUnlock(IOLock *lock) { lock->unlock(); }
inline void *IOMalloc(size_t size)
{
    if (modelFailBufferAllocation) { modelFailBufferAllocation = false; return nullptr; }
    void *result = std::malloc(size);
    if (result) ++modelBuffers;
    return result;
}
inline void IOFree(void *buffer, size_t)
{ assert(buffer && modelBuffers); --modelBuffers; std::free(buffer); }
[[noreturn]] inline void panic(const char *) { std::abort(); }
#define IOLog(...) ((void)0)
#define OSDeclareDefaultStructors(type)
#define OSDefineMetaClassAndStructors(type, base)
#define OSDynamicCast(type, value) dynamic_cast<type *>(value)

class OSObject {
    unsigned refs = 1;
public:
    OSObject() { ++modelLiveObjects; }
    virtual ~OSObject() { --modelLiveObjects; }
    void retain() { ++refs; }
    void release() { assert(refs); if (!--refs) { free(); delete this; } }
    virtual void free() {}
};
class OSDictionary : public OSObject {};

class IOService : public OSObject {
    IOService *provider = nullptr;
    bool started = false;
public:
    bool registered = false;
    virtual bool init(OSDictionary *) { return true; }
    bool attach(IOService *parent) { provider = parent; return parent != nullptr; }
    void detach(IOService *) { provider = nullptr; }
    virtual bool start(IOService *) { started = true; return true; }
    virtual void stop(IOService *) { started = false; }
    void registerService() { registered = true; }
    bool terminate(int)
    {
        if (!started) return false;
        stop(provider);
        return true;
    }
};

class IOMemoryDescriptor : public OSObject {
    IODirection direction;
public:
    std::vector<uint8_t> bytes;
    IOMemoryDescriptor(IODirection value, size_t length) : direction(value), bytes(length) {}
    IODirection getDirection() const { return direction; }
    UInt64 getLength() const { return bytes.size(); }
    size_t readBytes(UInt64 offset, void *destination, size_t length)
    {
        if (offset > bytes.size() || length > bytes.size() - static_cast<size_t>(offset)) return 0;
        std::memcpy(destination, bytes.data() + static_cast<size_t>(offset), length);
        return length;
    }
    size_t writeBytes(UInt64 offset, const void *source, size_t length)
    {
        if (offset > bytes.size() || length > bytes.size() - static_cast<size_t>(offset)) return 0;
        std::memcpy(bytes.data() + static_cast<size_t>(offset), source, length);
        return length;
    }
};

struct IOStorageAttributes {};
struct IOStorageCompletion {
    void *target;
    void *parameter;
    void (*action)(void *, void *, IOReturn, UInt64);
};
class IOStorage {
public:
    static void complete(IOStorageCompletion *completion, IOReturn result, UInt64 bytes)
    { completion->action(completion->target, completion->parameter, result, bytes); }
};
class IOBlockStorageDevice : public IOService {
public:
    void stop(IOService *provider) override { ++modelBaseStops; IOService::stop(provider); }
    virtual IOReturn doEjectMedia() { return kIOReturnUnsupported; }
    virtual IOReturn doFormatMedia(UInt64) { return kIOReturnUnsupported; }
    virtual UInt32 doGetFormatCapacities(UInt64 *, UInt32) const { return 0; }
    virtual char *getVendorString() { return nullptr; }
    virtual char *getProductString() { return nullptr; }
    virtual char *getRevisionString() { return nullptr; }
    virtual char *getAdditionalDeviceInfoString() { return nullptr; }
    virtual IOReturn reportBlockSize(UInt64 *) { return kIOReturnUnsupported; }
    virtual IOReturn reportMaxValidBlock(UInt64 *) { return kIOReturnUnsupported; }
    virtual IOReturn reportMediaState(bool *, bool *) { return kIOReturnUnsupported; }
    virtual IOReturn reportEjectability(bool *) { return kIOReturnUnsupported; }
    virtual IOReturn reportRemovability(bool *) { return kIOReturnUnsupported; }
    virtual IOReturn reportWriteProtection(bool *) { return kIOReturnUnsupported; }
    virtual IOReturn doAsyncReadWrite(IOMemoryDescriptor *, UInt64, UInt64,
                                      IOStorageAttributes *, IOStorageCompletion *)
    { return kIOReturnUnsupported; }
    virtual IOReturn doSynchronize(UInt64, UInt64, IOStorageSynchronizeOptions)
    { return kIOReturnUnsupported; }
};
