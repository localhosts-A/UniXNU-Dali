#include "IOKitModel.h"
#include "DaliUsbHid.h"
#include <cstdio>

static usb_host_device configured()
{
  usb_host_device device={}; device.state=USB_HOST_CONFIGURED; device.hid_count=2;
  device.hid[0].active=device.hid[1].active=true;
  device.hid[0].kind=USB_HOST_HID_KEYBOARD; device.hid[1].kind=USB_HOST_HID_MOUSE;
  device.device_descriptor[8]=0x34; device.device_descriptor[9]=0x12;
  return device;
}

int main()
{
  IOService provider; auto device=configured();
  int baseline=modelLiveObjects;
  auto hid=dali_usb_hid_publish(&provider, &device, 0);
  assert(hid && hid->getRetainCount()==2); // caller plus stop-proof lease
  uint8_t pressed[8]={0,0,4,0,0,0,0,0};
  assert(dali_usb_hid_submit(hid, pressed, 7)==USB_INVALID);
  assert(dali_usb_hid_submit(hid, nullptr, 8)==USB_INVALID);
  modelReportCallback=[&](IOHIDDevice *, const std::vector<uint8_t>& report) {
    pressed[2]=5; assert(report[2]==4); // source bytes are a synchronous copy
    assert(dali_usb_hid_quiesce(hid)==USB_BUSY);
    assert(dali_usb_hid_detach(hid, true)==USB_BUSY);
  };
  assert(dali_usb_hid_submit(hid, pressed, 8)==USB_OK);
  modelReportCallback={};
  assert(dali_usb_hid_submit(hid, pressed, 8)==USB_DISCONNECTED);
  modelBufferFailure=true;
  assert(dali_usb_hid_quiesce(hid)==USB_NO_MEMORY);
  assert(hid->getRetainCount()==2 && dali_usb_hid_terminate(hid)==USB_BUSY);
  assert(dali_usb_hid_quiesce(hid)==USB_OK);
  assert(modelReports.back()==std::vector<uint8_t>(8, 0));
  assert(dali_usb_hid_detach(hid, false)==USB_BUSY);
  assert(dali_usb_hid_detach(hid, true)==USB_OK && hid->getRetainCount()==1);
  assert(dali_usb_hid_terminate(hid)==USB_OK); hid->release();
  assert(modelLiveObjects==baseline);

  // Framework stop from inside a report callback is completed automatically
  // after handleReport returns, with the provider retained through that point.
  hid=dali_usb_hid_publish(&provider, &device, 1); assert(hid);
  unsigned providerRefs=provider.getRetainCount(); int stops=modelBaseStops;
  modelReportCallback=[&](IOHIDDevice *service, const std::vector<uint8_t>&) {
    if (modelReports.back()[0]) {
      service->stop(&provider);
      assert(modelBaseStops==stops && provider.getRetainCount()==providerRefs+1);
    }
  };
  const uint8_t mouse[3]={1,2,0xff};
  const uint8_t invalidMouse[3]={0xe0,0,0};
  assert(dali_usb_hid_submit(hid, invalidMouse, 3)==USB_INVALID);
  assert(dali_usb_hid_submit(hid, mouse, 3)==USB_OK);
  modelReportCallback={};
  assert(modelBaseStops==stops+1 && provider.getRetainCount()==providerRefs);
  assert(modelReports.back()==std::vector<uint8_t>(3, 0));
  assert(dali_usb_hid_submit(hid, mouse, 3)==USB_DISCONNECTED);
  assert(dali_usb_hid_detach(hid, true)==USB_OK);
  assert(dali_usb_hid_terminate(hid)==USB_OK); hid->release();
  assert(modelBaseStops==stops+1 && modelLiveObjects==baseline);

  modelStartFailure=true;
  assert(!dali_usb_hid_publish(&provider, &device, 0));
  modelStartFailure=false; assert(modelLiveObjects==baseline);
  modelBufferFailure=true;
  assert(!dali_usb_hid_publish(&provider, &device, 0));
  assert(modelLiveObjects==baseline);
  assert(!dali_usb_hid_publish(&provider, &device, 2));
  assert(!dali_usb_hid_publish(nullptr, &device, 0));
  assert(modelLiveObjects==baseline);
  std::puts("PASS: production USB IOHID child copy/admission/neutral/deferred-stop/lifetime model; runtime unqualified");
}
