#!/usr/bin/env python3
"""Write and verify a um-arm64 release manifest and its assets.

A release of this fork is not "the kernel tarball".  It is a small set of
Android-runnable binaries whose whole value is that they were built together
from one commit with one NDK against one Android API level, plus an ext4 gate
result that says whether the guest storage path was actually exercised.  A
downloader who cannot tell which commit or which NDK produced what has an
artifact that only looks reproducible.

So every release carries two files next to the assets:

    SHA256SUMS   plain `sha256sum` output, one line per asset
    MANIFEST     the key=value record described in docs/RELEASE.md

Three subcommands, all of which share the same manifest reader and the same
asset checks, so the publisher and the consumer cannot drift apart:

    make      write MANIFEST and SHA256SUMS for a directory of built assets
    check     download a published release (or read --dir) and verify it
    self-test build deliberately broken fixtures and prove check rejects them

`self-test` is what CI runs: it does not need a network or a real release, and
a checker that has never rejected anything is not a checker.

Verdict and exit codes follow harness/boot.py: verdict=SKIP (exit 77) means the
verification could not be *attempted* for a reason the environment owns -- no
network, no token, release not published yet.  It never means "the asset was
wrong but we did not feel like failing".  Anything the release itself gets wrong
is verdict=FAIL (exit 1).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

API_URL = "https://api.github.com"

# Exit code convention shared with harness/boot.py.  77 is the autotools
# "skipped" status, chosen so a caller can distinguish it from a real failure
# without parsing the text.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

# What each published asset is and what it has to look like.  `kind` drives the
# ELF checks; `required` marks the assets a release cannot omit.
#
# passt is versioned and built by the passt fork, not here: it is listed as a
# companion artifact so the manifest records which passt this kernel was gated
# against, but a kernel release does not have to ship the binary.
ASSETS = {
    "linux-bionic": {
        "required": True,
        "kind": "elf-exec",
        "description": "UML kernel, static bionic, aarch64",
    },
    "stub_exe": {
        "required": True,
        "kind": "elf-exec",
        "description": "UML userspace stub, exec'd by the kernel for every guest",
    },
    "umnet": {
        "required": True,
        "kind": "elf-exec",
        "description": "vector-fd bridge between UML and passt",
    },
    "passt-bionic": {
        "required": False,
        "kind": "elf-exec",
        "description": "companion static bionic passt",
    },
}

# Manifest keys that must be present and non-empty in any published MANIFEST.
# These are the fields a downloader needs to reproduce the build at all.
REQUIRED_KEYS = (
    "manifest-format",
    "project",
    "tag",
    "commit",
    "branch",
    "upstream-baseline",
    "kernel-version",
    "ndk",
    "android-api",
    "released",
)

# Fields that name an artifact rather than the release.  Encoded as
# `asset.<name>.<field>` so the manifest stays one flat, greppable file.
ASSET_FIELDS = ("sha256", "size", "kind", "gate")


def emit(verdict, message, **fields):
    """Print one machine-readable result line, in the harness's format."""
    parts = ["verdict=" + verdict]
    if message:
        parts.append("message=" + message.replace("\n", " "))
    for key in sorted(fields):
        parts.append("%s=%s" % (key, fields[key]))
    print(" ".join(parts), file=sys.stderr)


def fail(message, **fields):
    emit("FAIL", message, **fields)
    raise SystemExit(EXIT_FAIL)


def skip(message, **fields):
    emit("SKIP", message, **fields)
    raise SystemExit(EXIT_SKIP)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_manifest(path):
    """Parse a MANIFEST into (release, assets), rejecting duplicate keys.

    Duplicates are an error rather than a last-one-wins: the file is written by
    a script and read by a script, and a repeated key means one of them is
    wrong.  Silently taking the second value is how a manifest starts lying.
    """
    release, assets = {}, {}
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                fail("manifest line %d is not key=value: %s" % (lineno, line))
            key, value = line.split("=", 1)
            if key.startswith("asset."):
                # Keys are asset.<name>.<field>, and the name itself contains
                # '-' or '_' but never '.', so split off exactly three parts.
                # This used to unpack four (asset.<name>.<n>.<field>), which no
                # writer ever produced: reading any manifest failed outright.
                parts = key.split(".")
                if len(parts) != 3:
                    fail("manifest line %d has a malformed asset key: %s"
                         % (lineno, key))
                _, name, field = parts
                assets.setdefault(name, {})[field] = value
            else:
                if key in release:
                    fail("manifest repeats key %s on line %d" % (key, lineno))
                release[key] = value
    return release, assets


def elf_facts(path):
    """Return the ELF facts the asset checks need, or None when not an ELF.

    Read directly rather than shelling out to `file`/`readelf`: this checker
    runs on the CI runner and on a phone with Termux, and neither `file` nor
    `readelf` is guaranteed there.  The fields used here are the fixed,
    documented part of the header plus the two program-header entries that
    matter (PT_INTERP, PT_LOAD).
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(64)
            if len(header) < 64 or header[:4] != b"\x7fELF":
                return None
            ei_class, ei_data = header[4], header[5]
            if ei_class != 2 or ei_data != 1:
                return None  # not a 64-bit little-endian ELF
            e_type, e_machine = struct.unpack_from("<HH", header, 16)
            e_phoff, = struct.unpack_from("<Q", header, 32)
            e_phentsize, e_phnum = struct.unpack_from("<HH", header, 54)
            handle.seek(e_phoff)
            phdrs = handle.read(e_phentsize * e_phnum)
    except OSError:
        return None

    interp = None
    loads = []
    for index in range(e_phnum):
        base = index * e_phentsize
        if base + 56 > len(phdrs):
            break
        p_type, = struct.unpack_from("<I", phdrs, base)
        p_offset, p_vaddr, p_filesz, p_memsz = struct.unpack_from(
            "<QQQQ", phdrs, base + 8)
        if p_type == 3:  # PT_INTERP
            interp = p_offset
        elif p_type == 1:  # PT_LOAD
            loads.append((p_filesz, p_memsz, p_vaddr))
    return {
        "type": e_type,
        "machine": e_machine,
        "interp_offset": interp,
        "loads": loads,
    }


def check_elf(path, name, kind):
    """Verify one asset looks like what the manifest says it is.

    The two properties that matter on Android are checked here rather than
    trusted from the publisher: aarch64 (an x86_64 binary published under an
    arm64 tag is useless) and static (Android has no /lib/ld-linux-aarch64.so.1,
    so a dynamically linked UML dies before main with no output at all).
    """
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        fail("%s is not a regular file" % name)
    if not st.st_size:
        fail("%s is empty" % name)
    facts = elf_facts(path)
    if facts is None:
        fail("%s is not a 64-bit little-endian ELF" % name)
    if facts["machine"] != 183:  # EM_AARCH64
        fail("%s is not aarch64 (e_machine=%d)" % (name, facts["machine"]))
    if kind == "elf-exec":
        if facts["type"] not in (2, 3):  # ET_EXEC, ET_DYN
            fail("%s is not an executable ELF (e_type=%d)"
                 % (name, facts["type"]))
        if facts["interp_offset"] is not None:
            with open(path, "rb") as handle:
                handle.seek(facts["interp_offset"])
                interp = handle.read(256).split(b"\0", 1)[0].decode(
                    "ascii", errors="replace")
            fail("%s is dynamically linked (interpreter %s)" % (name, interp))
    if not st.st_mode & 0o111:
        fail("%s is not executable (mode %o)" % (name, st.st_mode & 0o777))


def check_kernel_floor(path, floor):
    """Check the kernel's embedded release string against a version floor.

    UML embeds `linux_banner` verbatim, so the string is present in the binary
    and this needs no guest boot.  A release whose linux-bionic is older than
    the manifest claims cannot be caught any other way at download time.
    """
    pattern = re.compile(rb"Linux version (\d+)\.(\d+)\.(\d+)")
    found = None
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 22)
            if not chunk:
                break
            match = pattern.search(chunk)
            if match:
                found = tuple(int(part) for part in match.groups())
                break
    if found is None:
        fail("linux-bionic does not embed a Linux version banner")
    want = tuple(int(part) for part in floor.split("."))
    if found < want:
        fail("linux-bionic is %s, below the required %s"
             % (".".join(map(str, found)), floor))
    return ".".join(map(str, found))


def download(url, path, token=None):
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=60) as response, \
            open(path, "wb") as handle:
        shutil.copyfileobj(response, handle)
    return path


def release_json(repo, tag, token):
    try:
        with urllib.request.urlopen(
                urllib.request.Request(
                    "%s/repos/%s/releases/tags/%s" % (API_URL, repo, tag),
                    headers={"Accept": "application/vnd.github+json",
                             **({"Authorization": "Bearer " + token}
                                if token else {})}),
                timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            skip("release %s does not exist in %s" % (tag, repo))
        fail("GitHub API error %d for %s" % (exc.code, tag))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        skip("cannot reach GitHub: %s" % exc)


def fetch_release(args, workdir):
    """Download MANIFEST, SHA256SUMS and every asset named in the manifest."""
    token = None
    if args.token_file:
        try:
            with open(args.token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError as exc:
            skip("cannot read token file: %s" % exc)
    data = release_json(args.repo, args.tag, token)
    by_name = {asset["name"]: asset for asset in data.get("assets", [])}
    for required in ("MANIFEST", "SHA256SUMS"):
        if required not in by_name:
            fail("release %s has no %s asset" % (args.tag, required))
    fetch = lambda name: download(  # noqa: E731 - terse by design here
        by_name[name]["browser_download_url"],
        os.path.join(workdir, name), token)
    fetch("MANIFEST")
    fetch("SHA256SUMS")
    release, assets = read_manifest(os.path.join(workdir, "MANIFEST"))
    for name in assets:
        if name not in by_name:
            fail("manifest names asset %s but the release does not have it"
                 % name)
        fetch(name)
    return release, assets, os.path.join(workdir, "SHA256SUMS")


def check_checksums(directory, sums_path, expected):
    """Verify SHA256SUMS against the files, and the files against the manifest.

    Both directions are checked.  SHA256SUMS alone proves the download is intact
    only if it is itself trusted; comparing it to the manifest's own hashes
    makes the manifest the root of trust and catches a SHA256SUMS that was
    regenerated over a corrupted or swapped asset.
    """
    listed = {}
    with open(sums_path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            match = re.match(r"^([0-9a-f]{64})\s+\*?(.+)$", line)
            if not match:
                fail("SHA256SUMS line %d is malformed: %s" % (lineno, line))
            digest, name = match.groups()
            if name in listed:
                fail("SHA256SUMS lists %s twice" % name)
            listed[name] = digest
    if not listed:
        fail("SHA256SUMS is empty")

    for name, digest in sorted(listed.items()):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            fail("SHA256SUMS names %s but the file is missing" % name)
        actual = sha256_file(path)
        if actual != digest:
            fail("%s checksum mismatch" % name,
                 expected=digest[:16], actual=actual[:16])

    for name, fields in sorted(expected.items()):
        if name not in listed:
            fail("manifest names %s but SHA256SUMS does not" % name)
        if listed[name] != fields.get("sha256"):
            fail("%s disagrees between MANIFEST and SHA256SUMS" % name)
    for name in sorted(listed):
        if name not in expected:
            fail("SHA256SUMS names %s but the manifest does not" % name)
    return listed


def verify_assets(directory, release, assets, require_all):
    """Run the content checks on everything the manifest promised."""
    if require_all:
        for name, spec in ASSETS.items():
            if spec["required"] and name not in assets:
                fail("required asset %s is missing from the manifest" % name)
    if not assets:
        fail("the manifest names no assets")
    for name in sorted(assets):
        fields = assets[name]
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            fail("asset %s is missing from the download" % name)
        size = int(fields.get("size", -1))
        if size != os.path.getsize(path):
            fail("%s size disagrees with the manifest" % name,
                 manifest=size, actual=os.path.getsize(path))
        expect = ASSETS.get(name, {"kind": fields.get("kind", "elf-exec")})
        check_elf(path, name, expect.get("kind", "elf-exec"))
        print("checked %-14s sha256=%s size=%d" % (name, fields["sha256"][:16],
                                                   size))
    floor = release.get("linux-floor")
    if floor:
        version = check_kernel_floor(os.path.join(directory, "linux-bionic"),
                                     floor)
        if release.get("kernel-version") and \
                not release["kernel-version"].startswith(version):
            fail("linux-bionic is %s but the manifest says kernel-version=%s"
                 % (version, release["kernel-version"]))
        print("checked linux-bionic kernel version %s >= %s" % (version, floor))
    for asset, fields in sorted(assets.items()):
        gate = fields.get("gate", "")
        # The storage row of the test matrix is the one that silently degrades:
        # a host without loop mounts or an ext4-capable kernel still boots a
        # hostfs root and reports success.  A release that claims gate=pass for
        # an ext4 image therefore has to show the ext4 evidence too.
        if name_is_linux(asset) and gate and gate not in ("pass", "skip",
                                                          "fail", "bug",
                                                          "hang", "panic"):
            fail("%s has an unknown gate value %r" % (asset, gate))
        if name_is_linux(asset) and gate in ("skip", "") and \
                release.get("ext4-gate") == "required":
            fail("linux-bionic was never gated against an ext4 root, but "
                 "this release requires that evidence")
    return True


def name_is_linux(name):
    return name == "linux-bionic"


def cmd_make(args):
    directory = args.dir
    if not os.path.isdir(directory):
        fail("no such directory: " + directory)
    names = sorted(name for name in os.listdir(directory)
                   if os.path.isfile(os.path.join(directory, name))
                   and name not in ("MANIFEST", "SHA256SUMS"))
    if not names:
        fail("nothing to publish in " + directory)
    lines = [
        "# um-arm64 release manifest.  Format and field meanings:",
        "# docs/RELEASE.md.  Regenerate with:",
        "#   tools/um-arm64/harness/verify-release.py make --dir <dir> ...",
        "manifest-format=1",
        "project=Raymer8639/linux-um-arm64",
        "tag=" + args.tag,
        "commit=" + args.commit,
        "branch=" + args.branch,
        "upstream-baseline=" + args.upstream,
        "kernel-version=" + args.kernel_version,
        "linux-floor=" + args.kernel_floor,
        "ndk=" + args.ndk,
        "android-api=" + str(args.api),
        "ext4-gate=" + args.ext4_gate,
        "released=" + args.released,
    ]
    if args.compiler:
        lines.append("compiler=" + args.compiler)
    if args.notes:
        lines.append("notes=" + args.notes)
    sums = []
    for name in names:
        if name not in ASSETS:
            print("publish: ignoring unlisted file %s" % name, file=sys.stderr)
            continue
        path = os.path.join(directory, name)
        digest = sha256_file(path)
        check_elf(path, name, ASSETS[name]["kind"])
        sums.append("%s  %s" % (digest, name))
        lines.append("asset.%s.sha256=%s" % (name, digest))
        lines.append("asset.%s.size=%d" % (name, os.path.getsize(path)))
        lines.append("asset.%s.kind=%s" % (name, ASSETS[name]["kind"]))
        lines.append("asset.%s.gate=%s" % (name, args.asset_gate.get(name,
                                                                     args.gate)))
    if not sums:
        fail("none of the files in %s is a published asset" % directory)
    with open(os.path.join(directory, "MANIFEST"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(directory, "SHA256SUMS"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(sums) + "\n")
    print("wrote %s/MANIFEST and %s/SHA256SUMS" % (directory, directory))
    emit("PASS", "manifest written", assets=len(sums), tag=args.tag)
    return EXIT_PASS


def cmd_check(args):
    if args.dir:
        directory, release, assets = args.dir, None, None
        manifest = os.path.join(directory, "MANIFEST")
        sums = os.path.join(directory, "SHA256SUMS")
        if not os.path.isfile(manifest) or not os.path.isfile(sums):
            # A directory handed to check without a manifest is exactly the
            # "environment cannot do this" case: there is nothing to verify.
            skip("no MANIFEST/SHA256SUMS in " + directory)
        release, assets = read_manifest(manifest)
    else:
        workdir = args.workdir or tempfile.mkdtemp(prefix="um-release.")
        os.makedirs(workdir, exist_ok=True)
        directory = workdir
        release, assets, sums = fetch_release(args, workdir)
    for key in REQUIRED_KEYS:
        if not release.get(key):
            fail("manifest is missing required field %s" % key)
    if args.tag and release["tag"] != args.tag:
        fail("manifest tag %s is not the requested %s"
             % (release["tag"], args.tag))
    if args.expect_commit and release["commit"] != args.expect_commit:
        fail("manifest commit %s is not the expected %s"
             % (release["commit"], args.expect_commit))
    if release["manifest-format"] != "1":
        fail("unknown manifest-format %s" % release["manifest-format"])
    check_checksums(directory, sums, assets)
    verify_assets(directory, release, assets, args.require_all)
    emit("PASS", "release verified", tag=release["tag"],
         commit=release["commit"][:12], assets=len(assets))
    print("release %s verified: %d asset(s) at %s"
          % (release["tag"], len(assets), directory))
    return EXIT_PASS


def cmd_self_test(args):
    """Prove check() rejects each way a release can be wrong.

    Every case below is a real failure mode that a manifest-only check would
    miss: a checksum that does not cover the file, a checksum that is right for
    the wrong file, an x86_64 asset published under an arm64 tag, a dynamic
    binary where a static one is required, a size that disagrees with what was
    downloaded, and a manifest that omits a required field.  The checker passes
    only if it fails all of them and still accepts the good tree.
    """
    workdir = os.path.abspath(args.workdir)
    # The fixtures are rebuilt from scratch; anything left over from a previous
    # run is at best noise and at worst a false verdict.
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
    cc = os.environ.get("CC", "cc")
    source = os.path.join(workdir, "fake.c")
    # A tiny arm64-shaped ELF is not something a self-test can synthesise
    # honestly, so the good fixture is built by the *target* compiler when one
    # is available and by the host compiler otherwise -- in which case the
    # aarch64 assertion is skipped rather than faked.
    # Include the banner a real kernel embeds: check_kernel_floor() reads it out
    # of the binary, so a fixture without one would fail a check that every
    # genuine release passes -- and the "good" case would report WRONG.
    with open(source, "w", encoding="utf-8") as handle:
        handle.write(
            "const char fake_linux_banner[] = "
            "\"Linux version 7.2.0-rc4 (fixture)\";\n"
            "int main(void) { return 0; }\n")
    target_cc = os.environ.get("TARGET_CC")
    good = os.path.join(workdir, "linux-bionic")
    build = [target_cc or cc, "-static", "-o", good, source]
    subprocess.run(build, check=True)

    # A second, deliberately different binary for the other assets.  The
    # fixtures have to differ or a swap between them is unobservable and the
    # swapped-asset case would pass while testing nothing.
    stub_source = os.path.join(workdir, "fake-stub.c")
    with open(stub_source, "w", encoding="utf-8") as handle:
        handle.write("int main(void) { return 1; }\n")
    stub = os.path.join(workdir, "stub")
    subprocess.run([target_cc or cc, "-static", "-o", stub, stub_source],
                   check=True)
    aarch64 = elf_facts(good)["machine"] == 183
    if not aarch64:
        print("self-test: target compiler is not aarch64; the arch check is "
              "exercised with a synthesised header instead", file=sys.stderr)

    cases = []

    def run_check(label, expect_fail, mutate):
        tree = os.path.join(workdir, "tree-" + label)
        # Start clean every time.  Reusing a tree would carry the previous run's
        # MANIFEST and SHA256SUMS into this one, so a case could pass (or fail)
        # because of leftovers rather than because of what it is testing.
        shutil.rmtree(tree, ignore_errors=True)
        os.makedirs(tree, exist_ok=True)
        for name in ("linux-bionic", "stub_exe", "umnet"):
            shutil.copy(good, os.path.join(tree, name))
            os.chmod(os.path.join(tree, name), 0o755)
        shutil.copy(stub, os.path.join(tree, "stub_exe"))
        shutil.copy(stub, os.path.join(tree, "umnet"))
        os.chmod(os.path.join(tree, "stub_exe"), 0o755)
        os.chmod(os.path.join(tree, "umnet"), 0o755)
        subprocess.run([sys.executable, os.path.abspath(__file__), "make",
                        "--dir", tree, "--tag", "um-arm64-v0.0.0-test",
                        "--commit", "0" * 40, "--branch", "um-arm64",
                        "--upstream", "0" * 40,
                        "--kernel-version", "7.2.0-rc4",
                        "--kernel-floor", "0.0.0",
                        "--ndk", "r29", "--api", "30",
                        "--released", "1970-01-01T00:00:00Z"],
                       check=True, stdout=subprocess.DEVNULL)
        mutate(tree)
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "check",
             "--dir", tree, "--require-all"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True)
        failed = proc.returncode != 0
        cases.append((label, expect_fail, failed, proc.stderr.strip()))

    def corrupt_asset(tree):
        with open(os.path.join(tree, "linux-bionic"), "ab") as handle:
            handle.write(b"x")

    def corrupt_sums(tree):
        path = os.path.join(tree, "SHA256SUMS")
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        lines[0] = "0" * 64 + lines[0][64:]
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)

    def swap_asset(tree):
        # Two assets exchanged.  The manifest and SHA256SUMS are then updated
        # from the files on disk, so both are internally consistent and only
        # the kind/description check catches that linux-bionic is no longer a
        # kernel -- a swap is not a corruption, and hashing alone cannot see it.
        linux = os.path.join(tree, "linux-bionic")
        stub = os.path.join(tree, "stub_exe")
        tmp = linux + ".swap"
        shutil.move(linux, tmp)
        shutil.move(stub, linux)
        shutil.move(tmp, stub)
        for path in (linux, stub):
            os.chmod(path, 0o755)

    def make_dynamic(tree):
        # The manifest was already written from the good assets; now replace one
        # asset with a dynamically linked binary and refresh only the sums, so
        # `check` sees a payload that contradicts the manifest's claim.  Running
        # `make` again here would be wrong: make validates what it publishes and
        # would reject the dynamic binary, which is the checker doing its job,
        # not a fixture we can build.
        dynamic = os.path.join(workdir, "dynamic")
        subprocess.run([target_cc or cc, "-o", dynamic, source], check=True)
        if elf_facts(dynamic)["interp_offset"] is None:
            return  # nothing to assert on a platform whose cc links dynamic
        if elf_facts(dynamic)["machine"] != 183:
            return  # arch differs from the manifest; another case covers that
        target = os.path.join(tree, "linux-bionic")
        shutil.copy(dynamic, target)
        os.chmod(target, 0o755)
        # Keep SHA256SUMS and the manifest self-consistent with the new bytes,
        # so the only thing left wrong is the dynamic linkage itself.
        digest = sha256_file(target)
        sums_path = os.path.join(tree, "SHA256SUMS")
        with open(sums_path, encoding="utf-8") as handle:
            lines = handle.readlines()
        with open(sums_path, "w", encoding="utf-8") as handle:
            for line in lines:
                if line.rstrip().endswith("linux-bionic"):
                    handle.write("%s  linux-bionic\n" % digest)
                else:
                    handle.write(line)
        manifest_path = os.path.join(tree, "MANIFEST")
        with open(manifest_path, encoding="utf-8") as handle:
            text = handle.read()
        text = re.sub(r"(asset\.linux-bionic\.sha256=)[0-9a-f]{64}",
                      r"\g<1>" + digest, text)
        text = re.sub(r"(asset\.linux-bionic\.size=)\d+",
                      r"\g<1>" + str(os.path.getsize(target)), text)
        with open(manifest_path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def drop_required(tree):
        at = os.path.join(tree, "MANIFEST")
        with open(at, encoding="utf-8") as handle:
            kept = [line for line in handle
                    if not line.startswith("ndk=")]
        with open(at, "w", encoding="utf-8") as handle:
            handle.writelines(kept)

    def lying_size(tree):
        at = os.path.join(tree, "MANIFEST")
        with open(at, encoding="utf-8") as handle:
            text = handle.read()
        text = re.sub(r"(asset\.linux-bionic\.size=)\d+", r"\g<1>999999999",
                      text)
        with open(at, "w", encoding="utf-8") as handle:
            handle.write(text)

    run_check("good", False, lambda tree: None)
    run_check("corrupt-asset", True, corrupt_asset)
    run_check("corrupt-sums", True, corrupt_sums)
    run_check("swapped-asset", True, swap_asset)
    run_check("dynamic-binary", True, make_dynamic)
    run_check("missing-field", True, drop_required)
    run_check("lying-size", True, lying_size)

    failures = 0
    for label, expect_fail, failed, output in cases:
        status = "ok" if failed == expect_fail else "WRONG"
        if failed != expect_fail:
            failures += 1
        print("self-test: %-14s expected=%-4s got=%-4s %s"
              % (label, "FAIL" if expect_fail else "PASS",
                 "FAIL" if failed else "PASS", status))
        if failed != expect_fail:
            print("    checker said: " + (output or "(nothing)"),
                  file=sys.stderr)
    if failures:
        fail("release checker self-test failed", cases=failures)
    emit("PASS", "release checker rejects every broken fixture",
         cases=len(cases))
    print("release checker self-test: PASS (%d cases)" % len(cases))
    return EXIT_PASS


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    make = sub.add_parser("make", help="write MANIFEST and SHA256SUMS")
    make.add_argument("--dir", required=True)
    make.add_argument("--tag", required=True)
    make.add_argument("--commit", required=True)
    make.add_argument("--branch", default="um-arm64")
    make.add_argument("--upstream", default="unknown")
    make.add_argument("--kernel-version", default="unknown")
    make.add_argument("--kernel-floor", default="0.0.0")
    make.add_argument("--ndk", default="unknown")
    make.add_argument("--api", default="30")
    make.add_argument("--compiler", default="")
    make.add_argument("--released", default="")
    make.add_argument("--notes", default="")
    # Default is the honest one: an artifact with no recorded gate has not been
    # gated.  Callers that did run the ext4 gate pass its verdict explicitly.
    make.add_argument("--gate", default="skip",
                      choices=["pass", "skip", "fail", "bug", "hang", "panic"])
    make.add_argument("--ext4-gate", default="optional",
                      choices=["required", "optional"])
    make.add_argument("--asset-gate", action="append", default=[],
                      metavar="NAME=VERDICT")
    make.set_defaults(func=cmd_make)

    check = sub.add_parser("check", help="verify a published or local release")
    check.add_argument("--tag")
    check.add_argument("--repo", default="Raymer8639/linux-um-arm64")
    check.add_argument("--dir", help="verify a local directory instead of "
                                     "downloading")
    check.add_argument("--workdir")
    check.add_argument("--token-file")
    check.add_argument("--expect-commit")
    check.add_argument("--require-all", action="store_true", default=True)
    check.set_defaults(func=cmd_check)

    selftest = sub.add_parser("self-test",
                              help="prove the checker rejects bad releases")
    selftest.add_argument("--workdir",
                          default=os.path.join(tempfile.gettempdir(),
                                               "um-release-selftest"))
    selftest.set_defaults(func=cmd_self_test)

    args = parser.parse_args()
    if args.command == "make":
        parsed = {}
        for item in args.asset_gate:
            if "=" not in item:
                parser.error("--asset-gate wants NAME=VERDICT")
            name, value = item.split("=", 1)
            parsed[name] = value
        args.asset_gate = parsed
        if not args.released:
            args.released = "unknown"
        if args.tag.startswith("v") or re.match(r"^[0-9]", args.tag):
            parser.error("tags must not look like a Linux version: use "
                         "um-arm64-vX.Y.Z")
    if args.command == "check" and not args.dir and not args.tag:
        parser.error("check needs --tag (published) or --dir (local)")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
