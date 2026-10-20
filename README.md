# XNU on the Xiaomi 18 Pro (`hongkong`, Qualcomm SM8950)

Bringing Apple's XNU kernel up on a Snapdragon 8-class phone, built on top of
[dipa-studio/ios-sweet](https://github.com/dipa-studio/ios-sweet) — the project
that already runs XNU on the Redmi Note 10 Pro (`sweet`, SM7150).

**Status:** XNU builds in CI, is loaded by the ios-sweet boot shim, and reaches
`arm_init()` on real hardware. It hangs inside early kernel bootstrap; the
current work is bisecting that point.

## How it works

```
fastboot boot  boot.img  (Android boot image, header v4)
        |
        +-- shim            ios-sweet's shim, retargeted at this panel:
        |                   UFS probe, MMU/identity map, console, payload loader
        |
        +-- SWEETXNU payload  our kernel in ios-sweet's payload format,
                              appended at offset 0x2f000 of the shim image
                              (this device's ABL takes the generic ramdisk
                               from init_boot, so the boot ramdisk is unused)
```

The shim relocates the kernel to the physical base from the ADT, fills
`boot_args`, maps the framebuffer, and jumps to `__start` at EL1 (Gunyah owns
EL2 on this SoC).

## Building

`.github/workflows/xnu.yml` builds with
[blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build)
pinned to xnu-8796.141.3, then runs `xnu/tools/apply_hongkong_all.py`, which:

1. extracts `xnu/tools/sweet_patches.bundle` (ios-sweet's 81-patch series) and
   applies it to the tree;
2. adds the `HONGKONG` machine config: `pexpert/pexpert/arm64/HONGKONG.h`,
   an entry in `board_config.h`, and `MACHINE_FLAGS_ARM64_HONGKONG` in
   `makedefs/MakeInc.def`.

`HONGKONG.h` deliberately also defines `SWEET`, so every `#if defined(SWEET)`
path from ios-sweet (early framebuffer console, GIC Group 1 dispatch, device
tree platform expert, boot CPU driver) stays active. Only the hardware
constants differ:

| | sweet (SM7150) | hongkong (SM8950) |
| --- | --- | --- |
| GICD | `0x17a00000` | `0x17000000` |
| GICR | `0x17a60000` | `0x17080000` |
| virtual timer INTID | 19 (Qualcomm PPI 3) | 27 (standard PPI 11) |
| framebuffer | `0x9c000000`, 1080x2400 | `0xf8c60000`, 1120x2436 |

The GIC bases and timer interrupt also come from the ADT
(`tools/mkadt_hongkong.py`), which is where the kernel actually reads them.

## Host-side tools

| File | Purpose |
| --- | --- |
| `port/mkadt_hongkong.py` | Builds the minimal Apple Device Tree this SoC needs |
| `port/shim2/` | ios-sweet's shim, patched for this panel and payload layout |
| `port/shim2/mkpayload.py` | Packs a kernel Mach-O + ADT into a SWEETXNU payload |
| `probe/pack_v4.js` | Wraps shim + payload into a v4 boot image |
| `port/cycle.ps1` | Fetch artifact, build payload, pack, `fastboot boot` |
| `port/docs/xnu-build-status.md` | Findings log: what was measured and why |

## Findings worth keeping

- **Gunyah owns EL2**, so XNU runs at EL1 and cannot use the monitor interface.
- **The boot ramdisk is not ours**: with boot header v4 the generic ramdisk
  lives in `init_boot`, and ABL assembles the initrd from `init_boot` +
  `vendor_boot`. `fastboot boot` only replaces `boot.img`, so the payload is
  appended to the shim instead. A header v2 image is rejected outright by this
  ABL (`Failed to load/authenticate boot image: Invalid Parameter`).
- **`start.s` converts to KVA, not PA**: `BA_VIRT_BASE` / `BA_PHYS_BASE` are
  read swapped relative to their names, so the effective arithmetic is
  `PA = VA + physBase - virtBase`.
- **The framebuffer must be mapped in the kernel's own page tables** before the
  MMU is enabled from `start.s`, otherwise every post-MMU write faults and the
  machine resets before any output appears.
- **ios-sweet's shim needs `--nohdr` packing**: its `start.S` already carries an
  arm64 Image header, and a second one shifts the image by 64 bytes, which
  breaks every `.data` initialiser (their `con_fb` ended up pointing at garbage).
- `configure_timer_apple_regs()`, `wfe_timeout_*()` and
  `configure_misc_apple_regs()` are called unconditionally by xnu-8796's
  `arm_init()` and are harmless on Qualcomm — ios-sweet does not patch them out.
