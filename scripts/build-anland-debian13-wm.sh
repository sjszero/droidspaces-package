#!/usr/bin/env bash
# Debian 13 ARM64 container entry for the Niri/Denial native package jobs.
# The caller supplies a disposable root container and an /output bind mount.
# Local validation may mount the uncommitted source at ANLAND_SOURCE_DIR instead
# of fetching a commit; the GitHub workflow always checks out ANLAND_COMMIT.
set -euo pipefail
: "${WM:?set WM=niri or WM=denial}"
case "$WM" in niri|denial) ;; *) echo "unsupported Debian WM: $WM" >&2; exit 1 ;; esac
[[ $(id -u) == 0 && $(uname -m) == aarch64 ]] || {
    echo 'requires a disposable root ARM64 Debian 13 CI container' >&2; exit 1;
}
source /etc/os-release
[[ $ID == debian && $VERSION_ID == 13 ]] || { echo 'requires Debian 13' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
# Dependencies must never start/restart a compositor, PipeWire or D-Bus service.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 0755 /usr/sbin/policy-rc.d
# Use the same Debian apt source/build-dep workflow as kde/Debian13_v5.
if [[ -f /etc/apt/sources.list.d/debian.sources ]]; then
    sed -i 's/^Types:[[:space:]]*deb[[:space:]]*$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources
elif [[ -f /etc/apt/sources.list ]]; then
    sed -i 's/^deb \(.*\)$/deb \1\ndeb-src \1/' /etc/apt/sources.list
else
    echo 'missing Debian apt source configuration' >&2
    exit 1
fi
apt-get update
apt-get install -y --no-install-recommends git ca-certificates curl sudo python3 util-linux
useradd -m -s /bin/bash anland
printf 'anland ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/sed\n' > /etc/sudoers.d/anland-apt
chmod 0440 /etc/sudoers.d/anland-apt
source_dir=/home/anland/.cache/anland-source
mkdir -p "$source_dir"
if [[ -n ${ANLAND_SOURCE_DIR:-} ]]; then
    [[ -d $ANLAND_SOURCE_DIR/producers/$WM/Debian13_v5 ]]
    cp -a "$ANLAND_SOURCE_DIR/." "$source_dir/"
    source_identity=local-worktree
else
    : "${ANLAND_REPO:?}" "${ANLAND_COMMIT:?}"
    [[ $ANLAND_COMMIT =~ ^[0-9a-f]{40}$ ]]
    git init --quiet "$source_dir"
    git -C "$source_dir" remote add origin "$ANLAND_REPO"
    git -C "$source_dir" fetch --quiet --depth=1 origin "$ANLAND_COMMIT"
    git -C "$source_dir" checkout --quiet --detach FETCH_HEAD
    [[ $(git -C "$source_dir" rev-parse HEAD) == "$ANLAND_COMMIT" ]]
    source_identity=$ANLAND_COMMIT
fi
recipe="$source_dir/producers/$WM/Debian13_v5"
for input in dependencies.lock xwayland.lock xwayland.patch; do test -s "$recipe/$input"; done
for script in build.sh startup.sh; do bash -n "$recipe/$script"; done
chown -R anland:anland /home/anland
mkdir -p /output
output_owner=$(stat -c '%u:%g' /output)
restore_output_owner() {
    local build_rc=$?
    trap - EXIT
    if ! chown -Rh -- "$output_owner" /output; then
        echo "failed to restore output ownership: $output_owner" >&2
        if [[ $build_rc == 0 ]]; then build_rc=1; fi
    fi
    exit "$build_rc"
}
trap restore_output_owner EXIT
chown anland:anland /output
runuser -u anland -- env HOME=/home/anland JOBS="${JOBS:-$(nproc)}" \
    DEBIAN_FRONTEND=noninteractive ANLAND_INSTALL=0 ANLAND_STAGE=build \
    ANLAND_WITH_DEPS=1 ANLAND_OFFLINE=0 ANLAND_ENGINE_MODE=auto ANLAND_WITH_DE=1 \
    WORKDIR="/home/anland/.cache/anland-$WM-debian13-v5" \
    ARTIFACTS_DIR=/output/artifacts bash "$recipe/build.sh"
cd /output/artifacts
shopt -s nullglob
packages=(*.deb)
[[ ${#packages[@]} -gt 0 ]]
found=()
for package in "${packages[@]}"; do
    [[ -f $package && ! -L $package && -s $package ]]
    [[ $(dpkg-deb -f "$package" Architecture) == arm64 ]]
    dpkg-deb --contents "$package" >/dev/null
    found+=("$(dpkg-deb -f "$package" Package)")
done
actual=$(printf '%s\n' "${found[@]}" | sort)
case "$WM" in
    niri) expected=$(printf '%s\n' niri-anland xwayland-satellite xwayland | sort) ;;
    denial) expected=$(printf '%s\n' denial-anland-engine denial denial-desktop xwayland | sort) ;;
esac
[[ $actual == "$expected" ]] || {
    printf 'Incomplete Debian 13 package set:\n%s\nExpected:\n%s\n' "$actual" "$expected" >&2
    exit 1
}
sha256sum "${packages[@]}"
chmod -R a+rX /output/artifacts
printf 'RUNNER_BUILD_ARTIFACTS_OK WM=%s DISTRO=debian13 COMMIT=%s\n' "$WM" "$source_identity"
