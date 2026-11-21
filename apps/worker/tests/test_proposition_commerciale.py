"""Tests for the proposition_commerciale workflow; all external effects are mocked."""

import asyncio
from datetime import timedelta

import pymupdf
import pytest
from mistralai.workflows.testing import create_test_worker
from worker.workflows import proposition_commerciale as pc

WORKFLOW_EXECUTION_TIMEOUT = timedelta(seconds=30)

HEADER = ["Prénom", "Mail", "Dernière visite", "Commande", "Proposition"]


def _make_template() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "Proposition de Projet")
    page.insert_text((50, 150), "PROPOSÉE À NOM Prénom du client")
    page.insert_text((50, 170), "DATE")
    page.insert_text((50, 190), "CORRES")
    return doc.tobytes()


def _content() -> pc.ProposalContent:
    return pc.ProposalContent(
        titre="Projet Pizza pour Jean",
        presentation_client="Jean est un client fidèle.",
        description_projet="Projet de pizzeria personnalisé.",
        points_offre=["Site web", "Marketing"],
        prochaines_etapes=["Appel de cadrage", "Envoi du devis"],
    )


class _FakeGoogle:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows
        self.updates: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, bytes]] = []

    async def access_token(self) -> str:
        return "test-token"

    async def get_values(self, token: str, sheet_id: str, range_: str) -> dict:
        return {"values": [HEADER, *self.rows]}

    async def update_value(
        self, token: str, sheet_id: str, range_: str, value: str
    ) -> dict:
        self.updates.append((range_, value))
        return {"updatedCells": 1}

    async def drive_download(self, token: str, file_id: str) -> bytes:
        return _make_template()

    async def drive_upload(
        self, token: str, folder_id: str, name: str, data: bytes
    ) -> dict:
        self.uploads.append((name, data))
        return {
            "id": "file-123",
            "name": name,
            "webViewLink": "https://drive.google.com/file/d/file-123/view",
        }

    async def generate_content(
        self, client_fields: dict[str, str], run_date: str, ref: str
    ) -> pc.ProposalContent:
        return _content()


@pytest.fixture
def fake_google(monkeypatch: pytest.MonkeyPatch) -> _FakeGoogle:
    fake = _FakeGoogle(
        rows=[
            ["Jean", "jean@example.com", "2026-09-30", "Pizza", ""],
            ["Paul", "paul@example.com", "2026-10-24", "Pâtes", "https://deja/la"],
        ]
    )
    monkeypatch.setattr(pc, "_access_token", fake.access_token)
    monkeypatch.setattr(pc, "_sheets_get_values", fake.get_values)
    monkeypatch.setattr(pc, "_sheets_update_value", fake.update_value)
    monkeypatch.setattr(pc, "_drive_download", fake.drive_download)
    monkeypatch.setattr(pc, "_drive_upload", fake.drive_upload)
    monkeypatch.setattr(pc, "_generate_content", fake.generate_content)
    return fake


async def _run_workflow(temporal_env, workflow_input: dict) -> dict:
    async with create_test_worker(
        temporal_env,
        workflows=[pc.PropositionCommercialeWorkflow],
        activities=[
            pc.ensure_status_column,
            pc.list_pending_clients,
            pc.generate_proposal_content,
            pc.render_and_upload_pdf,
            pc.mark_client_done,
        ],
    ):
        handle = await temporal_env.client.start_workflow(
            "proposition_commerciale",
            workflow_input,
            id=f"test-{workflow_input.get('dry_run')}-{workflow_input.get('client_name')}",
            task_queue="test-task-queue",
            execution_timeout=WORKFLOW_EXECUTION_TIMEOUT,
        )
        return await asyncio.wait_for(handle.result(), timeout=45)


async def test_workflow_processes_only_pending_clients(
    temporal_env, fake_google: _FakeGoogle
) -> None:
    result = await _run_workflow(temporal_env, {"dry_run": False})
    assert result["pending_count"] == 1
    assert len(result["processed"]) == 1
    done = result["processed"][0]
    assert done["name"] == "Jean"
    assert done["row_number"] == 2
    assert done["marked_done"] is True
    assert done["pdf_file_id"] == "file-123"
    assert done["pdf_link"].endswith("file-123/view")
    assert len(fake_google.uploads) == 1
    name, data = fake_google.uploads[0]
    assert "Jean" in name and name.endswith(".pdf")
    assert data.startswith(b"%PDF")
    assert ("Feuille 1!E2", done["pdf_link"]) in fake_google.updates


async def test_workflow_dry_run_writes_nothing(
    temporal_env, fake_google: _FakeGoogle
) -> None:
    result = await _run_workflow(temporal_env, {"dry_run": True})
    assert result["dry_run"] is True
    assert result["processed"][0]["dry_run"] is True
    assert result["processed"][0]["marked_done"] is False
    assert fake_google.uploads == []
    assert fake_google.updates == []


async def test_workflow_filters_by_client_name(
    temporal_env, fake_google: _FakeGoogle
) -> None:
    result = await _run_workflow(
        temporal_env, {"dry_run": False, "client_name": "paul"}
    )
    assert result["pending_count"] == 0
    assert result["processed"] == []
    assert fake_google.uploads == []


async def test_build_personalized_pdf_replaces_placeholders_and_appends_page() -> None:
    replacements = {
        "Prénom du client": "Jean",
        "DATE": "2026-11-21",
        "CORRES": "Jean-2026-11-21-row2",
    }
    pdf_bytes = pc._build_personalized_pdf(
        _make_template(),
        replacements,
        _content(),
        "Jean",
        {"Mail": "jean@example.com", "Commande": "Pizza"},
        "2026-11-21",
        "Jean-2026-11-21-row2",
    )
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    assert doc.page_count == 2
    text_page1 = doc[0].get_text()
    assert "Jean" in text_page1
    assert "Prénom du client" not in text_page1
    assert "2026-11-21" in text_page1
    assert "Jean-2026-11-21-row2" in text_page1
    text_page2 = doc[1].get_text()
    assert "Projet Pizza pour Jean" in text_page2
    assert "Site web" in text_page2


def test_column_letter() -> None:
    assert pc._column_letter(0) == "A"
    assert pc._column_letter(4) == "E"
    assert pc._column_letter(25) == "Z"
    assert pc._column_letter(26) == "AA"


def test_pdf_name_sanitizes_client_name() -> None:
    assert (
        pc._pdf_name("Jean Dupont", "2026-11-21")
        == "Proposition_-_Jean_Dupont_-_2026-11-21.pdf"
    )
    assert pc._pdf_name("", "2026-11-21") == "Proposition_-_client_-_2026-11-21.pdf"


async def test_drive_upload_falls_back_when_folder_not_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    class _Resp:
        def __init__(self, status: int, payload: dict) -> None:
            self.status_code = status
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    async def fake_request(method: str, url: str, token: str, **kw) -> _Resp:
        calls.append((url, kw))
        files = kw.get("files") or {}
        metadata = files.get("metadata")
        if metadata and "parents" in metadata[1]:
            return _Resp(404, {})
        if "/permissions" in url:
            return _Resp(200, {"id": "perm-1"})
        return _Resp(
            200, {"id": "f-1", "name": "n.pdf", "webViewLink": "https://x/f-1"}
        )

    monkeypatch.setattr(pc, "_google_request", fake_request)
    monkeypatch.setenv("DRIVE_SHARE_EMAIL", "owner@example.com")
    meta = await pc._drive_upload("tok", "folder-1", "n.pdf", b"%PDF-1.4")
    assert meta["id"] == "f-1"
    uploads = [c for c in calls if c[0].endswith("/files")]
    grants = [c for c in calls if "/permissions" in c[0]]
    assert len(uploads) == 2 and len(grants) == 1
