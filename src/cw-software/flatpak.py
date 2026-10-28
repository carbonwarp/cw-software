"""Flatpak CLI wrappers. Never needs a reboot."""
import os
import platform
import re
import shutil

from .runner import capture


def available():
    return shutil.which("flatpak") is not None


def refresh_appstream_argv(scope):
    """Re-download the remote's AppStream data (names, descriptions, screenshot URLs)."""
    return ["flatpak", "update", *_scope(scope), "--appstream", "-y", "--noninteractive"]


def valid_term(term):
    return bool(term) and not term.startswith("-")


def _scope(scope):
    return ["--user"] if scope == "user" else ["--system"]


def _parse(out):
    apps, seen = [], set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        app_id = parts[0].strip()
        if "." not in app_id or " " in app_id or app_id in seen:
            continue  # header, junk or duplicate branch
        seen.add(app_id)
        desc = parts[2].strip() if len(parts) > 2 else ""
        apps.append((app_id, parts[1].strip(), desc))
    return apps


def list_remote(scope, remote, callback):
    """All apps of a remote (local cache). callback([(app_id, name, description)])."""
    argv = ["flatpak", "remote-ls", *_scope(scope), "--app", "--cached",
            "--columns=application,name,description", remote]
    capture(argv, lambda st, out, err: callback(_parse(out)))


def installed_ids(scope, callback):
    """callback(set_of_app_ids)"""
    def done(status, out, err):
        callback({l.strip() for l in out.splitlines() if "." in l and " " not in l.strip()})
    capture(["flatpak", "list", "--app", *_scope(scope), "--columns=application"], done)


def install_argv(app_id, remote, scope):
    return ["flatpak", "install", *_scope(scope), "-y", "--noninteractive", remote, app_id]


def update_argv(app_id, scope):
    return ["flatpak", "update", *_scope(scope), "-y", "--noninteractive", app_id]


def updates(scope, callback):
    """Apps with a pending update, straight from the remotes (network).
    callback(set_of_app_ids) or callback(None) when the check failed."""
    def done(status, out, err):
        if status != 0:
            callback(None)
            return
        callback({l.strip() for l in out.splitlines() if "." in l and " " not in l.strip()})
    capture(["flatpak", "remote-ls", *_scope(scope), "--updates", "--app",
             "--columns=application"], done, env={"LC_ALL": "C"})


def uninstall_argv(app_id, scope):
    return ["flatpak", "uninstall", *_scope(scope), "-y", "--noninteractive", app_id]


def icon_path(app_id, remote, scope):
    """Cached AppStream icon of a Flatpak app, or None."""
    arch = platform.machine()
    system = "/var/lib/flatpak"
    user = os.path.expanduser("~/.local/share/flatpak")
    for base in ([user, system] if scope == "user" else [system, user]):
        for size in ("128x128", "64x64", "48x48"):
            p = f"{base}/appstream/{remote}/{arch}/active/icons/{size}/{app_id}.png"
            if os.path.exists(p):
                return p
    return None


def remote_info(scope, remote, app_id, callback):
    """`flatpak remote-info` as a dict with lower-case keys (version, license, download, installed...)."""
    def done(status, out, err):
        info = {}
        if status == 0:
            for line in out.splitlines():
                m = re.match(r"^\s*([A-Za-z][A-Za-z ]*?):\s*(.+)$", line)
                if m:
                    info.setdefault(m.group(1).strip().lower(), m.group(2).strip())
        callback(info)
    capture(["flatpak", "remote-info", *_scope(scope), remote, app_id], done, env={"LC_ALL": "C"})
