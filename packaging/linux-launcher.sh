#!/usr/bin/env bash
# Launcher shim for the Linux PyInstaller bundle: the AppImage's AppRun, the .deb's
# /usr/bin/anki-miner-game and the .tar.gz's anki-miner-game all start the app through it.
# Ported from Anki Miner packaging/linux-launcher.sh at commit 9959edc9.
#
# PyInstaller's bootloader puts the bundle's _internal directory on LD_LIBRARY_PATH, and Qt pulls
# libstdc++.so.6 / libgcc_s.so.1 into it. A host driver that Qt dlopens (Mesa during GL bring-up)
# then resolves libstdc++ from the bundle; built against a newer one than the build machine's, it
# aborts the process with no Python traceback. So when the host's libstdc++ is at least as new as
# the bundled one, LD_PRELOAD it: the app and every dlopened driver share one runtime that is new
# enough. The bundled copy stays in front on hosts older than the build machine (libstdc++ is only
# forward compatible).
#
# Every step degrades to "change nothing". Never add `set -e`: a failed probe must not stop the app.

set -u

_bundle_dir="${ANKI_MINER_GAME_BUNDLE_DIR:-$(dirname "$(readlink -f "$0")")}"
_internal="$_bundle_dir/_internal"

# Highest GLIBCXX_3.4.N symbol version in a libstdc++, or "" when it cannot be read.
_max_glibcxx() {
    [ -r "$1" ] || return 0
    LC_ALL=C grep -ao 'GLIBCXX_3\.4\.[0-9]\+' "$1" 2>/dev/null | sort -u -V | tail -1
}

# Absolute path to ldconfig, or "". Debian leaves the sbin directories off a non-root user's PATH.
_ldconfig_bin() {
    local candidate
    if candidate=$(command -v ldconfig 2>/dev/null) && [ -n "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
    fi
    for candidate in /sbin/ldconfig /usr/sbin/ldconfig; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
}

# Path to the host's 64-bit copy of a soname, or "". No closing paren in the match: ldconfig can
# append an ABI note, "(libc6,x86-64, OS ABI: Linux 3.2.0)".
_host_lib() {
    local ldconfig
    ldconfig=$(_ldconfig_bin)
    [ -n "$ldconfig" ] || return 0
    "$ldconfig" -p 2>/dev/null |
        awk -v soname="$1" 'index($0, soname " (libc6,x86-64") {print $NF; exit}'
}

_prefer_host_cxx_runtime() {
    local bundled="$_internal/libstdc++.so.6"
    [ -e "$bundled" ] || return 0

    local host
    host=$(_host_lib libstdc++.so.6)
    [ -n "$host" ] && [ -r "$host" ] || return 0

    local host_ver bundled_ver newest
    host_ver=$(_max_glibcxx "$host")
    bundled_ver=$(_max_glibcxx "$bundled")
    [ -n "$host_ver" ] && [ -n "$bundled_ver" ] || return 0

    newest=$(printf '%s\n%s\n' "$host_ver" "$bundled_ver" | sort -V | tail -1)
    [ "$newest" = "$host_ver" ] || return 0

    # libgcc_s travels with libstdc++: move them together or not at all.
    local preload="$host" host_gcc
    host_gcc=$(_host_lib libgcc_s.so.1)
    if [ -n "$host_gcc" ] && [ -r "$host_gcc" ]; then
        preload="$preload:$host_gcc"
    fi

    export LD_PRELOAD="${preload}${LD_PRELOAD:+:$LD_PRELOAD}"
}

# ANKI_MINER_GAME_NO_CXX_SHIM=1 skips the shim: a way back for an unforeseen interaction.
if [ "${ANKI_MINER_GAME_NO_CXX_SHIM:-}" != "1" ]; then
    _prefer_host_cxx_runtime
fi

exec "$_bundle_dir/AnkiMinerGame" "$@"
