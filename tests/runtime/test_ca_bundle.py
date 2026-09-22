"""A frozen Linux build finds CA certificates for stdlib HTTPS (spec 19, the add-on bootstrap): when the
bundled OpenSSL's own CA file and directory are both missing, ``SSL_CERT_FILE`` names the first
distribution bundle that exists."""

import logging
import os
import ssl
from pathlib import Path

from anki_miner_game.runtime.ca_bundle import CA_BUNDLES, CERT_DIR_ENV, CERT_FILE_ENV, use_distro_ca_bundle


def verify_paths(cafile: Path, capath: Path) -> ssl.DefaultVerifyPaths:
    """What ``ssl.get_default_verify_paths()`` reports for an OpenSSL built with these defaults."""
    return ssl.DefaultVerifyPaths(
        cafile=None,
        capath=None,
        openssl_cafile_env=CERT_FILE_ENV,
        openssl_cafile=str(cafile),
        openssl_capath_env=CERT_DIR_ENV,
        openssl_capath=str(capath),
    )


def bundle(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_text("-----BEGIN CERTIFICATE-----\n", encoding="ascii")
    return str(path)


def frozen_linux(
    tmp_path: Path, env: dict[str, str], bundles: list[str], defaults: ssl.DefaultVerifyPaths | None = None
):
    missing = verify_paths(tmp_path / "no-cert.pem", tmp_path / "no-certs")
    return use_distro_ca_bundle(env, frozen=True, platform="linux", defaults=defaults or missing, bundles=bundles)


def test_both_defaults_missing_points_ssl_cert_file_at_the_first_bundle_that_exists(tmp_path):
    first, second = bundle(tmp_path, "a.crt"), bundle(tmp_path, "b.crt")
    env = {"OTHER": "1"}
    assert frozen_linux(tmp_path, env, [str(tmp_path / "absent.crt"), first, second]) == first
    assert env == {"OTHER": "1", CERT_FILE_ENV: first}


def test_an_existing_default_ca_file_is_left_to_openssl(tmp_path):
    env: dict[str, str] = {}
    defaults = verify_paths(Path(bundle(tmp_path, "cert.pem")), tmp_path / "no-certs")
    assert frozen_linux(tmp_path, env, [bundle(tmp_path, "a.crt")], defaults) is None
    assert env == {}


def test_an_existing_default_ca_directory_is_left_to_openssl(tmp_path):
    env: dict[str, str] = {}
    (tmp_path / "certs").mkdir()
    defaults = verify_paths(tmp_path / "no-cert.pem", tmp_path / "certs")
    assert frozen_linux(tmp_path, env, [bundle(tmp_path, "a.crt")], defaults) is None
    assert env == {}


def test_the_users_own_ssl_cert_file_or_dir_is_never_replaced(tmp_path):
    for var in (CERT_FILE_ENV, CERT_DIR_ENV):
        env = {var: str(tmp_path / "chosen-by-the-user")}
        assert frozen_linux(tmp_path, env, [bundle(tmp_path, "a.crt")]) is None
        assert env == {var: str(tmp_path / "chosen-by-the-user")}


def test_a_source_run_and_a_windows_build_are_left_alone(tmp_path):
    missing = verify_paths(tmp_path / "no-cert.pem", tmp_path / "no-certs")
    found = [bundle(tmp_path, "a.crt")]
    for frozen, platform in ((False, "linux"), (True, "win32")):
        env: dict[str, str] = {}
        assert use_distro_ca_bundle(env, frozen=frozen, platform=platform, defaults=missing, bundles=found) is None
        assert env == {}


def test_no_bundle_anywhere_changes_nothing_and_says_so(tmp_path, caplog):
    env: dict[str, str] = {}
    with caplog.at_level(logging.WARNING, logger="anki_miner_game.runtime.ca_bundle"):
        assert frozen_linux(tmp_path, env, [str(tmp_path / "absent.crt")]) is None
    assert env == {}
    assert "HTTPS" in caplog.text


def test_the_default_call_leaves_a_source_run_alone(monkeypatch):
    monkeypatch.setenv(CERT_FILE_ENV, "set-by-this-test")
    monkeypatch.delenv(CERT_FILE_ENV)
    assert use_distro_ca_bundle() is None
    assert CERT_FILE_ENV not in os.environ


def test_the_bundles_cover_the_common_distributions():
    assert "/etc/ssl/certs/ca-certificates.crt" in CA_BUNDLES  # Debian, Ubuntu, Arch
    assert "/etc/pki/tls/certs/ca-bundle.crt" in CA_BUNDLES  # Fedora, RHEL
