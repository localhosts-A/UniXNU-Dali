// Test-only ABI seam. This is not an IOHID implementation or kernel ABI proof.
#pragma once
#include <cassert>
#include <cstdint>
#include <cstddef>
#include <cstdlib>
#include <functional>
#include <mutex>
#include <vector>
using UInt32 = uint32_t;
using IOReturn = int;
using IOLock = std::mutex;
constexpr IOReturn kIOReturnSuccess=0, kIOReturnOffline=1, kIOReturnNotReady=2;
constexpr IOReturn kIOReturnBadArgument=3, kIOReturnNoMemory=4;
constexpr int kIODirectionNone=0, kIOHIDReportTypeInput=0, kIOServiceRequired=1;
inline IOLock *IOLockAlloc() { return new IOLock; }
inline void IOLockFree(IOLock *p) { delete p; }
inline void IOLockLock(IOLock *p) { p->lock(); }
inline void IOLockUnlock(IOLock *p) { p->unlock(); }
[[noreturn]] inline void panic(const char *) { std::abort(); }
#define OSDeclareDefaultStructors(type)
#define OSDefineMetaClassAndStructors(type, base)
#define OSDynamicCast(type, value) dynamic_cast<type *>(value)
inline int modelLiveObjects=0, modelBaseStops=0;
inline bool modelStartFailure=false, modelBufferFailure=false;
class OSObject {
  unsigned refs=1;
public:
  OSObject() { ++modelLiveObjects; }
  virtual ~OSObject() { --modelLiveObjects; }
  void retain() { ++refs; }
  void release() { assert(refs); if (!--refs) { free(); delete this; } }
  unsigned getRetainCount() const { return refs; }
  virtual void free() {}
};
class OSDictionary : public OSObject {};
class OSString : public OSObject {
public: static OSString *withCString(const char *) { return new OSString; }
};
class OSNumber : public OSObject {
public: static OSNumber *withNumber(UInt32, unsigned) { return new OSNumber; }
};
class IOMemoryDescriptor : public OSObject {};
class IOBufferMemoryDescriptor : public IOMemoryDescriptor {
public:
  std::vector<uint8_t> bytes;
  static IOBufferMemoryDescriptor *withBytes(const void *p, size_t n, int) {
    if (modelBufferFailure) { modelBufferFailure=false; return nullptr; }
    auto result=new IOBufferMemoryDescriptor;
    const auto input=static_cast<const uint8_t *>(p);
    result->bytes.assign(input, input+n);
    return result;
  }
};
class IOService : public OSObject {
public:
  virtual bool init(OSDictionary *) { return true; }
  bool attach(IOService *) { return true; }
  void detach(IOService *) {}
  bool isInactive() const { return false; }
  bool setProperty(const char *, UInt32, unsigned) { return true; }
  virtual bool start(IOService *) { return true; }
  virtual void stop(IOService *) {}
  bool terminate(int) { stop(nullptr); return true; }
};
class IOHIDDevice;
inline std::function<void(IOHIDDevice *, const std::vector<uint8_t>&)> modelReportCallback;
inline std::vector<std::vector<uint8_t>> modelReports;
class IOHIDDevice : public IOService {
public:
  virtual IOReturn newReportDescriptor(IOMemoryDescriptor **) const=0;
  bool start(IOService *p) override {
    IOMemoryDescriptor *descriptor=nullptr;
    if (modelStartFailure || newReportDescriptor(&descriptor)!=kIOReturnSuccess) {
      stop(p); return false;
    }
    descriptor->release(); return true;
  }
  void stop(IOService *) override { ++modelBaseStops; }
  virtual OSString *newTransportString() const { return nullptr; }
  virtual OSString *newProductString() const { return nullptr; }
  virtual OSNumber *newVendorIDNumber() const { return nullptr; }
  virtual OSNumber *newProductIDNumber() const { return nullptr; }
  virtual OSNumber *newVersionNumber() const { return nullptr; }
  virtual OSNumber *newPrimaryUsagePageNumber() const { return nullptr; }
  virtual OSNumber *newPrimaryUsageNumber() const { return nullptr; }
  IOReturn handleReport(IOMemoryDescriptor *descriptor, int) {
    auto report=dynamic_cast<IOBufferMemoryDescriptor *>(descriptor);
    assert(report); modelReports.push_back(report->bytes);
    if (modelReportCallback) { modelReportCallback(this, report->bytes); }
    return kIOReturnSuccess;
  }
};
