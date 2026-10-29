"""rpm-ostree CLI wrappers. Auth via rpm-ostreed/polkit. Every change needs a reboot."""
import json
import re
import shutil

from .runner import capture


def available():
    return shutil.which("rpm-ostree") is not None


def valid_pkg(name):
    return bool(name) and not name.startswith("-") and " " not in name


def install_argv(pkg):
    return ["rpm-ostree", "install", "-y", "--idempotent", pkg]


def uninstall_argv(pkg):
    return ["rpm-ostree", "uninstall", pkg]


def update_argv():
    return ["rpm-ostree", "upgrade"]


def refresh_md_argv():
    return ["rpm-ostree", "refresh-md"]


def status(callback):
    """callback(info, err). info = {'pending': bool, 'head_req': set, 'booted_req': set}"""
    def done(st, out, err):
        if st != 0:
            callback(None, err.strip() or "rpm-ostree status failed")
            return
        try:
            deps = json.loads(out).get("deployments") or []
        except ValueError:
            callback(None, "could not parse rpm-ostree status")
            return
        if not deps:
            callback(None, "no deployments found")
            return
        head = deps[0]
        booted = next((d for d in deps if d.get("booted")), head)
        callback({
            "pending": not head.get("booted", False),
            "staged": bool(head.get("staged", False)),
            "head_version": head.get("version") or "",
            "booted_version": booted.get("version") or "",
            "head_req": set(head.get("requested-packages") or []),
            "booted_req": set(booted.get("requested-packages") or []),
        }, None)

    capture(["rpm-ostree", "status", "--json"], done)


def parse_check(status, out, err):
    """Interpret `rpm-ostree upgrade --check`. Returns (available, version, error)."""
    text = out + "\n" + err
    if status == 77 or "No updates available" in text:
        return False, "", None
    if status != 0:
        return None, "", (err.strip() or out.strip() or "rpm-ostree upgrade --check failed")
    if "AvailableUpdate" in text:
        m = re.search(r"^\s*Version:\s*(\S+)", text, re.M)
        return True, (m.group(1) if m else ""), None
    return None, "", "unexpected output from rpm-ostree upgrade --check"


def check_update(callback):
    """Ask rpm-ostree whether a new deployment exists. callback(available, version, error)."""
    capture(["rpm-ostree", "upgrade", "--check"],
            lambda st, out, err: callback(*parse_check(st, out, err)), env={"LC_ALL": "C"}, timeout=90)


def installed_names(callback):
    """Names of RPMs in the booted system. callback(set)."""
    capture(["rpm", "-qa", "--qf", "%{NAME}\\n"],
            lambda st, out, err: callback(set(out.split()) if st == 0 else set()))
