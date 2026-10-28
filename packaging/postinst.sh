#!/usr/bin/env sh

# Refresh meta data at post install to improve UX

flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo  >/dev/null 2>&1 || :
flatpak update --system --appstream -y --noninteractive  >/dev/null 2>&1 || :
