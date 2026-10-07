# Declaration: Code generated using Anthropic Claude (Sonnet 5.5)
import argparse
import csv
import hashlib
import json
import os
import shlex
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import ollama
from pdf2image import convert_from_path, pdfinfo_from_path
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

DEFAULT_MODEL = "gemma4:26b-a4b-it-q4_K_M"
DEFAULT_OCR_FOLDER = Path.home() / "Documents" / "OCR"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
DIRECT_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
DEFAULT_OPTIONS = {
    "temperature": 0.2,
    "repeat_penalty": 1.3,
    "num_predict": 4096,
    "num_ctx": 8192,
}
MANIFEST_COLUMNS = [
    "source_file", "page", "image_path", "txt_path", "model", "model_digest",
    "prompt_type", "language", "prompt_hash", "options", "status", "chars",
    "seconds", "timestamp", "error",
]

NO_TEXT_INSTRUCTION = (
    "If the image contains no legible text at all - for example a blank or "
    "blurred surface, or a photograph of an object, wall, desk, or person "
    "with no writing on it - respond with exactly: [no text identified]. "
    "Do not invent, guess, or hallucinate any transcription in that case."
)

PROMPTS = {
    "auto": (
        "Transcribe all text visible in this image of a historical document "
        "exactly as it appears. Include both printed and handwritten text if "
        "present. Preserve the original line breaks. Do not add any "
        "commentary, headings, markdown formatting, or summary. If a word or "
        "passage is illegible, write [illegible] in its place. Output only "
        "the transcription. " + NO_TEXT_INSTRUCTION
    ),
    "printed": (
        "Transcribe the printed text visible in this image of a historical "
        "document exactly as it appears, preserving the original line "
        "breaks. Do not add any commentary, headings, markdown formatting, "
        "or summary. If a word or passage is illegible, write [illegible] in "
        "its place. Output only the transcription. " + NO_TEXT_INSTRUCTION
    ),
    "handwriting": (
        "Transcribe the handwritten text visible in this image of a "
        "historical document as accurately as possible, preserving the "
        "original line breaks. Do your best even where the handwriting is "
        "difficult to read; if a word is uncertain, give your best guess "
        "followed by [?], and use [illegible] where no reasonable guess is "
        "possible. Do not add any commentary, headings, markdown "
        "formatting, or summary. Output only the transcription. "
        + NO_TEXT_INSTRUCTION
    ),
}

state = {
    "log_file": None,
    "manifest_file": None,
    "base_row": {},
    "total": 0,
    "position": 0,
    "timed_pages": 0,
    "timed_seconds": 0.0,
    "ok": 0,
    "warning": 0,
    "error": 0,
    "skipped": 0,
    "errors_in_a_row": 0,
    "disable_thinking": False,
}


def log(message):
    line = f"{datetime.now().isoformat(timespec='seconds')}  {message}"
    print(line, flush=True)
    with open(state["log_file"], "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_text(path, text):
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def add_manifest_row(**fields):
    row = dict(state["base_row"])
    row["timestamp"] = datetime.now().isoformat(timespec="seconds")
    row.update(fields)
    manifest_file = state["manifest_file"]
    is_new = not manifest_file.exists()
    with open(manifest_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def parse_options(option_args):
    options = dict(DEFAULT_OPTIONS)
    for item in option_args:
        if "=" not in item:
            sys.exit(f"--option must look like key=value, got: {item}")
        key, value = item.split("=", 1)
        try:
            options[key] = json.loads(value)
        except json.JSONDecodeError:
            options[key] = value
    return options


def find_files(input_path, output_root):
    if input_path.is_file():
        return [input_path]
    files = []
    for path in sorted(input_path.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if output_root in path.parents:
            continue
        if path.suffix.lower() == ".pdf" or path.suffix.lower() in IMAGE_EXTENSIONS:
            files.append(path)
    return files


def place_job(source, base_dir, output_dir, single_file, stem_clash):
    rel_dir = source.parent.relative_to(base_dir)
    out_dir = output_dir / rel_dir
    job = {"source": source}
    if source.suffix.lower() == ".pdf":
        job["folder"] = output_dir if single_file else out_dir / source.stem
    else:
        job["folder"] = out_dir
        if stem_clash:
            txt_name = f"{source.stem}_{source.suffix[1:].lower()}"
        else:
            txt_name = source.stem
        job["txt_path"] = out_dir / f"{txt_name}.txt"
        job["converted"] = output_dir / "converted" / rel_dir / f"{txt_name}.png"
    job["copy"] = job["folder"] / source.name
    return job


def copy_original(job):
    source, target = job["source"], job["copy"]
    job["folder"].mkdir(parents=True, exist_ok=True)
    if target.exists() or target.resolve() == source.resolve():
        return
    tmp_path = target.with_name(target.name + ".tmp")
    shutil.copy2(source, tmp_path)
    os.replace(tmp_path, target)


def save_png(image, path, max_side):
    image = image.convert("RGB")
    if max_side:
        image.thumbnail((max_side, max_side))
    tmp_path = path.with_name(path.name + ".tmp")
    image.save(tmp_path, format="PNG")
    os.replace(tmp_path, path)


def get_page_image(job, page, args):
    source = job["copy"]
    if source.suffix.lower() == ".pdf":
        png_path = job["folder"] / f"page_{page:04d}.png"
        if not png_path.exists():
            rendered = convert_from_path(
                str(source), dpi=args.dpi, first_page=page, last_page=page
            )
            save_png(rendered[0], png_path, args.max_side)
        return png_path
    if source.suffix.lower() in DIRECT_IMAGE_EXTENSIONS and not args.max_side:
        return source
    png_path = job["converted"]
    if not png_path.exists():
        png_path.parent.mkdir(parents=True, exist_ok=True)
        save_png(Image.open(source), png_path, args.max_side)
    return png_path


def transcribe(image_path, prompt, args, options):
    last_error = ""
    for attempt in range(args.retries + 1):
        try:
            chat_args = {
                "model": args.model,
                "messages": [
                    {"role": "user", "content": prompt, "images": [str(image_path)]}
                ],
                "options": options,
            }
            if args.keep_alive:
                chat_args["keep_alive"] = args.keep_alive
            if state["disable_thinking"]:
                chat_args["think"] = False
            chat_args["stream"] = True
            stream = ollama.chat(**chat_args)
            parts = []
            thinking_chunks = 0
            done_reason = None
            for chunk in stream:
                if chunk.message.content:
                    parts.append(chunk.message.content)
                if chunk.message.thinking:
                    thinking_chunks += 1
                    if args.max_think_tokens and not parts and thinking_chunks > args.max_think_tokens:
                        stream.close()
                        return None, None, (
                            f"model used {thinking_chunks} thinking tokens without writing a transcript "
                            "- try an -instruct version of the model"
                        )
                if chunk.done:
                    done_reason = chunk.done_reason
            return "".join(parts).strip(), done_reason, ""
        except Exception as error:
            last_error = str(error)
            log(f"  attempt {attempt + 1} failed: {last_error}")
            if attempt < args.retries:
                time.sleep(2)
    return None, None, last_error


def progress_line(job, page, seconds):
    state["timed_pages"] += 1
    state["timed_seconds"] += seconds
    average = state["timed_seconds"] / state["timed_pages"]
    remaining = state["total"] - state["position"]
    eta = timedelta(seconds=int(average * remaining))
    log(
        f"[{state['position']}/{state['total']}] {job['source'].name} page {page}"
        f" - {seconds:.1f}s - avg {average:.1f}s/page - ETA {eta}"
    )


def process_job(job, prompt, options, args):
    is_pdf = job["source"].suffix.lower() == ".pdf"
    copy_original(job)
    skipped_here = 0
    try:
        for page in range(1, job["pages"] + 1):
            state["position"] += 1
            if is_pdf:
                txt_path = job["folder"] / f"page_{page:04d}.txt"
            else:
                txt_path = job["txt_path"]
            if txt_path.exists() and not args.overwrite:
                state["skipped"] += 1
                skipped_here += 1
                continue
            started = time.time()
            image_path = None
            try:
                image_path = get_page_image(job, page, args)
                text, done_reason, error = transcribe(image_path, prompt, args, options)
            except Exception as problem:
                text, done_reason, error = None, None, f"image preparation failed: {problem}"
            if text == "":
                error = "empty output - model returned no text"
                if done_reason == "length":
                    error += " (hit num_predict limit)"
                if state["disable_thinking"]:
                    error += " - thinking model; try an -instruct version or a larger num_predict"
                text = None
            seconds = time.time() - started
            row = {
                "source_file": job["source"].name,
                "page": page,
                "image_path": str(image_path) if image_path else "",
                "txt_path": "",
                "seconds": round(seconds, 1),
            }
            if text is None:
                state["error"] += 1
                state["errors_in_a_row"] += 1
                add_manifest_row(status="error", chars=0, error=error, **row)
                log(f"  ERROR on {job['source'].name} page {page}: {error}")
            else:
                state["errors_in_a_row"] = 0
                write_text(txt_path, text)
                status = "ok"
                note = ""
                if len(text) < 3:
                    status, note = "warning", "empty or very short output"
                elif text.lower().rstrip(". ") == "[no text identified]":
                    status, note = "warning", "model reports no text on page"
                elif "[no text identified]" in text.lower():
                    status = "warning"
                    note = "transcript contains a stray [no text identified] marker alongside text"
                if done_reason == "length":
                    status = "warning"
                    note = "hit num_predict limit - transcript may be truncated"
                state[status] += 1
                row["txt_path"] = str(txt_path)
                add_manifest_row(status=status, chars=len(text), error=note, **row)
                if status == "warning":
                    log(f"  WARNING {job['source'].name} page {page}: {note}")
            progress_line(job, page, seconds)
            if args.max_errors and state["errors_in_a_row"] >= args.max_errors:
                log(f"Stopping: {state['errors_in_a_row']} errors in a row. Check the errors above and that Ollama is running, then run the same command again to resume.")
                return True
        return False
    finally:
        if skipped_here:
            log(f"{job['source'].name}: skipped {skipped_here} page(s) already transcribed")


def main():
    parser = argparse.ArgumentParser(description="OCR PDFs and images with a local Ollama vision model.")
    parser.add_argument("input", help="A PDF, an image, or a folder containing them")
    parser.add_argument("--output", help=f"Project folder for the results (default: {DEFAULT_OCR_FOLDER}/<input name>)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--document-type", choices=list(PROMPTS), default="auto")
    parser.add_argument("--prompt", help="Custom prompt text (replaces the preset)")
    parser.add_argument("--prompt-file", help="Text file containing a custom prompt")
    parser.add_argument("--language", help="Language of the text, e.g. 'early modern Latin'")
    parser.add_argument("--option", action="append", default=[], help="Ollama option as key=value (repeatable)")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--max-side", type=int, default=None, help="Shrink images so the longest side is this many pixels")
    parser.add_argument("--keep-alive", default=None, help="How long Ollama keeps the model loaded, e.g. 30m")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-think-tokens", type=int, default=300, help="Give up on a page if a thinking model produces this many thinking tokens and no transcript (0 = never give up)")
    parser.add_argument("--max-errors", type=int, default=5, help="Stop the run after this many errors in a row (0 = never stop)")
    parser.add_argument("--overwrite", action="store_true", help="Redo pages that already have a transcript")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        sys.exit(f"Not found: {input_path}")
    base_dir = input_path if input_path.is_dir() else input_path.parent
    if args.output:
        output_dir = Path(args.output).expanduser().resolve()
    else:
        output_dir = DEFAULT_OCR_FOLDER / (input_path.name if input_path.is_dir() else input_path.stem)
    (output_dir / "logs").mkdir(parents=True, exist_ok=True)
    state["log_file"] = output_dir / "logs" / f"run_log_{datetime.now():%Y-%m-%d_%H%M%S}.txt"
    state["manifest_file"] = output_dir / "manifest.csv"

    if args.prompt_file:
        base_prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8").strip()
        prompt_type = "custom"
    elif args.prompt:
        base_prompt = args.prompt.strip()
        prompt_type = "custom"
    else:
        base_prompt = PROMPTS[args.document_type]
        prompt_type = args.document_type
    prompt = base_prompt
    if args.language:
        prompt = f"The text in this image is in {args.language}. " + base_prompt
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:10]
    prompts_dir = output_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    write_text(prompts_dir / f"{prompt_hash}.txt", prompt + "\n")
    options = parse_options(args.option)

    try:
        installed = ollama.list()["models"]
    except Exception:
        sys.exit("Cannot reach Ollama. Is it running? Start it with: ollama serve")
    digest = None
    for entry in installed:
        if entry["model"] == args.model or entry["model"] == args.model + ":latest":
            digest = entry["digest"]
    if digest is None:
        names = ", ".join(entry["model"] for entry in installed)
        sys.exit(f"Model '{args.model}' is not installed. Installed models: {names}")
    capabilities = getattr(ollama.show(args.model), "capabilities", None) or []
    state["disable_thinking"] = "thinking" in capabilities

    state["base_row"] = {
        "model": args.model,
        "model_digest": digest[:12],
        "prompt_type": prompt_type,
        "language": args.language or "",
        "prompt_hash": prompt_hash,
        "options": json.dumps(options, sort_keys=True),
    }

    files = find_files(input_path, output_dir)
    if not files:
        sys.exit("No PDFs or images found.")
    if any(f.suffix.lower() == ".pdf" for f in files) and not shutil.which("pdftoppm"):
        sys.exit("Poppler is needed to read PDFs. Install it with: brew install poppler")

    log(f"Run started: {len(files)} file(s), model {args.model}, prompt {prompt_type} ({prompt_hash})")
    log(f"Options: {json.dumps(options, sort_keys=True)}")
    if state["disable_thinking"]:
        if args.max_think_tokens:
            watchdog_note = (
                f"A page is abandoned if the model produces {args.max_think_tokens} "
                "thinking tokens without any transcript (change with --max-think-tokens)."
            )
        else:
            watchdog_note = "The thinking-token limit is switched off (--max-think-tokens 0)."
        log(
            f"WARNING: {args.model} is a thinking model. It may use up its whole "
            "num_predict limit reasoning before it writes any transcript, so pages "
            "can come back empty or take minutes each, and this cannot always be "
            "switched off. An -instruct version of the model, if one exists, is "
            f"usually better for OCR. {watchdog_note}"
        )

    image_stems = [
        (f.parent, f.stem.lower()) for f in files if f.suffix.lower() in IMAGE_EXTENSIONS
    ]
    jobs = []
    for source in files:
        if source.suffix.lower() == ".pdf":
            try:
                pages = int(pdfinfo_from_path(str(source))["Pages"])
            except Exception as problem:
                log(f"ERROR cannot read {source.name}: {problem}")
                state["error"] += 1
                add_manifest_row(
                    source_file=source.name, page="", image_path="", txt_path="",
                    status="error", chars=0, seconds=0, error=f"cannot read PDF: {problem}",
                )
                continue
        else:
            pages = 1
        stem_clash = image_stems.count((source.parent, source.stem.lower())) > 1
        job = place_job(source, base_dir, output_dir, input_path.is_file(), stem_clash)
        job["pages"] = pages
        jobs.append(job)
    state["total"] = sum(job["pages"] for job in jobs)
    log(f"{state['total']} page(s) to check")

    run_started = time.time()
    try:
        for job in jobs:
            if process_job(job, prompt, options, args):
                break
    except KeyboardInterrupt:
        log("Interrupted. Finished pages are saved. To resume, run the same command again:")
        log(shlex.join(["python"] + sys.argv))

    elapsed = timedelta(seconds=int(time.time() - run_started))
    log(
        f"Summary: {state['ok']} ok, {state['warning']} warning, {state['error']} error, "
        f"{state['skipped']} skipped - {elapsed}"
    )
    log(f"Manifest: {state['manifest_file']}")


if __name__ == "__main__":
    main()
