"""Unified app model: merges Flathub (Flatpak) and RPM entries into one list."""
import re
from dataclasses import dataclass
from typing import Optional

from . import catalog, flatpak

INSTALLED_RPM = {"base", "layered", "pending-install"}


@dataclass
class AppEntry:
    name: str
    summary: str = ""
    flatpak_id: Optional[str] = None
    rpm_pkg: Optional[str] = None
    rpm_gui: bool = True              # False = command-line tool
    appstream_id: str = ""
    icon_ref: Optional[tuple] = None  # (origin, icon file) from AppStream
    discovered: bool = False          # found by a live RPM search, not part of the index
    flatpak_installed: bool = False
    flatpak_update: bool = False      # real `flatpak remote-ls --updates` result
    rpm_state: Optional[str] = None   # None | base | layered | pending-install | pending-removal
    rpm_update: bool = False          # newer version in the repos (actionable now)
    rpm_staged: bool = False          # newer version already staged, reboot applies it
    _icon: object = False             # resolved icon path cache (False = not resolved)
    cats: tuple = ()                  # AppStream categories (RPM + Flatpak, merged)
    group: int = 3                    # store section, see GROUPS


# Store sections for graphical apps, in display order.
PRODUCTIVITY, GAMING, DEVELOPMENT, OTHER = range(4)
GROUPS = {
    PRODUCTIVITY: "Productivity",
    GAMING: "Gaming",
    DEVELOPMENT: "Development",
    OTHER: "Other apps",
}

_OFFICE_CATS = {"office", "wordprocessor", "spreadsheet", "presentation", "calendar",
                "projectmanagement", "contactmanagement", "email", "pdftools", "publishing",
                "database", "chart", "flowchart", "finance", "dictionary", "scanning",
                "ocr", "documentation"}
_DEV_CATS = {"development", "ide", "programming", "debugger", "revisioncontrol",
             "versioncontrol", "webdevelopment", "building", "profiling"}
_DEV_ID = re.compile(
    r"(^|[./_-])(code|codium|vscode|visual.?studio.?code|intellij|idea|pycharm|goland|"
    r"webstorm|phpstorm|rubymine|clion|datagrip|rider|fleet|android.?studio|eclipse|"
    r"qtcreator|gnome.?builder|zed)([./_-]|$)", re.I)
_DEV_NAME = re.compile(
    r"visual studio code|visual studio code - insiders|vscodium|intellij idea|pycharm|goland|webstorm|phpstorm|"
    r"rubymine|clion|datagrip|jetbrains|android studio|eclipse ide|qt creator|"
    r"gnome builder|zed editor", re.I)
_EMU_ID = re.compile(r"retroarch|dolphin-?emu|pcsx|ppsspp|rpcs3|duckstation|mgba|melonds|"
                     r"cemu|snes9x|mame|dosbox|scummvm|flycast|xemu|yuzu|ryujinx|citra|"
                     r"azahar|vita3k|bsnes|higan|mesen|fceux|nestopia|desmume|"
                     r"lime3ds|shadps4|xenia|rmg|simple64|mupen|vbam|visualboyadvance|"
                     r"stella|hatari|fs-?uae|vice|openmsx|bizhawk|ares|punes|supermodel", re.I)
_EMU_TXT = re.compile(r"\bemulat(?:or|ors|ion|es|ing)\b|\bretro ?gam", re.I)
_PROD_TXT = re.compile(r"word processor|spreadsheet|presentation|office suite|note-?taking|"
                       r"\bnotes?\b app|to-?do|task manager|\bcalendar\b|e-?mail client|"
                       r"\bpdf\b|document (?:viewer|editor)|\bproductivity\b|kanban|mind ?map", re.I)
_GAME_TXT = re.compile(r"\bgames?\b|\barcade\b|\bpuzzle\b|game launcher|\bplatformer\b|"
                       r"\bchess\b|\bsolitaire\b", re.I)
_GAME_LAUNCHER = re.compile(r"steam|heroic|lutris|bottles|itch|prismlauncher|gamescope", re.I)
# Exact AppStream/Flatpak IDs belong here; keep product-specific exceptions in
# one place instead of scattering name-regex checks through classification.
APP_GROUP_OVERRIDES = {
    "com.visualstudio.code": DEVELOPMENT,
    "org.libreoffice.libreoffice": PRODUCTIVITY,
    "org.onlyoffice.desktopeditors": PRODUCTIVITY,
    "com.valvesoftware.steam": GAMING,
}

# Exact IDs to float to the top of their section. Add IDs here as needed.
FEATURED_APP_IDS = {
    DEVELOPMENT: {"com.visualstudio.code"},
    GAMING: {"com.valvesoftware.steam"},
}


def classify(e):
    """Classify graphical apps into store sections using AppStream metadata and known app IDs."""
    cats = {c.lower().replace(" ", "") for c in e.cats}
    app_ids = {x.strip().casefold() for x in (e.appstream_id, e.flatpak_id) if x}
    ident = " ".join(x for x in (e.appstream_id, e.flatpak_id, e.rpm_pkg, e.name) if x)
    text = f"{e.name or ''} {e.summary or ''}"

    # Exact IDs override inconsistent or missing AppStream categories.
    for app_id in app_ids:
        if app_id in APP_GROUP_OVERRIDES:
            return APP_GROUP_OVERRIDES[app_id]

    # Known developer tools take precedence over generic AppStream categories.
    if _DEV_ID.search(ident) or _DEV_NAME.search(text) or cats & _DEV_CATS:
        return DEVELOPMENT

    is_game = ("game" in cats or any(c.endswith("game") or c in ("roleplaying", "shooter")
                                      for c in cats))
    # Keep emulators and game launchers in the same Gaming section as games.
    if "emulator" in cats or _EMU_ID.search(ident) or _EMU_TXT.search(text):
        return GAMING
    if is_game or _GAME_TXT.search(text) or _GAME_LAUNCHER.search(ident):
        return GAMING
    if cats & _OFFICE_CATS:
        return PRODUCTIVITY
    if not cats and _PROD_TXT.search(text):
        return PRODUCTIVITY
    return OTHER


def text_for_entry(e):
    return f"{e.name or ''} {e.summary or ''}"


def featured_rank(e):
    """Rank explicitly configured app IDs first within their section."""
    app_ids = {x.strip().casefold() for x in (e.appstream_id, e.flatpak_id) if x}
    featured = FEATURED_APP_IDS.get(e.group, set())
    return 0 if app_ids & featured else 1


def norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def installed(e):
    return e.flatpak_installed or e.rpm_state in INSTALLED_RPM


def section(e):
    """0 = graphical application, 1 = command-line tool, 2 = other RPM package (search only)."""
    if e.discovered:
        return 2
    if not e.rpm_gui and not e.flatpak_id:
        return 1
    return 0


def sort_entries(entries):
    def rank(e):
        sec = section(e)
        if sec:
            return sec + 1
        return 0 if (e.flatpak_id or e.icon_ref) else 1
    entries.sort(key=lambda e: (rank(e), e.name.lower()))


def by_section(entries, grouped=False):
    """Stable partition: applications first, then CLI tools, then other packages.
    grouped=True also orders the applications by store section (Productivity, Gaming, Development, Other apps)."""
    if grouped:
        return sorted(entries, key=lambda e: (section(e), e.group if section(e) == 0 else 0))
    return sorted(entries, key=section)


def feed_key(e, grouped=False):
    """Header key of an entry: (section, store section)."""
    sec = section(e)
    return (sec, e.group if grouped and sec == 0 else 0)


def ensure_layered(entries, names):
    """Make sure every layered/requested RPM has an entry, so it shows under Installed."""
    known = {e.rpm_pkg for e in entries if e.rpm_pkg}
    added = [AppEntry(name=n, rpm_pkg=n) for n in sorted(names) if n not in known]
    if added:
        entries.extend(added)
        sort_entries(entries)
    return bool(added)


def build(flatpaks, appstream, primary, show_cli, rpm_enabled=True, fp_cats=None):
    entries, by_id, by_norm, by_pkg = [], {}, {}, {}
    fp_cats = fp_cats or {}

    def add(e):
        entries.append(e)
        if e.appstream_id:
            by_id.setdefault(e.appstream_id.lower(), e)
        by_norm.setdefault(norm(e.name), e)
        if e.rpm_pkg:
            by_pkg[e.rpm_pkg] = e

    if rpm_enabled:
        for a in appstream:
            if not a["gui"] and not show_cli:
                continue
            add(AppEntry(name=a["name"], summary=a["summary"], rpm_pkg=a["pkg"],
                         rpm_gui=a["gui"], appstream_id=a["id"],
                         icon_ref=(a["origin"], a["icon"]) if a["icon"] else None,
                         cats=tuple(a.get("cats", ()))))
        for pkg, summary, gui, cli in primary:
            if pkg in by_pkg or not (gui or (cli and show_cli)):
                continue
            add(AppEntry(name=pkg, summary=summary, rpm_pkg=pkg, rpm_gui=bool(gui)))

    for fid, name, desc in flatpaks:
        e = by_id.get(fid.lower()) or by_norm.get(norm(name))
        if e is not None and e.flatpak_id is None:
            e.flatpak_id = fid
            e.summary = e.summary or desc
        else:
            add(AppEntry(name=name, summary=desc, flatpak_id=fid))

    for e in entries:
        if e.flatpak_id:
            extra = fp_cats.get(e.flatpak_id.lower(), ())
            e.cats = tuple(dict.fromkeys(e.cats + tuple(extra)))
        e.group = classify(e)
    sort_entries(entries)
    return entries


def apply_state(entries, fp_installed, rpm_names, info):
    head, booted = info["head_req"], info["booted_req"]
    for e in entries:
        e.flatpak_installed = bool(e.flatpak_id and e.flatpak_id in fp_installed)
        p = e.rpm_pkg
        if not p:
            e.rpm_state = None
        elif p in head and p not in booted:
            e.rpm_state = "pending-install"
        elif p in booted and p not in head:
            e.rpm_state = "pending-removal"
        elif p in head:
            e.rpm_state = "layered"
        elif p in rpm_names:
            e.rpm_state = "base"
        else:
            e.rpm_state = None


def apply_updates(entries, fp_updates, rpm_upgrades, info, system_update):
    """Mark which installed entries have a real update.
    fp_updates / rpm_upgrades: sets, or None when that check has not (successfully) run."""
    pending = info["pending"]
    for e in entries:
        e.flatpak_update = bool(fp_updates and e.flatpak_installed and e.flatpak_id in fp_updates)
        e.rpm_update = e.rpm_staged = False
        if not rpm_upgrades or e.rpm_pkg not in rpm_upgrades:
            continue
        if e.rpm_state in ("layered", "pending-install"):
            e.rpm_staged = pending           # a new deployment already carries it
            e.rpm_update = not pending
        elif e.rpm_state == "base":
            # base packages only change with a new system deployment (System tab)
            e.rpm_update = bool(system_update) and not pending


def update_status(e, fp_known, rpm_known):
    """'update' | 'current' | 'unknown' for an installed entry."""
    unknown = False
    if e.flatpak_installed:
        if e.flatpak_update:
            return "update"
        unknown = unknown or not fp_known
    if e.rpm_state in INSTALLED_RPM:
        if e.rpm_update:
            return "update"
        unknown = unknown or not rpm_known
    return "unknown" if unknown else "current"


def search(entries, term):
    t = term.lower().strip()
    if not t:
        return list(entries)
    scored = []
    for e in entries:
        n = e.name.lower()
        if n == t:
            s = 0
        elif n.startswith(t):
            s = 1
        elif t in n:
            s = 2
        elif (e.rpm_pkg and t in e.rpm_pkg.lower()) or (e.flatpak_id and t in e.flatpak_id.lower()):
            s = 3
        elif t in e.summary.lower():
            s = 4
        else:
            continue
        scored.append((s, n, e))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in scored]


def icon_path(e, remote, scope):
    if e._icon is False:
        path = None
        if e.flatpak_id:
            path = flatpak.icon_path(e.flatpak_id, remote, scope)
        if not path and e.icon_ref:
            path = catalog.appstream_icon(*e.icon_ref)
        e._icon = path
    return e._icon
