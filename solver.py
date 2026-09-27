import os
import re
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import arabic_reshaper
from bidi import get_display

from google import genai
from google.genai import types

from reportlab.lib.pagesizes import A4
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_MODEL = "gemini-3.8-flash"
MODEL = DEFAULT_MODEL

FONT_PATH = BASE_DIR / "NotoNaskhArabic-Regular.ttf"
KEY_FILE = BASE_DIR / "gemini_key.txt"
ENV_FILE = BASE_DIR / ".env"

FILE_POLL_INTERVAL_SECONDS = 0.5
FILE_PROCESSING_TIMEOUT_SECONDS = 300
MAX_CONTINUATION_REQUESTS = 5

if not FONT_PATH.exists():
    raise FileNotFoundError(f"Missing font: {FONT_PATH}")

pdfmetrics.registerFont(TTFont("ArabicFont", str(FONT_PATH)))


def _read_api_key():
    """يجلب مفتاح Gemini من متغير البيئة أو من ملف gemini_key.txt أو .env."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key and api_key.strip():
        return api_key.strip()

    for candidate in (KEY_FILE, ENV_FILE):
        if not candidate.exists():
            continue
        try:
            for raw_line in candidate.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.upper().startswith("GEMINI_API_KEY"):
                    _, _, value = line.partition("=")
                else:
                    value = line
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
        except OSError:
            continue

    return None


def _create_client():
    api_key = _read_api_key()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured.\n"
            f"Set the GEMINI_API_KEY environment variable, or put the key "
            f"inside this file:\n  {KEY_FILE}\n"
            "Get a key from https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=api_key)


def _wait_for_file_active(client, uploaded_file):
    if not uploaded_file.name:
        raise RuntimeError("Gemini uploaded a file without returning a file name.")

    deadline = time.monotonic() + FILE_PROCESSING_TIMEOUT_SECONDS
    while True:
        state = uploaded_file.state.name if uploaded_file.state else None
        if state == "ACTIVE":
            return uploaded_file
        if state == "FAILED":
            raise RuntimeError(
                f"Gemini could not process the uploaded file "
                f"(file state: {state})."
            )
        if state != "PROCESSING":
            raise RuntimeError(
                f"Gemini returned an unexpected uploaded file state: {state!r}."
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Timed out waiting for Gemini to finish processing the uploaded file."
            )

        time.sleep(FILE_POLL_INTERVAL_SECONDS)
        uploaded_file = client.files.get(name=uploaded_file.name)


def is_arabic(text: str) -> bool:
    """التحقق مما إذا كان السطر يحتوي على حروف عربية."""
    arabic_pattern = re.compile(r'[\u0600-\u06FF]')
    return bool(arabic_pattern.search(text))


def reshape_text(text: str) -> str:
    """إعادة تشكيل وتحذيذ النصوص العربية للطباعة الصحيحة في ReportLab."""
    if not text:
        return ""
    reshaped = arabic_reshaper.reshape(text)
    bidi_text = get_display(reshaped)
    return bidi_text


def _read_docx_content(docx_path):
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(docx_path) as document:
        try:
            root = ElementTree.fromstring(document.read("word/document.xml"))
        except KeyError as error:
            raise ValueError(
                f"{docx_path} is not a valid DOCX document."
            ) from error

        paragraphs = []
        for paragraph in root.iter(f"{namespace}p"):
            text = "".join(
                node.text or ""
                for node in paragraph.iter(f"{namespace}t")
            ).strip()
            if text:
                paragraphs.append(text)

        images = []
        for member in document.namelist():
            if not member.startswith("word/media/"):
                continue
            extension = Path(member).suffix.lower()
            mime_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }.get(extension)
            if mime_type:
                images.append((member, mime_type, document.read(member)))

    if not paragraphs and not images:
        raise ValueError(f"No readable content was found in {docx_path}.")
    return "\n".join(paragraphs), images


def _response_was_truncated(response):
    for candidate in response.candidates or []:
        finish_reason = candidate.finish_reason
        if getattr(finish_reason, "name", str(finish_reason)) == "MAX_TOKENS":
            return True
    return False


def solve_files(file_paths):
    file_paths = [Path(path) for path in file_paths]
    if not file_paths:
        raise ValueError("At least one assignment attachment is required.")

    client = _create_client()
    uploaded_files = []
    content_parts = []
    try:
        for file_path in file_paths:
            if not file_path.is_file():
                raise FileNotFoundError(
                    f"Assignment attachment was not found: {file_path}"
                )

            print(f"Preparing: {file_path}")
            if file_path.suffix.lower() == ".docx":
                document_text, embedded_images = _read_docx_content(file_path)
                content_parts.append(
                    f"\n--- Word document: {file_path.name} ---\n"
                    f"{document_text}"
                )
                for image_name, mime_type, image_data in embedded_images:
                    content_parts.append(
                        f"Embedded image from {file_path.name}: "
                        f"{Path(image_name).name}"
                    )
                    content_parts.append(types.Part.from_bytes(
                        data=image_data,
                        mime_type=mime_type
                    ))
                continue

            mime_type = {
                ".pdf": "application/pdf",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".doc": "application/msword",
            }.get(file_path.suffix.lower())
            if not mime_type:
                raise ValueError(
                    f"Unsupported assignment attachment type: {file_path.name}"
                )

            try:
                uploaded = client.files.upload(
                    file=str(file_path),
                    config=types.UploadFileConfig(mime_type=mime_type)
                )
                uploaded_files.append(uploaded)
                uploaded_files[-1] = _wait_for_file_active(client, uploaded)
            except Exception as error:
                raise RuntimeError(
                    f"Gemini could not prepare {file_path.name}: {error}"
                ) from error

        prompt = """
You are an expert AI academic tutor and assistant solving university and school assignments. 
The attached file(s) could include PDF documents, Microsoft Word files (.docx), Google Docs, images (PNG, JPG, JPEG, WEBP), scanned sheets, or text files. They may contain text, tables, images, diagrams, or hand-written notes across multiple pages or multiple separate files.

CRITICAL INSTRUCTIONS:
1. Thoroughly scan and read EVERY page, section, image, and table from all attached files from start to finish. Do not miss any corner, margin, or sub-question.
2. DO NOT SKIP ANY QUESTIONS. You must solve and answer EVERY SINGLE QUESTION present across all attachments, regardless of its type (theoretical/essay, mathematical, multiple choice, code analysis, or diagram interpretation).
3. LANGUAGE MATCHING: 
   - If the question is in Arabic, write both the question text and your detailed answer in Arabic.
   - If the question is in English, write both the question text and your detailed answer in English.
   - If it is mixed, maintain the respective language for each part.
4. FOR THEORETICAL / ESSAY / EXPLANATION QUESTIONS: Do not give short summaries or skip explanations. Provide a COMPLETE, DEEP, THOROUGH, and WELL-STRUCTURED scientific/academic explanation.
5. FOR MULTIPLE CHOICE QUESTIONS (MCQs): Clearly state the chosen option and provide a brief explanation of why it is correct.
6. FOR IMAGES / DIAGRAMS / TABLES: Extract all data, labels, values, or visual contexts embedded in the images/tables and solve the questions based on them.

Use this strict and clean output format for each question:

---
### Question [Number/Title]:
[Write out the exact question text found in the files here]

**Answer:**
[Provide the complete, detailed, step-by-step solution or comprehensive theoretical explanation here]
---

Process all attached files now and ensure 100% completion without missing a single question.
"""
        contents = [*uploaded_files, *content_parts, prompt]
        target_model = MODEL
        answer_parts = []

        for continuation_index in range(MAX_CONTINUATION_REQUESTS + 1):
            try:
                if continuation_index == 0:
                    request_contents = contents
                else:
                    request_contents = [
                        *contents,
                        "\n".join(answer_parts),
                        "Continue the answer from exactly where it stopped. "
                        "Do not repeat any completed questions or answers."
                    ]

                response = client.models.generate_content(
                    model=target_model,
                    contents=request_contents,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        max_output_tokens=8192
                    )
                )
            except Exception as error:
                raise RuntimeError(
                    f"Gemini content generation failed using model "
                    f"{target_model}: {error}"
                ) from error

            if not response.text:
                raise RuntimeError("Gemini returned no text for the assignment.")
            answer_parts.append(response.text)

            if not _response_was_truncated(response):
                return "\n".join(answer_parts)

            if continuation_index == MAX_CONTINUATION_REQUESTS:
                raise RuntimeError(
                    "Gemini output exceeded the configured continuation limit "
                    "and may be incomplete. Increase "
                    "MAX_CONTINUATION_REQUESTS to process this assignment."
                )
    finally:
        for uploaded in uploaded_files:
            if uploaded.name:
                try:
                    client.files.delete(name=uploaded.name)
                except Exception as error:
                    print(
                        f"Warning: could not delete Gemini upload "
                        f"{uploaded.name}: {error}"
                    )


def solve_pdf(pdf_path):
    """Backward-compatible wrapper for callers that solve a single PDF."""
    return solve_files([pdf_path])


def create_pdf(text, output_path):
    document = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=45,
        leftMargin=45,
        topMargin=50,
        bottomMargin=50
    )

    # نمط الخط العربي (محاذاة لليمين)
    style_rtl = ParagraphStyle(
        "ArabicBody",
        fontName="ArabicFont",
        fontSize=12,
        leading=20,
        spaceAfter=8,
        alignment=2  # Right Alignment
    )

    # نمط الخط الإنجليزي (محاذاة لليسار)
    style_ltr = ParagraphStyle(
        "EnglishBody",
        fontName="ArabicFont",
        fontSize=12,
        leading=20,
        spaceAfter=8,
        alignment=0  # Left Alignment
    )

    elements = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            elements.append(Spacer(1, 8))
            continue

        # تحديد لغة السطر وضبط المحاذاة والتشكيل
        if is_arabic(line):
            processed_line = reshape_text(line)
            current_style = style_rtl
        else:
            processed_line = line
            current_style = style_ltr

        # الهروب من الرموز الخاصة بـ XML لـ ReportLab
        processed_line = (
            processed_line.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        elements.append(Paragraph(processed_line, current_style))

    document.build(elements)
