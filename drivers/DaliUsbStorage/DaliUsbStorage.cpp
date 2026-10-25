// SPDX-License-Identifier: BSD-2-Clause-Patent
#include "DaliUsbStorage.h"

#include <IOKit/IOLib.h>
#include "IOBlockStorageDevice.h"
#include "IOStorage.h"

namespace {
constexpr size_t kBounceBytes = 64 * 1024;

static int blockLock(void *context)
{
  if (context == nullptr) { return USB_INVALID; }
  IOLockLock(static_cast<IOLock *>(context));
  return USB_OK;
}

static void blockUnlock(void *context)
{
  if (context != nullptr) { IOLockUnlock(static_cast<IOLock *>(context)); }
}

static IOReturn mapUsbResult(int result)
{
  switch (result) {
  case USB_OK: return kIOReturnSuccess;
  case USB_INVALID:
  case USB_RANGE: return kIOReturnBadArgument;
  case USB_TIMEOUT: return kIOReturnTimeout;
  case USB_DISCONNECTED:
  case USB_BUSY:
  case USB_AGAIN: return kIOReturnNotReady;
  case USB_NO_MEMORY: return kIOReturnNoMemory;
  case USB_UNSUPPORTED: return kIOReturnUnsupported;
  default: return kIOReturnIOError;
  }
}
}

class DaliUsbStorage : public IOBlockStorageDevice {
  OSDeclareDefaultStructors (DaliUsbStorage)

  struct usb_bot *mBot = nullptr; // host-owned through controller-stop-proven detach
  struct usb_block_io mBlockIo = {};
  IOLock *mGate = nullptr;
  void *mBounce = nullptr;
  UInt64 mBlocks = 0;
  UInt32 mBlockSize = 0;
  bool mAdapterReady = false;
  bool mOnline = false;
  bool mReadOnly = false;

  static int copyToBot(void *context, uint64_t offset, void *destination, size_t length)
  {
    auto descriptor = static_cast<IOMemoryDescriptor *>(context);
    if (descriptor == nullptr || destination == nullptr ||
        descriptor->readBytes(offset, destination, length) != length) { return USB_IO; }
    return USB_OK;
  }

  static int copyFromBot(void *context, uint64_t offset, const void *source, size_t length)
  {
    auto descriptor = static_cast<IOMemoryDescriptor *>(context);
    if (descriptor == nullptr || source == nullptr ||
        descriptor->writeBytes(offset, source, length) != length) { return USB_IO; }
    return USB_OK;
  }

public:
  void setBot(struct usb_bot *bot) { mBot = bot; }
  bool hasStopProof()
  {
    if (!mAdapterReady || mGate == nullptr) { return false; }
    IOLockLock(mGate);
    bool stopped = mBlockIo.detached;
    IOLockUnlock(mGate);
    return stopped;
  }
  int quiesceStorage() { return mAdapterReady ? usb_block_io_quiesce(&mBlockIo) : USB_INVALID; }
  int detachStorage(bool controllerStopped)
  {
    if (!mAdapterReady) { return USB_INVALID; }
    int result = usb_block_io_detach(&mBlockIo, controllerStopped);
    if (result == USB_OK) {
      IOLockLock(mGate);
      mOnline = false;
      mBlockIo.bot = nullptr;
      mBot = nullptr;
      IOLockUnlock(mGate);
    }
    return result;
  }

  bool start(IOService *provider) override
  {
    if (provider == nullptr || mBot == nullptr || !mBot->present ||
        !IOBlockStorageDevice::start(provider)) { return false; }
    if ((!mBot->blocks || !mBot->block_size) && usb_bot_capacity(mBot) != USB_OK) { return false; }
    if (!mBot->blocks || mBot->block_size < 512 || mBot->block_size > 4096 ||
        (mBot->block_size & (mBot->block_size - 1)) != 0) { return false; }

    bool readOnly = false;
    if (usb_bot_read_only(mBot, &readOnly) != USB_OK) { return false; }
    mBlocks = mBot->blocks;
    mBlockSize = mBot->block_size;
    mGate = IOLockAlloc();
    mBounce = IOMalloc(kBounceBytes);
    if (mGate == nullptr || mBounce == nullptr) { return false; }
    if (usb_block_io_init(&mBlockIo, mBot, mBounce, kBounceBytes, mGate,
                          blockLock, blockUnlock) != USB_OK) { return false; }

    mReadOnly = readOnly;
    mAdapterReady = true;
    mOnline = true;
    registerService();
    return true;
  }

  void stop(IOService *provider) override
  {
    if (mAdapterReady) {
      int result = usb_block_io_quiesce(&mBlockIo);
      if (result != USB_OK && result != USB_DISCONNECTED) {
        IOLog("DaliUsbStorage: cache sync failed during stop (%d); host must stop DMA before detach\n", result);
      }
      IOLockLock(mGate);
      mOnline = false;
      IOLockUnlock(mGate);
    }
    IOBlockStorageDevice::stop(provider);
  }

  void free() override
  {
    if (mAdapterReady && !mBlockIo.detached) {
      panic("DaliUsbStorage: refusing to release BOT buffers before controller-stop proof");
    }
    if (mBounce != nullptr) { IOFree(mBounce, kBounceBytes); mBounce = nullptr; }
    if (mGate != nullptr) { IOLockFree(mGate); mGate = nullptr; }
    IOBlockStorageDevice::free();
  }

  IOReturn doEjectMedia() override { return kIOReturnUnsupported; }
  IOReturn doFormatMedia(UInt64) override { return kIOReturnNotWritable; }
  UInt32 doGetFormatCapacities(UInt64 *, UInt32) const override { return 0; }
  char *getVendorString() override { return const_cast<char *>("USB"); }
  char *getProductString() override { return const_cast<char *>("USB Mass Storage BOT"); }
  char *getRevisionString() override { return const_cast<char *>("2.0"); }
  char *getAdditionalDeviceInfoString() override { return const_cast<char *>("SCSI transparent/BOT, LUN 0"); }

  IOReturn reportBlockSize(UInt64 *value) override
  {
    if (value == nullptr || !mBlockSize) { return kIOReturnNotReady; }
    *value = mBlockSize;
    return kIOReturnSuccess;
  }

  IOReturn reportMaxValidBlock(UInt64 *value) override
  {
    if (value == nullptr || !mBlocks) { return kIOReturnNotReady; }
    *value = mBlocks - 1;
    return kIOReturnSuccess;
  }

  IOReturn reportMediaState(bool *present, bool *changed) override
  {
    if (present == nullptr || mGate == nullptr) { return kIOReturnBadArgument; }
    IOLockLock(mGate);
    *present = mOnline && mBlockIo.online;
    if (changed != nullptr) { *changed = false; }
    IOLockUnlock(mGate);
    return kIOReturnSuccess;
  }

  IOReturn reportEjectability(bool *value) override
  {
    if (value == nullptr) { return kIOReturnBadArgument; }
    *value = false;
    return kIOReturnSuccess;
  }

  IOReturn reportRemovability(bool *value) override
  {
    if (value == nullptr) { return kIOReturnBadArgument; }
    *value = true;
    return kIOReturnSuccess;
  }

  IOReturn reportWriteProtection(bool *value) override
  {
    if (value == nullptr) { return kIOReturnBadArgument; }
    *value = mReadOnly;
    return kIOReturnSuccess;
  }

  IOReturn doAsyncReadWrite(IOMemoryDescriptor *buffer, UInt64 block, UInt64 count,
                            IOStorageAttributes *, IOStorageCompletion *completion) override
  {
    if (buffer == nullptr || completion == nullptr || completion->action == nullptr ||
        !mAdapterReady || count == 0 || !mBlockSize ||
        block >= mBlocks || count > mBlocks - block ||
        count > UINT64_MAX / mBlockSize) { return kIOReturnBadArgument; }
    UInt64 bytes = count * mBlockSize;
    if (bytes > buffer->getLength() || bytes > SIZE_MAX) { return kIOReturnBadArgument; }
    IODirection direction = buffer->getDirection();
    bool writing = direction == kIODirectionOut;
    if (direction != kIODirectionIn && !writing) { return kIOReturnBadArgument; }
    if (writing && mReadOnly) { return kIOReturnNotWritable; }

    struct usb_block_buffer source = {buffer, bytes,
      writing ? copyToBot : nullptr, writing ? nullptr : copyFromBot};
    UInt64 completed = 0;
    int result = usb_block_io_transfer(&mBlockIo, writing, block, count, &source, &completed);
    IOStorage::complete(completion, mapUsbResult(result), completed);
    return kIOReturnSuccess;
  }

  IOReturn doSynchronize(UInt64 block, UInt64 count, IOStorageSynchronizeOptions) override
  {
    if (!mAdapterReady || block > mBlocks ||
        (count != 0 && count > mBlocks - block)) { return kIOReturnBadArgument; }
    return mapUsbResult(usb_block_io_sync(&mBlockIo));
  }
};

OSDefineMetaClassAndStructors (DaliUsbStorage, IOBlockStorageDevice)

extern "C" IOService *dali_usb_storage_publish(IOService *provider, struct usb_bot *bot)
{
  if (provider == nullptr || bot == nullptr || !bot->present) { return nullptr; }
  auto storage = new DaliUsbStorage;
  if (storage == nullptr || !storage->init()) {
    if (storage != nullptr) { storage->release(); }
    return nullptr;
  }
  storage->setBot(bot);
  if (!storage->attach(provider)) {
    storage->release();
    return nullptr;
  }
  if (!storage->start(provider)) {
    storage->stop(provider);
    storage->detach(provider);
    storage->release();
    return nullptr;
  }
  return storage;
}

extern "C" int dali_usb_storage_quiesce(IOService *service)
{
  auto storage = OSDynamicCast(DaliUsbStorage, service);
  return storage == nullptr ? USB_INVALID : storage->quiesceStorage();
}

extern "C" int dali_usb_storage_detach(IOService *service, bool controllerStopped)
{
  auto storage = OSDynamicCast(DaliUsbStorage, service);
  return storage == nullptr ? USB_INVALID : storage->detachStorage(controllerStopped);
}

extern "C" int dali_usb_storage_terminate(IOService *service)
{
  auto storage = OSDynamicCast(DaliUsbStorage, service);
  if (storage == nullptr) { return USB_INVALID; }
  if (!storage->hasStopProof()) { return USB_BUSY; }
  return service->terminate(kIOServiceRequired) ? USB_OK : USB_IO;
}
