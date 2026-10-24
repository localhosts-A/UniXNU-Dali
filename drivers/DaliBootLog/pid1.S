// SPDX-License-Identifier: BSD-2-Clause-Patent
// Owned static Darwin PID1: real BSD syscalls, no libc, device I/O or Apple blobs.
// Syscall numbers match pinned XNU10063 bsd/kern/syscalls.master. In particular
// Darwin syscall 240 is listxattr, not Linux nanosleep. Use select(0,...,1s).
.text
.global _start, _checkpoint, _pid1_failed
_start:
  mov x9, sp
  and x9, x9, #-16
  mov sp, x9
  adrp x19, _results@PAGE
  add x19, x19, _results@PAGEOFF
  mov x16, #20
  svc #0x80
  str x0, [x19]
  mrs x9, nzcv
  str x9, [x19, #16]
  b.cs _pid1_failed
  cmp x0, #1
  b.ne _pid1_failed

  adrp x0, _hw_mib@PAGE
  add x0, x0, _hw_mib@PAGEOFF
  mov x1, #2
  adrp x2, _user_page_size@PAGE
  add x2, x2, _user_page_size@PAGEOFF
  adrp x3, _old_length@PAGE
  add x3, x3, _old_length@PAGEOFF
  mov x4, #0
  mov x5, #0
  mov x16, #202
  svc #0x80
  str x0, [x19, #8]
  mrs x9, nzcv
  str x9, [x19, #24]
  b.cs _pid1_failed
  cbnz x0, _pid1_failed
  adrp x9, _user_page_size@PAGE
  ldr x9, [x9, _user_page_size@PAGEOFF]
  cmp x9, #4096
  b.ne _pid1_failed

  adrp x0, _launch_path@PAGE
  add x0, x0, _launch_path@PAGEOFF
  mov x1, #1 // O_WRONLY: the root must refuse this with EROFS.
  mov x2, #0
  mov x16, #5
  svc #0x80
  adrp x9, _readonly_result@PAGE
  str x0, [x9, _readonly_result@PAGEOFF]
  mrs x10, nzcv
  add x9, x9, _readonly_result@PAGEOFF
  str x10, [x9, #8]
  b.cc _pid1_failed
  cmp x0, #30
  b.ne _pid1_failed

  adrp x0, _lookup_mib@PAGE
  add x0, x0, _lookup_mib@PAGEOFF
  mov x1, #2
  adrp x2, _pid1_mib@PAGE
  add x2, x2, _pid1_mib@PAGEOFF
  adrp x3, _pid1_mib_length@PAGE
  add x3, x3, _pid1_mib_length@PAGEOFF
  adrp x4, _pid1_name@PAGE
  add x4, x4, _pid1_name@PAGEOFF
  mov x5, #14
  mov x16, #202
  svc #0x80
  b.cs _pid1_failed
  cbnz x0, _pid1_failed
  adrp x21, _pid1_message@PAGE
  add x21, x21, _pid1_message@PAGEOFF
  mov w20, #0
L_heartbeat:
  str w20, [x21, #8]
  adrp x0, _pid1_mib@PAGE
  add x0, x0, _pid1_mib@PAGEOFF
  adrp x9, _pid1_mib_length@PAGE
  ldr x1, [x9, _pid1_mib_length@PAGEOFF]
  lsr x1, x1, #2 // name2oid reports bytes; __sysctl expects an int count.
  mov x2, #0
  mov x3, #0
  mov x4, x21
  mov x5, #32
  mov x16, #202
  svc #0x80
  b.cs _pid1_failed
  cbnz x0, _pid1_failed
  cmp w20, #30
  b.eq _checkpoint
L_sleep:
  adrp x4, _sleep_time@PAGE
  add x4, x4, _sleep_time@PAGEOFF
  mov x9, #1
  stp x9, xzr, [x4]
  mov x0, #0
  mov x1, #0
  mov x2, #0
  mov x3, #0
  mov x16, #93
  svc #0x80
  b.cc L_slept
  cmp x0, #4 // EINTR: request a fresh full second before reporting another beat.
  b.eq L_sleep
  b _pid1_failed
L_slept:
  cbnz x0, _pid1_failed
  add w20, w20, #1
  b L_heartbeat
_checkpoint:
  b _checkpoint
_pid1_failed:
  adrp x9, _pid1_failure@PAGE
  mov x10, #1
  str x10, [x9, _pid1_failure@PAGEOFF]
  b _pid1_failed

.data
.balign 8
.global _results, _user_page_size, _old_length, _pid1_failure, _readonly_result
_results: .quad 0, 0, 0, 0
_user_page_size: .quad 0
_old_length: .quad 8
_pid1_failure: .quad 0
_readonly_result: .quad 0, 0
_hw_mib: .word 6, 7
_lookup_mib: .word 0, 3
_pid1_mib: .space 48
_pid1_mib_length: .quad 48
_sleep_time: .quad 1, 0
_pid1_message: .word 0x444c5031, 1, 0, 1, 4096, 0, 30, 0
_launch_path: .asciz "/sbin/launchd"
_pid1_name: .asciz "kern.dali_pid1"
