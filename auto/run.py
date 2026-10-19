import argparse
import os
import subprocess
import sys

from common import BASE, INBOX_DIR, setup_stdout


def pending_inbox():
    if not os.path.isdir(INBOX_DIR):
        return []
    return [name for name in os.listdir(INBOX_DIR)
            if name.lower().endswith(".json") and not name.startswith("_")]


def run_step(title, script, extra):
    print("")
    print("== %s ==" % title)
    command = [sys.executable, os.path.join(BASE, script)] + extra
    result = subprocess.run(command)
    if result.returncode != 0:
        print("[pipeline stopped] %s failed with code %d" % (script, result.returncode))
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="One-shot pipeline: submit inbox -> check -> build")
    parser.add_argument("--scope", choices=["local", "all"], default="local")
    parser.add_argument("--drop-dead", action="store_true")
    parser.add_argument("--inplace", action="store_true")
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--skip-submit", action="store_true")
    parser.add_argument("--skip-check", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    setup_stdout()

    if not args.skip_submit and pending_inbox():
        run_step("submit inbox", "submit.py",
                 ["--inbox", "--timeout", str(args.timeout)])
    else:
        print("== submit inbox: nothing pending ==")

    if not args.skip_check:
        run_step("health check", "check.py",
                 ["--workers", str(args.workers), "--timeout", str(args.timeout)])

    if not args.skip_build:
        build_args = ["--scope", args.scope]
        if args.drop_dead:
            build_args.append("--drop-dead")
        if args.inplace:
            build_args.append("--inplace")
        run_step("build configs", "build.py", build_args)

    print("")
    print("pipeline done")


if __name__ == "__main__":
    main()
