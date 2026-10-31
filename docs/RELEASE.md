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
| `passt-bionic` | no | a companion passt, built from upstream `df90211` with `harness/passt-bionic.patch` |

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

## Release checklist

Two kinds of step: what a GitHub runner does alone, and what has to happen on
the phone. A release is ready only when both are done and the device evidence
is archived in this repository.

### Automatic -- CI, on every pull request

1. **`x86_64-build`** -- the reference UML build compiles. It proves the tree
   is buildable, not that the Android binaries work.
2. **`harness-checks`** -- shell and Python syntax, the um-init gates, and
   `verify-release.py self-test` (the release checker proving it can reject
   broken fixtures). When harness tools gain an offline `--self-test`, add it
   here: those self-tests are the only thing that proves the tooling works
   when nobody has a phone.
3. **`android-bionic-build`** (non-blocking) -- `umnet` cross-compiles against
   NDK r29 for API 30 and comes out aarch64 and static. Non-blocking on
   purpose: a 3 GB NDK download must not turn a green tree red for reasons
   unrelated to the change.
4. **`record-evidence.py check`** (not wired into CI yet) -- every committed
   record under `docs/evidence/` validates; `--allow-synthetic` applies to the
   sample file only. Add it to `harness-checks` now that the evidence
   directory exists.

CI cannot do steps 5-9: they need a phone.

### Human, on the Termux host -- the part CI cannot do

5. **Rebuild the assets from the commit being released** (`linux-bionic`,
   `stub_exe`, `umnet`, and the companion `passt-bionic`). The manifest hashes
   come from these binaries, so they must be built from the released commit,
   not from a working tree with local changes.
6. **Run the network gate and archive the run**:

   ```sh
   tools/um-arm64/harness/record-evidence.sh record \
       --workdir "$PREFIX/tmp/um-network" --out docs/evidence
   ```

   Commit the record (JSON + markdown + guest log). A `SKIP` record documents
   an attempt and is worth committing, but it is **not** release evidence: a
   release claiming network support needs a `PASS` record for those binaries.
7. **Validate the record** before trusting or citing it:

   ```sh
   tools/um-arm64/harness/record-evidence.sh check docs/evidence/<file>.json --verify-hashes
   ```

   `--verify-hashes` fails if a component no longer matches the binary the
   record was made from.
8. **Soak and fault injection** -- recommended whenever the bridge, the kernel
   arguments or the teardown path changed. The three fault scenarios state
   required criteria and judge on the device; a run that cannot reach its
   injection point says so instead of passing quietly:

   ```sh
   # repetition: short smoke by default, --minutes for an hour-scale run
   tools/um-arm64/harness/soak-umnet.sh --rounds 20
   tools/um-arm64/harness/soak-umnet.sh --minutes 180 --rounds 5000

   tools/um-arm64/harness/inject-faults.sh --scenario link-drop
   tools/um-arm64/harness/inject-faults.sh --scenario kernel-kill
   tools/um-arm64/harness/inject-faults.sh --scenario guest-panic
   ```

   Records land in `docs/evidence/soak/` and `docs/evidence/faults/`; the
   latter's README names the current reference set. Do not run a soak and a
   fault scenario at the same time -- the leak criteria cannot tell them apart.
9. **State the device matrix and the gaps** in the release notes: the device,
   Android release and host kernel the evidence came from, the record's
   `not-verified` list, and anything the release does not cover.

### Release assembly

10. **Write `MANIFEST` and `SHA256SUMS`** from the asset directory (command
    above), and cite the evidence record in the release notes.
11. **Verify the manifest** with `verify-release.py check --tag <tag>` (or
    `--dir <dir> --require-all` before publishing).
12. **Tag, publish, then verify the published release once more** with
    `verify-release.py check --tag <tag>`: the published bytes are the ones a
    downloader receives.

### What blocks a release

| condition | verdict | release? |
| --- | --- | --- |
| CI red on the merge commit | `FAIL` | no |
| device gate failed after the last change to runtime behaviour | `FAIL` | no |
| device gate skipped (no phone or no assets) and runtime behaviour changed | `SKIP` | only with the gap written into the release notes |
| no device evidence at all for a release that claims device support | -- | no |
| documentation-only or repository-settings change | -- | no tag and no Release |

## ext4 gate

`ext4-gate` (and the per-asset `gate` field) must distinguish a real pass from
an environment that could not run the test:

* `pass` -- the ext4 path was exercised and worked;
* `fail` -- it was exercised and did not;
* `skip` -- the environment could not run it (no Termux host, no kernel).

Recording `skip` as `pass` would make the release claim coverage it never had,
which is worse than admitting the gap. The harness prints `verdict=SKIP` and
exits 77 in that case.

The `make` command accepts `pass`, `fail`, `skip` and `optional` (plus the
legacy `required`). `check` enforces the distinction: a release that records
`ext4-gate=fail` is refused, and a release that records `ext4-gate=pass` must
also record `asset.linux-bionic.gate=pass`. A manifest cannot claim the gate
passed while the asset it describes says the gate never ran.

`check --tag` restores the executable bit on downloaded assets before checking
them: GitHub serves asset bytes without file modes, so without that step no
published release could ever verify.
