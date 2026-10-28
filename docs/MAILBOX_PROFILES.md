# Mailbox delivery profiles

`MAILBOX_PROFILES_JSON` is optional. Without a profile, the default part size is **20 MB for non-Nauta recipients** and **10 MB for `@nauta.cu`**. The 20 MB preference reduces the number of parts; it does not guarantee compatibility with every server, and providers with lower limits can reject a message. A permanent response that explicitly indicates an oversized message permits a smaller retry **only if no part has been delivered yet**. Following partial delivery, the bot pauses rather than automatically splitting the file again and risking confusing duplicates. The built-in Nauta 10 MB ceiling allows margin below the official 25 MB message limit for MIME encoding.

Verified profiles match an exact email address. Storage quota is not inferred from a domain: mailbox capacity and per-message size are different limits, and the bot cannot see an account's remaining free space. Only operational limits and their provenance are stored, never credentials. An entry can look like this once its actual values have been verified:

```json
{
  "exact-recipient@example.org": {
    "verified": true,
    "safe_part_mb": 4,
    "min_delay_seconds": 45,
    "max_batch_parts": 3,
    "daily_cap_mb": 80,
    "source": "provider documentation or isolated test",
    "checked_at": "2026-10-27"
  }
}
```

**These numbers are illustrative, not recommendations for any provider.** Do not copy them without verification. Supported ranges: `safe_part_mb` 1–20; `min_delay_seconds` 0–86400; `max_batch_parts` 1–200; `daily_cap_mb` 1–100000. Omit fields that are not known. Daily and batch limits are per recipient; a sender-account quota is global and must be modeled separately.

For a profile to apply, `verified` must be `true`, and both `source` and `checked_at` are required. A profile may only lower the user's part size, increase the minimum delay, or tighten batch and daily ceilings. Each job captures its effective settings when created. Current verified limits are rechecked before delivery: a new or tighter profile can pause an existing job or increase its delay, but a profile change does not silently relax already queued work.

Unverified profiles do not change delivery. Malformed JSON, unknown fields, out-of-range numbers, domain-only keys, and verified profiles without provenance are rejected rather than silently treated as safe.

## Nauta and other destinations

- ETECSA publishes a 100 MB mailbox quota for `@nauta.cu` and a maximum of **25 MB per complete message**, including text and attachments. The 10 MB part ceiling leaves room for email encoding.
- Mailbox storage is not the mobile data allowance. A specific Plan toDus announcement described 1 GB for 25 CUP and 35 days for Nauta/toDus, while a general plan page still showed 600 MB. Keep these concepts separate and confirm current balances and validity with the provider.
- An indexed Disroot FAQ result listed a 1 GB mailbox and 64 MB attachment limit, but the FAQ link returned 404 when consulted; no specific profile was installed on that basis.
- `nine.testrun.org` is a chatmail relay. Its public page discusses rate and retention limits, not a confirmed 300 MB per-account storage quota. Do not equate it with `nine.org` or assume that figure.

Sources: [ETECSA: Nauta limits](https://www.etecsa.cu/en/node/4213), [ETECSA: Plan toDus update](https://www.etecsa.cu/es/noticias/actualizacion-plan-todus-ahora-se-obtiene-1-gb-por-25-cup), [ETECSA: general plans](https://www.etecsa.cu/en/movil/planes-personales), [Disroot FAQ](https://disroot.org/faq), [nine.testrun.org](https://nine.testrun.org/info.html).

The defaults remain configurable, and additional verified profiles require explicit configuration. Lower automatic retries depend on an explicit oversized-delivery response and the no-prior-delivery condition described above.
