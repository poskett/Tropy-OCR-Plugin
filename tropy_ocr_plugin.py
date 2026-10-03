# Declaration: Code generated using Anthropic Claude (Sonnet 5.5)
# All comments below this line are human-authored.

import argparse
import base64
import fnmatch
import html
import io
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path

import pytesseract
import requests
from PIL import Image

DEFAULT_MODEL = "qwen3-vl:8b-instruct-q4_K_M"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TAG = "ocr:auto"
DEFAULT_TESSERACT_LANG = "eng"
DEFAULT_MAX_THINK_TOKENS = 1000

MARKER_PREFIX = "[Automated OCR - model:"
MARKER_RE = re.compile(re.escape(MARKER_PREFIX) + r".*?\]")

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
    "handwritten": (
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


class ThinkingLimitError(RuntimeError):
    pass


class RepetitionLoop(RuntimeError):
    def __init__(self, text):
        super().__init__("the model began repeating itself")
        self.text = text


REPEAT_NOTICE = (
    "[OCR stopped: the model began repeating itself, so the rest of this page "
    "may be missing - please check this page]"
)


def find_repetition_cut(text):
    length = len(text)
    if length < 400:
        return None
    probe = text[-min(120, length // 4):]
    positions = []
    start = 0
    while True:
        index = text.find(probe, start)
        if index == -1:
            break
        positions.append(index)
        start = index + 1
    if len(positions) < 2:
        return None
    period = positions[-1] - positions[-2]
    if period <= 0:
        return None
    matched = 0
    position = length - 1
    while position - period >= 0 and text[position] == text[position - period]:
        matched += 1
        position -= 1
    needed = 2 * period if period >= 150 else 800
    if matched < needed:
        return None
    region_start = length - matched - period
    return region_start + period


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Transcribe Tropy project photos with a local Ollama vision "
            "model and write the results into Tropy Notes."
        )
    )
    parser.add_argument(
        "--api-url",
        required=True,
        help="Tropy's local HTTP API, e.g. http://localhost:2019. Tropy must be open.",
    )
    parser.add_argument("--run-label", help="Name of the list or item being processed, shown on the progress page")
    parser.add_argument("--list-id", type=int, help="Restrict to items in the Tropy list with this id, including its sublists")
    parser.add_argument(
        "--progress-port",
        type=int,
        help="Serve a live progress page on this local port (0 picks a free port)",
    )
    parser.add_argument(
        "--engine",
        choices=["vision", "tesseract"],
        default="vision",
        help=(
            "OCR engine to use: 'vision' calls a local Ollama vision model, "
            "'tesseract' calls the local tesseract through Python instead"
        ),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama vision model name (--engine vision only)")
    parser.add_argument("--ollama-host", default=DEFAULT_HOST, help="Ollama server URL (--engine vision only)")
    parser.add_argument(
        "--tesseract-lang",
        default=DEFAULT_TESSERACT_LANG,
        help="Tesseract language code, e.g. eng, fra, lat (--engine tesseract only)",
    )
    parser.add_argument(
        "--tesseract-cmd",
        help="Full path to the tesseract program (--engine tesseract only); found automatically if omitted",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "printed", "handwritten"],
        default="auto",
        help="Transcription style hint (--engine vision only)",
    )
    parser.add_argument("--item", type=int, action="append", help="Restrict to this item id (repeatable)")
    parser.add_argument("--photo", type=int, action="append", help="Restrict to this photo id (repeatable)")
    parser.add_argument("--list", dest="list_name", help="Restrict to items in this Tropy list, including its sublists; use 'Parent > Child' for a path")
    parser.add_argument("--filename-glob", help="Restrict to photos whose filename matches this glob pattern")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Print the item/photo ids and filenames matching your filters, then exit without running OCR",
    )
    parser.add_argument("--limit", type=int, help="Process at most this many photos")
    parser.add_argument("--tag", default=DEFAULT_TAG, help="Tag applied to processed photos")
    parser.add_argument("--no-tag", action="store_true", help="Do not tag processed photos")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate notes previously created by this tool",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run OCR and print results without writing to Tropy",
    )
    parser.add_argument(
        "--max-dimension",
        type=int,
        default=2000,
        help="Resize images so the longest edge is at most this many pixels",
    )
    parser.add_argument("--log-file", help="Write a JSONL record for every processed photo to this path")
    parser.add_argument("--language", default="en", help="Language code stored on each note")
    parser.add_argument("--retries", type=int, default=2, help="Number of retries for failed Ollama requests")
    parser.add_argument("--timeout", type=float, default=120.0, help="Timeout in seconds for each Ollama request")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=4096,
        help="Maximum tokens the model may generate per photo (raise for dense full pages)",
    )
    parser.add_argument(
        "--max-think-tokens",
        type=int,
        default=DEFAULT_MAX_THINK_TOKENS,
        help=(
            "Abort if the model produces this many thinking tokens without starting "
            "a transcript (0 disables). Thinking models can use their whole budget "
            "thinking; an -instruct version of the model is usually better for OCR."
        ),
    )
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=8192,
        help=(
            "Context window size given to the model. Must comfortably fit the image "
            "tokens, the prompt, and --max-tokens combined, or the request fails with "
            "a context-overflow error."
        ),
    )
    parser.add_argument(
        "--repeat-penalty",
        type=float,
        default=1.3,
        help=(
            "Penalty applied to already-generated tokens. Above 1.0 to stop the model "
            "looping on the same token on blank or badly damaged pages."
        ),
    )
    return parser


def locate_tesseract(requested):
    if requested:
        return requested
    for candidate in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if Path(candidate).exists():
            return candidate
    return None


def apply_limit(photos, args):
    if args.limit is not None:
        photos = photos[: args.limit]
    return photos


DOCUMENT_HOLD = {"key": None, "kind": None, "document": None}


def release_document():
    document = DOCUMENT_HOLD["document"]
    DOCUMENT_HOLD.update(key=None, kind=None, document=None)
    if document is not None:
        try:
            document.close()
        except Exception:
            pass


def is_pdf(mimetype, name):
    return mimetype == "application/pdf" or str(name or "").lower().endswith(".pdf")


def is_tiff(mimetype, name):
    return mimetype == "image/tiff" or str(name or "").lower().endswith((".tif", ".tiff"))


def is_multipage(photo):
    name = photo.get("filename") or photo.get("path")
    return is_pdf(photo.get("mimetype"), name) or is_tiff(photo.get("mimetype"), name) or bool(photo.get("page"))


def photo_label(photo):
    name = photo.get("filename") or ""
    if is_multipage(photo):
        return f"{name} p.{int(photo.get('page') or 0) + 1}"
    return name


def fit_to_max_dimension(image, max_dimension):
    width, height = image.size
    longest = max(width, height)
    if longest > max_dimension:
        scale = max_dimension / float(longest)
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
    return image


def hold_document(key, kind, opener):
    if DOCUMENT_HOLD["key"] == key and DOCUMENT_HOLD["kind"] == kind:
        return DOCUMENT_HOLD["document"]
    release_document()
    document = opener()
    DOCUMENT_HOLD.update(key=key, kind=kind, document=document)
    return document


def open_pdf(source):
    try:
        import pypdfium2
    except ImportError:
        raise RuntimeError("PDF support needs the pypdfium2 package (pip install pypdfium2)")
    if isinstance(source, Path) and not source.is_file():
        raise RuntimeError(f"the file {source.name} is missing from the project folder")
    try:
        return pypdfium2.PdfDocument(source)
    except Exception as error:
        text = str(error)
        if "password" in text.lower():
            raise RuntimeError("the PDF is password-protected")
        raise RuntimeError(f"could not open the PDF ({text})")


def render_pdf_page(document, page, max_dimension):
    total = len(document)
    if page < 0 or page >= total:
        raise RuntimeError(f"the PDF has {total} page(s) but page {page + 1} was requested")
    pdf_page = document[page]
    try:
        width, height = pdf_page.get_size()
        scale = max_dimension / float(max(width, height))
        return pdf_page.render(scale=scale).to_pil().convert("RGB")
    finally:
        pdf_page.close()


def load_page_image(key, get_source, mimetype, name, page, max_dimension):
    page = int(page or 0)
    if is_pdf(mimetype, name):
        document = hold_document(key, "pdf", lambda: open_pdf(get_source()))
        return fit_to_max_dimension(render_pdf_page(document, page, max_dimension), max_dimension)
    if is_tiff(mimetype, name) or page > 0:
        def open_image():
            source = get_source()
            return Image.open(io.BytesIO(source) if isinstance(source, bytes) else source)

        document = hold_document(key, "image", open_image)
        frames = getattr(document, "n_frames", 1)
        if page >= frames:
            raise RuntimeError(f"the file has {frames} page(s) but page {page + 1} was requested")
        document.seek(page)
        return fit_to_max_dimension(document.convert("RGB"), max_dimension)
    release_document()
    source = get_source()
    with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
        return fit_to_max_dimension(image.convert("RGB"), max_dimension).copy()


def image_to_b64(image):
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def verify_tesseract_available(lang):
    try:
        available_langs = set(pytesseract.get_languages(config=""))
    except pytesseract.TesseractNotFoundError:
        raise SystemExit(
            "tesseract is not installed or not on PATH. Install it first, e.g. on "
            "macOS: brew install tesseract tesseract-lang"
        )
    if not available_langs:
        raise SystemExit(
            "Could not determine which tesseract language packs are installed "
            "(tesseract --list-langs reported none). Check your tesseract install."
        )
    if lang not in available_langs:
        listing = ", ".join(sorted(available_langs))
        raise SystemExit(
            f"Tesseract language '{lang}' is not installed.\n"
            f"Install it, e.g. on macOS: brew install tesseract-lang\n"
            f"Languages currently available: {listing}"
        )


def call_tesseract(image, lang, timeout):
    try:
        return pytesseract.image_to_string(image, lang=lang, timeout=timeout)
    except (pytesseract.TesseractError, RuntimeError) as error:
        raise RuntimeError(f"tesseract failed: {error}") from error


def verify_model_available(host, model):
    url = f"{host.rstrip('/')}/api/tags"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        available = {entry.get("name") for entry in response.json().get("models", [])}
    except (requests.RequestException, ValueError) as error:
        raise SystemExit(f"Could not reach Ollama at {host}: {error}")
    if model not in available:
        listing = ", ".join(sorted(name for name in available if name)) or "(none)"
        raise SystemExit(
            f"Model '{model}' is not available on {host}.\n"
            f"Pull it first with: ollama pull {model}\n"
            f"Models currently available: {listing}"
        )


def describe_ollama_error(error, server_detail):
    detail_lower = server_detail.lower()
    if server_detail and "repeat limit" in detail_lower:
        return (
            f"{error} - {server_detail} "
            f"(the model got stuck generating the same token repeatedly, usually "
            f"on blank, very faint, or badly damaged pages; try raising "
            f"--repeat-penalty above its current value, or inspect this photo "
            f"to see if it is actually legible)"
        )
    if server_detail and ("context" in detail_lower or "slot" in detail_lower):
        return (
            f"{error} - {server_detail} "
            f"(the image plus prompt used more tokens than the model's context "
            f"window allows; try raising --num-ctx, or lowering --max-dimension "
            f"or --max-tokens)"
        )
    if server_detail:
        return f"{error} - {server_detail}"
    return str(error)


def call_ollama(host, model, prompt, image_b64, timeout, retries, max_tokens, num_ctx, repeat_penalty, max_think_tokens):
    url = f"{host.rstrip('/')}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": True,
        "options": {
            "temperature": 0,
            "num_predict": max_tokens,
            "num_ctx": num_ctx,
            "repeat_penalty": repeat_penalty,
        },
    }
    last_error = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout, stream=True)
            response.raise_for_status()
            parts = []
            thinking_chunks = 0
            since_check = 0
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(chunk["error"])
                if chunk.get("response"):
                    parts.append(chunk["response"])
                    since_check += len(chunk["response"])
                    if since_check >= 200:
                        since_check = 0
                        so_far = "".join(parts)
                        cut = find_repetition_cut(so_far)
                        if cut:
                            response.close()
                            raise RepetitionLoop(so_far[:cut].strip())
                if chunk.get("thinking"):
                    thinking_chunks += 1
                    if max_think_tokens and not parts and thinking_chunks > max_think_tokens:
                        response.close()
                        raise ThinkingLimitError(
                            f"model used {thinking_chunks} thinking tokens without writing a "
                            "transcript - try an -instruct version of the model"
                        )
                if chunk.get("done"):
                    break
            return "".join(parts).strip()
        except (ThinkingLimitError, RepetitionLoop):
            raise
        except requests.HTTPError as error:
            server_detail = ""
            try:
                server_detail = response.json().get("error", "")
            except ValueError:
                server_detail = response.text.strip()
            last_error = describe_ollama_error(error, server_detail)
            if attempt < retries:
                time.sleep(2 ** attempt)
        except (requests.RequestException, ValueError) as error:
            last_error = str(error)
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Ollama request failed after {retries + 1} attempts: {last_error}")


def transcribe_with_vision(args, image, prompt):
    try:
        raw_text = call_ollama(
            args.ollama_host,
            args.model,
            prompt,
            image_to_b64(image),
            args.timeout,
            args.retries,
            args.max_tokens,
            args.num_ctx,
            args.repeat_penalty,
            args.max_think_tokens,
        )
        return raw_text, False
    except RepetitionLoop as loop:
        return loop.text, True


def clean_ocr_text(raw_text):
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def is_no_text_response(text):
    normalized = text.strip().strip(".").lower()
    return normalized in {"[no text identified]", "no text identified"}


def make_marker(model_name):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{MARKER_PREFIX} {model_name} - {timestamp}]"


def finalize_note_text(ocr_text, model_name):
    text = ocr_text.rstrip("\n")
    return f"{text}\n\n{make_marker(model_name)}"


STATUS = {
    "state": "starting",
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "skipped": 0,
    "current": "",
    "recent": [],
    "started": time.time(),
    "elapsed": 0,
    "remaining": None,
    "dry_run": False,
    "model": "",
    "label": "",
    "message": "",
    "tag_errors": 0,
    "repetitions": 0,
    "updated": time.time(),
    "finished_at": None,
}
STOP_EVENT = threading.Event()

PROGRESS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tropy OCR progress</title>
<style>
:root{--bg:#f5f6f7;--card:#fff;--text:#1f2328;--muted:#656d76;--line:#d8dee4;--accent:#1a7f37;--bad:#b3261e;--warn:#fff8c5;--warnline:#d4a72c}
@media (prefers-color-scheme: dark){:root{--bg:#161b22;--card:#1f262e;--text:#e6edf3;--muted:#9da7b3;--line:#323b46;--accent:#3fb950;--bad:#f85149;--warn:#3a3217;--warnline:#8a6d1f}}
body{font-family:-apple-system,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text);margin:0;padding:2rem 1rem}
main{max-width:46rem;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1.25rem 1.5rem;margin-bottom:1rem}
h1{font-size:1.3rem;margin:0 0 .75rem}
dl{display:grid;grid-template-columns:7rem 1fr;gap:.25rem .75rem;margin:0}
dt{color:var(--muted)} dd{margin:0;font-weight:600;word-break:break-word}
.pill{display:inline-block;padding:.1rem .6rem;border-radius:999px;font-size:.85rem;font-weight:600;background:var(--accent);color:#fff}
.pill.stopped{background:var(--muted)}
progress{width:100%;height:1.1rem;accent-color:var(--accent);margin:.75rem 0 .25rem}
.stats{color:var(--muted);font-size:.95rem}
.stopnotice{border-color:var(--bad)}
.notice{background:var(--warn);border:1px solid var(--warnline);border-radius:8px;padding:.6rem .9rem;margin-bottom:1rem}
button{padding:.45rem 1rem;font-size:.95rem;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button:disabled{opacity:.5;cursor:default}
ul{list-style:none;padding:0;margin:.5rem 0 0;font-size:.9rem}
li{padding:.25rem 0;border-top:1px solid var(--line)}
li.ok{color:var(--accent)} li.failed{color:var(--bad)} li.skipped{color:var(--muted)}
</style></head><body><main>
<div class="notice stopnotice" id="message" style="display:none"></div>
<div class="notice" id="notice">Please keep this window open while processing so you can follow progress.</div>
<div class="card">
<h1>Tropy OCR <span class="pill" id="state"></span></h1>
<dl>
<dt>Model</dt><dd id="model"></dd>
<dt>Processing</dt><dd id="label"></dd>
<dt>Now</dt><dd id="current"></dd>
</dl>
<progress id="bar" value="0" max="1"></progress>
<div class="stats" id="counts"></div>
<div class="stats" id="times"></div>
<p><button id="stop">Stop after current photo</button></p>
</div>
<div class="card"><strong>Recent photos</strong><ul id="recent"></ul></div>
<script>
function fmt(seconds) {
  const s = Math.round(seconds);
  if (s < 60) return s + ' s';
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m + ' min' + (r ? ' ' + r + ' s' : '');
}
function setText(id, text) { document.getElementById(id).textContent = text; }
async function refresh() {
  let s;
  try { s = await (await fetch('/status.json')).json(); } catch (e) { return; }
  const done = s.state === 'finished' || s.state === 'stopped';
  const pill = document.getElementById('state');
  pill.textContent = s.state === 'running' ? 'Running' : s.state === 'finished' ? 'Finished' : s.state === 'stopped' ? 'Stopped' : 'Starting';
  pill.className = 'pill' + (s.state === 'stopped' ? ' stopped' : '');
  setText('model', s.model || '-');
  setText('label', (s.label || '-') + (s.dry_run ? ' (dry run: nothing is written)' : ''));
  setText('current', done ? '-' : (s.current || '-'));
  const bar = document.getElementById('bar');
  bar.max = Math.max(s.total, 1);
  bar.value = s.done;
  setText('counts', s.done + ' of ' + s.total + ' photos - ok ' + s.ok + ', failed ' + s.failed + ', skipped ' + s.skipped + (s.tag_errors ? ' - items not tagged ' + s.tag_errors : '') + (s.repetitions ? ' - cut short by repetition ' + s.repetitions : ''));
  let times = 'Elapsed ' + fmt(s.elapsed);
  if (!done && s.remaining !== null) times += ' - about ' + fmt(s.remaining) + ' left';
  setText('times', times);
  document.getElementById('notice').style.display = done ? 'none' : 'block';
  const message = document.getElementById('message');
  message.textContent = s.message || '';
  message.style.display = s.message ? 'block' : 'none';
  document.getElementById('stop').disabled = done;
  const list = document.getElementById('recent');
  list.replaceChildren();
  for (const r of s.recent) {
    const li = document.createElement('li');
    li.className = r.status === 'dry-run' ? 'ok' : r.status;
    li.textContent = r.line;
    list.append(li);
  }
}
document.getElementById('stop').onclick = () => fetch('/stop', {method: 'POST'});
refresh();
setInterval(refresh, 1000);
</script></main></body></html>
"""


def update_status(**changes):
    STATUS.update(changes)
    now = time.time()
    STATUS["updated"] = now
    if STATUS["state"] in ("finished", "stopped") and STATUS["finished_at"] is None:
        STATUS["finished_at"] = now
    STATUS["elapsed"] = (STATUS["finished_at"] or now) - STATUS["started"]
    if STATUS["finished_at"] is None and STATUS["done"] and STATUS["total"]:
        per_photo = STATUS["elapsed"] / STATUS["done"]
        STATUS["remaining"] = per_photo * (STATUS["total"] - STATUS["done"])
    else:
        STATUS["remaining"] = None


def live_status():
    snapshot = dict(STATUS)
    now = time.time()
    snapshot["elapsed"] = (snapshot["finished_at"] or now) - snapshot["started"]
    if snapshot["finished_at"] is not None or snapshot["remaining"] is None:
        snapshot["remaining"] = None
    else:
        snapshot["remaining"] = max(0, snapshot["remaining"] - (now - snapshot["updated"]))
    return snapshot


def emit_progress(event, **fields):
    print("PROGRESS " + json.dumps({"event": event, **fields}), flush=True)


class ProgressHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/status.json"):
            body = json.dumps(live_status()).encode("utf-8")
            content_type = "application/json"
        else:
            body = PROGRESS_PAGE.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/stop"):
            STOP_EVENT.set()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass


def start_progress_server(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), ProgressHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def process_is_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_run_lock(api_url):
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", api_url)
    lock_path = Path(tempfile.gettempdir()) / f"tropy_ocr_{safe_name}.lock"
    if lock_path.exists():
        try:
            other_pid = int(lock_path.read_text().strip())
        except ValueError:
            other_pid = None
        if other_pid and process_is_alive(other_pid):
            raise SystemExit(
                f"Another OCR run (process {other_pid}) is already writing to {api_url}. "
                "Wait for it to finish or stop it first."
            )
    lock_path.write_text(str(os.getpid()))
    return lock_path


def api_endpoint(args, path):
    return f"{args.api_url.rstrip('/')}/project{path}"


def api_request(args, method, path, **kwargs):
    response = requests.request(method, api_endpoint(args, path), timeout=args.timeout, **kwargs)
    response.raise_for_status()
    return response


def tropy_is_reachable(args):
    try:
        response = requests.get(f"{args.api_url.rstrip('/')}/", timeout=5)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return False
    return not (isinstance(data, dict) and "project" in data and data["project"] is None)


def check_api_reachable(args):
    try:
        response = requests.get(f"{args.api_url.rstrip('/')}/version", timeout=10)
        response.raise_for_status()
    except requests.RequestException as error:
        raise SystemExit(
            f"Could not reach Tropy's API at {args.api_url}: {error}. "
            "Check that Tropy is open and the API is switched on."
        )


def trailing_id(value):
    match = re.search(r"(\d+)/?$", str(value))
    return int(match.group(1)) if match else None


def as_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("@graph", "items", "photos"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]
    return []


def photo_marker_note_ids(args, entry):
    found = []
    for note in entry.get("notes") or []:
        note_id = note if isinstance(note, int) else trailing_id(note.get("@id") or note.get("id") or "")
        if not note_id:
            continue
        try:
            text = api_request(args, "GET", f"/notes/{note_id}", params={"format": "plain"}).text
        except requests.RequestException:
            continue
        if MARKER_PREFIX in text:
            found.append(note_id)
    return found


def payload_entries(payload):
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        nested = [value for value in payload.values() if isinstance(value, dict)]
        if nested and len(nested) == len(payload):
            return nested
        return [payload]
    return []


def api_all_lists(args):
    payload = api_request(args, "GET", "/lists", params={"expand": "true"}).json()
    lists = {}
    for entry in payload_entries(payload):
        list_id = trailing_id(entry.get("@id") or entry.get("id") or "")
        if list_id:
            lists[list_id] = entry
    return lists


def api_list_parent(entry):
    value = entry.get("parent", entry.get("parent_list_id"))
    if isinstance(value, dict):
        value = value.get("id") or value.get("@id")
    parent = trailing_id(value) if value is not None else 0
    return parent or 0


def api_list_children(lists):
    children = {list_id: [] for list_id in lists}
    children[0] = []
    for list_id, entry in lists.items():
        children.setdefault(api_list_parent(entry), []).append(list_id)
        for child in entry.get("children") or []:
            child_id = child if isinstance(child, int) else trailing_id(child.get("@id") or child.get("id") or "")
            if child_id and child_id not in children.setdefault(list_id, []):
                children[list_id].append(child_id)
    return children


def api_list_descendants(lists, list_id):
    children = api_list_children(lists)
    found = [list_id]
    for current in found:
        for child in children.get(current, []):
            if child not in found:
                found.append(child)
    return found


def api_list_path(lists, list_id):
    names = []
    seen = set()
    while list_id and list_id not in seen and list_id in lists:
        seen.add(list_id)
        names.append(str(lists[list_id].get("name", "")))
        list_id = api_list_parent(lists[list_id])
    return " > ".join(reversed(names))


def api_resolve_list(lists, list_name):
    if " > " in list_name:
        segments = list_name.split(" > ")
        parent_id = 0
        list_id = None
        for depth, segment in enumerate(segments):
            matches = [
                candidate
                for candidate, entry in lists.items()
                if api_list_parent(entry) == parent_id and entry.get("name") == segment
            ]
            if not matches:
                where = " > ".join(segments[:depth]) or "the top level"
                raise SystemExit(
                    f"No list named '{segment}' found under {where} "
                    f"while resolving path '{list_name}'."
                )
            list_id = matches[0]
            parent_id = list_id
    else:
        matches = [candidate for candidate, entry in lists.items() if entry.get("name") == list_name]
        if not matches:
            raise SystemExit(f"No list named '{list_name}' found in this project.")
        if len(matches) > 1:
            paths = ", ".join(f"'{api_list_path(lists, match)}' (list id {match})" for match in matches)
            raise SystemExit(
                f"Multiple lists named '{list_name}' exist ({paths}). "
                f"Re-run with --list using the full path, e.g. --list \"{api_list_path(lists, matches[0])}\"."
            )
        list_id = matches[0]
    return list_id


def api_items_in_lists(args, list_ids):
    item_ids = []
    for list_id in list_ids:
        payload = api_request(args, "GET", f"/lists/{list_id}/items").json()
        for entry in payload_entries(payload):
            item_id = trailing_id(entry.get("@id") or entry.get("id") or "")
            if item_id and item_id not in item_ids:
                item_ids.append(item_id)
    return item_ids


def api_item_ids(args):
    restrictions = []
    if args.item:
        restrictions.append(list(dict.fromkeys(args.item)))
    if args.list_name or args.list_id is not None:
        lists = api_all_lists(args)
        if args.list_name:
            restrictions.append(api_items_in_lists(args, api_list_descendants(lists, api_resolve_list(lists, args.list_name))))
        if args.list_id is not None:
            restrictions.append(api_items_in_lists(args, api_list_descendants(lists, args.list_id)))
    if not restrictions:
        payload = api_request(args, "GET", "/items").json()
        every_item = []
        for entry in payload_entries(payload):
            item_id = trailing_id(entry.get("@id") or entry.get("id") or "")
            if item_id and item_id not in every_item:
                every_item.append(item_id)
        return every_item
    selected = restrictions[0]
    for other in restrictions[1:]:
        selected = [item_id for item_id in selected if item_id in other]
    return selected


def api_build_selection(args):
    photos = []
    for item_id in api_item_ids(args):
        payload = api_request(args, "GET", f"/items/{item_id}/photos").json()
        for position, entry in enumerate(as_list(payload)):
            photo_id = trailing_id(entry.get("@id") or entry.get("id"))
            if not photo_id:
                continue
            if args.photo and photo_id not in args.photo:
                continue
            filename = entry.get("filename") or Path(str(entry.get("path", ""))).name or f"photo_{photo_id}"
            if args.filename_glob and not fnmatch.fnmatch(filename, args.filename_glob):
                continue
            marker_notes = photo_marker_note_ids(args, entry)
            if marker_notes:
                TAG_STATE["done_items"].add(item_id)
            if marker_notes and not args.overwrite:
                continue
            details = {
                "mimetype": entry.get("mimetype"),
                "page": entry.get("page"),
                "path": entry.get("path"),
                "filename": filename,
            }
            if is_multipage(details) and details["page"] is None:
                try:
                    details["page"] = api_request(args, "GET", f"/photos/{photo_id}").json().get("page")
                except (requests.RequestException, ValueError, AttributeError):
                    pass
            photo = {
                "photo_id": photo_id,
                "item_id": item_id,
                "filename": filename,
                "mimetype": details["mimetype"],
                "page": details["page"] if details["page"] is not None else 0,
                "page_unknown": is_multipage(details) and details["page"] is None,
                "source_path": entry.get("path"),
                "old_note_ids": marker_notes,
            }
            photo["label"] = photo_label(photo)
            photos.append(photo)
    return photos


def api_load_image(args, photo):
    if photo.get("page_unknown"):
        raise RuntimeError("Tropy did not say which page of this file the photo is, so it was not guessed")
    source_path = photo.get("source_path")
    local = Path(str(source_path)) if source_path else None
    key = str(local) if local else f"photo:{photo['photo_id']}"

    def get_source():
        if local is not None and local.is_file():
            return local
        return api_request(args, "GET", f"/photos/{photo['photo_id']}/raw").content

    return load_page_image(
        key, get_source, photo.get("mimetype"), photo.get("filename") or source_path, photo.get("page"), args.max_dimension
    )


def build_note_html(text):
    paragraphs = []
    for line in text.split("\n"):
        paragraphs.append(f"<p>{html.escape(line)}</p>" if line.strip() else "<p></p>")
    return "".join(paragraphs)


TAG_STATE = {"id": None, "tagged": set(), "failed": set(), "done_items": set()}


def tag_entries(payload):
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        if "name" in payload:
            return [payload]
        return [entry for entry in payload.values() if isinstance(entry, dict)]
    return []


def api_prepare_tag(args):
    wanted = args.tag.strip().lower()
    for entry in tag_entries(api_request(args, "GET", "/tags").json()):
        if str(entry.get("name", "")).strip().lower() == wanted:
            TAG_STATE["id"] = entry.get("id") or trailing_id(entry.get("@id", ""))
            return
    body = {"name": args.tag}
    created = api_request(args, "POST", "/tags", json=body).json()
    for entry in tag_entries(created):
        TAG_STATE["id"] = entry.get("id") or trailing_id(entry.get("@id", ""))


def api_tag_item(args, item_id):
    if item_id in TAG_STATE["tagged"] or item_id in TAG_STATE["failed"]:
        return
    try:
        api_request(args, "POST", f"/items/{item_id}/tags", json={"tag": args.tag})
    except requests.RequestException:
        if TAG_STATE["id"] is None:
            TAG_STATE["failed"].add(item_id)
            raise
        try:
            api_request(args, "POST", f"/items/{item_id}/tags", json={"tag": TAG_STATE["id"]})
        except requests.RequestException:
            TAG_STATE["failed"].add(item_id)
            raise
    TAG_STATE["tagged"].add(item_id)


def process_photo_api(photo, args):
    start = time.time()
    base = {"photo_id": photo["photo_id"], "filename": photo["filename"]}
    try:
        image = api_load_image(args, photo)
        repeated = False
        if args.engine == "tesseract":
            raw_text = call_tesseract(image, args.tesseract_lang, args.timeout)
            engine_label = f"tesseract {args.tesseract_version}:{args.tesseract_lang}"
        else:
            raw_text, repeated = transcribe_with_vision(args, image, PROMPTS[args.mode])
            engine_label = args.model
        ocr_text = clean_ocr_text(raw_text)
        no_text = not ocr_text or is_no_text_response(ocr_text)
        if no_text:
            ocr_text = "[no text identified]"
        if repeated:
            no_text = False
            ocr_text = REPEAT_NOTICE if ocr_text == "[no text identified]" else f"{ocr_text}\n\n{REPEAT_NOTICE}"
        final_text = finalize_note_text(ocr_text, engine_label)
    except ThinkingLimitError as error:
        return {**base, "status": "failed", "error": str(error), "thinking_abort": True, "elapsed": time.time() - start}
    except Exception as error:
        return {**base, "status": "failed", "error": str(error), "elapsed": time.time() - start}
    if args.dry_run:
        return {
            **base,
            "status": "dry-run",
            "chars": len(ocr_text),
            "no_text": no_text,
            "repetition": repeated,
            "elapsed": time.time() - start,
            "text": ocr_text,
        }
    try:
        created = api_request(
            args,
            "POST",
            "/notes",
            json={
                "html": build_note_html(final_text),
                "photo": photo["photo_id"],
                "language": args.language.strip().lower(),
            },
        ).json()
        if not created.get("id"):
            raise RuntimeError("Tropy did not report a new note id")
        for old_note_id in photo["old_note_ids"]:
            api_request(args, "DELETE", f"/notes/{old_note_id}")
    except Exception as error:
        return {**base, "status": "failed", "error": f"write failed: {error}", "elapsed": time.time() - start}
    result = {
        **base,
        "status": "ok",
        "chars": len(ocr_text),
        "no_text": no_text,
        "repetition": repeated,
        "elapsed": time.time() - start,
    }
    if not args.no_tag:
        already_failed = photo["item_id"] in TAG_STATE["failed"]
        try:
            api_tag_item(args, photo["item_id"])
        except Exception as error:
            result["tag_error"] = f"tag failed: {error}"
        else:
            if already_failed:
                result["tag_error"] = "tag failed earlier for this item"
    return result


def tropy_closed_message(index, total):
    return (
        f"Tropy was closed, so OCR stopped at photo {index} of {total}. "
        "Open Tropy and press Start OCR to continue; photos already done are skipped."
    )


def run_api_mode(args):
    check_api_reachable(args)
    if args.engine == "tesseract":
        verify_tesseract_available(args.tesseract_lang)
        args.tesseract_version = str(pytesseract.get_tesseract_version())
    else:
        verify_model_available(args.ollama_host, args.model)
    try:
        photos = api_build_selection(args)
    except requests.RequestException as error:
        raise SystemExit(f"Tropy's API refused a request while listing photos: {error}")
    photos = apply_limit(photos, args)
    if args.preview:
        for photo in photos:
            print(f"item {photo['item_id']:>6}  photo {photo['photo_id']:>6}  {photo.get('label') or photo['filename']}")
        print(f"{len(photos)} photo(s) matched")
        return
    lock_path = acquire_run_lock(args.api_url)
    signal.signal(signal.SIGTERM, lambda signum, frame: STOP_EVENT.set())
    server = None
    log_handle = open(args.log_file, "a", encoding="utf-8") if args.log_file else None
    counts = {}
    failed_ids = []
    repeated_ids = []
    run_start = time.time()
    closed_message = ""
    try:
        model_label = f"Tesseract ({args.tesseract_lang})" if args.engine == "tesseract" else args.model
        update_status(
            started=time.time(),
            state="running",
            total=len(photos),
            dry_run=args.dry_run,
            model=model_label,
            label=args.run_label or "",
        )
        if args.progress_port is not None:
            server = start_progress_server(args.progress_port)
            emit_progress("server", url=f"http://127.0.0.1:{server.server_address[1]}/")
        if not args.no_tag and not args.dry_run and (photos or TAG_STATE["done_items"]):
            try:
                api_prepare_tag(args)
            except Exception as error:
                print(f"Warning: could not set up the '{args.tag}' tag: {error}", file=sys.stderr)
            for done_item in sorted(TAG_STATE["done_items"] - {photo["item_id"] for photo in photos}):
                try:
                    api_tag_item(args, done_item)
                except Exception:
                    pass
        update_status(tag_errors=len(TAG_STATE["failed"]))
        emit_progress("start", total=len(photos))
        print(f"Selected {len(photos)} photo(s) to process")
        for index, photo in enumerate(photos, start=1):
            if STOP_EVENT.is_set():
                update_status(state="stopped", current="")
                print("Stopped on request.")
                break
            update_status(current=f"photo {photo['photo_id']} {photo.get('label') or photo['filename']}")
            result = process_photo_api(photo, args)
            line = format_progress(index, len(photos), photo, result)
            print(line + timing_suffix(run_start, index, len(photos)))
            if result["status"] == "dry-run":
                print("-" * 60)
                print(result["text"])
                print("-" * 60)
            if log_handle:
                log_handle.write(json.dumps(result) + "\n")
                log_handle.flush()
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            if result["status"] == "failed":
                failed_ids.append(photo["photo_id"])
            if result.get("repetition"):
                repeated_ids.append(photo["photo_id"])
            STATUS["recent"] = ([{"status": result["status"], "line": line}] + STATUS["recent"])[:10]
            update_status(
                done=index,
                ok=counts.get("ok", 0) + counts.get("dry-run", 0),
                failed=counts.get("failed", 0),
                skipped=counts.get("skipped", 0),
                tag_errors=len(TAG_STATE["failed"]),
                repetitions=len(repeated_ids),
            )
            emit_progress(
                "photo",
                index=index,
                total=len(photos),
                photo_id=photo["photo_id"],
                status=result["status"],
                error=result.get("error"),
            )
            if result.get("thinking_abort"):
                print(
                    "Stopping: the model is spending its token budget thinking, and every "
                    "remaining photo would do the same. Use an -instruct model, or raise "
                    "--max-think-tokens.",
                    file=sys.stderr,
                )
                break
            if result["status"] == "failed" and not tropy_is_reachable(args):
                closed_message = tropy_closed_message(index, len(photos))
                break
        if closed_message:
            print(closed_message)
            update_status(state="stopped", current="", message=closed_message)
        elif STATUS["state"] != "stopped":
            update_status(state="finished", current="")
    finally:
        release_document()
        if log_handle:
            log_handle.close()
        lock_path.unlink(missing_ok=True)
    emit_progress(
        "done",
        ok=counts.get("ok", 0) + counts.get("dry-run", 0),
        failed=counts.get("failed", 0),
        skipped=counts.get("skipped", 0),
        failed_ids=failed_ids,
        stopped=STOP_EVENT.is_set(),
        tag_errors=len(TAG_STATE["failed"]),
        repeated=len(repeated_ids),
        repeated_ids=repeated_ids,
        message=closed_message,
    )
    print(
        f"Done. ok={counts.get('ok', 0)} dry-run={counts.get('dry-run', 0)} "
        f"failed={counts.get('failed', 0)} in {format_duration(time.time() - run_start)}"
    )
    if failed_ids:
        print("Failed photo ids:", ", ".join(str(i) for i in failed_ids))
    if repeated_ids:
        print(
            f"Warning: the model began repeating itself on {len(repeated_ids)} photo(s); the output was cut "
            "short and flagged in the note. Check photo id(s): " + ", ".join(str(i) for i in repeated_ids)
        )
    if server:
        time.sleep(10 if closed_message else 3)
        server.shutdown()


def format_duration(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} min" + (f" {rest} s" if rest else "")


def timing_suffix(run_start, index, total):
    elapsed = time.time() - run_start
    suffix = f" [elapsed {format_duration(elapsed)}"
    if index < total:
        suffix += f", about {format_duration(elapsed / index * (total - index))} left"
    return suffix + "]"


def format_progress(index, total, photo, result):
    base = f"[{index}/{total}] photo {photo['photo_id']} {photo.get('label') or photo['filename']} - {result['status']}"
    if result["status"] in ("ok", "dry-run"):
        suffix = " - no text identified" if result.get("no_text") else f" - {result['chars']} chars"
        warning = " - note saved but tag failed" if result.get("tag_error") else ""
        if result.get("repetition"):
            warning += " - MODEL REPEATED ITSELF: output cut, check this page"
        return f"{base}{suffix} - {result['elapsed']:.1f}s{warning}"
    if result["status"] == "failed":
        return f"{base} - {result['error']}"
    return base


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    tesseract_cmd = locate_tesseract(args.tesseract_cmd)
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    run_api_mode(args)


if __name__ == "__main__":
    main()
