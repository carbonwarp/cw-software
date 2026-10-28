"""Screenshots for the app page.

URLs come from AppStream (Flathub's remote metadata for Flatpak apps, the swcatalog for RPM
apps) and the images are downloaded once into ~/.cache/software/screenshots. Nothing here
refreshes on its own: `clear()` is called only by Menu > Refresh Package Data.
"""
import hashlib
import json
import os
import shutil
import urllib.request

from gi.repository import GLib

CACHE = os.path.join(GLib.get_user_cache_dir(), "software", "screenshots")
MAX_SHOTS = 6
TIMEOUT = 20


def clear():
    shutil.rmtree(CACHE, ignore_errors=True)


def _name(text):
    return hashlib.sha1(text.encode()).hexdigest()


def urls(key, finder):
    """Screenshot URLs for `key`; looked up once via finder() and remembered until clear()."""
    path = os.path.join(CACHE, _name(key) + ".json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    found = list(finder() or [])[:MAX_SHOTS]
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(path, "w") as f:
            json.dump(found, f)
    except OSError:
        pass
    return found


def fetch(url):
    """Local file for an image URL (downloaded on first use), or None."""
    if not url.startswith("https://"):
        return None
    path = os.path.join(CACHE, _name(url) + ".img")
    if os.path.exists(path):
        return path
    try:
        os.makedirs(CACHE, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "com.carbonwarp.software"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read(20 * 1024 * 1024)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path
    except (OSError, ValueError):
        return None


def load(key, finder):
    """Blocking: local image files for `key`. Use catalog.in_thread."""
    return [p for p in (fetch(u) for u in urls(key, finder)) if p]
