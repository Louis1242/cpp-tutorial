// 发布打包：把 Cloudflare Pages 需要发布的静态文件收进 dist/。
// 用法：node tools/build-dist.mjs
// 无第三方依赖，只做「清空 dist → 按白名单复制」。

import { rm, mkdir, cp, readdir, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DIST = join(ROOT, 'dist');

// 需要发布的条目：文件按原路径复制，目录整棵复制
const ENTRIES = ['index.html', 'assets', 'icons', 'manifest.webmanifest', 'sw.js'];

async function walk(dir, base = dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...(await walk(full, base)));
    } else if (entry.isFile()) {
      const { size } = await stat(full);
      out.push({ path: full.slice(base.length + 1).replaceAll('\\', '/'), size });
    }
  }
  return out.sort((a, b) => a.path.localeCompare(b.path));
}

async function main() {
  await rm(DIST, { recursive: true, force: true });
  await mkdir(DIST, { recursive: true });

  const missing = [];
  for (const name of ENTRIES) {
    const src = join(ROOT, name);
    if (!existsSync(src)) {
      missing.push(name);
      continue;
    }
    await cp(src, join(DIST, name), { recursive: true });
  }

  if (missing.length) {
    console.error(`缺少发布文件：${missing.join('、')}`);
    process.exitCode = 1;
    return;
  }

  const files = await walk(DIST);
  const bytes = files.reduce((sum, f) => sum + f.size, 0);
  console.log(`dist/ 已生成：${files.length} 个文件，${bytes} 字节`);
  for (const f of files) console.log(`  ${f.path}  ${f.size}`);
}

await main();
