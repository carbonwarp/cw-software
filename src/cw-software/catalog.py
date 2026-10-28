"""RPM catalogs.

Primary source: `dnf repoquery` (read-only, works on Atomic without root):
  - packages that provide a desktop entry  -> graphical apps
  - packages shipping /usr/bin|sbin files   -> CLI tools (only when enabled)
AppStream collections (names, icons) and rpm-ostree repo metadata are used as helpers/fallback.
Everything runs off the UI thread; results are cached in ~/.cache/software.
"""
import glob
import gzip
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

from gi.repository import GLib

CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "software")
SWCATALOG = ["/usr/share/swcatalog", "/var/lib/swcatalog", "/var/cache/swcatalog",
             "/usr/share/app-info", "/var/lib/app-info", "/var/cache/app-info"]
PRIMARY_GLOB = "/var/cache/rpm-ostree/repomd/*/repodata/*primary.xml*"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
NS = "{http://linux.duke.edu/metadata/common}"
RPM_NS = "{http://linux.duke.edu/metadata/rpm}"
SKIP_SUFFIXES = ("-devel", "-debuginfo", "-debugsource", "-static")
TTL = 12 * 3600


def in_thread(fn, callback):
    """Run fn() in a thread; callback(result_or_exception) runs on the GTK main loop."""
    def work():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001 - handed to the caller
            res = e

        def deliver():
            callback(res)
            return False
        GLib.idle_add(deliver)

    threading.Thread(target=work, daemon=True).start()


def _readable(files):
    return sorted({p for p in files if os.access(p, os.R_OK)})


def _decompress(path):
    for ext, cmd in ((".zst", ["zstd", "-dc"]), (".gz", ["gzip", "-dc"]),
                     (".xz", ["xz", "-dc"]), (".bz2", ["bzip2", "-dc"])):
        if path.endswith(ext):
            return subprocess.Popen(cmd + [path], stdout=subprocess.PIPE)
    return subprocess.Popen(["cat", path], stdout=subprocess.PIPE)


def _sig_cached(name, files, parse):
    sig = [[p, int(os.stat(p).st_mtime), os.stat(p).st_size] for p in files]
    path = os.path.join(CACHE_DIR, name + ".json")
    try:
        with open(path) as f:
            d = json.load(f)
        if d.get("sig") == sig:
            return d["data"]
    except (OSError, ValueError):
        pass
    data = parse(files)
    _dump(path, {"sig": sig, "data": data})
    return data


def _ttl_cached(name, producer, force):
    path = os.path.join(CACHE_DIR, name + ".json")
    if not force:
        try:
            if time.time() - os.path.getmtime(path) < TTL:
                with open(path) as f:
                    return json.load(f)
        except (OSError, ValueError):
            pass
    data = producer()
    if data:  # never cache an empty answer
        _dump(path, data)
    return data


def _dump(path, obj):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f)
    except OSError:
        pass


# ---------- dnf repoquery ----------

def dnf_available():
    return shutil.which("dnf") is not None


def _repoquery(extra, refresh=False):
    cmd = ["dnf", "-q"] + (["--refresh"] if refresh else []) + [
        "repoquery", "--available", "--latest-limit=1",
        f"--arch={platform.machine()},noarch", *extra,
        "--queryformat", "%{name}\t%{summary}\n"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip().splitlines()[-1] if res.stderr.strip()
                           else "dnf repoquery failed")
    rows, seen = [], set()
    for line in res.stdout.splitlines():
        name, _, summary = line.partition("\t")
        name = name.strip()
        if name and name not in seen and not name.endswith(SKIP_SUFFIXES):
            seen.add(name)
            rows.append([name, summary.strip()])
    return rows


def _query_gui(refresh):
    rows = _repoquery(["--whatprovides", "application(*)"], refresh)
    if not rows:  # older packaging without application() provides
        rows = _repoquery(["--file", "/usr/share/applications/*.desktop"], refresh)
    return rows


def load_dnf(show_cli, force=False):
    """Rows [name, summary, is_gui, is_cli]. force=True re-queries (and refreshes dnf metadata)."""
    rows = {n: [n, s, 1, 0] for n, s in _ttl_cached("dnf-gui", lambda: _query_gui(force), force)}
    if show_cli:
        cli = _ttl_cached("dnf-cli", lambda: _repoquery(["--file", "/usr/bin/*", "/usr/sbin/*"], force),
                          force)
        for n, s in cli:
            rows.setdefault(n, [n, s, 0, 1])
    return list(rows.values())


def dnf_upgrades(refresh=False):
    """Names of installed packages that have a newer version in the repos. Blocking: use in_thread."""
    cmd = ["dnf", "-q"] + (["--refresh"] if refresh else []) + [
        "repoquery", "--upgrades", "--latest-limit=1",
        f"--arch={platform.machine()},noarch", "--queryformat", "%{name}\n"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip().splitlines()[-1] if res.stderr.strip()
                           else "dnf repoquery --upgrades failed")
    return sorted({l.strip() for l in res.stdout.splitlines() if l.strip()})


_TERM_OK = re.compile(r"^[A-Za-z0-9._+-]{2,64}$")


def search_rpm(term):
    """Live lookup of RPMs whose name contains `term` (works even when the cached index is
    stale or missing a package). Read-only. Returns [[name, summary], ...]. Blocking."""
    term = term.strip()
    if not _TERM_OK.match(term) or not dnf_available():
        return []
    return _repoquery([f"*{term}*"])


# ---------- AppStream (pretty names, icons) ----------

def appstream_files():
    files = []
    for base in SWCATALOG:
        for sub in ("xml", "xmls"):
            files += glob.glob(f"{base}/{sub}/*.xml*")
    return _readable(files)


def _plain(el, tag):
    for c in el.findall(tag):
        if XML_LANG not in c.attrib:
            return (c.text or "").strip()
    return ""


def _parse_appstream(files):
    apps = {}
    for path in files:
        origin = ""
        try:
            proc = _decompress(path)
        except OSError:
            continue
        try:
            for event, el in ET.iterparse(proc.stdout, events=("start", "end")):
                if event == "start":
                    if el.tag == "components":
                        origin = el.get("origin", "")
                    continue
                if el.tag != "component":
                    continue
                ctype = el.get("type", "")
                pkg = el.findtext("pkgname")
                name = _plain(el, "name")
                if ctype in ("desktop-application", "console-application") and pkg and name:
                    cid = (el.findtext("id") or "").strip()
                    if cid.endswith(".desktop"):
                        cid = cid[:-8]
                    icon = ""
                    for ic in el.findall("icon"):
                        if ic.get("type") == "cached":
                            icon = (ic.text or "").strip()
                            break
                    gui = ctype == "desktop-application"
                    cats = [(c.text or "").strip() for c in el.findall("categories/category")]
                    if pkg not in apps or (gui and not apps[pkg]["gui"]):
                        apps[pkg] = {"pkg": pkg, "id": cid, "name": name,
                                     "summary": _plain(el, "summary"),
                                     "icon": icon, "origin": origin, "gui": gui,
                                     "cats": cats}
                el.clear()
        except ET.ParseError:
            pass
        finally:
            proc.stdout.close()
            proc.wait()
    return list(apps.values())


def load_appstream():
    return _sig_cached("appstream2", appstream_files(), _parse_appstream)


def appstream_icon(origin, icon):
    if not icon:
        return None
    if os.path.isabs(icon):
        return icon if os.path.exists(icon) else None
    for base in SWCATALOG:
        for size in ("128x128", "64x64", "48x48"):
            p = f"{base}/icons/{origin}/{size}/{icon}"
            if os.path.exists(p):
                return p
    for base in SWCATALOG:
        for size in ("128x128", "64x64", "48x48"):
            hits = glob.glob(f"{base}/icons/*/{size}/{icon}")
            if hits:
                return hits[0]
    return None


def flatpak_appstream_files(remote):
    arch = platform.machine()
    files = []
    for base in ("/var/lib/flatpak", os.path.expanduser("~/.local/share/flatpak")):
        for fname in ("appstream.xml.gz", "appstream.xml"):
            files.append(f"{base}/appstream/{remote}/{arch}/active/{fname}")
    return _readable(files)


def _parse_flatpak_categories(files):
    cats = {}
    for path in files:
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rb") as f:
                for _ev, el in ET.iterparse(f, events=("end",)):
                    if el.tag != "component":
                        continue
                    cid = (el.findtext("id") or "").strip()
                    if cid.endswith(".desktop"):
                        cid = cid[:-8]
                    found = [(c.text or "").strip() for c in el.findall("categories/category")]
                    if cid and found:
                        cats[cid.lower()] = found
                    el.clear()
        except (OSError, EOFError, ET.ParseError):
            continue
    return cats


def load_flatpak_categories(remote):
    """{app id (lower-case): [AppStream categories]} for the remote. Cached; blocking."""
    return _sig_cached("flatpak-categories", flatpak_appstream_files(remote),
                       _parse_flatpak_categories)


# ---------- rpm-ostree repo metadata (fallback when dnf is missing) ----------

def primary_files():
    return _readable(glob.glob(PRIMARY_GLOB))


def _parse_primary(files):
    pkgs = {}
    for path in files:
        proc = _decompress(path)
        try:
            for _ev, el in ET.iterparse(proc.stdout, events=("end",)):
                if el.tag != NS + "package":
                    continue
                name = el.findtext(NS + "name") or ""
                if (name and el.findtext(NS + "arch") != "src"
                        and not name.endswith(SKIP_SUFFIXES) and name not in pkgs):
                    gui = cli = False
                    fmt = el.find(NS + "format")
                    if fmt is not None:
                        prov = fmt.find(RPM_NS + "provides")
                        if prov is not None:
                            gui = any((e.get("name") or "").startswith("application(") for e in prov)
                        for f in fmt.findall(NS + "file"):
                            t = f.text or ""
                            if "/bin/" in t or "/sbin/" in t:
                                cli = True
                                break
                    if gui or cli:
                        pkgs[name] = [name, (el.findtext(NS + "summary") or "").strip(),
                                      int(gui), int(cli)]
                el.clear()
        except ET.ParseError:
            pass
        finally:
            proc.stdout.close()
            proc.wait()
    return list(pkgs.values())


def load_primary():
    return _sig_cached("primary", primary_files(), _parse_primary)


# ---------- details: Flatpak AppStream component, RPM package info ----------

def _desc_text(el):
    d = next((c for c in el.findall("description") if XML_LANG not in c.attrib), None)
    if d is None:
        return ""
    parts = []
    for ch in d:
        if XML_LANG in ch.attrib:  # translated paragraph (older AppStream style)
            continue
        if ch.tag == "p":
            t = " ".join("".join(ch.itertext()).split())
            if t:
                parts.append(t)
        elif ch.tag in ("ul", "ol"):
            items = ["\u2022 " + " ".join("".join(li.itertext()).split()) for li in ch.findall("li") if XML_LANG not in li.attrib]
            if items:
                parts.append("\n".join(items))
    return "\n\n".join(parts)


def _screenshots(el):
    """Image URLs of an AppStream component: ~750px thumbnail when offered, else the source image."""
    out = []
    for sc in el.findall("screenshots/screenshot"):
        imgs = []
        for im in sc.findall("image"):
            url = (im.text or "").strip()
            if url.startswith("https://"):
                try:
                    width = int(im.get("width") or 0)
                except ValueError:
                    width = 0
                imgs.append((width, url, im.get("type", "source")))
        if not imgs:
            continue
        thumbs = [i for i in imgs if i[2] == "thumbnail" and i[0] >= 500]
        out.append(min(thumbs)[1] if thumbs else max(imgs)[1])
    return out


def rpm_screenshots(pkg):
    """Screenshot URLs of an RPM app from the system AppStream catalog. Blocking."""
    for path in appstream_files():
        try:
            proc = _decompress(path)
        except OSError:
            continue
        try:
            for _ev, el in ET.iterparse(proc.stdout, events=("end",)):
                if el.tag != "component":
                    continue
                if el.findtext("pkgname") == pkg:
                    shots = _screenshots(el)
                    if shots:
                        return shots
                el.clear()
        except ET.ParseError:
            pass
        finally:
            proc.kill()
            proc.stdout.close()
            proc.wait()
    return []


def _component_details(el):
    dev = _plain(el, "developer_name")
    if not dev:
        d = el.find("developer")
        if d is not None:
            dev = _plain(d, "name")
    rel = el.find("releases/release")
    return {
        "name": _plain(el, "name"),
        "summary": _plain(el, "summary"),
        "description": _desc_text(el),
        "developer": dev,
        "license": (el.findtext("project_license") or "").strip(),
        "urls": {u.get("type", ""): (u.text or "").strip() for u in el.findall("url")},
        "categories": [(c.text or "").strip() for c in el.findall("categories/category")],
        "screenshots": _screenshots(el),
        "release": (rel.get("version", ""), rel.get("date", "") or rel.get("timestamp", ""))
                   if rel is not None else None,
    }


_FP_META = {}


def flatpak_component(remote, app_id):
    """Details of a Flatpak app from the remote's AppStream cache, or None. Blocking: use in_thread."""
    key = (remote, app_id)
    if key in _FP_META:
        return _FP_META[key]
    arch = platform.machine()
    for base in ("/var/lib/flatpak", os.path.expanduser("~/.local/share/flatpak")):
        for fname in ("appstream.xml.gz", "appstream.xml"):
            path = f"{base}/appstream/{remote}/{arch}/active/{fname}"
            if not os.path.exists(path):
                continue
            opener = gzip.open if path.endswith(".gz") else open
            try:
                with opener(path, "rb") as f:
                    for _ev, el in ET.iterparse(f, events=("end",)):
                        if el.tag != "component":
                            continue
                        cid = (el.findtext("id") or "").strip()
                        if cid in (app_id, app_id + ".desktop"):
                            _FP_META[key] = _component_details(el)
                            return _FP_META[key]
                        el.clear()
            except (OSError, EOFError, ET.ParseError):
                continue
    _FP_META[key] = None
    return None


def rpm_info_argv(pkg, installed):
    """Command printing package info: rpm -qi for installed, dnf repoquery --info otherwise."""
    if installed:
        return ["rpm", "-qi", pkg]
    if not dnf_available():
        return None
    return ["dnf", "-q", "repoquery", "--info", "--latest-limit=1",
            f"--arch={platform.machine()},noarch", pkg]


def _paragraphs(text):
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    return "\n\n".join(p for p in paras if p)


def parse_pkg_info(text):
    """Parse `rpm -qi` / `dnf repoquery --info` output into a dict (lower-case keys)."""
    info, last, in_desc, desc = {}, None, False, []
    for line in text.splitlines():
        if in_desc:
            desc.append(re.sub(r"^\s+:\s?", "", line))
            continue
        m = re.match(r"^([A-Za-z][A-Za-z ./-]*?)\s*:\s?(.*)$", line)
        if m:
            key, val = m.group(1).strip().lower(), m.group(2).strip()
            if key == "description":
                in_desc = True
                if val:
                    desc.append(val)
                continue
            info.setdefault(key, val)
            last = key
        elif last and line.strip():
            info[last] = (info[last] + " " + re.sub(r"^\s*:\s?", "", line).strip()).strip()
    info["description"] = _paragraphs("\n".join(desc))
    return info
