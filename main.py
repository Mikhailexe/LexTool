import json
import sys

# إجبار التيرمينال على استخدام ترميز UTF-8 لدعم اللغة العربية والرموز
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

from pathlib import Path

from classroom import (
    get_services,
    get_assignments,
    get_supported_attachments,
    download_attachment,
    sanitize_filename
)

from solver import (
    solve_files,
    create_pdf
)

import solver as solver_module


BASE_DIR = Path(__file__).resolve().parent

STATE_FILE = BASE_DIR / "state.json"

INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"


def say(message=""):
    """طباعة فورية حتى لا يختفي الإخراج أثناء الحفظ في ملف."""
    print(message, flush=True)


def load_state():

    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return {}


def save_state(state):

    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


def process_assignment(
    assignment,
    drive,
    state
):

    assignment_id = assignment["id"]

    title = assignment["title"]
    safe_title = sanitize_filename(title, fallback="Untitled")
    safe_assignment_id = sanitize_filename(assignment_id, fallback="assignment")
    assignment_input_dir = INPUT_DIR / f"{safe_title} - {safe_assignment_id}"
    output_path = OUTPUT_DIR / f"{safe_title}.pdf"

    # Reprocess legacy state entries whose output was saved under an old name.
    if (
        assignment_id in state
        and state[assignment_id].get(
            "status"
        ) == "ready"
        and output_path.is_file()
    ):
        return

    say()
    say("=" * 50)
    say(f"New assignment: {title}")
    say("=" * 50)

    attachments = get_supported_attachments(
        assignment["materials"],
        drive
    )
    if not attachments:
        say(
            "No supported attachments found."
        )

        state[assignment_id] = {
            "status": "no_supported_attachments",
            "title": title
        }

        save_state(state)

        return

    # Prefix each attachment with the assignment title and sequence number.
    input_paths = []
    used_names = set()
    for index, attachment in enumerate(attachments, start=1):
        attachment_name = sanitize_filename(
            attachment["title"],
            fallback=f"attachment_{index}"
        )
        candidate_name = sanitize_filename(
            f"{safe_title} - {index:02d} - {attachment_name}"
        )
        suffix = 2
        while candidate_name.casefold() in used_names:
            candidate_path = Path(attachment_name)
            candidate_name = (
                f"{safe_title} - {index:02d} - "
                f"{candidate_path.stem}_{suffix}{candidate_path.suffix}"
            )
            candidate_name = sanitize_filename(candidate_name)
            suffix += 1
        used_names.add(candidate_name.casefold())
        input_path = assignment_input_dir / candidate_name

        if not input_path.exists():
            say(f"Downloading attachment {index}/{len(attachments)}: {candidate_name}")
            try:
                download_attachment(
                    drive,
                    attachment,
                    input_path
                )
            except Exception as error:
                say(f"Download failed for {candidate_name}: {error}")
                state[assignment_id] = {
                    "status": "download_failed",
                    "title": title,
                    "input": [str(path) for path in input_paths],
                    "failed_attachment": candidate_name,
                    "error": str(error)
                }
                save_state(state)
                return
        else:
            say(f"Attachment already downloaded: {candidate_name}")

        input_paths.append(input_path)

    # Solve
    say(
        f"Sending {len(input_paths)} attachment(s) to Gemini..."
    )

    try:
        solved_text = solve_files(
            input_paths
        )
    except Exception as error:

        say(f"Solving failed: {error}")

        state[assignment_id] = {
            "status": "solve_failed",
            "title": title,
            "input": [str(path) for path in input_paths],
            "error": str(error)
        }

        save_state(state)

        return

    # Output
    say(
        "Creating solved PDF..."
    )

    try:
        create_pdf(
            solved_text,
            output_path
        )
    except Exception as error:

        say(f"PDF creation failed: {error}")

        state[assignment_id] = {
            "status": "pdf_failed",
            "title": title,
            "input": [str(path) for path in input_paths],
            "error": str(error)
        }

        save_state(state)

        return

    # Save state
    state[assignment_id] = {
        "status": "ready",
        "title": title,
        "input": [str(path) for path in input_paths],
        "output": str(output_path)
    }

    save_state(state)

    say()
    say("HOMEWORK READY")
    say(
        f"Assignment: {title}"
    )
    say(
        f"File: {output_path}"
    )
    say()


def main():

    INPUT_DIR.mkdir(
        exist_ok=True
    )

    OUTPUT_DIR.mkdir(
        exist_ok=True
    )

    say(
        "Starting Google Classroom Solver..."
    )

    # فحص مبكر لمفتاح Gemini قبل الاتصال بـ Google
    if not solver_module._read_api_key():
        say()
        say("=" * 50)
        say("MISSING GEMINI_API_KEY")
        say("=" * 50)
        say(
            f"Paste your key into: {solver_module.KEY_FILE}"
        )
        say(
            "Get a free key from: https://aistudio.google.com/apikey"
        )
        say(
            "Then run the tool again."
        )
        say()
        return

    say(
        f"Gemini model: {solver_module.MODEL}"
    )

    classroom, drive = get_services()

    state = load_state()

    say(
        "Connected to Google Classroom."
    )

    say("Checking continuously; assignments are processed as soon as they are found.")

    attempted_assignments = set()

    while True:

        try:

            assignments = get_assignments(
                classroom
            )

            say(
                f"Found {len(assignments)} assignments."
            )

            for assignment in assignments:
                if assignment["id"] in attempted_assignments:
                    continue

                # عزل الأخطاء: فشل واجب واحد لا يوقف باقي الواجبات
                try:
                    process_assignment(
                        assignment,
                        drive,
                        state
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as error:
                    say(f"Skipped assignment: {error!r}")
                finally:
                    attempted_assignments.add(assignment["id"])

        except KeyboardInterrupt:

            say(
                "\nStopped."
            )

            break

        except Exception as error:

            say(
                "\nERROR:"
            )

            say(
                repr(error)
            )

if __name__ == "__main__":
    main()