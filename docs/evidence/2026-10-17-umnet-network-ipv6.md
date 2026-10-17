# Termux device evidence: umnet network gate

| field | value |
| --- | --- |
| verdict | `PASS` |
| exit-code | `0` |
| recorded | `2026-10-17T11:18:07Z` |
| message | `device gate passed` |
| synthetic | no |
| machine record | `2026-10-17-umnet-network-ipv6.json` |
| gate | `tools/um-arm64/harness/test-umnet-network.sh --workdir /data/data/com.termux/files/usr/tmp/um-network --log net-gate.log --timeout 300 --dns 8.8.8.8 --ipv6 --ipv6-dns=2001:4860:4860::8888` |

## Host

| field | value |
| --- | --- |
| android-abi | arm64-v8a |
| android-fingerprint | Redmi/air/air:15/AP3A.240905.015.A2/OS2.0.210.0.VGQCNXM:user/release-keys |
| android-release | 15 |
| android-sdk | 35 |
| device-manufacturer | Xiaomi |
| device-model | 23124RN87C |
| host-role | termux-host |
| kernel-release | 5.15.194-android13-8-00019-gf4321180a397-ab15212794 |
| machine | aarch64 |
| python | 3.14.6 |
| termux-prefix | /data/data/com.termux/files/usr |
| tracer-pid | 0 |
| uname | Linux localhost 5.15.194-android13-8-00019-gf4321180a397-ab15212794 #1 SMP PREEMPT Wed Apr 15 01:39:41 UTC 2026 aarch64 |

## Repository

| git | value |
| --- | --- |
| branch | harness/um-init-binds |
| commit | ba31c186e1ae23837c9ece68404a88d08fd60bf4 |
| dirty | False |
| dirty-scope | ['tools/um-arm64/harness', 'harness'] |
| repo | /data/data/com.termux/files/usr/var/lib/proot-distro/containers/archlinuxarm/rootfs/home/Raymer/work_space/um/repo/src |

## Components

| component | sha256 | size | path |
| --- | --- | --- | --- |
| linux-bionic | `b778f4f7c889b258278df8e4d542fc0244a32424bb30fe69067ca66e45ceb9f9` | 94860472 | /data/data/com.termux/files/usr/tmp/um-arm64/new/linux-bionic |
| stub_exe | `731c48d5bf42ef25e26625af6a02627a5d658bf9f67d6f7fc443d6d644d6e52e` | 2000 | /data/data/com.termux/files/usr/tmp/um-arm64/new/stub_exe |
| umnet | `e3701126e61a5aa4d24368b2734833103678426f4c0061e1a92bd25f91f8cc8b` | 2172024 | /data/data/com.termux/files/usr/tmp/um-network/umnet |
| passt-bionic | `17b81d3c2f40a92b956657a21d6488e17ed291f7451cccdfac7cff56843c4702` | 2709104 | /data/data/com.termux/files/usr/tmp/um-network/passt-bionic |

Guest kernel banner: `Linux version 7.2.0-rc4-g856737c31142 (um@arm64) (Android (dev, +pgo, -bolt, +lto, -mlgo, based on r563880c) clang version 21.0.0 (https://android.googlesource.com/toolchain/llvm-project), LLD 21.0.0) # Thu Jan  1 00:00:00 UTC 1970`

## Gate checks

| check | result |
| --- | --- |
| dhcp | ok |
| addr | ok |
| route | ok |
| dns-config | ok |
| icmp | ok |
| dns-lookup | ok |
| tcp | ok |
| concurrent | ok |
| clean-exit | ok |
| ipv6-addr | ok |
| ipv6-route | ok |
| ipv6-icmp | ok |
| ipv6-dns | ok |
| ipv6-tcp | ok |

## Metrics

| metric | value |
| --- | --- |
| icmp-rtt-ms | {"min": 1.2, "avg": 2.2, "max": 3.8, "n": 4} |
| wget-rates | [] |
| teardown | no passt teardown line |
| clean-shutdown | true |
| gate seconds | 15.688 |

## Guest log excerpt

`2026-10-17-umnet-network-ipv6.log` (sha256 `5ba485b5d03795941f65db8f5a24bd5000a94c3f7eb31d4d3dedf6f11102b946`, 218 lines)

_first lines_

```text
umnet: announcing nameserver 8.8.8.8 (--dns)
umnet: announcing IPv6 nameserver 2001:4860:4860::8888 (--ipv6-dns)
Unable to open /proc/sys/net/ipv4/ip_local_port_range: Permission denied
MAC:
    host: 9a:55:9a:55:9a:55
    NAT to host ::1: fe80::1
NDP/DHCPv6:
    assign: fd00::2
    router: fe80::1
    our link-local: fe80::1
DNS:
    2001:4860:4860::8888
Inbound forwarding rules (HOST):
Unable to open /proc/sys/net/ipv4/ip_local_port_range: Permission denied
MAC:
```

_last lines_

```text
Non-authoritative answer:
Name:	example.com
Address: 2606:4700:10::ac42:93f3
Name:	example.com
Address: 2606:4700:10::6814:179a
NET_IPV6_AAAA=2606:4700:10::ac42:93f3
NET_IPV6_TCP
Connecting to [2606:4700:10::ac42:93f3] ([2606:4700:10::ac42:93f3]:80)
writing to stdout
<!doctype html><html lang=en><head><meta charset=utf-8><link rel=icon href=data:,><meta name=viewport content="width=device-width,initial-scale=1"><title>Example Domain</title><style>html{color-scheme:light dark;background:light-dark(#eee,#222)}body{font:16px/1.6 system-ui,sans-serif;max-width:26em;margin:auto;padding:25vh 2em 2em;text-align:center}</style></head><body><p>This domain is for use in documentation examples without needing permission. This is not a service; avoid relying on it for testing and monitoring purposes.</p><script src=/s.js></script></body></html>
NET_IPV6_TCP_OK=1
NET_DONE
UM_NET_COMMAND_EXIT=0
reboot: Power down

```

## Not verified by this record

- IPv6: the guest is only exercised over IPv4 (DHCP, ICMP, DNS, TCP)
- ext4/COW rootfs: this gate boots hostfs only; see the ext4 gate
- long-run stability and resource leakage: see soak-umnet.sh
- fault injection (link drop, guest panic, kernel kill): see inject-faults.sh
- device coverage: one device and one Android release, as recorded above
- throughput and behaviour under sustained load: not measured by this gate
- upstream Linux UML parity: this gate does not compare against upstream
- release manifest and SHA256SUMS: verified separately by verify-release.py
- CI-side checks (x86_64 build, shell/python syntax, release checker self-test): those run on GitHub runners, not here
