# OCR Transcription for Tropy (version 0.5.1)
Adds a **Start OCR** button to Tropy. Select items, press the button, and each photo is transcribed by a model running on locally on your own Mac. The text is saved as a Tropy note on the photo, and the item is tagged `ocr:auto`.

**Requirements:** a Mac with an Apple Silicon chip (M1, M2, M3 or later; not an Intel Mac), and Tropy 1.17. A Mac with 16 GB of memory or more is recommended for the default model.

## AI declaration
The code for this project was generated using Claude Code (Sonnet 5.5). This readme  file was generated from the Claude Code project and then edited by James Poskett.

## Disclaimer
This plugin is an independent, unofficial tool. It is not affiliated with or
endorsed by the Tropy project. It is released **as is and without warranty**.

## Benefits

- Privacy (images do not leave your computer)
- Rights (no external AI model training)
- Cost (no payment for commercial service)
- Reproducability (specific models can be selected and pinned)

---

## Installation: follow these steps in order

### Step 1. Install Ollama (runs the AI model)
1. Go to https://ollama.com/download and download Ollama for macOS.
2. Open the downloaded file and drag Ollama into your Applications folder.
3. Open Ollama from Applications. A small llama icon appears at the top of your screen (menu bar). **Ollama must be running whenever you use the plugin.**

### Step 2. Download the model (one time only)
1. Open the **Terminal** app (press Cmd+Space, type Terminal, press Enter).
2. Copy and paste this line, then press Enter:

   `ollama pull qwen3-vl:8b-instruct-q4_K_M`

3. Wait until it finishes. This is a large download (several gigabytes), so use a good connection.
4. You can close Terminal.

### Step 3. Install the plugin in Tropy
1. Download `tropy-ocr-0.5.1.zip` from Release on this page.
2. Open Tropy.
3. In the menu bar choose **Tropy > Settings > Plugins**
4. Click "Install Plugin".
5. Select `tropy-ocr-0.5.1.zip` and click 'Open'.
6. The plugin 'OCR Transcription' should now appear.

### Step 4. Turn the plugin on
1. In the same settings panel, click **Enable** under **OCR Transcription**.
2. Click **Settings** and look through them. The defaults work for most people. The model name must match what you downloaded in Step 2.

### Step 5. Turn on Tropy's API (required)
1. In Tropy Preferences, open the **Settings** tab (it may be under Advanced).
2. Scroll down to **Developer API** at the bottom.
3. Switch **Developer API** on. Leave the port as 2019 unless you have a reason to change it.

### Step 6. Allow the plugin to run (only if macOS blocks it)
If you see a message that the program "cannot be opened" or "is damaged", open Terminal and paste this (adjust the path if your plugins folder is elsewhere), then press Enter:

`xattr -dr com.apple.quarantine ~/Library/Application\ Support/Tropy/plugins/tropy-ocr`

Then quit and reopen Tropy.

You are ready to start!

---

## Using the plugin
1. Make sure **Ollama is running** (llama icon in the menu bar) and **Tropy stays open**.
2. In Tropy's main window, click one or more items. (To do a whole list, open the list and select nothing.)
3. Click **Start OCR** at the bottom left of the window and confirm.
4. A progress page opens in your web browser showing the model, what is being processed, progress and time left. Keep it open while processing. The button reads "Processing 3/81" while it works.
5. When it finishes, a message appears. Each photo now has a note with the transcription. The last line of every note says it was produced automatically, with the model name and time. Always check it against the image.

Things to know:
- The **whole of every selected item** is processed, which can take a long time for items with many photos. The **Stop** button on the progress page stops after the current photo.
- The tag (default `ocr:auto`) is added to the **item**, not to individual photos, once its first note has been saved. Items that were already done in an earlier run are tagged too. The tag is created once and reused; to switch tagging off, tick "Do not tag processed items" in the settings.
- **PDFs** (and multi-page TIFFs) work too: Tropy lists each page as its own photo, and each page gets its own note. Pages are rendered in memory only, so nothing extra is saved on disk. The PDF's own text layer is ignored; every page is read from its image.
- If the model gets stuck repeating a page's text, OCR stops that page early, keeps the first pass, adds a visible "OCR stopped: the model began repeating itself" line to the note, and lists the photo at the end. Please check those pages.
- If the model reaches its token limit before finishing a page, the note ends with "OCR stopped: hit the token limit - this page may be incomplete". Raise **Max tokens per photo** (and **Context window**) for very dense pages.
- Some models "think" before answering (for example `gemma4`). The plugin detects these and switches thinking off, which is faster and stops them looping. `gemma4:26b-a4b-it-q4_K_M` works well if your Mac has the memory for it.
- Images are sent to the model as PNG, shrunk so the longest edge is at most 3000 pixels (**Longest image edge**; 0 = no limit). Ollama shrinks images further itself, so larger values add nothing: gemma4 gains no detail above about 2000 pixels and qwen3-vl none above about 3000. PDF pages are rendered at 200 dpi (**PDF render resolution**).
- **Temperature** (default 0.2) controls how adventurous the model is. Keep it low for faithful transcription.
- Photos that already have an automated OCR note are skipped. To redo them, switch on "Replace earlier automated OCR notes" in the plugin settings.
- If you close Tropy during a run, OCR stops. Open Tropy and press Start OCR again to carry on where it stopped.
- Do not quit Ollama during a run.

---

## Optional: Tesseract instead of an AI model
Tesseract is a simpler, faster tool that works best on clean printed text and poorly on handwriting.
1. Install Homebrew if you do not have it: https://brew.sh
2. In Terminal run: `brew install tesseract tesseract-lang`
3. In the plugin Settings change **Engine** to `tesseract` and set **Tesseract language** (for example `eng`, `fra`, `lat`).

You do not need Ollama or the model if you only use Tesseract.

---

## If something goes wrong
| Message or problem | What to do |
| --- | --- |
| "Could not reach Tropy's API" | Do Step 5 (API on) and check the port in Settings matches. |
| "Could not reach Ollama" or "Model ... is not available" | Open Ollama (Step 1) and check you completed Step 2. The model name in Settings must match exactly. |
| macOS says the program cannot be opened | Do Step 6. |
| No Start OCR button | Check the plugin is enabled (Step 4) and that you are in the main project window, not the photo viewer. |
| "Nothing to do" | The photos already have an automated OCR note. Turn on "Replace earlier automated OCR notes" to redo them. |
| A photo is listed as failed | See the progress page for the reason. Blank or very faint pages sometimes fail; the rest carry on. |
| An error mentioning the context window | Raise **Context window** (e.g. 12288) or lower **Longest image edge** (e.g. 2000). |
| Settings still show old values after updating | Tropy keeps settings you saved earlier. Change them by hand, or uninstall and reinstall the plugin to get the new defaults. |


## Uninstalling
Tropy > Settings > Plugins > OCR Transcription > **Uninstall**. This does not remove notes already created. To remove Ollama, delete it from Applications.

## For developers
- `tropy_ocr_plugin.py` is a separate, API-only version of the standalone `../tropy-ocr/tropy_ocr.py` (no `--project` mode, no tag colour). Changes are not shared between them automatically.
- `tropy_ocr_plugin.py` is the source of the bundled program `bin/tropy_ocr`; rebuild with `./build_bundle.sh`. It needs a `.venv` in this folder: `python3 -m venv .venv` then `.venv/bin/pip install -r requirements.txt`.
- Command line use: `bin/tropy_ocr --api-url http://localhost:2019 --item 12 --preview`.
- To use your own Python instead of the bundled program, set "Python executable" in the settings.
