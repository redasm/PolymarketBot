"""探查两个 config 的字段和前3行."""
import json
from datasets import load_dataset

REPO = "TimeSeventeen/Polymarket-v1"

for config in ["orderfilled", "daily_aligned"]:
    print(f"\n{'='*20} {config} {'='*20}")
    try:
        ds = load_dataset(REPO, config, split="train", streaming=True)
        for i, row in enumerate(ds):
            if i == 0:
                print("字段:", list(row.keys()))
            print(f"行{i}:", json.dumps({k: str(v)[:100] for k, v in row.items()}, ensure_ascii=False))
            if i >= 2:
                break
    except Exception as e:
        print(f"失败: {e}")
