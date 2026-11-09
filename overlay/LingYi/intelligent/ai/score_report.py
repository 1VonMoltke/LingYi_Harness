"""Read authoritative scores and score-change reasons from this run's engine replay."""

import argparse
import configparser
import json
import struct
import uuid
import zlib
from collections import defaultdict
from pathlib import Path

from engine.LingYi.entitys.gameenvs.common import Struct_pb2


SCORE_REASON_TYPE = 0x88  # display.index data type: THScoreChangeReason (not THScoreChange)


def find_engine_room(parent, engine_path):
    import psutil
    for child in parent.children(recursive=True):
        try:
            if child.name().lower() == "plat.exe" and Path(child.exe()).resolve() == engine_path.resolve():
                args = child.cmdline()
                if isinstance(args, (list, tuple)) and len(args) > 1:
                    return str(uuid.UUID(args[1]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            continue
    return None


def _scores_from_log(path, room_id, scenario):
    first, last = None, None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if '"TotalScore"' not in line:
                continue
            try:
                value = json.loads(line[line.index("{"):])
            except (ValueError, json.JSONDecodeError):
                continue
            if (value.get("RoomID") != room_id or
                value.get("ScenarioName") != scenario + ".scen" or not value.get("TotalScore")):
                continue
            if first is None:
                first = value
            last = value
    if last is None:
        raise ValueError("本局引擎日志尚无可确认的比分")
    return first, last


def _replay_config(path):
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
    config.read_string(text)
    sides = {}
    for entry in config.get("Scenario", "Oppositon", fallback="").split(";"):
        parts = entry.split(",")
        if len(parts) >= 2:
            sides[parts[1]] = parts[0]
    topics = {int(value): key for key, value in config.items("Topic")}
    return sides, topics


def _read_reasons(replay, topics, start_time):
    reasons, seen, warnings = defaultdict(list), set(), []
    with (replay / "display.index").open(encoding="ascii") as stream:
        for number, line in enumerate(stream, 1):
            try:
                fields = [int(value) for value in line.strip().split(",")]
                if len(fields) != 8:
                    raise ValueError("index column count")
                _, _, _, _, topic, data_type, offset, length = fields
                if data_type != SCORE_REASON_TYPE:
                    continue
                name = topics.get(topic, "")
                prefix = "SITUATION_DYNAMIC_"
                if not name.startswith(prefix) or name == prefix + "GLOBAL":
                    continue
                side_id = name[len(prefix):]
                # Topic names come from the replay, so forbid paths outside it.
                if Path(name).name != name or "/" in name or "\\" in name or offset < 32 or length < 6:
                    raise ValueError("invalid topic or range")
                identity = (topic, offset, length)
                if identity in seen:
                    continue
                seen.add(identity)
                with (replay / (name + ".dat")).open("rb") as data_file:
                    data_file.seek(offset)
                    block = data_file.read(length)
                if len(block) != length or struct.unpack_from("<I", block)[0] != length - 4:
                    raise ValueError("incomplete score record")
                message = Struct_pb2.THScoreChangeReason()
                message.ParseFromString(zlib.decompress(block[4:]))
                reasons[side_id].append({
                    "time": message.Time,
                    "simulation_seconds": (message.Time - start_time) / 1000,
                    "reason": message.Name or "未命名计分事件", "reason_id": message.ID,
                    "type": message.Type, "points": message.PointChange,
                    "related_unit_1": message.Para1, "related_unit_2": message.Para2,
                    "target_side": message.TargetSideName,
                    "evidence": "display.index:L%d" % number,
                })
            except Exception as error:
                warnings.append("计分索引第 %d 行无法读取：%s" % (number, type(error).__name__))
    return reasons, warnings


def build_score_report(data_root, room_id, scenario, ending):
    data_root = Path(data_root)
    room_id = str(uuid.UUID(room_id))
    logs = list((data_root / "Logs").glob("*#" + room_id))
    if len(logs) != 1:
        raise ValueError("未找到本局唯一的引擎日志目录")
    log_path = logs[0] / "Plat.log"
    first, last = _scores_from_log(log_path, room_id, scenario)
    # Match the room UUID, never whichever replay happens to be newest.
    replays = [path for path in (data_root / "Recordings").rglob("simulation.ini")
               if room_id in path.parts]
    names, reasons, notes = {}, {}, []
    replay = None
    if len(replays) == 1:
        replay = replays[0].parent
        try:
            names, topics = _replay_config(replays[0])
            reasons, notes = _read_reasons(replay, topics, last["StartTime"])
        except Exception as error:
            notes.append("回放计分来源无法读取：%s" % error)
    else:
        notes.append("本局回放不存在或不唯一，得分来源暂不可用")
    sides = []
    for side_id, total in last["TotalScore"].items():
        initial = first["TotalScore"].get(side_id)
        details = sorted(reasons.get(side_id, []), key=lambda row: row["time"])
        groups = {}
        for item in details:
            group = groups.setdefault(item["reason"], {"reason": item["reason"], "points": 0, "count": 0})
            group["points"] += item["points"]
            group["count"] += 1
        detail_total = sum(item["points"] for item in details)
        sides.append({"side_id": side_id, "side": names.get(side_id, "未映射阵营（%s）" % side_id),
                      "total": total, "initial": initial, "detail_total": detail_total,
                      "unattributed": None if initial is None else total - initial - detail_total,
                      "sources": sorted(groups.values(), key=lambda row: (-abs(row["points"]), row["reason"])),
                      "details": details})
    side_order = {"红方": 0, "蓝方": 1}
    sides.sort(key=lambda row: (side_order.get(row["side"], 2), row["side"]))
    return {"scenario": scenario, "room_id": room_id, "ending": ending,
            "final": ending == "scenario_complete" and last.get("State") == 4,
            "simulation_seconds": (last["Currentime"] - last["StartTime"]) / 1000,
            "engine_state": last.get("State"), "sides": sides, "notes": notes,
            "engine_log": str(log_path), "replay_directory": str(replay) if replay else None}


def _signed(value):
    return "未知" if value is None else "%+g" % value


def _cell(value):
    return str(value or "—").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def save_score_report(report, run_dir):
    run_dir = Path(run_dir)
    report["json_path"] = str(run_dir / "score_summary.json")
    report["markdown_path"] = str(run_dir / "score_summary.md")
    lines = ["# 本局得分明细", "", "想定：%s；%s。" %
             (report["scenario"], "终局比分" if report["final"] else "阶段比分，非终局结算"), ""]
    for side in report["sides"]:
        lines.extend(["## %s：%s 分" % (side["side"], _signed(side["total"])), "",
                      "初始 %s；明细合计 %s；未归因差额 %s。" %
                      (_signed(side["initial"]), _signed(side["detail_total"]), _signed(side["unattributed"])), "",
                      "| 得分来源 | 次数 | 分数 |", "| --- | ---: | ---: |"])
        for source in side["sources"]:
            lines.append("| %s | %d | %s |" % (_cell(source["reason"]), source["count"], _signed(source["points"])))
        if not side["sources"]:
            lines.append("| 未读取到计分来源 | — | — |")
        lines.extend(["", "### 逐次加扣分", "", "| 推演时间 | 来源 | 分数 | 相关单位1 | 相关单位2 | 证据 |",
                      "| --- | --- | ---: | --- | --- | --- |"])
        for item in side["details"]:
            seconds = int(item["simulation_seconds"])
            clock = "%02d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
            lines.append("| %s | %s | %s | %s | %s | %s |" %
                         (clock, _cell(item["reason"]), _signed(item["points"]), _cell(item["related_unit_1"]),
                          _cell(item["related_unit_2"]), item["evidence"]))
        lines.append("")
    lines.extend(["计分来源名称及分值均来自引擎记录；未归因差额不补猜来源。", "",
                  "引擎日志：`%s`" % report["engine_log"],
                  "回放目录：`%s`" % report["replay_directory"], ""])
    lines.extend("- " + note for note in report["notes"])
    Path(report["json_path"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(report["markdown_path"]).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--ending", default="scenario_complete")
    args = parser.parse_args()
    report = save_score_report(build_score_report(args.data_root, args.room_id, args.scenario, args.ending), args.run_dir)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
