# Dali USB platform components

This directory owns MT6991 board-specific USB resources and PMIC transport.
The reusable controller/protocol implementation lives in the pinned generic
repository's `drivers/UsbCore`. No UEFI/XNU USB profile is enabled yet.

`DaliUsbPmif.c` implements MT6991 PMIF-M SWINF2 transactions for MT6379 SID14.
It preserves inherited PMIF clocks/routes and never resets the shared controller.
The platform must validate the saved/live FDT contract and provide a shared
IRQ-safe try-lock covering every SWINF2 client, including the existing keypad.
The transport's local atomic flag also serializes concurrent initialization and
read/modify/write operations on the same object. Release reports whether an
owned command is still pending; the platform must reserve the channel for this
same persistent client until explicit recovery releases it. An ordinary mutex
without this reservation can lose response ownership across timeout boundaries.
The isolated firmware now has a singleton bus protocol and migrated SID4 keypad;
the USB platform adapter still needs to bind these callbacks to that protocol.

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

`DaliUsbTypec.c` adds the actual MT6379 TCPCI initialization, fixed/DRP
termination, orientation, bounded CC sampling/debounce and charger VBUS control.
Objects start zeroed and belong to a serialized platform worker. Probe checks
real VID/PID/DID without writes. Start requires USB DMA stopped and source boost
off, masks only local TCPC alerts and disables PD RX/VCONN. The poller requires
120ms of observations at <=5ms intervals before attachment, and a stopped
boundary before changing an active attachment. A rejected snapshot, bus conflict
or missed polling interval restarts the candidate timer. The reference's lost
DRP toggle workaround is allowed only while unpublished, quiesced and boost-off.

Before enabling source power, freeze a fresh published HOST attachment with
`dali_usb_typec_latch_attachment`. VBUS requires separate exclusive charger/TM
ownership and a real board connector-check callback. It rejects unknown checks,
incoming VBUS or unsafe voltage, programs exactly5.00V/500mA with the reference's
BE16 selector/passcode/PP7 sequence, and confirms actual boost and output voltage.
Repeated enable validates live output without replaying writes. Faults retain an
UNKNOWN power state until explicit shutdown; failure to quiesce prevents any
power change. Shutdown confirms our boost off without discharging another
source's incoming VBUS. A faulted instance must be destroyed/reinitialized.

```sh
python3 -m unittest discover -s tests -p 'test_usb_typec.py' -v
CC=gcc python3 -m unittest discover -s tests -p 'test_usb_typec.py' -v
```

This second fixture runs production Type-C/VBUS C against a PMIC byte-register
seam, separately from the production PMIF fixture. It covers polling continuity,
CC instability, DRP recovery guards, exact TM writes, output loss, failed
quiesce, per-operation enable failures and retained uncertainty. It does not
establish combined PMIF-to-port operation, board ADC/DPDM protection, automatic
USB role orchestration, PHY/clocks/repeaters or an OS binding; those remain to
be implemented and validated. No physical device is accessed by either fixture.
