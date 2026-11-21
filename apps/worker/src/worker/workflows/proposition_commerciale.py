"""Generate a personalized commercial-proposal PDF for each new client row of a Google Sheet.

A scheduled sweep reads the clients sheet; for every row whose status column is empty it
renders a proposal from the Drive PDF template (placeholders replaced, personalized page
appended), uploads the PDF to Drive and writes the PDF link back into the status column.
"""

import html
import json
import os
from datetime import timedelta

from mistralai import workflows
from pydantic import BaseModel

SHEETS_API = "https://sheets.googleapis.com/v4"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DEFAULT_SHEET_ID = "12UiZsrbL-ooGGgeM88SxaZPxGjPkXBaWeiWl26zPHf0"
DEFAULT_SHEET_TAB = "Feuille 1"
DEFAULT_NAME_COLUMN = "Prénom"
DEFAULT_STATUS_COLUMN = "Proposition"
DEFAULT_TEMPLATE_FILE_ID = "1-Zeky8c0-sdZJdfa0SnOgaB--R08yWGU"
DEFAULT_OUTPUT_FOLDER_ID = "0AJP4oNWffCarUk9PVA"
DEFAULT_PLACEHOLDER_MAP = {
    "Prénom du client": "{name}",
    "DATE": "{date}",
    "CORRES": "{ref}",
}
DEFAULT_TEMPLATE_BRIEF = (
    "Modèle 'Proposition Commerciale.pdf' (2 pages, Canva) : page 1 'Proposition de Projet' "
    "avec 'PROPOSÉE PAR', 'PROPOSÉE À' et les champs 'NOM Prénom du client', 'DATE', 'CORRES' ; "
    "page 2 sections 'NOTRE COMPAGNIE' (LE MANOIR MICHEL TERRIER ET CIE, 12 rue de Lagny, "
    "93100 Montreuil), 'LE CLIENT', 'LE PROJET' et 'PROCESSUS DE PROJET' (01 CONCEPTS, "
    "02 ÉVALUER, 03 FINALISER, 04 OPTIONS)."
)
DEFAULT_OFFER_BRIEF = (
    "Proposition de projet personnalisée pour le client, en s'appuyant sur le modèle "
    "'Proposition Commerciale.pdf' et sur la commande indiquée dans la fiche client."
)


class ProposalWorkflowInput(BaseModel):
    dry_run: bool = False
    client_name: str | None = None


class ProposalWorkflowOutput(BaseModel):
    dry_run: bool
    pending_count: int
    processed: list["ProcessedClient"]


class EnsureStatusInput(BaseModel):
    pass


class StatusColumn(BaseModel):
    column_letter: str
    column_index: int


class ListPendingInput(BaseModel):
    status_column_letter: str
    status_column_index: int
    client_name: str | None = None


class ClientRow(BaseModel):
    row_number: int
    name: str
    fields: dict[str, str]


class PendingClients(BaseModel):
    clients: list[ClientRow]
    status_column_letter: str


class GenerateContentInput(BaseModel):
    client_fields: dict[str, str]
    run_date: str
    ref: str


class ProposalContent(BaseModel):
    titre: str
    presentation_client: str
    description_projet: str
    points_offre: list[str]
    prochaines_etapes: list[str]


class RenderInput(BaseModel):
    client_name: str
    client_fields: dict[str, str]
    run_date: str
    ref: str
    content: ProposalContent
    pdf_name: str
    dry_run: bool


class PdfUpload(BaseModel):
    file_id: str
    name: str
    web_view_link: str
    dry_run: bool


class MarkDoneInput(BaseModel):
    status_column_letter: str
    row_number: int
    value: str


class MarkDone(BaseModel):
    ok: bool


class ProcessedClient(BaseModel):
    row_number: int
    name: str
    ref: str
    pdf_name: str
    pdf_file_id: str
    pdf_link: str
    marked_done: bool
    dry_run: bool


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _column_letter(index: int) -> str:
    result = ""
    i = index
    while True:
        result = chr(ord("A") + i % 26) + result
        i = i // 26 - 1
        if i < 0:
            return result


def _pdf_name(client_name: str, run_date: str) -> str:
    cleaned = "".join(
        c if (c.isalnum() or c in " -_") else "" for c in client_name
    ).strip()
    cleaned = cleaned.replace(" ", "_") or "client"
    return f"Proposition_-_{cleaned}_-_{run_date}.pdf"


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


async def _access_token() -> str:
    import asyncio

    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get(
        "google_service_account_json"
    )
    if not raw:
        raise workflows.WorkflowError(
            "missing_env_google_service_account_json", non_retryable=True
        )
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=GOOGLE_SCOPES
    )
    await asyncio.to_thread(creds.refresh, Request())
    if not creds.token:
        raise workflows.WorkflowError("google_token_empty", non_retryable=True)
    return str(creds.token)


async def _google_request(
    method: str,
    url: str,
    token: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    files: dict | None = None,
    timeout: float = 60,
    allow_statuses: set[int] | None = None,
):
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json_body,
            files=files,
        )
    if resp.status_code >= 400 and resp.status_code not in (allow_statuses or set()):
        retryable = resp.status_code in {429, 500, 502, 503, 504}
        raise workflows.WorkflowError(
            f"google_http_{resp.status_code}", non_retryable=not retryable
        )
    return resp


def _quote_range(range_: str) -> str:
    from urllib.parse import quote

    return quote(range_, safe="")


async def _sheets_get_values(token: str, sheet_id: str, range_: str) -> dict:
    resp = await _google_request(
        "GET",
        f"{SHEETS_API}/spreadsheets/{sheet_id}/values/{_quote_range(range_)}",
        token,
        params={"majorDimension": "ROWS"},
    )
    return resp.json()


async def _sheets_update_value(
    token: str, sheet_id: str, range_: str, value: str
) -> dict:
    resp = await _google_request(
        "PUT",
        f"{SHEETS_API}/spreadsheets/{sheet_id}/values/{_quote_range(range_)}",
        token,
        params={"valueInputOption": "USER_ENTERED"},
        json_body={"range": range_, "majorDimension": "ROWS", "values": [[value]]},
    )
    return resp.json()


async def _drive_download(token: str, file_id: str) -> bytes:
    resp = await _google_request(
        "GET",
        f"{DRIVE_API}/files/{file_id}",
        token,
        params={"alt": "media"},
        timeout=120,
    )
    return resp.content


def _upload_files(name: str, data: bytes, parents: list[str] | None) -> dict:
    metadata: dict = {"name": name, "mimeType": "application/pdf"}
    if parents:
        metadata["parents"] = parents
    return {
        "metadata": (None, json.dumps(metadata), "application/json"),
        "file": (name, data, "application/pdf"),
    }


async def _drive_upload(token: str, folder_id: str, name: str, data: bytes) -> dict:
    # A service account can only create files inside a folder shared with it, so the
    # upload targets the output folder and a 403/404 means that share is missing.
    resp = await _google_request(
        "POST",
        f"{DRIVE_UPLOAD_API}/files",
        token,
        params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
        files=_upload_files(name, data, [folder_id] if folder_id else None),
        timeout=180,
        allow_statuses={403, 404},
    )
    if resp.status_code in {403, 404}:
        raise workflows.WorkflowError(
            "drive_upload_forbidden_share_output_folder_with_service_account",
            non_retryable=True,
        )
    return resp.json()


def _content_html(
    content: ProposalContent,
    client_name: str,
    client_fields: dict[str, str],
    run_date: str,
    ref: str,
) -> str:
    esc = html.escape
    mail = esc(client_fields.get("Mail", ""))
    visite = esc(client_fields.get("Dernière visite", ""))
    commande = esc(client_fields.get("Commande", ""))
    offre = "".join(f"<li>{esc(p)}</li>" for p in content.points_offre)
    etapes = "".join(f"<li>{esc(p)}</li>" for p in content.prochaines_etapes)
    return f"""<html><head><meta charset="utf-8"><style>
body {{ font-family: helvetica, sans-serif; font-size: 11pt; color: #1a1a1a; }}
h1 {{ font-size: 20pt; color: #e8590c; }}
h2 {{ font-size: 13pt; color: #e8590c; margin-bottom: 4pt; }}
p.meta {{ color: #666; font-size: 9pt; }}
</style></head><body>
<h1>{esc(content.titre)}</h1>
<p class="meta">Proposition de projet proposée à {esc(client_name)} le {esc(run_date)} — réf. {esc(ref)}</p>
<h2>Le client</h2>
<p>{esc(content.presentation_client)}</p>
<p class="meta">Mail : {mail} — dernière visite : {visite} — commande : {commande}</p>
<h2>Le projet</h2>
<p>{esc(content.description_projet)}</p>
<h2>Notre offre</h2>
<ul>{offre}</ul>
<h2>Prochaines étapes</h2>
<ol>{etapes}</ol>
</body></html>"""


def _build_personalized_pdf(
    template_bytes: bytes,
    replacements: dict[str, str],
    content: ProposalContent,
    client_name: str,
    client_fields: dict[str, str],
    run_date: str,
    ref: str,
) -> bytes:
    import pymupdf

    doc = pymupdf.open(stream=template_bytes, filetype="pdf")
    targets: dict[int, list[tuple]] = {}
    for page_index in range(doc.page_count):
        page = doc[page_index]
        for placeholder, value in replacements.items():
            for rect in page.search_for(placeholder):
                targets.setdefault(page_index, []).append((page, rect, value))
    for page_index, items in targets.items():
        page = doc[page_index]
        for _page, rect, _value in items:
            page.add_redact_annot(rect, fill=(1, 1, 1))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
        for _page, rect, value in items:
            box = pymupdf.Rect(rect.x0 - 2, rect.y0 - 1, rect.x1 + 160, rect.y1 + 2)
            fontsize = max(6.0, min(12.0, (rect.y1 - rect.y0) * 0.75))
            if page.insert_textbox(box, value, fontsize=fontsize, fontname="helv") < 0:
                page.insert_textbox(
                    box, value, fontsize=fontsize * 0.7, fontname="helv"
                )
    import io

    story = pymupdf.Story(
        html=_content_html(content, client_name, client_fields, run_date, ref)
    )
    buffer = io.BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    mediabox = pymupdf.paper_rect("a4")
    where = mediabox + 40
    more = 1
    while more:
        device = writer.begin_page(mediabox)
        more, _filled = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    content_doc = pymupdf.open(stream=buffer.getvalue(), filetype="pdf")
    doc.insert_pdf(content_doc)
    return doc.tobytes()


async def _generate_content(
    client_fields: dict[str, str], run_date: str, ref: str
) -> ProposalContent:
    from mistralai.workflows.plugins.mistralai import get_mistral_client

    model = _env("PROPOSAL_MODEL", "mistral-medium-latest")
    payload = {
        "client": client_fields,
        "date_proposition": run_date,
        "reference": ref,
        "modele": _env("TEMPLATE_BRIEF", DEFAULT_TEMPLATE_BRIEF),
        "offre": _env("OFFER_BRIEF", DEFAULT_OFFER_BRIEF),
    }
    system = (
        "Tu es un consultant commercial francophone. À partir des données d'un client "
        "et d'un modèle de proposition de projet, tu rédiges le contenu personnalisé de "
        "la proposition. Réponds UNIQUEMENT avec un objet JSON valide contenant les "
        "champs : titre (str), presentation_client (str), description_projet (str), "
        "points_offre (liste de str), prochaines_etapes (liste de str)."
    )
    client = get_mistral_client()
    resp = await client.chat.complete_async(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise workflows.WorkflowError("llm_invalid_json") from None
    return ProposalContent.model_validate(data)


@workflows.activity(
    name="pc_ensure_status_column",
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy_max_attempts=3,
)
async def ensure_status_column(_inp: EnsureStatusInput) -> StatusColumn:
    token = await _access_token()
    sheet_id = _env("CLIENTS_SHEET_ID", DEFAULT_SHEET_ID)
    tab = _env("CLIENTS_SHEET_TAB", DEFAULT_SHEET_TAB)
    status_name = _env("CLIENTS_STATUS_COLUMN", DEFAULT_STATUS_COLUMN)
    header = await _sheets_get_values(token, sheet_id, f"{tab}!A1:ZZ1")
    values = header.get("values") or [[]]
    headers = [str(h) for h in values[0]]
    if status_name in headers:
        index = headers.index(status_name)
    else:
        index = next((i for i, h in enumerate(headers) if not h.strip()), len(headers))
        await _sheets_update_value(
            token, sheet_id, f"{tab}!{_column_letter(index)}1", status_name
        )
    return StatusColumn(column_letter=_column_letter(index), column_index=index)


@workflows.activity(
    name="pc_list_pending_clients",
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy_max_attempts=3,
)
async def list_pending_clients(inp: ListPendingInput) -> PendingClients:
    token = await _access_token()
    sheet_id = _env("CLIENTS_SHEET_ID", DEFAULT_SHEET_ID)
    tab = _env("CLIENTS_SHEET_TAB", DEFAULT_SHEET_TAB)
    name_column = _env("CLIENTS_NAME_COLUMN", DEFAULT_NAME_COLUMN)
    last_col = _column_letter(max(inp.status_column_index, 25))
    data = await _sheets_get_values(token, sheet_id, f"{tab}!A1:{last_col}1000")
    rows = data.get("values") or []
    headers = [str(h) for h in rows[0]] if rows else []
    name_index = headers.index(name_column) if name_column in headers else 0
    clients: list[ClientRow] = []
    for row_number, row in enumerate(rows[1:], start=2):
        name = str(row[name_index]).strip() if name_index < len(row) else ""
        if not name:
            continue
        status = (
            str(row[inp.status_column_index]).strip()
            if inp.status_column_index < len(row)
            else ""
        )
        if status:
            continue
        if inp.client_name and inp.client_name.strip().lower() not in name.lower():
            continue
        width = max(len(headers), len(row))
        fields = {
            headers[i] if i < len(headers) and headers[i] else f"col_{i}": (
                str(row[i]) if i < len(row) else ""
            )
            for i in range(width)
        }
        clients.append(ClientRow(row_number=row_number, name=name, fields=fields))
    return PendingClients(
        clients=clients, status_column_letter=inp.status_column_letter
    )


@workflows.activity(
    name="pc_generate_proposal_content",
    start_to_close_timeout=timedelta(seconds=120),
    retry_policy_max_attempts=3,
)
async def generate_proposal_content(inp: GenerateContentInput) -> ProposalContent:
    return await _generate_content(inp.client_fields, inp.run_date, inp.ref)


@workflows.activity(
    name="pc_render_and_upload_pdf",
    start_to_close_timeout=timedelta(seconds=180),
    retry_policy_max_attempts=2,
)
async def render_and_upload_pdf(inp: RenderInput) -> PdfUpload:
    token = await _access_token()
    template_id = _env("TEMPLATE_FILE_ID", DEFAULT_TEMPLATE_FILE_ID)
    template_bytes = await _drive_download(token, template_id)
    try:
        mapping = json.loads(
            _env(
                "PLACEHOLDER_MAP_JSON",
                json.dumps(DEFAULT_PLACEHOLDER_MAP, ensure_ascii=False),
            )
        )
    except json.JSONDecodeError:
        mapping = DEFAULT_PLACEHOLDER_MAP
    values: dict[str, str] = {
        "name": inp.client_name,
        "mail": inp.client_fields.get("Mail", ""),
        "date": inp.run_date,
        "commande": inp.client_fields.get("Commande", ""),
        "ref": inp.ref,
    }
    replacements = {
        placeholder: template.format_map(_SafeDict(values))
        for placeholder, template in mapping.items()
    }
    pdf_bytes = _build_personalized_pdf(
        template_bytes,
        replacements,
        inp.content,
        inp.client_name,
        inp.client_fields,
        inp.run_date,
        inp.ref,
    )
    if inp.dry_run:
        return PdfUpload(file_id="", name=inp.pdf_name, web_view_link="", dry_run=True)
    folder_id = _env("OUTPUT_FOLDER_ID", DEFAULT_OUTPUT_FOLDER_ID)
    meta = await _drive_upload(token, folder_id, inp.pdf_name, pdf_bytes)
    return PdfUpload(
        file_id=meta.get("id", ""),
        name=meta.get("name", inp.pdf_name),
        web_view_link=meta.get("webViewLink", ""),
        dry_run=False,
    )


@workflows.activity(
    name="pc_mark_client_done",
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy_max_attempts=3,
)
async def mark_client_done(inp: MarkDoneInput) -> MarkDone:
    token = await _access_token()
    sheet_id = _env("CLIENTS_SHEET_ID", DEFAULT_SHEET_ID)
    tab = _env("CLIENTS_SHEET_TAB", DEFAULT_SHEET_TAB)
    await _sheets_update_value(
        token, sheet_id, f"{tab}!{inp.status_column_letter}{inp.row_number}", inp.value
    )
    return MarkDone(ok=True)


@workflows.workflow.define(name="proposition_commerciale")
class PropositionCommercialeWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, inp: ProposalWorkflowInput) -> ProposalWorkflowOutput:
        run_date = workflows.workflow.now().strftime("%Y-%m-%d")
        status = await ensure_status_column(EnsureStatusInput())
        pending = await list_pending_clients(
            ListPendingInput(
                status_column_letter=status.column_letter,
                status_column_index=status.column_index,
                client_name=inp.client_name,
            )
        )
        processed: list[ProcessedClient] = []
        for client in pending.clients:
            ref = f"{client.name}-{run_date}-row{client.row_number}"
            content = await generate_proposal_content(
                GenerateContentInput(
                    client_fields=client.fields, run_date=run_date, ref=ref
                )
            )
            pdf_name = _pdf_name(client.name, run_date)
            pdf = await render_and_upload_pdf(
                RenderInput(
                    client_name=client.name,
                    client_fields=client.fields,
                    run_date=run_date,
                    ref=ref,
                    content=content,
                    pdf_name=pdf_name,
                    dry_run=inp.dry_run,
                )
            )
            marked_done = False
            if not inp.dry_run:
                marked = await mark_client_done(
                    MarkDoneInput(
                        status_column_letter=status.column_letter,
                        row_number=client.row_number,
                        value=pdf.web_view_link or pdf.name,
                    )
                )
                marked_done = marked.ok
            processed.append(
                ProcessedClient(
                    row_number=client.row_number,
                    name=client.name,
                    ref=ref,
                    pdf_name=pdf.name,
                    pdf_file_id=pdf.file_id,
                    pdf_link=pdf.web_view_link,
                    marked_done=marked_done,
                    dry_run=inp.dry_run,
                )
            )
        return ProposalWorkflowOutput(
            dry_run=inp.dry_run, pending_count=len(pending.clients), processed=processed
        )
