# ARM64 Android/Termux harness

These scripts provide the maintained reproducible bionic UML build path.

## Build

```sh
NDK=$HOME/android-sdk/ndk/29.0.14206865 \
  OUT=$PWD/out/android-arm64 \
  JOBS=4 \
  harness/build-bionic-termux.sh
```

Build on a glibc Linux host with gcc/g++, make, bison, flex and bc. NDK may be
the NDK root or its LLVM prebuilt directory. Output artifacts are under
`out/android-arm64/artifacts`. `stub_exe` is generated from existing kernel
source, not checked in as a binary. Use a dedicated OUT directory: this script
resets defconfig.

## Rootfs

On Termux, install bash, curl, tar, coreutils, python and e2fsprogs.
The script verifies the Alpine SHA256 and refuses to overwrite existing roots.
It installs `um-init` and a default `/um-command.sh` which prints uname.
Edit the command script in the directory root to test other commands.

`um-init` supports two modes. Without `neoproot_protocol=session-v1`, it keeps
the legacy `/um-command.sh` boot flow. Neoproot session mode is selected with
the UML command-line parameters `neoproot_protocol=session-v1`,
`neoproot_session=/SESSION`, and `neoproot_cwd=/GUEST/PATH`; `um-init` reads
these after mounting `/proc`, `/sys`, and `/dev`, mounts the hostfs session at
`/run/neoproot`, runs its `init.sh`, and writes the command status there.
The UML `hostfs=/HOST/RUNTIME` parameter is consumed by the kernel as the
hostfs root and is intentionally not expected in `/proc/cmdline`.

```sh
harness/rootfs-alpine-termux.sh $PREFIX/tmp/um-arm64/rootfs
```

## init protocol 2: binds

`um-init` implements neoproot's second init protocol, which lets an **ext4**
guest receive `-b HOST:GUEST[:ro]` binds. The backend cannot perform those
mounts itself: an ext4 guest has its own root and the only host directory it
sees is the hostfs session at `/run/neoproot`. Before `init.sh` runs,
`um-init` therefore writes `/run/neoproot/init-features`:

```
neoproot-init-protocol=2
feature=binds
```

That declaration is what the generated `init.sh` checks when the run asked for
binds. Without it the wrapper prints `UM_PROTOCOL_ERROR=binds-unsupported`
and exits 125 rather than run the command with its binds silently missing. The
write is best effort, and a session without a bind list never reads the file.

When `/run/neoproot/binds` exists, each line is one bind, tab-separated:

```
source<TAB>target<TAB>dir|file<TAB>ro|rw<TAB>name
```

`source` is a hostfs mount option (a path relative to the kernel's
`hostfs=` root), `target` an absolute guest path, and `name` the basename of
a file bind (`-` for a directory bind). Lines starting with `#` are ignored.
A directory bind is `mount -t hostfs -o SOURCE none TARGET`. A file bind
mounts the record's source -- the file's parent directory, because hostfs
cannot mount a file as a root -- on scratch space under `/run`, then binds
`SCRATCH/NAME` onto `TARGET`. `ro` remounts a directory bind read-only; for
a file bind the scratch mount is remounted read-only before the file is bound,
so the bind inherits it.

A bind that cannot be mounted writes `/run/neoproot/protocol-error` (for
example `UM_BIND_ERROR=/guest/path:mount-failed`), writes status `125`,
prints `UM_SESSION_ERROR=125`, and powers off. The command is not run.

A session without `/run/neoproot/binds` skips the bind code entirely, so the
legacy `/um-command.sh` flow and ordinary session runs behave as before.

The protocol is needed only for ext4 roots with binds: a directory root is
bound by the wrapper instead. The image must carry this `um-init`; an image
with an older init fails the run by design. The supervisor side is described in
neoproot's `README.md` (Binds) and reference `tests/um-init-ext4.sh`.

## Boot

Run from the Termux host, not inside a neoproot container:

```sh
python3 harness/boot.py \
  out/android-arm64/artifacts/linux-bionic \
  out/android-arm64/artifacts/stub_exe \
  $PREFIX/tmp/um-arm64/rootfs/alpine.ext4
```

Android refuses embedded memfd execution for the UML stub, so `stub_exe` is required.

Add `--hostfs` and pass the generated `root` directory to boot a directory root.
The init wrapper mounts proc/sys/dev, executes `/um-command.sh`, reports its
exit code and powers off. The gate requires the exact `UMARM_BOOT_OK` line,
clean shutdown and no BUG/WARNING/panic. It refuses traced containers and
kills only its own process group on timeout (default 120 seconds).
Hostfs exposes writable host storage; do not treat it as an untrusted-guest
security boundary.
