# TOO2API

Toonkit web session gateway for asynchronous video and music tasks. The service uses each account's logged-in Chromium profile for authenticated Toonkit requests. Its admin UI follows the pip2api account and task workflow, including online account creation, noVNC login, account checks, queue controls, priority, and concurrency.

## Public API

Use `Authorization: Bearer $TOO_API_KEY`.

- `POST /v1/videos` and `GET /v1/videos/{id}`
- `POST /v1/music` and `GET /v1/music/{id}`
- `GET /v1/models`

Video request example:

```json
{"model":"sd-2-5-480p","prompt":"Use @图片1 and @音频1 to make one video","duration":4,"aspect_ratio":"16:9","image_urls":["https://example.com/reference.png"],"audio_urls":["https://example.com/music.mp3"],"background":true}
```

Music custom mode:

```json
{"model":"suno-v6","custom_mode":true,"title":"Summer Sea Breeze","prompt":"Energetic retro pop","tags":"electro pop","lyrics":"[Verse]\nSea breeze moves across the sand"}
```

Simple mode accepts `model=suno-v6`, `prompt`, and `make_instrumental`. The current Toonkit capability response lists Suno V6, V6 Mini, and V6 Wild; it does not list Lyria 3.5. A request for `lyria-3.5` receives HTTP 422 instead of being silently routed to a different model.

`content` arrays with `text`, `image_url`, `video_url`, and `audio_url` entries are also accepted. Chinese and English numbered references are converted to Toonkit's `@图片N`, `@视频N`, and `@音频N` syntax.

## Deployment

The pre-production workflow builds and publishes `ghcr.io/btcfoxman/too2api` on the `too2api-pre` self-hosted runner, then deploys `/home/btcfoxman/docker/too2api` and configures `too2api.aiid.edu.kg` through cloudflared. Default LAN port is `8812`. Create a private `.env` from `.env.example` on the server before the first deployment. Never commit `.env`, browser profiles, or CDP captures.

In the management page, select **在线添加账号**, enter an account label and its account-level proxy, log in through the embedded browser, then press **完成登录并验证**. The same profile stays online for generation and account checks. The captured browser used SOCKS5 port `20022` on both local and 3.5 pre hosts.

Media downloads try direct access, the account proxy, then the default `10808` proxy. Uploads use the account proxy and retry transient failures. Submission retries after a *definitive* reference validation rejection reupload the assets once under the same public task. Ambiguous submit outcomes are not resent automatically.

Toonkit does not natively offer 480p for Seedance 2.0 Mini/Fast or 768p for H3 Max. These aliases request native 720p and convert the completed video locally with ffmpeg; converted files persist under `/app/data/results` and are served through signed TOO2API links. Current H3 Max capability accepts images but reports zero video and audio reference slots, so those inputs are rejected before submission. The observed `/api/signup-credit/claim` is a signup grant, not a verified daily claim; automatic daily check-in remains disabled.

The redacted CDP protocol record is in [`research/SESSION.md`](research/SESSION.md). Raw captures stay local and are excluded from Git.
