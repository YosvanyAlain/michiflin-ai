# AI providers and known limits

Free-tier quotas can change or depend on the plan and project. Michiflin treats them as approximate load-sharing limits, not a promise of availability or unlimited use. Rate limits, rejected keys, and retired models cause a pause and, where appropriate, fallback to another provider.

## SambaNova

The optional `sambanova` provider uses `SAMBANOVA_API_KEY` and its OpenAI-compatible endpoint at `https://api.sambanova.ai/v1`. The catalog includes the production models `DeepSeek-V3.1`, `Meta-Llama-3.3-70B-Instruct`, and `gpt-oss-120b`. It records a conservative internal ceiling of **20 requests per day per model** for the free tier. SambaNova also documents token-per-day and per-minute limits that can be reached sooner. A `429` response pauses the model and permits fallback. No key is configured by default.

Preview models are not included: SambaNova describes them as intended for evaluation and subject to removal with little notice.

## Hosted provider routing

The bot uses configured OpenAI-compatible provider APIs and optional free aggregators such as `openrouter/free`, Kilo, and LLM7. Some gateways accept requests without a key; others require the administrator to configure one through environment variables. Availability, limits, and retention policies depend on the external service and can change. OpenRouter does not fall back to paid models in the bundled catalog.

Messages, attachments, and code submitted to a model leave the Michiflin server and are received by the provider handling the request. Do not include passwords, private keys, or information you do not want sent to that provider. Michiflin's internal quotas do not replace provider limits or guarantee a permanently free service.

## Custom endpoints

`EXTRA_PROVIDERS` accepts OpenAI-compatible providers only when the exact HTTPS host in `base_url` is listed in `AI_EXTRA_ALLOWED_HOSTS`. IP literals, embedded credentials, nonstandard ports, query strings, and fragments are rejected. Redirects are disabled for these endpoints to avoid forwarding credentials. This reduces the risk of accidental key disclosure, but does not make an allowlisted provider trustworthy.

## Evaluated services

- **Arena.ai:** no integration or website login automation was added. Its terms restrict automated or programmatic access unless an applicable authorization or license permits it. Inputs may be shared with third-party model providers. See the [API](https://platform.arena.ai/docs) and [terms](https://help.arena.ai/articles/5629909088-terms-of-use).
- **DeepSeek:** its official API charges for tokens and successful Search/Fetch requests, so it is excluded from the free route. Third-party aggregators may offer DeepSeek models through their own free routes and conditions. See [pricing](https://api-docs.deepseek.com/quick_start/pricing/) and [Search/Fetch](https://api-docs.deepseek.com/guides/search/).
- **Z.AI:** only Flash variants identified as free in its [pricing table](https://docs.z.ai/guides/overview/pricing) are included. The table does not establish a universal daily allowance for these variants; Michiflin's limits are internal controls, not provider guarantees.
- **OpenRouter:** `openrouter/free` routes to free models. No paid fallback is configured; the provider warns that paid fallbacks can incur charges. Availability and limits can change; see [limits](https://openrouter.ai/docs/api_reference/limits) and the [free router](https://openrouter.ai/docs/cookbook/get-started/free-models-router-playground).
- **Cerebras:** the documented USD 5 trial requires a verified payment method and expires after 30 days; it is not treated as recurring free capacity.
- **Hugging Face Inference Providers:** the documentation consulted listed zero monthly credits for Free accounts, with credits for paid plans; it is not presented as recurring Free-account capacity.

## Sources consulted on October 27, 2026

- [SambaNova: rate limits](https://docs.sambanova.ai/docs/en/models/rate-limits)
- [SambaNova: SambaCloud models](https://docs.sambanova.ai/docs/en/models/sambacloud-models)
- [SambaNova: API keys and endpoint](https://docs.sambanova.ai/docs/en/get-started/api-keys-urls)
- [Cerebras: limits and trial](https://inference-docs.cerebras.ai/support/rate-limits)
- [Hugging Face: Inference Providers pricing and credits](https://huggingface.co/docs/inference-providers/pricing)

These are references, not immutable limits. Check provider documentation again before changing the catalog.
