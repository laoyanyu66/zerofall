#!/usr/bin/env node
// DSH 会话文件（session.v4.jsonl.zstd）的帧级读写助手
//
// 为什么需要它：会话文件是「一堆 zstd 帧首尾相接」，而 Node 的
// zlib.zstdDecompressSync 只解**第一帧**，所以必须先按 RFC 8878 走帧头/块头
// 算出每帧边界，再逐帧解。重写时也一样：每条 JSONL 写成独立一帧，
// 并打开块校验（ZSTD_c_checksumFlag），与 DSH 自己的落盘一致。
//
// 用法：
//   node session_zstd.cjs stats  <file>
//   node session_zstd.cjs rewrite <src> <dst> [--dedupe-seq]
//
// 输出：单行 JSON（便于 Python 侧解析），失败时 exit code != 0。
"use strict";

const fs = require("fs");
const zlib = require("zlib");

const MAGIC = 0xfd2fb528;
const SKIP_MAGIC_MIN = 0x184d2a50;
const SKIP_MAGIC_MAX = 0x184d2a5f;

function isSkippable(magic) {
  return magic >= SKIP_MAGIC_MIN && magic <= SKIP_MAGIC_MAX;
}

/** 走一遍帧结构，返回 [{start, end, headerBytes}] */
function walkFrames(buf) {
  const frames = [];
  let pos = 0;
  while (pos + 4 <= buf.length) {
    const magic = buf.readUInt32LE(pos);
    const start = pos;
    pos += 4;
    if (isSkippable(magic)) {
      if (pos + 4 > buf.length) throw new Error("截断：skippable 帧缺长度 @" + start);
      const size = buf.readUInt32LE(pos);
      pos += 4 + size;
      frames.push({ start, end: pos, headerBytes: 8, skippable: true });
      continue;
    }
    if (magic !== MAGIC) {
      throw new Error("帧魔数不对 @" + start + "：" + magic.toString(16));
    }
    const fhd = buf[pos++];
    const fcsFlag = fhd >> 6;
    const singleSegment = (fhd >> 5) & 1;
    const checksumFlag = (fhd >> 2) & 1;
    const didFlag = fhd & 3;
    if (!singleSegment) pos += 1;                    // Window_Descriptor
    pos += [0, 1, 2, 4][didFlag];                    // Dictionary_ID
    // Frame_Content_Size：FCS=0 且 single_segment=1 时仍有 1 字节（RFC 8878 §3.1.1.1.4）
    const fcsBytes = fcsFlag === 0 ? (singleSegment ? 1 : 0) : [0, 2, 4, 8][fcsFlag];
    pos += fcsBytes;
    for (;;) {
      if (pos + 3 > buf.length) throw new Error("截断：块头 @" + pos);
      const bh = buf[pos] | (buf[pos + 1] << 8) | (buf[pos + 2] << 16);
      pos += 3;
      const last = bh & 1;
      const type = (bh >> 1) & 3;
      const size = bh >> 3;
      if (type === 0 || type === 2) pos += size;      // raw / compressed
      else if (type === 1) pos += 1;                  // RLE
      else throw new Error("保留块类型 @" + pos);
      if (last) break;
    }
    if (checksumFlag) pos += 4;
    if (pos > buf.length) throw new Error("截断：帧结束越界 @" + start);
    frames.push({ start, end: pos, headerBytes: pos - start, skippable: false });
  }
  return frames;
}

function readSession(file) {
  const buf = fs.readFileSync(file);
  const frames = walkFrames(buf);
  const lines = [];
  const frameErrors = [];
  let seqs = 0, dups = 0;
  const seen = new Set();
  frames.forEach((f, idx) => {
    if (f.skippable) return;
    let text;
    try {
      text = zlib.zstdDecompressSync(buf.subarray(f.start, f.end)).toString("utf8");
    } catch (e) {
      frameErrors.push({ index: idx, error: String(e.message || e) });
      return;
    }
    for (const raw of text.split("\n")) {
      const line = raw.trim();
      if (!line) continue;
      lines.push({ text: line, frame: idx });
      try {
        const o = JSON.parse(line);
        if (o && typeof o.seq === "number") {
          seqs += 1;
          if (seen.has(o.seq)) dups += 1;
          seen.add(o.seq);
        }
      } catch (_) {
        // 语法错误留给上层统计
      }
    }
  });
  return { buf, frames, lines, frameErrors, seqs, dups };
}

function stats(file) {
  const { buf, frames, lines, frameErrors, seqs, dups } = readSession(file);
  let valid = 0, invalid = 0;
  const badLines = [];
  for (const l of lines) {
    try { JSON.parse(l.text); valid += 1; }
    catch (e) { invalid += 1; if (badLines.length < 5) badLines.push({ frame: l.frame, error: String(e.message || e).slice(0, 120) }); }
  }
  return {
    file,
    bytes: buf.length,
    frames: frames.length,
    skippable_frames: frames.filter(f => f.skippable).length,
    lines: lines.length,
    valid_json: valid,
    invalid_json: invalid,
    bad_lines: badLines,
    seq_count: seqs,
    dup_seq: dups,
    frame_errors: frameErrors.slice(0, 5),
    frame_error_count: frameErrors.length,
    // 单帧装下整份日志 = imxzt 说的「legacy 单帧布局」
    single_frame_layout: frames.filter(f => !f.skippable).length === 1 && lines.length > 1,
  };
}

function compressFrame(text) {
  return zlib.zstdCompressSync(Buffer.from(text, "utf8"), {
    params: { [zlib.constants.ZSTD_c_checksumFlag]: 1 },
  });
}

function rewrite(src, dst, opts) {
  const { lines } = readSession(src);
  const kept = [];
  const dropped = [];
  const seen = new Set();
  for (const l of lines) {
    let seq = null;
    try { const o = JSON.parse(l.text); if (o && typeof o.seq === "number") seq = o.seq; }
    catch (e) { throw new Error("第 " + (kept.length + dropped.length + 1) + " 行不是合法 JSON，拒绝重写：" + e.message); }
    if (opts.dedupeSeq && seq !== null && seen.has(seq)) {
      dropped.push({ seq, frame: l.frame });
      continue;
    }
    if (seq !== null) seen.add(seq);
    kept.push(l.text);
  }
  const chunks = kept.map(compressFrame);
  const out = Buffer.concat(chunks);
  fs.writeFileSync(dst, out);
  return {
    src, dst,
    frames_written: chunks.length,
    lines_written: kept.length,
    dropped_dup_seq: dropped.length,
    dropped_sample: dropped.slice(0, 5),
    bytes: out.length,
  };
}

function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  try {
    if (cmd === "stats") {
      const out = stats(rest[0]);
      process.stdout.write(JSON.stringify(out) + "\n");
      return;
    }
    if (cmd === "rewrite") {
      const [src, dst] = rest;
      const flags = rest.slice(2);
      const out = rewrite(src, dst, { dedupeSeq: flags.includes("--dedupe-seq") });
      process.stdout.write(JSON.stringify(out) + "\n");
      return;
    }
    throw new Error("用法：stats <file> | rewrite <src> <dst> [--dedupe-seq]");
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: String(e.message || e) }) + "\n");
    process.exit(1);
  }
}

main();
