"""Pure Discord defaults; consumed by DEFAULT_CONFIG without adapter imports."""

DISCORD_DEFAULTS = {
    "require_mention": True,  # require @mention to respond in server channels
    "free_response_channels": "",  # comma-separated channel IDs answered without mention
    "allowed_channels": "",  # if set, ONLY respond in these channel IDs (whitelist)
    "auto_thread": True,  # auto-create threads on @mention in channels (like Slack)
    # Free-response channels reply inline by default; true also gives each top-level
    # message in them its own thread (still mention-free). Env: DISCORD_FREE_RESPONSE_AUTO_THREAD.
    "free_response_auto_thread": False,
    "busy_composition_window_seconds": 0,
    "thread_require_mention": False,  # require @mention in threads too (multi-bot threads)
    # Bot authors must type @thisbot to trigger a reply; Discord reply pings alone do not count.
    # Set False only for trusted legacy relays. Humans are unaffected.
    "bots_require_inline_mention": True,
    # Prepend recent channel scrollback when triggered (recovers messages gated out by
    # require_mention); limit = max messages scanned.
    "history_backfill": True,
    "history_backfill_limit": 50,
    # Replay messages missed while offline, after reconnect/startup.
    "missed_message_backfill": {
        "enabled": False,
        "channels": "",  # comma-separated channel IDs; empty uses free_response_channels
        "window_seconds": 21600,  # only inspect messages from the last 6 hours
        "limit": 100,  # global cap on messages scanned per reconnect
        "max_dispatches": 10,  # cap on recovered messages dispatched per reconnect
        "max_attempts": 3,  # lifetime re-dispatch cap for one message, whatever its outcome
    },
    "reactions": True,  # add 👀/✅/❌ reactions to messages during processing
    # Gateway transport health probe: inspects the WebSocket's ready/open/heartbeat state (never
    # REST) as proof events still arrive. Any value 0 disables it.
    "websocket_liveness_interval_seconds": 15,
    "websocket_liveness_failure_threshold": 2,
    "websocket_heartbeat_ack_max_age_seconds": 60,
    "websocket_max_latency_seconds": 30,
    # Dispatch-side dimension: a socket that ACKs heartbeats but delivers no events for this
    # long is treated as deaf. 4 h absorbs a quiet server overnight; 0 disables it.
    "websocket_event_max_silence_seconds": 14400,
    # per-channel ephemeral system prompts (forum parents apply to child threads)
    "channel_prompts": {},
    # Opt-in DM role auth: DISCORD_ALLOWED_ROLES normally authorizes guild messages only (DMs
    # need DISCORD_ALLOWED_USERS). A guild ID here also authorizes DMs from that guild's members
    # holding the allowed role. Unset / "" / 0 = off.
    # See #12136.
    "dm_role_auth_guild": "",
    # discord / discord_admin tools: allowed actions (comma string or YAML list; empty = all,
    # subject to bot intents; unknown names dropped with a warning): list_guilds, server_info,
    # list_channels, channel_info, list_roles, member_info, search_members, fetch_messages,
    # list_pins, pin_message, unpin_message, create_thread, add_role, remove_role.
    "server_actions": "",
    # DEPRECATED no-op (uploads are always cached; messaging auth is the gate). Kept so existing
    # configs don't error. Env: DISCORD_ALLOW_ANY_ATTACHMENT.
    "allow_any_attachment": False,
    # Max bytes per cached attachment (held in memory while written); 0 = no cap. Env:
    # DISCORD_MAX_ATTACHMENT_BYTES.
    "max_attachment_bytes": 33554432,
    # Mention allowed users on approval prompts so owners notice them in shared channels. Env:
    # DISCORD_APPROVAL_MENTIONS.
    "approval_mentions": False,
    # Voice-channel inactivity timeout (seconds); 0 = stay until `/voice leave`.
    "voice_channel_inactivity_timeout_seconds": 300,
    # Minimum seconds before force-stopping a VC playback; the adapter probes clip duration and
    # extends this floor so long TTS isn't cut off.
    "voice_playback_timeout_seconds": 120,
    # Voice-channel software mixer (plugins/platforms/discord/voice_mixer.py): ambient
    # "thinking" bed, verbal acks and TTS OVERLAP (ambient ducked) vs stop-and-swap.
    "voice_fx": {
        "enabled": False,  # master switch for the mixer subsystem
        "ambient_enabled": True,  # play the idle "thinking" bed while tools run
        "ambient_path": "",  # custom loop audio file; "" = synthesised pad
        "ambient_gain": 0.18,  # idle bed loudness, 0.0–1.0
        "duck_gain": 0.06,  # ambient loudness while speech plays
        "speech_gain": 1.0,  # TTS / ack loudness, 0.0–1.0
        "ack_enabled": True,  # speak a short phrase before the first tool call
        "ack_phrases": [  # picked at random; [] disables phrases
            "Let me look into that.",
            "One moment.",
            "Checking on that now.",
            "Give me a sec.",
            "On it.",
        ],
    },
}
