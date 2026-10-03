# Security Boundary

UML runs a Linux kernel as an ordinary host process. It does not provide the
same isolation boundary as a VM or a hardware hypervisor.

In particular:

- `hostfs` exposes host storage and must not be used with untrusted guest code.
- The Android bionic build avoids root and KVM requirements but does not make
  the host process a security sandbox.
- A rootfs image is a safer default for isolation than a hostfs directory, but
  the host process, kernel and user account remain in scope.
- Network and passt access must be treated as host-integrated functionality.
- Reproduction reports should include the upstream baseline, downstream commit,
  host kernel, Android version, device architecture, page size and full gate log.

For a suspected security issue, avoid public issue details until the impact and
affected versions are understood. Upstream UML issues should follow the Linux
kernel reporting process and `linux-um@lists.infradead.org` guidance.
