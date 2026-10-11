# Release Policy

Downstream release tags must not reuse Linux kernel version tags. Use a name
that identifies this project and the downstream line, for example:

```text
um-arm64-v0.1.0
```

Each release records:

- downstream Git commit and branch;
- official UML upstream baseline and selected patch list;
- NDK, Android API and compiler versions;
- host kernel and device test matrix;
- rootfs name and checksum;
- `linux-bionic`, `stub_exe`, configuration and SHA256SUMS assets.

Documentation-only and repository-settings changes merge without a tag or
Release. A tag and Release are reserved for source, runtime behavior or
published artifacts that changed and passed the release gates.

## Assets

A release of this fork is not "the kernel tarball". It is a small set of
Android-runnable binaries whose value is that they were built **together**, from
one commit, with one NDK, against one Android API level:

| asset | required | what it is |
| --- | --- | --- |
| `linux-bionic` | yes | the UML kernel, static bionic, aarch64 |
| `stub_exe` | yes | the userspace stub the kernel execs for every guest |
| `umnet` | yes | the vector-fd bridge between UML and passt |
| `passt-bionic` | no | a companion passt, shipped when it was built alongside |

Every asset is checked for the two properties that actually matter on Android:
**aarch64** (an x86_64 binary under an arm64 tag is useless) and **static**
(Android has no `/lib/ld-linux-aarch64.so.1`, so a dynamic UML dies before
`main` with no output at all).

## Manifest and checksums

Each release carries two files next to the assets:

```text
SHA256SUMS   plain sha256sum output, one line per asset
MANIFEST     the key=value record below
```

`MANIFEST` is `key=value`, `#` starts a comment, and asset keys are
`asset.<name>.<field>`:

```text
manifest-format=1
project=Raymer8639/linux-um-arm64
tag=um-arm64-v0.1.0
commit=<sha>
branch=um-arm64
upstream-baseline=<sha>
kernel-version=7.2.0-rc4
linux-floor=7.2.0
ndk=r29
android-api=30
ext4-gate=pass|fail|skip|optional
released=<ISO 8601>
asset.linux-bionic.sha256=<sha256>
asset.linux-bionic.size=<bytes>
asset.linux-bionic.kind=elf-exec
asset.linux-bionic.gate=pass
```

`linux-floor` is the oldest kernel version the assets may contain. UML embeds
`linux_banner` verbatim, so the checker reads it straight out of the binary and
refuses a release whose kernel is older than the manifest claims -- something a
downloader cannot catch any other way.

## Publishing and verifying

```sh
# write MANIFEST and SHA256SUMS for a directory of built assets
tools/um-arm64/harness/verify-release.py make --dir <dir> \
  --tag um-arm64-v0.1.0 --commit <sha> --branch um-arm64 \
  --upstream <sha> --kernel-version 7.2.0-rc4 --kernel-floor 7.2.0 \
  --ndk r29 --api 30 --ext4-gate pass

# verify a published release, or a local directory
tools/um-arm64/harness/verify-release.py check --tag um-arm64-v0.1.0
tools/um-arm64/harness/verify-release.py check --dir <dir> --require-all

# prove the checker rejects broken releases (what CI runs)
tools/um-arm64/harness/verify-release.py self-test
```

`make` validates what it publishes, so a manifest cannot describe assets that
contradict their own claims. `check` re-hashes every file rather than trusting
`SHA256SUMS`, and rejects a `linux-bionic` that is not a kernel.

### Verdicts

Exit codes follow `harness/boot.py`:

| verdict | exit | meaning |
| --- | --- | --- |
| `PASS` | 0 | verified |
| `FAIL` | 1 | the release is wrong |
| `SKIP` | 77 | verification could not be *attempted* (no network, no token, not published yet) |

`SKIP` never means "looked wrong but we did not feel like failing". Anything the
release itself gets wrong is `FAIL`.

## ext4 gate

`ext4-gate` (and the per-asset `gate` field) must distinguish a real pass from
an environment that could not run the test:

* `pass` -- the ext4 path was exercised and worked;
* `fail` -- it was exercised and did not;
* `skip` -- the environment could not run it (no Termux host, no kernel).

Recording `skip` as `pass` would make the release claim coverage it never had,
which is worse than admitting the gap. The harness prints `verdict=SKIP` and
exits 77 in that case.
