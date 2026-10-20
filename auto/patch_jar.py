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

OLD_TEXT = '"proxyMode":"Go多线程"'
NEW_TEXT = '"proxyMode":"Java多线程"'
OLD_BYTES = OLD_TEXT.encode("utf-8")
NEW_BYTES = NEW_TEXT.encode("utf-8")

MAP_TYPE_STRING_DATA = 0x2002


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


def uleb_encode(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


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


def find_string_ids(data):
    count, offset = struct.unpack_from("<II", data, 0x38)
    found = []
    for index in range(count):
        str_off = struct.unpack_from("<I", data, offset + 4 * index)[0]
        if str_off >= len(data):
            continue
        units, start, end = string_bounds(data, str_off)
        payload = data[start:end]
        if OLD_BYTES in payload or NEW_BYTES in payload:
            found.append({"index": index, "str_off": str_off, "units": units,
                          "start": start, "end": end, "payload": payload})
    return found, count, offset


def bump_map_list(data, extra_items):
    map_off = struct.unpack_from("<I", data, 0x34)[0]
    if not map_off:
        return False
    size = struct.unpack_from("<I", data, map_off)[0]
    pos = map_off + 4
    for _ in range(size):
        kind, unused, count, offset = struct.unpack_from("<HHII", data, pos)
        if kind == MAP_TYPE_STRING_DATA:
            struct.pack_into("<I", data, pos + 4, count + extra_items)
            return True
        pos += 12
    return False


def patch_dex(data):
    matches, _, _ = find_string_ids(data)
    old_matches = [item for item in matches if OLD_BYTES in item["payload"]]
    if not old_matches:
        already = [item for item in matches if NEW_BYTES in item["payload"]]
        return None, len(already)
    new_dex = bytearray(data)
    appended = 0
    for item in old_matches:
        payload = item["payload"]
        occurrences = payload.count(OLD_BYTES)
        new_payload = payload.replace(OLD_BYTES, NEW_BYTES)
        new_units = item["units"] + 2 * occurrences
        new_item = uleb_encode(new_units) + new_payload + b"\x00"
        new_offset = len(new_dex)
        new_dex += new_item
        count, ids_off = struct.unpack_from("<II", new_dex, 0x38)
        struct.pack_into("<I", new_dex, ids_off + 4 * item["index"], new_offset)
        appended += 1
    file_size = struct.unpack_from("<I", new_dex, 0x20)[0]
    data_size = struct.unpack_from("<I", new_dex, 0x68)[0]
    extra = len(new_dex) - len(data)
    struct.pack_into("<I", new_dex, 0x20, file_size + extra)
    struct.pack_into("<I", new_dex, 0x68, data_size + extra)
    bump_map_list(new_dex, appended)
    signature = hashlib.sha1(bytes(new_dex[32:])).digest()
    new_dex[12:32] = signature
    checksum = zlib.adler32(bytes(new_dex[12:])) & 0xFFFFFFFF
    struct.pack_into("<I", new_dex, 8, checksum)
    return bytes(new_dex), len(old_matches)


def verify_dex(data):
    matches, _, _ = find_string_ids(data)
    old_count = sum(item["payload"].count(OLD_BYTES) for item in matches)
    new_count = sum(item["payload"].count(NEW_BYTES) for item in matches)
    return old_count, new_count


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
            new_data, state = patch_dex(data)
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
        print("[patch] pattern %s not found: nothing to do" % OLD_TEXT)
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
    print("[patch] default proxyMode -> Java多线程")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Patch tvbox.jar default proxyMode Go -> Java (idempotent)")
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
