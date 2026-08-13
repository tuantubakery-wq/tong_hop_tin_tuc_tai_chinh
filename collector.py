from __future__ import annotations

import json
import os
from datetime import datetime

from data_sources import fetch_all


def main() -> None:
    api_key = os.getenv("GEMINI_API_KEY") or None
    city = os.getenv("BDS_CITY", "TP.HCM")
    bundle = fetch_all(api_key=api_key, city=city)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "city": city,
        "datasets": {k: v.to_jsonable() for k, v in bundle.items()},
    }
    os.makedirs("data", exist_ok=True)
    with open("data/auto_snapshot.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({k: v.status for k, v in bundle.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
