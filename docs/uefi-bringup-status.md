# UEFI 路线 bring-up 状态（2026-10-21）

## 一句话结论

容器、shim、帧缓冲、固件卷结构**全部验证可用**；
固件自身（SEC 起）在跳转后**没有任何输出**，即使已经接通帧缓冲 DEBUG 通道。
当前最强假设：`MailiPkg` 源自 SM8850 的 `KaanapaliPkg`，SoC 层与 SM8950 不匹配，需要重写而非打补丁。

## 已确证可用

| 环节 | 证据 |
|---|---|
| v4 容器 + shim + FD | 屏幕显示 shim 六行输出 + 红绿色带 |
| panel0 / 帧缓冲 | `FB base=F8C60000 stride=00001180`，字形正常 |
| FD 拷贝 | `src=C782F000 dst=C6800000`，大小精确 0x369000 |
| ABL 传入 DTB | `x0=dtb=D2C87000`（在 `Kernel` 区域内，合理）|
| 固件卷入口 | 首指令 `b 0x1000`，`0x1000` 落在 SEC 文件范围 `0x48–0xE000` 内 |
| PCD 写入 | 固件内 `0xC76C4000`=1 次、`0xA760D000`=0 次、`0xC6800000`=2 次 |

## 已排除的假设

| 假设 | 验证方式 | 结果 |
|---|---|---|
| 容器格式错误 | 自建 v4 + shim 成功显示 | ❌ 排除 |
| 面板未供电 | 红绿条可见 | ❌ 排除 |
| 帧缓冲地址错 | 与 DTB `Display_Reserved` 一致 | ❌ 排除 |
| FD 拷贝失败 | 打印 src/dst，大小吻合 | ❌ 排除 |
| FD 入口错 | FFS 解析确认 0x1000 在 SEC 内 | ❌ 排除 |
| 栈落在保留区 | 改为 DTB 的 `UEFI_Stack` (`0xC76C4000`) | ❌ 排除 |
| 定时器 PCD 错 | 反汇编取真值 29/30/27/26 | ❌ 排除 |
| `ArmSmmuDetach` 错 | 改空实现 | ❌ 排除 |
| BootShim 更可靠 | 用上游 BootShim 装载 | ❌ 不可用（载荷偏移不符）|
| 固件无输出通道 | 实现 `FrameBufferSerialPortLib` | ✅ 通道已建，仍无输出 |

## 关键数据（从设备提取）

```
FD_BASE          0xC6800000        uefi_a 分区的 ELF64 PT_LOAD
FD_SIZE          0x369000          同上 filesz
GIC Distributor  0x17000000        DTB registermap
GIC Redist     0x17080000        DTB registermap
UEFI_Stack       0xC76C4000 + 0x30000   DTB memorymap
SEC_Heap         0xC7614000 + 0xB0000   同上
UEFI_FD          0xC6800000 + 0xD00000  同上
Display_Reserved 0xF8C60000 + 0x2B00000 同上（帧缓冲）
定时器 PPI        29 / 30 / 27 / 26     ArmTimerDxe .text @0x433c
```

完整 75 条 SID 映射见 `port/fw/hongkong-sids.txt`。

## 可复用工具

| 文件 | 用途 |
|---|---|
| `port/uefi-hongkong/shim/` | 裸机 shim（控制台 + 色带 + FD 拷贝），CI 自动构建 |
| `port/uefi-hongkong/Platforms/Xiaomi/hongkongPkg/` | 设备包（含 `FrameBufferSerialPortLib`）|
| `port/uefi-hongkong/Silicon/Qualcomm/MailiPkg/` | SoC 包（源自 KaanapaliPkg）|
| `port/scan_movz.py` | 从 stock PE 提取 `MOVZ` 立即数（定时器号就是这样找到的）|
| `port/fv_files.py` | FV/FFS 结构解析 |
| `port/fw/uefi_a.img.dump/` | UEFIExtract 解出的 1305 个 stock 模块 |

## 测试纪律（血泪教训）

1. **每次上机前确认设备状态干净**：长按电源 15 秒彻底断电，再进 fastboot
2. **确认屏幕在 fastboot 下能显示 FASTBOOT 字样**——否则"黑屏"这个观测被上一次的残留污染
3. `fastboot boot` 返回 OKAY 但设备仍在 fastboot，说明 ABL 未交接，属于状态问题而非代码问题
4. 用 `hongkong-fbdbg.img` 作为基线：它能给出 shim 六行输出，是唯一可信的"系统活着"证明

## 下一步

1. 对照 stock FV 的 `Sec` / `PI` 模块，反推 SM8950 的真实初始化序列（DDR、时钟、SMEM）
2. 为 SM8950 写**独立**的 SoC 包，而不是继续在 SM8850 副本上修补
3. 若仍无进展，考虑 Mu-Silicium 在 SM8950 上需要 XBL 预设状态这一可能性（stock 是 `XBL → UEFI`，我们跳过了 XBL）
