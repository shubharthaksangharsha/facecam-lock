/**
 * FaceCam Lock · Face Studio
 *
 * The camera stream only runs while the Studio tab is visible (the webcam LED
 * goes off otherwise). Frames arrive as binary WebSocket messages
 * ([u32 header length][JSON header][JPEG]); JPEG decoding happens off the main
 * thread via createImageBitmap, and drawing is coalesced into one rAF per frame.
 */

const MIN_ENROLL_SAMPLES = 5;
const POSES = [
  { id: "frontal", title: "Look straight ahead", hint: "Face the camera with a neutral expression." },
  { id: "left", title: "Turn slightly left", hint: "Turn your head about 15° to the left." },
  { id: "right", title: "Turn slightly right", hint: "Turn your head about 15° to the right." },
  { id: "tilt_up", title: "Tilt chin up", hint: "Lift your chin a little." },
  { id: "smile", title: "Smile", hint: "Smile naturally — expressions help recognition." },
];

const $ = (id) => document.getElementById(id);
const state = {
  tab: "studio",
  profile: null,
  settings: null,
  studioPassword: "",
  samples: [],
  poseIndex: 0,
  capturing: false,
  pendingAction: null,
};

/* ---------- toasts & confirm ---------- */

function toast(message, kind = "info") {
  const colors = { info: "border-accent/40 text-fg", ok: "border-ok/50 text-ok", warn: "border-warn/50 text-warn", bad: "border-bad/50 text-bad" };
  const el = document.createElement("div");
  el.className = `toast card pointer-events-auto max-w-xs px-4 py-3 text-sm ${colors[kind] || colors.info}`;
  el.textContent = message;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 3200);
}
window.toast = toast;

function confirmDialog(text) {
  return new Promise((resolve) => {
    const modal = $("confirmModal");
    $("confirmText").textContent = text;
    modal.classList.replace("hidden", "flex");
    const done = (value) => {
      modal.classList.replace("flex", "hidden");
      $("confirmYes").onclick = $("confirmNo").onclick = null;
      resolve(value);
    };
    $("confirmYes").onclick = () => done(true);
    $("confirmNo").onclick = () => done(false);
  });
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

/* ---------- tabs ---------- */

function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  document.querySelectorAll(".tab-content").forEach((c) => c.classList.toggle("hidden", c.id !== `tab-${tab}`));
  if (tab === "settings") loadHealth();
  stream.sync();
}

/* ---------- live stream ---------- */

const canvas = $("cameraCanvas");
const ctx = canvas.getContext("2d", { alpha: false });

const stream = {
  ws: null,
  wanted: false,
  retry: 0,
  decoding: false,
  pending: null, // { bitmap, header }
  rafQueued: false,
  frames: 0,
  fpsAt: performance.now(),

  lastFaceAt: 0,

  sync() {
    const wanted = state.tab === "studio" && !document.hidden;
    $("cameraPaused").classList.toggle("hidden", wanted);
    $("cameraPaused").classList.toggle("flex", !wanted);
    canvas.parentElement.classList.toggle("is-resting", !wanted);
    if (!wanted) setNoFace(false);
    else this.lastFaceAt = performance.now();
    if (wanted === this.wanted) return;
    this.wanted = wanted;
    wanted ? this.open() : this.close();
  },

  open() {
    if (this.ws) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/stream`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => { this.retry = 0; setCameraPill(true); };
    ws.onclose = () => {
      this.ws = null;
      setCameraPill(false);
      if (this.wanted) setTimeout(() => this.wanted && this.open(), Math.min(4000, 400 * 2 ** this.retry++));
    };
    ws.onmessage = (event) => this.onMessage(event.data);
  },

  close() {
    if (this.ws) this.ws.close();
    this.ws = null;
  },

  async onMessage(data) {
    if (typeof data === "string") {
      const msg = JSON.parse(data);
      if (msg.error) toast(msg.error, "bad");
      return;
    }
    if (this.decoding) return; // drop frames rather than queue latency
    this.decoding = true;
    try {
      const view = new DataView(data);
      const headLen = view.getUint32(0);
      const header = JSON.parse(new TextDecoder().decode(new Uint8Array(data, 4, headLen)));
      const bitmap = await createImageBitmap(new Blob([new Uint8Array(data, 4 + headLen)], { type: "image/jpeg" }));
      if (this.pending) this.pending.bitmap.close();
      this.pending = { bitmap, header };
      if (!this.rafQueued) {
        this.rafQueued = true;
        requestAnimationFrame(() => this.draw());
      }
    } catch (err) {
      console.error("frame decode failed", err);
    } finally {
      this.decoding = false;
    }
  },

  draw() {
    this.rafQueued = false;
    const frame = this.pending;
    this.pending = null;
    if (!frame) return;
    const { bitmap, header } = frame;
    if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
    }
    // Mirror like a selfie camera; overlay coordinates are flipped to match.
    ctx.setTransform(-1, 0, 0, 1, canvas.width, 0);
    ctx.drawImage(bitmap, 0, 0);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    bitmap.close();
    drawHUD(header);
    updateMeter(header.best_similarity, header.threshold);

    // Debounced so a single missed detection doesn't flash the hint.
    const nowMs = performance.now();
    if (header.faces && header.faces.length) this.lastFaceAt = nowMs;
    setNoFace(nowMs - this.lastFaceAt > 1200);

    this.frames++;
    const now = performance.now();
    if (now - this.fpsAt > 1000) {
      $("fpsText").textContent = `${Math.round((this.frames * 1000) / (now - this.fpsAt))} fps`;
      this.frames = 0;
      this.fpsAt = now;
    }
  },
};

function cssColor(token, alpha = 1) {
  const rgb = getComputedStyle(document.documentElement).getPropertyValue(`--${token}`).trim();
  return `rgb(${rgb} / ${alpha})`;
}

function drawHUD(header) {
  const { faces = [], best_similarity: sim, threshold = 0.38 } = header;
  const w = canvas.width;
  const matched = sim !== null && sim !== undefined && sim >= threshold;
  const color = matched ? cssColor("ok") : sim != null ? cssColor("warn") : cssColor("accent");

  for (const face of faces) {
    const [bx, y, bw, bh] = face.box;
    const x = w - bx - bw;
    const c = Math.min(22, bw / 4);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    ctx.moveTo(x, y + c); ctx.lineTo(x, y); ctx.lineTo(x + c, y);
    ctx.moveTo(x + bw - c, y); ctx.lineTo(x + bw, y); ctx.lineTo(x + bw, y + c);
    ctx.moveTo(x, y + bh - c); ctx.lineTo(x, y + bh); ctx.lineTo(x + c, y + bh);
    ctx.moveTo(x + bw - c, y + bh); ctx.lineTo(x + bw, y + bh); ctx.lineTo(x + bw, y + bh - c);
    ctx.stroke();

    ctx.fillStyle = cssColor("accent", 0.9);
    for (const [lx, ly] of face.landmarks || []) {
      ctx.beginPath();
      ctx.arc(w - lx, ly, 2.5, 0, Math.PI * 2);
      ctx.fill();
    }

    const label = matched ? `${header.name || state.profile?.display_name || "You"} · ${Math.round(sim * 100)}%`
      : sim != null ? `Match ${Math.round(sim * 100)}%` : `Face ${Math.round(face.score * 100)}%`;
    ctx.font = "600 11px 'JetBrainsMono Nerd Font', monospace";
    const tw = ctx.measureText(label).width + 12;
    const ty = Math.max(4, y - 22);
    ctx.fillStyle = matched ? cssColor("ok", 0.9) : cssColor("bg-deep", 0.85);
    ctx.fillRect(x, ty, tw, 18);
    ctx.fillStyle = matched ? cssColor("on-accent") : cssColor("fg");
    ctx.fillText(label, x + 6, ty + 13);
  }
}

function updateMeter(sim, threshold) {
  const bar = $("similarityBar");
  const text = $("similarityScoreText");
  if (sim === null || sim === undefined) {
    bar.style.width = "0%";
    text.textContent = state.profile ? "No face" : "Not enrolled";
    return;
  }
  const pct = Math.max(0, Math.min(100, Math.round(sim * 100)));
  bar.style.width = `${pct}%`;
  text.textContent = `${pct}% · needs ${Math.round(threshold * 100)}%`;
  bar.className = "h-full rounded-full transition-[width] duration-150 " +
    (sim >= threshold ? "bg-ok" : sim >= threshold - 0.1 ? "bg-warn" : "bg-bad");
}

let noFaceShown = false;
function setNoFace(show) {
  if (show === noFaceShown) return;
  noFaceShown = show;
  $("noFaceChip").classList.toggle("show", show);
}

function setCameraPill(on) {
  const pill = $("connectionStatus");
  pill.className = `pill ${on ? "border-ok/40 bg-ok/10 text-ok" : "border-line text-muted"}`;
  pill.innerHTML = `<span class="h-1.5 w-1.5 rounded-full ${on ? "animate-pulse bg-ok" : "bg-muted"}"></span> ${on ? "Camera on" : "Camera off"}`;
}

/* ---------- profile ---------- */

async function loadStatus() {
  const [data, people] = await Promise.all([api("/api/status"), api("/api/people").catch(() => [])]);
  state.profile = data.profile;
  state.settings = data.settings;
  state.people = people;
  renderProfile();
  renderPeople();
  populateSettings();
}

function renderPeople() {
  const people = state.people || [];
  $("peopleSection").classList.toggle("hidden", people.length === 0);
  $("peopleList").replaceChildren(...people.map((person) => {
    const li = document.createElement("li");
    li.className = "flex items-center justify-between rounded-lg border border-line/60 bg-bg-deep/40 px-3 py-2";
    const info = document.createElement("span");
    info.className = "text-sm";
    info.textContent = person.display_name;
    const meta = document.createElement("span");
    meta.className = "ml-2 font-mono text-[11px] text-muted";
    meta.textContent = `${person.samples} samples${person.has_photo ? " · photo" : ""}`;
    info.appendChild(meta);
    const remove = document.createElement("button");
    remove.className = "text-xs text-bad hover:underline";
    remove.textContent = "Remove";
    remove.onclick = () => {
      state.pendingPerson = person;
      state.studioPassword ? removePerson() : requestStudioPassword("remove-person");
    };
    li.append(info, remove);
    return li;
  }));
}

async function removePerson() {
  const person = state.pendingPerson;
  state.pendingPerson = null;
  if (!person || !(await confirmDialog(`Stop ${person.display_name} from unlocking this laptop?`))) return;
  try {
    await api(`/api/people/${encodeURIComponent(person.id)}/delete`, { password: state.studioPassword });
    toast(`${person.display_name} removed`, "ok");
    await loadStatus();
  } catch (err) {
    toast(err.message, "bad");
  }
}

function renderProfile() {
  const p = state.profile;
  $("profileCard").classList.toggle("hidden", !p);
  $("emptyProfileCard").classList.toggle("hidden", !!p || state.capturing);
  if (!p) return;
  $("profileUsername").textContent = p.display_name;
  $("profileSampleCount").textContent = `${p.sample_count} photos trained`;
  $("profileDate").textContent = p.created_at ? `Enrolled ${new Date(p.created_at).toLocaleDateString()}` : "";
  $("profileRetrainHint").classList.toggle("hidden", p.sample_count >= MIN_ENROLL_SAMPLES);

  const photo = $("profilePhoto");
  if (p.photo_version) {
    const url = `/api/profile/photo?v=${p.photo_version}`;
    photo.src = url;
    $("profileAvatar").src = url;
  } else if (p.avatar_base64) {
    $("profileAvatar").src = p.avatar_base64;
  }
  photo.classList.toggle("hidden", !p.photo_version);
  $("profilePhotoEmpty").classList.toggle("hidden", !!p.photo_version);
}

/* ---------- welcome photo ---------- */

function pickPhoto() {
  $("photoInput").value = "";
  $("photoInput").click();
}

function onPhotoChosen() {
  const file = $("photoInput").files[0];
  if (!file) return;
  if (file.size > 15 * 1024 * 1024) return toast("Photo is larger than 15 MB", "warn");
  const reader = new FileReader();
  reader.onload = () => {
    state.pendingPhoto = reader.result;
    state.studioPassword ? uploadPhoto() : requestStudioPassword("photo");
  };
  reader.readAsDataURL(file);
}

async function uploadPhoto() {
  const image = state.pendingPhoto;
  state.pendingPhoto = null;
  if (!image) return;
  const btn = $("changePhotoBtn");
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    const data = await api("/api/profile/photo", { password: state.studioPassword, image });
    if (!data.face_found) toast("No face found in that photo — used a centre crop", "warn");
    else if (data.similarity !== null && data.similarity < 0.38) toast("Saved, but that photo doesn't look like your enrolled face", "warn");
    else toast("Welcome photo updated", "ok");
    await loadStatus();
  } catch (err) {
    if (/password/i.test(err.message)) state.studioPassword = "";
    toast(err.message, "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Change";
  }
}

/* ---------- training password ---------- */

function requestStudioPassword(action) {
  state.pendingAction = action;
  const creating = !state.settings?.has_studio_password;
  $("studioPasswordTitle").textContent = creating ? "Create a training password" : "Unlock face training";
  $("studioPasswordHint").textContent = creating
    ? "Pick a password that protects face training on this machine."
    : "Enter the training password to capture and save face photos.";
  $("studioPasswordError").textContent = "";
  $("studioPasswordInput").value = state.studioPassword;
  $("studioPasswordModal").classList.replace("hidden", "flex");
  setTimeout(() => $("studioPasswordInput").focus(), 30);
}

function closeStudioPassword() {
  $("studioPasswordModal").classList.replace("flex", "hidden");
}

async function submitStudioPassword(event) {
  event.preventDefault();
  const password = $("studioPasswordInput").value;
  if (!password) return ($("studioPasswordError").textContent = "Password required");
  try {
    const data = await api("/api/auth/studio", { password });
    if (!data.valid) {
      $("studioPasswordError").textContent = data.message;
      $("studioPasswordInput").classList.add("animate-shake");
      setTimeout(() => $("studioPasswordInput").classList.remove("animate-shake"), 400);
      return;
    }
    if (data.created) {
      state.settings.has_studio_password = true;
      toast("Training password set", "ok");
    }
    state.studioPassword = password;
    closeStudioPassword();
    const action = state.pendingAction;
    state.pendingAction = null;
    if (action === "delete") deleteProfile();
    else if (action === "photo") uploadPhoto();
    else if (action === "remove-person") removePerson();
    else startEnrollment();
  } catch (err) {
    $("studioPasswordError").textContent = err.message;
  }
}

/* ---------- enrollment ---------- */

function startEnrollment() {
  state.samples = [];
  state.poseIndex = 0;
  state.capturing = true;
  $("enrollDisplayName").value = state.profile?.display_name || $("enrollDisplayName").value || "";
  $("enrollmentWizard").classList.replace("hidden", "flex");
  $("emptyProfileCard").classList.add("hidden");
  renderWizard();
}

function cancelEnrollment() {
  state.capturing = false;
  state.samples = [];
  $("enrollmentWizard").classList.replace("flex", "hidden");
  renderProfile();
}

function renderWizard() {
  const done = state.samples.length >= POSES.length;
  const pose = POSES[Math.min(state.poseIndex, POSES.length - 1)];
  $("stepCounter").textContent = done ? `All ${POSES.length} photos captured` : `Photo ${state.poseIndex + 1} of ${POSES.length}`;
  $("poseTitle").textContent = done ? "Ready to train" : pose.title;
  $("poseHint").textContent = done ? "Check the photos below, then train and save." : pose.hint;
  $("captureSampleBtn").classList.toggle("hidden", done);
  $("saveEnrollmentBtn").classList.toggle("hidden", state.samples.length < MIN_ENROLL_SAMPLES);

  $("poseRail").replaceChildren(...POSES.map((p, i) => {
    const li = document.createElement("li");
    const captured = state.samples.some((s) => s.pose === p.id);
    const current = !done && i === state.poseIndex;
    li.className = "h-1.5 rounded-full " + (captured ? "bg-ok" : current ? "bg-accent" : "bg-fg/15");
    li.title = p.title;
    return li;
  }));

  $("sampleGallery").replaceChildren(...state.samples.map((s, idx) => {
    const card = document.createElement("div");
    card.className = "group relative flex flex-col items-center rounded-lg border border-line/60 bg-surface/60 p-1.5";
    const img = document.createElement("img");
    img.src = s.thumbnail;
    img.alt = s.pose_title;
    img.className = "aspect-square w-full rounded-md object-cover";
    const label = document.createElement("span");
    label.className = "mt-1 w-full truncate text-center font-mono text-[9px] text-muted";
    label.textContent = s.pose_title;
    const remove = document.createElement("button");
    remove.className = "absolute right-1 top-1 hidden h-4 w-4 items-center justify-center rounded-full bg-bad text-[10px] text-on-accent group-hover:flex";
    remove.textContent = "×";
    remove.title = "Retake";
    remove.onclick = () => {
      state.samples.splice(idx, 1);
      state.poseIndex = POSES.findIndex((p) => !state.samples.some((x) => x.pose === p.id));
      if (state.poseIndex < 0) state.poseIndex = POSES.length;
      renderWizard();
    };
    card.append(img, label, remove);
    return card;
  }));
}

async function capturePose() {
  if (state.poseIndex >= POSES.length) return;
  const btn = $("captureSampleBtn");
  if (btn.disabled) return;
  const pose = POSES[state.poseIndex];
  btn.disabled = true;
  const original = btn.innerHTML;
  btn.textContent = "Capturing…";
  try {
    const data = await api(`/api/enroll/capture?pose_name=${encodeURIComponent(pose.id)}`, {});
    state.samples.push({
      pose: pose.id,
      pose_title: pose.title,
      score: data.score,
      sharpness: data.sharpness,
      embedding: data.embedding,
      thumbnail: data.thumbnail,
    });
    state.poseIndex = POSES.findIndex((p) => !state.samples.some((x) => x.pose === p.id));
    if (state.poseIndex < 0) state.poseIndex = POSES.length;
    renderWizard();
  } catch (err) {
    toast(err.message, "warn");
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

async function saveEnrollment() {
  if (state.samples.length < MIN_ENROLL_SAMPLES) return toast(`Capture at least ${MIN_ENROLL_SAMPLES} photos`, "warn");
  const btn = $("saveEnrollmentBtn");
  btn.disabled = true;
  btn.textContent = "Training…";
  try {
    const data = await api("/api/enroll/save", {
      samples: state.samples,
      avatar_base64: state.samples[0].thumbnail,
      display_name: $("enrollDisplayName").value.trim(),
      password: state.studioPassword,
    });
    toast(`Face enrolled as ${data.display_name}`, "ok");
    cancelEnrollment();
    await loadStatus();
    stream.close(); // reconnect so the live meter uses the new profile
    stream.wanted = false;
    stream.sync();
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Train & save profile";
  }
}

async function deleteProfile() {
  if (!(await confirmDialog(`Delete the face profile for ${state.profile?.display_name || "this user"}?`))) return;
  try {
    await api("/api/enroll/delete", { password: state.studioPassword });
    toast("Face profile deleted", "ok");
    await loadStatus();
  } catch (err) {
    toast(err.message, "bad");
  }
}

/* ---------- settings ---------- */

function populateSettings() {
  const s = state.settings;
  if (!s) return;
  $("settingThreshold").value = s.threshold;
  $("thresholdValue").textContent = Number(s.threshold).toFixed(2);
  $("settingAttempts").value = s.max_attempts;
  $("settingTimeout").value = s.attempt_timeout_sec;
  $("settingCamera").value = String(s.camera_index);
}

async function saveSettings() {
  try {
    const data = await api("/api/settings", {
      threshold: parseFloat($("settingThreshold").value),
      max_attempts: parseInt($("settingAttempts").value, 10),
      attempt_timeout_sec: parseFloat($("settingTimeout").value),
      camera_index: parseInt($("settingCamera").value, 10),
    });
    state.settings = data.settings;
    toast("Settings saved", "ok");
  } catch (err) {
    toast(err.message, "bad");
  }
}

async function loadHealth() {
  try {
    const h = await api("/api/system/health");
    const rows = [
      ["Face daemon", h.daemon, h.daemon ? "running" : "not running"],
      ["Lock plugin", h.plugin, h.plugin ? "installed" : "missing"],
      ["PAM hook", h.pam, h.pam ? "configured" : "missing"],
      ["Face profile", h.profile && h.samples >= MIN_ENROLL_SAMPLES, h.profile ? `${h.samples} photos` : "not enrolled"],
    ];
    $("healthList").replaceChildren(...rows.map(([name, ok, detail]) => {
      const li = document.createElement("li");
      li.className = "flex items-center justify-between rounded-lg border border-line/60 bg-bg-deep/40 p-2.5";
      const left = document.createElement("span");
      left.className = "text-muted";
      left.textContent = name;
      const right = document.createElement("span");
      right.className = ok ? "text-ok" : "text-warn";
      right.textContent = `${ok ? "●" : "○"} ${detail}`;
      li.append(left, right);
      return li;
    }));
  } catch {
    /* health is informational */
  }
}

/* ---------- wiring ---------- */

document.addEventListener("DOMContentLoaded", async () => {
  document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  document.addEventListener("visibilitychange", () => stream.sync());

  $("startEnrollBtn").onclick = () => requestStudioPassword("enroll");
  $("reEnrollBtn").onclick = () => requestStudioPassword("enroll");
  $("deleteProfileBtn").onclick = () => requestStudioPassword("delete");
  $("cancelEnrollBtn").onclick = cancelEnrollment;
  $("captureSampleBtn").onclick = capturePose;
  $("saveEnrollmentBtn").onclick = saveEnrollment;
  $("studioPasswordForm").onsubmit = submitStudioPassword;
  $("studioPasswordCancel").onclick = closeStudioPassword;
  $("saveSettingsBtn").onclick = saveSettings;
  $("changePhotoBtn").onclick = pickPhoto;
  $("photoInput").onchange = onPhotoChosen;
  $("settingThreshold").oninput = (e) => ($("thresholdValue").textContent = Number(e.target.value).toFixed(2));
  $("testLockScreenBtn").onclick = () => window.open("/lock", "_blank");
  $("triggerSystemLockBtn").onclick = () => api("/api/system/lock", {}).catch((e) => toast(e.message, "bad"));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeStudioPassword();
    if (e.code === "Space" && state.capturing && state.tab === "studio" && !/INPUT|TEXTAREA|SELECT|BUTTON/.test(e.target.tagName)) {
      e.preventDefault();
      capturePose();
    }
  });

  try {
    await loadStatus();
  } catch (err) {
    toast(err.message, "bad");
  }
  stream.sync();
});
