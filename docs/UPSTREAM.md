# Upstream Tracking

This repository is an ARM64 Android/Termux downstream port of Linux User-Mode
Linux (UML). The ARM64 fork is not the UML upstream.

## Official Source

- Repository: `https://git.kernel.org/pub/scm/linux/kernel/git/uml/linux.git`
- Development branch: `next`
- Integrated branch: `master`
- Urgent fixes: `fixes`
- Mainline integration: `https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git`
- Mailing list: `linux-um@lists.infradead.org`
- Patchwork: `https://patchwork.ozlabs.org/project/linux-um/list/`

The official maintainer list names Richard Weinberger, Anton Ivanov and
Johannes Berg. The maintained source areas are generic UML, x86/x86_64 UML,
hostfs and UML documentation. The current upstream architecture reference is
`arch/x86/um`; this repository's `um-arm64` branch is downstream work.

## Remote Layout

The local repository keeps the GitHub fork as `origin` and the official tree
as a fetch-only `upstream` remote:

```text
origin   https://github.com/Raymer8639/linux-um-arm64.git
upstream https://git.kernel.org/pub/scm/linux/kernel/git/uml/linux.git
```

The `upstream` push URL is intentionally disabled. Never push to the kernel
tree from this repository.

## Follow Policy

1. Record the current `master`, `next` and `fixes` commits before reviewing.
2. Inspect changes under `arch/um/`, `arch/x86/um/`, `fs/hostfs/` and UML docs.
3. Classify each relevant change as generic UML, x86-only, Android/ARM64-only,
   or not applicable.
4. Port generic fixes selectively to `um-arm64`; do not merge the x86 tree
   wholesale into the ARM64 branch.
5. Mark each ported change as downstream-only, prepared for upstream, submitted,
   or accepted upstream.
6. Record the upstream baseline, selected commits and test results in the
   project history before changing a release baseline.

At the initial project setup on 2026-10-03, the observed official refs were:

| Ref | Commit |
|---|---|
| `master` | `974b808d85abbc03c3914af63d60d5816aabf2ca` |
| `next` | `2f88f5689de1a039764d00f466209acf0c010eaf` |
| `fixes` | `af421e9aed3920c7ac88c24daa48606c7112feca` |

These are an observation record, not a replacement for fetching the refs.

## Baseline Record

| Date | Baseline | Commit | Notes |
|---|---|---|---|
| 2026-10-03 | uml/next, Linux 7.2-rc4 | `1590cf0329716306e948a8fc29f1d3ee87d3989f` | start of the ARM64 port |
| 2026-11-07 | stable `v7.3.1` | `79a907609d53d5cd645b42c11550d295d43abf99` | merged into `upgrade/kernel-v7.3.1` as `2858ca64afa3` |

The 2026-11-07 step moved the port from a 7.2-rc4 development snapshot to the
7.3.1 stable release. It was a straight merge of the stable tag into the
downstream branch. Upstream churn under `arch/um/` between the two points is
small (10 files); the ARM64 port's own files under `arch/arm64/um/` and
`arch/um/` merged without conflict. The single conflict was
`arch/x86/um/asm/ptrace.h`: upstream `a661c34` (guard the ptrace-abi register
macros with `__ASSEMBLER__` or `__FRAME_OFFSETS`, and define
`__FRAME_OFFSETS` for 32-bit as well) turned the conditional define into an
unconditional one, while this port additionally includes `<asm/ptrace-abi.h>`.
Both were kept: upstream's unconditional define plus the port's include.

Release baselines must be moved only after the build and the device gates pass;
see `docs/RELEASE.md`.
