# Termux device evidence: umnet network gate

| field | value |
| --- | --- |
| verdict | `PASS` |
| exit-code | `0` |
| recorded | `2026-10-16T23:16:27Z` |
| message | `device gate passed` |
| synthetic | yes (a sample gate, not evidence) |
| machine record | `SAMPLE-synthetic-umnet-network.json` |
| gate | `/tmp/evsample/sample-gate.py --workdir /tmp/evsample/work --log net-gate.log --timeout 300 --dns 1.1.1.1 --mode pass` |

## Host

| field | value |
| --- | --- |
| android-abi | - |
| android-fingerprint | - |
| android-release | - |
| android-sdk | - |
| device-manufacturer | - |
| device-model | - |
| device-probe | getprop unavailable |
| host-role | termux-host |
| kernel-release | 6.17.0-PRoot-Distro |
| machine | aarch64 |
| python | 3.14.8 |
| termux-prefix | /data/data/com.termux/files/usr |
| tracer-pid | 10281 |
| uname | Linux localhost 6.17.0-PRoot-Distro #1 SMP PREEMPT_DYNAMIC Fri, 10 Oct 2025 00:00:00 +0000 aarch64 |

## Repository

| git | value |
| --- | --- |
| branch | harness/evidence-and-soak |
| commit | cf1adce0ed5fac21b6e7a10a48d855b14c5af08f |
| dirty | True |
| dirty-scope | ['tools/um-arm64/harness', 'harness'] |
| repo | /home/Raymer/work_space/um/repo/src |

## Components

| component | sha256 | size | path |
| --- | --- | --- | --- |
| linux-bionic | `ae3647712e4fc46ba2ab8e928aa9aecba1f491f487882a31fd2ba32af098858d` | 52 | /tmp/evsample/assets/linux-bionic |
| stub_exe | `a8076d3d28d21e02012b20eaf7dbf75409a6277134439025f282e368e3305abf` | 10 | /tmp/evsample/assets/stub_exe |
| umnet | `a8076d3d28d21e02012b20eaf7dbf75409a6277134439025f282e368e3305abf` | 10 | /tmp/evsample/work/umnet |
| passt-bionic | `a8076d3d28d21e02012b20eaf7dbf75409a6277134439025f282e368e3305abf` | 10 | /tmp/evsample/work/passt-bionic |

Guest kernel banner: `Linux version 7.2.0-rc4 (sample fixture)`

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
| icmp-rtt-ms | {"min": 1.2, "avg": 1.4, "max": 1.9, "n": 5} |
| wget-rates | ["1.2 MB/s", "900 KB/s"] |
| teardown | umnet: passt exited |
| clean-shutdown | true |
| gate seconds | 0.547 |

## Guest log excerpt

`SAMPLE-synthetic-umnet-network.log` (sha256 `6c90be8ad42181b4115ee24cd886b0c2d3472fcdb23f32325e25fb72b603f028`, 23 lines)

_first lines_

```text
NET_START
1: vec0: <BROADCAST,MULTICAST,UP> mtu 1500
NET_VEC_UP
NET_DHCP
udhcpc: lease of 10.0.2.15 obtained from 10.0.2.2, lease time 86400
    inet 10.0.2.15/24 scope global vec0
    default via 10.0.2.2 dev vec0
nameserver 1.1.1.1
NET_PING_GW
2 packets transmitted, 2 packets received, 0% packet loss
time=1.2 ms
NET_DNS
Name:      example.com
Address:   93.184.216.34
NET_TCP
```

_last lines_

```text
NET_PING_GW
2 packets transmitted, 2 packets received, 0% packet loss
time=1.2 ms
NET_DNS
Name:      example.com
Address:   93.184.216.34
NET_TCP
<!doctype html><html><head><title>Example Domain</title>
NET_TCP_DONE
NET_CONCURRENT
NET_CONCURRENT_RC=0
NET_LATENCY
NET_DONE
umnet: passt exited
UM_NET_COMMAND_EXIT=0
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
- synthetic: produced by /tmp/evsample/sample-gate.py, not by the repository gate tools/um-arm64/harness/test-umnet-network.sh
- running under a tracer (TracerPid=10281); allowed only because this record is synthetic
