# 里程碑：XNU 首次运行到用户态（2026-10-21）

## 结果

```
*** SWEET userspace: init running as PID 1 ***
SWEET init: still alive
```

XNU 在本设备（Xiaomi 18 Pro / SM8950 / hongkong）上完成内核启动、挂载根文件系统、
执行 PID 1，并由用户在 EL0 写出第一行输出。

对应提交：`92b62c4 hongkong: give the early console fbcon's kernel VA`

## 完整启动链

```
ABL → 自制 shim（0xc7800000）→ SWEETXNU payload → XNU __start
  → arm_init（PE、拓扑、CPU 主核）
  → arm_vm_init（bootstrap 页表、physmap）
  → serial_init → fbcon 注册（ml_io_map，TTBR1）
  → kprintf 可用
  → machine_startup
  → bsd_init → BSD root: md0（mockfs，ramdisk 内的 init）
  → load_init_program → execve → load_machfile → 提交
  → 切换用户地址空间（TTBR0 = 用户页表）
  → PID 1 在 EL0 执行 → write(/dev/console)
```

## 决定性修复：早期控制台的映射来源

**现象**：`load_machfile()` 之后的标记全部消失，看起来像内核卡死。

**真因**：`sweet_early` 一直用**物理地址** `0xf8c60000` 写帧缓冲，依赖 TTBR0 身份映射；
exec 提交阶段切换地址空间后，TTBR0 变成用户页表，物理直连当场失效 —— 观测手段消失，
而内核其实一直在正常推进。

**修复**：让 fbcon 把它 `ml_io_map_wcomb()` 得到的**内核 VA**交给早期控制台：

```c
/* pe_serial.c */
fbcon_base = (volatile uint32_t *)ml_io_map_wcomb(reg[0], reg[1]);
sweet_early_set_fb(fbcon_base);      /* TTBR1，用户态切换后仍有效 */

/* sweet_early.c */
void sweet_early_set_fb(void *base)
{
    if (base != NULL) {
        early_fb = (volatile uint32_t *)base;
        __asm__ volatile("dsb sy" ::: "memory");
    }
}
```

## 沿途解决的其他问题（按发现顺序）

| 问题 | 根因 | 处理 |
|---|---|---|
| `stage > 20` 标记不显示 | `sweet_early_mark` 静默丢弃大编号 | 删除该限制 |
| 2 MB 块映射要求地址对齐 | `0xf8c60000` 非 2 MB 对齐 | 用 `0xf8c00000` + `0x60000` 偏移 |
| `phystokv()` 在 physmap 之后不可靠 | ptov 表刚建好，孔径未切换 | 改用线性偏移 `PA + (gVirtBase-gPhysBase)` |
| `pmap_map_bd: no PTE` | `io_map()` 启动期的预建 PTE 空间仅 12 MB，而 `FB_SIZE` 报了 43 MB | ADT 的 `FB_SIZE` 改为 `stride×height`（`0xa69000`）|
| fbcon 起始行落在不可见区 | 面板顶部约 300 行不可见 | `fbcon_row = 28` |
| 早期控制台清行使 kprintf 文字消失 | `sweet_early_clear_row` 整行涂黑 | 改为不清行（只画字形）|
| `BSD root` 等待 Apple_HFS | 内核按内容类型等根设备 | `rd=md0` |
| 首次 exec 失败后第二次卡在 `copyout` | `.development` 返回 ENOENT 后状态异常 | `launchdsuffix=release` |
| `cpu_id` 断言失败 | `cpus=1` 时拓扑找不到启动核 | 回退为 CPU 0（仅调试）|
| `PSCI CPU_ON` 返回 -4 | Gunyah 下 vCPU 已处于 on 状态 | 暂用 `cpus=1` |

## 遗留问题

- **SMP**：`cpus=1` 是为绕开 `PSCI CPU_ON rc=-4` 的临时方案，8 核尚未启用
- **TTBR0 未拆除**：`set_mmu_ttb(invalid_ttep)` 仍被注释（fbcon 已不依赖它，但早期标记过去依赖）
- **根文件系统**：只有 mockfs + 16 KB init，真实 iOS 根需要 UFS 4.0 驱动或 UEFI/NVMe 路线
- **显示**：fbcon 可用，但无 GPU/合成器
- **驱动**：触摸、按键、电池、看门狗均为桩实现

## 后续方向

转向 UEFI 路线（见 `docs/uefi-migration.md`）：UEFI 随 `boot.img` 加载，不刷分区；
用 UniXNU 的 DarwinBoot 取代自制 shim，可复用其驱动框架与 4 KiB 页补丁。
