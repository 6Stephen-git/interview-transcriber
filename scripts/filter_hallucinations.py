#!/usr/bin/env python3
"""
Whisper Hallucination Filter
Removes obvious hallucinations from SRT files like repeated "Thank you" during silence.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import srt

# 常见英文幻觉（多来自 YouTube 训练数据）
HALLUCINATION_PATTERNS = [
    r"^thank you\.?$",
    r"^thanks\.?$",
    r"^thank you very much\.?$",
    r"^thanks for watching\.?$",
    r"^don't forget to like and subscribe\.?$",
    r"^subscribe\.?$",
    r"^like and subscribe\.?$",
    r"^\.+$",  # 只有句点
    r"^$",  # 空内容
]

SHORT_FILLERS = {"thank you", "thanks", "yes", "no", "okay", "ok", "嗯", "啊", "呃", "哦"}


def utterance_weight(text: str) -> int:
    """估算有效内容量：中日韩字符按字计，拉丁/数字按词计。

    不能用 str.split()：中文常无空格，整句会被当成 1 个 token，
    从而在长静音后被误判为幻觉并连锁删除。
    """
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    latin_words = len(re.findall(r"[A-Za-z0-9]+", text))
    return cjk + latin_words


def is_hallucination(subtitle, previous_subtitle=None) -> bool:
    """判断字幕条目是否像明显幻觉，而非真实发言。"""
    text = subtitle.content.strip()
    lowered = text.lower()

    for pattern in HALLUCINATION_PATTERNS:
        if re.match(pattern, lowered):
            return True

    # 极短口头禅；长度限制避免误伤正常短句
    if utterance_weight(text) <= 3 and lowered in SHORT_FILLERS:
        return True

    # 长静音后的极短噪声（如单独“嗯”“ok”），不删除有实质内容的句子
    if previous_subtitle:
        gap = subtitle.start - previous_subtitle.end
        if gap.total_seconds() > 20 and utterance_weight(text) <= 2:
            return True

    return False


def filter_srt_file(input_file, output_file=None):
    """从 SRT 中过滤幻觉条目。"""
    if not output_file:
        input_path = Path(input_file)
        output_file = input_path.parent / f"{input_path.stem}_filtered{input_path.suffix}"

    try:
        with open(input_file, "r", encoding="utf-8") as f:
            content = f.read()
        subtitles = list(srt.parse(content))
        print(f"📄 Original subtitles: {len(subtitles)} entries")
    except Exception as e:
        print(f"❌ Error reading {input_file}: {e}")
        return False

    filtered_subtitles = []
    removed_count = 0
    previous_subtitle = None

    for subtitle in subtitles:
        if is_hallucination(subtitle, previous_subtitle):
            removed_count += 1
            print(f"🗑️  Removed hallucination: '{subtitle.content.strip()}'")
        else:
            subtitle.index = len(filtered_subtitles) + 1
            filtered_subtitles.append(subtitle)
            previous_subtitle = subtitle

    print(f"✅ Filtered subtitles: {len(filtered_subtitles)} entries")
    print(f"🎯 Removed {removed_count} hallucinations")

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(srt.compose(filtered_subtitles))
        print(f"💾 Saved filtered file: {output_file}")
        return True
    except Exception as e:
        print(f"❌ Error writing {output_file}: {e}")
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python filter_hallucinations.py <input.srt> [output.srt]")
        print("\nFilters obvious hallucinations from Whisper SRT files")
        print("If no output file specified, creates <input>_filtered.srt")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    if not Path(input_file).exists():
        print(f"❌ Input file not found: {input_file}")
        sys.exit(1)

    print(f"🧹 Filtering hallucinations from: {input_file}")

    if filter_srt_file(input_file, output_file):
        print("✅ Filtering complete!")
    else:
        print("❌ Filtering failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
