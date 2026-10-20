from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any


DEFAULT_SETTINGS: dict[str, Any] = {
    "calendar_source": "outlook_desktop",
    "client_id": "",
    "tenant_id": "organizations",
    "auto_record": False,
    "recording_consent": False,
    "cloud_consent": False,
    "microphone_id": None,
    "loopback_id": None,
    "whisper_model": "small",
    "language": "ja",
    "diarization": True,
    "llm_base_url": "https://api.openai.com/v1",
    "llm_model": "gpt-4.1-mini",
    "sync_interval_seconds": 60,
    "end_grace_seconds": 12,
}


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    return Path(base or Path.home() / ".local" / "share") / "giji-automemo"


class Secrets:
    """Only the operating system credential store; no plaintext fallback."""

    service = "giji-automemo"

    def get(self, name: str) -> str | None:
        import keyring

        try:
            raw = keyring.get_password(self.service, name)
            if raw and raw.startswith("giji-chunks:"):
                version, count = raw.removeprefix("giji-chunks:").split(":")
                if not 1 <= int(count) <= 10000:
                    return None
                parts = [keyring.get_password(self.service, f"{name}:{version}:{i}")
                         for i in range(int(count))]
                return "".join(parts) if all(p is not None for p in parts) else None
            return raw
        except (keyring.errors.KeyringError, ValueError, TypeError):
            return None

    def set(self, name: str, value: str) -> None:
        import keyring

        try:
            old = keyring.get_password(self.service, name)
            version = uuid.uuid4().hex
            # Windows Credential Manager limits individual blobs. MSAL caches do
            # not fit in one credential; write a new generation before its pointer.
            chunks = [value[i:i + 500] for i in range(0, len(value), 500)] or [""]
            if len(chunks) > 10000:
                raise RuntimeError("資格情報のサイズが上限を超えています。")
            written = []
            try:
                for i, chunk in enumerate(chunks):
                    part = f"{name}:{version}:{i}"
                    keyring.set_password(self.service, part, chunk)
                    written.append(part)
                keyring.set_password(self.service, name, f"giji-chunks:{version}:{len(chunks)}")
            except keyring.errors.KeyringError:
                for part in written:
                    try:
                        keyring.delete_password(self.service, part)
                    except keyring.errors.KeyringError:
                        pass
                raise
            self._remove_parts(name, old)
        except keyring.errors.KeyringError as exc:
            raise RuntimeError("OSの資格情報ストアに保存できません。Windowsのユーザー環境で起動してください。") from exc

    def delete(self, name: str) -> None:
        import keyring

        try:
            old = keyring.get_password(self.service, name)
            keyring.delete_password(self.service, name)
            self._remove_parts(name, old)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError as exc:
            raise RuntimeError("OSの資格情報ストアから削除できませんでした。") from exc

    def _remove_parts(self, name: str, raw: str | None) -> None:
        import keyring

        if raw and raw.startswith("giji-chunks:"):
            try:
                version, count = raw.removeprefix("giji-chunks:").split(":")
                total = max(0, min(int(count), 10000))
            except (ValueError, TypeError):
                return
            for i in range(total):
                try:
                    keyring.delete_password(self.service, f"{name}:{version}:{i}")
                except keyring.errors.KeyringError:
                    pass


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.data_dir / "giji.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meetings (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        self.db.commit()

    def settings(self) -> dict:
        with self._lock:
            result = dict(DEFAULT_SETTINGS)
            result.update({k: json.loads(v) for k, v in self.db.execute("SELECT key,value FROM settings")})
            return result

    def update_settings(self, values: dict) -> dict:
        with self._lock, self.db:
            for key, value in values.items():
                if key not in DEFAULT_SETTINGS:
                    continue
                self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
        return self.settings()

    def replace_meetings(self, meetings: list[dict]) -> None:
        with self._lock, self.db:
            self.db.execute("DELETE FROM meetings WHERE id NOT LIKE 'demo-%'")
            for meeting in meetings:
                self.db.execute(
                    "INSERT OR REPLACE INTO meetings VALUES (?,?)",
                    (meeting["id"], json.dumps(meeting, ensure_ascii=False)),
                )

    def add_meeting(self, meeting: dict) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meetings VALUES (?,?)",
                            (meeting["id"], json.dumps(meeting, ensure_ascii=False)))

    def meetings(self) -> list[dict]:
        with self._lock:
            return sorted(
                [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM meetings")],
                key=lambda m: m["start"],
            )

    def put_session(self, session: dict) -> dict:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)",
                            (session["id"], json.dumps(session, ensure_ascii=False)))
        return session

    def session(self, session_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT payload FROM sessions WHERE id=?", (session_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def patch_session(self, session_id: str, patch: dict) -> dict:
        with self._lock:
            session = self.session(session_id)
            if not session:
                raise KeyError(session_id)
            session.update(patch)
            return self.put_session(session)

    def sessions(self) -> list[dict]:
        with self._lock:
            return sorted(
                [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM sessions")],
                key=lambda s: s["started_at"], reverse=True,
            )

    def delete_session(self, session_id: str) -> None:
        with self._lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def remove_demo(self) -> None:
        with self._lock, self.db:
            self.db.execute("DELETE FROM meetings WHERE id LIKE 'demo-%'")
            self.db.execute("DELETE FROM sessions WHERE id LIKE 'demo-%'")

    def close(self) -> None:
        self.db.close()
