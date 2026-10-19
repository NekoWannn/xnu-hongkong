# M0: register facts read from the SM8950 boot hand-off

Read with a self-resetting shim loaded via `fastboot boot` (v4 image), which
draws the values as large 7-segment hex digits on the framebuffer and then
resets the phone after 120 s.

## Values

| Register | Value | Notes |
| --- | --- | --- |
| `CurrentEL` | `0x1` | **XNU/our code runs at EL1**; Gunyah owns EL2 |
| `ID_AA64MMFR0_EL1` | `0x00101024` | `TGran16 = 1` (16K granule supported), PARange 4 (48-bit PA), ASID 2 (16-bit) |
| `ID_AA64PFR0_EL1` | `0x23211111` | EL0-EL3 implemented, FP + AdvSIMD present, GIC field 3 |
| `MIDR_EL1` | `0x512F0031` | matches `/proc/cpuinfo` (cross-check passed) |
| `MPIDR_EL1` | `0x80000300` | bit31 set, Aff0 = 0 (CPU0) |

Cross-check: `MIDR` matches the Android-visible value, which validates the
whole draw-photograph-decode chain.

## Consequences for the XNU port

1. **EL1 guest, not EL2.** Anything in `start.s` / `pe_fiq.c` that touches EL2
   system registers (`HCR_EL2`, `VTCR_EL2`, `VMPIDR_EL2`, ...) is unavailable.
   The `PEBBLE` machine config must behave like a hypervisor guest.

2. **PSCI works over SMC.** `SYSTEM_RESET (0x84000009)` returns control to the
   bootloader and the phone reboots, so Gunyah proxies PSCI. CPU_ON/OFF should
   go the same way.

3. **16K granule is available** (`TGran16 = 1`), so XNU's `PAGE_SIZE_FIXED`
   assumption holds on this SoC.

4. **GIC access is the open question.** As a guest, the physical `GICD`
   (`0x17000000`) and `GICR` (`0x17080000`) may be trapped by Gunyah rather
   than passed through. The current `0012-pebble-lib-gaps` / `pe_fiq.c` code
   assumes direct MMIO. If those reads come back as 0 or fault, the port must
   use the virtual CPU interface (`ICC_*_EL1`, which is what `sleh.c` already
   dispatches through) and let Gunyah inject interrupts.

## How it was measured

`probe25` (see the probe history) draws five lines:

```
1 <EL>   2 <MMFR0>   3 <PFR0>   4 <MIDR>   5 <MPIDR>
```

in 7-segment digits (62x104 px, pitch 96) at y=1180..1860, then
`PSCI SYSTEM_RESET`. Photograph the screen, read the digits.

Earlier attempts (bit matrices) were unreadable through a hand-held photo; the
7-segment form is the first one that decoded cleanly.

## GIC accessibility (second measurement)

Same 7-segment shim, reading the physical GIC from EL1:

| Read | Value | Meaning |
| --- | --- | --- |
| `GICD_CTLR` @ `0x17000000` | `0x000000D0` | physical GICD **is** readable from the guest; ARE_NS and DS are set |
| `GICR_TYPER[31:0]` @ `0x17080008` | `0x00000001` | bit0 (PLPIS) set, bit4 (Last) clear |
| `GICR_TYPER[63:32]` @ `0x1708000C` | `0x00000000` | **affinity 0** |
| `ICC_CTLR_EL1` | `0x00088400` | EOImode = 0 |

### Consequence: redistributor matching must be relaxed

`MPIDR_EL1 = 0x80000300` gives Aff1 = 3, but the first redistributor reports
affinity 0, so an exact `Aff1:Aff0` comparison would never match and
`pe_init_fiq()` would panic. The PEBBLE branch must:

1. try exact `Aff1:Aff0`,
2. then match on `Aff0` only,
3. then fall back to the first frame.

Direct physical GIC access is otherwise viable, so the existing `pe_fiq.c`
Group-1 approach stands.