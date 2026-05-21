# SAS TLF Assistant MVP

This is a local first version of an AI-assisted SAS TLF generation workflow. It stores paired historical SAS programs, outputs, and shells; retrieves similar examples for a new shell; generates a first-draft SAS program; and performs static/log validation.

## Run

```powershell
python app.py
```

Open:

```text
http://127.0.0.1:8000
```

## Workflow

1. Go to **Knowledge Base**.
2. Click **Load Sample**, upload your own historical SAS program/output/shell pair, or use **Scan Output Directory**.
3. For directory scanning, enter a study output directory. The app reads the first page of each supported output, looks for the `Program:` note, resolves the relative `.sas` path against the output location, and adds matched pairs to the knowledge base.
4. Use **Clean Shell Agent** to read a new shell document, retrieve similar historical shell/output examples from the knowledge base, and create a `_clean` document that keeps each individual shell structure, expanded columns, and original rows.
5. Go to **Generate SAS**.
6. Provide the ADaM dataset path, shell document, optional MDDT document, and the folder where generated SAS programs should be saved.
7. Click **Generate SAS**.
8. Review retrieved examples, shell structure JSON, generated SAS, saved program path, and validation findings.
9. Paste a SAS log into the log box and click **Validate** to check runtime issues.

## Clean Shell Agent

The Clean Shell Agent reads either a local shell document path or an uploaded shell file and creates a clean shell file. The clean document is built from the new shell itself: each individual Table, Listing, and Figure shell keeps its original rows, applies its assigned treatment header horizontally, and expands the placeholder column under the original shell header across the expanded header columns. Retrieved knowledge-base examples and the configured LLM are used as interpretation support, not as a requirement to match a specific TFL number.

DOCX and RTF clean-shell outputs are written in landscape layout with 10-point text. The clean shell uses the treatment-column header table for the corresponding header without adding an extra underline separator row. Superscript and subscript markers from DOCX header definitions are preserved in generated DOCX output. In DOCX output, the expanded column headers are rendered as their own Word table, and the shell/body rows are rendered as a separate Word table with matching column widths.

The clean-shell reader preserves leading spaces from original shell row labels where possible. Multi-line shell rows are split into matching label/value rows before expansion, and generated DOCX value columns are centered so masked placeholders keep a similar position within each expanded column.

Blank spacer rows inside the original shell table are retained between variable groups. For multi-level DOCX headers, adjacent repeated first-level header cells are merged horizontally while the second-level columns remain separate.

For path-based inputs, it writes the clean file in the same folder. For uploaded files, provide **Clean Shell Output Folder** if you want the clean file written to a specific location; otherwise it is saved under `runs/clean_shells`.

For example:

```text
tfl_shells.docx -> tfl_shells_clean.docx
```

In this version, it supports `.docx`, `.txt`, and `.rtf`. It looks for:

- A treatment columns section.
- Header definitions such as `Header 1`, `Header 2`, or `Treatment Column Header 1`.
- A later TFL Shells/TLF Shells section where each shell references `Header 1`, `Header 2`, and so on.
- The assigned header table is copied under each shell that references it. Word table headers with merged cells are expanded to the final horizontal column count, so a grouped header such as Cohort 1/Cohort 2/Cohort 3/Overall becomes the corresponding clean-shell header table.
- An optional reference final output can be uploaded or provided by path. Its row structure is converted into a clean shell template with result values masked as `x` placeholders.
- When a matching knowledge-base output is available, the clean shell uses the paired historical output layout as the row template and masks result values as `x`/`xx`/`xx.x` placeholders instead of copying actual numbers.
- If no matching knowledge-base output is found, original row lines and content from each individual Table, Listing, or Figure shell are preserved under the copied header table.
- After a clean shell is created, use the **Refine With LLM** conversation window to enter follow-up prompts. The app sends the current clean shell, the prompt history, and retrieved knowledge-base context to the configured LLM, then rewrites the clean shell file with the refined version.
- Simple keep-only prompts such as `keep only Table 14-11.32.2` are handled locally without calling the LLM. Other LLM refinement calls retry rate-limit responses. If OpenAI quota or billing is unavailable, the chat returns a normal no-change response instead of failing the app.

When `OPENAI_API_KEY` is configured and **Use configured LLM** is enabled, the LLM receives the new shell text, local shell analysis, and retrieved knowledge-base examples. It returns structured JSON describing the clean outputs. If the LLM is unavailable, the app falls back to the local clean-shell builder.

For `.docx`, the app writes a clean Word document containing only the clean shell output paragraphs. For text/RTF, it writes a clean text/RTF copy.

## LLM-Assisted Generation

The generation workflow retrieves similar historical TLF examples from the local knowledge base and can send that context, the new shell, MDDT text, and ADaM dataset path to an OpenAI-compatible LLM.

Configure the OpenAI key before starting the app. The easiest local option is to copy `local.env.example` to `local.env` and fill in your key:

```text
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
```

`local.env` is ignored by git and is not included in the packaged zip source list. You can also set environment variables directly before starting the app:

```powershell
set OPENAI_API_KEY=your_api_key
set OPENAI_MODEL=gpt-4o-mini
set OPENAI_BASE_URL=https://api.openai.com/v1
```

`OPENAI_API_KEY` is required for LLM generation. `OPENAI_MODEL` and `OPENAI_BASE_URL` are optional. If no LLM key is configured, the app falls back to the local rule-based generator and still saves the SAS program to the requested output folder.

In **Generate SAS**, provide:

- **ADaM Dataset Path**: ADaM dataset folder for the new study.
- **SAS Program Output Folder**: required folder where the generated `.sas` program will be written.
- **Shell File** or **Shell Document Path**.
- **MDDT File** or **MDDT Document Path**, optional but recommended.
- **Use configured LLM when available**: enabled by default.

## Bulk Build From Output Directory

Many production TLF outputs print the source SAS program path on the first page footer. The app can use that convention to automatically build the program library.

In **Knowledge Base > Scan Output Directory**, provide:

- **Study ID**: optional, but recommended.
- **Output Directory**: folder containing RTF/TXT/LST/HTML/PDF/XLSX/DOCX outputs.
- **Program Search Directories**: optional line-separated folders to search when a printed path is stale or relative.
- **Dataset Path**: optional ADaM/SDTM/source dataset folder associated with the scanned programs.
- **Shell Documents**: optional uploaded shell files. If multiple are supplied, the app tries to match by TLF number or output filename.
- **MDDT File**: optional uploaded metadata file. The app stores the original file bytes and extracted text with each scanned example.
- **Scan subfolders**: enabled by default.

The scanner:

1. Reads only the first page of each supported output for pairing.
2. Extracts the TLF/TFL number and title from a first-page `Table`, `Listing`, `Figure`, `T`, `L`, or `F` header, with or without `:`, using up to five title lines after the number.
3. Extracts the `.sas` path from the `Program:` note.
4. Resolves relative paths against the output file location first.
5. Reads the matched SAS program and stores unique dataset references starting with `sdtm.`, `adam.`, or `adm.`.
6. Searches nearby folders and optional program search folders by SAS filename if needed.
7. Runs the scan as a background job and shows the current file being scanned in the UI.
8. Adds each matched program/output pair to the knowledge base immediately after that file finishes, before moving to the next file.
9. Reports created, skipped, and unmatched outputs.

## Clear the Local Database

Use **Clear Knowledge Base** in the top bar to delete local examples and generation history from the SQLite database. This does not delete files from disk; it only clears the app's indexed library.

## Stored Files

Shell and MDDT inputs are stored in the local SQLite database as file bytes, with extracted text used for search and generation context. Dataset locations remain paths because they usually refer to a folder of SAS datasets rather than a single uploaded document.

## Supported v1 File Parsing

- SAS/text: `.sas`, `.txt`, `.lst`, `.log`
- Output text: `.rtf`, `.html`
- Shell documents: `.docx`, `.xlsx`
- PDF: best-effort text extraction only

## Notes

- The MVP is dependency-light and uses Python standard library plus SQLite.
- Retrieval is local TF-IDF similarity, not model fine-tuning.
- Generation adapts the closest historical SAS program when available. If no prior program exists, it creates a runnable shell scaffold.
- Server-side SAS execution is disabled by default. Set `SAS_EXECUTABLE` to a SAS executable path if you want to extend `/api/run-sas`.

## Files

- `app.py`: backend, document readers, retrieval, generation, validation
- `static/index.html`: browser UI
- `static/app.js`: UI behavior and API calls
- `static/styles.css`: app styling
- `samples/`: seed and test files
- `data/`: local SQLite database, created at runtime
- `runs/`: optional SAS run artifacts
