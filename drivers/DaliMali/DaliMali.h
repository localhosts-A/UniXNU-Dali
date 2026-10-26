// SPDX-License-Identifier: BSD-2-Clause-Patent
#pragma once
#include "NativeMaliXnuMemory.h"
#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOTimerEventSource.h>
#include <IOKit/IOWorkLoop.h>
struct DaliMaliContext {
    native_mali::XnuMemoryOwner owner{};
    native_mali::Session session{};
    bool connected = false;
};
class DaliMali : public IOService {
    OSDeclareDefaultStructors(DaliMali)
public:
    bool start(IOService *) override;
    void stop(IOService *) override;
    void free() override;
    IOReturn newUserClient(task_t,void *,UInt32,OSDictionary *,IOUserClient **) override;
    DaliMaliContext *connect(task_t);
    void disconnect(DaliMaliContext *);
    uint32_t call(DaliMaliContext *,const nml_request &,nml_response *);
    IOReturn memory(DaliMaliContext *,uint32_t kind,uint32_t object,uint32_t stream,IOMemoryDescriptor **);
private:
    struct Window { native_mali::WindowId id{};uint64_t bytes=0;IOMemoryMap *map=nullptr; } windows_[32]{};
    size_t windowCount_=0;IOLock *lock_=nullptr;bool online_=false,held_=false;
    OSData *resourceData_=nullptr,*firmwareData_=nullptr;
    native_mali::Resources resources_{};native_mali::XnuMemory memory_{};
    native_mali::XnuMemoryOwner firmwareOwner_{};
    native_mali::Boot boot_{};native_mali::Scheduler scheduler_{};
    DaliMaliContext *contexts_[16]{};
    IOWorkLoop *loop_=nullptr;IOTimerEventSource *timer_=nullptr;
    Window *window(native_mali::WindowId);
    bool mapResources();void unmapResources();
    static bool read(void *,native_mali::WindowId,uint32_t,uint32_t *);
    static bool write(void *,native_mali::WindowId,uint32_t,uint32_t);
    static bool gpuRead(void *,uint32_t,uint32_t *);
    static bool gpuWrite(void *,uint32_t,uint32_t);
    static bool zeroShared(void *,size_t,size_t);
    static void barrier(void *);
    static bool oom(void *,uint8_t,uint64_t,uint32_t,uint32_t,uint32_t,uint64_t *);
    static void tick(OSObject *,IOTimerEventSource *);
};
class DaliMaliUserClient : public IOUserClient {
    OSDeclareDefaultStructors(DaliMaliUserClient)
public:
    bool initWithTask(task_t,void *,UInt32,OSDictionary *) override;
    bool start(IOService *) override;
    IOReturn clientClose() override;
    void stop(IOService *) override;
    void free() override;
    IOReturn externalMethod(uint32_t,IOExternalMethodArguments *,IOExternalMethodDispatch *,OSObject *,void *) override;
    IOReturn clientMemoryForType(UInt32,IOOptionBits *,IOMemoryDescriptor **) override;
private:
    task_t task_=TASK_NULL;DaliMali *controller_=nullptr;DaliMaliContext *context_=nullptr;
    IOLock *lock_=nullptr;uint32_t serial_=0;
    struct Token { uint32_t token=0,kind=0,object=0,stream=0; } tokens_[768]{};
    uint32_t token(uint32_t,uint32_t,uint32_t);
    void invalidate(uint32_t,uint32_t,uint32_t,bool allStreams=false);
    void closeContext();
    static IOReturn action(OSObject *,void *,IOExternalMethodArguments *);
};
