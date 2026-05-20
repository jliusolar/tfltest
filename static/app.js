const state = {
  activeTab: "knowledge",
  currentShell: null,
  currentProgram: "",
  currentFilename: "generated_tlf.sas",
  currentCleanShell: null,
  cleanShellConversation: [],
  scanPoller: null,
  scanLastCreatedCount: -1,
  scanLastScannedSoFar: -1,
};

const pageMeta = {
  knowledge: {
    title: "Knowledge Base",
    subtitle: "Upload pairs or scan output folders to build the library.",
  },
  parseShell: {
    title: "Clean Shell Agent",
    subtitle: "Create a clean shell with original rows and expanded columns for each TFL.",
  },
  generate: {
    title: "New Study / Deliver Programs",
    subtitle: "Use ADaM data, shell, MDDT, LLM, and knowledge-base examples to create SAS programs.",
  },
  history: {
    title: "Run History",
    subtitle: "Review generated programs and validation outcomes.",
  },
};

document.addEventListener("DOMContentLoaded", () => {
  bindNavigation();
  bindForms();
  checkHealth();
  refreshExamples();
  refreshRuns();
  renderCleanShellChat();
});

function bindNavigation() {
  document.querySelectorAll(".nav-button").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
}

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".nav-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.remove("active");
  });
  document.getElementById(`${tab}Panel`).classList.add("active");
  document.getElementById("pageTitle").textContent = pageMeta[tab].title;
  document.getElementById("pageSubtitle").textContent = pageMeta[tab].subtitle;
  if (tab === "history") refreshRuns();
}

function bindForms() {
  document.getElementById("directoryScanForm").addEventListener("submit", scanDirectory);
  document.getElementById("exampleForm").addEventListener("submit", saveExample);
  document.getElementById("parseShellForm").addEventListener("submit", parseShellDocument);
  document.getElementById("cleanShellChatForm").addEventListener("submit", refineCleanShell);
  document.getElementById("generateForm").addEventListener("submit", generateProgram);
  document.getElementById("seedButton").addEventListener("click", seedSamples);
  document.getElementById("clearKbButton").addEventListener("click", clearKnowledgeBase);
  document.getElementById("validateButton").addEventListener("click", validateCurrentProgram);
  document.getElementById("downloadProgram").addEventListener("click", downloadProgram);
}

async function checkHealth() {
  const status = document.getElementById("healthStatus");
  try {
    const data = await apiGet("/api/health");
    const llmStatus = data.llm_configured ? `LLM ready: ${data.openai_model || "configured"}` : "LLM key not configured";
    status.textContent = `Backend online - v${data.version} - ${llmStatus}`;
  } catch (error) {
    status.textContent = "Backend is not reachable";
  }
}

async function parseShellDocument(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const status = document.getElementById("parseShellStatus");
  const shellDocumentPath = form.elements.shell_document_path.value.trim();
  const shellFile = form.elements.shell_file;
  const outputDir = form.elements.output_dir.value.trim();
  const topK = Number(form.elements.top_k.value || 5);
  status.textContent = "Reading shell document...";

  try {
    if (!shellDocumentPath && !shellFile.files.length) {
      throw new Error("Upload a shell file or provide the original shell document path.");
    }
    const result = await apiPost("/api/parse-shell", {
      shell_document_path: shellDocumentPath,
      output_dir: outputDir,
      top_k: topK,
      use_llm: form.elements.use_llm.checked,
      shell_file: await readFileInput(shellFile),
    });
    state.currentCleanShell = result;
    state.cleanShellConversation = [];
    renderParseShellResult(result);
    renderCleanShellChat();
    status.textContent = `Created ${result.clean_path}`;
  } catch (error) {
    status.textContent = `Error: ${error.message}`;
  }
}

function renderParseShellResult(result) {
  const summary = document.getElementById("parseShellSummary");
  const box = document.getElementById("parseShellResult");
  const preview = document.getElementById("parseShellPreview");
  summary.textContent =
    `${result.output_count || 0} clean output(s) - ${result.header_count || 0} header(s) found - ${result.retrieved_count || 0} retrieved example(s) - ${result.method || "created"} - saved to ${result.clean_path || ""}`;
  preview.textContent = result.preview || "";

  box.innerHTML = `
    <div class="scan-summary">
      <div class="scan-stat"><strong>${result.output_count || 0}</strong><span>Outputs</span></div>
      <div class="scan-stat"><strong>${result.header_count || 0}</strong><span>Headers</span></div>
      <div class="scan-stat"><strong>${result.retrieved_count || 0}</strong><span>Examples</span></div>
    </div>
    <div class="scan-current">
      <strong>Clean document</strong>
      <div class="scan-path">${escapeHtml(result.clean_path || "")}</div>
      <div class="scan-path">Source: ${escapeHtml(result.source_path || result.source_name || "")}</div>
    </div>
  `;

  if (result.method) {
    const method = document.createElement("div");
    method.className = "retrieval-meta";
    method.textContent = `Creation method: ${result.method}`;
    box.appendChild(method);
  }

  (result.outputs || []).forEach((output) => {
    const element = document.createElement("div");
    element.className = "retrieval-item";
    element.innerHTML = `
      <div class="retrieval-title">
        <span>${escapeHtml(output.tfl_number || "Output")} ${escapeHtml(output.title || "")}</span>
        <span class="badge">${escapeHtml(output.tfl_type || "tfl")}</span>
      </div>
      <div class="retrieval-meta">Header ${escapeHtml(output.header_id || "auto")} - ${escapeHtml(String(output.column_count || 0))} column(s) - ${escapeHtml(String(output.row_count || 0))} row line(s)${output.template_source ? ` - ${escapeHtml(output.template_source)}` : ""}</div>
    `;
    box.appendChild(element);
  });

  (result.headers || []).forEach((header) => {
    const element = document.createElement("div");
    element.className = "retrieval-item";
    element.innerHTML = `
      <div class="retrieval-title">
        <span>Header ${escapeHtml(header.id)}</span>
        <span class="badge">${escapeHtml(String(header.line_count || 0))} line(s)</span>
      </div>
      <div class="retrieval-meta">${escapeHtml(header.preview || "")}</div>
    `;
    box.appendChild(element);
  });

  (result.retrieved || []).slice(0, 5).forEach((example) => {
    const element = document.createElement("div");
    element.className = "scan-result";
    element.innerHTML = `
      <div class="retrieval-title">
        <span>Example ${escapeHtml(example.tlf_number || "")} ${escapeHtml(example.title || "")}</span>
        <span class="badge">${Number(example.score || 0).toFixed(3)}</span>
      </div>
      <div class="retrieval-meta">${escapeHtml(example.study_id || "")} - ${escapeHtml(example.tlf_type || "")}</div>
    `;
    box.appendChild(element);
  });

  (result.applications || []).slice(0, 20).forEach((item) => {
    const element = document.createElement("div");
    element.className = "scan-result";
    element.innerHTML = `
      <div class="retrieval-title">
        <span>Line ${escapeHtml(item.line || "")}: Header ${escapeHtml(item.header_id || "")}</span>
        ${statusBadge("applied")}
      </div>
      <div>${escapeHtml(item.context || "")}</div>
    `;
    box.appendChild(element);
  });

  (result.warnings || []).forEach((warning) => {
    const element = document.createElement("div");
    element.className = "finding warning";
    element.innerHTML = `<strong>Warning</strong><br />${escapeHtml(warning)}`;
    box.appendChild(element);
  });
}

async function refineCleanShell(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const status = document.getElementById("cleanShellChatStatus");
  const button = document.getElementById("cleanShellChatButton");
  const instruction = form.elements.instruction.value.trim();

  try {
    if (!state.currentCleanShell) {
      throw new Error("Create a clean shell before using the refinement chat.");
    }
    if (!instruction) {
      throw new Error("Enter a refinement prompt.");
    }
    button.disabled = true;
    status.textContent = "Applying refinement...";
    const result = await apiPost("/api/refine-clean-shell", {
      clean_path: state.currentCleanShell.clean_path || "",
      source_name: state.currentCleanShell.source_name || "",
      clean_text: document.getElementById("parseShellPreview").textContent || "",
      instruction,
      conversation: state.cleanShellConversation,
    });

    state.currentCleanShell = {
      ...state.currentCleanShell,
      clean_path: result.clean_path || state.currentCleanShell.clean_path,
      preview: result.preview || "",
      method: result.method || state.currentCleanShell.method,
      warnings: result.warnings || state.currentCleanShell.warnings || [],
    };
    state.cleanShellConversation = result.conversation || [
      ...state.cleanShellConversation,
      { role: "user", content: instruction },
      { role: "assistant", content: result.message || "Applied the requested refinement." },
    ];

    document.getElementById("parseShellPreview").textContent = result.preview || "";
    document.getElementById("parseShellSummary").textContent =
      `Fine-tuned with ${result.method || "LLM"} - ${result.line_count || 0} line(s) - saved to ${result.clean_path || state.currentCleanShell.clean_path || ""}`;
    form.reset();
    renderCleanShellChat();
    status.textContent =
      result.method === "llm_refine_unavailable_no_change"
        ? "OpenAI quota unavailable; no changes made."
        : "Updated clean shell.";
  } catch (error) {
    status.textContent = `Error: ${error.message}`;
  } finally {
    button.disabled = !state.currentCleanShell;
  }
}

function renderCleanShellChat() {
  const log = document.getElementById("cleanShellChatLog");
  const button = document.getElementById("cleanShellChatButton");
  if (!state.currentCleanShell) {
    log.innerHTML = `<div class="chat-empty">Create a clean shell to start a refinement conversation.</div>`;
    button.disabled = true;
    return;
  }
  button.disabled = false;
  if (!state.cleanShellConversation.length) {
    log.innerHTML = `<div class="chat-empty">No refinement prompts yet.</div>`;
    return;
  }
  log.innerHTML = "";
  state.cleanShellConversation.forEach((item) => {
    const message = document.createElement("div");
    message.className = `chat-message ${item.role === "assistant" ? "assistant" : "user"}`;
    message.innerHTML = `
      <div class="chat-role">${item.role === "assistant" ? "LLM" : "User"}</div>
      <div>${escapeHtml(item.content || "")}</div>
    `;
    log.appendChild(message);
  });
}

async function saveExample(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const status = document.getElementById("exampleStatus");
  status.textContent = "Reading files...";

  try {
    const payload = {
      metadata: collectFormMetadata(form),
      program_file: await readFileInput(form.elements.program_file),
      output_file: await readFileInput(form.elements.output_file),
      shell_file: await readFileInput(form.elements.shell_file),
      mddt_file: await readFileInput(form.elements.mddt_file),
    };
    status.textContent = "Saving...";
    await apiPost("/api/examples", payload);
    form.reset();
    status.textContent = "Saved.";
    await refreshExamples();
  } catch (error) {
    status.textContent = `Error: ${error.message}`;
  }
}

async function scanDirectory(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const status = document.getElementById("scanStatus");
  status.textContent = "Starting scan...";
  const scanId = createScanId();

  try {
    const payload = {
      scan_id: scanId,
      study_id: form.elements.study_id.value.trim(),
      output_dir: form.elements.output_dir.value.trim(),
      program_dirs: form.elements.program_dirs.value.trim(),
      dataset_path: form.elements.dataset_path.value.trim(),
      shell_document_files: await readFileListInput(form.elements.shell_document_files),
      mddt_file: await readFileInput(form.elements.mddt_file),
      recursive: form.elements.recursive.checked,
      max_files: Number(form.elements.max_files.value || 1000),
    };
    const result = await apiPost("/api/start-output-scan", payload);
    startScanPolling(scanId, result);
    status.textContent = "Scan running...";
  } catch (error) {
    status.textContent = `Error: ${error.message}`;
    stopScanPolling();
  }
}

async function generateProgram(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const status = document.getElementById("generateStatus");
  status.textContent = "Reading shell...";

  try {
    if (!form.elements.shell_file.files.length && !form.elements.shell_document_path.value.trim()) {
      throw new Error("Upload a shell file or provide a Shell Document Path.");
    }
    if (!form.elements.program_output_dir.value.trim()) {
      throw new Error("Provide a SAS Program Output Folder.");
    }
    const payload = {
      metadata: collectFormMetadata(form),
      top_k: Number(form.elements.top_k.value || 5),
      use_llm: form.elements.use_llm.checked,
      shell_file: await readFileInput(form.elements.shell_file),
      mddt_file: await readFileInput(form.elements.mddt_file),
    };
    status.textContent = "Retrieving and generating...";
    const result = await apiPost("/api/generate", payload);
    state.currentShell = result.shell;
    state.currentProgram = result.program;
    state.currentFilename = outputFilename(result.shell);
    renderGenerationResult(result);
    status.textContent = `Run ${result.run_id} generated.`;
    refreshRuns();
  } catch (error) {
    status.textContent = `Error: ${error.message}`;
  }
}

function collectFormMetadata(form) {
  const metadata = {};
  const fields = [
    "study_id",
    "tlf_number",
    "tlf_type",
    "title",
    "population",
    "endpoint",
    "source_datasets",
    "dataset_path",
    "program_output_dir",
    "shell_document_path",
    "mddt_path",
    "macros",
    "notes",
  ];
  fields.forEach((field) => {
    if (form.elements[field]) metadata[field] = form.elements[field].value.trim();
  });
  return metadata;
}

async function readFileInput(input) {
  const file = input.files && input.files[0];
  if (!file) return null;
  const buffer = await file.arrayBuffer();
  return {
    name: file.name,
    type: file.type,
    content_base64: arrayBufferToBase64(buffer),
  };
}

async function readFileListInput(input) {
  const files = Array.from((input && input.files) || []);
  const encoded = [];
  for (const file of files) {
    const buffer = await file.arrayBuffer();
    encoded.push({
      name: file.name,
      type: file.type,
      content_base64: arrayBufferToBase64(buffer),
    });
  }
  return encoded;
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return btoa(binary);
}

async function refreshExamples() {
  const body = document.getElementById("examplesBody");
  const count = document.getElementById("exampleCount");
  try {
    const data = await apiGet("/api/examples");
    body.innerHTML = "";
    count.textContent = `${data.examples.length} example${data.examples.length === 1 ? "" : "s"} loaded.`;
    if (!data.examples.length) {
      body.innerHTML = `<tr><td colspan="7">No examples yet. Load the sample, scan an output directory, or upload a historical pair.</td></tr>`;
      return;
    }
    data.examples.forEach((example) => {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td>${escapeHtml(example.study_id)}</td>
        <td>${escapeHtml(example.tlf_number)}</td>
        <td>${escapeHtml(example.tlf_type)}</td>
        <td>${escapeHtml(example.title)}</td>
        <td>${escapeHtml(example.source_datasets)}</td>
        <td>${escapeHtml(example.dataset_path)}</td>
        <td>${escapeHtml(contextLabel(example))}</td>
      `;
      body.appendChild(row);
    });
  } catch (error) {
    body.innerHTML = `<tr><td colspan="7">Could not load examples: ${escapeHtml(error.message)}</td></tr>`;
  }
}

async function clearKnowledgeBase() {
  const confirmed = window.confirm(
    "Clear all knowledge base examples and generation history from the local database?"
  );
  if (!confirmed) return;
  const button = document.getElementById("clearKbButton");
  button.disabled = true;
  button.textContent = "Clearing...";
  try {
    const result = await apiPost("/api/clear-knowledge-base", {});
    await refreshExamples();
    await refreshRuns();
    document.getElementById("scanReport").classList.remove("active");
    button.textContent = `Cleared ${result.examples_deleted}`;
  } catch (error) {
    button.textContent = "Clear Failed";
    alert(`Could not clear knowledge base: ${error.message}`);
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      button.textContent = "Clear Knowledge Base";
    }, 1800);
  }
}

function contextLabel(example) {
  const parts = [];
  if (example.shell_file_stored || example.shell_name || example.shell_document_path) parts.push("Shell");
  if (example.mddt_file_stored || example.mddt_name || example.mddt_path) parts.push("MDDT");
  return parts.join(" + ");
}

function renderScanReport(result) {
  const report = document.getElementById("scanReport");
  report.classList.add("active");
  const rows = (result.results || []).slice(0, 100);
  report.innerHTML = `
    <div class="scan-summary">
      <div class="scan-stat"><strong>${result.scanned_count}</strong><span>Scanned</span></div>
      <div class="scan-stat"><strong>${result.matched_count}</strong><span>Matched</span></div>
      <div class="scan-stat"><strong>${result.created_count}</strong><span>Created</span></div>
      <div class="scan-stat"><strong>${result.unmatched_count}</strong><span>Unmatched</span></div>
    </div>
    <div class="retrieval-meta">Program search roots: ${escapeHtml((result.program_roots || []).join("; "))}</div>
    <div class="retrieval-meta">Matching rule: first page only, using the Program: path note.</div>
  `;

  rows.forEach((item) => {
    const element = document.createElement("div");
    element.className = "scan-result";
    element.innerHTML = `
      <div class="retrieval-title">
        <span>${escapeHtml(item.tlf_number || item.output_name || "Output")}</span>
        ${statusBadge(item.status)}
      </div>
      <div>${escapeHtml(item.title || item.message || "")}</div>
      <div class="scan-path">Datasets: ${escapeHtml(item.source_datasets || "")}</div>
      <div class="scan-path">Output: ${escapeHtml(item.output_path || "")}</div>
      <div class="scan-path">Program: ${escapeHtml(item.program_path || "Not resolved")}</div>
    `;
    report.appendChild(element);
  });

  if ((result.results || []).length > rows.length) {
    const more = document.createElement("div");
    more.className = "retrieval-meta";
    more.textContent = `${result.results.length - rows.length} additional result(s) omitted from the on-screen report.`;
    report.appendChild(more);
  }
}

function startScanPolling(scanId, initialProgress = null) {
  stopScanPolling();
  state.scanLastCreatedCount = -1;
  state.scanLastScannedSoFar = -1;
  renderScanProgress(
    initialProgress || {
      status: "starting",
      current_file: "",
      current_file_name: "",
      scanned_so_far: 0,
      total_files: 0,
      created_count: 0,
      matched_count: 0,
      skipped_count: 0,
      unmatched_count: 0,
      results: [],
    }
  );
  state.scanPoller = window.setInterval(async () => {
    try {
      const progress = await apiGet(`/api/scan-progress/${encodeURIComponent(scanId)}`);
      renderScanProgress(progress);
      updateScanStatus(progress);
      const createdCount = progress.created_count || 0;
      const scannedSoFar = progress.scanned_so_far || 0;
      if (
        createdCount !== state.scanLastCreatedCount ||
        scannedSoFar !== state.scanLastScannedSoFar
      ) {
        state.scanLastCreatedCount = createdCount;
        state.scanLastScannedSoFar = scannedSoFar;
        refreshExamples();
      }
      if (progress.status === "completed" || progress.status === "failed") {
        stopScanPolling();
        if (progress.final_result) renderScanReport(progress.final_result);
        refreshExamples();
      }
    } catch (error) {
      document.getElementById("scanStatus").textContent = `Progress error: ${error.message}`;
    }
  }, 400);
}

function stopScanPolling() {
  if (!state.scanPoller) return;
  window.clearInterval(state.scanPoller);
  state.scanPoller = null;
}

function renderScanProgress(progress) {
  const report = document.getElementById("scanReport");
  report.classList.add("active");
  const rows = (progress.results || []).slice(-12).reverse();
  const scanned = progress.scanned_so_far || 0;
  const total = progress.total_files || 0;
  const currentLabel =
    progress.status === "completed"
      ? "Scan complete"
      : progress.status === "queued" ||
          progress.status === "discovering_outputs" ||
          progress.status === "indexing_programs"
        ? "Scan status"
        : "Currently scanning";
  const currentFile = progress.current_file_name || progress.current_file || "Preparing scan...";
  report.innerHTML = `
    <div class="scan-summary">
      <div class="scan-stat"><strong>${scanned}${total ? `/${total}` : ""}</strong><span>Scanned</span></div>
      <div class="scan-stat"><strong>${progress.matched_count || 0}</strong><span>Matched</span></div>
      <div class="scan-stat"><strong>${progress.created_count || 0}</strong><span>Created</span></div>
      <div class="scan-stat"><strong>${progress.unmatched_count || 0}</strong><span>Unmatched</span></div>
    </div>
    <div class="scan-current">
      <strong>${escapeHtml(currentLabel)}</strong>
      <div>${escapeHtml(currentFile)}</div>
      <div class="scan-path">${escapeHtml(progress.current_file || "")}</div>
      ${progress.error ? `<div class="scan-path">Error: ${escapeHtml(progress.error)}</div>` : ""}
    </div>
  `;

  rows.forEach((item) => {
    const element = document.createElement("div");
    element.className = "scan-result";
    element.innerHTML = `
      <div class="retrieval-title">
        <span>${escapeHtml(item.output_name || "Output")}</span>
        ${statusBadge(item.status)}
      </div>
      <div>${escapeHtml(item.title || item.message || "")}</div>
      <div class="scan-path">Datasets: ${escapeHtml(item.source_datasets || "")}</div>
      <div class="scan-path">Program: ${escapeHtml(item.program_path || "Not resolved")}</div>
    `;
    report.appendChild(element);
  });
}

function updateScanStatus(progress) {
  const status = document.getElementById("scanStatus");
  if (progress.status === "completed") {
    status.textContent = `Completed. Created ${progress.created_count || 0}, matched ${progress.matched_count || 0}.`;
  } else if (progress.status === "failed") {
    status.textContent = `Failed: ${progress.error || "scan error"}`;
  } else if (progress.status === "queued") {
    status.textContent = "Scan queued...";
  } else if (progress.status === "discovering_outputs") {
    status.textContent = "Finding output files...";
  } else if (progress.status === "indexing_programs") {
    status.textContent = `Indexing SAS programs for ${progress.total_files || 0} output file(s)...`;
  } else {
    const scanned = progress.scanned_so_far || 0;
    const total = progress.total_files || 0;
    const file = progress.current_file_name || "preparing...";
    status.textContent = total ? `Scanning ${scanned + 1 > total ? total : scanned + 1}/${total}: ${file}` : "Preparing scan...";
  }
}

function createScanId() {
  if (window.crypto && window.crypto.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `scan-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function refreshRuns() {
  const body = document.getElementById("runsBody");
  try {
    const data = await apiGet("/api/runs");
    body.innerHTML = "";
    if (!data.runs.length) {
      body.innerHTML = `<tr><td colspan="6">No generation runs yet.</td></tr>`;
      return;
    }
    data.runs.forEach((run) => {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td>${run.id}</td>
        <td>${escapeHtml(run.tlf_number)}</td>
        <td>${escapeHtml(run.title)}</td>
        <td>${statusBadge(run.status)}</td>
        <td>${run.retrieved_count}</td>
        <td>${escapeHtml(formatDate(run.created_at))}</td>
      `;
      row.addEventListener("click", () => openRun(run.id));
      body.appendChild(row);
    });
  } catch (error) {
    body.innerHTML = `<tr><td colspan="6">Could not load runs: ${escapeHtml(error.message)}</td></tr>`;
  }
}

async function openRun(id) {
  try {
    const run = await apiGet(`/api/runs/${id}`);
    state.currentShell = run.shell;
    state.currentProgram = run.program;
    state.currentFilename = outputFilename(run.shell);
    renderGenerationResult(run);
    switchTab("generate");
  } catch (error) {
    alert(`Could not open run: ${error.message}`);
  }
}

function renderGenerationResult(result) {
  document.getElementById("parsedShell").textContent = JSON.stringify(result.shell, null, 2);
  document.getElementById("programEditor").value = result.program;
  document.getElementById("runSummary").textContent =
    `${result.shell.tlf_type || "TLF"} ${result.shell.tlf_number || ""} - ${result.retrieved.length} retrieved example(s) - ${result.generation_method || "generated"}${result.program_path ? ` - saved to ${result.program_path}` : ""}`;
  renderRetrieval(result.retrieved || []);
  renderValidation(result.validation || {});
}

function renderRetrieval(items) {
  const list = document.getElementById("retrievalList");
  list.innerHTML = "";
  if (!items.length) {
    list.textContent = "No matching examples found yet.";
    return;
  }
  items.forEach((item) => {
    const element = document.createElement("div");
    element.className = "retrieval-item";
    element.innerHTML = `
      <div class="retrieval-title">
        <span>${escapeHtml(item.tlf_number || "TLF")} - ${escapeHtml(item.title || "Untitled")}</span>
        <span class="badge">${Number(item.score || 0).toFixed(3)}</span>
      </div>
      <div class="retrieval-meta">
        ${escapeHtml(item.study_id || "")} - ${escapeHtml(item.tlf_type || "")} - ${escapeHtml(item.source_datasets || "")}
      </div>
    `;
    list.appendChild(element);
  });
}

async function validateCurrentProgram() {
  const editor = document.getElementById("programEditor");
  const log = document.getElementById("logInput").value;
  const program = editor.value;
  state.currentProgram = program;
  const validation = await apiPost("/api/validate", {
    program,
    log,
    shell: state.currentShell || {},
  });
  renderValidation(validation);
}

function renderValidation(validation) {
  const box = document.getElementById("validationBox");
  const findings = validation.findings || [];
  const status = validation.status || "not_run";
  box.innerHTML = `<div class="retrieval-title"><span>Validation</span>${statusBadge(status)}</div>`;
  if (validation.run_capability) {
    const note = document.createElement("div");
    note.className = "retrieval-meta";
    note.textContent = validation.run_capability;
    box.appendChild(note);
  }
  if (!findings.length) {
    const clean = document.createElement("div");
    clean.className = "finding info";
    clean.innerHTML = "<strong>No findings.</strong><br />Static checks did not detect issues.";
    box.appendChild(clean);
    return;
  }
  findings.forEach((finding) => {
    const item = document.createElement("div");
    item.className = `finding ${finding.severity}`;
    item.innerHTML = `<strong>${escapeHtml(finding.title)}</strong><br />${escapeHtml(finding.detail)}`;
    box.appendChild(item);
  });
}

async function seedSamples() {
  const button = document.getElementById("seedButton");
  button.disabled = true;
  button.textContent = "Loading...";
  try {
    const result = await apiPost("/api/seed", {});
    await refreshExamples();
    button.textContent = result.created ? "Sample Loaded" : "Sample Present";
  } catch (error) {
    button.textContent = "Load Failed";
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      button.textContent = "Load Sample";
    }, 1800);
  }
}

function downloadProgram() {
  const program = document.getElementById("programEditor").value || state.currentProgram;
  if (!program) return;
  const blob = new Blob([program], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = state.currentFilename || "generated_tlf.sas";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function outputFilename(shell) {
  const type = shell?.tlf_type || "table";
  const prefix = { table: "t", listing: "l", figure: "f" }[type] || "t";
  const number = (shell?.tlf_number || "x_x_x").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return `${prefix}_${number || "x_x_x"}.sas`;
}

async function apiGet(path) {
  const response = await fetch(path, { cache: "no-store" });
  return parseResponse(response);
}

async function apiPost(path, body) {
  const response = await fetch(path, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parseResponse(response);
}

async function parseResponse(response) {
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.message || data.error || response.statusText);
  }
  return data;
}

function statusBadge(status) {
  const safe = status || "not_run";
  return `<span class="badge ${escapeHtml(safe)}">${escapeHtml(safe.replaceAll("_", " "))}</span>`;
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
