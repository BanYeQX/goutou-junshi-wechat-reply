# Luofeng-Cloud attribution and extraction boundary

Original project: https://github.com/Luofeng-Cloud/WeChat-AI-AutoReply

Pinned commit: `d39948512b34e797c5763538f59a70af53ca7a97`.
Original copyright: Copyright (c) 2026 Luofeng-Cloud. License: MIT; full text in LICENSE alongside this notice.
Retain both files when redistributing these derived modules, including future packaged candidates.

Reviewed source: `wechat_ai_bot_dev.py`, Git blob `fb8633321cf52b9c2ba911d28397526c8d1f8337`.
Original license Git blob: `9635c838eac16ecc9990d0249c35ac99ac0e93fe`.
These are blob identities, not additional commit SHAs. Source was read at the pinned commit, never imported or launched.

| Original function (1-based source line) | Derived/reused implementation |
| --- | --- |
| compute_wechat_layout (170) | luofeng_primitives.compute_wechat_layout; bounded input validation, estimates only |
| extract_active_chat_title (464) | luofeng_primitives.extract_active_chat_title; candidate assembly only, never authorization |
| parse_chat_bubbles_chromatic (496) | luofeng_primitives.parse_chat_bubbles_chromatic; same integer colour rule and <40px grouping, but approved message ROI coordinates, unknown retention, no fabricated identity |
| grab_wechat_window (224) | Existing original printwindow_candidate.PrintWindowCandidate reused, not duplicated; no flag0/desktop fallback |
| activate_wechat (299) | luofeng_window_ops.WindowOperations.activate_wechat; only bound-window foreground request with explicit revocable action grant, no forced topmost/thread attachment/restore/desktop changes |
| post_click_client_point (340) | luofeng_window_ops.WindowOperations.post_click_client_point; same local-message sequence/LPARAM, per-action guards, checked queueing result, no swallowed failure/retry |
| send_reply_instant input-point calculation | luofeng_primitives.input_point_hint; estimate only, not wired to the proven editor locator |

Adaptations and additions are documented in LUOFENG_CANDIDATE_MIGRATION.md.
The original module initialization, scheduler, OCR initialization/model downloads, API configuration/history, contact fuzzy matching/correction,
green-pixel success criterion, input/keyboard injection, automatic retries, body logging, and stale clipboard restoration are NOT included.

These functions do not establish WeChat compatibility, event identity, editor readability, successful delivery, or account safety.
