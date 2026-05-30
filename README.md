---
title: Hotel Comms Gateway
emoji: 🛎️
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# Hotel Comms Gateway

Closed-loop communication layer for the **Grand Hotel** agent company on Paperclip.

External channels (Telegram / Email / Slack / Web chat) ↔ Paperclip issues ↔ hotel agents (Qwen3-32B via self-hosted LLM gateway).

## What it does

1. **Inbound** — guest sends a message on a channel.
2. Webhook hits this service. An intent classifier (one tiny `qwen3:8b-fast` call via the self-hosted LLM gateway) decides which department.
3. Service creates / appends a Paperclip issue under `guest-conversations`, assigned to the right agent (Front Office Mgr, Concierge, Reservations Mgr, Chief Engineer, etc.).
4. The hotel agent (Qwen3-32B) reads the issue on its next heartbeat and posts a reply comment.
5. **Outbound** — Paperclip fires an `issue.comment.created` webhook back to this service. The service reads `metadata.channel` / `to` from the issue and dispatches the reply via the matching channel adapter.

Closed loop. The guest sees the reply in the same thread they wrote in.

## Channels

| Channel | Status | Inbound | Outbound |
|---|---|---|---|
| Telegram | shipping | `/webhook/telegram` | Bot API `sendMessage` |
| Email (Brevo) | shipping | `/webhook/brevo` | Brevo transactional API |
| Slack | shipping | `/slack/events` + `/slack/command` | `chat.postMessage` |
| Web chat | shipping | `/webhook/web` | embedded JS widget reads back |
| WhatsApp | phase 2 | — | — |
| SMS (Twilio) | phase 2 | — | — |

## Required env

| Var | Purpose |
|---|---|
| `PAPERCLIP_API_BASE` | Paperclip board API target |
| `PAPERCLIP_API_KEY` *(secret)* | User-level token (board access) |
| `PAPERCLIP_COMPANY_ID` | Grand Hotel company id |
| `PAPERCLIP_GUEST_PROJECT_ID` | guest-conversations project id |
| `PAPERCLIP_STAFF_PROJECT_ID` | staff-channel project id |
| `GATEWAY_BASE_URL` | Self-hosted LLM gateway |
| `GATEWAY_API_KEY_CLASSIFIER` *(secret)* | pk_live_ bound to qwen3:8b-fast |
| `TG_BOT_TOKEN` *(secret)* | BotFather |
| `TG_SECRET` *(secret)* | webhook shared secret |
| `BREVO_API_KEY` *(secret)* | Brevo |
| `BREVO_INBOUND_SECRET` *(secret)* | Brevo inbound HMAC |
| `SLACK_SIGNING_SECRET` *(secret)* | Slack |
| `SLACK_BOT_TOKEN` *(secret)* | Slack |
| `WEB_CHAT_SECRET` *(secret)* | widget tamper check |

Mount a bucket at `/data` for SQLite thread map.

## Install

Deploy as a Docker HF Space. Hardware: **CPU basic (free)** — no inference here.

## License

MIT.
