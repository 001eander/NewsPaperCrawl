#!/usr/bin/env node
/**
 * 构建 Python sidecar（供 Tauri externalBin 打包）。
 *
 * 具体的 PyInstaller 流程由打包目录提供。这里只负责定位入口并转发参数，
 * 避免前端仓库重复实现打包逻辑：
 *
 *   npm run sidecar -- --target x86_64-pc-windows-msvc
 *
 * 也可用 SIDECAR_BUILD_CMD 指定任意命令（shell 执行）。
 */
import { existsSync } from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const extraArgs = process.argv.slice(2);

const envCommand = process.env.SIDECAR_BUILD_CMD?.trim();
if (envCommand) {
  const result = spawnSync(envCommand, extraArgs, {
    cwd: root,
    stdio: 'inherit',
    shell: true,
  });
  process.exit(result.status ?? 1);
}

const candidates = [
  'packaging/build_sidecar.py',
  'packaging/build.py',
  'packaging/sidecar.py',
  'scripts/build_sidecar.py',
];

const entry = candidates.find((candidate) => existsSync(path.join(root, candidate)));
if (!entry) {
  console.error('未找到 sidecar 构建脚本。');
  console.error('期望以下任一入口（由打包流程提供）：');
  for (const candidate of candidates) {
    console.error(`  - ${candidate}`);
  }
  console.error('\n也可以设置 SIDECAR_BUILD_CMD 自定义构建命令，例如：');
  console.error('  SIDECAR_BUILD_CMD="uv run pyinstaller ..." npm run sidecar');
  process.exit(1);
}

console.log(`sidecar: uv run python ${entry} ${extraArgs.join(' ')}`.trim());
const result = spawnSync('uv', ['run', 'python', entry, ...extraArgs], {
  cwd: root,
  stdio: 'inherit',
});
if (result.error) {
  console.error(`无法执行 uv：${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
