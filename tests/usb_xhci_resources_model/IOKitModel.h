/* Test-only IOKit seam; this is not a kernel ABI or hardware qualification. */
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

using IOOptionBits = uint32_t;
static constexpr IOOptionBits kIOMapAnywhere = 1u;
static constexpr IOOptionBits kIOMapInhibitCache = 2u;

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
public:
    virtual ~OSObject() = default;
    virtual void release() { delete this; }
};

class IOMemoryMap : public OSObject {
    IOKitMemoryModel *mModel;

public:
    explicit IOMemoryMap(IOKitMemoryModel *model) : mModel(model) {}
    uint64_t getVirtualAddress() const { return 0xfeed0000u; }
    void release() override { ++mModel->map_releases; delete this; }
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
    void release() override { ++mModel->memory_releases; delete this; }
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

class IOService : public OSObject {
    std::unordered_map<std::string, OSObject *> mProperties;

public:
    bool started = false;
    bool registered = false;

    virtual bool start(IOService *)
    {
        started = true;
        return true;
    }

    virtual void stop(IOService *) { started = false; }
    OSObject *getProperty(const char *key)
    {
        auto found = mProperties.find(key);
        return found == mProperties.end() ? nullptr : found->second;
    }
    void setTestProperty(const char *key, OSObject *value) { mProperties[key] = value; }
    void registerService() { registered = true; }
};

#define OSDeclareDefaultStructors(type)
#define OSDefineMetaClassAndStructors(type, base)
#define OSDynamicCast(type, value) dynamic_cast<type *>(value)
#define IOLog(...) ((void)0)
