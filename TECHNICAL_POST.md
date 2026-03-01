# Two agents, one robot, zero hierarchy

## The problem

Most AI agent setups have one LLM running the show. That works until you have two agents that are good at completely different things. We had Reachy Mini, a physical robot that talks to people face-to-face, and OpenClaw, a personal AI assistant that runs locally and connects to whatever messaging channels you use. Making one subordinate to the other felt wrong. They should work side by side.

We built this during a hackathon, connecting [Reachy Mini](https://www.pollen-robotics.com/reachy-mini/) (voice conversation robot) to [OpenClaw](https://github.com/openclaw/openclaw) (a local-first personal AI assistant with a multi-channel gateway supporting Discord, Telegram, WhatsApp, Slack, and others). For this project we used OpenClaw's Discord channel as the text interface. Two independent AI agents that share what they know and talk to each other.

## Architecture

```
┌──────────────────────────┐         ┌──────────────────────────┐
│     REACHY MINI          │         │       OPENCLAW           │
│                          │         │                          │
│  OpenAI Realtime API     │◄──────► │  Personal AI Assistant    │
│  (gpt-realtime-1.5)     │  Bridge │  (configurable model)   │
│                          │   API   │                          │
│  Voice in/out            │         │  Multi-channel gateway   │
│  Robot head movement     │         │  Browser tools           │
│  Camera tool             │         │  Link research           │
│  Dance/emotion/movement  │         │  Discord / Telegram / +  │
└──────────────────────────┘         └──────────────────────────┘
         ▲                                      ▲
         │                                      │
    User speaks                           User types on
    face-to-face                            Discord
```

Each agent has its own LLM session. Reachy runs OpenAI's Realtime API (`gpt-realtime-1.5`) for low-latency voice. OpenClaw runs a configurable text model with tool use for research. They don't share a context window. They share *summaries* through a bridge.

## The bridge

A lightweight API running alongside Reachy's conversation app on port 8100:

| Endpoint | Direction | Purpose |
|---|---|---|
| `POST /bridge/inject` | OpenClaw → Reachy | Push text into Reachy's voice session |
| `WS /bridge/ws` | Reachy → OpenClaw | Stream voice transcripts in real-time |
| `GET /bridge/status` | Both | Health check |

The interesting part is the `inject` endpoint. It accepts `text`, `image_b64`, and `response_instructions`, which lets you push content into Reachy's session and tell its LLM *how* to respond. OpenClaw can say "summarize these research findings in 2-3 sentences" or "briefly acknowledge this notification." That framing control matters more than it sounds.

## Three plugin hooks

On the OpenClaw side, a plugin (`extensions/reachy-mini/`) connects the two agents through three hooks:

### 1. `before_prompt_build` — voice context injection

Every time OpenClaw's agent runs (triggered by a Discord message), this hook prepends the last 50 voice transcript lines from Reachy's conversation. OpenClaw sees what the user has been *saying* to the robot, not just what they typed.

```
[Recent voice conversation between user and Reachy Mini robot]
User (voice): I'm sending you a link about quantum computing
Reachy: Got it, I'll keep an eye out for it
[End voice context]
```

So when a user drops a link in Discord, OpenClaw already knows *why*, because they just told Reachy about it out loud.

### 2. `message_received` — real-time notification

When a Discord message arrives, Reachy gets a heads-up via bridge inject:

> "tyson sent a message on Discord. OpenClaw is processing it."

The robot acknowledges it verbally: *"Looks like something just came in on Discord. OpenClaw's on it."* The user gets feedback before the research has even started.

Content is trimmed to ~1,500 tokens to avoid blowing out Reachy's context window.

### 3. `llm_output` — research forwarding

After OpenClaw finishes processing, its full response goes to Reachy (trimmed to ~10K tokens). Reachy then discusses the findings with the user.

Why `llm_output` instead of `message_sending`? We found out the hard way that Discord same-channel replies bypass the outbound delivery pipeline entirely (`deliverDiscordReply` → `sendMessageDiscord` directly). The `message_sending` hook never fires. `llm_output` fires after every LLM response regardless of delivery path. Subtle, but it cost us real debugging time.

## Full data flow

```
User speaks to Reachy: "I'm sending you a link about quantum computing"
    │
    ▼
Reachy transcript ──► WebSocket ──► reachy-mini extension
    ├─ Stored in transcriptBuffer (sliding window of 50 entries)
    └─ Forwarded to Discord #reachy-log as [You]/[Reachy] messages

User sends link in Discord #general
    │
    ▼
OpenClaw receives message via Discord
    │
    ├─► message_received hook → Reachy gets notification
    │
    ├─► before_prompt_build hook → Voice transcripts prepended to prompt
    │
    ├─► Agent processes message WITH voice context
    │
    ├─► llm_output hook → Response forwarded to Reachy
    │
    └─► Reply delivered to Discord

Reachy discusses findings verbally with user
```

## Loop prevention

Two agents talking to each other will happily loop forever if you let them. Two guards stop this:

1. Prefix filtering: `message_received` skips messages starting with `[You]` or `[Reachy]` (transcript echoes from the log channel). Stops transcript logs from triggering new notifications.
2. Channel separation: transcript logs go to `#reachy-log`, not `#general`. The agents don't see each other's observability output.

One thing we haven't solved: `llm_output` fires for all LLM completions regardless of trigger. Adding origin tracking would prevent the hook from forwarding responses that were themselves triggered by Reachy content. Works for now.

## Observability

A dedicated Discord channel (`#reachy-log`) shows what's happening in real time:

```
[You] I'm sending you a link about quantum computing
[Reachy] Got it, I'll keep an eye out for it
[→ Reachy] Notified: tyson sent a message (142 chars)
[→ Reachy] Forwarded OpenClaw response (2847 chars)
[Reachy] OpenClaw found some really interesting stuff about quantum error correction...
```

Every bridge interaction is logged. Failures show up as `[→ Reachy] ERROR ...` messages.

## What surprised us

The `message_sending` hook doesn't fire for Discord replies. This took a while to figure out. Discord's same-channel delivery path shortcuts the entire outbound payload pipeline. We had debug logging confirming the hook was registered, but it never triggered. Switching to `llm_output` fixed it because it fires at the LLM layer, before any channel-specific delivery logic.

macOS microphone permissions block USB audio devices silently. Reachy Mini has its own USB audio device ("Reachy Mini Audio"), and the SDK correctly selects it over the system default. But macOS still requires explicit microphone permission for the terminal process. Both the built-in mic and the USB device read zero until you grant it. We spent an embarrassing amount of time checking audio device selection code when the problem was a permissions dialog we hadn't seen.

Token budgets need active management. Without trimming, a long OpenClaw research response overflows Reachy's Realtime API context window. We cap incoming notifications at ~1,500 tokens (6,000 chars) and responses at ~10K tokens (40,000 chars).

## Bidirectional tasks

The bridge works both ways. Reachy can ask OpenClaw to do things too.

A user says to Reachy: *"Can you ask OpenClaw to look up the latest papers on transformer architectures?"*

Reachy's LLM calls the `ask_openclaw` tool, which sends the task through the bridge. OpenClaw picks it up, runs its agent with browser/research tools, and the response flows back through the existing `llm_output` → bridge inject path.

On Reachy's side, a Python tool:

```python
class AskOpenClaw(Tool):
    name = "ask_openclaw"
    description = "Send a task to OpenClaw, your AI partner on Discord."

    async def __call__(self, deps, **kwargs):
        task = kwargs["task"]
        await deps.bridge_state.broadcast({
            "type": "task",
            "task": task,
        })
        return {"status": "sent", "task": task}
```

The tool broadcasts a `type: "task"` message over the existing bridge WebSocket. No new infrastructure.

On OpenClaw's side, the `TranscriptService` already listens to this WebSocket. It handles `type: "task"` messages by posting them to Discord via webhook. The webhook posts as a different user ("Reachy Mini"), which matters because Discord bots ignore their own messages. The message enters the normal processing pipeline, gets handled, and the response flows back to Reachy through the existing `llm_output` hook.

One subtlety: webhook messages still have `author.bot = true` on the Discord side. OpenClaw's message handler drops bot messages by default. We enable `allowBots` in the Discord channel config so the webhook-posted tasks get processed.

```
Reachy LLM → ask_openclaw tool → bridge WebSocket → TranscriptService
    → Discord webhook → OpenClaw agent processes → llm_output hook
    → bridge inject → Reachy speaks result
```

Either agent can start a conversation, and the user talks to whichever one is convenient. The bridge is agent-agnostic. Anything that speaks HTTP and WebSocket can plug in, so a third agent (mobile app, Slack bot, another robot) would just be another client.

## Running it

```bash
# Terminal 1 — OpenClaw gateway + Discord bot
pnpm openclaw gateway run --force

# Terminal 2 — Reachy conversation app
cd reachy_mini_conversation_app
source .venv/bin/activate
reachy-mini-conversation-app --no-camera
```

Check for `Transcript WebSocket connected` in OpenClaw logs and `Bridge WS client connected` in Reachy logs.

## Stack

| Component | Technology |
|---|---|
| Reachy Mini voice | OpenAI Realtime API (`gpt-realtime-1.5`) |
| Reachy audio | SoundDevice + USB "Reachy Mini Audio" |
| Reachy bridge | FastAPI on port 8100 |
| OpenClaw agent | Local-first personal AI assistant, configurable LLM with tool use |
| OpenClaw channels | Discord for this project (also supports Telegram, WhatsApp, Slack, Signal, iMessage, etc.) |
| Plugin bridge | TypeScript extension with WebSocket + HTTP |
| Robot control | Zenoh pub/sub via reachy-mini SDK |

## Takeaway

You don't need one agent that does everything. Two specialized agents with a simple bridge covered more ground than we expected. The agents don't understand each other's internals at all. They just pass transcripts, inject messages, and delegate tasks. That turned out to be enough.

---

*When we say "we" in this post, we mean me (Tyson) and Claude Opus. The code, architecture, debugging, and this writeup were all pair-programmed with Claude.*
