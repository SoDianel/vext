// Runs before `tauri dev`/`tauri build` (see tauri.conf.json's beforeDevCommand/
// beforeBuildCommand). app/static/ is the single source of truth for the
// frontend (also served directly by FastAPI at / and /static/ — see app/web.py).
// Tauri needs its frontendDist to have index.html at the root, but that same
// index.html references /static/style.css and /static/app.js (matching
// FastAPI's own URL layout) — so this mirrors app/static/ into
// dist-mirror/static/ *and* copies index.html to dist-mirror/index.html,
// rather than changing those paths and having two different versions of the
// same file to keep in sync.
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(__dirname, "..", "app", "static");
const DIST = path.join(__dirname, "dist-mirror");

fs.rmSync(DIST, { recursive: true, force: true });
fs.mkdirSync(path.join(DIST, "static"), { recursive: true });
fs.cpSync(SRC, path.join(DIST, "static"), { recursive: true });
fs.copyFileSync(path.join(SRC, "index.html"), path.join(DIST, "index.html"));

console.log(`Mirrored ${SRC} -> ${DIST} (+ index.html at dist root)`);
