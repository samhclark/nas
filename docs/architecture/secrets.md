# Runtime secrets architecture

## Decision

SOPS-encrypted YAML in the image is the persistent source of truth. A single
root-owned boot service decrypts it and writes only the files each service is
allowed to read under `/run/nas-secrets/`. Rootless containers mount those
ephemeral files read-only.

Do not introduce rootless Podman `Secret=` objects or give service users an
age private key. This boundary is deliberate, not unfinished migration work.

## Persistent and runtime boundaries

The repository contains only ciphertext at:

```text
/usr/share/nas/secrets/secrets.sops.yaml
```

The NAS operator installs the age private key as a `systemd-creds` credential:

```text
/var/lib/nas-secrets/age-key.cred
```

At boot, `sops-distribute-secrets.service` recreates this tmpfs hierarchy:

```text
/run/nas-secrets/                         0711 root:root
/run/nas-secrets/<service>/               0710 root:<service-user>
/run/nas-secrets/<service>/<secret-name>  0400 <service-user>:<service-user>
```

The generated Quadlet contract is:

```ini
Volume=/run/nas-secrets/<service>/<secret-name>:/run/secrets/<secret-name>:ro,Z
ExecStartPre=/usr/bin/test -r /run/nas-secrets/<service>/<secret-name>
```

The compiler derives the service/secret mapping from `quadlets/*.toml` and
verifies that every declared name exists as an encrypted top-level key.

## Mullvad WireGuard key

The authored media-automation design declares the encrypted top-level SOPS key
`mullvad-private-key`. It is distributed to the root-owned runtime path
`/run/nas-secrets/mullvad/mullvad-private-key` with the same protected file
boundary as other runtime secrets. A generated systemd-networkd drop-in loads
that path as the credential named
`network.wireguard.private.70-wg-arr`; the generated `70-wg-arr.netdev` refers
to the credential rather than containing private key material.

This is a host-network secret, not a container secret. The Arr containers do
not receive the Mullvad private key and cannot configure `wg-arr`. Their
selected egress is authorized only after the host WireGuard interface and its
generated nftables policy are ready. The media-automation services are deployed
and operational. This secret contract does not itself claim that networkd
successfully loaded the credential, that Mullvad completed a handshake, or that
a guest's public traffic reached the provider; those remain separate egress
checks.

## Why runtime files instead of rootless Podman secrets

NAS testing established that user-scoped `systemd-creds` can work in a normal
service-user shell, but the same meaningful key modes are unavailable from a
rootless Podman helper user namespace:

- host-backed modes cannot read the host credential secret;
- TPM-backed modes cannot access `/dev/tpmrm0`;
- the unprotected `null` mode works but provides no useful secret protection.

An age-backed Podman shell driver only moves the problem: its private key must
still be readable from that helper context. Giving every service user a
general decryption key would also grant more authority than the specific
runtime files it needs.

The root distributor therefore performs the privileged decryption once and
hands each service a least-privilege file capability.

## SELinux evidence

The runtime file must be mounted with `:ro,Z`. Production validation showed
that rootless Podman can relabel a `/run` tmpfs file for its container. Without
that relabel, `var_run_t` is correctly denied to `container_t`.

Each service receives its own file copy, so private relabeling cannot steal a
shared file's MCS label from another container.

## Failure behavior

- Plaintext is never written to persistent storage.
- The distributor writes temporary files and renames them into place only
  after successful decryption and validation.
- A missing runtime file fails the Quadlet readability guard.
- Rootless services retry normally after the distributor becomes ready; they
  do not depend directly on a unit in the system manager.
- Adding or removing a declared secret requires regenerating the fleet and
  updating the encrypted SOPS document together.
