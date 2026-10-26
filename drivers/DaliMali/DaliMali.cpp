// SPDX-License-Identifier: BSD-2-Clause-Patent
#include "DaliMali.h"
#include <IOKit/IODeviceTreeSupport.h>
#include <libkern/OSByteOrder.h>
#include <pexpert/pexpert.h>
#include <vm/vm_map.h>
#include <mach/vm_param.h>
extern "C" uint64_t gPhysBase;
extern "C" vm_size_t mem_size;
using namespace native_mali;
DaliMali::Window *DaliMali::window(WindowId id){for(size_t i=0;i<windowCount_;++i)if(windows_[i].id==id)return windows_+i;return nullptr;}
bool DaliMali::mapResources(){
    const WindowId ids[]={WindowId::Gpu,WindowId::MfgTop,WindowId::PlatformSmmu,WindowId::PowerRpc,WindowId::Pll,WindowId::PllSc0,
        WindowId::PllSc1,WindowId::Sleep,WindowId::VcoreAo,WindowId::Hbvc,WindowId::CpuWrap,WindowId::ClockSystem,WindowId::Gpueb,
        WindowId::GpuebGpr,WindowId::GpuebConfig,WindowId::GpuebIntc,WindowId::GpuebDma,WindowId::Mbox0,WindowId::MboxInit,
        WindowId::MboxSet,WindowId::MboxClear,WindowId::MboxSend,WindowId::MboxReceive,WindowId::Mfg0Power,WindowId::GhpmEco,
        WindowId::SpmMfg0Power,WindowId::GpuebClock,WindowId::GpuebClockSet,WindowId::GpuebClockClear,WindowId::GpuebShared};
    for(auto id:ids){const auto *r=resources_.window(id);if(!r)continue;
        auto *descriptor=IOMemoryDescriptor::withPhysicalAddress(r->base,r->bytes,kIODirectionInOut|kIOMemoryMapperNone);
        if(!descriptor)return false;
        auto *map=descriptor->map(kIOMapAnywhere|(r->kind==WindowKind::ReservedMemory?kIOMapWriteCombine:kIOMapInhibitCache));
        descriptor->release();if(!map||map->getLength()<r->bytes){if(map)map->release();return false;}
        windows_[windowCount_++]={id,r->bytes,map};
    }return true;
}
void DaliMali::unmapResources(){for(size_t i=0;i<windowCount_;++i){if(windows_[i].map)windows_[i].map->release();windows_[i]={};}windowCount_=0;}
bool DaliMali::read(void *c,WindowId id,uint32_t off,uint32_t *out){
    auto *w=static_cast<DaliMali *>(c)->window(id);if(!w||!out||(off&3)||off>w->bytes-4)return false;
    __asm__ volatile("dsb osh":::"memory");*out=*reinterpret_cast<volatile uint32_t *>(w->map->getVirtualAddress()+off);
    __asm__ volatile("dmb oshld":::"memory");return true;
}
bool DaliMali::write(void *c,WindowId id,uint32_t off,uint32_t value){
    auto *w=static_cast<DaliMali *>(c)->window(id);if(!w||(off&3)||off>w->bytes-4)return false;
    __asm__ volatile("dmb oshst":::"memory");*reinterpret_cast<volatile uint32_t *>(w->map->getVirtualAddress()+off)=value;
    __asm__ volatile("dsb oshst":::"memory");return true;
}
bool DaliMali::gpuRead(void *c,uint32_t off,uint32_t *out){return read(c,WindowId::Gpu,off,out);}
bool DaliMali::gpuWrite(void *c,uint32_t off,uint32_t value){return write(c,WindowId::Gpu,off,value);}
void DaliMali::barrier(void *){__asm__ volatile("dsb sy":::"memory");}
bool DaliMali::zeroShared(void *c,size_t offset,size_t bytes){
    auto *w=static_cast<DaliMali *>(c)->window(WindowId::GpuebShared);
    if(!w||offset>w->bytes||bytes>w->bytes-offset)return false;
    bzero(reinterpret_cast<void *>(w->map->getVirtualAddress()+offset),bytes);barrier(c);return true;
}
bool DaliMali::oom(void *c,uint8_t as,uint64_t heap,uint32_t start,uint32_t end,uint32_t frag,uint64_t *chunk){
    auto *self=static_cast<DaliMali *>(c);return as<16&&self->contexts_[as]&&self->contexts_[as]->session.oom(heap,start,end,frag,chunk);
}
void DaliMali::tick(OSObject *owner,IOTimerEventSource *){
    auto *self=OSDynamicCast(DaliMali,owner);if(!self||!self->lock_)return;
    IOLockLock(self->lock_);
    if(self->online_){if(self->scheduler_.poll()!=QueueResult::Ok){self->online_=false;self->setProperty("device-lost",true);}
        else self->timer_->setTimeoutMS(2);}
    IOLockUnlock(self->lock_);
}
bool DaliMali::start(IOService *provider){
    unsigned enabled=0;
    if(!PE_parse_boot_argn("dali-mali",&enabled,sizeof(enabled))||enabled!=1||page_size!=4096||
        vm_map_page_size(kernel_map)!=4096)return false;
    auto *node=IORegistryEntry::fromPath("/arm-io/dali-mali",gIODTPlane);
    resourceData_=node?OSDynamicCast(OSData,node->getProperty("resources-v1")):nullptr;
    firmwareData_=node?OSDynamicCast(OSData,node->getProperty("csf-firmware")):nullptr;
    if(resourceData_)resourceData_->retain();if(firmwareData_)firmwareData_->retain();if(node)node->release();
    if(!resourceData_||!firmwareData_||!resources_.decode(resourceData_->getBytesNoCopy(),resourceData_->getLength()))return false;
    const auto *shared=resources_.window(WindowId::GpuebShared);
    // maxmem is required by this profile. An ADT memory-map property alone does
    // not reserve RAM from XNU's page allocator. Validate the actual managed end
    // before mapping or writing GPUEB shared memory or any power register.
    if(!shared||gPhysBase!=0x80000000ull||!mem_size||gPhysBase>shared->base||mem_size>shared->base-gPhysBase){
        IOLog("DaliMali: GPUEB reservation overlaps XNU managed RAM; require maxmem=1992\n");return false;}
    if(!IOService::start(provider))return false;
    lock_=IOLockAlloc();if(!lock_||!mapResources())return false;
    firmwareOwner_={&memory_,kernel_task};BootConfig config{};
    config.resources=resourceData_->getBytesNoCopy();config.resourceBytes=resourceData_->getLength();
    config.firmware=static_cast<const uint8_t *>(firmwareData_->getBytesNoCopy());config.firmwareBytes=firmwareData_->getLength();
    config.platform={this,read,write,barrier,XnuMemory::delay,zeroShared};config.registers={this,gpuRead,gpuWrite,XnuMemory::delay};
    config.tables=memory_.tableOps();config.memory=memory_.memoryOps(&firmwareOwner_);config.sharedReservationOwned=true;
    retain();held_=true;const auto result=boot_.start(config);
    setProperty("boot-stage",uint64_t(boot_.stage()),32);setProperty("boot-detail",uint64_t(boot_.detail()),32);
    if(result!=BootResult::Ok){IOLog("DaliMali: boot failed stage=%u detail=%u retained=%u\n",unsigned(boot_.failedStage()),boot_.detail(),boot_.stage()==BootStage::Retained);
        if(boot_.stage()!=BootStage::Retained&&memory_.empty()){held_=false;release();}return false;}
    if(scheduler_.initialize(boot_,{this,XnuMemory::delay,oom},100000)!=QueueResult::Ok){
        boot_.stop();if(boot_.hardware().quiesced()&&memory_.empty()){held_=false;release();}return false;}
    loop_=IOWorkLoop::workLoop();timer_=IOTimerEventSource::timerEventSource(this,tick);
    if(!loop_||!timer_||loop_->addEventSource(timer_)!=kIOReturnSuccess){boot_.stop();
        if(boot_.hardware().quiesced()&&memory_.empty()){held_=false;release();}return false;}
    setName(NML_SERVICE_NAME);setProperty("NativeMaliABI",uint64_t(NML_ABI_VERSION),32);
    setProperty("GPU-ID",uint64_t(boot_.properties()->gpuId),32);setProperty("hardware-qualified",false);
    online_=true;timer_->setTimeoutMS(2);registerService();return true;
}
DaliMaliContext *DaliMali::connect(task_t task){
    if(!lock_||!task)return nullptr;IOLockLock(lock_);DaliMaliContext *context=nullptr;
    if(online_)for(uint8_t as=1;as<16;++as)if(!contexts_[as]&&(boot_.properties()->addressSpaces&(1u<<as))){
        context=new DaliMaliContext;if(!context)break;context->owner={&memory_,task};task_reference(task);
        SessionOps ops{memory_.memoryOps(&context->owner),memory_.tableOps(),XnuMemory::importHost,XnuMemory::delay,XnuMemory::now};
        const auto result=context->session.initialize(boot_,scheduler_,as,ops,256*1024*1024);
        if(result==NML_OK){context->connected=true;contexts_[as]=context;}
        else if(context->session.close()!=NML_OK){contexts_[as]=context;context=nullptr;}
        else {task_deallocate(task);delete context;context=nullptr;}break;
    }IOLockUnlock(lock_);return context;
}
void DaliMali::disconnect(DaliMaliContext *context){
    if(!context||!lock_)return;IOLockLock(lock_);context->connected=false;
    const auto as=context->session.addressSpace();
    if(context->session.close(boot_.hardware().quiesced())==NML_OK){if(as<16&&contexts_[as]==context)contexts_[as]=nullptr;
        task_deallocate(context->owner.task);delete context;}
    else IOLog("DaliMali: retaining AS%u after unconfirmed stop\n",as);
    IOLockUnlock(lock_);
}
uint32_t DaliMali::call(DaliMaliContext *context,const nml_request &request,nml_response *out){
    if(!context||!lock_)return NML_STATE;IOLockLock(lock_);
    const auto result=online_&&context->connected?context->session.call(request,out):uint32_t(NML_DEVICE_LOST);
    IOLockUnlock(lock_);return result;
}
IOReturn DaliMali::memory(DaliMaliContext *context,uint32_t kind,uint32_t object,uint32_t stream,IOMemoryDescriptor **out){
    if(!out||!context||!lock_)return kIOReturnBadArgument;*out=nullptr;IOLockLock(lock_);
    FirmwareAllocation allocation{};size_t bytes=0,offset=0;bool valid=false;
    if(online_&&context->connected){
        if(kind==1)valid=context->session.bufferBacking(object,&allocation,&bytes);
        else if(kind==2){QueueView view{};valid=context->session.queueBacking(object,stream,&view)&&
            boot_.firmwareMemory().backing(view.output,4096,&allocation,&offset);bytes=4096;}
    }
    if(valid){auto *base=memory_.descriptor(allocation);if(base){
        if(kind==1)*out=base;
        else {*out=IOMemoryDescriptor::withSubRange(base,offset,bytes,kIODirectionOut);base->release();}
    }}IOLockUnlock(lock_);return *out?kIOReturnSuccess:kIOReturnNotFound;
}
void DaliMali::stop(IOService *provider){
    if(timer_){timer_->cancelTimeout();timer_->disable();}
    if(lock_){IOLockLock(lock_);online_=false;
        if(boot_.stop()==BootResult::Ok){scheduler_.resetAfterQuiescence();
            for(auto &context:contexts_)if(context){const auto result=context->session.close(true);
                if(result==NML_OK&&!context->connected){task_deallocate(context->owner.task);delete context;context=nullptr;}}}
        IOLockUnlock(lock_);}
    if(held_&&boot_.hardware().quiesced()&&memory_.empty()){held_=false;release();}
    IOService::stop(provider);
}
void DaliMali::free(){
    if(timer_){if(loop_)loop_->removeEventSource(timer_);timer_->release();timer_=nullptr;}
    if(loop_){loop_->release();loop_=nullptr;}unmapResources();
    if(resourceData_)resourceData_->release();if(firmwareData_)firmwareData_->release();if(lock_)IOLockFree(lock_);
    IOService::free();
}
IOReturn DaliMali::newUserClient(task_t task,void *security,UInt32 type,OSDictionary *properties,IOUserClient **out){
    if(!out||type)return kIOReturnBadArgument;*out=nullptr;auto *client=new DaliMaliUserClient;if(!client)return kIOReturnNoMemory;
    bool attached=false;
    if(!client->initWithTask(task,security,type,properties)||!(attached=client->attach(this))||!client->start(this)){
        if(attached)client->detach(this);client->release();return kIOReturnNotReady;}
    *out=client;return kIOReturnSuccess;
}
bool DaliMaliUserClient::initWithTask(task_t task,void *security,UInt32 type,OSDictionary *properties){
    if(!task||type||!IOUserClient::initWithTask(task,security,type,properties))return false;
    task_=task;task_reference(task);lock_=IOLockAlloc();return lock_!=nullptr;
}
bool DaliMaliUserClient::start(IOService *provider){
    controller_=OSDynamicCast(DaliMali,provider);if(!controller_||!IOUserClient::start(provider))return false;
    context_=controller_->connect(task_);return context_!=nullptr;
}
void DaliMaliUserClient::closeContext(){if(!lock_)return;IOLockLock(lock_);
    if(context_&&controller_){controller_->disconnect(context_);context_=nullptr;}IOLockUnlock(lock_);}
IOReturn DaliMaliUserClient::clientClose(){closeContext();terminate();return kIOReturnSuccess;}
void DaliMaliUserClient::stop(IOService *provider){closeContext();controller_=nullptr;IOUserClient::stop(provider);}
void DaliMaliUserClient::free(){closeContext();if(task_)task_deallocate(task_);if(lock_)IOLockFree(lock_);IOUserClient::free();}
uint32_t DaliMaliUserClient::token(uint32_t kind,uint32_t object,uint32_t stream){
    for(const auto &entry:tokens_)if(entry.token&&entry.kind==kind&&entry.object==object&&entry.stream==stream)return entry.token;
    if(serial_==0xffffffff)return 0;
    for(auto &entry:tokens_)if(!entry.token){entry={++serial_,kind,object,stream};return entry.token;}return 0;
}
void DaliMaliUserClient::invalidate(uint32_t kind,uint32_t object,uint32_t stream,bool all){
    for(auto &entry:tokens_)if(entry.token&&entry.kind==kind&&entry.object==object&&(all||entry.stream==stream))entry={};
}
IOReturn DaliMaliUserClient::action(OSObject *target,void *,IOExternalMethodArguments *args){
    auto *self=OSDynamicCast(DaliMaliUserClient,target);
    if(!self||!args->structureInput||!args->structureOutput||args->structureInputDescriptor||args->structureOutputDescriptor)return kIOReturnBadArgument;
    nml_request request{};nml_response response{};memcpy(&request,args->structureInput,sizeof(request));
    IOLockLock(self->lock_);bool space=false;for(const auto &entry:self->tokens_)if(!entry.token){space=true;break;}
    const bool needsToken=request.operation==NML_BUFFER_ALLOC||request.operation==NML_BUFFER_IMPORT||request.operation==NML_QUEUE_BIND;
    uint32_t status=needsToken&&(!space||self->serial_==0xffffffff)?uint32_t(NML_NO_MEMORY):
        self->context_&&self->controller_?self->controller_->call(self->context_,request,&response):uint32_t(NML_STATE);
    if(status==NML_OK){
        if(request.operation==NML_BUFFER_ALLOC||request.operation==NML_BUFFER_IMPORT)response.auxiliary=self->token(1,response.object,0);
        if(request.operation==NML_QUEUE_BIND)response.auxiliary=self->token(2,request.object,uint32_t(request.value));
        if(request.operation==NML_BUFFER_FREE)self->invalidate(1,request.object,0);
        if(request.operation==NML_QUEUE_UNBIND)self->invalidate(2,request.object,uint32_t(request.value));
        if(request.operation==NML_GROUP_DESTROY)self->invalidate(2,request.object,0,true);
        if((request.operation==NML_BUFFER_ALLOC||request.operation==NML_BUFFER_IMPORT||request.operation==NML_QUEUE_BIND)&&!response.auxiliary)status=NML_NO_MEMORY;
    }
    response.version=NML_ABI_VERSION;response.size=sizeof(response);response.status=status;
    memcpy(args->structureOutput,&response,sizeof(response));args->structureOutputSize=sizeof(response);IOLockUnlock(self->lock_);return kIOReturnSuccess;
}
IOReturn DaliMaliUserClient::externalMethod(uint32_t selector,IOExternalMethodArguments *args,IOExternalMethodDispatch *,OSObject *,void *){
    static const IOExternalMethodDispatch method={action,0,sizeof(nml_request),0,sizeof(nml_response)};
    if(selector!=NML_SELECTOR_CALL)return kIOReturnUnsupported;
    return IOUserClient::externalMethod(selector,args,const_cast<IOExternalMethodDispatch *>(&method),this,nullptr);
}
IOReturn DaliMaliUserClient::clientMemoryForType(UInt32 type,IOOptionBits *options,IOMemoryDescriptor **out){
    if(!options||!out||!type||!lock_)return kIOReturnBadArgument;*out=nullptr;IOLockLock(lock_);IOReturn result=kIOReturnNotFound;
    if(controller_&&context_)for(const auto &entry:tokens_)if(entry.token==type){
        result=controller_->memory(context_,entry.kind,entry.object,entry.stream,out);
        if(result==kIOReturnSuccess){*options|=kIOMapWriteCombine;if(entry.kind==2)*options|=kIOMapReadOnly;}break;}
    IOLockUnlock(lock_);return result;
}
OSDefineMetaClassAndStructors(DaliMali,IOService)
OSDefineMetaClassAndStructors(DaliMaliUserClient,IOUserClient)
