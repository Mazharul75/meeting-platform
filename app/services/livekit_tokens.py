"""LiveKit token issuing and room control (plan Section 10.1, 6.4).

Every token is for exactly one room, expires in 10 minutes, and uses a random identity so
LiveKit never learns a real name. Only this server holds the API secret.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from livekit import api

from app.config import Settings

log = logging.getLogger("app")
TOKEN_TTL = timedelta(minutes=10)


def new_identity() -> str:
    return uuid.uuid4().hex


def create_join_token(settings: Settings, room_name: str, identity: str) -> str:
    """A short-lived token that can join exactly one room and publish/subscribe media."""
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_ttl(TOKEN_TTL)
        .with_grants(api.VideoGrants(room_join=True, room=room_name, can_publish=True, can_subscribe=True))
    )
    return token.to_jwt()


async def close_room(settings: Settings, room_name: str) -> None:
    """Ends a live meeting for everyone by closing its LiveKit room."""
    if not settings.livekit_url:
        return
    client = api.LiveKitAPI(settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret)
    try:
        await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
    except Exception:  # noqa: BLE001 - closing a room that never opened is not an error
        log.info("could not close LiveKit room (it may already be closed)")
    finally:
        await client.aclose()


class ParticipantNotFound(Exception):
    """The identity is not currently in the room (they may have already left)."""


async def mute_participant_microphone(settings: Settings, room_name: str, identity: str) -> bool:
    """Host-only moderation: force-mutes one participant's microphone.

    LiveKit only allows the *server* to mute a track, never to unmute it - the participant has
    to unmute themselves. That is deliberate on LiveKit's part (and ours): a host can silence
    someone, but nothing server-side can secretly turn a stranger's microphone back on.

    Returns True if a microphone track was found and muted, False if the participant has no
    live microphone track right now (e.g. it was already off).
    """
    client = api.LiveKitAPI(settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret)
    try:
        try:
            participant = await client.room.get_participant(
                api.RoomParticipantIdentity(room=room_name, identity=identity)
            )
        except Exception as exc:  # noqa: BLE001 - the LiveKit SDK raises a generic error for 404s
            raise ParticipantNotFound(identity) from exc
        mic = next(
            (t for t in participant.tracks if t.type == api.TrackType.AUDIO and not t.muted),
            None,
        )
        if mic is None:
            return False
        await client.room.mute_published_track(
            api.MuteRoomTrackRequest(room=room_name, identity=identity, track_sid=mic.sid, muted=True)
        )
        return True
    finally:
        await client.aclose()
