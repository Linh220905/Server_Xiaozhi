"""Alarm scheduler: monitor `alarms.json` and trigger alarms.

Scheduler will attempt to deliver alarm audio to any connected clients by
sending TTS/ringtone frames over their WebSocket connections.

Note: This does NOT include any proprietary ringtone files. To use a ringtone,
place a local audio file and pass its path in the `ringtone` field when calling
the `set_alarm` tool (e.g. "/path/to/ringtone.mp3").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any

from app.websocket import handler as ws_handler
from app.websocket.session import get_all_sessions

logger = logging.getLogger(__name__)

# Default ringtone path (generated if missing)
DEFAULT_RINGTONE = os.path.join(os.path.dirname(__file__), "BaoThuc.mp3")


async def _trigger_alarm_for_session(session, ws, alarm: dict[str, Any]):
    """Send alarm message + ringtone to a single session/ws.

    Uses the session.pipeline._tts to stream audio frames and sends JSON
    notifications similar to other TTS flows in the project.
    """
    try:
        # Ensure session state allows playback: clear idle, mark speaking
        try:
            session.is_idling = False
            session.is_speaking = True
            session.aborted = False
        except Exception:
            pass

        # Inform client TTS start + sentence
        # Acquire per-session send lock if available to avoid concurrent send races
        send_lock = getattr(ws_handler, "_session_send_locks", {}).get(session.session_id)
        try:
            if send_lock:
                await send_lock.acquire()
            await ws.send_text(json.dumps({"type": "tts", "state": "start", "session_id": session.session_id}, ensure_ascii=False))
            await ws.send_text(json.dumps({"type": "tts", "state": "sentence_start", "text": alarm.get("message", "Báo thức"), "session_id": session.session_id}, ensure_ascii=False))
        except Exception:
            logger.warning("Failed to send TTS start/sentence to session %s", session.session_id)
        finally:
            if send_lock and send_lock.locked():
                try:
                    send_lock.release()
                except Exception:
                    pass

        # Play ringtone if provided, else TTS speak the message
        ringtone = alarm.get("ringtone") or DEFAULT_RINGTONE
        tts = session.pipeline._tts

        if ringtone:
            # If local path, ffmpeg accepts it. Otherwise treat as URL.
            path = ringtone
            if not os.path.isabs(path):
                # Try relative to this module
                base = os.path.dirname(__file__)
                path = os.path.join(base, path)

            # Play the file at least once (stream_audio_url yields opus frames).
            # If alarm has `play_duration` (seconds) and it's longer than file length,
            # loop the file until total played time >= play_duration.
            play_duration = None
            try:
                pd = alarm.get("play_duration")
                if pd is not None:
                    play_duration = float(pd)
            except Exception:
                play_duration = None

            total_played_s = 0.0
            played_once = False

            # Helper to stream one full pass and return played seconds
            async def _stream_once() -> float:
                frames = 0
                async for frame in tts.stream_audio_url(path):
                        try:
                            if send_lock:
                                await send_lock.acquire()
                            await ws.send_bytes(frame)
                        except Exception:
                            logger.warning("Failed to send audio frame to %s", session.session_id)
                            if send_lock and send_lock.locked():
                                try:
                                    send_lock.release()
                                except Exception:
                                    pass
                            return float(frames) * tts.frame_duration_s
                        finally:
                            if send_lock and send_lock.locked():
                                try:
                                    send_lock.release()
                                except Exception:
                                    pass
                        frames += 1
                        # pacing to match client playback
                        await asyncio.sleep(tts.frame_duration_s)
                return float(frames) * tts.frame_duration_s

            while True:
                played = await _stream_once()
                played_once = True
                total_played_s += played
                # If no frames were streamed, break to avoid infinite loop
                if played == 0:
                    logger.warning("No frames streamed for ringtone %s", path)
                    break

                # If no play_duration requested, play only once
                if not play_duration:
                    break

                # If we've reached the desired total play duration, stop
                if total_played_s >= play_duration:
                    break

                # Otherwise loop and play again
        else:
            # Fallback: TTS speak the message
            async for frame in tts.synthesize(alarm.get("message", "Báo thức")):
                try:
                    await ws.send_bytes(frame)
                    await asyncio.sleep(tts.frame_duration_s)
                except Exception:
                    logger.warning("Failed to send TTS frame to %s", session.session_id)

        try:
            await ws.send_text(json.dumps({"type": "tts", "state": "stop", "session_id": session.session_id}, ensure_ascii=False))
        except Exception:
            pass
        # restore speaking flag
        try:
            session.is_speaking = False
        except Exception:
            pass

        logger.info("Alarm delivered to session %s", session.session_id)
    except Exception as e:
        logger.error("Error triggering alarm for %s: %s", session.session_id, e, exc_info=True)


async def _alarm_loop(poll_interval: float = 5.0) -> None:
    base = os.path.dirname(__file__)
    path = os.path.join(base, "alarms.json")

    # ensure default ringtone exists
    _ensure_default_ringtone()

    while True:
        now = datetime.now()
        try:
            if not os.path.exists(path):
                await asyncio.sleep(poll_interval)
                continue

            with open(path, "r", encoding="utf-8") as f:
                alarms = json.load(f)

            changed = False
            for alarm in alarms:
                if alarm.get("triggered"):
                    continue
                try:
                    alarm_dt = datetime.fromisoformat(alarm.get("time"))
                except Exception:
                    logger.warning("Skipping alarm with invalid time: %s", alarm)
                    continue

                if now >= alarm_dt:
                    # mark triggered asap to avoid double-trigger
                    alarm["triggered"] = True
                    changed = True

                    # Deliver to all connected sessions (best-effort)
                    sessions = get_all_sessions()
                    for session in sessions:
                        ws = ws_handler._session_ws.get(session.session_id)
                        if not ws:
                            continue
                        # fire off tasks per session
                        asyncio.create_task(_trigger_alarm_for_session(session, ws, alarm))

            if changed:
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(alarms, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.error("Failed to update alarms.json: %s", e, exc_info=True)

        except Exception as e:
            logger.error("Alarm scheduler error: %s", e, exc_info=True)

        await asyncio.sleep(poll_interval)


async def start_scheduler() -> None:
    logger.info("Starting alarm scheduler background task")
    asyncio.create_task(_alarm_loop())


def _ensure_default_ringtone(duration_s: float = 3.0, rate: int = 24000) -> None:
    """Generate a simple multi-tone WAV file as default ringtone if missing.

    This avoids adding copyrighted ringtones to the repo.
    """
    if os.path.exists(DEFAULT_RINGTONE):
        return

    try:
        import math
        import wave
        import struct

        logger.info("Generating default ringtone: %s", DEFAULT_RINGTONE)
        freq1 = 880.0
        freq2 = 1320.0
        amplitude = 16000
        n_samples = int(rate * duration_s)

        with wave.open(DEFAULT_RINGTONE, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            for i in range(n_samples):
                t = float(i) / rate
                # simple two-tone melody (beep-buzz)
                sample = amplitude * 0.5 * (
                    math.sin(2.0 * math.pi * freq1 * t) + 0.6 * math.sin(2.0 * math.pi * freq2 * t)
                )
                val = int(max(-32767, min(32767, sample)))
                wf.writeframes(struct.pack('<h', val))
        logger.info("Default ringtone generated")
    except Exception as e:
        logger.error("Failed to generate default ringtone: %s", e, exc_info=True)
