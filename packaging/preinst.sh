#!/usr/bin/env sh

# Stop rpm-ostree refresh to make cw-software work better

systemctl disable --now rpm-ostreed-automatic.timer  >/dev/null 2>&1 || :

