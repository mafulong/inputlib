# !/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IME Dictionary Converter (no CLI params)
- Set parameters in the CONFIG section below, then run: python ime_convert_nocli.py
- Supports Baidu .bdict/.bcd and Sogou .scel
- Outputs alongside each input:
    <basename>.txt         (unique word list)
    <basename>.dict.yaml   (Rime dict: word \t pinyin \t freq)
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple, Dict
import os

# ===================== CONFIG =====================
ROOT_DIR = "."  # 改成你的目录（支持相对/绝对路径）
RECURSE = False  # 是否递归子目录
INCLUDE_BAIDU = True  # 是否处理 .bdict/.bcd
INCLUDE_SOGOU = False  # 是否处理 .scel
WRITE_TXT = True  # 是否导出 <name>.txt
WRITE_RIME = True  # 是否导出 <name>.dict.yaml

# Rime 词库名字策略：
RIME_NAME_MODE = "basename"  # "basename" 用文件名；"fixed" 用下面的 RIME_FIXED_NAME
RIME_FIXED_NAME = "custom_dict"

# 偏移与解析参数（通常不需要改动）
BAIDU_START_OFFSET = 0x350
SCEL_START_PY = 0x1540
SCEL_START_CHINESE = 0x2628


# ==================================================

@dataclass
class Entry:
    word: str
    pinyin: List[str]
    freq: int = 1


def _uniq_stable_words(entries: List[Entry]) -> List[str]:
    seen = set()
    out = []
    for e in entries:
        w = e.word.strip()
        if not w or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def write_words_txt(entries: List[Entry], out_path: str) -> None:
    words = _uniq_stable_words(entries)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for w in words:
            f.write(w + "\n")


def write_rime_yaml(entries: List[Entry], out_path: str, name: str) -> None:
    # 去重：同词取最大频次、首个拼音
    best: Dict[str, Tuple[List[str], int]] = {}
    for e in entries:
        cur = best.get(e.word)
        if cur is None or (e.freq or 0) > (cur[1] or 0):
            best[e.word] = (e.pinyin, e.freq if e.freq is not None else 1)

    header = f"""# Rime dictionary
# encoding: utf-8
---
name: {name}
version: "0.1"
sort: by_weight
use_preset_vocabulary: false
...
"""
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(header)
        for w, (py, freq) in best.items():
            py_str = " ".join(py) if py else ""
            f.write(f"{w}\t{py_str}\t{freq}\n")


# ------------------------- Baidu .bdict/.bcd -------------------------
BDICT_SM = [
    "c", "d", "b", "f", "g", "h", "ch", "j", "k", "l", "m", "n",
    "", "p", "q", "r", "s", "t", "sh", "zh", "w", "x", "y", "z",
]
BDICT_YM = [
    "uang", "iang", "iong", "ang", "eng", "ian", "iao", "ing", "ong",
    "uai", "uan", "ai", "an", "ao", "ei", "en", "er", "ua", "ie", "in", "iu",
    "ou", "ia", "ue", "ui", "un", "uo", "a", "e", "i", "o", "u", "v",
]


def _u16le(b: bytes, off: int) -> Tuple[int, int]:
    if off + 2 > len(b):
        raise EOFError
    return b[off] | (b[1 + off] << 8), off + 2


def parse_baidu(path: str, start_offset: int = BAIDU_START_OFFSET) -> List[Entry]:
    data = open(path, "rb").read()
    off = start_offset
    n = len(data)
    out: List[Entry] = []

    def remain() -> int:
        return n - off

    while remain() > 4:
        try:
            py_len, off = _u16le(data, off)
            freq, off = _u16le(data, off)
        except EOFError:
            break
        if remain() < 2:
            break
        peek0, peek1 = data[off], data[off + 1]

        # Type 3
        if peek0 == 0x00 and peek1 == 0x00:
            off += 2
            try:
                word_len, off = _u16le(data, off)
            except EOFError:
                break
            need = py_len * 2 + word_len * 2
            if remain() < need:
                break
            code = data[off:off + py_len * 2].decode("utf-16le", errors="ignore");
            off += py_len * 2
            word = data[off:off + word_len * 2].decode("utf-16le", errors="ignore");
            off += word_len * 2
            out.append(Entry(word=word, pinyin=[code], freq=freq))
            continue

        # Type 2 (english)
        sm_idx = peek0
        if sm_idx >= len(BDICT_SM) and sm_idx != 0xFF:
            off += 1
            if remain() < py_len:
                break
            eng = data[off:off + py_len].decode("ascii", errors="ignore");
            off += py_len
            out.append(Entry(word=eng, pinyin=[eng], freq=freq))
            continue

        # Type 1 (normal)
        pinyin: List[str] = []
        ok = True
        for _ in range(py_len):
            if remain() < 2:
                ok = False
                break
            sm_i, ym_i = data[off], data[off + 1];
            off += 2
            if sm_i == 0xFF:
                pinyin.append(chr(ym_i))
            else:
                if sm_i >= len(BDICT_SM) or ym_i >= len(BDICT_YM):
                    ok = False
                    break
                pinyin.append(BDICT_SM[sm_i] + BDICT_YM[ym_i])
        if not ok:
            break
        need = py_len * 2
        if remain() < need:
            break
        word = data[off:off + need].decode("utf-16le", errors="ignore");
        off += need
        out.append(Entry(word=word, pinyin=pinyin, freq=freq))

    return out


# ------------------------- Sogou .scel -------------------------
def _read_pinyin_table_scel(data: bytes, start_py: int, start_chinese: int) -> Dict[int, str]:
    pos = start_py + 4
    py_table: Dict[int, str] = {}
    while pos + 4 <= len(data) and pos < start_chinese:
        index = data[pos] | (data[pos + 1] << 8);
        pos += 2
        ln = data[pos] | (data[pos + 1] << 8);
        pos += 2
        if ln <= 0 or pos + ln > len(data):
            break
        py = data[pos:pos + ln].decode("utf-16le", errors="ignore");
        pos += ln
        py_table[index] = py
    return py_table


def _parse_py_indexes_scel(py_bytes: bytes, py_table: Dict[int, str]) -> List[str]:
    out: List[str] = []
    if len(py_bytes) % 2 == 1:
        py_bytes = py_bytes[:-1]
    for i in range(0, len(py_bytes), 2):
        idx = py_bytes[i] | (py_bytes[i + 1] << 8)
        p = py_table.get(idx, "")
        if p:
            out.append(p)
    return out


def parse_scel(path: str, start_py: int = SCEL_START_PY, start_chinese: int = SCEL_START_CHINESE) -> List[Entry]:
    data = open(path, "rb").read()
    py_table = _read_pinyin_table_scel(data, start_py, start_chinese)
    pos = start_chinese
    n = len(data)
    out: List[Entry] = []

    def remain() -> int:
        return n - pos

    def _u16le_mem(b: bytes, p: int) -> Tuple[int, int]:
        if p + 2 > len(b): raise EOFError
        return b[p] | (b[p + 1] << 8), p + 2

    while remain() > 8:
        try:
            same, pos = _u16le_mem(data, pos)
            py_idx_len, pos = _u16le_mem(data, pos)
        except EOFError:
            break
        if py_idx_len <= 0 or remain() < py_idx_len:
            break
        py_idx = data[pos:pos + py_idx_len];
        pos += py_idx_len
        py_list = _parse_py_indexes_scel(py_idx, py_table)

        for _ in range(same):
            if remain() < 2: break
            wlen, pos = _u16le_mem(data, pos)
            if wlen <= 0 or remain() < wlen: break
            word = data[pos:pos + wlen].decode("utf-16le", errors="ignore");
            pos += wlen

            if remain() < 2: break
            ext_len, pos = _u16le_mem(data, pos)
            if ext_len < 0 or remain() < ext_len: break
            ext = data[pos:pos + ext_len];
            pos += ext_len
            freq = int.from_bytes(ext[:2], "little", signed=False) if len(ext) >= 2 else 0

            out.append(Entry(word=word, pinyin=py_list, freq=freq))
    return out


# ------------------------- Runner -------------------------
BAIDU_SUFFIXES = {".bdict", ".bcd"}
SOGOU_SUFFIXES = {".scel"}


def process_dir(root: str) -> None:
    root_path = os.path.abspath(root)
    total_files = 0
    converted = 0
    for dirpath, _, filenames in os.walk(root_path):
        if not RECURSE and dirpath != root_path:
            continue
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            in_path = os.path.join(dirpath, fname)
            do_baidu = INCLUDE_BAIDU and ext in BAIDU_SUFFIXES
            do_sogou = INCLUDE_SOGOU and ext in SOGOU_SUFFIXES
            if not (do_baidu or do_sogou):
                continue

            total_files += 1
            try:
                if do_baidu:
                    entries = parse_baidu(in_path, start_offset=BAIDU_START_OFFSET)
                else:
                    entries = parse_scel(in_path, start_py=SCEL_START_PY, start_chinese=SCEL_START_CHINESE)

                base = os.path.splitext(in_path)[0]
                if WRITE_TXT:
                    write_words_txt(entries, base + ".txt")
                if WRITE_RIME:
                    dict_name = os.path.basename(base) if RIME_NAME_MODE == "basename" else RIME_FIXED_NAME
                    write_rime_yaml(entries, base + ".dict.yaml", name=dict_name)

                converted += 1
                kind = "Baidu" if do_baidu else "Sogou"
                outs = []
                if WRITE_TXT: outs.append(base + ".txt")
                if WRITE_RIME: outs.append(base + ".dict.yaml")
                print(f"[OK] {kind} -> {', '.join(outs)}  ({len(entries)} entries)")
            except Exception as e:
                print(f"[ERR] {in_path}: {e}")
    print(f"Done. scanned={total_files} converted={converted}")


def main():
    if not ROOT_DIR or ROOT_DIR == "/path/to/dir":
        print("请先在脚本顶部 CONFIG 中设置 ROOT_DIR 路径，再运行本脚本。")
        return
    process_dir(ROOT_DIR)


if __name__ == "__main__":
    main()
