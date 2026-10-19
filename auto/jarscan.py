import os
import struct
import time
import zipfile

from common import BASE, ROOT, load_json, now_iso, save_json, setup_stdout

JAR_PATH = os.path.join(ROOT, "tvbox.jar")
CACHE_PATH = os.path.join(BASE, "jarscan.json")

VALUE_STRING = 0x17
VALUE_NESTED_INDEX = (0x16, 0x18, 0x19, 0x1A, 0x1B)
VALUE_ARRAY = 0x1C
VALUE_ANNOTATION = 0x1D
VALUE_NULL = 0x1E
VALUE_BOOLEAN = 0x1F


def short_name(descriptor):
    return descriptor[1:-1].rsplit("/", 1)[-1] if descriptor.startswith("L") and descriptor.endswith(";") else descriptor


def _uleb(data, offset):
    result = 0
    shift = 0
    while True:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7


def _read_strings(data):
    count, offset = struct.unpack_from("<II", data, 0x38)
    strings = []
    for index in range(count):
        str_off = struct.unpack_from("<I", data, offset + 4 * index)[0]
        length, pos = _uleb(data, str_off)
        strings.append(data[pos:pos + length].decode("utf-8", "replace"))
    return strings


def _scan_code_urls(data, insns_off, insns_size_units, urls_by_index, found):
    end = insns_off + insns_size_units * 2
    pos = insns_off
    while pos + 4 <= end:
        unit = struct.unpack_from("<H", data, pos)[0]
        if unit == 0x001A:
            url = urls_by_index.get(struct.unpack_from("<H", data, pos + 2)[0])
            if url:
                found.add(url)
            pos += 4
        elif unit == 0x001B:
            if pos + 6 > end:
                break
            url = urls_by_index.get(struct.unpack_from("<I", data, pos + 2)[0])
            if url:
                found.add(url)
            pos += 6
        else:
            pos += 2


def _scan_class_data(data, offset, urls_by_index, found):
    pos = offset
    static_fields, pos = _uleb(data, pos)
    instance_fields, pos = _uleb(data, pos)
    direct_methods, pos = _uleb(data, pos)
    virtual_methods, pos = _uleb(data, pos)
    for _ in range(static_fields + instance_fields):
        _, pos = _uleb(data, pos)
        _, pos = _uleb(data, pos)
    for _ in range(direct_methods + virtual_methods):
        _, pos = _uleb(data, pos)
        _, pos = _uleb(data, pos)
        code_off, pos = _uleb(data, pos)
        if code_off and code_off + 16 <= len(data):
            insns_size = struct.unpack_from("<I", data, code_off + 12)[0]
            if insns_size:
                _scan_code_urls(data, code_off + 16, insns_size, urls_by_index, found)


def _read_index(data, pos, value_arg):
    size = value_arg + 1
    chunk = data[pos:pos + size]
    if len(chunk) < size:
        raise ValueError("index out of range")
    return int.from_bytes(chunk, "little"), pos + size


def _scan_value(data, pos, urls_by_index, found):
    header, pos = _uleb(data, pos)
    value_type = header & 0x1F
    value_arg = header >> 5
    if value_type == VALUE_STRING:
        index, pos = _read_index(data, pos, value_arg)
        url = urls_by_index.get(index)
        if url:
            found.add(url)
        return pos
    if value_type in VALUE_NESTED_INDEX:
        _, pos = _read_index(data, pos, value_arg)
        return pos
    if value_type == VALUE_ARRAY:
        size, pos = _uleb(data, pos)
        for _ in range(size):
            pos = _scan_value(data, pos, urls_by_index, found)
        return pos
    if value_type == VALUE_ANNOTATION:
        _, pos = _uleb(data, pos)
        size, pos = _uleb(data, pos)
        for _ in range(size):
            _, pos = _uleb(data, pos)
            pos = _scan_value(data, pos, urls_by_index, found)
        return pos
    if value_type in (VALUE_NULL, VALUE_BOOLEAN):
        return pos
    return pos + value_arg + 1


def _scan_encoded_array(data, offset, urls_by_index, found):
    size, pos = _uleb(data, offset)
    for _ in range(size):
        pos = _scan_value(data, pos, urls_by_index, found)


def _scan_dex(data):
    strings = _read_strings(data)
    urls_by_index = {index: text for index, text in enumerate(strings)
                     if text.startswith(("http://", "https://"))}

    type_ids_size, type_ids_off = struct.unpack_from("<II", data, 0x40)
    class_defs_size, class_defs_off = struct.unpack_from("<II", data, 0x60)
    type_string_index = [struct.unpack_from("<I", data, type_ids_off + 4 * i)[0] for i in range(type_ids_size)]

    classes = set()
    urls_by_class = {}
    warnings = []
    for i in range(class_defs_size):
        base = class_defs_off + 32 * i
        class_idx = struct.unpack_from("<I", data, base)[0]
        class_data_off = struct.unpack_from("<I", data, base + 24)[0]
        static_values_off = struct.unpack_from("<I", data, base + 28)[0]
        if class_idx >= len(type_string_index):
            continue
        name = strings[type_string_index[class_idx]]
        if not (name.startswith("L") and name.endswith(";")):
            continue
        classes.add(name)
        found = set()
        try:
            if static_values_off:
                _scan_encoded_array(data, static_values_off, urls_by_index, found)
        except Exception as error:
            warnings.append("%s static_values: %s" % (name, error))
        try:
            if class_data_off:
                _scan_class_data(data, class_data_off, urls_by_index, found)
        except Exception as error:
            warnings.append("%s class_data: %s" % (name, error))
        if found:
            urls_by_class[name] = sorted(found)
    return classes, urls_by_class, warnings


def scan_jar(path=None):
    path = path or JAR_PATH
    classes = set()
    urls_by_class = {}
    warnings = []
    with zipfile.ZipFile(path) as archive:
        for entry in archive.namelist():
            if not entry.endswith(".dex"):
                continue
            dex_classes, dex_urls, dex_warnings = _scan_dex(archive.read(entry))
            classes.update(dex_classes)
            warnings.extend(dex_warnings)
            for descriptor, urls in dex_urls.items():
                merged = set(urls_by_class.get(descriptor, []))
                merged.update(urls)
                urls_by_class[descriptor] = sorted(merged)
    return {"classes": sorted(classes), "urls_by_class": urls_by_class, "warnings": warnings}


def _cache_key(path):
    stat = os.stat(path)
    return "%d-%d" % (int(stat.st_mtime), stat.st_size)


def load_jarscan(force=False, path=None):
    path = path or JAR_PATH
    if not os.path.exists(path):
        return {"classes": [], "urls_by_class": {}, "missing_jar": True}
    key = _cache_key(path)
    cached = load_json(CACHE_PATH)
    if cached and cached.get("key") == key and not force:
        return cached
    scan = scan_jar(path)
    scan["key"] = key
    scan["source"] = os.path.basename(path)
    scan["scanned_at"] = now_iso()
    save_json(CACHE_PATH, scan)
    return scan


def main():
    setup_stdout()
    started = time.time()
    scan = load_jarscan(force=True)
    classes = scan.get("classes", [])
    urls_by_class = scan.get("urls_by_class", {})
    print("scanned %s in %.1fs: classes=%d classes_with_urls=%d urls=%d"
          % (scan.get("source", "tvbox.jar"), time.time() - started,
             len(classes), len(urls_by_class), sum(len(v) for v in urls_by_class.values())))

    by_name = {short_name(descriptor).lower(): descriptor for descriptor in classes}
    tvbox = load_json(os.path.join(ROOT, "tvbox.json"), {})
    referenced = []
    for site in tvbox.get("sites", []):
        api = site.get("api")
        if isinstance(api, str) and api.startswith("csp_"):
            referenced.append(api[4:])
    unique = sorted(set(referenced), key=str.lower)
    missing = [name for name in unique if name.lower() not in by_name]
    print("csp referenced=%d | missing=%s" % (len(unique), ", ".join(missing) if missing else "none"))
    print("")
    print("embedded urls per referenced class:")
    for name in unique:
        descriptor = by_name.get(name.lower())
        urls = urls_by_class.get(descriptor, []) if descriptor else []
        if urls:
            print("  %-16s %s" % (name, "  ".join(urls[:4])))


if __name__ == "__main__":
    main()
