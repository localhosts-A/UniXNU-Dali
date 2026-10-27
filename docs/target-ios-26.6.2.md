# iOS 26.6.2 target input

The target IPSW is Apple's iPhone18,2 restore image, iOS 26.6.2 (23G90),
`v54ap`. Its BuildManifest identifies chip `0x8150`, board `0x0e` and
`kernelcache.release.v54` for the customer install identities.

- IPSW source: `https://updates.cdn-apple.com/2026SummerFCS/1a337b3d-ffd8-424a-abc4-092b64d551ab/iPhone18%2C2_26.6.2_23G90_Restore.ipsw`
- Full IPSW length: 11,439,109,684 bytes. Apple CDN reports SHA-256
  `45c6942e20d8a4a40fe83084a23a9534fa038e266e74f38f4176d9f95f0f21aa`;
  the complete archive has not been downloaded or hashed locally.
- BuildManifest SHA-256: `913dc7d39244478bf1f4812beaf30f5fd36ea73f74fba7e896e5e3e12ec7b869`.
- Kernelcache SHA-256: `ca27ef86c6153f2dc1639fbdb7713ae3fef66b90f8466ac73fa4f6c6a4ce1da7`.
  Its locally calculated SHA-384 matches the BuildManifest's KernelCache digest:
  `0b5988ef0e2042b45b07114b2d3ea9309e4b0aa8045ba917c5d4914f242dddc2bc86232bd4b16e2420e5684292042886`.
- `ipsw kernel dec` and `ipsw kernel version --json` report Darwin `25.6.0`,
  XNU `12377.162.14~4`, RELEASE ARM64, T8150, dated 2026-07-30.

The exact corresponding Apple OSS XNU tag was not found in the `xnu-12377*`
remote tags as of 2026-10-27. `xnu-12377.121.6` is an earlier reference,
not an established source match. Page size, prelink inputs, PAC compatibility,
boot-args and driver ABI still require examination before enabling an OS profile.
The target kernelcache and manifest remain in private local storage, not Git.
