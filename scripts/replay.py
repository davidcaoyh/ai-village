"""Print a past session. Twelve lines, because the event log did the work."""

import sys

from village.config import load_settings
from village.store import Store


def main() -> None:
    store = Store(load_settings(require_key=False).db_path)
    session = sys.argv[1] if len(sys.argv) > 1 else store.latest_session()
    if not session:
        print("no sessions yet")
        return
    for e in store.tail(session):
        p = e["payload"]
        body = (p.get("message") or p.get("text") or p.get("summary")
                or p.get("name") or p.get("kind"))
        print(f"{e['id']:>5}  {e['agent'] or '-':<9} {e['type']:<8} {str(body)[:150]}")


if __name__ == "__main__":
    main()
