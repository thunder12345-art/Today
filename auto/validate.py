import argparse
import json
import os
import re
import sys
import zipfile

from common import JAR_CACHE_DIR, ROOT, file_md5, load_json_safe, setup_stdout

PROFILES = {
    "tvbox.json": "jar/spider.jar",
    "tvbox.phone.json": "jar/spider.jar",
    "tvbox.github.json": "jar/spider.jar",
    "tvbox.legacy.json": "tvbox.jar",
}
MD5_RE = re.compile(r"^[0-9a-f]{32}$")
PRIVATE_RE = re.compile(r"(?<![0-9])(?:192\.168\.|10\.|172\.(?:1[6-9]|2[0-9]|3[01])\.)")


def _core(value):
    return value.strip().split(";md5;", 1)[0].split("?", 1)[0]


def _name(value):
    return _core(value).rsplit("/", 1)[-1]


def _digest(value):
    parts = value.split(";md5;", 1)
    return parts[1].strip() if len(parts) == 2 else ""


def _read_dex_classes(path):
    try:
        with zipfile.ZipFile(path) as archive:
            data = b"".join(archive.read(name) for name in archive.namelist()
                            if name.endswith(".dex"))
    except (OSError, zipfile.BadZipFile):
        return set()
    return {item.decode("ascii", "ignore") for item in re.findall(
        rb"Lcom/github/catvod/spider/[A-Za-z0-9_$]+;", data)}


def _jar_classes(path):
    return _read_dex_classes(path)


def _check_jar(path, required, label, errors):
    if not os.path.exists(path):
        errors.append("%s 不存在: %s" % (label, path))
        return set()
    classes = _jar_classes(path)
    for name in required:
        if name not in classes:
            errors.append("%s 缺少类 %s" % (label, name))
    return classes


def _check_config(path, expected_jar, errors, warnings):
    data = load_json_safe(path)
    name = os.path.basename(path)
    if not isinstance(data, dict):
        errors.append("%s 不是 JSON 对象" % name)
        return {}
    sites = data.get("sites")
    if not isinstance(sites, list) or not sites:
        errors.append("%s 缺少 sites" % name)
        sites = []
    spider = data.get("spider")
    if not isinstance(spider, str) or not spider.strip():
        errors.append("%s 缺少 spider" % name)
        return data
    expected_name = os.path.basename(expected_jar)
    if _name(spider) != expected_name:
        errors.append("%s 的 spider 应为 %s，实际为 %s"
                      % (name, expected_name, _name(spider)))
    digest = _digest(spider)
    if not digest:
        errors.append("%s 的 spider 缺少 md5" % name)
    elif not MD5_RE.match(digest):
        errors.append("%s 的 spider md5 非法" % name)
    if name == "tvbox.github.json":
        if not _core(spider).lower().startswith("https://"):
            errors.append("GitHub 配置的 spider 必须是绝对 HTTPS 地址")
        if PRIVATE_RE.search(json.dumps(data, ensure_ascii=False)):
            errors.append("GitHub 配置包含内网地址")
    else:
        if not _core(spider).startswith("./"):
            errors.append("%s 的 spider 应为相对路径" % name)

    seen_apis = set()
    missing_local = set()
    for item in sites:
        if not isinstance(item, dict):
            errors.append("%s 包含非法站点项" % name)
            continue
        api = item.get("api")
        if isinstance(api, str) and api.startswith("csp_"):
            seen_apis.add(api[4:])
        jar = item.get("jar")
        if not isinstance(jar, str) or not jar.strip():
            continue
        jar_name = _name(jar)
        jar_digest = _digest(jar)
        if jar_digest and not MD5_RE.match(jar_digest):
            errors.append("%s 站点 %s 的 jar md5 非法" % (name, item.get("key", "?")))
        if _core(jar).startswith("./"):
            local = os.path.join(ROOT, _core(jar)[2:])
            cached = os.path.join(JAR_CACHE_DIR, jar_name)
            if not os.path.exists(local) and not os.path.exists(cached):
                missing_local.add(_core(jar))
        if name == "tvbox.github.json" and PRIVATE_RE.search(jar):
            errors.append("%s 站点 %s 的 jar 含内网地址"
                          % (name, item.get("key", "?")))
    if missing_local:
        warnings.append("%s 有 %d 个本地 jar 尚未缓存: %s"
                        % (name, len(missing_local), ", ".join(sorted(missing_local))))
    return {"data": data, "sites": sites, "apis": seen_apis, "spider": spider}


def validate_outputs(directory):
    errors = []
    warnings = []
    configs = {}
    for filename, expected_jar in PROFILES.items():
        path = os.path.join(directory, filename)
        if not os.path.exists(path):
            errors.append("缺少配置文件: %s" % filename)
            continue
        configs[filename] = _check_config(path, expected_jar, errors, warnings)

    if configs:
        qist_jar = os.path.join(ROOT, "jar", "spider.jar")
        qist_classes = _check_jar(qist_jar, {
            "Lcom/github/catvod/spider/Init;",
            "Lcom/github/catvod/spider/Init$UP;",
        }, "jar/spider.jar", errors)
        old_jar = os.path.join(ROOT, "tvbox.jar")
        _check_jar(old_jar, {"Lcom/github/catvod/spider/Init;"},
                   "tvbox.jar", errors)

        for filename, info in configs.items():
            if not info:
                continue
            spider = info.get("spider", "")
            expected = os.path.join(ROOT, _core(spider).lstrip("./"))
            actual = file_md5(expected)
            digest = _digest(spider)
            if actual and digest and actual != digest:
                errors.append("%s 的 spider md5 与实际文件不一致" % filename)
            if filename == "tvbox.legacy.json":
                missing = sorted(api for api in info["apis"]
                                 if "Lcom/github/catvod/spider/%s;" % api
                                 not in _jar_classes(old_jar))
                if missing:
                    warnings.append("legacy 配置有 %d 个站点 API 不在旧 jar 中"
                                    % len(missing))
            elif filename == "tvbox.json" and qist_classes:
                missing = sorted(api for api in info["apis"]
                                 if "Lcom/github/catvod/spider/%s;" % api
                                 not in qist_classes)
                if missing:
                    warnings.append("lan 配置有 %d 个站点 API 不在 jar/spider.jar 中"
                                    % len(missing))

    compat = load_json_safe(os.path.join(directory, "compat.json"))
    if not isinstance(compat, dict):
        errors.append("缺少或非法: compat.json")
    elif compat.get("product") != "Tomorrow":
        errors.append("compat.json 的 product 必须为 Tomorrow")
    else:
        profiles = compat.get("profiles")
        if not isinstance(profiles, dict):
            errors.append("compat.json 缺少 profiles")
        else:
            for key in ("lan", "phone", "github", "legacy"):
                if key not in profiles:
                    errors.append("compat.json 缺少 profile: %s" % key)
        for key in ("spider.jar", "pg_upgraded.jar", "tvbox.jar"):
            item = (compat.get("jars") or {}).get(key)
            if not isinstance(item, dict) or not item.get("md5"):
                errors.append("compat.json 缺少 jar 信息: %s" % key)

    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description="校验 Tomorrow 兼容配置")
    parser.add_argument("directory", nargs="?", default=ROOT)
    args = parser.parse_args()
    setup_stdout()
    errors, warnings = validate_outputs(args.directory)
    for warning in warnings:
        print("[compat warning] %s" % warning)
    for error in errors:
        print("[compat error] %s" % error)
    if errors:
        return 1
    print("[compat] ok: %s" % args.directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
