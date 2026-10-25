# Dali USB platform components

This directory owns MT6991 board-specific USB resources and PMIC transport.
The reusable controller/protocol implementation lives in the pinned generic
repository's `drivers/UsbCore`. No UEFI/XNU USB profile is enabled yet.

`DaliUsbPmif.c` implements MT6991 PMIF-M SWINF2 transactions for MT6379 SID14.
It preserves inherited PMIF clocks/routes and never resets the shared controller.
The platform must validate the saved/live FDT contract and provide a shared
IRQ-safe try-lock covering every SWINF2 client, including the existing keypad.
The transport's local atomic flag also serializes concurrent initialization and
read/modify/write operations on the same object. Platform lock wiring remains
required; the existing isolated firmware keypad code is unchanged at this stage.

Initialization reads INIT_DONE and channel state only. Explicit identification
reads MT6379 DEV_INFO and checks vendor nibble `0x70` before any PMIC write is
allowed. The interface uses 16-bit addresses up to `0xdff`, one or two bytes per
command, and the fixed M route (SID14 bit clear in the FDT route mask). Overlap
with the last write waits at least 8 microseconds. Writes poll actual completion
and bit18 error status; failures are never automatically replayed.

Busy foreign channels are left untouched. A timeout/error latches a fault and
retains whether this instance issued a pending command. Explicit recovery may
discard its own late read response or wait for its own write to complete, but
does not consume a foreign valid response or clear the persistent
`write_uncertain` diagnostic. Callers must account for an uncertain PMIC write
when deciding how to stop the USB role. Failed reads preserve the caller buffer.

Run the production transport model without a device:

```sh
python3 -m unittest discover -s tests -p 'test_usb_pmif.py' -v
CC=gcc python3 -m unittest discover -s tests -p 'test_usb_pmif.py' -v
```

The test uses actual production C and a PMIF/PMIC state model with ASan/UBSan,
including concurrent initialization, NACK, delayed response, timeout, MMIO error,
unknown identity, foreign channel ownership and write uncertainty. It does not
qualify the physical PMIF, MT6379 or USB port. `hardware_qualified=false`.
