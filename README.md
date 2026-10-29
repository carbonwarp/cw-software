<div align="center">
  <img src=packaging/software.svg alt=cw-software width=200 />
</div>

# Software

## CW Software

Software store for RPM-based atomic operating systems with a unified interface to manage system packages, sandboxed applications from Flathub, and system updates.

[Get .rpm](github.com/carbonwarp/cw-software/releases/latest/cw-software.rpm)

### Key Features

- **Atomic-First:** Built specifically for atomic operating systems, with their unique package management workflows in mind.
- **Fast and Reliable:** Designed to provide a smooth and dependable software management experience.
- **Filling the Gap:** Addresses the lack of a dedicated, unified software store tailored to atomic systems.

The project's scope is limited to atomic operating systems with a focus on a native GNOME look and feel.

**Supported Platforms:** Alma Linux Bootc, Fedora Atomic Desktops, and RHEL for Edge.



### Build from Source

You can use the application via Python or package as rpm and use it. 

**1. via Python**

```
git clone git@github.com:carbonwarp/cw-software.git --depth 1
cd cw-software/src
python -m cw-software
```

**2. Package from source**

> Required: nfpm and ascii armored gpg key (private.asc at repo root).

```
git clone git@github.com:carbonwarp/cw-software.git --depth 1
cd cw-software && make VERSION="1.4.3"
rpm-ostree install dist/*.rpm
```

You can also use the following url to get the latest rpm package of cw-software which you can download and install or layer at build time.
```
https://dl.carbonwarp.com/cw-software.rpm
```
### Contributing

Contributions are welcome, provided that both the contributor and the reviewer manually review the changes before they are accepted.
By contributing to this project, you agree to license your contributions as per the MIT License.

### License

CW Software is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
