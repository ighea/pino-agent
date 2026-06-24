import asyncio
import base64
import io
import mimetypes
import os
import time

import markdown as _md

from nio import (
    AsyncClient,
    AsyncClientConfig,
    LoginError,
    MegolmEvent,
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageText,
    UploadError,
)

import app.history as _history
from app.logger import logger
from app.triggers.base import BaseTrigger, TriggerEvent

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "")
MATRIX_USER = os.getenv("MATRIX_USER", "")
MATRIX_PASSWORD = os.getenv("MATRIX_PASSWORD", "")
MATRIX_ROOM_IDS = [r.strip() for r in os.getenv("MATRIX_ROOM_IDS", "").split(",") if r.strip()]
MATRIX_STORE_PATH = os.getenv("MATRIX_STORE_PATH", "./nio_store")
MATRIX_MAX_MSG_LEN = int(os.getenv("MATRIX_MAX_MSG_LEN", "4000"))
_FILE_TEXT_MAX = 8000  # max chars extracted from file content


def _split_message(text: str) -> list[str]:
    """Split text into chunks of at most MATRIX_MAX_MSG_LEN chars, breaking on paragraphs."""
    if len(text) <= MATRIX_MAX_MSG_LEN:
        return [text]
    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if len(para) > MATRIX_MAX_MSG_LEN:
            if current:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            for i in range(0, len(para), MATRIX_MAX_MSG_LEN):
                chunks.append(para[i:i + MATRIX_MAX_MSG_LEN])
            continue
        added = len(para) + (2 if current else 0)
        if current_len + added > MATRIX_MAX_MSG_LEN:
            chunks.append("\n\n".join(current))
            current, current_len = [para], len(para)
        else:
            current.append(para)
            current_len += added
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _extract_text_from_bytes(data: bytes, mime_type: str) -> str | None:
    """Extract readable text from file bytes based on MIME type."""
    if mime_type.startswith("text/") or mime_type in (
        "application/json", "application/xml", "application/yaml",
    ):
        try:
            text = data.decode("utf-8", errors="replace")
            if len(text) > _FILE_TEXT_MAX:
                text = text[:_FILE_TEXT_MAX] + "\n...[truncated]"
            return text
        except Exception:
            return None

    if mime_type == "application/pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(data))
            pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n\n".join(p.strip() for p in pages if p.strip())
            if len(text) > _FILE_TEXT_MAX:
                text = text[:_FILE_TEXT_MAX] + "\n...[truncated]"
            return text or None
        except ImportError:
            return None
        except Exception:
            return None

    return None


class MatrixTrigger(BaseTrigger):
    def __init__(self) -> None:
        self._server = None
        self._client: AsyncClient | None = None
        self._room_locks: dict[str, asyncio.Lock] = {}
        self._mention_prefixes: list[str] = []
        self._start_ms: int = 0

    async def start(self, server) -> None:
        self._server = server

        if not all([MATRIX_HOMESERVER, MATRIX_USER, MATRIX_PASSWORD]):
            print("Matrix trigger: MATRIX_HOMESERVER, MATRIX_USER, or MATRIX_PASSWORD not set — skipping.")
            return

        os.makedirs(MATRIX_STORE_PATH, exist_ok=True)
        config = AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True)
        self._client = AsyncClient(
            MATRIX_HOMESERVER,
            MATRIX_USER,
            store_path=MATRIX_STORE_PATH,
            config=config,
        )

        response = await self._client.login(MATRIX_PASSWORD)
        if isinstance(response, LoginError):
            print(f"Matrix trigger: login failed — {response.message}")
            await self._client.close()
            self._client = None
            return

        if self._client.should_upload_keys:
            await self._client.keys_upload()

        profile = await self._client.get_profile(self._client.user_id)
        display_name = (
            profile.displayname
            if hasattr(profile, "displayname") and profile.displayname
            else MATRIX_USER.split(":")[0].lstrip("@")
        )
        self._mention_prefixes = [
            f"@{display_name}: ",
            f"@{display_name} ",
            f"{display_name}: ",
            f"{display_name} ",
        ]

        for room_id in MATRIX_ROOM_IDS:
            await self._client.join(room_id)

        # Initial sync — loads room membership and E2EE session keys; no callbacks registered yet
        # so historical messages are silently consumed and won't be reprocessed.
        await self._client.sync(timeout=10000, full_state=True)
        await self._trust_all_room_devices()

        self._start_ms = int(time.time() * 1000)
        self._client.add_event_callback(self._on_undecryptable, MegolmEvent)
        self._client.add_event_callback(self._on_message, RoomMessageText)
        self._client.add_event_callback(self._on_image, RoomMessageImage)
        self._client.add_event_callback(self._on_file, RoomMessageFile)

        # Register proactive send handler so scheduler can push to Matrix rooms
        from app import scheduler as _sched
        _sched.register_proactive_handler(self._proactive_send)

        print(
            f"Matrix trigger connected as {MATRIX_USER} ({display_name}), "
            f"watching {len(MATRIX_ROOM_IDS)} room(s)."
        )
        try:
            await self._client.sync_forever(timeout=30000)
        finally:
            await self._client.close()
            self._client = None

    async def _trust_all_room_devices(self) -> None:
        """Fetch and verify all devices in our rooms so E2EE messages can be decrypted."""
        user_ids: set[str] = set()
        for room_id in MATRIX_ROOM_IDS:
            room = self._client.rooms.get(room_id)
            if room:
                user_ids.update(room.users.keys())
        if not user_ids:
            return
        try:
            await self._client.keys_query()
            for user_id in user_ids:
                try:
                    devices = self._client.device_store.active_user_devices(user_id)
                except AttributeError:
                    devices = self._client.device_store.get(user_id, {}).values()
                for device in devices:
                    self._client.verify_device(device)
        except Exception as e:
            print(f"[matrix] device trust step failed ({e}); "
                  "senders may need to re-send after their client shares keys.")

    async def _on_undecryptable(self, room, event: MegolmEvent) -> None:
        print(
            f"[matrix] could not decrypt message in {room.display_name} from {event.sender} "
            "— re-send the message or wait for the sender's client to share session keys."
        )

    def _extract_message(self, event, body: str) -> str | None:
        mentions = event.source.get("content", {}).get("m.mentions", {})
        if self._client.user_id in mentions.get("user_ids", []):
            return self._strip_prefix(body)
        for prefix in self._mention_prefixes:
            if body.lower().startswith(prefix.lower()):
                return body[len(prefix):].strip()
        return None

    def _strip_prefix(self, body: str) -> str:
        for prefix in self._mention_prefixes:
            if body.lower().startswith(prefix.lower()):
                return body[len(prefix):].strip()
        return body

    def _is_mentioned(self, event, body: str) -> bool:
        mentions = event.source.get("content", {}).get("m.mentions", {})
        if self._client.user_id in mentions.get("user_ids", []):
            return True
        return any(body.lower().startswith(p.lower()) for p in self._mention_prefixes)

    async def _download_bytes(self, event) -> tuple[bytes, str] | None:
        """Download a Matrix file/image event, decrypt if E2EE. Returns (bytes, mime_type) or None."""
        content = event.source.get("content", {})
        encrypted_file = content.get("file")
        mxc_url = encrypted_file.get("url") if encrypted_file else getattr(event, "url", None)

        if not mxc_url:
            return None

        try:
            dl = await self._client.download(mxc=mxc_url)
            if not hasattr(dl, "body"):
                logger.log_error("Download response has no body", {"mxc": mxc_url})
                return None

            data: bytes = dl.body
            mime_type: str = (
                getattr(dl, "content_type", None)
                or getattr(event, "mimetype", None)
                or "application/octet-stream"
            )

            if encrypted_file:
                from nio.crypto import decrypt_attachment
                key = base64.urlsafe_b64decode(encrypted_file["key"]["k"] + "==")
                iv = base64.urlsafe_b64decode(encrypted_file["iv"] + "==")
                sha256 = encrypted_file["hashes"]["sha256"]
                data = decrypt_attachment(data, key, sha256, iv)
                mime_type = encrypted_file.get("mimetype", mime_type)

            return data, mime_type
        except Exception as e:
            logger.log_error("Download failed", {"error": str(e)})
            return None

    async def _download_image(self, event) -> tuple[str, str] | None:
        """Download an image event and return (base64_data, mime_type) or None."""
        result = await self._download_bytes(event)
        if result is None:
            return None
        data, mime_type = result
        return base64.b64encode(data).decode(), mime_type

    async def _proactive_send(self, room_id: str | None, text: str) -> None:
        """Send a proactive message to one room or all configured rooms."""
        if self._client is None:
            return
        targets = [room_id] if room_id else MATRIX_ROOM_IDS
        for rid in targets:
            try:
                chunks = _split_message(text)
                for chunk in chunks:
                    await self._client.room_send(
                        room_id=rid,
                        message_type="m.room.message",
                        content={
                            "msgtype": "m.text",
                            "body": chunk,
                            "format": "org.matrix.custom.html",
                            "formatted_body": _md.markdown(chunk),
                        },
                    )
            except Exception as e:
                print(f"[matrix] proactive send to {rid} failed: {e}")

    async def _fire_event(self, room, matrix_event, input_content: str | list) -> None:
        """Build trigger callbacks and dispatch a TriggerEvent for a room message."""
        captured_event_id = matrix_event.event_id
        captured_room_id = room.room_id
        lock = self._room_locks.setdefault(captured_room_id, asyncio.Lock())

        async def react_fn(emoji: str) -> None:
            try:
                await self._client.room_send(
                    room_id=captured_room_id,
                    message_type="m.reaction",
                    content={
                        "m.relates_to": {
                            "rel_type": "m.annotation",
                            "event_id": captured_event_id,
                            "key": emoji,
                        }
                    },
                )
            except Exception as e:
                print(f"[matrix] reaction '{emoji}' failed: {e}")

        async def respond_fn(text: str) -> None:
            if not text.strip():
                return
            chunks = _split_message(text)
            for chunk in chunks:
                await self._client.room_send(
                    room_id=captured_room_id,
                    message_type="m.room.message",
                    content={
                        "msgtype": "m.text",
                        "body": chunk,
                        "format": "org.matrix.custom.html",
                        "formatted_body": _md.markdown(chunk),
                    },
                )

        async def status_fn(message: str) -> None:
            if not message:
                return
            try:
                await self._client.room_send(
                    room_id=captured_room_id,
                    message_type="m.room.message",
                    content={"msgtype": "m.notice", "body": message},
                )
            except Exception as e:
                print(f"[matrix] status message failed: {e}")

        async def deliver_fn(path: str) -> str:
            from app.tools.files import _safe_path

            target = _safe_path(path)
            if target is None or not target.is_file():
                return "Error: file not found in workspace."

            filename = target.name
            file_bytes = target.read_bytes()
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

            response, _ = await self._client.upload(
                io.BytesIO(file_bytes),
                content_type=mime_type,
                filename=filename,
                filesize=len(file_bytes),
            )
            if isinstance(response, UploadError):
                return f"Error: Matrix upload failed — {response.message}"

            if mime_type.startswith("image/"):
                msgtype = "m.image"
            elif mime_type.startswith("video/"):
                msgtype = "m.video"
            elif mime_type.startswith("audio/"):
                msgtype = "m.audio"
            else:
                msgtype = "m.file"

            await self._client.room_send(
                room_id=captured_room_id,
                message_type="m.room.message",
                content={
                    "msgtype": msgtype,
                    "body": filename,
                    "url": response.content_uri,
                    "info": {"mimetype": mime_type, "size": len(file_bytes)},
                },
            )
            return f"File '{filename}' sent to the room."

        trigger_event = TriggerEvent(
            input=input_content,
            source="matrix",
            metadata={"room_id": captured_room_id, "sender": matrix_event.sender},
            history=_history.load(captured_room_id),
            respond_fn=respond_fn,
            status_fn=status_fn,
            react_fn=react_fn,
            deliver_fn=deliver_fn,
        )

        async def _typing_keepalive() -> None:
            try:
                while True:
                    await self._client.room_typing(captured_room_id, typing_state=True, timeout=30000)
                    await asyncio.sleep(25)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"[matrix] typing keepalive error: {e}")

        async def run_with_lock() -> None:
            async with lock:
                typing_task = asyncio.create_task(_typing_keepalive())
                try:
                    await self._server.handle_event(trigger_event)
                    _history.save(captured_room_id, trigger_event.history)
                    asyncio.create_task(react_fn("✅"))
                except Exception:
                    asyncio.create_task(react_fn("❌"))
                    raise
                finally:
                    typing_task.cancel()
                    await self._client.room_typing(captured_room_id, typing_state=False)
            logger.log_event("MATRIX_RESPONSE", {"room": captured_room_id, "sender": matrix_event.sender})

        asyncio.create_task(react_fn("👀"))
        asyncio.create_task(run_with_lock())

    async def _on_message(self, room, event) -> None:
        print(f"[matrix] event: sender={event.sender} ts={event.server_timestamp} start={self._start_ms} body={getattr(event, 'body', '?')!r}")
        if event.server_timestamp < self._start_ms:
            print("[matrix] skipped: stale event")
            return
        if event.sender == self._client.user_id:
            print("[matrix] skipped: own message")
            return

        body = event.body.strip()
        if not body:
            return

        is_dm = len(room.users) == 2
        print(f"[matrix] is_dm={is_dm} users={list(room.users.keys())}")
        if is_dm:
            message = body
        else:
            message = self._extract_message(event, body)
            print(f"[matrix] extracted message: {message!r}")
            if message is None:
                return

        if not message:
            return

        sender_name = room.user_name(event.sender) or event.sender
        input_text = f"[{sender_name}]: {message}" if not is_dm else message
        await self._fire_event(room, event, input_text)

    async def _on_image(self, room, event) -> None:
        print(f"[matrix] image event: sender={event.sender}")
        if event.server_timestamp < self._start_ms:
            return
        if event.sender == self._client.user_id:
            return

        is_dm = len(room.users) == 2
        body = event.body.strip()

        if not is_dm:
            if not self._is_mentioned(event, body):
                return
            caption = self._strip_prefix(body) if body else ""
        else:
            caption = body

        image_data = await self._download_image(event)
        if image_data is None:
            return

        b64, mime_type = image_data
        sender_name = room.user_name(event.sender) or event.sender
        prefix = f"[{sender_name}]: " if not is_dm else ""
        text_part = f"{prefix}{caption}".strip() or "Describe this image."

        multimodal_input: list = [
            {"type": "text", "text": text_part},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
        ]
        await self._fire_event(room, event, multimodal_input)

    async def _on_file(self, room, event) -> None:
        print(f"[matrix] file event: sender={event.sender} body={getattr(event, 'body', '?')!r}")
        if event.server_timestamp < self._start_ms:
            return
        if event.sender == self._client.user_id:
            return

        is_dm = len(room.users) == 2
        filename = event.body.strip() or "file"

        if not is_dm:
            if not self._is_mentioned(event, filename):
                return

        result = await self._download_bytes(event)
        sender_name = room.user_name(event.sender) or event.sender
        prefix = f"[{sender_name}]: " if not is_dm else ""

        if result is not None:
            data, mime_type = result
            text = _extract_text_from_bytes(data, mime_type)
        else:
            text = None

        if text:
            input_text = f"{prefix}Contents of {filename!r}:\n\n{text}"
        else:
            content = event.source.get("content", {})
            size = content.get("info", {}).get("size", 0)
            size_str = f"{size // 1024} KB" if size >= 1024 else f"{size} B"
            mime_type = result[1] if result else "unknown"
            input_text = (
                f"{prefix}Sent a file: {filename!r} ({mime_type}, {size_str}). "
                "I can't read this file type directly."
            )

        await self._fire_event(room, event, input_text)

    async def stop(self) -> None:
        if self._client:
            await self._client.close()
