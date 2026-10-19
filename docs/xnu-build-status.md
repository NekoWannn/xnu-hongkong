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
