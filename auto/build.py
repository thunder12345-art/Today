import argparse
import os
import shutil
import sys
import tempfile

import validate
from common import (BASE, DB_PATH, OUT_DIR, PHONE_TOKEN_URL, PUBLIC_JAR_URLS,
                    PUBLIC_SPIDER_URL, ROOT, file_md5, is_whitelisted, jar_md5,
                    load_json, load_json_safe, load_whitelist, now_iso,
                    rewrite_token_parts, save_json, setup_stdout, strip_meta,
                    with_jar_md5)

SETTINGS_PATH = os.path.join(BASE, "settings.json")


def apply_public_jars(config, jar_urls, spider_url, hashes=None):
    sites = []
    for item in config.get("sites") or []:
        entry = item
        jar = entry.get("jar")
        if isinstance(jar, str):
            core = jar.strip().split(";md5;", 1)[0].split("?", 1)[0]
            name = core.rsplit("/", 1)[-1]
            public_jar = jar_urls.get(name)
            if public_jar:
                entry = dict(entry)
                entry["jar"] = with_jar_md5(public_jar, name, hashes, replace=True)
        sites.append(entry)
    result = dict(config)
    result["sites"] = sites
    spider = result.get("spider")
    if isinstance(spider, str) and spider.strip().split(";md5;", 1)[0].rsplit("/", 1)[-1] \
            in ("tvbox.jar", "spider.jar"):
        result["spider"] = with_jar_md5(jar_urls.get("spider.jar", spider_url),
                                        "spider.jar", hashes, replace=True)
    return result


def apply_remote_base(config, base, hashes=None):
    base = (base or "").strip()
    if not base:
        return config
    token_url = base.rstrip("/") + "/token.json"
    sites = []
    for item in config.get("sites") or []:
        entry = item
        ext = entry.get("ext")
        new_ext = rewrite_token_parts(ext, token_url)
        if new_ext != ext:
            entry = dict(entry)
            entry["ext"] = new_ext
        sites.append(entry)
    result = dict(config)
    result["sites"] = sites
    return apply_public_jars(result, PUBLIC_JAR_URLS, PUBLIC_SPIDER_URL, hashes)


def apply_token_url(config, token_url):
    if not token_url:
        return config
    sites = []
    for item in config.get("sites") or []:
        entry = item
        ext = entry.get("ext")
        new_ext = rewrite_token_parts(ext, token_url)
        if new_ext != ext:
            entry = dict(entry)
            entry["ext"] = new_ext
        sites.append(entry)
    result = dict(config)
    result["sites"] = sites
    return result


def apply_jar_md5(config, hashes=None):
    sites = []
    for item in config.get("sites") or []:
        entry = item
        jar = entry.get("jar")
        if isinstance(jar, str):
            core = jar.strip().split(";md5;", 1)[0].split("?", 1)[0]
            name = core.rsplit("/", 1)[-1]
            if name in ("spider.jar", "pg_upgraded.jar"):
                entry = dict(entry)
                entry["jar"] = with_jar_md5(jar, name, hashes, replace=True)
        sites.append(entry)
    result = dict(config)
    result["sites"] = sites
    spider = result.get("spider")
    if isinstance(spider, str) and spider.strip().split(";md5;", 1)[0].endswith("tvbox.jar"):
        result["spider"] = with_jar_md5("./jar/spider.jar", "spider.jar", hashes,
                                        replace=True)
    return result


def apply_local_jars(config, hashes=None):
    sites = []
    for item in config.get("sites") or []:
        entry = item
        jar = entry.get("jar")
        if isinstance(jar, str):
            core = jar.strip().split(";md5;", 1)[0].split("?", 1)[0]
            name = core.rsplit("/", 1)[-1]
            if name == "tvbox.jar":
                entry = dict(entry)
                entry["jar"] = with_jar_md5("./tvbox.jar", name, hashes, replace=True)
            elif name in ("spider.jar", "pg_upgraded.jar"):
                entry = dict(entry)
                entry["jar"] = with_jar_md5("./jar/" + name, name, hashes,
                                            replace=True)
        sites.append(entry)
    result = dict(config)
    result["sites"] = sites
    spider = result.get("spider")
    if isinstance(spider, str) and spider.strip().split(";md5;", 1)[0].endswith("tvbox.jar"):
        result["spider"] = with_jar_md5("./jar/spider.jar", "spider.jar", hashes,
                                        replace=True)
    return result


def apply_phone_mode(config, hashes=None):
    sites = []
    for item in config.get("sites") or []:
        entry = item
        ext = entry.get("ext")
        new_ext = rewrite_token_parts(ext, PHONE_TOKEN_URL)
        if new_ext != ext:
            entry = dict(entry)
            entry["ext"] = new_ext
        sites.append(entry)
    result = dict(config)
    result["sites"] = sites
    return apply_local_jars(result, hashes)


def apply_github_mode(config, hashes=None):
    return apply_public_jars(apply_phone_mode(config, hashes),
                             PUBLIC_JAR_URLS, PUBLIC_SPIDER_URL, hashes)


def apply_legacy_mode(config, hashes=None):
    result = apply_local_jars(config, hashes)
    result["spider"] = with_jar_md5("./tvbox.jar", "tvbox.jar", hashes,
                                    replace=True)
    return result


def _jar_meta(name):
    candidates = (os.path.join(ROOT, name), os.path.join(ROOT, "jar", name))
    path = next((item for item in candidates if os.path.exists(item)), "")
    return {
        "name": name,
        "path": os.path.relpath(path, ROOT).replace("\\", "/") if path else "",
        "md5": file_md5(path) if path else "",
        "size": os.path.getsize(path) if path else 0,
    }


def build_compat(configs, hashes):
    return {
        "schema": 1,
        "product": "Tomorrow",
        "generated_at": now_iso(),
        "profiles": {
            "lan": {
                "path": "/tvbox",
                "file": "tvbox.json",
                "clients": "FongMi/TVBox 5.8.9+，局域网",
                "spider": configs["lan"].get("spider", ""),
                "jar": "jar/spider.jar",
            },
            "phone": {
                "path": "/tvbox.phone",
                "file": "tvbox.phone.json",
                "clients": "手机/外网，NAS 域名可用",
                "spider": configs["phone"].get("spider", ""),
                "jar": "jar/spider.jar",
            },
            "github": {
                "path": "/tvbox.github",
                "file": "tvbox.github.json",
                "clients": "NAS/域名不可用时的 GitHub 兜底",
                "spider": configs["github"].get("spider", ""),
                "jar": "jar/spider.jar",
            },
            "legacy": {
                "path": "/tvbox.legacy",
                "file": "tvbox.legacy.json",
                "clients": "旧版 TVBox/FongMi 5.8.5 及以下，仅局域网",
                "spider": configs["legacy"].get("spider", ""),
                "jar": "tvbox.jar",
            },
        },
        "jars": {
            "spider.jar": _jar_meta("spider.jar"),
            "pg_upgraded.jar": _jar_meta("pg_upgraded.jar"),
            "tvbox.jar": _jar_meta("tvbox.jar"),
        },
        "hashes": dict(hashes or {}),
    }


def apply_run_meta(config, run_id=None, epoch=None):
    run_id = (run_id if run_id is not None else os.environ.get("PIPELINE_RUN_ID") or "").strip()
    if not run_id:
        return config
    epoch_value = epoch
    if epoch_value is None:
        epoch_value = os.environ.get("PIPELINE_EPOCH") or ""
    meta = config.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    meta = dict(meta)
    meta["run_id"] = run_id
    meta["generated_at"] = now_iso()
    try:
        meta["epoch"] = int(epoch_value)
    except (TypeError, ValueError):
        pass
    result = dict(config)
    result["_meta"] = meta
    return result


def is_local_origin(origin):
    if origin in (None, "local", "own"):
        return True
    return isinstance(origin, str) and origin.startswith("feed")


def collect_entries(db, group, scope, drop_dead, whitelist):
    result = []
    excluded = 0
    for item in db.get(group, []):
        meta = item.get("_meta") or {}
        origin = meta.get("origin")
        if scope == "local" and not is_local_origin(origin):
            continue
        if meta.get("blocked"):
            excluded += 1
            continue
        if drop_dead and (meta.get("last_check") or {}).get("ok") is False \
                and not is_whitelisted(whitelist, group, item):
            excluded += 1
            continue
        result.append(strip_meta(item))
    return result, excluded


def main():
    parser = argparse.ArgumentParser(description="Regenerate tvbox.json / lmw.json / caiji.json from auto/sources.json")
    parser.add_argument("--out", default=OUT_DIR, help="output directory (default auto/out)")
    parser.add_argument("--inplace", action="store_true", help="overwrite the repo files directly")
    parser.add_argument("--scope", choices=["local", "all"], default="local",
                        help="local = repo originals only, all = include submitted entries")
    parser.add_argument("--drop-dead", action="store_true", help="exclude entries whose last check failed")
    args = parser.parse_args()
    setup_stdout()

    db = load_json(DB_PATH)
    if db is None:
        print("sources.json not found, run first: python auto/seed.py")
        return

    whitelist = load_whitelist()

    tvbox_file = load_json_safe(os.path.join(ROOT, "tvbox.json"), {}) or {}
    caiji = load_json_safe(os.path.join(ROOT, "caiji.json"), {}) or {}
    template = db.get("template")
    if isinstance(template, dict) and template:
        tvbox = dict(template)
    else:
        tvbox = dict(tvbox_file)

    sites, blocked_sites = collect_entries(db, "sites", args.scope, args.drop_dead, whitelist)
    caiji_sites, blocked_caiji = collect_entries(db, "caiji", args.scope, args.drop_dead, whitelist)
    tvbox["sites"] = sites
    caiji["sites"] = caiji_sites

    blocked_parses = set(db.get("blocked_parses") or [])
    parses = db.get("parses")
    if parses is None:
        parses = tvbox_file.get("parses") or []
    parses = [strip_meta(item) for item in parses if isinstance(item, dict)]
    parses_total = len(parses)
    if blocked_parses:
        parses = [item for item in parses if item.get("url") not in blocked_parses]
    tvbox["parses"] = parses
    parses_excluded = parses_total - len(parses)

    repos = []
    blocked_repos = 0
    for item in db.get("repos", []):
        meta = item.get("_meta") or {}
        origin = meta.get("origin")
        if args.scope == "local" and not is_local_origin(origin):
            continue
        if meta.get("blocked"):
            blocked_repos += 1
            continue
        if args.drop_dead and (meta.get("last_check") or {}).get("ok") is False \
                and not is_whitelisted(whitelist, "repos", item):
            blocked_repos += 1
            continue
        repos.append({"url": item.get("url", ""), "name": item.get("name", "")})
    lmw = {"urls": repos}

    inplace = bool(args.inplace)
    work_dir = tempfile.mkdtemp(prefix=".build-", dir=BASE) if inplace else args.out
    os.makedirs(work_dir, exist_ok=True)
    tvbox.pop("_meta", None)
    hashes = {name: jar_md5(name) for name in ("spider.jar", "pg_upgraded.jar",
                                               "tvbox.jar")}
    settings = load_json(SETTINGS_PATH, {}) or {}
    center_token = str(settings.get("center_token_url") or "").strip() \
        if isinstance(settings, dict) else ""
    tvbox = apply_token_url(tvbox, center_token)
    tvbox = apply_jar_md5(tvbox, hashes)
    remote_base = str(settings.get("remote_base_url") or "").strip() if isinstance(settings, dict) else ""

    configs = {
        "lan": tvbox,
        "phone": apply_phone_mode(tvbox, hashes),
        "github": apply_github_mode(tvbox, hashes),
        "legacy": apply_legacy_mode(tvbox, hashes),
    }
    outputs = {
        "tvbox.json": configs["lan"],
        "tvbox.phone.json": configs["phone"],
        "tvbox.github.json": configs["github"],
        "tvbox.legacy.json": configs["legacy"],
        "caiji.json": caiji,
        "lmw.json": lmw,
        "compat.json": build_compat(configs, hashes),
    }
    if remote_base:
        outputs["tvbox.remote.json"] = apply_remote_base(tvbox, remote_base, hashes)

    for name, data in outputs.items():
        save_json(os.path.join(work_dir, name), data)

    errors, warnings = validate.validate_outputs(work_dir)
    for warning in warnings:
        print("[compat warning] %s" % warning)
    if errors:
        for error in errors:
            print("[compat error] %s" % error)
        print("[build rejected] 配置未通过兼容性校验，旧文件保持不变")
        if inplace:
            shutil.rmtree(work_dir, ignore_errors=True)
        return 1

    if inplace:
        for name in outputs:
            os.replace(os.path.join(work_dir, name), os.path.join(ROOT, name))
        shutil.rmtree(work_dir, ignore_errors=True)
        out_dir = ROOT
    else:
        out_dir = work_dir

    print("built -> %s" % out_dir)
    print("tvbox.json sites=%d | caiji.json sites=%d | lmw.json urls=%d"
          % (len(tvbox["sites"]), len(caiji["sites"]), len(repos)))
    print("profiles: lan phone github legacy | compat.json")
    if blocked_sites or blocked_caiji or blocked_repos or parses_excluded:
        print("excluded: sites=%d caiji=%d repos=%d parses=%d (blocked / drop-dead)"
              % (blocked_sites, blocked_caiji, blocked_repos, parses_excluded))
    return 0


if __name__ == "__main__":
    sys.exit(main())
