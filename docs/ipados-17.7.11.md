# iPadOS 17.7.11 on Dali

Target: iPadOS 17.7.11 build 21H461 on REDMI K80 Ultra (`dali`, MT6991). The donor IPSW selector is iPad7,1/J120AP; Dali remains the physical platform and ADT identity.

## Root-policy profiles

- `dali_ipados_defconfig` is the source-only baseline and does not enable owner-root authorization.
- `dali_ipados_root_defconfig` compiles the Dali read-only root provider and XNU owner-root audit/trust path. It uses weak, empty VerifiedRoot inputs, so the provider rejects all roots. The `ipados-root-compile` Action selects this profile and emits no device artifact.
- `dali_ipados_root_boot_defconfig` is the separate signed-policy profile. It consumes `out/dali/userdata/userdata-root.img` plus its verified JSON report and an RSA-3072 owner key pair under the build tree's `build/dali-verified-root/keys`. Do not use it for a device artifact until the production policy archive, Data seed, target prelink, APFS/userland, and display gates are qualified.
- The `ipados-root-policy-compile` Action uses that signed-policy profile to build hash-pinned OS/SystemOS windows, create a per-run owner key, and link the resulting production-format policy archive. It also runs the pinned 21H461 prelink audit, skips QEMU, and emits no device artifact; the XNU cache restore is disabled because the owner key changes each run.

`build-dali-verified-root.py` revalidates the complete SHNGROOT container and its OS/SystemOS window hashes before signing. It binds the policies to the pinned volume UUIDs, requires both policies to share one owner anchor, and emits `libVerifiedRootDali.a` with the public key, manifests, VerifiedRoot verifier, and XNU crypto adapter. The private key is not copied into the archive. With no policy inputs, the script skips generation and leaves the weak deny-all definitions in `libDaliUfs.a`.

`build-dali-ipados-data-seed.py` consumes the same pinned OS DMG and its read-only mount. It copies the exact `private/var` tree except `staged_system_apps`, checks UID/GID/mode/content/xattrs, packs the APFS image into DarwinBoot's `DBRDISK1` format, and applies 21H461 skip-setup/no-auto-dim preferences. Its report deliberately leaves owner-keybag creation, seed signature, payload fit, production storage, and device boot unqualified. It is an offline candidate input, not a flashable artifact.

The measured 21H461 APFS volume UUIDs are OS `04769D26-2CA0-4825-B443-E2E5F88300F2` and SystemOS `8846FE78-2910-4297-AF97-D59BB6F8FC16`. They were read from the exact local DMGs with `apfsutil` and are mirrored in `board.toml` and the Dali root-provider checks.

The prelink profile pins the decompressed arm64 kernelcache SHA-256 `f18b4df865405f2a287b84977d7aca745e056d656d9fc9df1662970ce2969de7`; the separate compressed IPSW member hash remains in `[ipsw.sha256]`. Its roots, source-interface bindings, and AppleMobileFileIntegrity initializer are audit inputs only. The current mappings cover two IOMedia methods and the APFS-referenced `IOPartitionScheme` D2 destructor; each is tied to the exact kernelcache pin and source export, but this does not establish ABI or runtime compatibility. `NativePrelink` currently reports instruction relocation, ABI, runtime, and loadability as false; the workflow's one-day artifact is explicitly named `not-for-device` and must not be flashed.

## Current verification

The [macOS compile-only Action](https://github.com/localhosts-A/UniXNU-Dali/actions/runs/40467906256) passed source XNU compilation/linking, Dali UFS archive creation, UniXNU packaging, DarwinBoot build, and handoff fixtures. It skipped QEMU and produced no flashable artifact. No device or partition has been written during this phase, and no new backup was created.

The raw userdata image and target Data seed have not been generated. Production VerifiedRoot policies, iPadOS prelink/ABI, Apple `launchd`/SpringBoard, and Dali post-EBS display remain unqualified. Keep all device root boot arguments off until those gates pass.
