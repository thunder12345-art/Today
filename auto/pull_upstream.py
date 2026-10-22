import argparse
import os
import subprocess
import sys

from common import BASE, ROOT, setup_stdout

OWN_PREFIXES = ("auto/",)
OWN_FILES = {".gitattributes", ".gitignore", ".dockerignore", "Dockerfile", "docker-compose.yml"}
MANAGED_EXCLUDES = ["--", ".", ":(exclude)auto", ":(exclude).gitignore", ":(exclude).dockerignore",
                    ":(exclude)Dockerfile", ":(exclude)docker-compose.yml", ":(exclude).gitattributes"]


def git(*args, check=True, binary=False):
    if binary:
        result = subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True)
    else:
        result = subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True,
                                encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        print("git %s failed:" % " ".join(args))
        if binary:
            print(result.stdout[:400])
            print(result.stderr[:400])
        else:
            print(result.stdout)
            print(result.stderr)
        sys.exit(1)
    return result.stdout


def git_run(input_data, *args):
    result = subprocess.run(["git", "-C", ROOT] + list(args), input=input_data, capture_output=True)
    if result.returncode != 0:
        print("git %s failed:" % " ".join(args))
        print(result.stdout[:400])
        print(result.stderr[:400])
        sys.exit(1)
    return result.stdout


def parse_ls_tree(text):
    tree = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode, kind, blob = meta.split()
        tree[path] = (mode, blob)
    return tree


def resolve_mapping(ours_files, up_files):
    ours_fold = fold_map(ours_files)
    chosen = {}
    for up_path in up_files:
        key = up_path.casefold()
        current = chosen.get(key)
        if current is None:
            chosen[key] = up_path
            continue
        if current not in ours_fold.values() and up_path in ours_fold.values():
            chosen[key] = up_path
    mapping = {}
    for key, up_path in chosen.items():
        mapping[ours_fold.get(key, up_path)] = up_path
    return mapping


def lines(text):
    return [line for line in text.splitlines() if line.strip()]


def is_own(path):
    return path in OWN_FILES or any(path.startswith(prefix) for prefix in OWN_PREFIXES)


def fold_map(paths):
    return {path.casefold(): path for path in paths}


def scan_tree(tree_dir):
    paths = []
    for dirpath, dirnames, filenames in os.walk(tree_dir):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            paths.append(os.path.relpath(full, tree_dir).replace(os.sep, "/"))
    return sorted(paths)


def scan_worktree(root):
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            paths.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(paths)


def read_file_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def run_tree(tree_dir, args):
    up_all = [path for path in scan_tree(tree_dir) if not is_own(path)]
    ours_all = [path for path in scan_worktree(ROOT) if not is_own(path)]
    ours_set = set(ours_all)
    ours_fold = fold_map(ours_all)
    up_fold = fold_map(up_all)
    managed_roots = {path.split("/", 1)[0] for path in up_all}

    mapping = resolve_mapping(ours_all, up_all)
    chosen_up = set(mapping.values())
    chosen_folds = {path.casefold() for path in chosen_up}
    twins = {path for path in up_all if path not in chosen_up and path.casefold() in chosen_folds}
    own_for_fold = {up_path.casefold(): own_path for own_path, up_path in mapping.items()}
    collisions = sorted((twin, own_for_fold.get(twin.casefold(), twin)) for twin in twins)

    added = sorted(path for path in up_all
                   if path not in ours_set and path.casefold() not in ours_fold)
    stale = sorted(path for path in ours_all
                   if path not in set(up_all) and path.casefold() not in up_fold
                   and path.split("/", 1)[0] in managed_roots)

    changed = []
    for own_path, up_path in sorted(mapping.items()):
        if own_path == "tvbox.jar":
            continue
        up_data = read_file_bytes(os.path.join(tree_dir, up_path.replace("/", os.sep)))
        target = os.path.join(ROOT, own_path.replace("/", os.sep))
        own_data = read_file_bytes(target) if os.path.exists(target) else None
        if own_data != up_data:
            changed.append(own_path)

    jar_changed = False
    jar_entry = mapping.get("tvbox.jar")
    if jar_entry:
        up_data = read_file_bytes(os.path.join(tree_dir, jar_entry.replace("/", os.sep)))
        jar_target = os.path.join(ROOT, "tvbox.jar")
        own_data = read_file_bytes(jar_target) if os.path.exists(jar_target) else None
        jar_changed = own_data != up_data

    print("")
    print("== preview (tree mode) ==")
    print("upstream files: %d | repo files: %d | added: %d | stale: %d" %
          (len(up_all), len(ours_all), len(added), len(stale)))
    for path in added[:30]:
        print("   + %s" % path)
    for path in stale[:30]:
        print("   - %s" % path)
    for up_path, own_path in collisions:
        print("   case collision: keep %s (upstream twin: %s)" % (own_path, up_path))
    print("")
    print("content changes (%d):" % len(changed))
    for path in changed[:60]:
        print("   ~ %s" % path)
    if jar_changed:
        print("   ~ tvbox.jar (will re-patch)")

    if not args.apply:
        print("")
        print("preview only. re-run with --apply to update the working tree.")
        return 0

    print("")
    print("== applying (byte-faithful, tree mode) ==")
    written = 0
    for own_path in changed:
        up_path = mapping[own_path]
        data = read_file_bytes(os.path.join(tree_dir, up_path.replace("/", os.sep)))
        target = os.path.join(ROOT, own_path.replace("/", os.sep))
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)
        written += 1
    if jar_changed:
        data = read_file_bytes(os.path.join(tree_dir, jar_entry.replace("/", os.sep)))
        with open(os.path.join(ROOT, "tvbox.jar"), "wb") as handle:
            handle.write(data)
        print("upstream tvbox.jar changed, re-patching")
        written += 1
    removed = []
    for own_path in stale:
        target = os.path.join(ROOT, own_path.replace("/", os.sep))
        if os.path.exists(target):
            os.remove(target)
        removed.append(own_path)
    print("updated %d file(s) | removed %d stale file(s)" % (written, len(removed)))

    if not args.skip_patch:
        print("")
        print("== re-patching tvbox.jar ==")
        subprocess.run([sys.executable, os.path.join(BASE, "patch_jar.py")])
    if not args.skip_sync:
        print("")
        print("== reconciling auto/sources.json ==")
        subprocess.run([sys.executable, os.path.join(BASE, "sync.py"), "--prune-whitelist"])
    print("")
    print("== refreshing jarscan cache ==")
    subprocess.run([sys.executable, os.path.join(BASE, "jarscan.py")])
    print("")
    print("tree mode: changes written; run git add -A && git commit on the host.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Office-side: pull upstream content into this repo (keeps auto/)")
    parser.add_argument("--apply", action="store_true", help="actually apply changes (default: preview only)")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--skip-patch", action="store_true")
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--tree", default=None, metavar="DIR",
                        help="sync from a materialized upstream tree (no git needed; for container use on NAS)")
    args = parser.parse_args()
    setup_stdout()

    if args.tree:
        if not os.path.isdir(args.tree):
            print("tree dir not found: %s" % args.tree)
            return 1
        return run_tree(args.tree, args)

    if not args.no_fetch:
        print("== fetching upstream ==")
        git("fetch", "upstream")
    upstream = git("rev-parse", "--abbrev-ref", "upstream/HEAD").strip() or "upstream/master"
    print("upstream ref: %s | head: %s" % (upstream, git("rev-parse", upstream).strip()[:10]))

    ours_files = [path for path in lines(git("ls-tree", "-r", "--name-only", "HEAD")) if not is_own(path)]
    up_files = lines(git("ls-tree", "-r", "--name-only", upstream))
    ours_set = set(ours_files)
    ours_fold = fold_map(ours_files)
    up_fold = fold_map(up_files)
    managed_roots = {path.split("/", 1)[0] for path in up_files}

    mapping = resolve_mapping(ours_files, up_files)
    chosen_up = set(mapping.values())
    chosen_folds = {path.casefold() for path in chosen_up}
    twins = {path for path in up_files if path not in chosen_up and path.casefold() in chosen_folds}
    own_for_fold = {}
    for own_path, up_path in mapping.items():
        own_for_fold[up_path.casefold()] = own_path
    collisions = sorted((twin, own_for_fold.get(twin.casefold(), twin)) for twin in twins)

    added = sorted(path for path in up_files
                   if path not in ours_set and path.casefold() not in ours_fold)
    stale = sorted(path for path in ours_files
                   if path not in set(up_files) and path.casefold() not in up_fold
                   and path.split("/", 1)[0] in managed_roots)

    def keep(path):
        return not is_own(path) and path not in twins

    all_diff = [path for path in lines(git("diff", "--name-only", "HEAD", upstream, *MANAGED_EXCLUDES))
                if keep(path)]
    real_diff = [path for path in lines(git("diff", "--ignore-cr-at-eol", "--name-only",
                                            "HEAD", upstream, *MANAGED_EXCLUDES))
                 if keep(path)]
    real_diff_set = set(real_diff)
    eol_only = sorted(path for path in all_diff if path not in real_diff_set)

    print("")
    print("== preview ==")
    print("upstream files: %d | repo files: %d | added: %d | stale: %d" %
          (len(up_files), len(ours_files), len(added), len(stale)))
    for path in added[:30]:
        print("   + %s" % path)
    for path in stale[:30]:
        print("   - %s" % path)
    for up_path, own_path in collisions:
        print("   case collision: keep %s (upstream twin: %s)" % (own_path, up_path))
    if real_diff:
        print("")
        print("real content changes (%d):" % len(real_diff))
        stat = git("diff", "--ignore-cr-at-eol", "--stat", "HEAD", upstream, *MANAGED_EXCLUDES)
        for line in stat.splitlines()[-min(len(real_diff) + 1, 60):]:
            if keep(line.strip().split(" |")[0]):
                print("  %s" % line.strip())
    else:
        print("real content changes: none")
    if eol_only:
        print("")
        print("line-ending-only differences (byte-identical otherwise): %d files" % len(eol_only))

    if not args.apply:
        print("")
        print("preview only. re-run with --apply to update the working tree.")
        return 0

    print("")
    print("== applying (byte-faithful manual checkout) ==")
    head_tree = parse_ls_tree(git("ls-tree", "-r", "HEAD"))
    up_tree = parse_ls_tree(git("ls-tree", "-r", upstream))
    ours_managed = [path for path in head_tree if not is_own(path)]
    up_managed = [path for path in up_tree if not is_own(path)]
    mapping = resolve_mapping(ours_managed, up_managed)
    up_folds = {path.casefold() for path in up_managed}

    index_lines = []
    written = 0
    removed = []
    for own_path, up_path in sorted(mapping.items()):
        if own_path == "tvbox.jar":
            continue
        up_mode, up_blob = up_tree[up_path]
        own_entry = head_tree.get(own_path)
        if own_entry and own_entry[1] == up_blob:
            continue
        data = git("cat-file", "-p", "%s:%s" % (upstream, up_path), binary=True)
        target = os.path.join(ROOT, own_path.replace("/", os.sep))
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)
        new_blob = git_run(data, "hash-object", "-w", "--stdin").decode("ascii", "replace").strip()
        index_lines.append("%s %s\t%s" % (up_mode, new_blob, own_path))
        written += 1
    jar_entry = mapping.get("tvbox.jar")
    if jar_entry:
        up_blob = up_tree[jar_entry][1]
        head_blob = (head_tree.get("tvbox.jar") or (None, None))[1]
        if up_blob != head_blob:
            jar_target = os.path.join(ROOT, "tvbox.jar")
            data = git("cat-file", "-p", "%s:%s" % (upstream, jar_entry), binary=True)
            with open(jar_target, "wb") as handle:
                handle.write(data)
            print("upstream tvbox.jar changed, re-patching")
            subprocess.run([sys.executable, os.path.join(BASE, "patch_jar.py")])
            with open(jar_target, "rb") as handle:
                patched = handle.read()
            patched_blob = git_run(patched, "hash-object", "-w", "--stdin").decode("ascii", "replace").strip()
            index_lines.append("100644 %s\ttvbox.jar" % patched_blob)
            written += 1
    for own_path in ours_managed:
        if own_path.casefold() in up_folds:
            continue
        own_entry = head_tree.get(own_path)
        if own_entry and own_entry[0] == "160000":
            continue
        index_lines.append("0 %s\t%s" % ("0" * 40, own_path))
        target = os.path.join(ROOT, own_path.replace("/", os.sep))
        if os.path.exists(target):
            os.remove(target)
        removed.append(own_path)
    if index_lines:
        git_run("\n".join(index_lines).encode("utf-8") + b"\n", "update-index", "--index-info")
    print("updated %d file(s) | removed %d stale file(s)" % (written, len(removed)))
    for path in removed[:30]:
        print("   - %s" % path)

    if not args.skip_patch:
        print("")
        print("== re-patching tvbox.jar ==")
        subprocess.run([sys.executable, os.path.join(BASE, "patch_jar.py")])
    if not args.skip_sync:
        print("")
        print("== reconciling auto/sources.json ==")
        subprocess.run([sys.executable, os.path.join(BASE, "sync.py"), "--prune-whitelist"])
    print("")
    print("== refreshing jarscan cache ==")
    subprocess.run([sys.executable, os.path.join(BASE, "jarscan.py")])

    status = lines(git("status", "--short"))
    print("")
    print("== working tree now has %d changed path(s) ==" % len(status))
    for line in status[:40]:
        print("  %s" % line)
    print("")
    print("next: git add -A && git commit -m \"sync upstream <date>\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
