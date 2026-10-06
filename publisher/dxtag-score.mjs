import {readFileSync} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const root = process.env.DXTAG_ROOT;
const file = process.argv[2];
if (!root || !file) {
  process.stderr.write('缺少 DXTAG_ROOT 或 maidata 路径\n');
  process.exit(2);
}
const {scoreMaidata} = await import(pathToFileURL(path.join(root, 'dist', 'index.mjs')).href);
const bytes = readFileSync(file);
const encoding = bytes[0] === 0xff && bytes[1] === 0xfe ? 'utf-16le' : bytes[0] === 0xfe && bytes[1] === 0xff ? 'utf-16be' : 'utf-8';
let text;
try {
  text = new TextDecoder(encoding, {fatal: true}).decode(bytes);
} catch {
  process.stderr.write('文件编码无法识别\n');
  process.exit(2);
}
const errors = [];
let result;
try {
  result = scoreMaidata(text, undefined, (error) => {
    errors.push(`${error.difficulty}：${error.message}`);
  });
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exit(1);
}
if (errors.length) {
  process.stderr.write(`${errors.join('\n')}\n`);
  process.exit(1);
}
process.stdout.write(JSON.stringify(result));
