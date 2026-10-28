#!/usr/bin/env sh

# Stop rpm-ostree refresh to make cw-software work better

rpm-ostree cancel  >/dev/null 2>&1 || :
systemctl disable --now rpm-ostreed-automatic.timer  >/dev/null 2>&1 || :

