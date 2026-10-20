const $ = (id) => document.getElementById(id);

const form = $("dl-form");
const statusEl = $("status");
const statusText = $("status-text");
const statusCount = $("status-count");
const barFill = $("bar-fill");
const grid = $("grid");
const warnings = $("warnings");
const resultMeta = $("result-meta");
const filesEl = $("files");
const outPath = $("out-path");
const outputInput = $("output");

function setBusy(busy) {
  $("btn-scrape").disabled = busy;
  $("btn-download").disabled = busy;
  $("btn-browse").disabled = busy;
}

function showStatus(text, done = 0, total = 0) {
  statusEl.classList.remove("hidden");
  statusText.textContent = text;
  if (total > 0) {
    const pct = Math.round((done / total) * 100);
    barFill.style.width = `${pct}%`;
    statusCount.textContent = `${done}/${total}`;
  } else {
    barFill.style.width = "8%";
    statusCount.textContent = "";
  }
}

function setOutputPath(path) {
  if (!path) return;
  outputInput.value = path;
  outPath.textContent = path;
}

function payloadFromForm() {
  return {
    url: $("url").value.trim(),
    limit: Number($("limit").value || 40),
    quality: $("quality").value,
    workers: Number($("workers").value || 6),
    output: outputInput.value.trim() || null,
  };
}

function renderItems(data) {
  warnings.innerHTML = "";
  grid.innerHTML = "";
  const videoCount = data.video_count ?? (data.items || []).filter((i) => i.is_video || i.video_url).length;
  resultMeta.textContent = `${data.kind || "?"} · ${data.count ?? data.items?.length ?? 0} item(s)${videoCount ? ` · ${videoCount} video` : ""} · ${data.title || ""}`;
  (data.warnings || []).forEach((w) => {
    const d = document.createElement("div");
    d.className = "warning";
    d.textContent = w;
    warnings.appendChild(d);
  });
  (data.items || []).forEach((item) => {
    const tile = document.createElement("article");
    tile.className = "tile";
    const isVideo = !!(item.is_video || item.video_url);
    const img = document.createElement("img");
    img.loading = "lazy";
    const thumb = item.image_url || "";
    img.src = thumb
      ? thumb.replace("/originals/", "/474x/").replace("/1200x/", "/474x/")
      : "";
    img.alt = item.title || item.id || "pin";
    if (!thumb) {
      img.style.display = "none";
    }
    const meta = document.createElement("div");
    meta.className = "meta";
    const badges = [`<div class="qbadge">${escapeHtml(item.quality || "hq")}</div>`];
    if (isVideo) {
      badges.push(`<div class="qbadge video">VIDEO</div>`);
    }
    meta.innerHTML = `<strong title="${escapeHtml(item.title || item.id)}">${escapeHtml(item.title || item.id)}</strong>
      <span>${escapeHtml(item.id)}</span>
      ${badges.join("")}`;
    tile.appendChild(img);
    tile.appendChild(meta);
    grid.appendChild(tile);
  });
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/** Open native OS folder window (server-side tkinter on this machine). */
async function pickFolder() {
  showStatus("Opening folder window… choose where to save media");
  const res = await fetch("/api/pick-folder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      initial: outputInput.value.trim() || null,
      title: "Choose folder to save Pinterest media",
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "Could not open folder window");
  if (data.cancelled || !data.path) {
    showStatus("Folder selection cancelled");
    return null;
  }
  setOutputPath(data.path);
  showStatus(`Save folder: ${data.path}`);
  return data.path;
}

async function scrape() {
  const body = payloadFromForm();
  if (!body.url) return;
  setBusy(true);
  showStatus("Fetching page…");
  try {
    const res = await fetch("/api/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Scrape failed");
    renderItems(data);
    showStatus(`Preview ready · ${data.count} item(s)`, data.count, data.count);
  } catch (err) {
    showStatus(err.message || String(err));
  } finally {
    setBusy(false);
  }
}

async function pollJob(jobId) {
  while (true) {
    const res = await fetch(`/api/jobs/${jobId}`);
    const job = await res.json();
    if (!res.ok) throw new Error(job.detail || "Job missing");
    showStatus(job.message || job.status, job.done || 0, job.total || 0);
    if (job.scrape && grid.childElementCount === 0) {
      renderItems(job.scrape);
    }
    if (job.status === "done" || job.status === "error") {
      if (job.scrape) renderItems(job.scrape);
      if (job.output) setOutputPath(job.output);
      await refreshFiles();
      return job;
    }
    await new Promise((r) => setTimeout(r, 700));
  }
}

async function download(ev) {
  ev.preventDefault();
  const body = payloadFromForm();
  if (!body.url) return;

  setBusy(true);
  try {
    // Always open folder window when checkbox is on (default)
    const askEveryTime = $("ask-folder").checked;
    if (askEveryTime) {
      const path = await pickFolder();
      if (!path) return; // cancelled
      body.output = path;
    } else if (!body.output) {
      const path = await pickFolder();
      if (!path) return;
      body.output = path;
    }

    // Client already picked — do not open a second dialog on the server
    body.pick_folder = false;

    showStatus("Starting download job…");
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to start job");
    if (data.cancelled) {
      showStatus("Download cancelled");
      return;
    }
    if (data.output) setOutputPath(data.output);
    const job = await pollJob(data.job_id);
    if (job.status === "error") throw new Error(job.message);
  } catch (err) {
    showStatus(err.message || String(err));
  } finally {
    setBusy(false);
  }
}

async function refreshFiles() {
  try {
    const q = outputInput.value.trim()
      ? `?output=${encodeURIComponent(outputInput.value.trim())}`
      : "";
    const res = await fetch(`/api/files${q}`);
    const data = await res.json();
    if (data.output) setOutputPath(data.output);
    filesEl.innerHTML = "";
    (data.files || []).forEach((f) => {
      const li = document.createElement("li");
      const left = document.createElement("span");
      left.textContent = f.name;
      const right = document.createElement("a");
      right.href = `/api/files/download?path=${encodeURIComponent(f.path)}`;
      right.textContent = `${Math.round(f.size / 1024)} KB`;
      li.appendChild(left);
      li.appendChild(right);
      filesEl.appendChild(li);
    });
    if (!data.files?.length) {
      filesEl.innerHTML = `<li class="muted">No downloads yet.</li>`;
    }
  } catch {
    /* ignore */
  }
}

$("btn-scrape").addEventListener("click", scrape);
$("btn-browse").addEventListener("click", async () => {
  setBusy(true);
  try {
    await pickFolder();
    await refreshFiles();
  } catch (err) {
    showStatus(err.message || String(err));
  } finally {
    setBusy(false);
  }
});
form.addEventListener("submit", download);
$("btn-refresh").addEventListener("click", refreshFiles);
refreshFiles();
