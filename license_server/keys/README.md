# Signing keys

`DEV_ONLY_private.pem` / `DEV_ONLY_public.pem` are the development pair. The
public half is embedded in `app/plannerboard/license/client.py` as
`DEFAULT_PUBLIC_KEY_PEM`. Anyone holding the private half can mint keys that
dev build accepts, so:

- never deploy `DEV_ONLY_private.pem`
- before production: `python scripts/gen_keys.py --out /etc/licserver --name prod`,
  set `LIC_PRIVATE_KEY_PATH`, paste the new public PEM into the app client,
  and follow "Key rotation" in `../README.md`.

## Production key (2026-11-03)

The production pair was generated with `gen_keys.py --name prod`. The PUBLIC
half is `DEFAULT_PUBLIC_KEY_PEM` in `app/plannerboard/license/client.py`.
The PRIVATE half (`prod_private.pem`) is kept outside this repository, on the
owner's machine, and must be copied to the license server host and pointed
to with `LIC_PRIVATE_KEY_PATH` before the server issues real keys. Losing it
means every shipped build must be re-released with a new public key.
