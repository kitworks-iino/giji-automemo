from __future__ import annotations

import hmac
import hashlib
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import WHISPER_MODELS
from .service import GijiService, public_session


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calendar_source: Literal["teams_desktop", "outlook_desktop", "graph"] = "teams_desktop"
    teams_calendar_mode: Literal["uia", "cdp"] = "uia"
    client_id: str = Field("", max_length=100)
    tenant_id: str = Field("organizations", max_length=100)
    auto_record: bool = False
    recording_consent: bool = False
    cloud_consent: bool = False
    microphone_id: int | None = Field(None, ge=0)
    loopback_id: int | None = Field(None, ge=0)
    whisper_model: str = "small"
    language: str = Field("ja", pattern=r"^[a-z]{2,3}$")
    diarization: bool = True
    llm_base_url: str = Field("https://api.openai.com/v1", max_length=500)
    llm_model: str = Field("gpt-4.1-mini", min_length=1, max_length=150)
    sync_interval_seconds: int = Field(60, ge=30, le=3600)
    end_grace_seconds: int = Field(12, ge=4, le=120)
    api_key: str = Field("", max_length=16000)

    @field_validator("client_id")
    @classmethod
    def client(cls, value):
        if value and not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", value):
            raise ValueError("クライアントIDはアプリ登録のUUIDを指定してください")
        return value

    @field_validator("tenant_id")
    @classmethod
    def tenant(cls, value):
        if value not in ("organizations", "common", "consumers"):
            cls.client(value)
            if not value:
                raise ValueError("テナントIDを指定してください")
        return value

    @field_validator("whisper_model")
    @classmethod
    def model_name(cls, value):
        if value not in WHISPER_MODELS:
            raise ValueError("対応モデルを指定してください")
        return value

    @field_validator("llm_base_url")
    @classmethod
    def url(cls, value):
        parsed = urlparse(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("LLM APIには認証情報を含まないHTTPSのURLを指定してください")
        return value.rstrip("/")


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meeting_id: str | None = None
    title: str | None = Field(None, max_length=200)


class SpeakersRequest(BaseModel):
    speaker_names: dict[str, str]

    @field_validator("speaker_names")
    @classmethod
    def bounded(cls, value):
        if len(value) > 100 or any(len(k) > 80 or not 0 < len(v.strip()) <= 100 for k, v in value.items()):
            raise ValueError("話者名は1〜100文字で入力してください")
        return {k: v.strip() for k, v in value.items()}


def create_app(service: GijiService, token: str, port: int, *, run_background=True,
               frontend_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        if run_background:
            await service.start()
        yield
        if run_background:
            await service.close()

    app = FastAPI(title="giji", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    ui_dir = (frontend_dir or Path(__file__).resolve().parent.parent / "frontend" / "dist").resolve()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    def audio_signature(path: str) -> str:
        return hmac.new(token.encode(), path.encode(), hashlib.sha256).hexdigest()

    def session_response(session: dict) -> dict:
        response = public_session(session)
        if not session["id"].startswith("demo-"):
            urls = {}
            for track in ("microphone", "system"):
                path = f"/api/sessions/{session['id']}/audio/{track}"
                urls[track] = f"{path}?playback={audio_signature(path)}"
            response["audio_urls"] = urls
        return response

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"detail": "Invalid local host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins:
            return JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)
        if request.url.path.startswith("/api"):
            expected = f"Bearer {token}"
            # A signature grants read access to exactly one audio track. The
            # browser can use Range requests without buffering a huge WAV in JS.
            audio_read = request.method in ("GET", "HEAD") and re.fullmatch(
                r"/api/sessions/[a-f0-9]{32}/audio/(microphone|system)", request.url.path)
            signed_audio = bool(audio_read) and hmac.compare_digest(
                request.query_params.get("playback", ""), audio_signature(request.url.path))
            if not signed_audio and not hmac.compare_digest(request.headers.get("authorization", ""), expected):
                return JSONResponse({"detail": "アプリを起動し直して画面を開いてください。"}, status_code=401)
        # The app only accepts small control payloads. Audio is never uploaded.
        try:
            if int(request.headers.get("content-length", "0")) > 1_000_000:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Invalid request"}, status_code=400)
        response = await call_next(request)
        response.headers.update({
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; font-src 'self'; connect-src 'self'; media-src 'self' blob:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "会議が見つかりません。"}, status_code=404)

    @app.exception_handler(RuntimeError)
    async def runtime_error(request, exc):
        return JSONResponse({"detail": "操作に失敗しました。接続・デバイス・資格情報ストアを確認してください。"}, status_code=400)

    def get_session(sid):
        session = service.store.session(sid)
        if not session:
            raise KeyError(sid)
        return session

    @app.get("/api/snapshot")
    def snapshot():
        return service.snapshot()

    @app.post("/api/settings")
    async def settings(patch: SettingsPatch):
        return await service.update_settings(patch.model_dump(exclude_unset=True))

    @app.get("/api/devices")
    def devices():
        return service.recorder.list_devices()

    @app.post("/api/auth/login")
    async def login():
        return await service.login()

    @app.post("/api/auth/logout")
    async def logout():
        await service.logout()
        return {"ok": True}

    @app.post("/api/sync")
    async def sync():
        await service.sync()
        return {"ok": True}

    @app.post("/api/teams/prepare")
    async def teams_prepare():
        return await service.prepare_teams()

    @app.post("/api/teams/disconnect")
    async def teams_disconnect():
        return await service.disconnect_teams()

    @app.post("/api/meetings/{meeting_id}/arm")
    def arm(meeting_id: str):
        service.arm_meeting(meeting_id)
        return {"ok": True}

    @app.post("/api/meetings/{meeting_id}/join")
    async def join(meeting_id: str):
        from .microsoft import valid_teams_url
        meeting = next((m for m in service.store.meetings() if m["id"] == meeting_id), None)
        if not meeting or meeting.get("cancelled"):
            raise KeyError(meeting_id)
        if not valid_teams_url(meeting.get("join_url", "")):
            raise ValueError("参加URLを取得できませんでした。Teamsでこの会議を開いて参加してください。")
        service.arm_meeting(meeting_id)
        return {"join_url": meeting["join_url"]}

    @app.post("/api/recordings/start")
    async def start(body: StartRequest):
        return await service.start_recording(body.meeting_id, body.title)

    @app.post("/api/recordings/stop")
    async def stop():
        return await service.stop_recording()

    @app.get("/api/sessions/{sid}")
    def session(sid: str):
        return session_response(get_session(sid))

    @app.post("/api/sessions/{sid}/retry")
    async def retry(sid: str):
        return await service.retry(sid)

    @app.post("/api/sessions/{sid}/speakers")
    def speakers(sid: str, body: SpeakersRequest):
        session = get_session(sid)
        if sid in service.pending:
            raise ValueError("音声処理が完了してから話者名を変更してください。")
        labels = {s["speaker"] for s in session["segments"]}
        if any(k not in labels for k in body.speaker_names):
            raise ValueError("文字起こしにない話者は変更できません。")
        return public_session(service.store.patch_session(sid, {
            "speaker_names": {**session["speaker_names"], **body.speaker_names}}))

    @app.get("/api/sessions/{sid}/audio/{track}")
    def audio(sid: str, track: str):
        session = get_session(sid)
        if track not in ("microphone", "system") or sid.startswith("demo-"):
            raise HTTPException(404, "音声がありません。")
        path = service.safe_directory(session) / f"{track}.wav"
        if not path.is_file():
            raise HTTPException(404, "音声がありません。")
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/sessions/{sid}/export")
    def export(sid: str):
        from .pipeline import render_markdown
        session = get_session(sid)
        safe_title = re.sub(r'[\\/:*?"<>|\r\n]', "_", session["title"])[:90]
        return Response(
            render_markdown(session), media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_title + '.md')}"},
        )

    @app.delete("/api/sessions/{sid}")
    def delete(sid: str):
        service.delete_session(sid)
        return {"ok": True}

    @app.post("/api/models/download")
    async def models():
        await service.download_models()
        return {"ok": True}

    @app.post("/api/demo")
    def demo():
        service.seed_demo()
        return {"ok": True}

    @app.delete("/api/demo")
    def remove_demo():
        service.store.remove_demo()
        return {"ok": True}

    @app.get("/{path:path}")
    def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        target = (ui_dir / path).resolve()
        if not target.is_relative_to(ui_dir):
            raise HTTPException(404)
        if path and target.is_file():
            return FileResponse(target)
        index = ui_dir / "index.html"
        if not index.is_file():
            return Response("UIが未ビルドです。frontendで npm ci と npm run build を実行してください。",
                            media_type="text/plain", status_code=503)
        return FileResponse(index)

    return app
