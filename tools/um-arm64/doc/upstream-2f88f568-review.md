# 上游 2f88f568「um: fix shutdown __inittext access」审阅

## 结论：**不移植**（本下游不具备触发条件）

## 上游问题

commit `2f88f568`（Johannes Berg，2026-09-28）修的是 shutdown/reboot 走到
`__init` 代码时崩溃：

- `Fixes: a418ab8fddea ("um: mprotect() __init memory")`
- 前置：`8966ed0b2d48 ("um: move init text between __init_begin/end")`

即上游先把 **init text** 移进 `__init_begin`/`__init_end` 之间，再把该区间
设为不可访问；于是退出路径上仍在执行的 `__init` 函数（`os_exit` 相关、
`start_idle_thread`、`longjmp` 等）取指即崩。修法是把这些函数标 `__noreturn`，
并把 `main()` 退出部分拆成独立的 `os_exit()`。

## 本下游为什么不适用

### 1. `__init_begin`/`__init_end` 之间只有 `init.data`，没有 init text

`arch/um/kernel/vmlinux.lds.S` 按 `CONFIG_LD_SCRIPT_STATIC` 二选一：

| 脚本 | 相关片段 |
|---|---|
| `arch/um/kernel/uml.lds.S`（静态，本下游使用） | `__init_begin = .;` / `init.data : { INIT_DATA }` / `__init_end = .;` |
| `arch/um/kernel/dyn.lds.S`（动态） | 同上，只有 `init.data` |

两个脚本都**只把 `init.data`** 放进该区间。上游 `8966ed0b2d48` 把 init text
移进去的动作，本树没有。

### 2. `free_initmem()` 是空实现

`arch/um/kernel/mem.c`：

```c
/*
 * This can't do anything because nothing in the kernel image can be freed
 * since it's not in kernel physical memory.
 */
void free_initmem(void)
{
}
```

本树也没有 `a418ab8fddea` 那套 init-memory `mprotect` 机制；`mark_rodata_ro()`
只保护 `.rodata`（`__start_rodata`..`__end_rodata`），不碰 init text。

### 3. 实测退出路径本来就正常

本下游所有 Termux 真机 gate 日志（含 2026-10-11 的 5 轮连跑）都出现：

```
reboot: Power down
```

并且 supervisor 收到 exit status 0。若上游那个 bug 存在，退出会崩而不是
干净下电。`INIT_JMP_HALT`/`INIT_JMP_REBOOT` 路径（`os-Linux/skas/process.c`）
每次 gate 都会走到。

## 决定

**不移植**，理由不是"优先级低"而是"触发条件不存在"：
- 移植它需要上游 `8966ed0b2d48` + `a418ab8fddea` 两个前置，本树都没有；
- 强行套用这组 `__noreturn` 改动只会改签名而不修任何真实缺陷，属于无谓 churn；
- 若将来合并上游 7.2 之后的 UML 变更、把 init text 移入 `__init_begin` 区间，
  **必须重新评估并移植此修复**——这是明确的跟进条件。

## 跟进条件（写入待办）

本树出现下列任一情况时重开此审阅：
1. `arch/um/kernel/{uml,dyn}.lds.S` 的 `__init_begin`..`__init_end` 区间开始包含
   `.init.text`/`INIT_TEXT`；
2. 引入上游 `a418ab8fddea`（mprotect `__init` memory）或 `free_initmem()` 不再为空；
3. 真机出现退出/reboot 崩溃或 panic（现为干净 `Power down`）。
