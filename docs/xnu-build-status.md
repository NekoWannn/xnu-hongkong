# XNU build status: why the link fails

## Symptom

`kernel.development.pebble.unstripped` fails to link with 261 undefined
symbols, led by `__start` and `_call_continuation`.

## Measurements (GitHub Actions run 39288969157, job 118184297105)

```
object count                     873
link.filelist                    988 entries
osfmk.filelist                   304 entries
bsd.filelist                     480, iokit 94, libkern 75
pexpert 11, security 23, libsa 1, san 0
```

`grep -E 'start\.o|cswitch\.o' link.filelist` returns **nothing**, while
`machine_routines.o`, `copyio.o` and `bsd/copyio_tests.o` **are** present.

## Conclusion

C sources make it into the per-component object lists; **assembly sources
(`start.s`, `cswitch.s`, `locore.s`, ...) do not**. `__start` lives in
`start.s` and `_call_continuation` in `cswitch.s`, which is exactly the pair
the linker reports first; the remaining undefined symbols are the ones that
only those objects (and their initialisers) provide.

So the problem is not the machine-config registration, not `MASTER.*`, and
not the patch set: it is that the `.s` files never reach `$(OBJS)`.

## Next step

Find where assembly sources are added to a component's object list — not in
`osfmk/conf/files` (no `.s` entries) and not in `osfmk/arm64/Makefile` (which
only exports headers). Candidates to inspect in a fresh session:

- `makedefs/MakeInc.cmd` (ASM handling, `BUILD_ASM`, per-directory rules)
- `makedefs/MakeInc.rule` (the `%.o: %.s` pattern rule)
- whether a whitelist keyed on `MACHINE_CONFIG` gates which `.s` files build

A quick fix once located: append the needed objects explicitly, e.g. in
`osfmk/arm64/Makefile`

```make
OBJS += start.o cswitch.o locore.o ...
```

but prefer fixing the gate so the list stays correct.

## Notes

- `pe_fiq.c` redistributor matching was relaxed (exact affinity, then Aff0,
  then first frame) after measuring `GICR_TYPER` affinity = 0 with
  `MPIDR = 0x80000300`; see `docs/m0-registers.md`.
- Diagnostics in the workflow must not use backticks around `$O`; the YAML is
  copied verbatim into bash.
