import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import app
from backend.storage import SQLiteStore


class FakeCoordinator:
    async def process(self, question, context=None, session_id=None, enable_swarm=None, user_id="anonymous"):
        return {
            "answer": f"已收到：{question}",
            "swarm_enabled": False,
            "session_id": session_id,
            "suggestions": ["如有不适请咨询医生"],
            "disclaimer": "仅供参考",
        }


class ApiTests(unittest.TestCase):
    def setUp(self):
        app.state.coordinator = FakeCoordinator()
        app.state.store = SQLiteStore("/tmp/medix-test.db")
        self.client = TestClient(app)

    def test_health_and_frontend(self):
        self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_chat_and_session(self):
        response = self.client.post("/api/v1/chat", json={"message": "什么是高血压？"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "single_agent")
        session = self.client.get(f"/api/v1/sessions/{payload['session_id']}")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(len(session.json()["messages"]), 2)

    def test_stream_contains_workflow_events(self):
        response = self.client.post("/api/v1/chat/stream", json={"message": "流式测试"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: accepted", response.text)
        self.assertIn("event: completed", response.text)

    def test_upload_records_integrity_and_parse_chunks(self):
        content = f"P0 fixture {uuid.uuid4()}\n第二行".encode()
        response = self.client.post(
            "/api/v1/knowledge/upload",
            files={"file": ("p0.txt", content, "text/plain")},
        )
        self.assertEqual(response.status_code, 200)
        document = response.json()["document"]
        self.assertEqual(document["size_bytes"], len(content))
        self.assertEqual(len(document["sha256"]), 64)

        parse = self.client.post(f"/api/v1/knowledge/documents/{document['id']}/parse")
        self.assertEqual(parse.status_code, 200)
        stored = self.client.get(f"/api/v1/knowledge/documents/{document['id']}").json()
        self.assertEqual(stored["status"], "parsed")
        self.assertGreaterEqual(stored["total_chunks"], 1)
        self.assertTrue(self.client.get(f"/api/v1/knowledge/documents/{document['id']}/chunks").json())
        self.assertEqual(self.client.get(f"/api/v1/knowledge/documents/{document['id']}/file").status_code, 200)
        Path(document["path"]).unlink(missing_ok=True)

    def test_answer_artifacts_persist_with_session(self):
        class EvidenceCoordinator:
            async def process(self, question, context=None, session_id=None, enable_swarm=None, user_id="anonymous"):
                return {
                    "answer": "结论 [kb-demo]",
                    "swarm_enabled": False,
                    "suggestions": ["复核指南"],
                    "citations": [{"id": "kb-demo", "title": "本地指南", "type": "clinical_guideline", "citation_allowed": True}],
                    "evidence_trace": {"retrieved_count": 1},
                }

        app.state.coordinator = EvidenceCoordinator()
        response = self.client.post("/api/v1/chat", json={"message": "证据持久化测试"})
        session = self.client.get(f"/api/v1/sessions/{response.json()['session_id']}").json()
        metadata = session["messages"][-1]["metadata"]
        self.assertEqual(metadata["suggestions"], ["复核指南"])
        self.assertEqual(metadata["citations"][0]["id"], "kb-demo")
        self.assertTrue(metadata["evidence_trace"]["citation_validation"]["valid"])

    def test_session_rename_and_delete(self):
        response = self.client.post("/api/v1/chat", json={"message": "会话生命周期"})
        session_id = response.json()["session_id"]
        renamed = self.client.patch(f"/api/v1/sessions/{session_id}", json={"title": "高血压随访"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["title"], "高血压随访")
        self.assertEqual(self.client.delete(f"/api/v1/sessions/{session_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/sessions/{session_id}").status_code, 404)

    def test_document_metadata_reparse_search_and_delete(self):
        content = f"指南章节 {uuid.uuid4()}\n高血压管理建议".encode()
        uploaded = self.client.post(
            "/api/v1/knowledge/upload",
            files={"file": ("lifecycle.txt", content, "text/plain")},
            data={"category": "clinical_guideline"},
        ).json()["document"]
        document_id = uploaded["id"]
        patched = self.client.patch(
            f"/api/v1/knowledge/documents/{document_id}",
            json={"category": "research", "source_type": "internal", "authority_level": "high", "tags": ["高血压", "指南"]},
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["tags"], ["高血压", "指南"])
        self.assertEqual(self.client.post(f"/api/v1/knowledge/documents/{document_id}/prepare").status_code, 200)
        searched = self.client.get(f"/api/v1/knowledge/documents/{document_id}/chunks/search?q=高血压&page_size=5")
        self.assertEqual(searched.status_code, 200)
        self.assertGreaterEqual(searched.json()["total"], 1)
        self.assertEqual(self.client.post(f"/api/v1/knowledge/documents/{document_id}/reparse").status_code, 200)
        refreshed = self.client.get(f"/api/v1/knowledge/documents/{document_id}").json()
        self.assertEqual(refreshed["version"], 2)
        self.assertEqual(self.client.delete(f"/api/v1/knowledge/documents/{document_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/knowledge/documents/{document_id}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
