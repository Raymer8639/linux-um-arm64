[English](README.md) | [简体中文](README.zh-CN.md)

# Linux/UML on arm64

`ARCH=um SUBARCH=arm64` —— 一个把 Linux 内核当作普通 aarch64 用户态进程来跑的实现，
于是手机不需要 root、KVM 或 hypervisor 也能运行真正的内核。

本仓库是 Linux User-Mode Linux（UML）的 Android/Termux ARM64 下游移植。它不是 UML
的官方上游，也不主张 ARM64 UML 由主线 Linux 维护。官方 UML 树面向通用 UML 与
x86/x86_64 实现；本分支承载的是手机上需要的 Android bionic 与 ARM64 工作。

官方上游：`https://git.kernel.org/pub/scm/linux/kernel/git/uml/linux.git`

项目文档在 `docs/` 下：[UPSTREAM.md](docs/UPSTREAM.md)、[BUILD.md](docs/BUILD.md)、
[TESTING.md](docs/TESTING.md)、[SECURITY.md](docs/SECURITY.md)、
[CONTRIBUTING.md](docs/CONTRIBUTING.md)、[RELEASE.md](docs/RELEASE.md)。

## 状态

能用，而且是测出来的，不是声称的：

| | |
|---|---|
| 启动到用户态 | Alpine 与 Debian 12，4 KB 与 16 KB guest 页 |
| 系统调用拦截 | `PTRACE_SYSEMU`；宿主没有该能力时的系统调用**取消**；seccomp 过滤器拒绝被取消的调用时的系统调用**替换** |
| 用户态模式 | `SECCOMP`（默认快路径）与 `ptrace` |
| 可加载模块 | `modprobe`/`rmmod`、树外驱动、固件加载 |
| 网络 | 基于 vector `fd` 传输的用户态 TCP/IP（passt）—— DHCP、DNS、TCP |
| USB 透传 | 真实的 RTL8811AU 网卡从 Android 应用经 USB/IP 进入 guest，`rtw88_8821au` 绑定成功，`wlan0` 出现并可扫描 |
| 容器 | `dockerd` 在 cgroup v2 上用 overlay2；`docker run hello-world` 可以拉取并运行 |
| 宿主 libc | glibc 与 bionic（Android NDK） |

7.3.1 基线在发布前于 Termux 宿主上过了一遍门禁：启动到 Arch aarch64 用户态
（`GATE_OK`、`uname -m` = `aarch64`）、网络 10/10（DHCP、地址、路由、ICMP、DNS、
TCP、并发、干净退出）、带 IPv6 出口时 15/15（AAAA 解析与 guest 侧的 IPv6 HTTP 抓取）、
ext4 读写并在重启后保持、只读挂载拒绝写入，以及短 soak 8/8 次干净启动。

尚未完成、或尚未验证：

- **没有 32 位兼容层。** `CONFIG_COMPAT` 关闭，guest 只能跑 aarch64 用户态。
- **`wlan0` 关联（association）未测试。** 射频能起来，固件能加载，扫描能返回真实 AP
  与信号强度。关联需要当时拿不到的凭据，除扫描外的路径都没有跑过。
- **`BUG: Bad rss-counter state ... type:MM_FILEPAGES val:1`** 过去每个退出进程打印一次。
  在 16 KB 页下，`PTRS_PER_PTE` 跟着 `PAGE_SHIFT` 走之后就消失了，而且是在过去能稳定
  复现它的负载下。4 KB 构建是否也干净尚未测试：那里修掉的机制在 4 KB 下不可能发生，
  所以如果还出现，说明另有原因。
- **没有 cpuset 控制器。** `CONFIG_CPUSETS` 依赖 `SMP`，而 UML 是单处理器。
  `docker info` 会警告然后照常运行，没有可以绑定的东西。
- guest 的 vDSO 时间（不经过宿主系统调用的 `clock_gettime`）已经写了但没合并：它塞不进
  4 KB 页，并且需要 `GENERIC_VDSO_OVERFLOW_PROTECT`，否则 guest 时间会在几十分钟后回绕。

三个值得点名的 bug，都是在手机上跑出来的（不是读代码读出来的），并且都已在本分支修好：

- **stub 里的指针认证没有被关掉。** 每个 guest mm 一个 stub，意味着 guest 的一次
  `fork()` 会启动新 stub，而启动 stub 是一次 `execve()`，arm64 上这会生成新的 PAC
  密钥。子进程返回到一个 `APIAKey` 并没有签过它栈上返回地址的 stub，而 glibc 的
  `_Fork` 正是 `paciasp` / `svc #0` / `autiasp` —— 于是在 FEAT_FPAC 上子进程在
  `_Fork` 里直接死掉，什么都不打印。该问题在 Armv8.3 之前的硬件上是 NOP，在 Armv9 上
  是致命的。它会让每一个用分支保护编译出来的容器、在每一次 fork 时挂掉。
- **SECCOMP 模式下 guest 的 FP/SIMD 状态跨信号从来没有保存/恢复**，因为判断条件拿
  `host_fp_size` 和 `sizeof(struct user_fpsimd_state)`（528）比，而不是和实际拷贝的
  负载（520）比。Go 的 `SIGURG` 抢占随后破坏了它自己的 AES-GCM 状态，凡是超过几 MB 的
  TLS 传输都会以 `bad record MAC` 失败。
- **`PTRS_PER_PTE` 被写死成 512**，而它应当是 `PMD_SIZE >> PAGE_SHIFT`：4 KB 时刚好
  正确，16 KB 时大了四倍，于是通用 mm 会写到 PMD 所拥有的窗口之外。

测试过的设备：

- **Poco F3**（`M2012K11AG`/`alioth`，骁龙 870 / SM8250），Android 15，宿主内核
  `4.19.246`，4 KB 页，Armv8.2。这台宿主的 arm64 还没有 `PTRACE_SYSEMU`（5.3 才加入），
  所以取消/替换两条路径不是纸面功能，而是每天在跑。它既没有指针认证也没有 SVE，
  因此对这两者什么也说明不了。
- **Galaxy S26 Ultra**（`SM-S948B`/`m3q`），Android 16，宿主内核 `6.12.30`，
  4 KB 页，Armv9，带 SVE 与指针认证。另一个极端：当前宿主，`PTRACE_SYSEMU` 在，
  且 CPU 具备老机器缺的特性。上面三个 bug 里有两个在 Poco 上不可见、在这里立刻暴露
  —— PAC 那个在这颗 CPU 上致命、在那颗上是 NOP —— 所以这一对设备本身就是重点，
  而不是其中任何一台。
- 一台 **arm64 Debian 运行域**，用于跑门禁。

两条拦截回退路径在任何地方都可以用 `nosysemu` 与 `nocancel` 强制打开，所以不需要
手里有一台 4.19 的手机也能测。

## 发布

发布内容不是 tarball。这里的一次发布，是一小组能在 Android 上直接运行的二进制：
同一次提交、同一个 NDK、同一个 Android API level 一起构建出来的。

| 发布 | 基线 | 说明 |
|---|---|---|
| [`um-arm64-v0.4.0`](https://github.com/Raymer8639/linux-um-arm64/releases/tag/um-arm64-v0.4.0) | Linux 7.3.1 | 当前版本；UP 内核，6 个资产 |
| [`um-arm64-v0.3.0`](https://github.com/Raymer8639/linux-um-arm64/releases/tag/um-arm64-v0.3.0) | Linux 7.2-rc4 | 增加 SMP + Docker 内核 profile |

`um-arm64-v0.4.0` 包含 `linux-bionic`、`stub_exe`、`umnet`、`passt-bionic`、
`MANIFEST` 与 `SHA256SUMS`。每个资产都是 aarch64 且**静态链接**：Android 上没有
`/lib/ld-linux-aarch64.so.1`，动态链接的 UML 会在 `main` 之前就死掉、并且什么都不打印；
而且应用本身也只能从自己的 native library 目录执行。`linux-bionic-smp`（SMP + Docker）
是可选附加资产 —— 见 [RELEASE.md](docs/RELEASE.md)。

MANIFEST 钉住内核版本与上游基线，校验器会从二进制里把版本读出来，而不是相信 tag：

```sh
tools/um-arm64/harness/verify-release.py check --tag um-arm64-v0.4.0
```

## 基线

- 基线：**Linux 7.3.1**（stable `v7.3.1`，peeled `79a90760`），2026-11-07 合并进
  `um-arm64`（merge `2858ca64afa3`，first-parent `a541b5561e98`）。移植本身最早是对着
  uml/next `1590cf032`（Linux 7.2-rc4）写的。
- 分支：**`um-arm64`**，一个改动一个提交。上游是**合并**进来的，从不 rebase 上去，
  所以一次基线迁移不会改变下游提交的哈希。
- 7.3.1 这一步是直接 merge stable tag：上游在 `arch/um/` 下动了十个文件，唯一的冲突是
  `arch/x86/um/asm/ptrace.h` —— 上游 `a661c34` 把 `__FRAME_OFFSETS` 改成无条件定义，
  好让 32 位构建保留寄存器偏移，而本移植另外包含了 uapi 的 `<asm/ptrace-abi.h>`。
  两者都保留。
- 整个系列里大约三分之一并不是 arm64 特有的（一路上顺手修的 x86 与通用 `um/` 修复），
  这些改动可以独立成立。

## 构建

从 x86_64 交叉编译只需要 clang：上游已经把 `ARCH=um` 下的 `SUBARCH=arm64` 映射成
`--target=aarch64-linux-gnu`，所以命令行和在 arm64 宿主上完全一样。

**glibc**（开发用，门禁用的就是这个）：

```sh
make ARCH=um SUBARCH=arm64 LLVM=1 defconfig
make ARCH=um SUBARCH=arm64 LLVM=1 -j$(nproc)
```

或者用 `tools/um-arm64/harness/build.sh`，它会加 ccache，并把门禁实际运行的那个二进制
发布出来。

**bionic**（Android NDK，应用能 exec 的版本）：

```sh
NDK=/path/to/android-ndk-r29 harness/build-bionic-termux.sh
```

内核自己靠编译器是否定义 `__ANDROID__` 来识别 bionic，所以那个脚本唯一的差别是
target triple。它做静态链接，因为 Android 没有 `/lib/ld-linux-aarch64.so.1`，而且
应用只能从自己的 native library 目录执行。

额外的配置片段：

```sh
EXTRA_CONFIG="config/usb-wifi.config config/docker.config" tools/um-arm64/harness/build-bionic.sh
```

## 运行

一条命令，一个 shell：

```sh
./linux mem=512M ubd0=rootfs/alpine.ext4 root=/dev/ubda rw \
        init=/bin/sh con=null con0=fd:0,fd:1
```

`con0=fd:0,fd:1` 把 guest 控制台接到当前终端。stdin 不是可 poll 的对象时改用
`con0=null,fd:1` —— UML 会把控制台描述符注册进 epoll，而对普通文件做 `epoll_ctl`
会失败于 `EPERM`，之后控制台就停了，启动看起来像是死掉了。

本系列新增的常用选项：

| | |
|---|---|
| `seccomp=on/auto/off` | 用户态模式；`on` 每次系统调用大约快 5 倍 |
| `nosysemu` | 假装宿主没有 `PTRACE_SYSEMU` |
| `nocancel` | 假装宿主拒绝被取消（`-1`）的系统调用 |
| `seccomp_spin=<us>` | stub 在挂起前的自旋预算；`0` 恢复为总是挂起 |
| `prefault=<bytes>` | 匿名缺页的 fault-around 窗口；`0` 关闭 |
| `stub_exe=<path>` | 从路径而不是 memfd 执行 stub |

## 测试

下面的一切都可复现，而且 harness 故意比它测的东西更严格。

一个 gate 就是一次必须打印出 marker 字符串的启动。**判定只看 marker，永远不看退出码** ——
`adb shell` 无论远端命令做了什么都会返回 0，而被截断的日志和「内核停了」无法区分。

在别人已经持有的 backing image 上再启动第二个 guest，会被提前拒绝
（`harness/boot.py`）。UML 对镜像取的是排它 `F_SETLK`，第二个 guest 会以**没有磁盘**的
方式起来：控制台说 `NO_DISK`，启动 marker 照样打印，而所有写入被静默丢弃。只读 marker 的
gate 会通过，数据却哪儿都没去 —— 所以 harness 在启动前探一次锁，并告诉你要么复制镜像、
要么先停掉另一个 guest。

```sh
GATE=g3 MARKER=UMARM_BOOT_OK INIT=/gate3-init \
  UBD0=$PWD/rootfs/alpine.ext4 EXTRA_ARGS="rw seccomp=on" tools/um-arm64/harness/boot.sh
```

它会打印 `verdict=PASS|FAIL|SIGNAL`、产物目录，以及 `kernel_bugs=N` —— 对启动日志里
`BUG:`/`WARNING:` 的**独立**扫描，因为早期版本里 marker 会短路掉 bug 检查，一个真实的
rss-counter BUG 就这样藏在绿色的 gate 里。

门禁矩阵，包括按 N 轮抽样跑的昂贵项：

```sh
N=20 tools/um-arm64/harness/loop.sh            # g2mini g2alpine g3 g5 g3noaslr g4 fp fpnoaslr
```

手机上走 adb，接口相同：

```sh
PUSH=1 BIN=$PWD/artifacts/linux-bionic GATE=g3 INIT=/gate3-init \
  UBD0=alpine.ext4 EXTRA_ARGS="seccomp=on" tools/um-arm64/harness/android.sh
```

输出**在设备上**捕获、事后再拉回，绝不做流式传输：流式会丢掉尾部，而在这项改动之前，
那台手机上每一个「失败」的 gate 其实都是通过的。

顺带一提的探针：`tools/um-arm64/harness/probe/vethprobe.c` 通过 rtnetlink 建一对 veth，
因为 busybox 的 `ip` 不认识 veth 链路类型 —— 无论 `CONFIG_VETH` 开没开它都以同样的方式
失败，作为测试毫无用处。

### 故障注入与 soak

通过的 gate 只证明快乐路径。`tools/um-arm64/harness/inject-faults.sh` 在 **guest 正在
使用**链路、guest 内核或 UML 进程时把它们打掉，并且在跑之前就写明必须满足的判据，
这样「它跑过了」不能冒充「它通过了」：

| 场景 | 运行中被毁掉的东西 | 必须满足的判据 |
|---|---|---|
| `link-drop` | passt helper（`SIGKILL`） | 注入时是活的；运行自己结束；没有报 `PASS`；没有残留；bridge 的输出显示链路消失 |
| `kernel-kill` | UML 进程（`SIGKILL`） | 注入时是活的；运行自己结束；没有报 `PASS`；没有残留 |
| `guest-panic` | guest 内核（`sysrq-c`） | guest 日志里有 panic；触发确实到达内核；guest 没有干净退出；运行自己结束；没有残留 |

三个场景都在真机上判定并通过（2026-10-17），记录在 `docs/evidence/faults/`。有两条设备
事实是它们能判定的前提：passt 在这个平台上无法靠扫 `/proc` 找到，所以 bridge 把 helper
的 pid 写进 `--passt-pid-file`；以及 UML 即使由脚本启动也需要一个控制终端 —— 在管道上
`con0=fd:0,fd:1` 会在进入用户态前死掉且没有任何输出，读起来和内核故障一模一样。

`tools/um-arm64/harness/soak-umnet.sh` 是重复性的对应物：逐轮判定、泄漏进程与内存漂移。
`--minutes` 在 `--rounds` 之外再加一道墙钟上限，所以
`tools/um-arm64/harness/soak-umnet.sh --minutes 180 --rounds 5000` 是一次小时级运行；
记录落在 `docs/evidence/soak/`。设备工具一次只跑一个 —— 泄漏判据按进程名和启动时间计数。

## 基准

在 Poco F3 上、`adb shell`、guest 里跑 `perfbench` 测得。下表每一列都是**同一次交错运行
内**的一种条件，所以同一行里的差异不可能是温度漂移或调频状态造成的。不同表的列来自不同
运行，不能互相比较 —— 这条限制正是这套 harness 的意义，不是形式主义。

7 轮交错的中位数，单位 µs/op，seccomp 模式：

| | native | proot | 128K 窗口 | 512K 窗口 |
|---|---|---|---|---|
| syscall | 0.079 | 0.166 | 2.049 | **1.999** |
| openat | 2.503 | 28.005 | 4.494 | **4.466** |
| fault | 1.652 | 1.496 | 11.812 | **10.884** |
| forkexec | 3196 | 516 | 1187 | **1177** |

**`fault` 这一行不是一次测量。** `perfbench` 按 `sysconf(_SC_PAGESIZE)` 步进，而这个
guest 跑 16K 页、宿主是 4K，所以 native 的数字是每 4K 页、guest 的是每 16K 页 —— 同一个
数字对应的内存差了四倍。按字节算，guest 在新分配的匿名内存上是宿主的 **1.77×**，而不是
这一列看起来的约 7×。这一行的「按页」读法在这里被信了很久，还害了不少工作去追一个其实
主要是单位错误的开销；现在 `tools/um-arm64/harness/probe/faultbench.c` 同时报按页和按 MB，
让这件事不会再发生。

两个 guest 列是同一个二进制、两条命令行，只差 fault-around 窗口：128K 是过去的默认值
（16K 页下 8 页），512K 是现在的默认（32 页，斜坡顶部）。另一次只跑 seccomp 的运行把窗口
拆开看 —— 同样是同一次运行内的所有条件：

| | native | proot | `prefault=0` | 8 页 | 32 页 |
|---|---|---|---|---|---|
| fault | 1.540 | 1.504 | 19.076 | 11.791 | **10.913** |

也就是说：有窗口本身值 1.62×，从 8 页加宽到 32 页再添 8%。`syscall`（2.001 / 1.953 /
2.043）与 `forkexec`（1144 / 1152 / 1144）在这三种条件下不动，这正是「改动只碰了缺页
路径」的检查。

本文件历史上的更早表格曾带 `before` 与 `+waiter-bit` 两列，那次运行与上面任何数字都不再
共享机器状态。它们没有和更新的数字并排放，而是不再复现：waiter-bit 的改动把 `syscall`
从 9.841 拉到 1.995 µs，fault-around 把 `fault` 从 18.897 拉到 11.076，各自都只在自己
那次运行内可比。

**方法学**，因为没有它这些数字没有意义：

- 绑在 4–5 核，并在 6 核上挂一个忙循环。这三个核共享一个 cpufreq policy，调速器是
  `schedutil`，而没有 root 去钉频率，所以没有这个「守门」进程，`perfbench` 测的就是
  频率爬坡本身。光这一点就能把 validator 从 0.892 推到 1.3 ns/iter。
- 条件**交错**跑，不是分块跑，这样漂移对每个条件的影响一样。
- 用中位数，从不用均值。
- `compute`（纯用户态算术、不进内核）是**校验器**。如果它跨条件的差异超过 5%，或在某个
  条件内抖动超过 8%，整张表作废、什么都不打印。完全相同的指令流不可能有差异；有差异
  就说明机器动了。
- 这个校验器很值。上面的窗口扫描在产出表格前跑了两遍：三个内核 × 两种拦截模式是八个
  条件，长到足以让手机在底下变化，两次都停在约 27% 抖动、什么也没打印。只点名问题真正
  需要的那个模式（`MODES=sec`）把运行砍半，它以 1.1% 通过了。收集时间比机器保持静止的
  时间还长的表格，并不因此更严谨。
- `native` 和 `proot` 是对照。放 proot 是因为它是未 root 手机上真正的替代方案，
  不是因为裸机才是。

**注意事项：**

- 一台手机、一颗 SoC。这里没有任何结论说明它在别处会怎样。
- 这些是 `adb shell` 的数字。同一个内核跑在 Android 应用里是不绑核、不升频的，
  实测约 1.9 µs/系统调用，`compute` 慢约 18% —— 那是真实世界，不是最好情况。
- `syscall` 是 `getppid()`，最便宜的系统调用，所以它是对拦截开销最纯粹的度量，也最不
  像真实工作。`openat` 才是像程序实际行为的那一行。
- `compute` 按构造就是原生速度 —— guest 用户代码就是在真实 CPU 上跑真实指令。这正是
  整件事的意义，也是为什么在 guest 里编译的代价大致等于编译本身的代价。

复现：

```sh
KERNELS="base=... waiter=... fault=..." ROUNDS=7 tools/um-arm64/harness/verifybench.sh
```

它会写出 `manifest.txt`（git HEAD、每个二进制的 md5 与 `file(1)`、设备型号、宿主内核、
页大小、每个 cpufreq policy、电池状态）、`results.tsv`（每个样本一行）、每次测量的原始
记录，以及 `env-before/after.txt`。表格只由 TSV 推导，TSV 只由原始记录推导，所以三者
里任何一个都能从下一层重算出来。

## 与上游的关系

Linux UML 的维护者是 Richard Weinberger、Anton Ivanov 与 Johannes Berg。开发在
`linux-um@lists.infradead.org`；维护者树提供 `master`、`next` 与 `fixes`。主线源码
拥有 `arch/um/`、`arch/x86/um/`、`fs/hostfs/` 与 UML 文档。

通用 UML 与 x86 改动，与 Android/ARM64 专属改动分开评审、分开准备上游提交。下游分支保持
为一个聚焦的移植，而不是把 ARM64 fork 当成第二个上游。

这个系列是 `git format-patch`-ready 的：一个改动一个提交、带 DCO sign-off，并用内核
常规工具检查过。许可证与内核其余部分一样是 GPL-2.0。
