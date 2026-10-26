# UFS scratch candidate/source binding

Scratch authorization must compare independent observations to the grant. The
grant's candidate SHA names the entire original source-only Mach-O kernel file.
The final boot image includes that grant; its hash belongs to the durable host
lease. These are different byte objects.

The kernel now contains one 180-byte `gDaliUfsSourceIdentity` record: a 16-byte
`DALIUFSSRC1` marker followed by device, UniXNU, UEFI and XNU commit strings,
each 40 lowercase hex bytes plus NUL. The platform builder obtains these from
the real repositories behind the build tree's driver symlinks, checks clean
device/UniXNU sources and board/feed pins, and passes them to the compiler.
It checks the actual object record. Artifact staging requires exactly one
matching record in the actual kernel. No kernel hash is embedded into this
record, avoiding a self-hashing build dependency.

XNU source is compared in alternate Git indexes against its pinned HEAD plus
the declared device-overlaid generic patch stack and NativePrelink patches.
Omitted DriverKit/TrustCache headers must match the fixed-tag API/blob records
already used by the builder. Unexpected tracked, untracked and ignored source
files are refused. The verifier changes neither the real index nor worktree;
it may create unreachable Git objects. Reports retain the effective tree and
patch/header-index hashes. This validates consistency with those header
records; it does not independently authenticate their network provenance.

Kernel cache keys now include the exact device/UniXNU revisions and firmware
pins because these are compiled inputs. Cache stamps also recheck the effective
XNU source. Even a documentation-only commit changes the compiled identity and
therefore requires a new kernel.

The scratch consumer expects a 32-byte `/chosen/darwinboot-kernel-sha256`
measurement produced by the trusted loader from the original file before any
segment relocation. It combines that measurement with the compiled source pins
to reconstruct exactly the host's `DALI-UFS-SOURCE-V1\0` plus canonical JSON
preimage, hashes it with the real kernel SHA implementation and compares both
candidate and source digests to the grant. Missing, malformed or mismatching
inputs refuse scratch initialization before controller MMIO. Every write gate
additionally requires explicit candidate/source verification flags. Failed
verification clears both flags and both observation digests.

The isolated DarwinBoot producer now validates a unique 32-byte zero slot in
`/chosen` and fills it from the whole original kernel file before allocation or
relocation. Its directly linked SHA avoids platform Null-library selection.
The Dali generator emits only a zero slot for the explicit scratch profile;
the host never fills a measurement. Native production-C/parser tests and actual
ARM64/QEMU exercise malformed slots and a kernel trailer outside all loaded
segments, including the post-EBS value and an EBS retry.
Board pins UniXNU `504d2cccbcdb7c962cb4918e2d8fbed5eb1daf7d`, whose DarwinBoot
gitlink is `6203a83fda0272cb10e740e93ec7c219808a8915`. The previous CI40267179773
kernel is evidence for its older device8e8ba36/UniXNUa3dfbe4 inputs only.

An arbitrary ADT property is not evidence of a loader measurement. UEFI verifier
`b649eb64d13c238dabc53ecd2586c0af7ca60855` now requires a unique ARM64 DarwinBoot
inside the final FD matching the local compiled EFI, its measurement property
contract, the clean generic/submodule revisions and exact gitlink, and the
five actual link objects including KernelMeasurement. The record hashes source,
object and library-list inputs. The combined runner recomputes it before flash;
manifest-only claims are refused. These are local build consistency checks,
not an independent compiler attestation or a write grant. Actual new-pin Dali
firmware/candidate qualification still requires fresh build evidence. The
frozen read-only candidate is unchanged; UEFI's grant remains zero and no
physical scratch write is authorized.

Board now pins UEFI `0f82a6a85d28660cbaa5db662507ea2a78891c44`, retaining the
loader binding and requiring explicit normal shutdown after the combined
read-only diagnostics. The production worker's ordering is tested on the host;
actual source-only compilation and device stop qualification remain separate.

Remaining gates include actual new-pin final-FD/candidate and source-only build
evidence, runtime UFS geometry/GPT/layout authorization, evidence export before
restoring original boot_b, full firmware/XNU ownership, and separate UEFI/XNU
write/sync/same-candidate reboot/readback qualification with three fresh tokens.
