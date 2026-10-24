// SPDX-License-Identifier: BSD-2-Clause-Patent
// Included after the ordinary Dali ring writer and validated AEE callback.
#pragma once
#include <sys/errno.h>
#include <sys/proc.h>
#include <sys/sysctl.h>
#include <sys/mount.h>
#include <sys/vnode.h>
#include <kern/clock.h>
#include "DaliPid1.h"

extern "C" {
// Read by the offline debugger at the real EL0 completion instruction.
struct DaliPid1Report {
  uint32_t active, offline, pid, sequence, readonlyRoot, failures, complete, reserved;
  uint64_t firstNs, elapsedNs;
};
extern volatile DaliPid1Report dali_pid1_report;
volatile DaliPid1Report dali_pid1_report = {};
}
static DaliPid1State gPid1State;
static IOLock *gPid1Lock;
static IOMemoryMap *gPid1Console;
static char gPid1Token[33];

static bool pid1Record(const char *stage, uint32_t sequence, uint64_t elapsedNs)
{
  char line[224] = "DALI-PID1: run=";
  char *at = line + sizeof ("DALI-PID1: run=") - 1;
  char *end = line + sizeof (line) - 2;
  at = appendText (at, end, gPid1Token);
  at = appendText (at, end, " stage=");
  at = appendText (at, end, stage);
  at = appendText (at, end, " pid=1 seq=");
  at = appendUnsigned (at, end, sequence, 10);
  at = appendText (at, end, " elapsed-ns=");
  at = appendUnsigned (at, end, elapsedNs, 10);
  at = appendText (at, end, " root=mockfs readonly=1 pagesize=4096 backend=");
  at = appendText (at, end, dali_pid1_report.offline ? "qemu" : "ramoops");
  *at++ = '\n';
  if (dali_pid1_report.offline) {
    *at = '\0';
    IOLog ("%s", line);
    return true;
  }
  return appendRamoops (gPid1Console, line, static_cast<size_t> (at - line));
}

static bool pid1ReadonlyRoot()
{
  vnode_t root = nullptr;
  if (vnode_lookup ("/", 0, &root, vfs_context_current ()) != 0) { return false; }
  mount_t mount = vnode_mount (root);
  char name[16] = {};
  bool valid = false;
  if (mount != nullptr) {
    vfs_name (mount, name);
    valid = vfs_isrdonly (mount) != 0 && (vfs_flags (mount) & MNT_ROOTFS) != 0 &&
            memcmp (name, "mockfs\0", 7) == 0;
  }
  vnode_put (root);
  return valid;
}

static void pid1EndReset()
{
  // The 30-second test runs without the firmware's short recovery watchdog.
  // Request only the already reviewed AEE reset, after durable PASS. Do not
  // program RGU/boot-mode/storage fields or claim automatic fastboot.
  IORegistryEntry *entry = IORegistryEntry::fromPath ("/arm-io/dali-boot-log", gIODTPlane);
  if (entry == nullptr) { return; }
  auto *aee = OSDynamicCast (OSData, entry->getProperty ("recovery-aee-reset-type"));
  uint32_t resetType = 0;
  if (aee != nullptr && aee->getLength () == sizeof (resetType)) {
    memcpy (&resetType, aee->getBytesNoCopy (), sizeof (resetType));
  }
  entry->release ();
  if (resetType != 0x80000201U) { return; }
  if (!pid1Record ("end-reset-issue", 30, dali_pid1_report.elapsedNs)) { return; }
  RecoveryContext context = {0, gPid1Console, gPid1Token, UINT32_MAX, UINT32_MAX, resetType};
  recoveryFence (&context);
  recoverySystemReset (&context);
  pid1Record ("end-reset-returned", 30, dali_pid1_report.elapsedNs);
}

static int pid1Sysctl SYSCTL_HANDLER_ARGS
{
  if (!dali_pid1_report.active || gPid1Lock == nullptr) { return ENOENT; }
  if (req->p == nullptr || proc_pid (req->p) != 1 || proc_selfpid () != 1) { return EPERM; }
  if (req->newptr == 0 || req->newlen != sizeof (DaliPid1Message)) { return EINVAL; }
  DaliPid1Message message = {};
  const int error = SYSCTL_IN (req, &message, sizeof (message));
  if (error != 0) { return error; }
  const bool readonlyRoot = pid1ReadonlyRoot ();
  uint64_t uptime, nowNs;
  clock_get_uptime (&uptime);
  absolutetime_to_nanoseconds (uptime, &nowNs);
  IOLockLock (gPid1Lock);
  DaliPid1State next = gPid1State;
  const auto result = daliPid1Advance (next, message, proc_selfpid (), readonlyRoot, nowNs);
  if (result == DaliPid1Result::Reject) {
    dali_pid1_report.failures++;
    // This is diagnostic input, never a PASS or a root-verified heartbeat.
    char refused[192] = "DALI-PID1: run=";
    char *at = refused + sizeof ("DALI-PID1: run=") - 1;
    char *end = refused + sizeof (refused) - 2;
    at = appendText (at, end, gPid1Token);
    at = appendText (at, end, " stage=refused seq=");
    at = appendUnsigned (at, end, message.sequence, 10);
    at = appendText (at, end, " readonly-root=");
    at = appendUnsigned (at, end, readonlyRoot, 10);
    *at++ = '\n';
    *at = '\0';
    if (dali_pid1_report.offline) { IOLog ("%s", refused); }
    else { appendRamoops (gPid1Console, refused, static_cast<size_t> (at - refused)); }
    IOLockUnlock (gPid1Lock);
    return EINVAL;
  }
  const uint64_t elapsed = nowNs - next.firstNs;
  if (!pid1Record (result == DaliPid1Result::Complete ? "PASS" : "heartbeat",
                   message.sequence, elapsed)) {
    dali_pid1_report.failures++;
    IOLockUnlock (gPid1Lock);
    return EIO;
  }
  gPid1State = next;
  dali_pid1_report.pid = 1;
  dali_pid1_report.sequence = message.sequence;
  dali_pid1_report.readonlyRoot = readonlyRoot;
  dali_pid1_report.firstNs = next.firstNs;
  dali_pid1_report.elapsedNs = elapsed;
  dali_pid1_report.complete = next.complete;
  IOLockUnlock (gPid1Lock);
  if (result == DaliPid1Result::Complete && !dali_pid1_report.offline) { pid1EndReset (); }
  return 0;
}
SYSCTL_PROC (_kern, OID_AUTO, dali_pid1, CTLTYPE_OPAQUE | CTLFLAG_WR | CTLFLAG_LOCKED,
             nullptr, 0, pid1Sysctl, "S,DaliPid1Message", "Owned Dali PID1 bring-up instrument");

static bool pid1Initialize(IOMemoryMap *console, const char *token, bool offline)
{
  if (gPid1Lock != nullptr || (!offline && console == nullptr)) { return false; }
  gPid1Lock = IOLockAlloc ();
  if (gPid1Lock == nullptr) { return false; }
  memcpy (gPid1Token, token, sizeof (gPid1Token));
  // The registered service owns this mapping for the remainder of this boot.
  // Keep an explicit reference for the sysctl endpoint's lifetime as well.
  if (console != nullptr) { console->retain (); }
  gPid1Console = console;
  dali_pid1_report.offline = offline;
  dali_pid1_report.active = 1;
  return true;
}
