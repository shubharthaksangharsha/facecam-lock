/**
 * FaceCam Lock · browser lock preview.
 * Mirrors the real Omarchy lock screen: camera first, "Welcome back" on a match,
 * "Can't detect face" plus the password field on failure, and a password button
 * that is always available.
 */

const $ = (id) => document.getElementById(id);
let unlocked = false;
let passwordOpen = false;

function tickClock() {
  const now = new Date();
  $("lockTime").textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  $("lockDate").textContent = now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}

function setStatus(text, tone) {
  const el = $("biometricStatusText");
  el.textContent = text;
  el.className = "min-h-[24px] text-center font-medium " + ({
    scan: "text-sm text-fg",
    warn: "text-sm text-warn",
    bad: "text-sm text-bad",
    ok: "text-lg font-semibold text-ok",
  }[tone] || "text-sm");
}

function setAperture(tone) {
  const border = { scan: "border-accent", warn: "border-warn", bad: "border-bad", ok: "border-ok" }[tone];
  $("scannerAperture").className = $("scannerAperture").className.replace(/border-(accent|warn|bad|ok)\b/g, "").trim() + " " + border;
}

function renderDots(attempt, max) {
  $("attemptDots").replaceChildren(...Array.from({ length: max }, (_, i) => {
    const dot = document.createElement("span");
    dot.className = "h-2.5 w-2.5 rounded-full " + (i < attempt ? "bg-accent" : "bg-fg/15");
    return dot;
  }));
}

function openPassword(reason) {
  passwordOpen = true;
  $("passwordHint").textContent = reason || "Type your session password";
  $("passwordFallbackContainer").classList.replace("hidden", "flex");
  $("passwordInput").focus();
}

function fadeOut() {
  document.body.style.transition = "opacity 0.4s";
  document.body.style.opacity = "0";
  setTimeout(() => { window.close(); location.href = "/"; }, 420);
}

function onSuccess(data) {
  unlocked = true;
  $("scanBeam").remove();
  setAperture("ok");
  $("unlockCheckmark").classList.replace("hidden", "flex");
  const name = data.display_name || document.body.dataset.displayName;
  setStatus(name ? `Welcome back ${name}` : "Welcome back", "ok");
  setTimeout(fadeOut, 700);
}

function onFail(data) {
  setAperture("bad");
  setStatus(data.message || "Can't detect face", "bad");
  openPassword("Can't detect face — enter your password");
}

function startVerify() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/verify`);
  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "attempt_start") {
      renderDots(data.attempt, data.max_attempts);
      setAperture("scan");
      setStatus(data.attempt === 1 ? "Looking for your face…" : `Scanning face… (${data.attempt}/${data.max_attempts})`, "scan");
    } else if (data.type === "frame" && data.frame) {
      $("lockCameraPreview").src = data.frame;
    } else if (data.type === "attempt_failed") {
      setAperture("warn");
    } else if (data.type === "result") {
      data.success ? onSuccess(data) : onFail(data);
    }
  };
  ws.onerror = () => onFail({});
}

async function submitPassword(event) {
  event.preventDefault();
  const input = $("passwordInput");
  if (!input.value) return;
  const btn = $("passwordSubmitBtn");
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    const res = await fetch("/api/auth/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: input.value }),
    });
    const data = await res.json();
    if (data.valid) return fadeOut();
    $("passwordError").textContent = data.message || "Incorrect password";
    input.value = "";
    input.classList.add("animate-shake");
    setTimeout(() => input.classList.remove("animate-shake"), 400);
  } catch (err) {
    $("passwordError").textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Unlock";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  tickClock();
  setInterval(tickClock, 1000);
  renderDots(1, 3);
  $("openPasswordBtn").onclick = () => openPassword();
  $("passwordFallbackContainer").onsubmit = submitPassword;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !unlocked && !passwordOpen) location.reload();
  });
  startVerify();
});
