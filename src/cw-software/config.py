import json
import os

from gi.repository import GLib

PATH = os.path.join(GLib.get_user_config_dir(), "software", "config.json")
LEGACY_PATH = os.path.join(GLib.get_user_config_dir(), "softstore", "config.json")
DEFAULTS = {
    "theme": "system",          # system | light | dark
    "scope": "system",          # Flatpak installation (system-wide only)
    "remote": "flathub",        # Flathub remote name
    "show_console": True,
    "separate_feeds": False,    # extra Flathub / RPM tabs next to the unified feed
    "show_cli": False,          # include RPM packages without a desktop entry
}


class Config:
    def __init__(self):
        self.data = dict(DEFAULTS)
        for path in (PATH, LEGACY_PATH):  # fall back to the pre-rename config once
            try:
                with open(path) as f:
                    self.data.update(json.load(f))
                break
            except (OSError, ValueError):
                continue
        self.data["scope"] = "system"  # user installs are no longer supported
        self.data["remote"] = "flathub"  # fixed: not configurable

    def __getitem__(self, key):
        return self.data[key]

    def set(self, key, value):
        self.data[key] = value
        try:
            os.makedirs(os.path.dirname(PATH), exist_ok=True)
            with open(PATH, "w") as f:
                json.dump(self.data, f, indent=2)
        except OSError:
            pass
