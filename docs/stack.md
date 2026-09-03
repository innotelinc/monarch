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

## Explicitly does NOT own

- Storage (ONYX)
- Billing (Magnate)
- Identity (Authentik)


> **Current state:** ONYX media storage integration is on the roadmap; Monarch currently uses local /data volumes.

## Secrets (Infisical)

Secrets for this platform live in **Infisical** (SecretOps): credentials are imported
into an Infisical workspace and the stack's `.env` is derived from it. Enable it with:

```bash
# generate the required keys and add them to .env
openssl rand -base64 32   # INFISICAL_ENCRYPTION_KEY
openssl rand -hex 16      # INFISICAL_AUTH_SECRET
openssl rand -hex 16      # INFISICAL_DB_PASSWORD

# start the profile and provision the workspace + import .env secrets
docker compose -f docker-compose.yml -f compose.infisical.yml --profile infisical up -d
bash scripts/infisical-setup.sh
```

See [compose.infisical.yml](../compose.infisical.yml) and
[scripts/infisical-setup.py](../scripts/infisical-setup.py) for details.

## Golden rules

- **Authentik = Identity** · **Infisical = Secrets** · **Cerulean = Trust** ·
  **ONYX = Storage** · **Magnate = Revenue** — everything else is a business function.
- No platform duplicates another's responsibility.
- No credit in commits, footers, or headers to anyone but the project owner.

---

*Monarch · MediaOps · [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack)*
