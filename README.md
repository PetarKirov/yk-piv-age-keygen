<!--
SPDX-FileCopyrightText: 2026 Philip Eklöf

SPDX-License-Identifier: MIT
-->

# yk-piv-age-keygen

This tool generates keys+certificates in YubiKey PIV that are compatible with
[age-plugin-yubikey](https://github.com/str4d/age-plugin-yubikey), **on
YubiKeys configured with an AES management key** (supported since firmware 5.4,
and the default since 5.7). age-plugin-yubikey can't authenticate with AES
management keys yet, so `age-plugin-yubikey --generate` doesn't work on those.
This tool performs the same operation, from the command line rather than via an
interactive text menu.

While age-plugin-yubikey is written in rust, yk-piv-age-keygen performs the
very same thing with python, via the libs provided by
[yubikey-manager](https://github.com/Yubico/yubikey-manager).

## Quick start

1. Install the tool, along with `age` and `age-plugin-yubikey` to use the key:
   ```
   $ nix profile add github:PetarKirov/yk-piv-age-keygen \
       nixpkgs#age nixpkgs#age-plugin-yubikey
   ```

2. Generate a key in retired slot 20, entering the PIN and touching the YubiKey
   when asked, and save the age identity for it:
   ```
   $ yk-piv-age-keygen --slot 20 --name laptop \
       --pin-policy once --touch-policy always
   $ mkdir -p ~/.config/age
   $ age-plugin-yubikey --identity --slot 20 > ~/.config/age/laptop-yubikey.txt
   ```
   Keys go in one of the 20 retired key management slots, keeping the primary
   slots (e.g. 9a for SSH or smart card login) free. Slot 20 is an arbitrary
   choice that stays clear of `age-plugin-yubikey --generate`, which fills them
   from slot 1 upwards.
   The identity file only identifies the key, the private key itself never
   leaves the YubiKey. If the management key isn't stored on the YubiKey
   (see [below](#starting-from-a-reset-yubikey)), you're also asked for it.

3. Encrypt a file to the key:
   ```
   $ age --encrypt --identity ~/.config/age/laptop-yubikey.txt \
       --output secret.txt.age secret.txt
   ```
   To let others encrypt to it, share the recipient (`age1yubikey1...`) printed
   by `age-plugin-yubikey --list --slot 20`.

4. Decrypt it, entering the PIN and touching the YubiKey when asked:
   ```
   $ age --decrypt --identity ~/.config/age/laptop-yubikey.txt secret.txt.age
   ```

If other PIV slots on the YubiKey hold Ed25519 or X25519 certificates (5.7+),
age-plugin-yubikey 0.5.0 fails with "invalid object". See [Why?](#why) for a
patched fork.

## Starting from a reset YubiKey

To set up the PIV application from scratch, reset it and replace the default
credentials before following the [quick start](#quick-start).

**`ykman piv reset` erases all PIV keys and certificates on the YubiKey,
including existing age identities. Anything encrypted to them can no longer be
decrypted.**

```
$ nix profile add nixpkgs#yubikey-manager
$ ykman piv reset
$ ykman piv access change-pin
$ ykman piv access change-puk
$ ykman piv access change-management-key --algorithm aes256 --protect
```

After the reset, the PIN is `123456`, the PUK is `12345678` and the management
key has its well-known default value, so press Enter when asked for the current
management key. The last command replaces it with a random AES-256 key (YubiKey
5.4+), stored on the YubiKey and protected by the PIN, so you never have to
enter it. Then continue with step 2 of the quick start.

## Changes in this fork

This fork of
[phiekl/yk-piv-age-keygen](https://github.com/phiekl/yk-piv-age-keygen)
hardens the tool against leaking secrets and destroying keys by accident:

- The PIN and management key are read from stdin (`--pin-stdin`,
  `--mgmt-key-stdin`) or prompted for, never taken as arguments, and the
  management key is never included in error messages.
- The tool refuses to run without exclusive access to the YubiKey, so no other
  process can change the slot between checking it and generating the key.
- `--serial 0` is rejected instead of silently selecting the only connected
  YubiKey.
- `--force` names the age identity it is about to destroy and asks for
  confirmation when stdin is a terminal. On YubiKeys older than 5.3, which
  can't report whether a slot holds a key, `--force` is always required.
- If certificate generation fails, the new key is deleted again. If key
  generation itself is interrupted, the tool says the slot state is uncertain
  and how to recover.
- `--name` sets the identity name, like `age-plugin-yubikey --generate --name`.
  Names with control or other non-printable characters are rejected.
- Warnings are logged for a default PIN or management key, a PIN-derived
  management key, and PIN and touch policies that are both `never`.
- Unknown APDU status words are reported instead of crashing, and the `Keygen`
  class has a valid default PIN policy.
- Dependencies are upgraded (cryptography 50, yubikey-manager 5.9), there is a
  Nix package and dev shell built from the lock files, and mocked tests run
  with `pytest`.


## Why?

YubiKey 5.4+ supports AES management keys (the default since 5.7), which
*should* be used rather than the older Triple-DES variants. Unfortunately, this
support has not landed in age-plugin-yubikey yet ([issue
#92](https://github.com/str4d/age-plugin-yubikey/issues/92)), meaning that you
currently can't generate keys with `age-plugin-yubikey` if your YubiKey is
configured using an AES management key. For now, `yk-piv-age-keygen` fills that
gap.

There's some other non-closed issues with YubiKey 5.7 at this time, as well.
Combining `yk-piv-age-keygen` with my tiny
[age-plugin-yubikey-57](https://github.com/phiekl/age-plugin-yubikey-57) fork
simply makes age-plugin-yubikey usable with YubiKey >=5.7.

**Hence, this project is pointless and obsolete as soon as those issuses are
fixed.** Instead, it can be seen as an example on how to use the ykman/yubikit
libs, at least for PIV operations.

## Installation

The tool is packaged as a [Nix](https://nixos.org) flake (flakes must be
enabled). Install it into your profile with:
```
$ nix profile add github:PetarKirov/yk-piv-age-keygen
```
or try it without installing, using
`nix run github:PetarKirov/yk-piv-age-keygen -- --help`. The package only adds
the `yk-piv-age-keygen` command to your `PATH`. The toolchain is pinned by
`flake.lock`, and every Python dependency is built by Nix from the versions and
hashes pinned in `uv.lock` (via
[uv2nix](https://github.com/pyproject-nix/uv2nix)).

pcscd must be running to talk to the YubiKey. On NixOS, enable it with
`services.pcscd.enable = true;`. If `ykman piv info` works, so should this.

## Development

`nix develop` provides a dev shell with `yk-piv-age-keygen` (editable), `uv`,
`ruff`, `reuse` and `pytest`. Run the tests with `pytest`. After changing
dependencies, run `uv lock` inside the shell and re-enter it.

## Usage

```
$ yk-piv-age-keygen --help
usage: yk-piv-age-keygen [-h] [-v] [-q] [-S <num>] -s {1..20} [--pin-stdin]
                         [--mgmt-key-stdin] [-P {never,once,always}]
                         [-T {never,always,cached}] [--name <name>] [--force]

options:
  -h, --help            show this help message and exit
  -v, --verbose         increase the verbosity level
  -q, --quiet           decrease log level to WARNING
  -S, --serial <num>    use the YubiKey with this serial (required with
                        multiple devices)
  -s, --slot {1..20}    generate a key and certificate in this retired slot
  --pin-stdin           read the PIN from the first line of stdin (requested
                        interactively if not specified)
  --mgmt-key-stdin      read the management key from the next line of stdin
                        (requested interactively if needed and not specified)
  -P, --pin-policy {never,once,always}
                        new PIN policy for slot (default: once)
  -T, --touch-policy {never,always,cached}
                        new touch policy for slot (default: always)
  --name <name>         name for the generated identity (default: 'age
                        identity HEX_TAG')
  --force               replace an existing key/certificate in the slot,
                        permanently destroying it (asks for confirmation if
                        stdin is a terminal)
```

Secrets are never accepted as arguments, since those are visible to other
users via `ps` and end up in shell history. For non-interactive use, pipe them
in instead, e.g. from a password manager:
```
$ pass show yubikey/piv-pin | yk-piv-age-keygen -s 20 --pin-stdin
$ { pass show yubikey/piv-pin; pass show yubikey/piv-mgmt-key; } \
    | yk-piv-age-keygen -s 20 --pin-stdin --mgmt-key-stdin
```

Without `--force`, the tool refuses to touch a slot that already contains a key
or certificate. With `--force`, the existing key is destroyed for good: anything
encrypted to its age recipient can no longer be decrypted. The identity being
replaced is logged first, and if stdin is a terminal you are asked to confirm.
When secrets are piped in, `--force` itself counts as the confirmation.

YubiKeys older than 5.3 can't report whether a slot contains a key, only
whether it contains a certificate. Since a key without a certificate can't be
ruled out, `--force` is always required on those.

If generating the certificate fails (e.g. the touch prompt times out), the new
key and any stale certificate are deleted from the slot again (deleting keys
requires YubiKey 5.7+).

## Example

Generate a new key+certificate in the reserved slot #20:
```
$ yk-piv-age-keygen -s 20
INFO: Found YubiKey with serial '12345678'.
INFO: No existing key found in PIV slot 95 (RETIRED20).
INFO: No existing certificate found in PIV slot 95 (RETIRED20).
Enter PIN for YubiKey with serial '12345678' (default: 123456):
INFO: Management key fetched from data protected by PIN.
INFO: Authenticating ...
Touch your YubiKey...
INFO: Successfully authenticated.
INFO: Generating ECCP256 key in slot 95 (RETIRED20) ...
INFO: Private key generated in slot 95 (RETIRED20) of type ECCP256
INFO: Generating certificate ...
Touch your YubiKey...
INFO: Data written to object slot 0x5fc120
INFO: Certificate written to slot 95 (RETIRED20), compression=False
INFO: Data written to object slot 0x5fc102
```

... which would make it show up like this:

```
$ ykman piv info
PIV version:              5.7.1
PIN tries remaining:      3/3
PUK tries remaining:      3/3
Management key algorithm: AES192
Management key is stored on the YubiKey, protected by PIN.
CHUID: d0c674ab66d02f0c09f555ac70216de537490f910da2088cd46933d6775d56781a71f152211a12b104cc8d471884bed16289af118daad8c7ab3d6d
CCC:   No data available
Slot 95 (RETIRED20):
  Private key type: ECCP256
  Public key type:  ECCP256
  Subject DN:       CN=age identity ba46c6ba,OU=0.5.0,O=age-plugin-yubikey
  Issuer DN:        CN=age identity ba46c6ba,OU=0.5.0,O=age-plugin-yubikey
  Serial:           b0:38:a5:3e:c9:21:6d:5f:f0:bd:c8:7e:f3:90:42:8c:20:9a:35:c3
  Fingerprint:      90fe04ee14d13ab94c68e23a79f508733f926dd39f0dfce3f85adc213a519f1e
  Not before:       2026-01-20T23:29:09+00:00
  Not after:        9999-12-31T23:59:59+00:00
```

... and gets recognized by `age-plugin-yubikey` itself:
```
$ age-plugin-yubikey --list
#       Serial: 12345678, Slot: 20
#         Name: age identity ba46c6ba
#      Created: Tue, 20 Jan 2026 23:29:09 +0000
#   PIN policy: Once   (A PIN is required once per session, if set)
# Touch policy: Always (A physical touch is required for every decryption)
age1yubikey1qv0s8amuj04jhdjz0nkadeurekd5xyqrhxt9xksd09d0jca424sau5pvfum
```

Just FYI, it won't overwrite populated slots unless `--force` is used:
```
$ yk-piv-age-keygen -s 20
INFO: Found YubiKey with serial '12345678'.
ERROR: PIV slot 95 (RETIRED20) already contains a key and certificate, use --force to replace it.
```
