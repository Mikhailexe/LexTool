import json
import re
import tempfile
import unicodedata
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials


GOOGLE_API_URL = "https://console.developers.google.com/apis/library"


def _describe_drive_error(error):
    """يحوّل أخطاء Google الصعبة إلى رسالة مفهومة مع رابط الحل."""
    message = str(error)

    if "accessNotConfigured" in message or (
        "has not been used in project" in message
    ):
        project_id = ""
        marker = "project "
        if marker in message:
            project_id = message.split(marker, 1)[1].split(" ", 1)[0]

        return RuntimeError(
            "Google Drive API is not enabled for this project.\n"
            f"Enable it here: {GOOGLE_API_URL}/drive.googleapis.com"
            f"?project={project_id}\n"
            "Then wait a minute and run the tool again."
        )

    if error.resp.status == 404:
        return RuntimeError(
            "Google Drive file not found (404). "
            "The file was deleted or the account has no access to it."
        )

    if error.resp.status == 403:
        return RuntimeError(
            "Google Drive refused the download (403). "
            "The OAuth token may be missing the drive.readonly scope. "
            "Delete token.json and run the tool to authorize again."
        )

    return error


SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

BASE_DIR = Path(__file__).resolve().parent

TOKEN_FILE = BASE_DIR / "token.json"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"


def _load_saved_credentials():
    if not TOKEN_FILE.exists():
        return None

    try:
        token_info = json.loads(
            TOKEN_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        print(
            f"Existing OAuth token could not be read ({error}); "
            "requesting authorization again. The existing file will be "
            "replaced only after authorization succeeds."
        )
        return None

    if not isinstance(token_info, dict):
        print(
            "Existing OAuth token has an invalid format; requesting "
            "authorization again. The existing file will be replaced only "
            "after authorization succeeds."
        )
        return None

    token_scopes = token_info.get("scopes", [])
    if isinstance(token_scopes, str):
        token_scopes = token_scopes.split()
    if not isinstance(token_scopes, list) or not all(
        isinstance(scope, str) for scope in token_scopes
    ):
        token_scopes = []

    missing_scopes = set(SCOPES).difference(token_scopes)
    if missing_scopes:
        print(
            "Existing OAuth token does not include the required scopes "
            f"({', '.join(sorted(missing_scopes))}); requesting authorization "
            "again. The existing file will be replaced only after "
            "authorization succeeds."
        )
        return None

    try:
        return Credentials.from_authorized_user_info(token_info)
    except ValueError as error:
        print(
            f"Existing OAuth token is invalid ({error}); requesting "
            "authorization again. The existing file will be replaced only "
            "after authorization succeeds."
        )
        return None


def get_credentials():
    creds = _load_saved_credentials()

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as error:
            print(
                f"Existing OAuth credentials could not be refreshed "
                f"({error}); requesting authorization again."
            )
            creds = None

    if creds and creds.valid:
        return creds

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        SCOPES
    )
    creds = flow.run_local_server(
        port=0,
        prompt="consent"
    )

    granted_scopes = creds.granted_scopes or creds.scopes or []
    if isinstance(granted_scopes, str):
        granted_scopes = granted_scopes.split()
    missing_scopes = set(SCOPES).difference(granted_scopes)
    if missing_scopes:
        raise RuntimeError(
            "Google did not grant all required OAuth scopes: "
            f"{', '.join(sorted(missing_scopes))}. "
            "Check the OAuth consent-screen configuration and authorize again."
        )

    TOKEN_FILE.write_text(
        creds.to_json(),
        encoding="utf-8"
    )

    return creds


def get_services():
    creds = get_credentials()

    classroom = build(
        "classroom",
        "v1",
        credentials=creds
    )

    drive = build(
        "drive",
        "v3",
        credentials=creds
    )

    return classroom, drive


def get_assignments(classroom):
    assignments = []
    courses_page_token = None

    while True:
        courses_request = classroom.courses().list(
            courseStates=["ACTIVE"],
            pageSize=100,
            pageToken=courses_page_token
        )
        courses_response = courses_request.execute()

        for course in courses_response.get("courses", []):
            course_id = course["id"]
            coursework_page_token = None

            while True:
                coursework_request = classroom.courses().courseWork().list(
                    courseId=course_id,
                    pageSize=100,
                    pageToken=coursework_page_token
                )
                coursework_response = coursework_request.execute()

                for assignment in coursework_response.get("courseWork", []):
                    assignments.append({
                        "course_id": course_id,
                        "course_name": course.get(
                            "name",
                            "Unknown course"
                        ),
                        "id": assignment["id"],
                        "title": assignment.get("title") or "Untitled",
                        "creation_time": assignment.get(
                            "creationTime"
                        ),
                        "materials": assignment.get(
                            "materials",
                            []
                        ),
                    })

                coursework_page_token = coursework_response.get(
                    "nextPageToken"
                )
                if not coursework_page_token:
                    break

        courses_page_token = courses_response.get("nextPageToken")
        if not courses_page_token:
            break

    return assignments


SUPPORTED_ATTACHMENT_TYPES = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/vnd.google-apps.document": ".pdf",
}

SUPPORTED_ATTACHMENT_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
}


def sanitize_filename(name, fallback="attachment"):
    """Keep readable Unicode filenames while removing unsafe Windows characters."""
    sanitized = "".join(
        "_" if character in '<>:"/\\|?*' or unicodedata.category(character) == "Cc"
        else character
        for character in str(name)
    ).strip(" .")

    if not sanitized:
        sanitized = fallback
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", sanitized, re.I):
        sanitized = f"_{sanitized}"

    if len(sanitized) > 200:
        path = Path(sanitized)
        suffix = path.suffix
        stem = path.stem
        sanitized = f"{stem[:200 - len(suffix)]}{suffix}"

    return sanitized


def get_supported_attachments(materials, drive):
    attachments = []
    for material in materials:
        drive_wrapper = material.get(
            "driveFile"
        )
        if not drive_wrapper:
            continue
        drive_file = drive_wrapper.get(
            "driveFile"
        )
        if not drive_file:
            continue

        file_id = drive_file.get("id")
        if not file_id:
            continue

        title = drive_file.get("title") or "attachment"
        mime_type = drive_file.get("mimeType")
        if not mime_type:
            metadata = drive.files().get(
                fileId=file_id,
                fields="id,name,mimeType",
                supportsAllDrives=True
            ).execute()
            mime_type = metadata.get("mimeType")
            title = metadata.get("name") or title

        mime_type = (mime_type or "").lower()
        extension = Path(title).suffix.lower()
        if mime_type not in SUPPORTED_ATTACHMENT_TYPES:
            mime_type = SUPPORTED_ATTACHMENT_EXTENSIONS.get(extension, "")
        if not mime_type:
            continue

        is_google_doc = mime_type == "application/vnd.google-apps.document"
        if is_google_doc:
            title = f"{Path(title).stem or 'document'}.pdf"
        else:
            expected_extension = SUPPORTED_ATTACHMENT_TYPES[mime_type]
            if extension != expected_extension and not (
                mime_type == "image/jpeg" and extension == ".jpeg"
            ):
                title = f"{Path(title).stem}{expected_extension}"

        attachments.append({
            "id": file_id,
            "title": sanitize_filename(title),
            "mimeType": mime_type,
            "exportMimeType": "application/pdf" if is_google_doc else None,
        })

    return attachments


def download_attachment(
    drive,
    attachment,
    output_path,
):
    output_path = Path(output_path)
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    file_id = attachment["id"]
    source_mime_type = attachment["mimeType"]
    export_mime_type = attachment.get("exportMimeType")
    if export_mime_type:
        request = drive.files().export_media(
            fileId=file_id,
            mimeType=export_mime_type
        )
        downloaded_mime_type = export_mime_type
    else:
        request = drive.files().get_media(
            fileId=file_id,
            supportsAllDrives=True
        )
        downloaded_mime_type = source_mime_type

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".download",
            delete=False
        ) as file:
            temporary_path = Path(file.name)
            downloader = MediaIoBaseDownload(
                file,
                request
            )

            done = False
            while not done:
                try:
                    status, done = downloader.next_chunk()
                except HttpError as error:
                    raise _describe_drive_error(error) from error

                if status:
                    percent = int(status.progress() * 100)
                    print(f"Downloading: {percent}%", flush=True)

        with temporary_path.open("rb") as file:
            header = file.read(1024)

        if not header:
            raise ValueError(
                f"Google Drive returned an empty file for attachment {file_id}."
            )

        if downloaded_mime_type == "application/pdf" and b"%PDF-" not in header:
            raise ValueError(
                f"Google Drive returned non-PDF content for attachment "
                f"{file_id}; the downloaded file was not saved."
            )

        temporary_path.replace(output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def find_pdf_material(materials):
    """Retained for compatibility with callers that only need a PDF."""
    attachments = []
    for material in materials:
        drive_wrapper = material.get("driveFile")
        drive_file = drive_wrapper.get("driveFile") if drive_wrapper else None
        if not drive_file:
            continue
        title = drive_file.get("title", "")
        mime_type = drive_file.get("mimeType")
        if mime_type == "application/pdf" or Path(title).suffix.lower() == ".pdf":
            attachments.append({
                "id": drive_file.get("id"),
                "title": title or "homework.pdf",
                "mimeType": "application/pdf",
            })
            break
    return attachments[0] if attachments else None


def download_pdf(drive, file_id, output_path):
    """Retained for compatibility with callers that download a single PDF."""
    return download_attachment(
        drive,
        {
            "id": file_id,
            "mimeType": "application/pdf",
        },
        output_path
    )