import json
import os
import platform
import re
import shlex
import shutil
import subprocess

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import catalog, flatpak, model, rpmostree, screenshots
from .config import Config
from .model import AppEntry
from .runner import Runner, capture

APP_ID = "com.carbonwarp.software"
VERSION = "0.5.1"
PAGE = 36             # tiles rendered per batch
MAX_COLS = 4          # at most four tiles per row
MIN_TILE = 240        # lower bound for a tile; the real minimum is measured at runtime
GAP = 12
LIST_MARGIN = 16      # side margin of the tile grid
MAX_CONTENT = 1240    # content never gets wider than this, so tiles stay a sensible size
THEMES = ["system", "light", "dark"]
SCHEMES = {
    "system": Adw.ColorScheme.DEFAULT,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
}
SECTION_TITLES = {0: "Applications", 1: "Command-line tools", 2: "Other RPM packages"}
PREVIEW = 12          # tiles per store section before "Show all" (12 fills 1-4 column rows)
CSS = b"""
.chip { border-radius: 999px; padding: 2px 16px; min-height: 30px; }
.store-action { border-radius: 999px; padding: 2px 12px; min-height: 30px; }
.tag { border-radius: 6px; padding: 1px 8px; font-size: 0.8em; font-weight: bold;
       background: alpha(currentColor, 0.1); }
.tag-rpm { color: @accent_color;}
.tag-flathub { color: @accent_color;}
.tag-reboot { color: @warning_color; background: alpha(@warning_color, 0.18); }
.tag-uptodate {
    color: @success_color;
    padding: 1px 8px;
    border-radius: 6px;
    font-size: 0.8em;
    font-weight: bold;
}
.app-card { padding: 14px; }
.tile-summary { min-height: 2.6em; }
.screenshot { border-radius: 10px; }
.install-action, .open-action { color: @accent_color; }
.uninstall-action { color: @error_color; }
"""


# ---------------------------------------------------------------- helpers

def gather(starters, done):
    """Run async starters {key: fn(cb)}; call done(results) once every cb fired."""
    results, remaining = {}, [len(starters)]
    if not starters:
        done(results)
        return
    for key, start in starters.items():
        def cb(val, key=key):
            results[key] = val
            remaining[0] -= 1
            if remaining[0] == 0:
                done(results)
        start(cb)


def clear_box(box):
    """Remove all children (works for Box, Grid and anything with remove())."""
    while (c := box.get_first_child()) is not None:
        box.remove(c)


def initial_window_size():
    """Returns (width, height, small). Fits the SMALLEST connected screen (logical pixels,
    so HiDPI scaling is included). Small screens open maximized so nothing overflows."""
    width, height, small = 1120, 780, False
    try:
        monitors = Gdk.Display.get_default().get_monitors()
        geos = [monitors.get_item(i).get_geometry() for i in range(monitors.get_n_items())]
        if geos:
            screen_w = min(g.width for g in geos)
            screen_h = min(g.height for g in geos)
            width = max(480, min(width, screen_w - 96))
            height = max(360, min(height, screen_h - 120))
            small = screen_w < 1200 or screen_h < 720
    except (AttributeError, TypeError):
        pass
    return width, height, small


class Console(Gtk.ScrolledWindow):
    def __init__(self):
        super().__init__(min_content_height=130, margin_start=12, margin_end=12,
                         margin_bottom=12, margin_top=6)
        self.add_css_class("card")
        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=8,
                                 right_margin=8, top_margin=6, bottom_margin=6)
        self.set_child(self.view)
        self.buf = self.view.get_buffer()
        self.mark = self.buf.create_mark(None, self.buf.get_end_iter(), False)

    def clear(self):
        self.buf.set_text("")

    def append(self, line):
        self.buf.insert(self.buf.get_end_iter(), line + "\n")
        self.buf.move_mark(self.mark, self.buf.get_end_iter())
        self.view.scroll_to_mark(self.mark, 0, False, 0, 0)


_DESKTOP_APP_CACHE = None


def _all_desktop_apps():
    global _DESKTOP_APP_CACHE
    if _DESKTOP_APP_CACHE is None:
        try:
            _DESKTOP_APP_CACHE = tuple(Gio.AppInfo.get_all())
        except (GLib.Error, TypeError):
            _DESKTOP_APP_CACHE = ()
    return _DESKTOP_APP_CACHE


def _desktop_apps(e, package_files=False):
    """Find only launchers belonging to this app, never package helper utilities."""
    ids = {v.removesuffix(".desktop").casefold()
           for v in (e.appstream_id, e.flatpak_id, e.rpm_pkg) if v}
    name = (e.name or "").strip().casefold()
    apps = []

    def add(app, source_rank=0):
        if not app:
            return
        try:
            app_id = (app.get_id() or "").removesuffix(".desktop").casefold()
            app_name = (app.get_name() or "").strip().casefold()
            command = (app.get_commandline() or "").casefold()
            # Helpers such as nautilus-autorun-software are not user-facing apps.
            if "nautilus-autorun-software" in command or "nautilus-autorun-software" in app_id:
                return
            if not (app_id in ids or (name and app_name == name)):
                return
            try:
                if not app.should_show():
                    return
            except (AttributeError, GLib.Error):
                pass
            apps.append((0 if app_id in ids else 1, source_rank, app))
        except (GLib.Error, TypeError, AttributeError):
            return

    for app in _all_desktop_apps():
        add(app)

    # Some base-image launchers are absent from normal discovery. Resolve the
    # package's desktop files, but only accept an ID/name match for this app.
    if package_files and e.rpm_pkg:
        try:
            result = subprocess.run(
                ["rpm", "-ql", "--", e.rpm_pkg], capture_output=True, text=True,
                timeout=5, check=False,
            )
            for path in result.stdout.splitlines():
                if (path.endswith(".desktop") and
                        path.startswith(("/usr/share/applications/",
                                         "/usr/local/share/applications/"))):
                    add(Gio.DesktopAppInfo.new_from_filename(path), 1)
        except (OSError, subprocess.SubprocessError, GLib.Error):
            pass

    unique, seen = [], set()
    for _rank, _source_rank, app in sorted(apps, key=lambda item: (item[0], item[1])):
        key = app.get_id() or app.get_name() or str(id(app))
        if key not in seen:
            seen.add(key)
            unique.append(app)
    return unique


def _icon_exists(icon, theme):
    """Return whether a GIcon can actually be rendered on this system."""
    if icon is None:
        return False
    try:
        if isinstance(icon, Gio.ThemedIcon):
            return any(theme and theme.has_icon(name) for name in icon.get_names())
        if isinstance(icon, Gio.FileIcon):
            file = icon.get_file()
            return bool(file and os.path.isfile(file.get_path() or ""))
    except (AttributeError, GLib.Error, TypeError):
        return False
    # For other GIcon implementations, let GTK try rendering it.
    return True


def _has_real_icon(win, e):
    """Best-effort icon probe used for ordering; never let it break the store."""
    try:
        display = Gdk.Display.get_default()
        theme = Gtk.IconTheme.get_for_display(display) if display else None
        for app in _desktop_apps(e, package_files=False):
            try:
                if _icon_exists(app.get_icon(), theme):
                    return True
            except (GLib.Error, TypeError, AttributeError):
                continue
        try:
            path = model.icon_path(e, win.config["remote"], win.config["scope"])
            if path and os.path.isfile(path):
                return True
        except (GLib.Error, OSError, KeyError, TypeError, AttributeError):
            pass
        return bool(theme and any(name and theme.has_icon(name)
                                  for name in (e.appstream_id, e.flatpak_id, e.rpm_pkg)))
    except (GLib.Error, OSError, KeyError, TypeError, AttributeError):
        return False


def make_icon(win, e, size=48):
    # Desktop entries provide the real installed icon; IDs often differ from icon names.
    display = Gdk.Display.get_default()
    theme = Gtk.IconTheme.get_for_display(display) if display else None
    for app in _desktop_apps(e, package_files=True):
        icon = app.get_icon()
        if _icon_exists(icon, theme):
            img = Gtk.Image(pixel_size=size, valign=Gtk.Align.CENTER)
            img.add_css_class("icon-dropshadow")
            img.set_from_gicon(icon)
            return img

    path = model.icon_path(e, win.config["remote"], win.config["scope"])
    if path and os.path.isfile(path):
        img = Gtk.Image(pixel_size=size, valign=Gtk.Align.CENTER)
        img.add_css_class("icon-dropshadow")
        img.set_from_file(path)
        return img
    for name in (e.appstream_id, e.flatpak_id, e.rpm_pkg):
        if name and theme and theme.has_icon(name):
            img = Gtk.Image(pixel_size=size, valign=Gtk.Align.CENTER)
            img.add_css_class("icon-dropshadow")
            img.set_from_icon_name(name)
            return img
    return Adw.Avatar(size=size, text=e.name, show_initials=True, valign=Gtk.Align.CENTER)


def open_url(win, url):
    if not url.startswith(("http://", "https://")):
        return
    if hasattr(Gtk, "UriLauncher"):
        Gtk.UriLauncher.new(url).launch(win, None, lambda *_: None)
    else:
        Gtk.show_uri(win, url, Gdk.CURRENT_TIME)


# Every label that sits in a tile is ellipsized, so a tile can shrink: its minimum width
# must stay small or the grid forces the window (or the page) wider than the screen.

def tag(text, css, installed=False):
    full = ("✓ " if installed else "") + text
    lbl = Gtk.Label(label=full, ellipsize=Pango.EllipsizeMode.END, tooltip_text=full)
    lbl.add_css_class("tag")
    lbl.add_css_class(css)
    return lbl


def dim_label(text, tooltip=None):
    lbl = Gtk.Label(label=text, tooltip_text=tooltip, ellipsize=Pango.EllipsizeMode.END)
    lbl.add_css_class("dim-label")
    lbl.add_css_class("caption")
    return lbl


def up_to_date_label(tooltip=None):
    """Non-clickable green indicator."""
    lbl = Gtk.Label(label="✓ Up to date", tooltip_text=tooltip or "Up to date",
                    ellipsize=Pango.EllipsizeMode.END)
    lbl.add_css_class("tag-uptodate")
    return lbl


def make_action(options, label, accent=False, big=False):
    """Compact chip-like action; multiple choices are shown in a popover."""
    if len(options) == 1:
        text, fn = options[0]
        btn = Gtk.Button(label=label, valign=Gtk.Align.CENTER, tooltip_text=text)
        btn.connect("clicked", lambda _b: fn())
    else:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for text, fn in options:
            b = Gtk.Button(label=text)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, fn=fn, pop=pop: (pop.popdown(), fn()))
            box.append(b)
        pop.set_child(box)
        btn = Gtk.MenuButton(label=label, popover=pop, valign=Gtk.Align.CENTER,
                             always_show_arrow=True)
    btn.add_css_class("flat")
    btn.add_css_class("store-action")
    if label == "Install":
        btn.add_css_class("install-action")
    elif label == "Uninstall":
        btn.add_css_class("uninstall-action")
    return btn


# ---------------------------------------------------------------- launching installed apps

def _spawn(argv):
    """Start a program directly (no D-Bus activation); GIO reaps the child."""
    Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE)


def _desktop_argv(app):
    """Return a safe executable argv from a desktop entry, excluding field codes."""
    command = app.get_commandline()
    if not command:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    argv = [a for a in argv if not (a.startswith("%") and len(a) == 2)]
    if argv and os.path.basename(argv[0]) == "env":
        argv = argv[1:]
        while argv and "=" in argv[0] and not argv[0].startswith("/"):
            argv.pop(0)
    if not argv or os.path.basename(argv[0]) == "nautilus-autorun-software":
        return None
    # Avoid trying to launch a missing executable and reporting a misleading success.
    if not (os.path.isabs(argv[0]) and os.access(argv[0], os.X_OK)) and not shutil.which(argv[0]):
        return None
    return argv


def launch_installed(win, e):
    """Launch Flatpaks normally and RPM apps via their real desktop Exec command."""
    if e.flatpak_installed and e.flatpak_id:
        try:
            _spawn(["flatpak", "run", e.flatpak_id])
        except GLib.Error as exc:
            win.toast(f"Could not open app: {exc.message}")
        return

    ident = " ".join((e.appstream_id or "", e.rpm_pkg or "", e.name or "")).casefold()
    if "nautilus" in ident and shutil.which("nautilus"):
        try:
            _spawn([shutil.which("nautilus")])
            return
        except (GLib.Error, OSError, ValueError):
            pass

    apps = _desktop_apps(e, package_files=True)

    # Prefer the actual Exec command. Gio.AppInfo.launch() can route certain desktop
    # entries through helper programs (e.g. nautilus-autorun-software) rather than
    # opening the application window requested by the user.
    for app in apps:
        argv = _desktop_argv(app)
        if not argv:
            continue
        try:
            _spawn(argv)
            return
        except (GLib.Error, OSError, ValueError):
            continue

    for app in apps:
        try:
            if app.launch([], None):
                return
        except (GLib.Error, ValueError, TypeError):
            continue
    win.toast("Could not find a runnable desktop entry for this RPM app. "
              "The package may not provide a graphical launcher.")


# ---------------------------------------------------------------- app state on tiles / pages

RPM_STATE_TEXT = {
    "base": "Included in the system image",
    "layered": "Installed (layered)",
    "pending-install": "Installed · reboot required",
    "pending-removal": "Removed · reboot required",
}


class Details(Gtk.Box):
    """Description and metadata of an app page. Flathub metadata is preferred when the app
    exists as both Flathub and RPM."""

    def __init__(self, win, entry):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.win, self.e = win, entry
        self.info_rows = []
        self.has_desc = False

        about = Gtk.Label(label="About", xalign=0)
        about.add_css_class("title-4")
        self.desc = Gtk.Label(label="Loading details…", wrap=True, xalign=0, selectable=True)
        self.info_group = Adw.PreferencesGroup(title="Details")
        self.info_group.set_visible(False)
        for w in (about, self.desc, self.info_group):
            self.append(w)
        self._load()

    def _set_desc(self, text):
        self.has_desc = bool(text)
        self.desc.set_label(text or "No description available.")

    def _add_info(self, title, value, link=False):
        if not value:
            return
        row = Adw.ActionRow(title=title, subtitle=value, use_markup=False, subtitle_selectable=True)
        if link:
            row.set_activatable(True)
            row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
            row.connect("activated", lambda _r: open_url(self.win, value))
        self.info_group.add(row)
        self.info_rows.append(row)
        self.info_group.set_visible(True)

    def _load(self):
        e, win = self.e, self.win
        if e.flatpak_id:
            scope, remote = win.config["scope"], win.config["remote"]
            gather({"meta": lambda cb: catalog.in_thread(
                        lambda: catalog.flatpak_component(remote, e.flatpak_id), cb),
                    "info": lambda cb: flatpak.remote_info(scope, remote, e.flatpak_id, cb)},
                   self._got_flatpak)
        elif e.rpm_pkg and win.rpm_enabled:
            self._load_rpm()
        else:
            self._set_desc(e.summary)

    def _got_flatpak(self, r):
        e = self.e
        meta, info = r.get("meta"), r.get("info") or {}
        if isinstance(meta, Exception):
            meta = None
        meta = meta or {}
        self._set_desc(meta.get("description", "") or meta.get("summary", ""))
        version = info.get("version") or (meta.get("release") or ("", ""))[0]
        self._add_info("Developer", meta.get("developer", ""))
        self._add_info("Version", version)
        self._add_info("License", meta.get("license") or info.get("license", ""))
        self._add_info("Download size", info.get("download", ""))
        self._add_info("Installed size", info.get("installed", ""))
        self._add_info("Runtime", info.get("runtime", ""))
        self._add_info("Categories", ", ".join(c for c in meta.get("categories", []) if c))
        self._add_info("Website", (meta.get("urls") or {}).get("homepage", ""), link=True)
        self._add_info("Source", f"Flathub · {self.win.config['remote']}")
        self._add_info("App ID", e.flatpak_id)
        if not self.has_desc:
            if e.rpm_pkg and self.win.rpm_enabled:
                self._load_rpm()  # description fallback only
            else:
                self._set_desc(e.summary)

    def _load_rpm(self):
        e = self.e
        argv = catalog.rpm_info_argv(e.rpm_pkg, e.rpm_state in ("base", "layered"))
        if argv is None:
            self._rpm_loaded(-1, "", "")
            return
        capture(argv, self._rpm_loaded, env={"LC_ALL": "C"})

    def _rpm_loaded(self, status, out, err):
        e = self.e
        info = catalog.parse_pkg_info(out) if status == 0 else {}
        if not self.has_desc:
            self._set_desc(info.get("description") or e.summary)
        if e.flatpak_id:
            return  # Flathub details take precedence; RPM only supplied the description
        version = "-".join(x for x in (info.get("version"), info.get("release")) if x)
        self._add_info("Version", version)
        self._add_info("License", info.get("license", ""))
        self._add_info("Size", info.get("download size") or info.get("size")
                       or info.get("installed size", ""))
        self._add_info("Repository", info.get("repository") or info.get("repo")
                       or info.get("from repo", ""))
        self._add_info("Website", info.get("url", ""), link=True)
        self._add_info("Source", "RPM · rpm-ostree")
        self._add_info("Package", e.rpm_pkg)


def update_options(win, e):
    opts = []
    if e.flatpak_installed and e.flatpak_update:
        opts.append(("Flathub", lambda: win.act(e, "update", "flatpak")))
    if e.rpm_update:
        if e.rpm_state == "base":
            opts.append(("RPM · updated with the system (System tab)",
                         lambda: win.act(e, "update", "rpm")))
        else:
            opts.append(("RPM · rpm-ostree updates layered packages together with the system "
                         "(reboot required)", lambda: win.act(e, "update", "rpm")))
    return opts


def fill_state(win, e, tags, slot, installed_view=False, big=False):
    """Source tags + the one contextual action: Install / Update / Up to date."""
    clear_box(tags)
    clear_box(slot)
    rpm_on = bool(e.rpm_pkg) and win.rpm_enabled
    if installed_view:
        # Installed view: only show the source(s) the app is actually installed from.
        if e.flatpak_installed:
            tags.append(tag("Flathub", "tag-flathub"))
        if rpm_on and e.rpm_state in model.INSTALLED_RPM:
            tags.append(tag("System (Non-remove)" if e.rpm_state == "base"
                            else "System", "tag-rpm"))
    else:
        if e.flatpak_id:
            tags.append(tag("Flathub", "tag-flathub", e.flatpak_installed))
        if rpm_on:
            tags.append(tag("RPM", "tag-rpm", e.rpm_state in model.INSTALLED_RPM))
            if not e.rpm_gui and not e.discovered:
                tags.append(tag("CLI", "tag"))
    if e.rpm_state in ("pending-install", "pending-removal") or e.rpm_staged:
        tags.append(tag("Reboot required", "tag-reboot"))

    if model.installed(e):
        status = win.update_status(e)
        if status == "update":
            slot.append(make_action(update_options(win, e), "Update", accent=True, big=big))
        elif status == "current":
            slot.append(up_to_date_label(
                "Part of the system image; updated from the System tab"
                if e.rpm_state == "base" and not e.flatpak_installed else None))
        else:
            slot.append(dim_label("Checking…" if win.checking_updates else "Update status unknown"))
    else:
        inst = []
        if e.flatpak_id:
            inst.append(("Flathub", lambda: win.act(e, "install", "flatpak")))
        if rpm_on and e.rpm_state != "pending-removal":
            inst.append(("RPM (reboot required)", lambda: win.act(e, "install", "rpm")))
        if inst:
            slot.append(make_action(inst, "Install", accent=True, big=big))
        elif e.rpm_state == "pending-removal":
            slot.append(dim_label("Reboot to finish removal"))


class Card(Gtk.Box):
    """Store tile. Source tags on the left, a compact action on the right.
    Clicking the tile opens the app's own page."""

    def __init__(self, win, entry, installed_view=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        self.win, self.e, self.installed_view = win, entry, installed_view
        self.set_cursor_from_name("pointer")
        self.add_css_class("card")
        self.add_css_class("app-card")

        top = Gtk.Box(spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                       valign=Gtk.Align.CENTER)
        title = Gtk.Label(label=entry.name, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                          max_width_chars=1, hexpand=True)
        title.add_css_class("heading")
        summary = Gtk.Label(label=entry.summary or (entry.rpm_pkg or entry.flatpak_id or ""),
                            xalign=0, yalign=0, wrap=True, lines=2, max_width_chars=1,
                            ellipsize=Pango.EllipsizeMode.END)
        summary.add_css_class("dim-label")
        summary.add_css_class("caption")
        summary.add_css_class("tile-summary")
        text.append(title)
        text.append(summary)
        top.append(make_icon(win, entry, 56))
        top.append(text)

        bottom = Gtk.Box(spacing=6)
        self.tags = Gtk.Box(spacing=6, hexpand=True, valign=Gtk.Align.CENTER)
        self.tags.set_overflow(Gtk.Overflow.HIDDEN)
        self.slot = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.END)
        bottom.append(self.tags)
        bottom.append(self.slot)
        self.append(top)
        self.append(bottom)

        click = Gtk.GestureClick()
        click.connect("released", lambda _g, n, _x, _y: win.show_details(self.e) if n == 1 else None)
        self.add_controller(click)
        self.update()

    def update(self):
        fill_state(self.win, self.e, self.tags, self.slot, self.installed_view)


def make_carousel(paths, height=280):
    """Horizontally scrolling row of screenshots (None when no image could be loaded)."""
    row = Gtk.Box(spacing=12)
    count = 0
    for p in paths:
        try:
            tex = Gdk.Texture.new_from_filename(p)
        except GLib.Error:
            continue
        pic = Gtk.Picture.new_for_paintable(tex)
        pic.set_can_shrink(True)
        pic.set_size_request(int(height * tex.get_width() / max(1, tex.get_height())), height)
        pic.set_overflow(Gtk.Overflow.HIDDEN)
        pic.add_css_class("screenshot")
        row.append(pic)
        count += 1
    if not count:
        return None
    return Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                              hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                              propagate_natural_height=True, child=row)


class DetailsPage(Adw.NavigationPage):

    def __init__(self, win, entry):
        super().__init__(title=entry.name, tag="details")
        self.win, self.e = win, entry

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_top=12,
                      margin_bottom=16, margin_start=16, margin_end=16)
        head = Gtk.Box(spacing=20)
        icon = make_icon(win, entry, 96)
        icon.set_valign(Gtk.Align.START)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        title = Gtk.Label(label=entry.name, xalign=0, wrap=True)
        title.add_css_class("title-1")
        subtitle = Gtk.Label(label=entry.summary, xalign=0, wrap=True)
        subtitle.add_css_class("dim-label")
        self.variants = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=4)
        for w in (title, subtitle, self.variants):
            side.append(w)
        head.append(icon)
        head.append(side)

        self.shots = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.shots.set_visible(False)
        self.body = Details(win, entry)
        for w in (head, self.shots, self.body):
            box.append(w)

        # EXTERNAL: the page content never sets the window's minimum width.
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                      propagate_natural_width=False,
                                      child=Adw.Clamp(maximum_size=900, child=box))
        toolbar = Adw.ToolbarView(content=scroller)
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_child(toolbar)
        self.update()
        self._load_shots()

    def _variant(self, src):
        e = self.e
        if src == "flathub":
            installed = e.flatpak_installed
            chip = tag("Flathub", "tag-flathub", installed)
            sub = f"{e.flatpak_id} · " + ("Installed" if installed else "Not installed")
        else:
            state = e.rpm_state
            installed = state in model.INSTALLED_RPM
            chip = tag("RPM", "tag-rpm", installed)
            sub = f"{e.rpm_pkg} · " + RPM_STATE_TEXT.get(
                state, "Not installed · needs a reboot to apply")
        row = Gtk.Box(spacing=12)
        chip.set_valign(Gtk.Align.CENTER)
        lbl = Gtk.Label(label=sub, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        lbl.add_css_class("dim-label")
        lbl.add_css_class("caption")
        row.append(chip)
        row.append(lbl)
        return row

    def update(self):
        clear_box(self.variants)
        e, win = self.e, self.win
        if e.flatpak_id:
            self.variants.append(self._variant("flathub"))
        if e.rpm_pkg and win.rpm_enabled:
            self.variants.append(self._variant("rpm"))
        if e.rpm_staged or e.rpm_state in ("pending-install", "pending-removal"):
            self.variants.append(tag("Reboot required", "tag-reboot"))

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          margin_top=4, hexpand=True)
        actions.set_valign(Gtk.Align.CENTER)
        installed = (model.installed(e) or e.flatpak_installed
                     or e.rpm_state in model.INSTALLED_RPM)
        if installed:
            status = win.update_status(e)
            if status == "update":
                actions.append(make_action(update_options(win, e), "Update"))
            elif status == "current":
                current = up_to_date_label(
                    "Part of the system image; updated from the System tab"
                    if e.rpm_state == "base" and not e.flatpak_installed else None)
                current.set_halign(Gtk.Align.START)
                actions.append(current)
            else:
                actions.append(dim_label(
                    "Checking…" if win.checking_updates else "Update status unknown"))

            actions.append(Gtk.Box(hexpand=True))
            remove_options = []
            if e.flatpak_installed:
                remove_options.append(("Uninstall from Flathub",
                                       lambda: win.act(e, "uninstall", "flatpak")))
            if e.rpm_state in ("layered", "pending-install") and win.rpm_enabled:
                remove_options.append(("Uninstall RPM (reboot required)",
                                       lambda: win.act(e, "uninstall", "rpm")))
            if remove_options:
                actions.append(make_action(remove_options, "Uninstall"))

            open_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
            open_btn.add_css_class("flat")
            open_btn.add_css_class("store-action")
            open_btn.add_css_class("open-action")
            open_btn.connect("clicked", lambda _b: launch_installed(self.win, self.e))
            actions.append(open_btn)
        else:
            actions.append(Gtk.Box(hexpand=True))
            install_options = []
            if e.flatpak_id:
                install_options.append(("Flathub", lambda: win.act(e, "install", "flatpak")))
            if e.rpm_pkg and win.rpm_enabled and e.rpm_state != "pending-removal":
                install_options.append(("RPM · reboot required",
                                        lambda: win.act(e, "install", "rpm")))
            if install_options:
                actions.append(make_action(install_options, "Install"))
        if actions.get_first_child():
            self.variants.append(actions)

    # screenshots (AppStream; cached, refreshed only from the menu)
    def _load_shots(self):
        e, win = self.e, self.win
        sources = []
        if e.flatpak_id:
            remote = win.config["remote"]
            sources.append(("Flathub", f"flathub:{remote}:{e.flatpak_id}",
                            lambda: (catalog.flatpak_component(remote, e.flatpak_id) or {})
                            .get("screenshots", [])))
        if e.rpm_pkg and win.rpm_enabled:
            sources.append(("RPM", f"rpm:{e.rpm_pkg}", lambda: catalog.rpm_screenshots(e.rpm_pkg)))
        for label, key, finder in sources:
            catalog.in_thread(lambda key=key, finder=finder: screenshots.load(key, finder),
                              lambda paths, label=label: self._got_shots(label, paths, len(sources)))

    def _got_shots(self, label, paths, nsources):
        if isinstance(paths, Exception) or not paths:
            return
        carousel = make_carousel(paths)
        if carousel is None:
            return
        if not self.shots.get_visible():
            head = Gtk.Label(label="Screenshots", xalign=0)
            head.add_css_class("title-4")
            self.shots.append(head)
            self.shots.set_visible(True)
        if nsources > 1:   # app exists as Flathub and RPM: show both
            cap = Gtk.Label(label=label, xalign=0)
            cap.add_css_class("dim-label")
            cap.add_css_class("caption")
            self.shots.append(cap)
        self.shots.append(carousel)


# ---------------------------------------------------------------- feed

class Feed(Gtk.Box):
    """Searchable tile feed. mode: all (unified) | flathub | rpm.

    Layout: a Gtk.Grid with equal-width columns. The number of columns (1..MAX_COLS) is
    derived from the viewport width and the *measured* minimum width of a tile, and is
    recomputed whenever the viewport width changes. The scroller uses the EXTERNAL
    horizontal policy, so the tiles can never push the window wider."""

    def __init__(self, win, mode="all"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.mode = win, mode
        self.term, self.installed_only = "", False
        self.results, self.shown, self.cards, self.items = [], 0, [], []
        self.extra = []              # live RPM search hits that are not in the index
        self._cols = 1
        self._disc_src = 0
        self._sec, self._mixed = None, False
        self.grouped = False         # store sections (Productivity, Gaming, ...) when not searching
        self.expanded = set()        # store sections the user opened with "Show all"
        self.counts = {}             # entries per store section (before the preview cut)

        hint = {"all": "Search apps", "flathub": "Search Flathub apps",
                "rpm": "Search RPM packages"}[mode]
        self.entry = Gtk.SearchEntry(placeholder_text=hint, hexpand=True)
        self.entry.connect("search-changed", self._on_search)
        self.entry.connect("stop-search", lambda e: e.set_text(""))

        self.chip_all = Gtk.ToggleButton(label="All", active=True)
        self.chip_inst = Gtk.ToggleButton(label="Installed")
        self.chip_inst.set_group(self.chip_all)
        chips = Gtk.Box(spacing=8)
        for c in (self.chip_all, self.chip_inst):
            c.add_css_class("chip")
            c.connect("toggled", self._on_chip)
            chips.append(c)

        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12,
                       margin_bottom=6, margin_start=LIST_MARGIN, margin_end=LIST_MARGIN)
        head.append(self.entry)
        head.append(chips)
        self.append(head)

        self.grid = Gtk.Grid(column_homogeneous=True, column_spacing=GAP, row_spacing=GAP,
                             hexpand=True, valign=Gtk.Align.START, margin_top=6,
                             margin_bottom=LIST_MARGIN, margin_start=LIST_MARGIN,
                             margin_end=LIST_MARGIN)
        clamp = Adw.Clamp(maximum_size=MAX_CONTENT, tightening_threshold=MAX_CONTENT,
                          child=self.grid)
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                           propagate_natural_width=False, child=clamp)
        self.scroller.connect("edge-reached", self._on_edge)
        # Re-flow when the viewport width changes (window resize). Event driven: no
        # per-frame callback, so an idle window costs no CPU.
        self.scroller.get_hadjustment().connect("notify::page-size", self._on_width)
        self.connect("map", self._on_width)
        self.status = Adw.StatusPage()
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.scroller, "feed")
        self.stack.add_named(self.status, "status")
        self.append(self.stack)
        self.show_loading()

    # --- states ---
    def show_loading(self):
        self.status.set_icon_name(None)
        self.status.set_title("Loading apps…")
        self.status.set_description("")
        self.status.set_child(Gtk.Spinner(spinning=True, width_request=32, height_request=32,
                                          halign=Gtk.Align.CENTER))
        self.stack.set_visible_child_name("status")

    def _show_empty(self, icon, title, desc=""):
        self.status.set_child(None)
        self.status.set_icon_name(icon)
        self.status.set_title(title)
        self.status.set_description(desc)
        self.stack.set_visible_child_name("status")

    # --- search ---
    def _on_search(self, entry):
        self.term = entry.get_text()
        self.refilter()
        self._schedule_discovery()

    def _schedule_discovery(self):
        """Read-only live RPM lookup, so packages missing from the cached index are found too.
        This only ever *lists* packages; installing needs an explicit Install click."""
        if self._disc_src:
            GLib.source_remove(self._disc_src)
            self._disc_src = 0
        term = self.term.strip()
        if (self.mode == "flathub" or self.installed_only or len(term) < 2
                or not self.win.rpm_enabled or not catalog.dnf_available()):
            return
        self._disc_src = GLib.timeout_add(400, self._discover, term)

    def _discover(self, term):
        self._disc_src = 0
        if term == self.term.strip():
            catalog.in_thread(lambda: catalog.search_rpm(term),
                              lambda rows: self._discovered(term, rows))
        return False

    def _discovered(self, term, rows):
        if term != self.term.strip() or isinstance(rows, Exception):
            return
        known = {e.rpm_pkg for e in self.win.entries if e.rpm_pkg}
        fresh = [AppEntry(name=n, summary=s, rpm_pkg=n, rpm_gui=False, discovered=True)
                 for n, s in rows if n not in known]
        self.extra = model.search(fresh, term)[:30]
        self.win.apply(self.extra)
        self.refilter()

    # --- filtering / paging ---
    def _on_chip(self, btn):
        if btn.get_active():
            self.installed_only = btn is self.chip_inst
            self.refilter()
            self._schedule_discovery()

    def _on_edge(self, _sw, pos):
        if pos == Gtk.PositionType.BOTTOM:
            self._render_more()

    def _in_mode(self, e):
        if self.mode == "flathub":
            return bool(e.flatpak_id)
        if self.mode == "rpm":
            return bool(e.rpm_pkg)
        return True

    def refilter(self, soft=False):
        """Rebuild the list. soft=True keeps the tiles (and scroll position) if nothing changed."""
        win = self.win
        if not win.catalog_ready:
            return
        base = [e for e in win.entries if self._in_mode(e)]
        if self.term.strip():
            base += [e for e in self.extra if self._in_mode(e)]
        if self.installed_only:
            base = [e for e in base if model.installed(e)]
        self.grouped = not self.term.strip()
        results = model.by_section(model.search(base, self.term), self.grouped)
        # Keep the existing section/group order; within each group show real icons first.
        icon_status = {id(e): _has_real_icon(win, e) for e in results}
        results.sort(key=lambda e: (
            model.section(e),
            e.group if self.grouped and model.section(e) == 0 else 0,
            # Keep iconless apps last, then put the recognizable flagship apps first
            # among apps with usable icons so they appear in the initial preview.
            not icon_status.get(id(e), False),
            model.featured_rank(e) if self.grouped and model.section(e) == 0 else 1,
            (e.name or "").casefold()))
        if self.grouped:
            self.counts, shown, seen = {}, [], {}
            for e in results:
                key = model.feed_key(e, True)
                self.counts[key] = self.counts.get(key, 0) + 1
            for e in results:
                key = model.feed_key(e, True)
                seen[key] = seen.get(key, 0) + 1
                if key[0] or key in self.expanded or seen[key] <= PREVIEW:
                    shown.append(e)
            results = shown

        if soft and self.cards and [id(e) for e in results] == [id(e) for e in self.results]:
            for c in self.cards:
                c.update()
            return

        self.results = results
        clear_box(self.grid)
        self.items, self.cards, self.shown, self._sec = [], [], 0, None
        self._mixed = len({model.section(e) for e in results}) > 1
        if not results:
            if self.installed_only and not self.term:
                self._show_empty("view-grid-symbolic", "Nothing installed here yet")
            elif self.term:
                self._show_empty("edit-find-symbolic", "No apps found", "Try a different search term.")
            else:
                self._show_empty("view-grid-symbolic", "No apps available",
                                 "Use Menu › Refresh Package Data.")
            return
        self.stack.set_visible_child_name("feed")
        self.scroller.get_vadjustment().set_value(0)
        self._render_more()

    def _render_more(self):
        chunk = self.results[self.shown:self.shown + PAGE]
        for e in chunk:
            sec = model.feed_key(e, self.grouped)
            if sec != self._sec:
                if self._mixed or sec[0] or self.grouped:
                    self.items.append(("hdr", self._header(sec)))
                self._sec = sec
            card = Card(self.win, e, self.installed_only)
            self.items.append(("card", card))
            self.cards.append(card)
        self.shown += len(chunk)
        self._layout()                    # tiles must be in the window to be measured
        if self._update_cols():
            self._layout()

    def _header(self, key):
        """Section title; store sections with more than PREVIEW apps get a Show all / Show less."""
        sec, grp = key
        title = SECTION_TITLES[sec] if sec or not self.grouped else model.GROUPS[grp]
        hdr = Gtk.Box(spacing=8, margin_top=8)
        label = Gtk.Label(label=title, xalign=0, hexpand=True)
        label.add_css_class("title-4")
        hdr.append(label)
        total = self.counts.get(key, 0)
        if self.grouped and not sec and total > PREVIEW:
            opened = key in self.expanded
            btn = Gtk.Button(label="Show less" if opened else f"Show all ({total})")
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b, k=key: self._toggle(k))
            hdr.append(btn)
        return hdr

    def _toggle(self, key):
        self.expanded ^= {key}
        adj = self.scroller.get_vadjustment()
        pos = adj.get_value()
        self.refilter()
        GLib.idle_add(lambda: adj.set_value(pos) or False)

    # --- responsive columns ---
    def _content_width(self):
        """Width the tiles can use: the scroller's own allocation, never a child's width."""
        w = self.scroller.get_width()
        if w <= 0:
            w = self.win.get_width()
        if w <= 0:
            w = self.win.get_default_size()[0]
        return min(w, MAX_CONTENT)

    def _tile_min(self):
        """Real minimum width of a tile (CSS padding, buttons and tags included)."""
        best = MIN_TILE
        for c in self.cards:
            best = max(best, c.measure(Gtk.Orientation.HORIZONTAL, -1)[0])
        return best

    def _update_cols(self):
        """1..MAX_COLS columns that really fit: a tile is never narrower than its own
        minimum, so rows cannot overflow. Returns True when the count changed."""
        available = max(MIN_TILE, self._content_width() - 2 * LIST_MARGIN)
        cols = max(1, min(MAX_COLS, (available + GAP) // (self._tile_min() + GAP)))
        changed = cols != self._cols
        self._cols = cols
        return changed

    def _on_width(self, *_args):
        """Viewport width changed (or the page became visible): re-flow the columns."""
        if self._update_cols() and self.items:
            self._layout()

    def _layout(self):
        """Place tiles in equal-width columns; section headers span the full row."""
        clear_box(self.grid)
        row = col = 0
        for kind, w in self.items:
            if kind == "hdr":
                if col:
                    row, col = row + 1, 0
                self.grid.attach(w, 0, row, self._cols, 1)
                row += 1
            else:
                self.grid.attach(w, col, row, 1, 1)
                col += 1
                if col == self._cols:
                    row, col = row + 1, 0

    def update_cards(self):
        """Refresh install/update state on visible tiles without losing scroll position."""
        self.win.apply(self.extra)
        self.refilter(soft=True)
        if self._update_cols() and self.items:   # an Update button etc. can widen tiles
            self._layout()


# ---------------------------------------------------------------- system tab

class SystemPage(Gtk.ScrolledWindow):
    """Operating system updates through rpm-ostree, separate from application updates."""

    def __init__(self, win):
        # EXTERNAL: this page never sets the window's minimum width.
        super().__init__(hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                         propagate_natural_width=False)
        self.win = win
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=10,
                      margin_bottom=16, margin_start=12, margin_end=12)
        self.set_child(Adw.Clamp(maximum_size=640, child=box))

        # Compact GNOME Settings-style header: OS identity on the left, icon-only
        # distribution logo on the right, followed by hardware details.
        release = self._os_release()
        os_name = release.get("PRETTY_NAME") or release.get("NAME", "Linux")
        os_name = re.sub(r"\s*\((?:Silverblue)\)", "", os_name, flags=re.IGNORECASE).strip()
        self._os_names = (os_name, release.get("PRETTY_NAME", ""))
        # Quick fallback now; the full deployment version is read asynchronously below.
        os_version = win.info.get("booted_version") or release.get("VERSION_ID") \
            or release.get("VERSION", "")

        identity = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16,
                           margin_top=2, margin_bottom=4)
        identity_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True,
                                valign=Gtk.Align.CENTER)
        name_label = Gtk.Label(label=os_name, xalign=0, wrap=True)
        name_label.add_css_class("title-1")
        self.version_label = Gtk.Label(xalign=0, wrap=True)
        self.version_label.add_css_class("dim-label")
        identity_text.append(name_label)
        identity_text.append(self.version_label)
        self._set_version(os_version)
        self.os_logo = self._make_os_logo(release)
        self.os_logo.set_pixel_size(112)
        self.os_logo.set_size_request(112, 112)
        self.os_logo.set_halign(Gtk.Align.END)
        self.os_logo.set_valign(Gtk.Align.CENTER)
        identity.append(identity_text)
        identity.append(self.os_logo)
        box.append(identity)

        about_group = Adw.PreferencesGroup()
        for title, value in self._hardware_details():
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3,
                              margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
            label = Gtk.Label(label=title, xalign=0)
            label.add_css_class("dim-label")
            label.add_css_class("caption")
            value_label = Gtk.Label(label=value, xalign=0, wrap=True, selectable=True)
            row_box.append(label)
            row_box.append(value_label)
            row = Adw.PreferencesRow()
            row.set_child(row_box)
            about_group.add(row)
        box.append(about_group)

        group = Adw.PreferencesGroup(title="System updates",
                                     description="Manage the current rpm-ostree deployment "
                                                 "and available system updates.")
        self.row_state = Adw.ActionRow(title="This update", use_markup=False)
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.btn_check = Gtk.Button(label="Check for Updates", valign=Gtk.Align.CENTER,
                                    tooltip_text="Check for a new deployment")
        self.btn_check.connect("clicked", lambda _b: win.refresh_updates())
        self.btn_update = Gtk.Button(label="Update", valign=Gtk.Align.CENTER)
        self.btn_update.add_css_class("suggested-action")
        self.btn_update.connect("clicked", lambda _b: win.update_system())
        for w in (self.spinner, self.btn_check, self.btn_update):
            self.row_state.add_suffix(w)
        group.add(self.row_state)
        box.append(group)

        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.bar = Gtk.ProgressBar(show_text=False)
        self.phase = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.phase.add_css_class("dim-label")
        self.phase.add_css_class("caption")
        self.progress_box.append(self.bar)
        self.progress_box.append(self.phase)
        box.append(self.progress_box)

        self.error = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.error.add_css_class("error")
        box.append(self.error)

        self.reboot_group = Adw.PreferencesGroup()
        self.row_reboot = Adw.ActionRow(title="Reboot required", use_markup=False)
        btn_reboot = Gtk.Button(label="Reboot", valign=Gtk.Align.CENTER)
        btn_reboot.add_css_class("destructive-action")
        btn_reboot.connect("clicked", lambda _b: win.reboot())
        self.row_reboot.add_suffix(btn_reboot)
        self.reboot_group.add(self.row_reboot)
        box.append(self.reboot_group)

        self._load_full_version()
        self.refresh()

    def _set_version(self, text):
        text = re.sub(r"\s*\([^)]*\)", "", text or "").strip()
        show = bool(text) and text not in self._os_names
        self.version_label.set_label(text if show else "")
        self.version_label.set_visible(show)

    def _load_full_version(self):
        """Full version of the booted deployment, read without blocking the UI."""
        def done(status, out, err):
            if status != 0:
                return
            try:
                deployments = json.loads(out).get("deployments", [])
            except (ValueError, AttributeError):
                return
            booted = next((d for d in deployments if d.get("booted")), None)
            if not booted:
                return
            version = booted.get("version")
            if not version:
                meta = booted.get("base-commit-meta")
                version = meta.get("version") if isinstance(meta, dict) else ""
            if version:
                self._set_version(str(version))

        capture(["rpm-ostree", "status", "--json"], done, env={"LC_ALL": "C"})

    @staticmethod
    def _os_release():
        """Parse os-release according to its shell-style value rules."""
        values = {}
        try:
            with open("/etc/os-release", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    try:
                        parsed = shlex.split(value, posix=True)
                        values[key] = parsed[0] if parsed else ""
                    except ValueError:
                        values[key] = value.strip().strip('"\'')
        except OSError:
            pass
        return values

    @staticmethod
    def _make_os_logo(release):
        """Use the distro logo from os-release/icon theme, with Fedora file fallbacks."""
        if "fedora" in release.get("ID", "").lower():
            # Icon-only Fedora mark; deliberately avoid wordmark/logo-plus-text files.
            for path in (
                "/usr/share/icons/hicolor/scalable/apps/fedora-logo-icon.svg",
                "/usr/share/icons/hicolor/scalable/apps/org.fedoraproject.FedoraLogo.svg",
                "/usr/share/icons/hicolor/128x128/apps/fedora-logo-icon.png",
                "/usr/share/icons/hicolor/64x64/apps/fedora-logo-icon.png",
                "/usr/share/pixmaps/fedora-logo-icon.png",
            ):
                if os.path.isfile(path):
                    return Gtk.Image.new_from_file(path)

        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        candidates = (["fedora-logo-icon", "org.fedoraproject.FedoraLogo", "fedora-logo"]
                      if "fedora" in release.get("ID", "").lower() else [])
        candidates += [release.get("LOGO", ""), "distributor-logo", "computer-symbolic"]
        for name in candidates:
            if name and theme.has_icon(name):
                return Gtk.Image.new_from_icon_name(name)
        return Gtk.Image.new_from_icon_name("computer-symbolic")

    @staticmethod
    def _hardware_details():
        details = []

        def read_first(*paths):
            for path in paths:
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        value = f.read().strip()
                    if value and value.lower() not in {
                        "unknown", "none", "to be filled by o.e.m.",
                        "system product name", "default string", "not specified"
                    }:
                        return value
                except OSError:
                    continue
            return ""

        vendor = read_first("/sys/class/dmi/id/sys_vendor", "/sys/devices/virtual/dmi/id/sys_vendor")
        model_name = read_first("/sys/class/dmi/id/product_name",
                                "/sys/devices/virtual/dmi/id/product_name")
        if model_name:
            hw = (f"{vendor} {model_name}".strip()
                  if vendor and vendor.lower() not in model_name.lower() else model_name)
            details.append(("Hardware model", hw))
        elif vendor:
            details.append(("Manufacturer", vendor))

        # "model name" is meaningful on x86; ARM often reports it as Hardware or Processor.
        cpu = ""
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    key, value = (part.strip() for part in line.split(":", 1))
                    if key.lower() in ("model name", "hardware") and value:
                        cpu = value
                        break
                    if key.lower() == "processor" and value and not value.isdigit() and not cpu:
                        cpu = value
        except OSError:
            pass
        if not cpu:
            cpu = platform.processor().strip()
        if cpu:
            details.append(("Processor", cpu))

        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                first = f.readline().split()
            if len(first) >= 2 and first[0] == "MemTotal:":
                details.append(("Memory", f"{int(first[1]) / 1024 / 1024:.1f} GiB"))
        except (OSError, ValueError):
            pass

        architecture = platform.machine().strip()
        if architecture:
            details.append(("Architecture", architecture))
        return details

    def on_line(self, line):
        """Real progress of the running rpm-ostree command."""
        m = re.search(r"(\d{1,3})%", line)
        if m:
            self.bar.set_fraction(min(int(m.group(1)), 100) / 100)
        else:
            self.bar.pulse()
        text = line.strip()
        if text:
            self.phase.set_label(text[:120])

    def refresh(self):
        w = self.win
        info, sysinfo = w.info, w.sys
        working = w.busy and w.op_kind == "system"
        checking = sysinfo["checking"]

        # Show the real booted deployment checksum when the status parser provides it.
        deployment_id = info.get("booted_checksum") or info.get("booted_id")
        self.row_state.set_title(f"This update ({deployment_id})" if deployment_id else "This update")
        self.row_reboot.set_subtitle(
            f"Version {info['head_version']} is staged and will be used on the next boot"
            if info.get("head_version") else "A new deployment will be used on the next boot")
        self.reboot_group.set_visible(info["pending"])

        err = sysinfo["error"] or w.status_error
        if not w.rpm_enabled:
            state = "rpm-ostree is not available on this system"
        elif working:
            state = "Downloading and staging the update…"
        elif checking:
            state = "Checking for updates…"
        elif info["pending"]:
            state = ("Update staged" if info.get("staged") else "Update ready") + " · restart to apply it"
        elif sysinfo["available"]:
            state = "Update available" + (f": {sysinfo['version']}" if sysinfo["version"] else "")
        elif sysinfo["available"] is False:
            state = "Your system is up to date"
        else:
            state = "Not checked yet"
        self.row_state.set_subtitle(state)

        self.spinner.set_spinning(working or checking)
        self.spinner.set_visible(working or checking)
        self.btn_check.set_sensitive(w.rpm_enabled and not w.busy and not checking)
        self.btn_update.set_visible(bool(sysinfo["available"]) and not info["pending"])
        self.btn_update.set_sensitive(not w.busy and not checking)
        self.progress_box.set_visible(working)
        if not working:
            self.bar.set_fraction(0)
            self.phase.set_label("")
        shown_err = err if (err and not working and not checking) else ""
        self.error.set_label(shown_err)
        self.error.set_visible(bool(shown_err))


# ---------------------------------------------------------------- window

class StoreWindow(Adw.ApplicationWindow):
    def __init__(self, app, config):
        width, height, small = initial_window_size()
        super().__init__(application=app, title="Software", default_width=width,
                         default_height=height)
        # Resizable with a small minimum (HiDPI laptops can have ~680x380 logical pixels).
        # Nothing inside may request more width than the window gives (see Feed).
        self.set_resizable(True)
        self.set_size_request(480, 360)
        if small:
            self.maximize()
        self.config = config
        # One-time reset: the split Flathub/RPM tabs and the command output are hidden by
        # default and only appear after the user enables them in Preferences.
        if not getattr(config, "data", {}).get("ui_defaults_v2"):
            config.set("separate_feeds", False)
            config.set("show_console", False)
            config.set("ui_defaults_v2", True)

        # state (defined before any widget that reads it)
        self.busy = False
        self.op_kind = None            # "app" | "system" while busy
        self.runner = None
        self._syncing = False
        self.entries = []
        self.catalog_ready = False
        self.rpm_enabled = rpmostree.available()
        self.info = {"pending": False, "staged": False, "head_version": "", "booted_version": "",
                     "head_req": set(), "booted_req": set()}
        self.fp_installed, self.rpm_names = set(), set()
        self.fp_updates = None         # None = not (successfully) checked
        self.rpm_upgrades = None
        self.sys = {"checking": False, "available": None, "version": "", "error": None}
        self.checking_updates, self._recheck = False, False
        self.details = None
        self.status_error = None       # last `rpm-ostree status` failure

        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        for name, cb in (("preferences", self._open_prefs), ("about", self._show_about),
                         ("refresh", self._on_refresh)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

        self.console = Console()
        self.console_revealer = Gtk.Revealer(child=self.console,
                                             reveal_child=bool(config["show_console"]))
        self.banner = Adw.Banner(title="Reboot required to apply changes", button_label="Reboot")
        self.banner.connect("button-clicked", lambda _b: self.reboot())

        self.feed_all, self.feed_fp, self.feed_rpm = (Feed(self, "all"), Feed(self, "flathub"),
                                                      Feed(self, "rpm"))
        self.feeds = [self.feed_all, self.feed_fp, self.feed_rpm]
        self.system_page = SystemPage(self)
        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.set_hhomogeneous(False)      # a wide page must not widen the others
        self.stack.add_titled_with_icon(self.feed_all, "all", "Apps", "view-grid-symbolic")
        self.stack.add_titled_with_icon(self.feed_fp, "flathub", "Flathub", "package-x-generic-symbolic")
        self.stack.add_titled_with_icon(self.feed_rpm, "rpm", "RPM", "computer-symbolic")
        self.stack.add_titled_with_icon(self.system_page, "system", "System",
                                        "emblem-system-symbolic")

        # Header: view switcher; on narrow windows a title in the header and the switcher
        # moves to a bottom bar (see _setup_narrow_mode).
        self.header = Adw.HeaderBar()
        self.switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.window_title = Adw.WindowTitle(title="Software", subtitle="")
        self.window_title.set_visible(False)
        title_box = Gtk.Box(halign=Gtk.Align.CENTER)
        title_box.append(self.switcher)
        title_box.append(self.window_title)
        self.header.set_title_widget(title_box)
        self.header.pack_end(self._build_menu_button())
        self.spinner = Gtk.Spinner()
        self.header.pack_end(self.spinner)

        # Shared progress area for install, uninstall, update and refresh commands. It lives
        # under the header (not in it), so a running command can never widen the header.
        self.operation_status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                                        margin_start=LIST_MARGIN, margin_end=LIST_MARGIN,
                                        margin_top=6, margin_bottom=2)
        self.operation_label = Gtk.Label(label="", xalign=0, hexpand=True,
                                         ellipsize=Pango.EllipsizeMode.END)
        self.operation_label.add_css_class("dim-label")
        self.operation_label.add_css_class("caption")
        self.operation_progress = Gtk.ProgressBar(show_text=True, valign=Gtk.Align.CENTER)
        self.operation_progress.set_size_request(140, -1)
        self.operation_progress.set_pulse_step(0.08)
        self.operation_status.append(self.operation_label)
        self.operation_status.append(self.operation_progress)
        self.operation_status.set_visible(False)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_hexpand(True)
        content.set_vexpand(True)
        content.append(self.banner)
        content.append(self.operation_status)
        content.append(self.stack)
        content.append(self.console_revealer)
        toolbar = Adw.ToolbarView(content=content)
        toolbar.add_top_bar(self.header)
        self._setup_narrow_mode(toolbar)

        self.prefs_page = self._build_prefs_page()
        self.nav = Adw.NavigationView()
        self.nav.push(Adw.NavigationPage(child=toolbar, title="Software", tag="main"))
        self.nav.connect("popped", lambda _n, page: setattr(self, "details", None)
                         if page is self.details else None)
        self.toasts = Adw.ToastOverlay(child=self.nav)
        self.set_content(self.toasts)

        self.apply_feed_mode()
        self.set_theme(config["theme"])
        self.load_catalog()
        self.refresh_updates()

    def _setup_narrow_mode(self, toolbar):
        """Below 600px the switcher moves from the header to a bottom bar (libadwaita >= 1.4)."""
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        toolbar.add_bottom_bar(self.switcher_bar)
        if not hasattr(Adw, "Breakpoint"):
            return
        try:
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600px"))
            bp.add_setter(self.switcher_bar, "reveal", True)
            bp.add_setter(self.switcher, "visible", False)
            bp.add_setter(self.window_title, "visible", True)
            self.add_breakpoint(bp)
        except (TypeError, GLib.Error):
            pass

    # ----- app page -----

    def show_details(self, entry):
        page = self.nav.get_visible_page()
        if page is not None and page.get_tag() in ("details", "preferences"):
            return
        self.details = DetailsPage(self, entry)
        self.nav.push(self.details)

    # ----- feed modes -----

    def apply_feed_mode(self):
        sep = self.config["separate_feeds"]
        self.stack.get_page(self.feed_fp).set_visible(sep)
        self.stack.get_page(self.feed_rpm).set_visible(sep and self.rpm_enabled)
        if not sep and self.stack.get_visible_child_name() in ("flathub", "rpm"):
            self.stack.set_visible_child(self.feed_all)

    # ----- catalog / state -----

    def load_catalog(self, force=False):
        self.catalog_ready = False
        for f in self.feeds:
            f.show_loading()
        scope, remote, cli = self.config["scope"], self.config["remote"], self.config["show_cli"]
        starters = {"flatpak": lambda cb: flatpak.list_remote(scope, remote, cb),
                    "fpcats": lambda cb: catalog.in_thread(
                        lambda: catalog.load_flatpak_categories(remote), cb)}
        if self.rpm_enabled:
            starters["appstream"] = lambda cb: catalog.in_thread(catalog.load_appstream, cb)
            if catalog.dnf_available():
                starters["pkgs"] = lambda cb: catalog.in_thread(
                    lambda: catalog.load_dnf(cli, force), cb)
            elif cli:
                starters["pkgs"] = lambda cb: catalog.in_thread(catalog.load_primary, cb)
        gather(starters, self._catalog_loaded)

    def _catalog_loaded(self, r):
        ap, pk = r.get("appstream", []), r.get("pkgs", [])
        if isinstance(ap, Exception):
            ap = []
        if isinstance(pk, Exception):
            self.toast("RPM index failed: " + (str(pk)[:120] or "unknown error"))
            pk = []
        elif self.rpm_enabled and not ap and not pk:
            self.toast("No RPM packages found. Check that 'dnf repoquery' works, "
                       "then use Menu › Refresh Package Data.")
        fc = r.get("fpcats", {})
        self.entries = model.build(r.get("flatpak", []), ap, pk, self.config["show_cli"],
                                   self.rpm_enabled, {} if isinstance(fc, Exception) else fc)
        for f in self.feeds:
            f.extra = []
        self.catalog_ready = True
        self.refresh_state(rebuild=True)

    def apply(self, entries):
        """Stamp installed + update state from the real package managers onto entries."""
        model.apply_state(entries, self.fp_installed, self.rpm_names, self.info)
        model.apply_updates(entries, self.fp_updates, self.rpm_upgrades, self.info,
                            self.sys["available"])

    def update_status(self, e):
        return model.update_status(e, self.fp_updates is not None, self.rpm_upgrades is not None)

    def refresh_state(self, rebuild=False):
        """Installed state (local, fast)."""
        scope = self.config["scope"]
        starters = {"fp": lambda cb: flatpak.installed_ids(scope, cb)}
        if self.rpm_enabled:
            starters["status"] = lambda cb: rpmostree.status(lambda info, err: cb((info, err)))
            starters["names"] = rpmostree.installed_names
        gather(starters, lambda r: self._state_loaded(r, rebuild))

    def _state_loaded(self, r, rebuild):
        info, err = r.get("status") or (None, None)
        if info is not None:
            self.info = info
        self.status_error = err
        self.fp_installed = r.get("fp", set())
        self.rpm_names = r.get("names", set())
        if self.rpm_enabled and self.catalog_ready:
            rebuild = model.ensure_layered(self.entries,
                                           self.info["head_req"] | self.info["booted_req"]) or rebuild
        self.apply(self.entries)
        self.banner.set_revealed(self.info["pending"])
        self._redraw(rebuild)

    def _redraw(self, rebuild=False):
        for f in self.feeds:
            if rebuild:
                self.apply(f.extra)
                f.refilter()
            else:
                f.update_cards()
        self.system_page.refresh()
        if self.details:
            self.details.update()

    # ----- update availability (flatpak remotes, dnf repos, rpm-ostree) -----

    def refresh_updates(self, refresh_md=False):
        if self.checking_updates:
            self._recheck = True
            return
        self.checking_updates = True
        scope = self.config["scope"]
        starters = {"fp": lambda cb: flatpak.updates(scope, cb)}
        if self.rpm_enabled:
            if catalog.dnf_available():
                starters["rpm"] = lambda cb: catalog.in_thread(
                    lambda: catalog.dnf_upgrades(refresh_md), cb)
            starters["sys"] = lambda cb: rpmostree.check_update(lambda a, v, e: cb((a, v, e)))
            self.sys["checking"] = True
        self.system_page.refresh()
        for f in self.feeds:
            f.update_cards()
        gather(starters, self._updates_loaded)

    def _updates_loaded(self, r):
        self.fp_updates = r.get("fp")
        rpm = r.get("rpm")
        self.rpm_upgrades = None if (rpm is None or isinstance(rpm, Exception)) else set(rpm)
        if "sys" in r:
            available, version, err = r["sys"]
            self.sys = {"checking": False, "available": available, "version": version, "error": err}
        self.checking_updates = False
        self.apply(self.entries)
        self._redraw()
        if self._recheck:
            self._recheck = False
            self.refresh_updates()

    # ----- install / update / uninstall (only ever from an explicit button) -----

    def act(self, e, op, fmt):
        if op == "uninstall":
            source = "Flathub" if fmt == "flatpak" else "RPM package"
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="Uninstall this application?",
                body=f"Do you want to uninstall {e.name} from {source}?",
                close_response="cancel",
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("uninstall", "Uninstall")
            dialog.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.connect(
                "response",
                lambda _d, response: self._perform_action(e, op, fmt)
                if response == "uninstall" else None,
            )
            dialog.present()
            return
        self._perform_action(e, op, fmt)

    def _perform_action(self, e, op, fmt):
        scope, remote = self.config["scope"], self.config["remote"]
        if fmt == "flatpak":
            if not flatpak.valid_term(e.flatpak_id or ""):
                self.toast("Invalid Flathub app id")
                return
            argv = {"install": lambda: flatpak.install_argv(e.flatpak_id, remote, scope),
                    "update": lambda: flatpak.update_argv(e.flatpak_id, scope),
                    "uninstall": lambda: flatpak.uninstall_argv(e.flatpak_id, scope)}[op]()
            self.run_command(argv)
            return
        if not rpmostree.valid_pkg(e.rpm_pkg):
            self.toast("Invalid package name")
            return
        if op == "update":
            if e.rpm_state == "base":  # base packages change with the system deployment
                self.stack.set_visible_child(self.system_page)
                self.toast("This package is part of the system image. Update it from the System tab.")
                return
            self.update_system()
            return
        argv = (rpmostree.install_argv(e.rpm_pkg) if op == "install"
                else rpmostree.uninstall_argv(e.rpm_pkg))
        self.run_command(argv, reboot=True, kind="system")

    def update_system(self):
        self.run_command(rpmostree.update_argv(), reboot=True, kind="system")

    def set_busy(self, busy, kind=None):
        self.busy = busy
        self.op_kind = kind if busy else None
        self.spinner.set_spinning(busy)
        self.system_page.refresh()
        self.operation_status.set_visible(busy)
        if not busy:
            self.operation_progress.set_fraction(0)
            self.operation_progress.set_text("")
            self.operation_label.set_label("")

    def run_command(self, argv, reboot=False, after=None, kind="app"):
        if self.busy:
            self.toast("Another operation is still running")
            return
        command = " ".join(argv)
        verb = "Working"
        lowered = command.lower()
        for key, label in (("install", "Installing"), ("uninstall", "Uninstalling"),
                           ("remove", "Removing"), ("update", "Updating"),
                           ("upgrade", "Updating"), ("refresh", "Refreshing package data"),
                           ("check", "Checking for updates")):
            if key in lowered:
                verb = label
                break
        self.set_busy(True, kind)
        self.operation_label.set_label(f"{verb}…")
        self.operation_progress.set_fraction(0)
        self.operation_progress.pulse()
        self.console.clear()
        self.console.append("$ " + command)

        def on_line(line):
            self.console.append(line)
            text = line.strip()
            if text:
                self.operation_label.set_label(f"{verb}…  {text[:140]}")
            # Determinate progress when the package manager reports a percentage; otherwise
            # pulse to show ongoing work without inventing a percentage.
            match = re.search(r"(?<!\d)(\d{1,3})(?:\.\d+)?%", line)
            if match:
                percent = min(100, max(0, int(match.group(1))))
                self.operation_progress.set_fraction(percent / 100.0)
                self.operation_progress.set_text(f"{percent}%")
            else:
                self.operation_progress.pulse()
            if kind == "system":
                self.system_page.on_line(line)

        def done(status):
            self.console.append(f"[exit {status}]")
            if status == 0:
                self.operation_progress.set_fraction(1.0)
                self.operation_label.set_label("Completed")
            else:
                self.operation_label.set_label("Operation failed")
            self.set_busy(False)
            if status != 0:
                self.toast(f"Command failed (exit {status})")
                if kind == "system":
                    self.sys["error"] = f"The command failed (exit {status}). See the output below."
            else:
                self.toast("Done. Reboot required to apply changes." if reboot else "Done")
                if kind == "system":
                    self.sys["error"] = None
            if after and status == 0:
                after()
            else:
                self.refresh_state()
                self.refresh_updates()

        self.runner = Runner(on_line, done)  # keep a reference
        self.runner.run(argv)

    def _on_refresh(self, *_):
        """Manual refresh: re-download AppStream (Flathub) and repo metadata (rpm-ostree),
        forget cached screenshots, rebuild the catalog and re-check updates."""
        def finish():
            screenshots.clear()
            self.load_catalog(force=True)
            self.refresh_updates(True)

        def rpm_step():
            if self.rpm_enabled:
                self.run_command(rpmostree.refresh_md_argv(), kind="system", after=finish)
            else:
                finish()

        if flatpak.available():
            self.run_command(flatpak.refresh_appstream_argv(self.config["scope"]), after=rpm_step)
        else:
            rpm_step()

    # ----- menu -----

    def _build_theme_box(self):
        box = Gtk.Box(spacing=18, halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        self.theme_btns, first = {}, None
        for key, icon, tip in (("system", "video-display-symbolic", "Follow system style"),
                               ("light", "weather-clear-symbolic", "Light style"),
                               ("dark", "weather-clear-night-symbolic", "Dark style")):
            b = Gtk.ToggleButton(icon_name=icon, tooltip_text=tip)
            b.add_css_class("circular")
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", self._on_theme_toggled, key)
            self.theme_btns[key] = b
            box.append(b)
        return box

    def _build_menu_button(self):
        menu = Gio.Menu()
        theme_item = Gio.MenuItem.new(None, None)
        theme_item.set_attribute_value("custom", GLib.Variant.new_string("theme"))
        s1 = Gio.Menu()
        s1.append_item(theme_item)
        s2 = Gio.Menu()
        s2.append("Refresh Package Data", "win.refresh")
        s3 = Gio.Menu()
        s3.append("Preferences", "win.preferences")
        s3.append("About Software", "win.about")
        for s in (s1, s2, s3):
            menu.append_section(None, s)
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.add_child(self._build_theme_box(), "theme")
        return Gtk.MenuButton(icon_name="open-menu-symbolic", popover=pop, primary=True,
                              tooltip_text="Main Menu")

    # ----- preferences -----

    def _build_prefs_page(self):
        cfg = self.config
        page = Adw.PreferencesPage()

        g_look = Adw.PreferencesGroup(title="Appearance")
        self.theme_row = Adw.ComboRow(title="Color scheme",
                                      model=Gtk.StringList.new(["Follow system", "Light", "Dark"]))
        self.theme_row.connect("notify::selected", self._on_theme_row)
        g_look.add(self.theme_row)

        g_feed = Adw.PreferencesGroup(title="Feed")
        sep = Adw.SwitchRow(title="Separate feeds",
                            subtitle="Add Flathub and RPM tabs next to the unified feed")
        sep.set_active(bool(cfg["separate_feeds"]))
        sep.connect("notify::active", self._on_separate)
        g_feed.add(sep)

        g_rpm = Adw.PreferencesGroup(title="RPM")
        cli = Adw.SwitchRow(title="Show command-line tools",
                            subtitle="List RPM packages without a desktop entry in their own section")
        cli.set_active(bool(cfg["show_cli"]))
        cli.connect("notify::active", self._on_cli)
        if not self.rpm_enabled:
            g_rpm.set_sensitive(False)
        g_rpm.add(cli)

        g_gen = Adw.PreferencesGroup(title="General")
        console_row = Adw.SwitchRow(title="Show command output",
                                    subtitle="Live output of install and update commands")
        console_row.set_active(bool(cfg["show_console"]))
        console_row.connect("notify::active", self._on_console)
        g_gen.add(console_row)

        for g in (g_look, g_feed, g_rpm, g_gen):
            page.add(g)
        nav_page = Adw.NavigationPage(title="Preferences", tag="preferences")
        tv = Adw.ToolbarView(content=page)
        tv.add_top_bar(Adw.HeaderBar())
        nav_page.set_child(tv)
        return nav_page

    def _open_prefs(self, *_):
        if self.nav.get_visible_page() is not self.prefs_page:
            self.nav.push(self.prefs_page)

    def _on_separate(self, row, _p):
        self.config.set("separate_feeds", row.get_active())
        self.apply_feed_mode()

    def _on_cli(self, row, _p):
        self.config.set("show_cli", row.get_active())
        self.load_catalog()

    def _on_console(self, row, _p):
        self.config.set("show_console", row.get_active())
        self.console_revealer.set_reveal_child(row.get_active())

    # ----- theme -----

    def _on_theme_toggled(self, btn, key):
        if btn.get_active() and not self._syncing:
            self.set_theme(key)

    def _on_theme_row(self, row, _p):
        if not self._syncing:
            self.set_theme(THEMES[row.get_selected()])

    def set_theme(self, key):
        if key not in SCHEMES:
            key = "system"
        self._syncing = True
        self.config.set("theme", key)
        Adw.StyleManager.get_default().set_color_scheme(SCHEMES[key])
        self.theme_btns[key].set_active(True)
        self.theme_row.set_selected(THEMES.index(key))
        self._syncing = False

    # ----- about / reboot / toast -----
    def _show_about(self, *_):
        kwargs = dict(
            application_name="Software",
            application_icon="cw-software",
            developer_name="Carbonwarp",
            version=VERSION,
            copyright="© 2026 Carbonwarp",
            comments=(
                "Software store for RPM-based atomic operating systems, "
                "providing easy access to RPM packages, Flathub applications, "
                "and system updates.\n\n"
                "For more information, visit <a href=\"https://carbonwarp.com\">carbonwarp.com</a>."
                "\nBrowse the source code on "
                "<a href=\"https://github.com/carbonwarp/software\">Github</a>."
            ),
            website="https://carbonwarp.com",
            support_url="https://github.com/carbonwarp/software/issues",
            issue_url="https://github.com/carbonwarp/software/issues",
        )
        if hasattr(Adw, "AboutDialog"):
            Adw.AboutDialog(**kwargs).present(self)
        else:
            Adw.AboutWindow(transient_for=self, **kwargs).present()            
                
    def reboot(self):
        try:
            Gio.Subprocess.new(["systemctl", "reboot"], Gio.SubprocessFlags.NONE)
        except GLib.Error as e:
            self.toast(e.message)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast.new(text))


class StoreApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.config = Config()

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name("Software")
        quit_act = Gio.SimpleAction.new("quit", None)
        quit_act.connect("activate", lambda *_: self.quit())
        self.add_action(quit_act)
        self.set_accels_for_action("app.quit", ["<primary>q"])
        self.set_accels_for_action("win.preferences", ["<primary>comma"])

    def do_activate(self):
        win = self.props.active_window or StoreWindow(self, self.config)
        win.present()
