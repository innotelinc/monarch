# 🦋 Monarch — Platform Stack Role

**Classification: MediaOps**

Streaming and media discovery: Jellyfin libraries, user profiles, watch history, recommendations, collections, and live TV.

This page declares Monarch's role in the
[**Innotel Platform Stack**](https://github.com/innotelinc/innotel-platform-stack) —
the canonical single-responsibility architecture. The stack is defined in exactly one
place; this page links each product to it and states what this platform owns, consumes,
provides, and explicitly does not own.

## Owns

- Streaming
- Media libraries
- User profiles
- Watch history
- Recommendations
- Collections
- Live TV
- Playback
- Media discovery

## Provides

- Media platform for the ecosystem

## Consumes

- Authentik — identity, SSO, paid_users access
- Cerulean Vault — secrets, API keys
- ONYX — media storage
- Magnate — subscriptions and entitlements
- Cerulean — certificates and trust
- NPM Edge — public routing, TLS termination at the edge

## Explicitly does NOT own

- Storage (ONYX)
- Billing (Magnate)
- Identity (Authentik)


> **Current state:** ONYX media storage integration is on the roadmap; Monarch currently uses local /data volumes.
>
> **Alignment scorecard:** [docs/mission-alignment.md](mission-alignment.md) maps every mission
> capability (streaming, live TV, profiles, recommendations, analytics, tiers) to what this
> repo actually ships, what is delegated to Authentik/Cerulean Vault/ONYX/Magnate, and the gaps in
> priority order — with the commands that verify each claim.

## Secrets (Cerulean Vault)

The platform's SecretOps is **Cerulean Vault** — HashiCorp Vault, KV v2, hosted by
Cerulean — with `vault://<mount>/<path>#<key>` references in `.env`.

Cerulean mints this stack's **path-scoped** token (its policy covers only
`cerulean/data/monarch`, never a sibling's secrets) and renews it in place. Copy
it to `./data/vault/token/monarch.token`, then move any plaintext values across:

```bash
VAULT_ADDR=http://<cerulean-host>:8200 \
  VAULT_TOKEN_FILE=./data/vault/token/monarch.token \
  VAULT_PREFIX=cerulean VAULT_PATH=monarch \
  python3 scripts/vault-migrate.py --from-env-file .env \
    --keys MONARCH_PASSWORD,HOMARR_SECRET_ENCRYPTION_KEY,NPM_PASSWORD
```

`vault-migrate.py` never prints a value, unions with whatever is already at the
path (so a re-run is a no-op, not an overwrite), and accepts either `.env` or a
legacy Infisical workspace as its source. `--dry-run` lists the keys that are not
in the store yet, which is the drift view for this stack:

```bash
python3 scripts/vault-migrate.py --from-env-file .env --dry-run  # what is not in the store
bash scripts/drift-check.sh                                      # the running stack, read-only
```

This stack has no runtime resolver, so `.env` must hold the **resolved value** — a
`vault://` reference left in place reaches the container as a literal string.
`drift-check` fails on any it finds, which is what keeps a rotated secret from
living only in one of the two places.

## Golden rules

- **Authentik = Identity** · **Cerulean Vault = Secrets** · **Cerulean = Trust** ·
  **ONYX = Storage** · **Magnate = Revenue** · **NPM Edge = Edge** — everything else is a business function.
- No platform duplicates another's responsibility.
- No credit in commits, footers, or headers to anyone but the project owner.

---

*Monarch · MediaOps · [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack)*
