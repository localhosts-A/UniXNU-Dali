// SPDX-License-Identifier: BSD-2-Clause-Patent
#include "DaliUsbHid.h"
#include <IOKit/hid/IOHIDDevice.h>
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOLocks.h>

class DaliUsbHid : public IOHIDDevice {
  OSDeclareDefaultStructors(DaliUsbHid)
  IOLock *mGate = nullptr;
  const uint8_t *mDescriptor = nullptr; // immutable UsbHost static descriptor
  size_t mDescriptorLength = 0;
  size_t mReportLength = 0;
  UInt32 mVendor = 0, mProduct = 0, mVersion = 0;
  enum usb_host_hid_kind mKind = USB_HOST_HID_KEYBOARD;
  bool mActive = false, mAccepting = false, mReporting = false;
  bool mNeutral = true, mQuiesced = true, mStopProven = false;
  bool mLifetimeLease = false;
  bool mStopPending = false, mStopping = false, mBaseStopped = false;
  IOService *mStopProvider = nullptr;

  void completeStop(IOService *provider)
  {
    // A failed neutral report cannot keep framework event sources alive.
    // Admission is closed and this path runs only after the prior report ends.
    (void)quiesce();
    IOHIDDevice::stop(provider);
    IOLockLock(mGate);
    mActive = mAccepting = false;
    mQuiesced = mNeutral = mBaseStopped = true;
    mStopping = false;
    IOLockUnlock(mGate);
    if (provider != nullptr) { provider->release(); }
  }

  int deliver(const uint8_t *bytes, size_t length, bool neutral)
  {
    if (mGate == nullptr || bytes == nullptr || length != mReportLength ||
        (mKind == USB_HOST_HID_KEYBOARD && bytes[1]) ||
        (mKind == USB_HOST_HID_MOUSE && (bytes[0] & 0xe0))) { return USB_INVALID; }
    IOLockLock(mGate);
    if (!mActive || mStopProven || (!neutral && !mAccepting)) {
      IOLockUnlock(mGate); return USB_DISCONNECTED;
    }
    if (mReporting) { IOLockUnlock(mGate); return USB_BUSY; }
    IOBufferMemoryDescriptor *report = IOBufferMemoryDescriptor::withBytes(bytes, length, kIODirectionNone);
    if (report == nullptr) { IOLockUnlock(mGate); return USB_NO_MEMORY; }
    mReporting = true;
    retain();
    IOLockUnlock(mGate);

    // IOHID owns its workloop lock. Do not hold our admission lock across a
    // synchronous report callback, which may itself request quiescence.
    IOReturn result = handleReport(report, kIOHIDReportTypeInput);
    report->release();
    IOLockLock(mGate);
    mReporting = false;
    if (result == kIOReturnSuccess) { mNeutral = neutral; }
    else { mAccepting = false; }
    bool stopPending = mStopPending && !mStopping;
    IOService *stopProvider = nullptr;
    if (stopPending) {
      mStopPending = false;
      mStopping = true;
      stopProvider = mStopProvider;
      mStopProvider = nullptr;
    }
    IOLockUnlock(mGate);
    if (stopPending) { completeStop(stopProvider); }
    release();
    if (result == kIOReturnSuccess) { return USB_OK; }
    if (result == kIOReturnOffline || result == kIOReturnNotReady) { return USB_DISCONNECTED; }
    return USB_IO;
  }

public:
  bool init(OSDictionary *dictionary = nullptr) override
  {
    if (!IOHIDDevice::init(dictionary)) { return false; }
    mGate = IOLockAlloc();
    return mGate != nullptr;
  }
  bool configure(const struct usb_host_device *device, uint8_t index)
  {
    if (device == nullptr || device->state != USB_HOST_CONFIGURED ||
        index >= device->hid_count || index >= USB_HOST_MAX_HID_INTERFACES ||
        !device->hid[index].active) { return false; }
    mKind = device->hid[index].kind;
    if (usb_host_boot_report_descriptor(mKind, &mDescriptor, &mDescriptorLength)) { return false; }
    mReportLength = mKind == USB_HOST_HID_KEYBOARD ? 8 : 3;
    mVendor = device->device_descriptor[8] | (UInt32)device->device_descriptor[9] << 8;
    mProduct = device->device_descriptor[10] | (UInt32)device->device_descriptor[11] << 8;
    mVersion = device->device_descriptor[12] | (UInt32)device->device_descriptor[13] << 8;
    return setProperty("DaliUsbInterfaceNumber", device->hid[index].interface_number, 32);
  }
  bool activate()
  {
    IOLockLock(mGate);
    if (mBaseStopped || mStopping || mStopPending || isInactive()) {
      IOLockUnlock(mGate); return false;
    }
    retain(); // retained until independent controller-stop proof
    mLifetimeLease = true;
    mActive = mAccepting = true;
    mQuiesced = false;
    IOLockUnlock(mGate);
    return true;
  }
  int submit(const uint8_t *bytes, size_t length) { return deliver(bytes, length, false); }
  int quiesce()
  {
    if (mGate == nullptr) { return USB_INVALID; }
    IOLockLock(mGate);
    mAccepting = false;
    if (mReporting) { IOLockUnlock(mGate); return USB_BUSY; }
    if (!mActive || mNeutral) {
      mQuiesced = true; IOLockUnlock(mGate); return USB_OK;
    }
    IOLockUnlock(mGate);
    const uint8_t released[8] = {};
    int result = deliver(released, mReportLength, true);
    IOLockLock(mGate);
    if (result == USB_OK || result == USB_DISCONNECTED) { mQuiesced = true; result = USB_OK; }
    IOLockUnlock(mGate);
    return result;
  }
  int detachController(bool stopped)
  {
    if (!stopped) { return USB_BUSY; }
    IOLockLock(mGate);
    if (!mQuiesced || mReporting) { IOLockUnlock(mGate); return USB_BUSY; }
    mStopProven = true;
    mActive = mAccepting = false;
    bool releaseLease = mLifetimeLease;
    mLifetimeLease = false;
    IOLockUnlock(mGate);
    if (releaseLease) { release(); }
    return USB_OK;
  }
  bool canTerminate()
  {
    IOLockLock(mGate);
    bool allowed = mStopProven && !mReporting;
    IOLockUnlock(mGate);
    return allowed;
  }
  void stop(IOService *provider) override
  {
    if (mGate == nullptr) { IOHIDDevice::stop(provider); return; }
    IOLockLock(mGate);
    mAccepting = false;
    if (mBaseStopped || mStopping || mStopPending) { IOLockUnlock(mGate); return; }
    if (provider != nullptr) { provider->retain(); }
    if (mReporting) {
      mStopPending = true;
      mStopProvider = provider;
      IOLockUnlock(mGate);
      return; // deliver() completes this stop after synchronous callbacks return
    }
    mStopping = true;
    IOLockUnlock(mGate);
    completeStop(provider);
  }
  void free() override
  {
    if (mLifetimeLease || mReporting || mStopPending || mStopping) {
      panic("DaliUsbHid: premature lifetime release");
    }
    if (mGate != nullptr) { IOLockFree(mGate); mGate = nullptr; }
    IOHIDDevice::free();
  }
  IOReturn newReportDescriptor(IOMemoryDescriptor **descriptor) const override
  {
    if (descriptor == nullptr || mDescriptor == nullptr) { return kIOReturnBadArgument; }
    *descriptor = IOBufferMemoryDescriptor::withBytes(mDescriptor, mDescriptorLength, kIODirectionNone);
    return *descriptor != nullptr ? kIOReturnSuccess : kIOReturnNoMemory;
  }
  OSString *newTransportString() const override { return OSString::withCString("USB"); }
  OSString *newProductString() const override
  { return OSString::withCString(mKind == USB_HOST_HID_KEYBOARD ? "USB boot keyboard" : "USB boot mouse"); }
  OSNumber *newVendorIDNumber() const override { return OSNumber::withNumber(mVendor, 32); }
  OSNumber *newProductIDNumber() const override { return OSNumber::withNumber(mProduct, 32); }
  OSNumber *newVersionNumber() const override { return OSNumber::withNumber(mVersion, 32); }
  OSNumber *newPrimaryUsagePageNumber() const override { return OSNumber::withNumber((UInt32)1, 32); }
  OSNumber *newPrimaryUsageNumber() const override
  { return OSNumber::withNumber((UInt32)(mKind == USB_HOST_HID_KEYBOARD ? 6 : 2), 32); }
};
OSDefineMetaClassAndStructors(DaliUsbHid, IOHIDDevice)

extern "C" IOService *dali_usb_hid_publish(IOService *provider, const struct usb_host_device *device, uint8_t index)
{
  if (provider == nullptr) { return nullptr; }
  auto hid = new DaliUsbHid;
  if (hid == nullptr || !hid->init(nullptr)) {
    if (hid != nullptr) { hid->release(); }
    return nullptr;
  }
  if (!hid->configure(device, index) || !hid->attach(provider)) { hid->release(); return nullptr; }
  // IOHIDDevice::start calls stop on its own failure path.
  if (!hid->start(provider)) { hid->detach(provider); hid->release(); return nullptr; }
  if (!hid->activate()) {
    hid->stop(provider); hid->detach(provider); hid->release(); return nullptr;
  }
  return hid;
}
extern "C" int dali_usb_hid_submit(IOService *service, const uint8_t *bytes, size_t length)
{
  auto hid = OSDynamicCast(DaliUsbHid, service);
  return hid != nullptr ? hid->submit(bytes, length) : USB_INVALID;
}
extern "C" int dali_usb_hid_quiesce(IOService *service)
{
  auto hid = OSDynamicCast(DaliUsbHid, service);
  return hid != nullptr ? hid->quiesce() : USB_INVALID;
}
extern "C" int dali_usb_hid_detach(IOService *service, bool stopped)
{
  auto hid = OSDynamicCast(DaliUsbHid, service);
  return hid != nullptr ? hid->detachController(stopped) : USB_INVALID;
}
extern "C" int dali_usb_hid_terminate(IOService *service)
{
  auto hid = OSDynamicCast(DaliUsbHid, service);
  if (hid == nullptr) { return USB_INVALID; }
  if (!hid->canTerminate()) { return USB_BUSY; }
  return hid->terminate(kIOServiceRequired) ? USB_OK : USB_IO;
}
