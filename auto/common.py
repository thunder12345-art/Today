import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
DB_PATH = os.path.join(BASE, "sources.json")
INBOX_DIR = os.path.join(BASE, "inbox")
OUT_DIR = os.path.join(BASE, "out")
REPORT_TXT = os.path.join(BASE, "report.txt")
REPORT_JSON = os.path.join(BASE, "report.json")

UA = ("Mozilla/5.0 (Linux; Android 13; V2049A Build/TP1A.220624.014; wv) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def setup_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=4)
        handle.write("\n")
    os.replace(temp, path)


def is_http(value):
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def wire_url(url):
    parts = urllib.parse.urlsplit(url.strip())
    host = parts.hostname or ""
    if any(ord(ch) > 127 for ch in host):
        host = host.encode("idna").decode("ascii")
    netloc = host
    if parts.port:
        netloc = "%s:%d" % (host, parts.port)
    path = urllib.parse.quote(parts.path, safe="/%:@&=+$,-_.!~*'()")
    query = urllib.parse.quote(parts.query, safe="/%:@&=+$,-_.!~*'()?")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


def http_fetch(url, timeout=10, max_bytes=1048576):
    request = urllib.request.Request(wire_url(url), headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout, context=SSL_CTX) as response:
        return response.getcode(), response.read(max_bytes)


def _http_check_once(url, timeout=8):
    start = time.time()
    try:
        code, _ = http_fetch(url, timeout=timeout, max_bytes=8192)
        return {"ok": 200 <= code < 400, "code": code,
                "latency_ms": int((time.time() - start) * 1000), "url": url}
    except urllib.error.HTTPError as error:
        return {"ok": False, "code": error.code,
                "latency_ms": int((time.time() - start) * 1000), "url": url,
                "detail": "HTTP %s" % error.code}
    except Exception as error:
        return {"ok": False, "code": None,
                "latency_ms": int((time.time() - start) * 1000), "url": url,
                "detail": "%s: %s" % (type(error).__name__, str(error)[:160])}


def http_check(url, timeout=8, retries=1):
    result = _http_check_once(url, timeout)
    for _ in range(retries):
        if result["ok"] or result["code"] is not None:
            return result
        time.sleep(0.5)
        result = _http_check_once(url, timeout)
    return result


def cms_check_url(api):
    if not is_http(api):
        return api
    if api.endswith(("?", "&")):
        return api + "ac=list"
    if "?" in api:
        return api + "&ac=list"
    return api + "?ac=list"


def derive_targets(site):
    found = []

    def add(value):
        if is_http(value):
            normalized = value.strip()
            if normalized not in found:
                found.append(normalized)

    add(site.get("api"))
    ext = site.get("ext")
    if is_http(ext):
        add(ext)
    elif isinstance(ext, dict):
        site_url = ext.get("site")
        if isinstance(site_url, list):
            for item in site_url:
                add(item)
        else:
            add(site_url)
        add(ext.get("url"))
    return found


def norm_key(site):
    for field in ("key", "api", "name"):
        value = site.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def norm_target(url):
    try:
        parts = urllib.parse.urlsplit(str(url).strip().lower())
        host = parts.hostname or ""
        netloc = host
        if parts.port:
            netloc = "%s:%d" % (host, parts.port)
        return netloc + parts.path.rstrip("/")
    except Exception:
        return str(url).strip().lower()


def strip_meta(obj):
    return {key: value for key, value in obj.items() if not key.startswith("_")}


def build_meta(old_meta, origin, targets=None):
    meta = dict(old_meta) if isinstance(old_meta, dict) else {}
    meta.setdefault("origin", origin)
    meta.setdefault("added_at", now_iso())
    if targets is not None:
        meta["targets"] = targets
    return meta


def coerce_site(raw):
    if not isinstance(raw, dict):
        return None, "not a JSON object"
    site = {key: value for key, value in raw.items() if not key.startswith("_")}
    warnings = []
    if not isinstance(site.get("name"), str) or not site.get("name", "").strip():
        return None, "missing name"
    if not site.get("api") and not site.get("ext"):
        return None, "missing api/ext"
    if "type" not in site:
        site["type"] = 1 if is_http(site.get("api")) else 3
        warnings.append("type inferred as %s" % site["type"])
    if not isinstance(site.get("key"), str) or not site["key"].strip():
        if isinstance(site.get("api"), str) and site["api"].strip():
            site["key"] = site["api"].strip()
            warnings.append("key filled from api")
        else:
            site["key"] = site["name"].strip()
            warnings.append("key filled from name")
    return site, "; ".join(warnings)


def extract_entries(data):
    sites = []
    repos = []
    if isinstance(data, dict):
        raw_sites = data.get("sites")
        if isinstance(raw_sites, list):
            sites.extend([item for item in raw_sites if isinstance(item, dict)])
        raw_urls = data.get("urls")
        if isinstance(raw_urls, list):
            for item in raw_urls:
                if isinstance(item, dict) and is_http(item.get("url")):
                    repos.append({"url": item["url"].strip(), "name": item.get("name", "")})
                elif is_http(item):
                    repos.append({"url": item.strip(), "name": ""})
        if not raw_sites and not raw_urls and data.get("name"):
            sites.append(data)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and is_http(item.get("url")) and not item.get("api") and not item.get("ext"):
                repos.append({"url": item["url"].strip(), "name": item.get("name", "")})
            elif isinstance(item, dict):
                sites.append(item)
    return sites, repos


def find_duplicate(db, site):
    key = norm_key(site)
    targets = {norm_target(item) for item in derive_targets(site)}
    for group in ("sites", "caiji"):
        for existing in db.get(group, []):
            if key and norm_key(existing) == key:
                return group, existing, "key=%s" % key
            existing_targets = {norm_target(item) for item in derive_targets(existing)}
            overlap = targets & existing_targets
            if overlap:
                return group, existing, "target=%s" % ", ".join(sorted(overlap))
    return None


CMS_MARKERS = ('"class"', "<class>", "<?xml", "<rss", "<list")


def _cms_probe_once(api, timeout=8):
    probe = cms_check_url(api)
    start = time.time()
    try:
        code, body = http_fetch(probe, timeout=timeout, max_bytes=65536)
        result = {"ok": 200 <= code < 400, "code": code,
                  "latency_ms": int((time.time() - start) * 1000),
                  "url": probe, "target": api}
        if result["ok"]:
            text = body.decode("utf-8", "replace").lower()
            if not any(marker in text for marker in CMS_MARKERS):
                result["suspect"] = True
                result["detail"] = "reachable but no CMS markers (%d bytes)" % len(body)
        return result
    except urllib.error.HTTPError as error:
        return {"ok": False, "code": error.code,
                "latency_ms": int((time.time() - start) * 1000),
                "url": probe, "target": api, "detail": "HTTP %s" % error.code}
    except Exception as error:
        return {"ok": False, "code": None,
                "latency_ms": int((time.time() - start) * 1000),
                "url": probe, "target": api,
                "detail": "%s: %s" % (type(error).__name__, str(error)[:160])}


def cms_probe(api, timeout=8, retries=1):
    result = _cms_probe_once(api, timeout)
    for _ in range(retries):
        if result["ok"] or result["code"] is not None or result.get("suspect"):
            return result
        time.sleep(0.5)
        result = _cms_probe_once(api, timeout)
    return result


def check_entry(site, timeout=8, max_targets=3, extra_targets=None):
    targets = derive_targets(site)
    source = "ext"
    if not targets and extra_targets:
        targets = list(extra_targets)
        source = "jar"
    if not targets:
        return {"ok": None, "detail": "no_target", "checked_at": now_iso()}
    results = []
    for target in targets[:max_targets]:
        if site.get("type") == 1:
            result = cms_probe(target, timeout=timeout)
        else:
            result = http_check(target, timeout=timeout)
            result["target"] = target
        results.append(result)
        if result["ok"] and not result.get("suspect"):
            break
    last = results[-1]
    entry = {"ok": any(item["ok"] for item in results), "checked_at": now_iso(),
             "code": last.get("code"), "latency_ms": last.get("latency_ms"),
             "url": last.get("url"), "detail": last.get("detail"),
             "target_source": source,
             "targets_total": len(targets), "results": results}
    if any(item.get("suspect") for item in results):
        entry["suspect"] = True
    return entry


def check_repo(repo, timeout=8):
    result = http_check(repo.get("url", ""), timeout=timeout)
    result["checked_at"] = now_iso()
    return result
