from __future__ import annotations

import argparse
import json

from station_mesh_guard.calibration.study import run_study
from station_mesh_guard.registry.runtime import DomainError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="离线传感器校准研究：参考站切换、维修批次与事件相关性下的校准收益验证"
    )
    parser.add_argument("--input", required=True, help="合成观测与配对/维修登记 JSON")
    parser.add_argument("--database", required=True, help="SQLite 台账路径（记录研究证据）")
    parser.add_argument("--output", required=True, help="校准研究 JSON 输出路径")
    args = parser.parse_args(argv)
    try:
        result = run_study(args.input, args.database, args.output)
    except DomainError as exc:
        print(f"校准研究输入或步骤无效：{exc}")
        return 2
    summary = {
        "study_id": result["study_id"],
        "output": args.output,
        "supported_devices": result["applicability"]["supported_devices"],
        "rejected_candidates": len(result["applicability"]["rejected_candidates"]),
        "no_evidence_cases": len(result["applicability"]["no_evidence_cases"]),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
