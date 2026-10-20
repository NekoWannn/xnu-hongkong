# 从自制 shim 迁移到 BootShim + UEFI

## 结论先说

UniXNU 的启动链与本项目**同构**：都是 `fastboot boot` 一个自定义镜像 → shim 跳转到自己的负载。

区别只在负载是什么：

| | 本项目（现状）| UniXNU（目标） |
|---|---|---|
| boot.img kernel 字段 | shim + `SWEETXNU` payload | **BootShim + UEFI 固件卷** |
| 负载 | XNU 内核镜像 + ADT + ramdisk | UEFI 固件卷（内含 UniXNU + DarwinBoot）|
| 内核加载者 | 我们的 `loader.c` | **DarwinBoot**（UEFI 应用）|
| 分区改动 | 无 | **无** |

**因此迁移不需要刷写任何分区，风险与当前 `fastboot boot` 相同。**

## 三块组件

| 来源 | 内容 | 状态 |
|---|---|---|
| 手头的 SM8950 UEFI | 提取 Qualcomm DXE 驱动二进制（PE/COFF）：存储、电源、显示、USB | ✅ 已有，**仅用于提取驱动** |
| `uefi-sheng` 的 `Mu_Basecore` + `Silicon` | 开源 EDK2 核心与 Qualcomm 框架 | ⬜ 待拉取 |
| 我们自己的 `Platforms/<device>` | 内存布局、GOP 帧缓冲参数、驱动加载顺序（DSC/FDF）| ⬜ 待编写 |

三者用 `build_uefi.py` 组装成固件卷，再与 BootShim 拼接进 `boot.img`。

## 需要的设备参数

从现有 ADT 与设备信息可直接取得大部分：

- 固件基址与大小（`FD_BASE` / `FD_SIZE`）——决定 BootShim 的编译参数
- UI 帧缓冲：物理地址 `0xf8c60000`、1120×2436、stride 4480（GOP 配置）
- GIC：`0x17000000` / `0x17080000`
- UFS 控制器：`0x01d84000`
- 内存窗口：`0xa8000000` 起，大小按实际 DRAM

## 分阶段验证

对齐 UniXNU 的 bring-up 顺序，每一步都有明确的可观察结果：

1. **固件**：UEFI 起得来、能看到输出
2. **菜单**：UniXNU 显示启动菜单
3. **负载**：DarwinBoot 能读到 `kernel.macho`
4. **内核入口**：XNU 的 `start.s` 能跑（本项目已验证过 SoC 侧可行）
5. **平台专家**：`ArmDTPlatform` 匹配 ADT
6. **存储**：userdata 分区被发布
7. **根卷**：挂载并启动 `launchd`

## 现有资产的处置

| 资产 | 迁移后 |
|---|---|
| `port/shim2/`（自制 shim）| 由 BootShim 取代，**退役** |
| `port/hongkong-0003-early-console.patch` | 早期控制台/页表绕过，**退役**（UEFI 负责 GOP 与内存）|
| `port/mkadt_hongkong.py` | **保留**，ADT 仍需生成，可移植到设备仓库 |
| `port/hongkong-0001-board.patch` | 板级配置，部分可留作参考 |
| `port/sweet-init.S` | 初期仍可用于验证用户态 |

## 风险

- **Platforms 配置错误**会导致 UEFI 在早期挂死且无输出；先用最简配置（仅串口/帧缓冲 + 内存映射）验证
- 提取驱动时需确认依赖链（DXE 之间的 protocol 依赖），缺一个就可能卡在 BDS
- GOP 参数必须与面板一致，否则菜单不可见
