#!/usr/bin/env node
/**
 * 校验桌面端发布版本号在所有来源保持一致：
 *   package.json / package-lock.json
 *   pyproject.toml [project].version
 *   crawler/__init__.py __version__
 *   src-tauri/tauri.conf.json version
 *   src-tauri/Cargo.toml [package].version
 *
 * 任一来源缺失、格式非法或版本不一致都会以非零码退出。
 */
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const VERSION_RE = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;

const sources = [
  readPackage(),
  readLock(),
  readPyproject(),
  readCrawlerInit(),
  readTauriConf(),
  readCargo(),
];

const expected = sources[0].version;
let failed = false;
let width = 0;
for (const source of sources) {
  width = Math.max(width, source.label.length);
}

for (const source of sources) {
  const status = source.version === expected ? 'OK  ' : 'FAIL';
  console.log(`${status}  ${source.label.padEnd(width)}  ${source.version || '（缺失）'}`);
  if (source.version !== expected) {
    failed = true;
  }
}

if (!VERSION_RE.test(expected)) {
  console.error(`\n版本号格式不合法：${expected}`);
  process.exit(1);
}

if (failed) {
  console.error(`\n版本不一致：所有来源都应为 ${expected}`);
  process.exit(1);
}

console.log(`\n版本一致：${expected}`);

function readFile(relative) {
  const file = path.join(root, relative);
  if (!existsSync(file)) {
    console.error(`缺少文件：${relative}`);
    process.exit(1);
  }
  return readFileSync(file, 'utf8');
}

function readPackage() {
  const pkg = JSON.parse(readFile('package.json'));
  return { label: 'package.json', version: String(pkg.version ?? '') };
}

function readLock() {
  const lock = JSON.parse(readFile('package-lock.json'));
  const version = lock.packages?.['']?.version ?? lock.version ?? '';
  return { label: 'package-lock.json', version: String(version) };
}

function readPyproject() {
  const text = readFile('pyproject.toml');
  const section = text.match(/\[project\]([\s\S]*?)(?:\n\[|$)/);
  const match = section?.[1].match(/^\s*version\s*=\s*"([^"]+)"/m);
  return { label: 'pyproject.toml', version: match?.[1] ?? '' };
}

function readCrawlerInit() {
  const text = readFile(path.join('crawler', '__init__.py'));
  const match = text.match(/^__version__\s*=\s*["']([^"']+)["']/m);
  return { label: 'crawler/__init__.py', version: match?.[1] ?? '' };
}

function readTauriConf() {
  const conf = JSON.parse(readFile(path.join('src-tauri', 'tauri.conf.json')));
  return { label: 'src-tauri/tauri.conf.json', version: String(conf.version ?? '') };
}

function readCargo() {
  const text = readFile(path.join('src-tauri', 'Cargo.toml'));
  const packageSection = text.match(/\[package\]([\s\S]*?)(?:\n\[|$)/);
  const section = packageSection?.[1] ?? text;
  const match = section.match(/^\s*version\s*=\s*"([^"]+)"/m);
  return { label: 'src-tauri/Cargo.toml', version: match?.[1] ?? '' };
}
