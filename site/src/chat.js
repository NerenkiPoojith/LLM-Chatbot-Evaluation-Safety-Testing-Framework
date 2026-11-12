// Chat page: ask questions, drop in a manual, choose which manual answers come from.
// Every string from the server or the user is inserted with textContent, never as HTML.
(() => {
  const $ = (id) => document.getElementById(id);
  const transcript = $("transcript");
  const welcome = $("welcome");
  const form = $("composer");
  const input = $("question");
  const sendBtn = $("send");
  const fileInput = $("file-input");
  const overlay = $("drop-overlay");
  const scopeBtn = $("scope-btn");
  const scopePanel = $("scope-panel");
  const scopeOptions = $("scope-options");
  const scopeValue = $("scope-value");
  const modeEl = $("mode");
  const modeNote = $("mode-note");
  const banner = $("server-banner");
  if (!transcript || !form) return;
  const scopeBox = scopeBtn.closest(".scope");
  const dropHint = document.querySelector(".dropcard-hint");

  // Any file may be dropped: the server recognises the format from its content and explains what it can't read.
  const FORMATS_HINT = "PDF, Word, slides, spreadsheets, web pages, e-books, scans and photos";
  const OCR_SECONDS_PER_PAGE = 6;
  let maxMb = 20; // replaced by the server's own limit (MAX_UPLOAD_MB) once /health answers
  let manuals = [];
  let scope = ""; // "" = all manuals, otherwise a manual_id
  let busy = false;
  let serverUp = false;

  // ------------------------------------------------------------ helpers
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };

  const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  const icon = (kind) => {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 22 22");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(ns, "path");
    const shapes = {
      doc: "M6 2h7l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Zm7 0v5h5M8 12h6M8 15.5h6",
      ok: "M11 2a9 9 0 1 1 0 18 9 9 0 0 1 0-18Zm-4 9.2 2.8 2.8 5.4-5.8",
      warn: "M11 3 2 19h18L11 3Zm0 6v4.5m0 3v.2",
    };
    path.setAttribute("d", shapes[kind] || shapes.doc);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.7");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
    return svg;
  };

  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(path, options);
    } catch {
      // The server stopped after the page loaded. serverUp stays true, so a restarted server works without a reload.
      showServerDown();
      throw new Error("Can't reach the chatbot server. Is it still running?");
    }
    banner.hidden = true;
    let body = null;
    if (response.status !== 204) {
      try { body = await response.json(); } catch { body = null; }
    }
    if (!response.ok) {
      let detail = body && body.detail;
      if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join(" ");
      throw new Error(detail || `The server answered with an error (${response.status}).`);
    }
    return body;
  }

  // The conversation scrolls inside the transcript, except on very short windows, where the whole page scrolls (chat.css).
  const scrollToEnd = () => {
    transcript.scrollTop = transcript.scrollHeight;
    window.scrollTo(0, document.documentElement.scrollHeight);
  };

  // Hiding the welcome block must not strand keyboard focus on the drop card or a chip that just disappeared.
  function startConversation() {
    if (welcome.hidden) return;
    const hadFocus = welcome.contains(document.activeElement);
    welcome.hidden = true;
    if (hadFocus) input.focus();
  }

  function addMessage(className) {
    startConversation();
    const node = el("div", `msg ${className}`);
    transcript.appendChild(node);
    scrollToEnd();
    return node;
  }

  function systemMessage(kind, text, { error = false } = {}) {
    const node = addMessage(`msg-system${error ? " msg-error" : ""}`);
    node.appendChild(kind === "spinner" ? el("span", "spinner") : icon(kind));
    const body = el("div");
    body.appendChild(el("p", null, text));
    node.appendChild(body);
    return { node, body };
  }

  // aria-disabled rather than disabled: a disabled button cannot keep keyboard focus, so focus would fall to the page.
  function setBusy(value) {
    busy = value;
    sendBtn.setAttribute("aria-disabled", String(value));
    form.setAttribute("aria-busy", String(value));
  }

  // ------------------------------------------------------------ server status and manuals
  // Moving focus to the banner makes screen readers read it again when it is already showing.
  function showServerDown(focus = false) {
    banner.hidden = false;
    if (focus) banner.focus();
  }

  async function init() {
    if (location.protocol === "file:") return showServerDown();
    try {
      const health = await api("health");
      serverUp = true;
      if (Number.isInteger(health.max_upload_mb) && health.max_upload_mb > 0) maxMb = health.max_upload_mb;
      overlay.querySelector(".drop-overlay-hint").textContent = FORMATS_HINT;
      dropHint.textContent = `${FORMATS_HINT}, up to ${maxMb} MB`;
      const claude = health.llm_provider === "claude";
      modeEl.textContent = claude ? "Claude mode" : "Offline mode";
      modeEl.hidden = false;
      modeNote.textContent = claude
        ? `Claude mode: answers are written by ${health.chat_model} from the manual text.`
        : "Offline mode: answers quote the manual word for word.";
      await loadManuals();
    } catch {
      showServerDown();
    }
  }

  async function loadManuals() {
    manuals = await api("v1/manuals");
    if (scope && !manuals.some((m) => m.manual_id === scope)) scope = "";
    renderScope();
  }

  const manualById = (id) => manuals.find((m) => m.manual_id === id);

  function setScope(id) {
    scope = id || "";
    const manual = manualById(scope);
    scopeValue.textContent = manual ? manual.title : "All manuals";
    scopeBtn.title = manual ? `Answers come only from ${manual.title}` : "Answers can come from any manual";
    for (const radio of scopeOptions.querySelectorAll("input[type=radio]")) radio.checked = radio.value === scope;
  }

  // Planted risk documents are indexed for the safety tests; they get their own group so they never pass as sample manuals.
  function renderScope() {
    scopeOptions.replaceChildren(scopeOption({ manual_id: "", title: "All manuals", meta: "Search every manual" }));
    const groups = [
      ["Your manuals", manuals.filter((m) => m.uploaded)],
      ["Sample manuals", manuals.filter((m) => !m.uploaded && !m.planted)],
      ["Test documents (planted risks)", manuals.filter((m) => !m.uploaded && m.planted).map((m) => ({ ...m, meta: "Used by the safety tests" }))],
    ];
    groups.forEach(([caption, items], index) => {
      if (!items.length) return;
      const group = el("div");
      const heading = el("p", "scope-group", caption);
      heading.id = `scope-group-${index}`;
      group.setAttribute("role", "group");
      group.setAttribute("aria-labelledby", heading.id);
      group.append(heading, ...items.map(scopeOption));
      scopeOptions.appendChild(group);
    });
    setScope(scope);
  }

  // The Remove button sits beside the label, not inside it, so it is not read as part of the option's name.
  function scopeOption(manual) {
    const option = el("div", "scope-option");
    const label = el("label");
    const radio = el("input");
    radio.type = "radio";
    radio.name = "scope";
    radio.value = manual.manual_id;
    radio.addEventListener("change", () => setScope(radio.value));
    // Arrow keys and Space only move the choice (their clicks have detail 0); a pointer click or Enter confirms it.
    radio.addEventListener("click", (event) => { if (event.detail > 0) closeScope(true); });
    const text = el("span", "scope-option-text");
    text.appendChild(el("span", "scope-option-title", manual.title));
    const meta = manual.meta || [manual.pages ? count(manual.pages, "page") : null, count(manual.chunks, "section")].filter(Boolean).join(", ");
    text.appendChild(el("span", "scope-option-meta", meta));
    label.append(radio, text);
    option.appendChild(label);
    if (manual.uploaded) option.appendChild(removeButton(manual));
    return option;
  }

  function removeButton(manual) {
    const button = el("button", "remove-btn", "Remove");
    button.type = "button";
    button.setAttribute("aria-label", `Remove ${manual.title}`);
    let timer = null;
    const disarm = () => {
      clearTimeout(timer);
      button.classList.remove("is-confirming");
      button.textContent = "Remove";
      button.setAttribute("aria-label", `Remove ${manual.title}`);
    };
    button.addEventListener("click", async () => {
      if (!button.classList.contains("is-confirming")) {
        button.classList.add("is-confirming");
        button.textContent = "Confirm";
        button.setAttribute("aria-label", `Confirm removing ${manual.title}`);
        timer = setTimeout(disarm, 8000);
        return;
      }
      clearTimeout(timer);
      try {
        await api(`v1/manuals/${encodeURIComponent(manual.manual_id)}`, { method: "DELETE" });
        const wasScope = scope === manual.manual_id;
        if (wasScope) scope = "";
        await loadManuals();
        systemMessage("doc", `Removed “${manual.title}”.${wasScope ? " Questions now search all manuals." : ""}`);
      } catch (error) {
        disarm();
        systemMessage("warn", `Couldn't remove the manual: ${error.message}`, { error: true });
      }
      closeScope(true); // the panel would cover the message, and the button that had focus may be gone
    });
    return button;
  }

  function openScope() {
    scopePanel.hidden = false;
    scopeBtn.setAttribute("aria-expanded", "true");
    const checked = scopeOptions.querySelector("input:checked") || scopeOptions.querySelector("input");
    if (checked) checked.focus();
  }

  function closeScope(returnFocus) {
    if (scopePanel.hidden) return;
    scopePanel.hidden = true;
    scopeBtn.setAttribute("aria-expanded", "false");
    if (returnFocus) scopeBtn.focus();
  }

  scopeBtn.addEventListener("click", () => (scopePanel.hidden ? openScope() : closeScope(false)));
  document.addEventListener("click", (event) => {
    if (!scopePanel.hidden && !scopeBox.contains(event.target)) closeScope(false);
  });
  // Listening on .scope, not the panel, so Escape also works while the scope button has focus.
  scopeBox.addEventListener("keydown", (event) => {
    if (scopePanel.hidden) return;
    if (event.key === "Escape") {
      event.stopPropagation();
      closeScope(true);
    } else if (event.key === "Enter" && event.target.matches("input[type=radio]")) {
      event.preventDefault(); // otherwise Enter on a radio submits the question form
      closeScope(true);
    }
  });
  scopePanel.addEventListener("focusout", (event) => {
    if (event.relatedTarget && !scopeBox.contains(event.relatedTarget)) closeScope(false);
  });

  // ------------------------------------------------------------ asking
  function renderAnswer(node, result) {
    node.replaceChildren(el("span", "visually-hidden", "Answer:"));
    const answer = result.answer || "";
    const abstained = /^(i couldn't find that|i'm sorry, i can only help)/i.test(answer.trim());
    node.classList.toggle("is-abstention", abstained);

    const answerEl = el("div", "answer");
    answer.split(/\n+/).filter(Boolean).forEach((line) => answerEl.appendChild(el("p", null, line)));
    node.appendChild(answerEl);

    const guard = result.guardrails || {};
    if (guard.input_flagged) {
      node.appendChild(notice("Blocked", "fail", "This looked like an attempt to override the chatbot's instructions, so it was not sent to the model."));
    }
    const dropped = guard.dropped_chunks || [];
    if (dropped.length) {
      const reasons = [...new Set(dropped.map((d) => (d.reason === "pii_dense" ? "contained customer records" : "contained instructions aimed at the AI")))];
      node.appendChild(notice("Ignored", "fail", `${count(dropped.length, "retrieved passage")} ${reasons.join(" or ")}.`));
    }
    if (guard.pii_redacted_in_answer) {
      node.appendChild(notice("Redacted", "fail", "Personal data was removed from this answer."));
    }

    const sources = result.sources || [];
    if (sources.length && !guard.blocked && !abstained) { // a "couldn't find" reply used no source
      const details = el("details", "sources");
      const first = sources[0];
      // Titles already contain dashes and commas, so the best match is written as "section (title)".
      const best = first.section ? `${first.section} (${first.title})` : first.title;
      details.appendChild(el("summary", null, `${count(sources.length, "source")} · best match: ${best}`));
      const list = el("ul", "source-list");
      sources.forEach((source) => {
        const item = el("li", "source-item");
        const head = el("div", "source-head");
        head.appendChild(el("span", null, `${source.title}${source.section ? ` — ${source.section}` : ""}`));
        head.appendChild(el("span", "source-score", `match ${Math.round(source.score * 100)}%`));
        item.appendChild(head);
        const text = (source.text || "").replace(/^\[[^\n]*\]\n/, ""); // drop the chunk's context header
        item.appendChild(el("p", "source-text", text));
        list.appendChild(item);
      });
      details.appendChild(list);
      node.appendChild(details);
    }
  }

  function notice(label, kind, text) {
    const box = el("div", "notice");
    box.appendChild(el("span", `stamp ${kind === "fail" ? "fail" : ""}`.trim(), label));
    box.appendChild(el("span", null, text));
    return box;
  }

  async function ask(question) {
    if (busy) return;
    if (!serverUp) return showServerDown(true);
    const userMsg = addMessage("msg-user");
    userMsg.append(el("span", "visually-hidden", "You: "), question);

    const botMsg = addMessage("msg-bot");
    const typing = el("span", "typing");
    typing.append(el("i"), el("i"), el("i"));
    botMsg.append(typing, el("span", "visually-hidden", "Searching the manuals…"));
    setBusy(true);
    try {
      const payload = { question };
      if (scope) payload.manual_id = scope;
      const result = await api("v1/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      renderAnswer(botMsg, result);
    } catch (error) {
      botMsg.replaceChildren(el("p", null, `Sorry, something went wrong: ${error.message}`));
      botMsg.classList.add("msg-error");
      if (/unknown manual_id/i.test(error.message)) loadManuals().catch(() => {});
    } finally {
      setBusy(false);
      scrollToEnd();
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question || busy) return;
    if (!serverUp) return showServerDown(true); // keep the typed question
    input.value = "";
    autosize();
    ask(question);
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`; // chat.css lowers the cap on short windows
  }
  input.addEventListener("input", autosize);

  for (const chip of document.querySelectorAll(".chip[data-question]")) {
    chip.addEventListener("click", () => {
      setScope(""); // the example questions are about the sample manuals
      input.focus(); // before ask(), which moves focus to the banner when the server is down
      ask(chip.dataset.question);
    });
  }

  // ------------------------------------------------------------ uploading
  // An upload does not block questions: the server answers them meanwhile, and the new manual joins the
  // index only once it is complete. Uploads run one after another, so the last "Questions now..." is true.
  let uploadQueue = Promise.resolve();

  const timeLeft = (seconds) => (seconds < 60
    ? "less than a minute left" : `about ${count(Math.round(seconds / 60), "minute")} left`);

  // Follows GET v1/manuals/progress/{id}. `status` is read out by screen readers, so it changes only when the
  // stage does (reading, text recognition, indexing); `counter` holds the page count, updated every 1.5 s,
  // and is not read out. The time left is measured on this computer once two pages are done.
  function followProgress(uploadId, name, status, counter) {
    let stage = "reading";
    let started = null; // [time, pages done] when text recognition was first seen
    return setInterval(async () => {
      try {
        const response = await fetch(`v1/manuals/progress/${uploadId}`);
        if (!response.ok) return;
        const { stage: now, done, total } = await response.json();
        if (now === "ocr" && total) {
          if (!started) started = [performance.now(), done];
          const measured = done - started[1] >= 2;
          const perPage = measured ? (performance.now() - started[0]) / 1000 / (done - started[1]) : OCR_SECONDS_PER_PAGE;
          const left = timeLeft((total - done) * perPage);
          if (stage !== "ocr") {
            status.textContent = total === 1
              ? `Reading the text in “${name}” with text recognition…`
              : `Reading ${total} scanned pages of “${name}” with text recognition, ${left}. You can keep asking questions meanwhile.`;
          }
          counter.textContent = total > 1 ? `Page ${done + 1} of ${total}, ${left}.` : "";
        } else if (now === "indexing" && stage !== "indexing") {
          status.textContent = `Indexing “${name}” (${count(total, "section")})…`;
          counter.textContent = "";
        }
        stage = now;
      } catch { /* the upload request itself reports a stopped server */ }
    }, 1500);
  }

  const newUploadId = () => (crypto.randomUUID ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`).toLowerCase();

  // Focus moves to the question box after an upload only if the user has not put it somewhere else meanwhile.
  const focusIsFree = () => [null, document.body, $("attach"), $("dropcard")].includes(document.activeElement);

  // Returns the added manual, or null. With several files the scope is set once, after the last one.
  async function upload(file, several = false) {
    const name = file.name || "manual";
    if (file.size > maxMb * 1024 * 1024) {
      systemMessage("warn", `“${name}” is larger than the ${maxMb} MB limit.`, { error: true });
      return null;
    }

    const { node, body } = systemMessage("spinner", `Reading “${name}”…`);
    const status = body.firstChild;
    const counter = el("p", "msg-note");
    counter.setAttribute("aria-live", "off"); // a page counter every 1.5 s would drown a screen reader
    body.appendChild(counter);
    const uploadId = newUploadId();
    const timer = followProgress(uploadId, name, status, counter);
    try {
      const formData = new FormData();
      formData.append("file", file, name);
      formData.append("upload_id", uploadId);
      const manual = await api("v1/manuals", { method: "POST", body: formData });
      clearInterval(timer);
      closeScope(false); // the list is about to be rebuilt; an open panel would cover the result
      await loadManuals();
      const slides = (manual.format || "").includes("presentation");
      const size = [manual.format, manual.pages ? count(manual.pages, slides ? "slide" : "page") : null,
        count(manual.chunks, "section")].filter(Boolean).join(", ");
      // A copy of a built-in manual is not "yours": it is one of the samples.
      const already = manual.uploaded ? `“${manual.title}” was already added.` : `“${manual.title}” is already one of the built-in manuals.`;
      const text = el("div");
      if (several) {
        text.appendChild(el("p", null, manual.duplicate ? already : `Added “${manual.title}” (${size}).`));
      } else {
        setScope(manual.manual_id);
        text.appendChild(el("p", null, manual.duplicate
          ? `${already} Questions now answer from it.`
          : `Added “${manual.title}” (${size}). Questions now answer from this manual only.`));
        const actions = el("p", "actions-inline");
        const allBtn = el("button", "btn btn-secondary btn-sm", "Search all manuals instead");
        allBtn.type = "button";
        allBtn.addEventListener("click", () => { setScope(""); input.focus(); });
        actions.appendChild(allBtn);
        text.appendChild(actions);
        if (focusIsFree()) input.focus();
      }
      if (manual.ocr_pages && !manual.duplicate) {
        text.appendChild(el("p", "msg-note", `${count(manual.ocr_pages, "page")} ${manual.ocr_pages === 1 ? "was" : "were"} `
          + "read from images with text recognition (OCR). It can misread a character, so check important numbers "
          + "against the manual."));
      }
      node.replaceChildren(icon("ok"), text);
      return manual;
    } catch (error) {
      node.replaceChildren(icon("warn"), el("div"));
      node.lastChild.appendChild(el("p", null, `Couldn't add “${name}”: ${error.message}`));
      node.classList.add("msg-error");
      return null;
    } finally {
      clearInterval(timer);
      scrollToEnd();
    }
  }

  // Several files are added one after another; questions then search all manuals.
  async function uploadBatch(files) {
    if (files.length === 1) return upload(files[0]);
    let added = 0;
    for (const file of files) if (await upload(file, true)) added += 1;
    if (added) {
      setScope("");
      systemMessage("doc", "Questions now search all manuals. To ask about one of them only, pick it in the “All manuals” menu below.");
    }
  }

  // Files dropped while another upload runs wait their turn.
  function uploadFiles(files) {
    if (!files.length) return uploadQueue;
    if (!serverUp) {
      showServerDown(true);
      return uploadQueue;
    }
    uploadQueue = uploadQueue.then(() => uploadBatch(files)).catch(() => {});
    return uploadQueue;
  }

  const pickFile = () => fileInput.click();
  $("attach").addEventListener("click", pickFile);
  $("dropcard").addEventListener("click", pickFile);
  fileInput.addEventListener("change", () => {
    const files = Array.from(fileInput.files);
    fileInput.value = "";
    uploadFiles(files);
  });

  // Drag a file anywhere over the page to drop it in.
  let dragDepth = 0;
  const hasFiles = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");
  window.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    overlay.hidden = false;
  });
  window.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  window.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) overlay.hidden = true;
  });
  window.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    overlay.hidden = true;
    uploadFiles(Array.from(event.dataTransfer.files));
  });

  init();
})();
