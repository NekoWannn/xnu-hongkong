# PEBBLE — SM8950 (Xiaomi 18 Pro) XNU port, step 1

This is the build-enablement step: teach xnu-8796 about a new arm64 machine
config called `PEBBLE`, modelled on the `SWEET` config from
[dipa-studio/ios-sweet](https://github.com/dipa-studio/ios-sweet).

Nothing here runs on the phone yet. The goal of this step is to get a
`kernel.<config>.pebble` out of CI, then bring it up with a boot shim.

## Files

| File | Purpose |
| --- | --- |
| `0001-pebble-machine-config.patch` | Adds `PEBBLE` to `makedefs/MakeInc.def`, hooks `PEBBLE.h` into `board_config.h`, adds the header itself |
| `0002-pebble-gic-timer.patch` | GICv3 Group-1 dispatch in `osfmk/arm64/sleh.c` (timer PPI 27 / IPIs) and the `start.s` SCTLR readback fix for Gunyah |
| `0003-pebble-gic-init.patch` | `pe_init_fiq()` for PEBBLE in `pexpert/arm/pe_fiq.c`: Group-1 redistributor wake-up, IGROUPR0/ISENABLER0, ICC_SRE/PMR/BPR1, IGRPEN1 |
| `xnu.yml` | GitHub Actions workflow (macos-14 + pinned darwin-xnu-build) |
| `PEBBLE.h` | Board config header (also embedded in the patch) |
| `apply_pebble.py` | Alternative to the patch: applies the same edits by anchor string |
| `gen_patches.py` | Regenerates the patch from a source tree |

## Why the patch looks like this

- **`PEBBLE` in `SUPPORTED_ARM64_MACHINE_CONFIGS`** plus
  `MACHINE_FLAGS_ARM64_PEBBLE = -DARM64_BOARD_CONFIG_PEBBLE -march=armv8.2-a`.
- **plain arm64, not arm64e**: the Oryon cores implement PAC, but XNU's arm64e
  ABI assumes Apple's PAC keys and ABI details. `NO_MONITOR` / `NO_ECORE` come
  from the same reasoning as `SWEET`.
- The header records what we measured from the live device tree:
  GICv3 at `0x17000000`, timer frames at `0x17810000`, 8 CPUs, 64-byte cache
  lines, standard arch-timer PPIs 27/26.

## How to run it

1. Create a GitHub repo (public gets free macOS runner minutes).
2. Put `xnu.yml` at `.github/workflows/xnu.yml`.
3. Put the patch at `xnu/patches/0001-pebble-machine-config.patch`.
4. Actions → *xnu* → Run workflow (machine_config = `PEBBLE`,
   kernel_config = `DEVELOPMENT`, keep_going = true).
5. Download the `xnu-kernel` artifact: `kernel.development.pebble`.

Keep `keep_going=true` for the first runs: a single run then reports *every*
compile error instead of stopping at the first one.

## What will break first (expected)

- `arm64e` assumptions elsewhere: some files key off `__arm64e__`; a plain
  arm64 build may need a few more `#if defined(PEBBLE)` guards.
- `pexpert/arm64/pe_fiq.c` has no PEBBLE branch yet — the next patch adds the
  GICv3 Group 1 setup (`ICC_SRE_EL1`, `GICR_WAKER`, group/priority registers).
- `osfmk/arm64/sleh.c` needs the Group 1 IRQ dispatch (timer PPI 27, IPIs < 16).
- `pexpert/arm64/start.s` has an Apple-only SCTLR round-trip check to skip.

Those are the patch-0002/0003 items; they are behaviour, not build enablement,
so they come after the first successful link.

## Status of the on-device work

| Item | State |
| --- | --- |
| Boot path (`fastboot boot` of a custom v4 image) | **works, verified on the phone** |
| v4 boot image layout (`header_size=1584`, arm64 `Image` header, `b +64`) | **solved** |
| Framebuffer geometry (`0xf8c60000`, pitch 4480, 32bpp, 1120x2436) | **calibrated on hardware** |
| Platform facts (GICv3, UFS 4.0, Gunyah, 22 RAM fragments) | **harvested from the live DT** |
| GIC addresses (`GICD 0x17000000/64K`, `GICR 0x17080000/2M`) | **confirmed via `/proc/iomem`** |
| Register-level probe output (EL / PSCI / MMFR0) | **blocked**: the panel stopped scanning out our writes after the first few boots |
| XNU build | this step |

## Next, in order

1. Get a kernel binary out of CI (this step).
2. Write the boot shim: load the kernel + generated Apple device tree + boot
   args, then hand off — the same shape as `xnu-loader`, but as an Android boot
   image so `fastboot boot` can load it.
3. Bring the panel back for diagnostics (DSI + backlight re-init inside the
   shim), or move diagnostics to a channel that does not need the screen.
4. Storage: UFS 4.0 host driver (largest unknown; needs vendor kernel source).
