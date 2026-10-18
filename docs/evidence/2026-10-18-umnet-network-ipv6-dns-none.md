# Termux device evidence: umnet network gate

| field | value |
| --- | --- |
| verdict | `PASS` |
| exit-code | `0` |
| recorded | `2026-10-18T07:15:58Z` |
| message | `device gate passed` |
| synthetic | no |
| machine record | `2026-10-18-umnet-network-ipv6-dns-none.json` |
| gate | `tools/um-arm64/harness/test-umnet-network.sh --workdir /data/data/com.termux/files/usr/tmp/um-network --log net-gate.log --timeout 300 --dns 1.1.1.1 --ipv6 --ipv6-dns=none` |

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
| branch | um-arm64 |
| commit | 3c727c4583058546dc3ac20d769ded1050170fc6 |
| dirty | False |
| dirty-scope | ['tools/um-arm64/harness', 'harness'] |
| repo | /data/data/com.termux/files/usr/var/lib/proot-distro/containers/archlinuxarm/rootfs/home/Raymer/work_space/um/repo-clone |

## Components

| component | sha256 | size | path |
| --- | --- | --- | --- |
| linux-bionic | `b778f4f7c889b258278df8e4d542fc0244a32424bb30fe69067ca66e45ceb9f9` | 94860472 | /data/data/com.termux/files/usr/tmp/um-arm64/new/linux-bionic |
| stub_exe | `731c48d5bf42ef25e26625af6a02627a5d658bf9f67d6f7fc443d6d644d6e52e` | 2000 | /data/data/com.termux/files/usr/tmp/um-arm64/new/stub_exe |
| umnet | `7683b4f8cba8cd403b889d8073cc47f390c0de699260d7ecb0f40f3479ce5ebb` | 2172944 | /data/data/com.termux/files/usr/tmp/um-network/umnet |
| passt-bionic | `502d980c7c4a582b59a44d2e1bda578f4a67a9b2926c7cb05648cdaef5065a06` | 2709336 | /data/data/com.termux/files/usr/tmp/um-network/passt-bionic |

Guest kernel banner: `Linux version 7.2.0-rc4-g856737c31142 (um@arm64) (Android (dev, +pgo, -bolt, +lto, -mlgo, based on r563880c) clang version 21.0.0 (https://android.googlesource.com/toolchain/llvm-project), LLD 21.0.0) # Thu Jan  1 00:00:00 UTC 1970`

## Gate checks

| check | result |
| --- | --- |
| dhcp | ok |
| addr | ok |
| route | ok |
| icmp | ok |
| clean-exit | ok |
| dns-config | ok |
| dns-lookup | ok |
| tcp | ok |
| concurrent | ok |
| ipv6-addr | ok |
| ipv6-route | ok |
| ipv6-icmp | ok |
| helper-dns | ok |

## Metrics

| metric | value |
| --- | --- |
| icmp-rtt-ms | {"min": 1.7, "avg": 2.2, "max": 2.6, "n": 4} |
| wget-rates | [] |
| teardown | no passt teardown line |
| clean-shutdown | true |
| gate seconds | 41.595 |

## Guest log excerpt

`2026-10-18-umnet-network-ipv6-dns-none.log` (sha256 `37ad7e70f7df472d84ff44bd87da4018d870f2428d330e32cde53736800da810`, 216 lines)

_first lines_

```text
umnet: announcing nameserver 1.1.1.1 (--dns)
umnet: announcing no IPv6 nameserver (--ipv6-dns none)
Unable to open /proc/sys/net/ipv4/ip_local_port_rangeUnable to open /proc/sys/net/ipv4/ip_local_port_range: Permission denied: Permission denied

MAC:
    host: 9a:55:9a:55:9a:55
    NAT to host ::1: fe80::1MAC:
    host: 9a:55:9a:55:9a:55
    NAT to host 127.0.0.1: 10.0.2.2
DHCP:
    assign: 10.0.2.15
    mask: 255.255.255.0
    router: 10.0.2.2
DNS:
    1.1.1.1
```

_last lines_

```text
nslookup: bad address 'none'
nslookup: bad address 'none'
nslookup: bad address 'none'
nslookup: bad address 'none'
nslookup: bad address 'none'
nslookup: bad address 'none'
NET_IPV6_AAAA=
NET_IPV6_TCP



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
