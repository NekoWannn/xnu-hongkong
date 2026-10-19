# XNU build: current failure point (run 39305684645)

## What the earlier fix changed

`osfmk/conf/files.arm64` gates the open-source assembly and pmap on the
`nos_arm_asm` / `nos_arm_pmap` options, which this board never defines, so
`start.s`, `cswitch.s`, `locore.s` and the pmap were silently dropped. The
applier now rewrites those lines to `standard`, which is what finally made the
kernel try to build them.

That immediately exposed three missing pieces, now handled:

| Problem | Fix |
| --- | --- |
| `arm64/ppl/sart.h`, `arm64/ppl/uat.h` not in the open drop | stubs written by the applier |
| `arm64/tunables/tunables.s` not in the open drop | empty stub written by the applier |
| `pmap.c:119: Unknown __ARM_VMSA__` | `-D__ARM_VMSA__=8` in `MACHINE_FLAGS_ARM64_PEBBLE` |

Also fixed in the applier: `write()` now creates parent directories, and the
`config/MASTER.arm64.pebble` hunk was dropped from the stub patch because our
copy already differs (the patch failed atomically and did nothing).

## Remaining blocker

`osfmk/arm/pmap/pmap.c` now compiles far enough to hit Apple's closed PPL
(Page Protection Layer) and IOMMU helpers:

```
pmap.c:4680   call to undeclared function 'ptep_get_iommu'
pmap.c:6315   call to undeclared function 'pmap_phys_write_disable'
pmap.c:10984  static assertion failed (KERNEL_BASE 0x68719460352 outside the
              expected range)
pmap.c:11626  call to undeclared function 'pmap_ppl_lockdown_pages'
pmap.c:11636  call to undeclared function 'pmap_ppl_unlockdown_pages'
pmap.c:11938  static declaration of 'pmap_phys_write_disable' follows
              non-static declaration
```

These are only referenced when the PPL path is compiled in, and the static
assertion encodes an Apple address-space assumption, so the next job is to
decide which side to align:

- **A**: make PEBBLE build without PPL (`config_pmap_ppl` off and no PPL
  references), which is what a hypervisor guest without Apple's PPL should do;
- **B**: bring in the remaining ios-sweet patches that stub out PPL, and
  correct `KERNEL_BASE` so the assertion passes.

A is closer to how a non-Apple SoC actually works; B is what ios-sweet ended
up doing and is known to compile.

## Pipeline notes

- Repository writes and CI queries are done from the PC with `node` +
  `port/gh.token` (`gh_put.js`), because the phone's HTTPS to api.github.com
  intermittently returns an empty reply.
- The workflow only auto-triggers on `xnu/tools/**` and the workflow file;
  patch uploads need an explicit dispatch.
- Never put backticks around `$O` in the workflow's shell blocks.

## Update: option A progress (run 39311889278)

Done and confirmed working:

- `-D__ARM_16K_PG__` added to `MACHINE_FLAGS_ARM64_PEBBLE`; `ARM_PGSHIFT` is
  selected in `osfmk/arm64/proc_reg.h:1157`, so the 4K branch and its
  `static_assert` are gone. The kernel now builds for the 16K granule the
  hardware reports (`MMFR0.TGran16 = 1`).
- PPL/IOMMU declarations are supplied by the applier.

Remaining, single issue:

```
osfmk/arm/pmap/pmap.c:132:16: error: unknown type name 'pte_t'
```

The stub block has to sit after `pte_t` becomes visible. It is **not** defined
in `pmap.h` or `pmap_pt_geometry.h`; the first use is inside
`pmap_page_protect()` around line 4676, so the block should be inserted
immediately before that function instead of near the include list.

Fastest next move: locate `pmap_page_protect`'s signature in `pmap.c`, anchor
`sub()` on it, and put `PPL_STUBS` in front. No other change is needed.
## Update: link reached, 32 symbols left (run 39314750184)

Assembly and the open pmap now build:

```
[arm64 pebble]  AS start.o
[arm64 pebble]  AS cswitch.o
[arm64 pebble]  LD kernel.development.pebble.unstripped
```

Zero compile errors. Undefined symbols dropped **261 -> 32**, in three groups:

1. **kpc** (~25): `kpc_arch_init`, `kpc_get_*`, `kpc_configurable_*`.
   `osfmk/kern/kpc_common.c` is compiled but `osfmk/arm64/kpc.c` is excluded,
   so exclude the rest of the subsystem as well (it programs Apple PMU).
2. **`_CleanPoC_DcacheRegion_Force_nopreempt_nohid`**, referenced by
   `pmap_batch_set_cache_attributes_internal`. Lives in `arm64/caches_asm.s`;
   check whether the file is actually in the object list and whether the
   symbol is conditionally compiled out.
3. **PLK section symbols**: `__PLK_DATA_CONST`, `__PLK_TEXT_EXEC`,
   `__PLK_LINKEDIT`, `__PLK_LLVM_COV`. These belong to Apple's Page-Locked
   Kernel; a non-PPL build must not reference them.

All three are "Apple-only, should not exist without PPL", so option A is
converging rather than changing direction.
## SUCCESS: kernel builds (run 39319201778)

```
LD kernel.development.pebble.unstripped
built kernel at dxb/build/xnu.obj/DEVELOPMENT_ARM64_PEBBLE/kernel.development.pebble
STRIP / CTFCONVERT / CTFMERGE   (11922 functions, 4672 data objects, 27768 types)
```

Artifact `xnu-kernel` (66 MB) contains:

| File | Size |
| --- | --- |
| `kernel.development.pebble` | 16,371,136 |
| `kernel.development.pebble.unstripped` | 21,181,912 |
| `kernel.development.pebble.ctf` | 66,063,604 |
| `kernel.development.pebble.dSYM` | dir |

Verified header: `MH_MAGIC_64`, `CPU_TYPE_ARM64`, `MH_EXECUTE`, 19 segments,
`__TEXT` at `0xfffffe0007004000` (matches `VM_KERNEL_LINK_ADDRESS`).

### What actually fixed it

1. `osfmk/conf/files.arm64` gates asm/pmap on `nos_arm_asm` / `nos_arm_pmap`;
   the applier rewrites those lines to `standard`, but **only for files that
   exist** — `amcc_rorgn_*.c` and friends are Apple-internal and must stay out
   or make fails with "no rule to make target".
2. `-D__ARM_VMSA__=8` and `-D__ARM_16K_PG__` in `MACHINE_FLAGS_ARM64_PEBBLE`
   (the latter selects `ARM_PGSHIFT 14` in `osfmk/arm64/proc_reg.h:1157`).
3. Stub headers `arm64/ppl/{sart.h,uat.h}` and `arm64/tunables/tunables.s`,
   plus a `pte_t` typedef, all written by the applier.
4. PPL/IOMMU declarations injected into `pmap.c` after its include list
   (`pte_t` is only visible there).
5. `osfmk/arm64/pebble_stubs.s`: assembly stubs for 20 Apple-only functions
   (kpc, `ml_enable_monitor`, cache helper) and 8 data symbols.
6. Artifact collection had to copy from
   `dxb/build/xnu.obj/DEVELOPMENT_ARM64_PEBBLE/`, not `fakeroot/...`.

### Still missing before it can boot

- boot shim: load this Mach-O, synthesise the Apple device tree and boot args,
  enter at `__start`
- storage (UFS 4.0) and display drivers
- the phone currently only ever runs `fastboot boot` images; nothing is flashed