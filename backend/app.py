"""FastAPI entry point for the MediX full-stack MVP."""
from __future__ import annotations

import time
import uuid
import json
import asyncio
import shutil
import hashlib
import mimetypes
import re
import os
import threading
from pathlib import Path
from typing import Any, Dict

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from swarm import SwarmCoordinator

from .schemas import ChatRequest, ChatResponse, SessionMessage, SessionResponse
from .storage import SQLiteStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


# Parsing large PDFs concurrently is memory-heavy, while Milvus Lite permits a
# single writer for one database file. Keep the HTTP endpoints responsive but
# bound the actual background work inside the process.
_PARSE_LIMITER = threading.Semaphore(_positive_int_env("MEDIX_PARSE_CONCURRENCY", 2))
_INDEX_JOB_LOCK = threading.Lock()

app = FastAPI(
    title="MediX Medical Evidence Assistant",
    version="0.2.0",
    description="Provenance-aware multi-agent medical RAG service",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def recover_ingestion_state() -> None:
    """Make interrupted ingestion visible and retryable after a restart."""
    store = getattr(app.state, "store", None) or SQLiteStore()
    store.recover_incomplete_jobs()


def _state(request: Request) -> Dict[str, Any]:
    if not hasattr(request.app.state, "coordinator"):
        request.app.state.store = SQLiteStore()
        request.app.state.coordinator = SwarmCoordinator(conversation_store=request.app.state.store)
    return {
        "coordinator": request.app.state.coordinator,
        "store": request.app.state.store,
    }


def _citation_list(result: Dict[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("citations") or result.get("sources") or []
    if not raw:
        for call in result.get("tool_results") or []:
            tool_result = call.get("result") or {}
            raw.extend(tool_result.get("citations") or tool_result.get("sources") or [])
    citations = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        citations.append(
            {
                "id": str(item.get("id", "source")),
                "title": str(item.get("title", item.get("id", "医学来源"))),
                "type": str(item.get("type", "unknown")),
                "url": item.get("url"),
                "citation_allowed": bool(item.get("citation_allowed", True)) and item.get("type") != "generated",
                "status": str(item.get("status", "retrieved")),
                "excerpt": str(item.get("excerpt", ""))[:1000],
                "heading_path": item.get("heading_path"),
                "document_record_id": item.get("document_record_id"),
                "chunk_id": item.get("chunk_id"),
                "page": item.get("page"),
                "retrieval_score": item.get("retrieval_score"),
                "rerank_score": item.get("rerank_score"),
                "authority": item.get("authority"),
                "content_fetched": item.get("content_fetched"),
                "domain": item.get("domain"),
            }
        )
    return citations


def _evidence_trace(result: Dict[str, Any]) -> Dict[str, Any]:
    trace = dict(result.get("evidence_trace") or result.get("swarm_metadata") or {})
    unresolved = []
    for call in result.get("tool_results") or []:
        tool_result = call.get("result") or {}
        if tool_result.get("evidence_trace"):
            trace.update(tool_result["evidence_trace"])
        unresolved.extend(tool_result.get("unresolved_knowledge") or [])
    if unresolved:
        trace["unresolved_knowledge"] = list(dict.fromkeys(unresolved))
    return trace


def _validate_citations(answer: str, citations: list[dict[str, Any]]) -> dict[str, Any]:
    """Check that bracketed citation IDs in the answer are real and citable."""
    referenced = set(re.findall(r"\[((?:kb|web|gen)-[A-Za-z0-9_-]+)\]", answer or ""))
    known = {str(item.get("id")) for item in citations}
    invalid = sorted(referenced - known)
    generated = sorted(str(item.get("id")) for item in citations if not item.get("citation_allowed", True) and str(item.get("id")) in referenced)
    return {"referenced_ids": sorted(referenced), "invalid_ids": invalid, "generated_ids_in_answer": generated, "valid": not invalid and not generated}


@app.get("/api/v1/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "medix-api", "version": app.version}


@app.get("/api/v1/ready")
async def readiness(request: Request) -> dict[str, Any]:
    """Dependency/config readiness without triggering a costly embedding load."""
    checks: Dict[str, Any] = {"database": "ok", "uploads": "ok", "llm_key": "configured"}
    try:
        _state(request)["store"].list_sessions()
    except Exception as exc:
        checks["database"] = f"error: {exc}"
    try:
        upload_root = PROJECT_ROOT / "data" / "uploads"
        upload_root.mkdir(parents=True, exist_ok=True)
        checks["uploads"] = "ok" if upload_root.is_dir() else "error"
    except Exception as exc:
        checks["uploads"] = f"error: {exc}"
    import os
    if not os.getenv("MEDIX_LLM_API_KEY"):
        checks["llm_key"] = "missing"
    ready = all(value in {"ok", "configured"} for value in checks.values())
    return {"status": "ok" if ready else "degraded", "checks": checks}


@app.post("/api/v1/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    state = _state(request)
    session_id = payload.session_id or str(uuid.uuid4())
    store = state["store"]
    store.append_message(session_id, "user", payload.message, metadata={"source": "user_input", "user_id": payload.user_id, "request_id": request_id}, owner_id=payload.user_id)

    try:
        result = await state["coordinator"].process(
            question=payload.message,
            context=payload.context,
            session_id=session_id,
            enable_swarm=payload.enable_swarm,
            user_id=payload.user_id,
        )
    except Exception as exc:
        store.append_message(session_id, "assistant", "医学问答服务暂时不可用。", metadata={"status": "failed", "error": str(exc)[:1000], "request_id": request_id, "source": "api_error"}, owner_id=payload.user_id)
        raise HTTPException(status_code=502, detail=f"医学问答服务暂时不可用: {exc}") from exc

    answer = str(result.get("answer") or "抱歉，系统未能生成回答。")
    warnings = list(result.get("warnings") or [])
    if result.get("timeout_occurred"):
        warnings.append("部分 Agent 未在时限内完成，当前结果可能不完整。")
    if not result.get("agents_involved"):
        warnings.append("未返回可用的 Agent 执行轨迹。")
    citations = _citation_list(result)
    evidence_trace = _evidence_trace(result)
    citation_validation = _validate_citations(answer, citations)
    evidence_trace["citation_validation"] = citation_validation
    if not citation_validation["valid"]:
        warnings.append("回答中的部分引用未通过来源校验，请以证据栏为准。")
    if result.get("error"):
        warnings.append("上游模型调用失败，已停止重复重试；请检查模型和接口配置。")

    mode = str(result.get("mode") or ("swarm" if result.get("swarm_enabled") else "single_agent"))
    response = ChatResponse(
        request_id=request_id,
        session_id=session_id,
        answer=answer,
        mode=mode,
        agents_involved=list(result.get("agents_involved") or []),
        suggestions=list(result.get("suggestions") or []),
        citations=citations,
        evidence_trace=evidence_trace,
        warnings=warnings,
        disclaimer=str(result.get("disclaimer") or "以上信息仅供参考，不能替代专业医生的诊断和治疗。"),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    message_metadata = response.model_dump(exclude={"answer"})
    message_metadata.update({
        "source": "api_response",
        "memory_version": request_id,
        "tool_results": [
            {"tool": item.get("tool"), "result": str(item.get("result", ""))[:2000]}
            for item in (result.get("tool_results") or [])
        ],
    })
    store.append_message(session_id, "assistant", answer, metadata=message_metadata, owner_id=payload.user_id)
    return response


@app.post("/api/v1/chat/stream")
async def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    """SSE endpoint: emits workflow stages and the final structured response."""
    async def events():
        started = time.perf_counter()
        request_id = str(uuid.uuid4())
        state = _state(request)
        session_id = payload.session_id or str(uuid.uuid4())
        store = state["store"]
        store.append_message(session_id, "user", payload.message, metadata={"source": "user_input", "user_id": payload.user_id, "request_id": request_id}, owner_id=payload.user_id)

        def emit(event: str, data: Dict[str, Any]) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield emit("accepted", {"request_id": request_id, "session_id": session_id})
        await asyncio.sleep(0)
        yield emit("stage", {"name": "routing", "label": "分析问题并选择 Agent"})
        try:
            result = await state["coordinator"].process(
                question=payload.message,
                context=payload.context,
                session_id=session_id,
                enable_swarm=payload.enable_swarm,
                user_id=payload.user_id,
            )
            answer = str(result.get("answer") or "抱歉，系统未能生成回答。")
            yield emit("stage", {"name": "evidence", "label": "整理检索证据和来源"})
            # AgentLoop currently returns a completed answer; expose it incrementally
            # over SSE so the UI remains responsive while token-level generation is
            # added to LLMClient in the next iteration.
            for offset in range(0, len(answer), 80):
                yield emit("answer_delta", {"text": answer[offset:offset + 80]})
                await asyncio.sleep(0)
            warnings = list(result.get("warnings") or [])
            if result.get("timeout_occurred"):
                warnings.append("部分 Agent 未在时限内完成，当前结果可能不完整。")
            citations = _citation_list(result)
            evidence_trace = _evidence_trace(result)
            citation_validation = _validate_citations(answer, citations)
            evidence_trace["citation_validation"] = citation_validation
            if not citation_validation["valid"]:
                warnings.append("回答中的部分引用未通过来源校验，请以证据栏为准。")
            if result.get("error"):
                warnings.append("上游模型调用失败，已停止重复重试；请检查模型和接口配置。")
            response = ChatResponse(
                request_id=request_id,
                session_id=session_id,
                answer=answer,
                mode=str(result.get("mode") or ("swarm" if result.get("swarm_enabled") else "single_agent")),
                agents_involved=list(result.get("agents_involved") or []),
                suggestions=list(result.get("suggestions") or []),
                citations=citations,
                evidence_trace=evidence_trace,
                warnings=warnings,
                disclaimer=str(result.get("disclaimer") or "以上信息仅供参考，不能替代专业医生的诊断和治疗。"),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            message_metadata = response.model_dump(exclude={"answer"})
            message_metadata.update({
                "source": "api_stream_response",
                "memory_version": request_id,
                "tool_results": [
                    {"tool": item.get("tool"), "result": str(item.get("result", ""))[:2000]}
                    for item in (result.get("tool_results") or [])
                ],
            })
            store.append_message(session_id, "assistant", answer, metadata=message_metadata, owner_id=payload.user_id)
            yield emit("completed", response.model_dump())
        except Exception as exc:
            error_message = f"医学问答服务暂时不可用: {exc}"
            store.append_message(session_id, "assistant", "医学问答服务暂时不可用。", metadata={"status": "failed", "error": str(exc)[:1000], "request_id": request_id, "source": "api_error"}, owner_id=payload.user_id)
            yield emit("error", {"message": error_message, "request_id": request_id, "retryable": getattr(exc, "medix_non_retryable", False) is False})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/v1/sessions/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, request: Request) -> SessionResponse:
    state = _state(request)
    messages = state["store"].get_messages(session_id)
    if messages is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionResponse(
        session_id=session_id,
        messages=[SessionMessage(**message) for message in messages],
    )


@app.get("/api/v1/memories")
async def list_memories(user_id: str = "anonymous", include_deleted: bool = False, request: Request = None) -> list[dict[str, Any]]:
    """List memories within one user scope; project/agent scopes are intentionally not used yet."""
    store = _state(request)["store"]
    return store.list_memories(user_id, status=None if include_deleted else "active")


@app.post("/api/v1/memories")
async def create_memory(request: Request) -> dict[str, Any]:
    payload = await request.json()
    user_id = str(payload.get("user_id") or "anonymous")
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    return _state(request)["store"].add_memory(
        user_id=user_id,
        content=content,
        memory_type=str(payload.get("memory_type") or "fact"),
        source_session_id=str(payload.get("source_session_id") or ""),
        source_message_id=payload.get("source_message_id"),
        confidence=float(payload.get("confidence", 0.5)),
        expires_at=payload.get("expires_at"),
        metadata=payload.get("metadata") or {},
    )


@app.patch("/api/v1/memories/{memory_id}")
async def update_memory(memory_id: int, request: Request, user_id: str = "anonymous") -> dict[str, Any]:
    payload = await request.json()
    if "metadata" in payload:
        payload["metadata_json"] = json.dumps(payload.pop("metadata") or {}, ensure_ascii=False)
    memory = _state(request)["store"].update_memory(memory_id, user_id, **payload)
    if not memory:
        raise HTTPException(status_code=404, detail="memory not found")
    return memory


@app.delete("/api/v1/memories/{memory_id}")
async def delete_memory(memory_id: int, user_id: str = "anonymous", request: Request = None) -> dict[str, Any]:
    if not _state(request)["store"].delete_memory(memory_id, user_id):
        raise HTTPException(status_code=404, detail="memory not found")
    return {"status": "deleted", "memory_id": memory_id}


@app.get("/api/v1/sessions")
async def list_sessions(request: Request) -> list[dict[str, Any]]:
    return _state(request)["store"].list_sessions()


@app.patch("/api/v1/sessions/{session_id}")
async def rename_session(session_id: str, request: Request) -> dict[str, Any]:
    payload = await request.json()
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    store = _state(request)["store"]
    if not store.rename_session(session_id, title):
        raise HTTPException(status_code=404, detail="session not found")
    return {"id": session_id, "title": title[:80]}


@app.delete("/api/v1/sessions/{session_id}")
async def delete_session(session_id: str, request: Request) -> dict[str, Any]:
    store = _state(request)["store"]
    if not store.delete_session(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return {"status": "deleted", "session_id": session_id}


@app.get("/api/v1/knowledge/documents")
async def list_documents(request: Request, category: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    documents = _state(request)["store"].list_documents()
    if category:
        documents = [item for item in documents if item.get("category") == category]
    if status:
        documents = [item for item in documents if item.get("status") == status]
    return documents


@app.get("/api/v1/knowledge/documents/{document_id}")
async def get_document(document_id: int, request: Request) -> dict[str, Any]:
    document = _state(request)["store"].get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    return document


@app.patch("/api/v1/knowledge/documents/{document_id}")
async def update_document_metadata(document_id: int, request: Request) -> dict[str, Any]:
    store = _state(request)["store"]
    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    payload = await request.json()
    changes = {}
    for key in ("category", "source_type", "authority_level", "source_url", "publisher", "license", "external_id", "corpus_id", "language", "published_at"):
        if key in payload and payload[key] is not None:
            changes[key] = str(payload[key])[:100]
    if "tags" in payload:
        tags = payload["tags"] if isinstance(payload["tags"], list) else []
        changes["tags_json"] = json.dumps([str(tag)[:50] for tag in tags[:30]], ensure_ascii=False)
    if changes:
        store.update_document(document_id, **changes)
    return store.get_document(document_id) or document


@app.get("/api/v1/knowledge/documents/{document_id}/file")
async def download_document(document_id: int, request: Request) -> FileResponse:
    document = _state(request)["store"].get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    target = Path(document["path"]).resolve()
    upload_root = (PROJECT_ROOT / "data" / "uploads").resolve()
    if upload_root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="original file not found")
    return FileResponse(target, filename=document["filename"], media_type=document.get("mime_type") or None)


@app.get("/api/v1/knowledge/documents/{document_id}/preview")
async def preview_document(document_id: int, request: Request) -> FileResponse:
    """Serve the original file inline for browser-capable formats."""
    document = _state(request)["store"].get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    target = Path(document["path"]).resolve()
    upload_root = (PROJECT_ROOT / "data" / "uploads").resolve()
    if upload_root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="original file not found")
    return FileResponse(target, filename=document["filename"], media_type=document.get("mime_type") or None, content_disposition_type="inline")


@app.get("/api/v1/knowledge/documents/{document_id}/chunks")
async def list_document_chunks(document_id: int, request: Request) -> list[dict[str, Any]]:
    store = _state(request)["store"]
    if not store.get_document(document_id):
        raise HTTPException(status_code=404, detail="document not found")
    return store.list_chunks(document_id)


@app.get("/api/v1/knowledge/documents/{document_id}/chunks/search")
async def search_document_chunks(
    document_id: int,
    request: Request,
    q: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    store = _state(request)["store"]
    if not store.get_document(document_id):
        raise HTTPException(status_code=404, detail="document not found")
    return store.search_chunks(document_id, q, page, page_size)


@app.get("/api/v1/knowledge/documents/{document_id}/parsed")
async def parsed_document_preview(document_id: int, request: Request) -> dict[str, Any]:
    store = _state(request)["store"]
    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    return {
        "document": document,
        "chunking": {"strategy": "heading_tree_and_tables_then_character_window", "window_size": 1600, "overlap": 100, "unit": "characters"},
        "chunks": store.list_chunks(document_id),
    }


@app.get("/api/v1/knowledge/citations/{citation_id}")
async def citation_detail(citation_id: str, request: Request, document_id: int | None = None, chunk_id: int | None = None) -> dict[str, Any]:
    """Return the exact persisted chunk behind a citation when available."""
    store = _state(request)["store"]
    if document_id is not None:
        document = store.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="document not found")
        chunks = store.list_chunks(document_id)
        if chunk_id is not None and 0 <= chunk_id < len(chunks):
            return {"citation_id": citation_id, "document": document, "chunk": chunks[chunk_id]}
        return {"citation_id": citation_id, "document": document, "chunks": chunks}
    return {"citation_id": citation_id, "external": True, "message": "该来源没有本地文档定位信息"}


@app.get("/api/v1/knowledge/jobs/{job_id}")
async def get_ingest_job(job_id: str, request: Request) -> dict[str, Any]:
    job = _state(request)["store"].get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="ingest job not found")
    return job


@app.get("/api/v1/knowledge/documents/{document_id}/jobs")
async def list_ingest_jobs(document_id: int, request: Request) -> list[dict[str, Any]]:
    store = _state(request)["store"]
    if not store.get_document(document_id):
        raise HTTPException(status_code=404, detail="document not found")
    return store.list_jobs(document_id)


@app.get("/api/v1/knowledge/stats")
async def knowledge_stats(request: Request) -> dict[str, int]:
    return _state(request)["store"].document_stats()


@app.post("/api/v1/knowledge/documents/{document_id}/parse")
async def parse_document_endpoint(document_id: int, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Queue parsing/chunking only; vector indexing is an explicit next step."""
    store = _state(request)["store"]
    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    if document["status"] in {"processing", "indexing"}:
        raise HTTPException(status_code=409, detail="document is already processing")
    job_id = store.create_job(document_id, "parse")
    store.update_document_status(document_id, "processing", progress=0, current_stage="parse_queued", error_code="", error_message="", started_at=None, finished_at=None)
    background_tasks.add_task(_parse_document_job, store, document_id, job_id, document["path"])
    return {"status": "processing", "document_id": document_id, "job_id": job_id}


@app.post("/api/v1/knowledge/documents/{document_id}/prepare")
async def prepare_document(document_id: int, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Backward-compatible alias for the explicit parse action."""
    return await parse_document_endpoint(document_id, request, background_tasks)


@app.post("/api/v1/knowledge/documents/{document_id}/reparse")
async def reparse_document(document_id: int, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Rebuild parsed chunks while keeping the previous chunks until success."""
    store = _state(request)["store"]
    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    if document["status"] in {"processing", "indexing"}:
        raise HTTPException(status_code=409, detail="document is already processing")
    job_id = store.create_job(document_id, "reparse")
    store.update_document_status(document_id, "processing", progress=0, current_stage="parse_queued", error_code="", error_message="", started_at=None, finished_at=None)
    background_tasks.add_task(_parse_document_job, store, document_id, job_id, document["path"], True)
    return {"status": "processing", "document_id": document_id, "job_id": job_id}


@app.post("/api/v1/knowledge/documents/{document_id}/index")
async def index_document(document_id: int, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    store = _state(request)["store"]
    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    chunks = store.list_chunks(document_id)
    if document["status"] not in {"parsed", "indexed", "failed", "interrupted"} or not chunks:
        raise HTTPException(status_code=409, detail="document must be parsed successfully before indexing")
    job_id = store.create_job(document_id, "index")
    store.update_document_status(document_id, "indexing", progress=0, current_stage="index_queued", error_code="", error_message="", started_at=None, finished_at=None)
    background_tasks.add_task(_index_document_job, store, document_id, job_id)
    return {"status": "indexing", "document_id": document_id, "job_id": job_id}


@app.post("/api/v1/knowledge/documents/{document_id}/reindex")
async def reindex_document(document_id: int, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    return await index_document(document_id, request, background_tasks)


@app.post("/api/v1/knowledge/documents/{document_id}/retry")
async def retry_document(document_id: int, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    store = _state(request)["store"]
    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    if document["status"] in {"processing", "indexing"}:
        raise HTTPException(status_code=409, detail="document is already processing")
    if document["status"] in {"parsed", "indexed", "failed", "interrupted"} and store.list_chunks(document_id):
        return await index_document(document_id, request, background_tasks)
    return await parse_document_endpoint(document_id, request, background_tasks)


def _job_error(exc: Exception) -> tuple[str, str]:
    return (exc.__class__.__name__.lower(), str(exc)[:1000] or "unknown ingestion error")


def _parse_document_job(store: SQLiteStore, document_id: int, job_id: str, path: str, bump_version: bool = False) -> None:
    with _PARSE_LIMITER:
        _run_parse_document_job(store, document_id, job_id, path, bump_version)


def _run_parse_document_job(store: SQLiteStore, document_id: int, job_id: str, path: str, bump_version: bool = False) -> None:
    """Durable parse/chunk job. Parsed chunks are persisted before indexing."""
    try:
        from knowledge.parser import parse_document
        from knowledge.scripts.import_documents import document_to_chunks, infer_doc_type

        store.update_job(job_id, status="running", progress=10, stage="parsing", started_at=store._now())
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"原文件不存在: {source}")
        parsed = parse_document(source)
        store.update_job(job_id, progress=60, stage="chunking")
        chunks = document_to_chunks(parsed, infer_doc_type(source, source.parent.name), source.parent.name)
        document = store.get_document(document_id) or {}
        category = str(document.get("category") or "unclassified")
        for chunk in chunks:
            chunk.setdefault("metadata", {})["category"] = category
            chunk["metadata"]["source_type"] = document.get("source_type", "internal")
            chunk["metadata"]["authority_level"] = document.get("authority_level", "unknown")
            for key in ("source_url", "publisher", "license", "external_id", "corpus_id", "language", "published_at"):
                if document.get(key):
                    chunk["metadata"][key] = document[key]
            if category != "unclassified":
                chunk["metadata"]["type"] = category
        if not chunks:
            raise ValueError("文档解析后没有可用内容")
        if bump_version and (int(document.get("indexed_chunks") or 0) > 0 or document.get("status") == "indexed"):
            from knowledge.milvus_kb import delete_document_vectors
            delete_document_vectors(document_id)
        store.replace_chunks(document_id, chunks)
        store.update_job(job_id, status="completed", progress=100, stage="parsed", finished_at=store._now())
        changes: Dict[str, Any] = {"progress": 100, "current_stage": "parsed", "total_chunks": len(chunks), "indexed_chunks": 0, "error_code": "", "error_message": "", "finished_at": store._now()}
        if bump_version:
            changes["version"] = int(document.get("version") or 1) + 1
        store.update_document_status(document_id, "parsed", **changes)
    except Exception as exc:
        code, message = _job_error(exc)
        store.update_job(job_id, status="failed", progress=100, stage="parse_failed", error_code=code, error_message=message, finished_at=store._now())
        store.update_document_status(document_id, "failed", progress=100, current_stage="parse_failed", error_code=code, error_message=message, finished_at=store._now())


def _index_document_job(store: SQLiteStore, document_id: int, job_id: str) -> None:
    # One process-wide writer prevents duplicate model loads and Milvus Lite
    # file-lock contention when the UI submits a large batch.
    with _INDEX_JOB_LOCK:
        _run_index_document_job(store, document_id, job_id)


def _run_index_document_job(store: SQLiteStore, document_id: int, job_id: str) -> None:
    """Background embedding/index job; errors remain visible and retryable."""
    try:
        from knowledge.milvus_kb import MedicalKnowledgeBase
        from knowledge.chunking import DEFAULT_POLICY, TokenCounter, split_table_text, split_text
        from dataclasses import replace

        chunks = store.list_chunks(document_id)
        if not chunks:
            raise ValueError("没有可索引的解析块")
        store.update_job(job_id, status="running", progress=10, stage="embedding", started_at=store._now())
        kb = MedicalKnowledgeBase()
        tokenizer = getattr(kb.embedding_model, "tokenizer", None)
        model_limit = int(getattr(kb.embedding_model, "max_seq_length", 512) or 512)
        safe_limit = max(32, min(DEFAULT_POLICY.max_tokens, model_limit - 2))
        safe_policy = replace(
            DEFAULT_POLICY,
            max_tokens=safe_limit,
            overlap_tokens=min(DEFAULT_POLICY.overlap_tokens, max(0, safe_limit // 4)),
        )
        counter = TokenCounter(tokenizer=tokenizer)
        normalized_chunks = []
        changed = False
        for chunk in chunks:
            content = str(chunk.get("content") or "")
            metadata = dict(chunk.get("metadata") or {})
            if counter.count(content) <= safe_limit:
                normalized_chunks.append({"content": content, "metadata": metadata})
                continue
            splitter = split_table_text if metadata.get("is_table") else split_text
            parts = splitter(content, safe_policy, tokenizer=tokenizer)
            if not parts:
                raise ValueError(f"切片 {chunk['chunk_index']} 超限且无法重新切分")
            changed = True
            for part_index, part in enumerate(parts):
                if counter.count(part) > safe_limit:
                    raise ValueError(f"切片 {chunk['chunk_index']} 重新切分后仍超过 {safe_limit} tokens")
                part_metadata = dict(metadata)
                part_metadata.update({
                    "parent_chunk_index": int(chunk["chunk_index"]),
                    "embedding_rechunked": True,
                    "embedding_part": part_index,
                    "embedding_parts": len(parts),
                })
                normalized_chunks.append({"content": part, "metadata": part_metadata})
        if changed:
            store.replace_chunks(document_id, normalized_chunks)
            store.update_document(document_id, total_chunks=len(normalized_chunks), indexed_chunks=0)
            chunks = store.list_chunks(document_id)

        documents = []
        document_version = int((store.get_document(document_id) or {}).get("version") or 1)
        for chunk in chunks:
            metadata = dict(chunk.get("metadata") or {})
            metadata["document_record_id"] = document_id
            metadata["chunk_id"] = int(chunk["chunk_index"])
            metadata["version"] = document_version
            documents.append({"id": f"document-{document_id}-chunk-{chunk['chunk_index']}", "content": chunk["content"], "metadata": metadata})
        store.update_job(job_id, progress=40, stage="milvus_insert")
        # Auto-ID collections need an application-level cleanup before retry;
        # otherwise a partially failed job could create duplicate vectors.
        kb.delete_by_document_record_id(document_id)
        # document_to_chunks 已执行统一 token 上限和语义边界切分。
        count = kb.add_documents(documents, chunk_size=0)
        store.update_job(job_id, status="completed", progress=100, stage="indexed", finished_at=store._now())
        store.update_document_status(document_id, "indexed", progress=100, current_stage="indexed", indexed_chunks=count, error_code="", error_message="", finished_at=store._now())
    except Exception as exc:
        code, message = _job_error(exc)
        store.update_job(job_id, status="failed", progress=100, stage="index_failed", error_code=code, error_message=message, finished_at=store._now())
        store.update_document_status(document_id, "failed", progress=100, current_stage="index_failed", error_code=code, error_message=message, finished_at=store._now())


@app.delete("/api/v1/knowledge/documents/{document_id}")
async def delete_document_endpoint(document_id: int, request: Request) -> dict[str, Any]:
    """Delete original file, persisted chunks/jobs and matching Milvus vectors."""
    store = _state(request)["store"]
    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    if document["status"] in {"processing", "indexing"}:
        raise HTTPException(status_code=409, detail="processing documents cannot be deleted")
    if int(document.get("indexed_chunks") or 0) > 0 or document.get("status") == "indexed":
        try:
            from knowledge.milvus_kb import delete_document_vectors
            delete_document_vectors(document_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"向量删除失败，文档未删除: {exc}") from exc
    target = Path(document["path"]).resolve()
    upload_root = (PROJECT_ROOT / "data" / "uploads").resolve()
    if target.is_file() and upload_root in target.parents:
        target.unlink()
    store.delete_document(document_id)
    return {"status": "deleted", "document_id": document_id}


@app.post("/api/v1/knowledge/upload")
async def upload_document(request: Request, file: UploadFile = File(...), category: str = Form("unclassified"), source_type: str = Form("internal"), authority_level: str = Form("unknown")) -> dict[str, Any]:
    allowed = {".pdf", ".docx", ".xlsx", ".xlsm", ".txt", ".md"}
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(status_code=415, detail=f"不支持的文件类型: {suffix or 'unknown'}")
    upload_dir = PROJECT_ROOT / "data" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    target = upload_dir / safe_name
    digest = hashlib.sha256()
    size = 0
    with target.open("wb") as output:
        while True:
            block = file.file.read(1024 * 1024)
            if not block:
                break
            output.write(block)
            digest.update(block)
            size += len(block)
    store = _state(request)["store"]
    existing = store.find_document_by_sha256(digest.hexdigest())
    if existing:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail={"message": "相同文件已经存在", "document": existing})
    document = store.add_document(file.filename or safe_name, str(target), "uploaded", category=category, source_type=source_type, authority_level=authority_level, mime_type=file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream", size_bytes=size, sha256=digest.hexdigest())
    return {"status": "uploaded", "document": document, "message": "文件已进入待整理队列"}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    built = FRONTEND_ROOT / "dist" / "index.html"
    return FileResponse(built if built.exists() else FRONTEND_ROOT / "index.html")


@app.get("/assets/{file_path:path}", include_in_schema=False)
async def frontend_asset(file_path: str) -> FileResponse:
    asset = (FRONTEND_ROOT / "dist" / "assets" / file_path).resolve()
    assets_root = (FRONTEND_ROOT / "dist" / "assets").resolve()
    if assets_root not in asset.parents or not asset.is_file():
        raise HTTPException(status_code=404, detail="frontend asset not found")
    return FileResponse(asset)
