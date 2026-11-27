"""用已有群聊 journal 回放 send_gate 的窗口/合并行为。

只做离线模拟，不调用 LLM，不修改插件运行状态。

用法：
    python tools/simulate_send_gate.py

默认读取：
    群聊记录_751966140/journal.jsonl
    群聊记录_957302634/journal.jsonl

模拟口径：
- addressed：只把“明确 @ 本体”的人类消息当 root；
- all：把每条人类消息都当 root（上限口径）；
- root 只合并同一发送者在 max_wait 内、max_window 内的后续消息；
- 不模拟 Jev/ONNX，只模拟 send_gate 的窗口结构。
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import json
import re
from pathlib import Path
from typing import Iterable


@dataclasses.dataclass(frozen=True)
class Message:
    ts: float
    text: str
    sender: str
    sender_id: str
    role: str
    raw: dict


@dataclasses.dataclass
class RootResult:
    group: str
    mode: str
    sender: str
    start: str
    count: int
    waited_ms: int
    gap_ms: int


def parse_ts(value: str) -> float | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt).timestamp()
        except (TypeError, ValueError):
            continue
    return None


def load_messages(path: Path) -> list[Message]:
    messages: list[Message] = []
    with path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_ts(str(row.get("timestamp") or ""))
            if ts is None:
                continue
            text = str(row.get("content") or "")
            sender = str(row.get("sender") or "")
            sender_id = str(row.get("sender_id") or "")
            role = str(row.get("role") or "")
            messages.append(Message(ts, text, sender, sender_id, role, row))
    messages.sort(key=lambda item: item.ts)
    return messages


def is_human(message: Message, bot_names: set[str], bot_ids: set[str]) -> bool:
    if message.role != "user":
        return False
    if message.sender in bot_names or message.sender_id in bot_ids:
        return False
    return True


def is_addressed(message: Message, self_id: str, self_names: set[str]) -> bool:
    text = message.text
    if self_id and re.search(rf"\[At:\s*{re.escape(self_id)}\]", text):
        return True
    if any(name and f"@{name}" in text for name in self_names):
        return True
    if f"[引用消息({self_id}:" in text:
        return True
    return False


def is_command_only(message: Message) -> bool:
    text = message.text.strip()
    return text.startswith(("#", "/")) and "[At:" not in text


def percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    values = sorted(values)
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * p))))
    return values[index]


def simulate(
    group: str,
    messages: list[Message],
    *,
    mode: str,
    self_id: str,
    self_names: set[str],
    bot_names: set[str],
    bot_ids: set[str],
    max_window: float,
    max_wait: float,
    min_wait: float,
) -> tuple[list[RootResult], list[Message], dict]:
    humans = [
        item
        for item in messages
        if is_human(item, bot_names, bot_ids)
        and item.text.strip()
        and not is_command_only(item)
    ]
    by_sender: dict[str, list[Message]] = collections.defaultdict(list)
    for item in humans:
        by_sender[item.sender].append(item)

    roots: list[RootResult] = []
    consumed: set[int] = set()
    for sender, items in by_sender.items():
        for index, root in enumerate(items):
            if index in consumed:
                continue
            if mode == "addressed" and not is_addressed(root, self_id, self_names):
                continue
            # 即使 root 不合并，也记录一个窗口。
            followers: list[Message] = [root]
            j = index + 1
            while j < len(items):
                nxt = items[j]
                if j in consumed:
                    j += 1
                    continue
                if nxt.ts - followers[-1].ts > max_wait:
                    break
                if nxt.ts - root.ts > max_window:
                    break
                followers.append(nxt)
                consumed.add(j)
                j += 1
            last = followers[-1]
            release = min(last.ts + min_wait, root.ts + max_window)
            roots.append(
                RootResult(
                    group=group,
                    mode=mode,
                    sender=sender,
                    start=dt.datetime.fromtimestamp(root.ts).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    count=len(followers),
                    waited_ms=max(0, int((release - root.ts) * 1000)),
                    gap_ms=max(0, int((last.ts - root.ts) * 1000)),
                )
            )

    all_gaps: list[int] = []
    for items in by_sender.values():
        for prev, cur in zip(items, items[1:]):
            all_gaps.append(int((cur.ts - prev.ts) * 1000))

    merged = [r for r in roots if r.count > 1]
    stats = {
        "group": group,
        "mode": mode,
        "human_messages": len(humans),
        "senders": len(by_sender),
        "roots": len(roots),
        "merged_roots": len(merged),
        "merge_rate": round(len(merged) / len(roots), 4) if roots else 0.0,
        "merged_messages": sum(r.count - 1 for r in roots),
        "roots_with_2plus": sum(1 for r in roots if r.count >= 2),
        "waited_ms_avg": int(sum(r.waited_ms for r in roots) / len(roots))
        if roots
        else 0,
        "waited_ms_p50": percentile([r.waited_ms for r in roots], 0.50),
        "waited_ms_p95": percentile([r.waited_ms for r in roots], 0.95),
        "gap_ms_p50": percentile(all_gaps, 0.50),
        "gap_ms_p90": percentile(all_gaps, 0.90),
        "burst_hist": dict(
            collections.Counter(
                str(min(r.count, 5)) if r.count < 5 else "5+"
                for r in roots
            )
        ),
        "max_window": max_window,
        "max_wait": max_wait,
        "min_wait": min_wait,
    }
    return roots, humans, stats


def render_markdown(results: list[dict], args: argparse.Namespace) -> str:
    lines = [
        "# send_gate 聊天记录模拟测试",
        "",
        f"- 生成时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- max_window：{args.max_window}s",
        f"- max_wait：{args.max_wait}s",
        f"- min_wait：{args.min_wait}s",
        "",
        "## 口径",
        "",
        "```text",
        "addressed：只把明确 @ 本体的人类消息当 root（较低口径）",
        "all：每条人类消息都当 root（上限口径）",
        "root 只合并同一发送者在 max_wait 内、max_window 内的后续消息",
        "不模拟 Jev/ONNX，只模拟发送窗口和合并结构",
        "```",
        "",
        "## 结果",
        "",
        "| 群 | 模式 | 人类消息 | roots | 有合并 root | 合并率 | 合并消息数 | 等待 P50 | 等待 P95 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in results:
        lines.append(
            f"| {item['group']} | {item['mode']} | {item['human_messages']} | "
            f"{item['roots']} | {item['merged_roots']} | {item['merge_rate']:.1%} | "
            f"{item['merged_messages']} | {item['waited_ms_p50']}ms | "
            f"{item['waited_ms_p95']}ms |"
        )
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "```text",
            "addressed 是最保守的真实请求近似；all 是理论上限。",
            "真正 send_gate 只会在用户请求已经产生后接管，因此实际值应落在两者之间。",
            "下一步应使用 attention_gate 的 action 日志和 bot 回复时间进一步校正 roots。",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(r"D:/QQbot"))
    parser.add_argument("--self-id", default="3909610392")
    parser.add_argument("--self-name", action="append", default=["哦鲸鲸", "鲸鲸"])
    parser.add_argument("--max-window", type=float, default=30)
    parser.add_argument("--max-wait", type=float, default=10)
    parser.add_argument("--min-wait", type=float, default=2)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--md-out", type=Path, default=None)
    args = parser.parse_args()

    groups = [
        ("751966140", args.root / "群聊记录_751966140" / "journal.jsonl"),
        ("957302634", args.root / "群聊记录_957302634" / "journal.jsonl"),
    ]
    bot_names: set[str] = set()
    bot_ids: set[str] = set()
    # 751 的 bots.json 是 ID 列表；957 的 bots.json 是名字列表。
    for group, path in groups:
        bots_path = path.parent / "bots.json"
        if not bots_path.exists():
            continue
        data = json.loads(bots_path.read_text(encoding="utf-8"))
        bot_ids.update(str(x) for x in data.get("ids", []))
        bot_names.update(str(x) for x in data.get("names", []))
    bot_names.update({"小德", "小德2号", "宵崎奏", "Tantalite", "蓝色大肥鱼"})

    all_results: list[dict] = []
    for group, path in groups:
        if not path.exists():
            continue
        messages = load_messages(path)
        for mode in ("addressed", "all"):
            _roots, _humans, stats = simulate(
                group,
                messages,
                mode=mode,
                self_id=args.self_id,
                self_names=set(args.self_name),
                bot_names=bot_names,
                bot_ids=bot_ids,
                max_window=args.max_window,
                max_wait=args.max_wait,
                min_wait=args.min_wait,
            )
            all_results.append(stats)
            print(json.dumps(stats, ensure_ascii=False))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(all_results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.md_out:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(render_markdown(all_results, args), encoding="utf-8")
        print(f"markdown -> {args.md_out}")


if __name__ == "__main__":
    main()
