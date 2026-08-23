#!/usr/bin/env node

// Regenerate the checked-in installer rasters from the editable SVG sources.
// macOS's sips is used only by designers; release and Windows builds consume
// the committed PNG/BMP files and do not execute this script.

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const scratch = mkdtempSync(join(tmpdir(), "kirocrew-installer-assets-"));

function run(binary, args, label) {
  const result = spawnSync(
    binary,
    args,
    { encoding: "utf8" }
  );
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || `${label} failed`);
  }
}

function render(source, format, destination) {
  run(
    "/usr/bin/sips",
    ["-s", "format", format, join(here, source), "--out", destination],
    `sips render for ${source}`
  );
}

function renderBmp24(source, destination) {
  const intermediate = join(scratch, `${source.replaceAll(/[\\/]/g, "-")}.bmp`);
  render(source, "bmp", intermediate);
  bmp32To24(intermediate, destination);
}

function renderSvgTextBmp24(name, svg, destination) {
  const source = join(scratch, `${name}.svg`);
  const intermediate = join(scratch, `${name}.bmp`);
  writeFileSync(source, svg);
  run(
    "/usr/bin/sips",
    ["-s", "format", "bmp", source, "--out", intermediate],
    `sips render for ${name}`
  );
  bmp32To24(intermediate, destination);
}

function renderSvgCropBmp24(name, svg, crop, destination) {
  const cropped = svg.replace(
    'width="1280" height="860" viewBox="0 0 1280 860"',
    `width="${crop.width}" height="${crop.height}" viewBox="${crop.x} ${crop.y} ${crop.width} ${crop.height}"`
  );
  renderSvgTextBmp24(name, cropped, destination);
}

function renderSvgAtScale(source, scale, destination) {
  const sourcePath = join(here, source);
  const scaledPath = join(scratch, `${source.replace(".svg", "")}@${scale}x.svg`);
  const scaled = readFileSync(sourcePath, "utf8")
    .replace('width="660"', `width="${660 * scale}"`)
    .replace('height="420"', `height="${420 * scale}"`);
  writeFileSync(scaledPath, scaled);
  run(
    "/usr/bin/sips",
    ["-s", "format", "png", scaledPath, "--out", destination],
    `sips ${scale}x render for ${source}`
  );
}

function bmp32To24(source, destination) {
  const input = readFileSync(source);
  if (input.toString("ascii", 0, 2) !== "BM") throw new Error(`${source} is not a BMP`);

  const sourceOffset = input.readUInt32LE(10);
  const width = input.readInt32LE(18);
  const signedHeight = input.readInt32LE(22);
  const height = Math.abs(signedHeight);
  const bitsPerPixel = input.readUInt16LE(28);
  if (width <= 0 || height <= 0 || bitsPerPixel !== 32) {
    throw new Error(`${source} must be a non-empty 32-bit BMP from sips`);
  }

  const sourceStride = width * 4;
  const destinationStride = Math.ceil((width * 3) / 4) * 4;
  const pixelBytes = destinationStride * height;
  const output = Buffer.alloc(54 + pixelBytes);
  output.write("BM", 0, 2, "ascii");
  output.writeUInt32LE(output.length, 2);
  output.writeUInt32LE(54, 10);
  output.writeUInt32LE(40, 14);
  output.writeInt32LE(width, 18);
  output.writeInt32LE(height, 22);
  output.writeUInt16LE(1, 26);
  output.writeUInt16LE(24, 28);
  output.writeUInt32LE(pixelBytes, 34);
  output.writeInt32LE(2835, 38);
  output.writeInt32LE(2835, 42);

  for (let outputY = 0; outputY < height; outputY += 1) {
    const sourceY = signedHeight < 0 ? height - 1 - outputY : outputY;
    const sourceRow = sourceOffset + sourceY * sourceStride;
    const destinationRow = 54 + outputY * destinationStride;
    for (let x = 0; x < width; x += 1) {
      output[destinationRow + x * 3] = input[sourceRow + x * 4];
      output[destinationRow + x * 3 + 1] = input[sourceRow + x * 4 + 1];
      output[destinationRow + x * 3 + 2] = input[sourceRow + x * 4 + 2];
    }
  }
  writeFileSync(destination, output);
}

try {
  const dmg1x = join(scratch, "dmg-background.png");
  const dmg2x = join(scratch, "dmg-background@2x.png");
  renderSvgAtScale("dmg-background.svg", 1, dmg1x);
  renderSvgAtScale("dmg-background.svg", 2, dmg2x);
  run(
    "/usr/bin/tiffutil",
    ["-cathidpicheck", dmg1x, dmg2x, "-out", join(here, "dmg-background.tiff")],
    "Retina TIFF assembly"
  );
  for (const name of [
    "windows-installer-sidebar",
    "windows-installer-header",
    "windows-installer-full-light",
    "windows-installer-full-dark",
  ]) {
    renderBmp24(`${name}.svg`, join(here, `${name}.bmp`));
  }

  // All eight opening-animation characters get a one-time pop-in followed by
  // the same gentle vertical bob and staggered blink. Each 24-bit frame is a
  // compact crop that covers the matching static character underneath.
  const openingFrames = [
    { scale: 0.001, dy: 0 },
    { scale: 0.55, dy: 0 },
    { scale: 1.14, dy: 0 },
    { scale: 0.95, dy: 0 },
    { scale: 1, dy: -4 },
    { scale: 1, dy: 0 },
    { scale: 1, dy: 4 },
    { scale: 1, dy: 0, blink: true },
  ];
  const openingGhosts = {
    "top-left": { x: 190, y: 0, width: 190, height: 130, anchorX: 282, anchorY: 13 },
    large: { x: 760, y: 0, width: 270, height: 240, anchorX: 896, anchorY: 17 },
    left: { x: 0, y: 210, width: 190, height: 270, anchorX: 15, anchorY: 344 },
    right: { x: 1090, y: 350, width: 190, height: 280, anchorX: 1265, anchorY: 499 },
    bottom: { x: 180, y: 600, width: 400, height: 260, anchorX: 384, anchorY: 851 },
    small: { x: 1000, y: 80, width: 180, height: 180, anchorX: 1088, anchorY: 163 },
    "small-left": { x: 85, y: 540, width: 170, height: 190, anchorX: 166, anchorY: 636 },
    "bottom-right": { x: 770, y: 580, width: 170, height: 200, anchorX: 845, anchorY: 671 },
  };
  for (const theme of ["light", "dark"]) {
    const source = readFileSync(join(here, `windows-installer-full-${theme}.svg`), "utf8");
    for (const [ghost, crop] of Object.entries(openingGhosts)) {
      const marker = `id="ghost-${ghost}" transform="translate(${crop.anchorX} ${crop.anchorY}) scale(1) translate(-${crop.anchorX} -${crop.anchorY})"`;
      for (let index = 0; index < openingFrames.length; index += 1) {
        const { scale, dy, blink } = openingFrames[index];
        let frame = source.replace(
          marker,
          `id="ghost-${ghost}" transform="translate(0 ${dy}) translate(${crop.anchorX} ${crop.anchorY}) scale(${scale.toFixed(4)}) translate(-${crop.anchorX} -${crop.anchorY})"`
        );
        if (blink) {
          frame = frame.replace(
            'id="ghost-eyes"',
            'id="ghost-eyes" transform="translate(0 487) scale(1 .08) translate(0 -487)"'
          );
        }
        renderSvgCropBmp24(
          `windows-installer-progress-${theme}-${ghost}-${index}`,
          frame,
          crop,
          join(here, `windows-installer-progress-${theme}-${ghost}-${index}.bmp`)
        );
      }
    }
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
