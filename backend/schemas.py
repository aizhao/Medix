from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)
    enable_swarm: bool = True


class Citation(BaseModel):
    id: str
    title: str
    type: str = "unknown"
    url: Optional[str] = None
    citation_allowed: bool = True
    status: str = "retrieved"
    excerpt: str = ""
    heading_path: Optional[str] = None
    document_record_id: Optional[int] = None
    chunk_id: Optional[int] = None
    page: Optional[int] = None
    retrieval_score: Optional[float] = None
    rerank_score: Optional[float] = None
    authority: Optional[float] = None
    content_fetched: Optional[bool] = None
    domain: Optional[str] = None


class ChatResponse(BaseModel):
    request_id: str
    session_id: str
    answer: str
    mode: str
    agents_involved: List[str] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)
    evidence_trace: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
    disclaimer: str
    latency_ms: int


class SessionMessage(BaseModel):
    role: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


class SessionResponse(BaseModel):
    session_id: str
    messages: List[SessionMessage] = Field(default_factory=list)
