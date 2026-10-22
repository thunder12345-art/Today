import argparse
import hashlib
import os
import shutil
import struct
import sys
import time
import zipfile
import zlib

from common import OUT_DIR, ROOT, setup_stdout

OLD_MARK = '"proxyMode":"Go多线程"'
NEW_MARK = '"proxyMode":"Java多线程"'

SIZE_BALANCED_REPLACEMENTS = [
    ('"proxyMode":"Go多线程"', '"proxyMode":"Java多线程"'),
    ('"pansouUrl":"https://so.252035.xyz"', '"pansouUrl":"http://so.252035.xyz"'),
    ('"baiduThread":"10"', '"baiduThread":"9"'),
]


def uleb_decode(data, offset):
    result = 0
    shift = 0
    while True:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7


def string_bounds(data, offset):
    units, pos = uleb_decode(data, offset)
    start = pos
    count = 0
    while count < units:
        byte = data[pos]
        if byte < 0x80:
            pos += 1
            count += 1
        elif byte & 0xE0 == 0xC0:
            pos += 2
            count += 1
        elif byte & 0xF0 == 0xE0:
            pos += 3
            count += 1
        else:
            pos += 4
            count += 2
    return units, start, pos


def find_string_matches(data, marker):
    marker_bytes = marker.encode("utf-8")
    count, offset = struct.unpack_from("<II", data, 0x38)
    matches = []
    for index in range(count):
        str_off = struct.unpack_from("<I", data, offset + 4 * index)[0]
        if str_off >= len(data):
            continue
        units, start, end = string_bounds(data, str_off)
        payload = data[start:end]
        if marker_bytes in payload:
            matches.append({"index": index, "start": start, "end": end, "payload": payload})
    return matches


def verify_dex(data):
    return len(find_string_matches(data, OLD_MARK)), len(find_string_matches(data, NEW_MARK))


def patch_dex(data):
    old_items = find_string_matches(data, OLD_MARK)
    if not old_items:
        return None, len(find_string_matches(data, NEW_MARK)), None
    if len(old_items) != 1:
        raise ValueError("expected exactly 1 template string, found %d" % len(old_items))
    item = old_items[0]
    payload = bytes(item["payload"])
    for old_text, new_text in SIZE_BALANCED_REPLACEMENTS:
        old_bytes = old_text.encode("utf-8")
        new_bytes = new_text.encode("utf-8")
        occurrences = payload.count(old_bytes)
        if occurrences != 1:
            raise ValueError("expected 1 occurrence of %r, found %d" % (old_text, occurrences))
        payload = payload.replace(old_bytes, new_bytes)
    if len(payload) != item["end"] - item["start"]:
        raise ValueError("size-balanced replacement changed length: %d -> %d"
                         % (item["end"] - item["start"], len(payload)))
    new_dex = bytearray(data)
    new_dex[item["start"]:item["end"]] = payload
    outside = 0
    for i in range(len(data)):
        if data[i] != new_dex[i] and not (8 <= i < 32 or item["start"] <= i < item["end"]):
            outside += 1
    if outside:
        raise ValueError("unexpected changes outside payload/header: %d bytes" % outside)
    signature = hashlib.sha1(bytes(new_dex[32:])).digest()
    new_dex[12:32] = signature
    checksum = zlib.adler32(bytes(new_dex[12:])) & 0xFFFFFFFF
    struct.pack_into("<I", new_dex, 8, checksum)
    return bytes(new_dex), 1, (item["start"], item["end"])


def backup_path(backup_dir, jar_path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(backup_dir, "%s.%s.bak" % (os.path.basename(jar_path), stamp))


def run(jar_path, backup_dir, check_only):
    with zipfile.ZipFile(jar_path) as archive:
        infos = archive.infolist()
        dex_names = [info.filename for info in infos if info.filename.endswith(".dex")]
        if not dex_names:
            print("[patch] no .dex entries in %s" % jar_path)
            return 1
        changed = {}
        patched_total = 0
        already_total = 0
        for name in dex_names:
            data = archive.read(name)
            try:
                new_data, state, bounds = patch_dex(data)
            except ValueError as error:
                print("[patch] failed for %s: %s" % (name, error))
                return 1
            if new_data is None:
                already_total += state
                continue
            old_left, new_count = verify_dex(new_data)
            if old_left or not new_count:
                print("[patch] verification failed for %s: old=%d new=%d" % (name, old_left, new_count))
                return 1
            changed[name] = bytes(new_data)
            patched_total += 1

    if not changed:
        if already_total:
            print("[patch] already patched: default proxyMode is Java多线程 (%d string(s))" % already_total)
            return 0
        print("[patch] pattern %s not found: nothing to do" % OLD_MARK)
        return 1

    if check_only:
        print("[patch] jar needs patching: %s" % ", ".join(sorted(changed)))
        return 0

    if backup_dir:
        os.makedirs(backup_dir, exist_ok=True)
        target = backup_path(backup_dir, jar_path)
        shutil.copyfile(jar_path, target)
        print("[patch] backup saved: %s" % target)

    temp_path = jar_path + ".tmp"
    with zipfile.ZipFile(jar_path) as source, zipfile.ZipFile(temp_path, "w") as out:
        for info in source.infolist():
            if info.filename in changed:
                out.writestr(info, changed[info.filename])
            else:
                out.writestr(info, source.read(info.filename))
    with zipfile.ZipFile(temp_path) as check:
        bad = check.testzip()
        if bad:
            print("[patch] zip check failed at %s" % bad)
            os.remove(temp_path)
            return 1
    os.replace(temp_path, jar_path)
    print("[patch] patched %s: %s" % (jar_path, ", ".join(sorted(changed))))
    print("[patch] default proxyMode -> Java多线程 (size-preserving in-place edit)")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Patch tvbox.jar default proxyMode Go -> Java (in-place, idempotent)")
    parser.add_argument("--jar", default=os.path.join(ROOT, "tvbox.jar"))
    parser.add_argument("--backup-dir", default=os.path.join(OUT_DIR, "jar_backup"))
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--check", action="store_true", help="only report whether patching is needed")
    args = parser.parse_args()
    setup_stdout()

    if not os.path.exists(args.jar):
        print("[patch] jar not found: %s" % args.jar)
        sys.exit(1)
    backup_dir = None if args.no_backup else args.backup_dir
    sys.exit(run(args.jar, backup_dir, args.check))


if __name__ == "__main__":
    main()
