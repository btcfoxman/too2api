# Toonkit CDP protocol record

Captured on 2026-10-20/21 using the user's already logged-in `国g022yon.bat` browser (CDP port 9226, account SOCKS5 proxy port 20022). The local `research/captures/*.jsonl` records endpoint paths, method, status, and redacted request/response shapes. It omits prompts, lyrics, binary media, cookies, bearer tokens, CDN tokens, and signed URLs. Raw captures are excluded from Git.

## Auth and account

- `GET /api/auth/session` returns `accessToken` and `ncpToken`. Toonkit API requests use `Authorization: Bearer <accessToken>` from the same logged-in Chrome profile.
- `GET /api/v1/users/me` returns `credit.totalBalance`, monthly and purchased credit balances.
- Browser login used Google and Cloudflare Turnstile. The profile must remain available for account checks, submission, and polling.
- `/api/signup-credit/claim` was observed in signup flow. It has not been established as a daily claim endpoint, so scheduled claiming remains off.

## Reference media

1. `POST /api/v1/uploads` with `intent=QUICKSTART_REFERENCE`, `fileName`, `contentType`, `size`, `animated=false` returns `sessionId` and `presignedUrl`.
2. `PUT` file bytes to the presigned object-storage URL.
3. `POST /api/v1/uploads/confirm` with `sessionId`, plus `durationSeconds` for audio/video, returns the media `id` used in generation arrays.

## Video

- `POST /api/v1/generation/credit-quote` with `kind=VIDEO`, `video.surface=PLAYGROUND`, model, mode, resolution, duration, aspect ratio, audio flag, and reference ID arrays.
- `POST /api/v1/quickstart/batch/videos` (HTTP 202) uses `modelId`, `mode`, `prompt`, `resolution`, `aspectRatio`, `durationSeconds`, `shareToCommunity`, `sound`, `referenceImageIds`, `referenceVideoIds`, `referenceAudioIds`, `referenceVideoTotalSeconds`, `count`, `clientRequestId`.
- The response contains `generationId`, `itemIds`, and `creditCost`. Poll `GET /api/v1/quickstart/media/{itemId}/status`. A completed status may omit `outputVideoUrl`; `GET /api/v1/quickstart/media?limit=100` contains the URL.
- Captured 2.5 reference task: 480p, 4 seconds, 4:3, 5 images, 1 video, 2 audio clips, 95 credits. Captured 2.0 Mini reference task: 720p, 4 seconds, 21:9, 5 images, 2 audio clips, 60 credits.
- Live capability metadata confirms 2.0 reference mode limits 9 images/3 videos/3 audios, 2.5 limits 30/10/10, Wan 3.0 limits 10/5/5. H3 Max currently accepts 9 images, zero videos, zero audios. 2.0 Mini/Fast have no native 480p; H3 Max has no native 768p. The service converts these aliases from 720p after generation.

## Music and results

- `POST /api/v1/quickstart/music` (HTTP 202) supports `SUNO_V6`, `SUNO_V6_MINI`, and `SUNO_V6_WILD`; `CUSTOM` and `DEFAULT` modes. Custom requests include title, lyrics, and style; simple requests include prompt and instrumental flag. One V6 request was quoted at 15 credits with two audio outputs.
- `GET /api/v1/quickstart/media/{itemId}/status` reports task state. The media listing includes completed `outputAudioUrl` / `outputVideoUrl` values.
- Private CDN URLs need the browser session's `ncpToken` as a `token` query parameter, as the web frontend's `addAuthToken` helper does. Old media tested on the following day returned CDN HTTP 403 even with the current token; fresh-task delivery must be verified separately.
- `lyria-3.5` was absent from both `generation/capabilities` and `quickstart/music/capabilities` for this account. The service rejects this alias clearly rather than using a different music model.
