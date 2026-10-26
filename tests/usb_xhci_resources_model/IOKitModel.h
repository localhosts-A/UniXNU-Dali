/* Test-only IOKit seam; this is not a kernel ABI or hardware qualification. */
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

class OSObject {
public:
    virtual ~OSObject() = default;
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
