#!/usr/bin/env node

import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const assets = join(here, "..", "..", "packaging", "installer-assets");
const scratch = mkdtempSync(join(tmpdir(), "kirocrew-installer-evidence-"));
const ffmpeg = process.env.FFMPEG || "ffmpeg";
const openingGhosts = [
  { name: "top-left", x: 190, y: 0, delay: 0, offset: 0 },
  { name: "large", x: 760, y: 0, delay: 1, offset: 3 },
  { name: "left", x: 0, y: 210, delay: 2, offset: 6 },
  { name: "right", x: 1090, y: 350, delay: 3, offset: 9 },
  { name: "bottom", x: 180, y: 600, delay: 4, offset: 12 },
  { name: "small", x: 1000, y: 80, delay: 5, offset: 15 },
  { name: "small-left", x: 85, y: 540, delay: 6, offset: 18 },
  { name: "bottom-right", x: 770, y: 580, delay: 7, offset: 21 },
];

function run(binary, args) {
  const result = spawnSync(binary, args, { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout || `${binary} failed`);
}

function checkMark(x, y, theme) {
  const fill = theme === "dark" ? "#ffffff" : "#6332b4";
  const stroke = theme === "dark" ? "#2b144b" : "#ffffff";
  return `<rect x="${x}" y="${y}" width="14" height="14" rx="3" fill="${fill}"/><path d="M${x + 3} ${y + 7}l3 3 5-7" fill="none" stroke="${stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
}

function controls(theme, page, scope, progress) {
  const dark = theme === "dark";
  const primary = dark ? "#ffffff" : "#24143c";
  const muted = dark ? "#e3d9f1" : "#5c4d6d";
  const control = dark ? "#482878" : "#ffffff";
  const controlOpacity = dark ? 0.74 : 0.72;
  const border = dark ? "#dccbf4" : "#69459d";
  const action = dark ? "#ffffff" : "#6332b4";
  const actionText = dark ? "#2b144b" : "#ffffff";
  const allUsers = scope === "all";
  const scopeText = allUsers
    ? "Anyone who uses this computer (all users)"
    : "Only for me (demo.user)";
  const path = allUsers ? "C:\\Program Files\\KiroCrew" : "C:\\Users\\demo.user\\AppData\\Local\\Programs\\KiroCrew";
  const note = allUsers
    ? "Fresh install for all users. (will prompt for admin credentials)"
    : "Fresh install for current user only.";

  let body = "";
  if (page === "options") {
    body = `
      <text x="248" y="596" font-size="16" font-weight="600">Install options</text>
      <text x="248" y="637" fill="${muted}" font-size="13">Install for</text>
      <rect x="444" y="612" width="568" height="38" rx="9" fill="${control}" fill-opacity="${controlOpacity}" stroke="${border}" stroke-opacity="0.4"/>
      <text x="455" y="636" font-size="13">${scopeText}</text><path d="M995 627l6 6 6-6" fill="none" stroke="${primary}" stroke-width="1.5"/>
      <text x="444" y="669" fill="${muted}" font-size="11">${note}</text>
      <text x="248" y="706" fill="${muted}" font-size="13">Install location</text>
      <rect x="444" y="682" width="454" height="38" rx="9" fill="${control}" fill-opacity="${controlOpacity}" stroke="${border}" stroke-opacity="0.4"/>
      <text x="455" y="706" font-size="13">${path}</text>
      <rect x="906" y="682" width="106" height="38" rx="9" fill="${control}" fill-opacity="${controlOpacity}" stroke="${border}" stroke-opacity="0.4"/>
      <text x="959" y="706" text-anchor="middle" font-size="12">Browse…</text>
      ${checkMark(380, 733, theme)}<text x="401" y="745" fill="${muted}" font-size="12">Create a desktop shortcut</text>
      ${checkMark(666, 733, theme)}<text x="687" y="745" fill="${muted}" font-size="12">Start Kiro Crew when Windows starts</text>
      <text x="248" y="803" font-size="13" font-weight="500">Ready to install</text>
      <rect x="852" y="779" width="160" height="40" rx="11" fill="${action}"/><text x="932" y="804" text-anchor="middle" fill="${actionText}" font-size="12" font-weight="600">Install Kiro Crew</text>`;
  } else if (page === "progress") {
    const fill = Math.round((784 * progress) / 100);
    body = `
      <text x="248" y="662" font-size="15" font-weight="600">Installing, please wait...</text>
      <rect x="248" y="710" width="784" height="12" rx="6" fill="${control}" fill-opacity="${controlOpacity}"/>
      <rect x="248" y="710" width="${fill}" height="12" rx="6" fill="#8e48ff"/>`;
  } else {
    body = `
      <text x="248" y="642" font-size="26" font-weight="650">Kiro Crew is ready</text>
      ${checkMark(248, 682, theme)}<text x="269" y="695" font-size="14">Open Kiro Crew</text>
      <rect x="852" y="779" width="160" height="40" rx="11" fill="${action}"/><text x="932" y="804" text-anchor="middle" fill="${actionText}" font-size="12" font-weight="600">Finish</text>`;
  }

  return `<g fill="${primary}" font-family="Segoe UI Variable Text, Segoe UI, Arial, sans-serif">
    <rect x="1142" y="27" width="106" height="36" rx="18" fill="#3d1381" fill-opacity="0.28" stroke="#ffffff" stroke-opacity="0.18"/>
    <text x="1195" y="50" text-anchor="middle" fill="#ffffff" fill-opacity="0.84" font-size="12">Exit setup  ×</text>
    ${body}
  </g>`;
}

function visualFrame(tick, delay, offset) {
  if (tick < 11) return Math.max(0, Math.min(4, tick - delay));
  const bob = (tick - 11 + offset) % 24;
  if (bob === 0) return 7;
  if (bob < 6) return 4;
  if (bob < 12) return 5;
  if (bob < 18) return 6;
  return 5;
}

function renderSvgPng(name, svg, destination) {
  const source = join(scratch, `${name}.svg`);
  writeFileSync(source, svg);
  run("/usr/bin/sips", ["-s", "format", "png", source, "--out", destination]);
}

function composePng(name, theme, page, scope, tick, progress, destination) {
  const inputs = ["-i", join(scratch, `base-${theme}.png`)];
  const filters = [];
  let previous = "0:v";
  openingGhosts.forEach((ghost, index) => {
    const frame = visualFrame(tick, ghost.delay, ghost.offset);
    inputs.push(
      "-i",
      join(assets, `windows-installer-progress-${theme}-${ghost.name}-${frame}.bmp`)
    );
    const next = `ghost${index}`;
    filters.push(`[${previous}][${index + 1}:v]overlay=${ghost.x}:${ghost.y}[${next}]`);
    previous = next;
  });

  const controlsPng = join(scratch, `${name}-controls.png`);
  renderSvgPng(
    `${name}-controls`,
    `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="860" viewBox="0 0 1280 860">${controls(theme, page, scope, progress)}</svg>`,
    controlsPng
  );
  inputs.push("-i", controlsPng);
  filters.push(`[${previous}][${openingGhosts.length + 1}:v]overlay=0:0:format=auto[out]`);
  run(ffmpeg, [
    "-y",
    "-loglevel",
    "error",
    ...inputs,
    "-filter_complex",
    filters.join(";"),
    "-map",
    "[out]",
    "-frames:v",
    "1",
    destination,
  ]);
}

try {
  mkdirSync(here, { recursive: true });
  for (const theme of ["light", "dark"]) {
    run("/usr/bin/sips", [
      "-s",
      "format",
      "png",
      join(assets, `windows-installer-full-${theme}.svg`),
      "--out",
      join(scratch, `base-${theme}.png`),
    ]);
  }
  composePng("light-options", "light", "options", "current", 16, 0, join(here, "light-options.png"));
  composePng("dark-all-users", "dark", "options", "all", 16, 0, join(here, "dark-all-users.png"));
  composePng("dark-progress", "dark", "progress", "all", 16, 58, join(here, "dark-progress.png"));

  const frames = join(scratch, "frames");
  mkdirSync(frames);
  for (let index = 0; index < 40; index += 1) {
    const page = index < 18 ? "options" : index < 34 ? "progress" : "finish";
    const pageTick = index < 18 ? index : index < 34 ? index - 18 : index - 34;
    const progress = Math.min(92, 18 + Math.max(0, index - 18) * 6);
    const destination = join(frames, `frame-${String(index).padStart(3, "0")}.png`);
    composePng(
      `frame-${String(index).padStart(3, "0")}`,
      "dark",
      page,
      index < 8 ? "current" : "all",
      pageTick,
      progress,
      destination
    );
  }
  const installerFrameRate = "20/3"; // One rendered frame per 150 ms NSIS timer tick.
  run(ffmpeg, ["-y", "-framerate", installerFrameRate, "-i", join(frames, "frame-%03d.png"), "-vf", "scale=960:-2:flags=lanczos", "-loop", "0", join(here, "install-flow.gif")]);
  run(ffmpeg, ["-y", "-framerate", installerFrameRate, "-i", join(frames, "frame-%03d.png"), "-vf", "scale=1280:-2:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-movflags", "+faststart", join(here, "install-flow.mp4")]);
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
