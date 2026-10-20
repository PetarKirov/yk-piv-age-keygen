#
# Copyright 2026 Philip Eklöf
#
# SPDX-License-Identifier: MIT
#
# ruff: noqa: S101 # Use of `assert` detected
# ruff: noqa: INP001 # File is part of an implicit namespace package

"""Tests for the yk-piv-age-keygen CLI, with the YubiKey mocked out."""

import contextlib
import getpass
import logging
import warnings
from types import SimpleNamespace
from unittest import mock

import click
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from yubikit.core import NotSupportedError
from yubikit.core.smartcard import SW, ApduError

from yk_piv_age_keygen import cli


class FakeExclusiveConnection:
    """Stand-in for pyscard's exclusive connection decorator."""


@pytest.fixture
def keygen() -> cli.Keygen:
    """Return a Keygen for slot 20 with a mocked PIV session."""
    kg = cli.Keygen(slot=20, pin="654321")
    kg.session = mock.Mock()
    return kg


def set_stdin_tty(monkeypatch: pytest.MonkeyPatch, *, tty: bool) -> None:
    """Make stdin look like a terminal (or a pipe)."""
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: tty))


def device_list(*serials: int | None) -> list[tuple[str, SimpleNamespace]]:
    """Return a device enumeration with the given serials."""
    return [(f"dev{i}", SimpleNamespace(serial=s)) for i, s in enumerate(serials)]


def occupy_slot(kg: cli.Keygen, cn: str = "age identity deadbeef") -> None:
    """Make the mocked slot contain a key and a certificate with the given CN."""
    crt = mock.Mock()
    crt.subject.get_attributes_for_oid.return_value = [SimpleNamespace(value=cn)]
    kg.session.get_certificate.return_value = crt


def test_format_sw() -> None:
    """Known status words are named, unknown ones are shown in hex."""
    assert cli.format_sw(SW.FILE_NOT_FOUND) == "FILE_NOT_FOUND"
    assert cli.format_sw(0x6F42) == "0x6F42"


def test_constructor_defaults() -> None:
    """The constructor defaults are valid."""
    kg = cli.Keygen()
    assert kg.pin_policy == cli.PinPolicy.ONCE


@pytest.mark.parametrize("serial", [0, -1])
def test_non_positive_serial_rejected(serial: int) -> None:
    """Serials <= 0 are rejected rather than treated as "no serial"."""
    with pytest.raises(cli.KeygenError, match="not a positive integer"):
        cli.Keygen(serial=serial)


class TestPromptSecret:
    """Reading secrets interactively."""

    def test_hidden_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Secrets are read with echo disabled."""
        monkeypatch.setattr(getpass, "getpass", lambda _prompt: "654321")
        assert cli.prompt_secret("PIN") == "654321"

    def test_echo_not_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If echo can't be disabled, nothing is read and an error is raised."""
        read = mock.Mock(return_value="654321")

        def fallback_getpass(prompt: str) -> str:
            warnings.warn("Can not control echo", getpass.GetPassWarning, stacklevel=1)
            return read(prompt)

        monkeypatch.setattr(getpass, "getpass", fallback_getpass)
        with pytest.raises(cli.KeygenError, match="--pin-stdin"):
            cli.prompt_secret("PIN")
        read.assert_not_called()

    def test_pin_prompt_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The PIN isn't verified if it couldn't be read securely."""
        kg = cli.Keygen()
        kg.session = mock.Mock()
        monkeypatch.setattr(cli, "yk_get_pivman_data", mock.Mock())
        monkeypatch.setattr(
            cli,
            "prompt_secret",
            mock.Mock(side_effect=cli.KeygenError("echo")),
        )
        with pytest.raises(cli.KeygenError, match="echo"):
            kg.authenticate_piv()
        kg.session.verify_pin.assert_not_called()


class TestGetDevice:
    """Device selection."""

    @pytest.fixture(autouse=True)
    def _scripting_device(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cli,
            "YubiKeyScriptingDevice",
            lambda dev, info: SimpleNamespace(dev=dev, info=info),
        )

    def test_selects_by_serial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The device with the requested serial is selected."""
        monkeypatch.setattr(cli, "yk_list_all_devices", lambda: device_list(1, 2))
        kg = cli.Keygen(serial=2)
        kg.get_device()
        assert kg.device.dev == "dev1"

    def test_serial_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing serial fails, even if only one device is connected."""
        monkeypatch.setattr(cli, "yk_list_all_devices", lambda: device_list(1))
        kg = cli.Keygen(serial=2)
        with pytest.raises(cli.KeygenDeviceSelectionError):
            kg.get_device()
        assert kg.device is None

    def test_multiple_devices_need_serial(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without a serial, multiple devices are ambiguous."""
        monkeypatch.setattr(cli, "yk_list_all_devices", lambda: device_list(1, 2))
        kg = cli.Keygen()
        with pytest.raises(cli.KeygenDeviceSelectionError, match="Multiple"):
            kg.get_device()

    def test_device_without_serial(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A device without a serial is selected and logged without one."""
        monkeypatch.setattr(cli, "yk_list_all_devices", lambda: device_list(None))
        kg = cli.Keygen()
        with caplog.at_level(logging.INFO):
            kg.get_device()
        assert kg.serial is None
        assert "without a readable serial" in caplog.text


class TestGetSession:
    """Opening the PIV session."""

    @pytest.fixture(autouse=True)
    def _exclusive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cli,
            "ExclusiveConnectCardConnection",
            FakeExclusiveConnection,
        )
        monkeypatch.setattr(cli, "YubiKeyPivSession", mock.Mock())

    def test_exclusive_connection(self) -> None:
        """An exclusive connection is used for the session."""
        connection = mock.Mock(connection=FakeExclusiveConnection())
        kg = cli.Keygen()
        kg.device = mock.Mock(**{"smart_card.return_value": connection})
        kg.get_session()
        cli.YubiKeyPivSession.assert_called_once_with(connection)

    def test_shared_connection_refused(self) -> None:
        """A shared connection is closed and refused."""
        connection = mock.Mock(connection=object())
        kg = cli.Keygen()
        kg.device = mock.Mock(**{"smart_card.return_value": connection})
        with pytest.raises(cli.KeygenError, match="exclusive access"):
            kg.get_session()
        connection.close.assert_called_once()
        assert kg.session is None
        cli.YubiKeyPivSession.assert_not_called()


class TestCheckSlotEmpty:
    """Detecting and replacing existing keys."""

    def test_empty_slot(self, keygen: cli.Keygen) -> None:
        """An empty slot passes without confirmation."""
        keygen.session.get_slot_metadata.side_effect = ApduError(
            b"",
            SW.REFERENCE_DATA_NOT_FOUND,
        )
        keygen.session.get_certificate.side_effect = ApduError(
            b"",
            SW.FILE_NOT_FOUND,
        )
        with mock.patch.object(click, "confirm") as confirm:
            keygen.check_slot_empty()
        confirm.assert_not_called()

    def test_occupied_without_force_stops_before_pin(
        self,
        keygen: cli.Keygen,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An occupied slot fails before the PIN is used."""
        occupy_slot(keygen)
        monkeypatch.setattr(keygen, "get_device", lambda: None)
        monkeypatch.setattr(keygen, "get_session", lambda: None)
        with pytest.raises(cli.KeygenError, match="use --force"):
            keygen.main()
        keygen.session.verify_pin.assert_not_called()
        keygen.session.generate_key.assert_not_called()

    def test_force_confirms_on_tty(
        self,
        keygen: cli.Keygen,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """With --force on a terminal, the identity is named and confirmed."""
        keygen.force = True
        occupy_slot(keygen)
        set_stdin_tty(monkeypatch, tty=True)
        with mock.patch.object(click, "confirm") as confirm:
            keygen.check_slot_empty()
        confirm.assert_called_once()
        assert "age identity deadbeef" in caplog.text
        assert "permanently destroys" in caplog.text

    def test_force_declined_aborts(
        self,
        keygen: cli.Keygen,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Declining the confirmation aborts."""
        keygen.force = True
        occupy_slot(keygen)
        set_stdin_tty(monkeypatch, tty=True)
        monkeypatch.setattr(click, "confirm", mock.Mock(side_effect=click.Abort))
        with pytest.raises(click.Abort):
            keygen.check_slot_empty()

    def test_force_with_piped_stdin(
        self,
        keygen: cli.Keygen,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """With piped stdin, --force is the confirmation, but still warns."""
        keygen.force = True
        occupy_slot(keygen)
        set_stdin_tty(monkeypatch, tty=False)
        with mock.patch.object(click, "confirm") as confirm:
            keygen.check_slot_empty()
        confirm.assert_not_called()
        assert "permanently destroys" in caplog.text

    @pytest.fixture
    def no_metadata_support(self, keygen: cli.Keygen) -> None:
        """Simulate firmware < 5.3 with no certificate in the slot."""
        keygen.session.get_slot_metadata.side_effect = NotSupportedError("5.3")
        keygen.session.get_certificate.side_effect = ApduError(
            b"",
            SW.FILE_NOT_FOUND,
        )

    @pytest.mark.usefixtures("no_metadata_support")
    def test_undetectable_key_needs_force(
        self,
        keygen: cli.Keygen,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On firmware < 5.3, a possible key isn't overwritten without --force."""
        monkeypatch.setattr(keygen, "get_device", lambda: None)
        monkeypatch.setattr(keygen, "get_session", lambda: None)
        with pytest.raises(cli.KeygenError, match="can't detect"):
            keygen.main()
        keygen.session.verify_pin.assert_not_called()
        keygen.session.generate_key.assert_not_called()

    @pytest.mark.usefixtures("no_metadata_support")
    def test_undetectable_key_with_force(
        self,
        keygen: cli.Keygen,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """On firmware < 5.3, --force replaces any key, with a warning."""
        keygen.force = True
        set_stdin_tty(monkeypatch, tty=False)
        keygen.check_slot_empty()
        assert "requires 5.3+" in caplog.text
        assert "Replacing any key" in caplog.text


class TestGeneration:
    """Key generation failures and cleanup."""

    @pytest.fixture(autouse=True)
    def _authenticated(self, keygen: cli.Keygen) -> None:
        keygen.authenticated = True

    @pytest.mark.parametrize(
        "sw",
        [SW.SECURITY_CONDITION_NOT_SATISFIED, SW.INCORRECT_PARAMETERS, 0x6400],
    )
    def test_rejected_generation(
        self,
        keygen: cli.Keygen,
        caplog: pytest.LogCaptureFixture,
        sw: int,
    ) -> None:
        """A rejected command left the slot untouched, so no warning."""
        keygen.session.generate_key.side_effect = ApduError(b"", sw)
        with pytest.raises(ApduError):
            keygen.generate_key()
        assert "may now contain" not in caplog.text

    @pytest.mark.parametrize("sw", [SW.MEMORY_FAILURE, 0x6300, 0x6F00, 0x9999])
    def test_ambiguous_generation_failure(
        self,
        keygen: cli.Keygen,
        caplog: pytest.LogCaptureFixture,
        sw: int,
    ) -> None:
        """An error that may have written to the slot leaves it uncertain."""
        keygen.session.generate_key.side_effect = ApduError(b"", sw)
        with pytest.raises(ApduError):
            keygen.generate_key()
        assert f"failed ({cli.format_sw(sw)})" in caplog.text
        assert "may now contain" in caplog.text
        keygen.session.delete_key.assert_not_called()

    def test_interrupted_generation(
        self,
        keygen: cli.Keygen,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A lost response leaves the slot uncertain, nothing is deleted."""
        keygen.session.generate_key.side_effect = OSError("device removed")
        with pytest.raises(OSError, match="device removed"):
            keygen.generate_key()
        assert "interrupted" in caplog.text
        assert "ykman piv info" in caplog.text
        keygen.session.delete_key.assert_not_called()
        keygen.session.delete_certificate.assert_not_called()

    def test_certificate_failure_cleans_up(
        self,
        keygen: cli.Keygen,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed certificate generation deletes the new key again."""
        for step in ("get_device", "get_session", "check_slot_empty"):
            monkeypatch.setattr(keygen, step, lambda: None)
        monkeypatch.setattr(keygen, "authenticate_piv", lambda: None)
        monkeypatch.setattr(
            keygen,
            "generate_certificate",
            mock.Mock(side_effect=RuntimeError("touch timeout")),
        )
        with pytest.raises(RuntimeError, match="touch timeout"):
            keygen.main()
        keygen.session.delete_certificate.assert_called_once_with(keygen.slot)
        keygen.session.delete_key.assert_called_once_with(keygen.slot)


class TestCleanup:
    """Deleting the new key after a failed certificate generation."""

    def test_complete_cleanup(
        self,
        keygen: cli.Keygen,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The slot is only reported empty if both deletions succeeded."""
        with caplog.at_level(logging.INFO):
            keygen.delete_key()
        assert "is now empty" in caplog.text
        assert "may still contain" not in caplog.text

    @pytest.mark.parametrize(
        ("crt_error", "key_error", "leftovers"),
        [
            (OSError("crt"), None, ["certificate"]),
            (None, OSError("key"), ["the new key"]),
            (None, NotSupportedError("5.7"), ["the new key"]),
            (OSError("crt"), NotSupportedError("5.7"), ["the new key", "certificate"]),
        ],
    )
    def test_partial_cleanup(
        self,
        keygen: cli.Keygen,
        caplog: pytest.LogCaptureFixture,
        crt_error: Exception | None,
        key_error: Exception | None,
        leftovers: list[str],
    ) -> None:
        """Whatever couldn't be deleted is reported, never an empty slot."""
        keygen.session.delete_certificate.side_effect = crt_error
        keygen.session.delete_key.side_effect = key_error
        with caplog.at_level(logging.INFO):
            keygen.delete_key()
        assert "is now empty" not in caplog.text
        assert "may still contain" in caplog.text
        for leftover in leftovers:
            assert leftover in caplog.text
        assert ("must not be used" in caplog.text) == (crt_error is not None)


class TestName:
    """The identity name stored in the certificate."""

    @pytest.fixture(autouse=True)
    def _software_signing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sign certificates with a software key instead of the YubiKey."""
        key = ec.generate_private_key(ec.SECP256R1())
        monkeypatch.setattr(cli, "yk_prompt_timeout", contextlib.nullcontext)
        monkeypatch.setattr(
            cli,
            "yk_sign_certificate_builder",
            lambda _session, _slot, _key_type, builder: builder.sign(
                key,
                hashes.SHA256(),
            ),
        )

    @staticmethod
    def certificate_cn(kg: cli.Keygen) -> str:
        """Generate a certificate and return its CN."""
        kg.session = mock.Mock()
        kg.pubkey = ec.generate_private_key(ec.SECP256R1()).public_key()
        kg.generate_certificate()
        cns = kg.crt.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return cns[0].value

    def test_default_name(self) -> None:
        """Without a name, the CN matches age-plugin-yubikey's default."""
        kg = cli.Keygen()
        assert self.certificate_cn(kg) == f"age identity {kg._pubkey_tag()}"  # noqa: SLF001

    def test_custom_name(self) -> None:
        """A given name is used as the CN."""
        assert self.certificate_cn(cli.Keygen(name="laptop")) == "laptop"

    def test_custom_name_unicode(self) -> None:
        """Printable non-ASCII names and spaces are allowed."""
        assert self.certificate_cn(cli.Keygen(name="Büro laptop")) == "Büro laptop"

    @pytest.mark.parametrize("name", ["", "x" * 65])
    def test_invalid_name_length(self, name: str) -> None:
        """Names that don't fit in a CN are rejected."""
        with pytest.raises(cli.KeygenError, match="1-64 characters"):
            cli.Keygen(name=name)

    @pytest.mark.parametrize(
        "name",
        ["a\nAGE-PLUGIN-YUBIKEY-1", "a\tb", "\x1b[31mred", "a\u2028b"],
    )
    def test_non_printable_name(self, name: str) -> None:
        """Control and line separator characters are rejected."""
        with pytest.raises(cli.KeygenError, match="non-printable"):
            cli.Keygen(name=name)


class TestWarnings:
    """Warnings about weak setups."""

    def test_weak_policies(self, caplog: pytest.LogCaptureFixture) -> None:
        """PIN and touch policy both "never" is warned about."""
        cli.Keygen(pin_policy="never", touch_policy="never").warn_weak_policies()
        assert "both 'never'" in caplog.text

    def test_default_policies(self, caplog: pytest.LogCaptureFixture) -> None:
        """The default policies aren't warned about."""
        cli.Keygen().warn_weak_policies()
        assert not caplog.text

    def test_derived_management_key(
        self,
        keygen: cli.Keygen,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A PIN-derived management key is warned about."""
        keygen.mgmt_key_derived = True
        keygen.warn_weak_credentials()
        assert "derived from the PIN" in caplog.text
