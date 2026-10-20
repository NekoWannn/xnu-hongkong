#!/usr/bin/env python3
"""Port XNU to the Xiaomi SM8950 ("hongkong") by layering on ios-sweet.

The heavy lifting is done by ios-sweet's xnu-8796 patch series, vendored in
`sweet_patches.bundle`.  This script only:

  1. splits that bundle into dxb/patches/ so the build script applies it,
  2. adds a HONGKONG machine config next to their SWEET one,
  3. points the GIC/timer/framebuffer at this device's addresses.

HONGKONG deliberately defines `SWEET` as well, so every `#if defined(SWEET)`
code path in their patches (early framebuffer console, GIC Group 1 IRQ
handling, DT platform expert, boot CPU driver) stays active and we only
override the hardware constants that differ.
"""
import io
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.join(HERE, "sweet_patches.bundle")
TMP_DIR = tempfile.mkdtemp(prefix="sweetpatches-")

report = []


def read(path):
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except IOError:
        return None


def write(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def sub(path, old, new, label):
    s = read(path)
    if s is None:
        report.append("MISSING  %s" % label)
        return
    if new in s:
        report.append("SKIP     %s" % label)
        return
    if old not in s:
        report.append("NOANCHOR %s" % label)
        return
    write(path, s.replace(old, new, 1))
    report.append("PATCHED  %s" % label)


# ---------------------------------------------------------------- 1. patches
def install_sweet_patches():
    """Split the bundle and apply every patch ourselves.

    The workflow hook runs after dxb/build.sh has already walked its own
    patches directory, so we cannot rely on that loop to pick these up.
    """
    if not os.path.exists(BUNDLE):
        report.append("MISSING  sweet_patches.bundle")
        return
    if not os.path.isdir(TMP_DIR):
        os.makedirs(TMP_DIR)
    text = read(BUNDLE)
    chunks = re.split(r"^### (PATCH|STAGE) (.+)$", text, flags=re.M)

    applied = failed = 0
    stages = []
    for i in range(1, len(chunks), 3):
        kind = chunks[i].strip()
        name = chunks[i + 1].strip()
        body = chunks[i + 2].lstrip("\n")
        path = os.path.join(TMP_DIR, name)
        write(path, body)
        if kind == "STAGE":
            stages.append((name, path))
            continue
        if apply_patch(path):
            applied += 1
        else:
            failed += 1
            if failed <= 6:
                report.append("PATCHFAIL %s" % name)
    report.append("APPLIED  %d sweet patches (%d failed)" % (applied, failed))

    # The stage scripts fetch/stage external sources (BearSSL, libpthread, HFS).
    # They are written for the SWEET config and guard on MACHINE_CONFIG, so run
    # them with that value and let them place the trees the patches refer to.
    env = dict(os.environ)
    env["MACHINE_CONFIG"] = "SWEET"
    env.setdefault("MACOS_VERSION", "13.5")
    for name, path in stages:
        os.chmod(path, 0o755)
        try:
            r = subprocess.call(["bash", path], env=env)
        except OSError as e:
            report.append("STAGEFAIL %s (%s)" % (name, e))
            continue
        report.append(("STAGED   %s" if r == 0 else "STAGEFAIL %s (rc=%d)")
                      % ((name,) if r == 0 else (name, r)))

    hk = sorted(f for f in os.listdir(HERE)
                if f.startswith("hongkong") and f.endswith(".patch"))
    for name in hk:
        if apply_patch(os.path.join(HERE, name)):
            report.append("APPLIED  %s" % name)
        else:
            report.append("PATCHFAIL %s" % name)


def apply_patch(path):
    for cmd in (["git", "apply", "--whitespace=nowarn", path],
                ["patch", "-p1", "--forward", "--no-backup-if-mismatch", "-s", "-i", path]):
        try:
            r = subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            continue
        if r == 0:
            return True
    return False


# ------------------------------------------------------- 2. board config file
HONGKONG_H = r'''/*
 * Board config for the Xiaomi 18 Pro ("hongkong", Qualcomm SM8950): Kryo 470 = Cortex-A76 + Cortex-A55, ARMv8.2, no PAC.
 *
 * Generic ARM like BCM2837 (no APPLE_ARM64_ARCH_FAMILY: none of Apple's
 * implementation-defined system registers exist), 16K pages and GICv3 like
 * VMAPPLE. Unlike VMAPPLE the GIC has two security states (GICD_TYPER
 * SecurityExtn=1): Group 0 belongs to TrustZone, so every interrupt,
 * including the timer, is a Group 1 IRQ taken through sleh_irq().
 */

#ifndef _PEXPERT_ARM64_HONGKONG_H
#define _PEXPERT_ARM64_HONGKONG_H

#define NO_MONITOR                1
#define NO_ECORE                  1

#define HONGKONG                  1
#define SWEET                     1

/*
 * All eight cores (six A55 + two A76) as one homogeneous SMP cluster:
 * cpus=N is clamped here. Same ISA level (ARMv8.2), 64-byte lines, PSCI
 * CPU_ON; the scheduler does not know the A76s are faster (no AMP/P-E
 * clusters yet).
 */
#define SWEET_SMP_MAX_CPUS         8

#define ARM_ARCH_TIMER
#define __ARM_ARCH__              8
#define __ARM_VMSA__              8
#define __ARM_VFP__               4
#define __ARM_COHERENT_CACHE__    1
#define __ARM_DEBUG__             7
#define __ARM_ENABLE_SWAP__       1
#define __ARM_V8_CRYPTO_EXTENSIONS__ 1
#define __ARM_PAN_AVAILABLE__     1
#define __ARM_16K_PG__            1

#define HAS_GIC_V3                1

#ifndef ASSEMBLER
#define PLATFORM_PANIC_LOG_DISABLED
#endif /* ! ASSEMBLER */

/*
 * Qualcomm numbers the architected timer PPIs 1/2/3/0 (secure phys, phys,
 * virt, hyp) instead of the usual 13/14/11/10: the virtual timer XNU uses
 * (CNTV) is INTID 16 + 3 = 19.
 */
#define SWEET_VTIMER_INTID        27
#define SWEET_IPI_SGI             1       // SGI used for cross-CPU signals

#define GIC_SPURIOUS_IRQ          1023    // IRQ no. for GIC spurious interrupt

#define GICR_PE_SIZE              0x20000 // Size of each redistributor region

/* GICv3 register definitions; see GICv3 spec (Arm IHI 0069G) */
#define GICD_CTLR                 0x0

/* Non-secure view of GICD_CTLR (DS=0) */
#define GICD_CTLR_ENABLEGRP1      0x1
#define GICD_CTLR_ENABLEGRP1A     0x2
#define GICD_CTLR_ARE_NS          0x10
#define GICD_CTLR_RWP             0x80000000

#define GICR_TYPER                              0x08
#define GICR_WAKER                              0x14
#define GICR_IGROUPR0                           0x10080
#define GICR_ISENABLER0                         0x10100
#define GICR_IPRIORITYR0                        0x10400

#define GICR_TYPER_AFFINITY_VALUE_SHIFT         32
#define GICR_TYPER_LAST                         0x10

#define GICR_WAKER_PROCESSORSLEEP               0x2
#define GICR_WAKER_CHILDRENASLEEP               0x4

#define ICC_CTLR_EOIMODE                        0x2

#define ICC_SRE_SRE                             0x1
/* End of GICv3 register definitions */

#endif /* ! _PEXPERT_ARM64_HONGKONG_H */
'''


def install_board_header():
    """Board files now arrive as a hongkong patch; only report if missing."""
    if os.path.exists("pexpert/pexpert/arm64/HONGKONG.h"):
        report.append("OK       HONGKONG.h present")
    else:
        report.append("MISSING  HONGKONG.h (no hongkong patch applied?)")


def main():
    if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
        os.chdir(sys.argv[1])
        print("cwd ->", os.getcwd())
    install_sweet_patches()
    install_board_header()
    print("\n".join(report))
    bad = [r for r in report if r.startswith(("NOANCHOR", "MISSING"))]
    print("\n%d edits, %d problems" % (len(report), len(bad)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
