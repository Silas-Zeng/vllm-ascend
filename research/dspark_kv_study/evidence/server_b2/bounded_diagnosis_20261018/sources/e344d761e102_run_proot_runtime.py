"""Run correctness diagnostics inside a verified image without mount privileges.

PRoot traces host system calls. Results obtained through this launcher must not
be published as native end-to-end performance measurements.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path


def invocation(args):
    root = args.root.resolve()
    rootfs = root / "bundle/rootfs"
    plan = json.loads((root / "runtime_manifest.json").read_text(encoding="utf-8"))
    if json.loads((root / "state/runtime_pull.json").read_text())["status"] != "complete":
        raise ValueError("runtime download has not passed its content checks")
    if not (rootfs / "bin/bash").exists():
        raise ValueError("verified runtime is not unpacked")
    config_digest = plan["manifest"]["config"]["digest"].split(":", 1)[1]
    config_bytes = (root / "oci/blobs/sha256" / config_digest).read_bytes()
    if hashlib.sha256(config_bytes).hexdigest() != config_digest:
        raise ValueError("runtime configuration differs from pinned digest")
    config = json.loads(config_bytes)["config"]
    environment = dict(setting.split("=", 1) for setting in config["Env"])
    study = args.study.resolve()
    environment.update({"LANG": "C.UTF-8", "HOME": "/root"})
    environment["PYTHONPATH"] = str(study / "study") + ":" + environment.get("PYTHONPATH", "")
    for setting in args.env:
        key, value = setting.split("=", 1)
        if not key.isidentifier():
            raise ValueError("invalid environment key")
        environment[key] = value
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("a correctness diagnostic command is required")
    argv = [str(args.proot.resolve()), "-r", str(rootfs), "-w", config.get("WorkingDir") or "/"]
    for source in (
        Path("/dev"),
        Path("/proc"),
        Path("/sys"),
        Path("/usr/local/Ascend/driver"),
        Path("/usr/local/dcmi"),
        Path("/etc/ascend_install.info"),
        Path("/etc/resolv.conf"),
        Path("/etc/hosts"),
        study,
        Path("/model"),
    ):
        if source.exists():
            argv.extend(("-b", str(source)))
    argv.extend(("/usr/bin/env", "-i"))
    argv.extend(key + "=" + value for key, value in environment.items())
    argv.extend(config.get("Entrypoint") or [])
    argv.extend(command)
    return argv


def main(args):
    argv = invocation(args)
    # This library path belongs to the host PRoot binary, not to the guest.
    outer = dict(os.environ)
    outer["LD_LIBRARY_PATH"] = str(args.proot_library_path.resolve())
    outer["PROOT_NO_SECCOMP"] = "1"
    os.execvpe(argv[0], argv, outer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--proot", type=Path, required=True)
    parser.add_argument("--proot-library-path", type=Path, required=True)
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    main(parser.parse_args())
