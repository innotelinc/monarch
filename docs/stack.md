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
- Infisical — secrets, API keys
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
> repo actually ships, what is delegated to Authentik/Infisical/ONYX/Magnate, and the gaps in
> priority order — with the commands that verify each claim.

## Secrets (Infisical)

Secrets for this platform live in **Infisical** (SecretOps): credentials are imported
into an Infisical workspace and the stack's `.env` is **derived** from it — one
direction only. `INFISICAL_*` is the bootstrap set that has to stay in `.env`
(the address, workspace id, environment and service token that get you in);
everything else is rendered from the store, never hand-edited:

```bash
# generate the required keys and add them to .env
openssl rand -base64 32   # INFISICAL_ENCRYPTION_KEY
openssl rand -hex 16      # INFISICAL_AUTH_SECRET
openssl rand -hex 16      # INFISICAL_DB_PASSWORD

# start the profile and provision the workspace + import .env secrets
docker compose -f docker-compose.yml -f compose.infisical.yml --profile infisical up -d
bash scripts/infisical-setup.sh
```

```bash
python3 scripts/infisical-setup.py --check    # is .env derived from the store?
python3 scripts/infisical-setup.py --render   # pull every secret into .env
```

`--check` never writes and reports key names only (never values); it exits 1 on
drift, `monarch-drift-check` runs it, and `--render` is the fix. This is what
makes a rotated secret land on the host instead of living only in one of the
two places.

See [compose.infisical.yml](../compose.infisical.yml) and
[scripts/infisical-setup.py](../scripts/infisical-setup.py) for details.

## Golden rules

- **Authentik = Identity** · **Infisical = Secrets** · **Cerulean = Trust** ·
  **ONYX = Storage** · **Magnate = Revenue** · **NPM Edge = Edge** — everything else is a business function.
- No platform duplicates another's responsibility.
- No credit in commits, footers, or headers to anyone but the project owner.

---

*Monarch · MediaOps · [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack)*
