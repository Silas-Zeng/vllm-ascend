"""Run the pinned image in a private mount namespace using its own entrypoint.

No host driver, CANN installation, Python environment or system service is
replaced. Bind mounts exist only for the lifetime of the isolated process.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def mount_target(rootfs, relative, is_file=False):
    target = rootfs / relative
    if target.is_symlink():
        raise ValueError(f"refuse bind target symlink: {relative}")
    if not target.resolve().is_relative_to(rootfs.resolve()):
        raise ValueError("bind target escapes the image root")
    if is_file:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=True)
    else:
        target.mkdir(parents=True, exist_ok=True)
    return target


def run(args):
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("a runtime command is required")
    root, study = args.root.resolve(), args.study.resolve()
    rootfs = root / "bundle/rootfs"
    if not args.inside:
        invocation = [
            "sudo",
            "-n",
            "unshare",
            "--mount",
            "--propagation",
            "private",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "--inside",
            "--root",
            str(root),
            "--study",
            str(study),
        ]
        for setting in args.env:
            invocation.extend(("--env", setting))
        return subprocess.run([*invocation, "--", *command], check=False).returncode

    if os.geteuid() != 0:
        raise PermissionError("private bind mounts and chroot require the existing sudo capability")
    if not (rootfs / "bin/bash").exists() or not (root / "bundle/config.json").is_file():
        raise ValueError("the verified runtime image has not been unpacked")
    for source, relative, recursive, readonly in (
        (Path("/dev"), "dev", True, False),
        (Path("/proc"), "proc", True, False),
        (Path("/sys"), "sys", True, False),
        (Path("/usr/local/Ascend/driver"), "usr/local/Ascend/driver", False, True),
        (root / "models", "models", False, True),
        (study, "study", False, False),
        (root / "runs", "results", False, False),
        (root / "sources", "sources", False, False),
    ):
        source.mkdir(parents=True, exist_ok=True)
        target = mount_target(rootfs, relative)
        subprocess.run(["mount", "--rbind" if recursive else "--bind", str(source), str(target)], check=True)
        if readonly:
            subprocess.run(["mount", "-o", "remount,bind,ro", str(target)], check=True)
    for source, relative in (
        (Path("/etc/resolv.conf"), "etc/resolv.conf"),
        (Path("/etc/hosts"), "etc/hosts"),
        (Path("/usr/local/bin/npu-smi"), "usr/local/bin/npu-smi"),
    ):
        if source.exists():
            target = mount_target(rootfs, relative, is_file=True)
            subprocess.run(["mount", "--bind", str(source), str(target)], check=True)
            subprocess.run(["mount", "-o", "remount,bind,ro", str(target)], check=True)

    plan = json.loads((root / "runtime_manifest.json").read_text(encoding="utf-8"))
    config_digest = plan["manifest"]["config"]["digest"].split(":", 1)[1]
    config = json.loads((root / "oci/blobs/sha256" / config_digest).read_text(encoding="utf-8"))["config"]
    environment = dict(setting.split("=", 1) for setting in config["Env"])
    environment.update({"LANG": "C.UTF-8", "HOME": "/root"})
    for setting in args.env:
        key, value = setting.split("=", 1)
        if not key.isidentifier():
            raise ValueError("invalid environment key")
        environment[key] = value
    entrypoint = config.get("Entrypoint") or []
    os.chroot(rootfs)
    os.chdir(config.get("WorkingDir") or "/")
    os.execvpe(entrypoint[0] if entrypoint else command[0], [*entrypoint, *command], environment)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    raise SystemExit(run(parser.parse_args()))
