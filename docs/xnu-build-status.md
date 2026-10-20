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
## MILESTONE: control transfers into XNU on hardware

Load chain, verified on the phone tonight:

```
[screen] green  = shim running
         cyan   = all 11 Mach-O segments relocated to 0xa8000000
         white  = one block per segment
         magenta= boot_args + device tree in place, about to jump
         -> device resets: XNU __start executed and faulted
```

### Loader facts

- Boot image = shim padded to 0x10000, then payload
  (header 24 bytes: magic "XNUF", nsegs, entry_vm, link_base;
   then nsegs x {vmaddr, vmsize, fileoff, filesize}; raw segment bytes;
   then u32 dtb_len + dtb).
- Kernel loads at PHYS_BASE 0xa8000000, entry at +0x466488 (`__start`).
- `boot_args` and the DTB are placed after the kernel image, not at fixed
  offsets (a fixed 0xE00000 offset silently overlapped the kernel).
- Two pitfalls that cost cycles:
  * the payload header is 24 bytes, not 16 - a wrong offset reads garbage
    `filesize` and the copy loop never terminates;
  * with the MMU off all memory is Device type, where **unaligned 64-bit
    accesses fault** - the DTB sits behind a u32 length prefix, so it must be
    copied byte-wise.
- `fastboot boot` of a 16 MB image works fine.

### Kernel side

- `kernel.development.pebble` now contains a framebuffer console
  (`pe_serial.c`: `pebble_fbcon_serial_functions`, drawn at 0xf8c60000,
  1120x2436, stride 4480) registered from `serial_init()`.
- No text appears yet: XNU faults before `serial_init()`, i.e. in
  `arm_init` / platform bring-up, which still expects Apple's AIC and
  platform nodes this SoC does not have.

### Next

GICv3 + virtual timer (PPI 27) + a device-tree platform expert inside XNU,
so that `arm_init` can finish and `serial_init()` registers the framebuffer
console - at which point every later fault becomes readable on the panel.
## Why there is no output before kernel startup

`pexpert/arm/pe_kprintf.c` registers the console through the STARTUP mechanism:

```c
STARTUP(KPRINTF, STARTUP_RANK_FIRST, PE_init_kprintf);
```

so `serial_init()` and therefore the framebuffer console only become live late
in boot. Any `panic()` inside `arm_init()` - including inside
`PE_init_platform()` -> `SecureDTInit()` / `pe_identify_machine()` - produces
**no output at all**, just a silent reboot. That is exactly what the phone
does.

`PE_init_platform(FALSE, args)` order: copy boot args, `SecureDTInit(...)`,
`pe_identify_machine(...)`. Nothing there prints.

## Plan: an early framebuffer mapping in start.s (approved direction)

`start.s` builds bootstrap tables with 2 MB L2 block entries
(`create_l2_block_entries`, `ARM_TTE_BLOCK_L2_SHIFT`), covering only the
kernel image, so the framebuffer at `0xf8c60000` is unmapped while
`arm_init()` runs.

Make it reachable without touching the pmap:

1. grow `bootstrap_pagetables` from 4 pages to 5 (one extra L2 table);
2. point L1 entry 3 (the 1 GB region containing `0xf8c00000`) at that table;
3. add one 2 MB block entry `VA 0xf8c00000 -> PA 0xf8c00000`, device memory;
4. add `pebble_early_putc()` to the kernel, writing straight to `0xf8c60000`
   with stride 4480;
5. sprinkle `pebble_early_putc()` calls through `arm_init()` to bisect at
   statement granularity.

That turns the panel into a real early console, which is what blocks all
further progress: right now the only signal is "stopped vs rebooted".

## Bisect hook already in place

The applier understands `PEBBLE_STALL=<site>` and can insert a `wfe` loop at a
given site (currently `arm_init`). The workflow passes it through, so a single
dispatch answers "did XNU reach this point?" by whether the phone hangs or
reboots.
## Where the kernel stops (measured, run 39364647773 + current shim)

Screen markers are drawn at fixed rows and are unambiguous once the shim is
silent.  Latest observation, five bands, all visible:

```
red    (row 1200)  after set_mmu_ttb      -> executed
green  (row 1300)  after SCTLR_EL1 (MMU on)-> executed
white  (row ~1100) entry                  -> executed
yellow (row 1400)  after MIDR_EL1         -> executed
cyan   (row 1500)  just before the return -> executed
```

So the whole bootstrap page-table path works, **including enabling the MMU**,
but execution never reaches `arm_init()`'s C code (its first action clears the
panel and paints blue, which never happens).

Two candidate causes, both about the last step:

1. the return/jump target is computed wrongly.  start.s does
   `lr = arm_init_VA + boot_args.virtBase - boot_args.physBase`, and the same
   arithmetic for the exception/interrupt stacks.  With virtBase = link address
   and physBase = load address the difference has the wrong sign, so both the
   target and the stacks are bogus.  An explicit `adrp arm_init; sub x22;
   add x23; br` replacement did not help, so the register roles need verifying
   rather than guessing.
2. the first stack store faults and the exception is taken at EL2 (Gunyah),
   which is why our EL1 vector table never runs and no register dump appears.

### What is proven working

- Mach-O relocation, device tree handoff, boot_args, entry into `_start`.
- Bootstrap page tables and the V=P window.
- The framebuffer must be mapped **inside the kernel's own V=P table**
  (`0xf8000000`, one 32 MB WA block) or every post-MMU write faults - that was
  the cause of the earlier "MMU on = dead" behaviour.
- `fastboot boot` of a 16 MB image works; the shim loads in ~1 s.

### Next

Print `boot_args.virtBase/physBase` and the computed target from inside
start.s to the panel before the jump, so the last step stops being a guess.