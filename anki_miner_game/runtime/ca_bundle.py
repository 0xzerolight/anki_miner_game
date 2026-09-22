"""CA certificates for stdlib HTTPS in a frozen Linux build (spec 19: the add-on bootstrap downloads ``uv``).

The bundle carries its own OpenSSL, which looks for CA certificates where the build machine's
OpenSSL keeps them (``openssl_cafile`` and ``openssl_capath`` of ``ssl.get_default_verify_paths()``).
A distribution that keeps them elsewhere has neither, and every certificate check fails. Then
``use_distro_ca_bundle`` points ``SSL_CERT_FILE`` at the first distribution bundle that exists;
OpenSSL reads the variable each time a context loads its default certificates, so the call has to
come before the first HTTPS request. A ``SSL_CERT_FILE`` or ``SSL_CERT_DIR`` the user set is never
replaced. A source run is left alone, and so is a Windows build, which reads the system store.
"""

import logging
import os
import ssl
import sys
from collections.abc import MutableMapping, Sequence
from typing import Final

log = logging.getLogger(__name__)

CERT_FILE_ENV: Final = "SSL_CERT_FILE"
CERT_DIR_ENV: Final = "SSL_CERT_DIR"

CA_BUNDLES: Final = (
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch, Gentoo
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # RHEL, CentOS
    "/etc/ssl/cert.pem",  # Alpine
)
"""Where distributions keep their CA bundle (the files Go's ``crypto/x509`` looks for on Linux)."""


def use_distro_ca_bundle(
    environ: MutableMapping[str, str] | None = None,
    *,
    frozen: bool | None = None,
    platform: str | None = None,
    defaults: ssl.DefaultVerifyPaths | None = None,
    bundles: Sequence[str] | None = None,
) -> str | None:
    """Set ``SSL_CERT_FILE`` in ``environ`` (default ``os.environ``) when OpenSSL has no CA certificates.

    Only in a frozen Linux build whose OpenSSL default CA file and directory are both missing, and
    only when neither ``SSL_CERT_FILE`` nor ``SSL_CERT_DIR`` is set. Returns the bundle it set, or
    ``None``. ``frozen`` defaults to ``sys.frozen``, ``platform`` to ``sys.platform``, ``defaults``
    to ``ssl.get_default_verify_paths()`` and ``bundles`` to ``CA_BUNDLES``.
    """
    env = os.environ if environ is None else environ
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen or not (platform or sys.platform).startswith("linux"):
        return None
    if env.get(CERT_FILE_ENV) or env.get(CERT_DIR_ENV):
        return None
    paths = ssl.get_default_verify_paths() if defaults is None else defaults
    if os.path.isfile(paths.openssl_cafile) or os.path.isdir(paths.openssl_capath):
        return None
    for candidate in CA_BUNDLES if bundles is None else bundles:
        if os.path.isfile(candidate):
            env[CERT_FILE_ENV] = candidate
            log.info(
                "OpenSSL's CA file %s and directory %s are missing; %s=%s",
                paths.openssl_cafile,
                paths.openssl_capath,
                CERT_FILE_ENV,
                candidate,
            )
            return candidate
    log.warning(
        "OpenSSL's CA file %s and directory %s are missing and no distribution CA bundle was found; "
        "HTTPS downloads will fail (set %s to a CA bundle)",
        paths.openssl_cafile,
        paths.openssl_capath,
        CERT_FILE_ENV,
    )
    return None
