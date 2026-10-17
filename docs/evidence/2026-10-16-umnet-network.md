# Termux device evidence: umnet network gate

| field | value |
| --- | --- |
| verdict | `PASS` |
| exit-code | `0` |
| recorded | `2026-10-16T23:46:15Z` |
| message | `device gate passed` |
| synthetic | no |
| machine record | `2026-10-16-umnet-network.json` |
| gate | `tools/um-arm64/harness/test-umnet-network.sh --workdir /data/data/com.termux/files/usr/tmp/um-network --log net-gate.log --timeout 300 --dns 1.1.1.1` |

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
| branch | harness/evidence-and-soak |
| commit | b7348c9ed1ac4e6f77a239f9229ae58b750855e6 |
| dirty | False |
| dirty-scope | ['tools/um-arm64/harness', 'harness'] |
| repo | /data/data/com.termux/files/usr/var/lib/proot-distro/containers/archlinuxarm/rootfs/home/Raymer/work_space/um/repo/src |

## Components

| component | sha256 | size | path |
| --- | --- | --- | --- |
| linux-bionic | `dd389053e6dae9d75fbead54654b8946379cac220dae5f5ab521b32c8bb948bc` | 88428936 | /data/data/com.termux/files/usr/tmp/um-arm64/new/linux-bionic |
| stub_exe | `731c48d5bf42ef25e26625af6a02627a5d658bf9f67d6f7fc443d6d644d6e52e` | 2000 | /data/data/com.termux/files/usr/tmp/um-arm64/new/stub_exe |
| umnet | `4b32ff453402d522775cb1661d79b634e661476a89ba43d738c3c9498b6397cb` | 2166376 | /data/data/com.termux/files/usr/tmp/um-network/umnet |
| passt-bionic | `289aa0c00e99f6264b63947f2f9742cd247baee3ebf14ff89b5bb01fa9ddd752` | 2708792 | /data/data/com.termux/files/usr/tmp/um-network/passt-bionic |

Guest kernel banner: `Linux version 7.2.0-rc4-g7a2325517b5d-dirty (um@arm64) (Android (dev, +pgo, -bolt, +lto, -mlgo, based on r563880c) clang version 21.0.0 (https://android.googlesource.com/toolchain/llvm-project), LLD 21.0.0) # Thu Jan  1 00:00:00 UTC 1970`

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

## Metrics

| metric | value |
| --- | --- |
| icmp-rtt-ms | {"min": 1.7, "avg": 1.9, "max": 2.2, "n": 2} |
| wget-rates | [] |
| teardown | no passt teardown line |
| clean-shutdown | true |
| gate seconds | 4.852 |

## Guest log excerpt

`2026-10-16-umnet-network.log` (sha256 `630ff9207c5fed6ffb587c4b878939f4587fa85b73ec4698d0b9f161e408785c`, 161 lines)

_first lines_

```text
umnet: announcing nameserver 1.1.1.1 (--dns)
Address space: host top 0x8000000000, stub at 0x7fffff4000, guest TASK_SIZE 0x7fc0000000
Core dump limits :
	soft - 0
	hard - NONE
Stub signal stack: 16384 bytes, host AT_MINSIGSTKSZ 4720
Checking environment variables for a tempdir.../data/data/com.termux/files/usr/tmp
Checking if /data/data/com.termux/files/usr/tmp is on tmpfs...no
Warning: tempdir /data/data/com.termux/files/usr/tmp is not on tmpfs
Unable to open /proc/sys/net/ipv4/ip_local_port_range: Permission denied
Checking PROT_EXEC mmap in /data/data/com.termux/files/usr/tmp...OK
MAC:Checking the stub's cycle counter...
    host: 9a:55:9a:55:9a:55
    NAT to host 127.0.0.1: 10.0.2.2
DHCP:
```

_last lines_

```text
writing to stdout
<!doctype html><html lang=en><head><meta charset=utf-8><link rel=icon href=data:,><meta name=viewport content="width=device-width,initial-scale=1"><title>Example Domain</title><style>html{color-scheme:light dark;background:light-dark(#eee,#222)}body{font:16px/1.6 system-ui,sans-serif;max-width:26em;margin:auto;padding:25vh 2em 2em;text-align:center}</style></head><body><p>This domain is for use in documentation examples without needing permission. This is not a service; avoid relying on it for testing and monitoring purposes.</p><script src=/s.js></script></body></html>
NET_TCP_DONE
NET_CONCURRENT
NET_CONCURRENT_RC=0
NET_LATENCY
time=2.176
time=2.177
time=1.275
time=13.266
time=11.025
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
