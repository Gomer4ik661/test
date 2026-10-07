"""Проверка сюжета: битые переходы, тупики, недостижимые сцены и концовки.
Запуск: python validate.py [story.yaml]"""
from __future__ import annotations

import sys
from collections import deque

from engine import Story


def main(path: str) -> int:
    try:
        story = Story(path)
    except Exception as e:  # битый YAML или переход в несуществующую сцену
        print(f"❌ {e}")
        return 1

    # Обходим все реально возможные состояния (сцена + набор флагов).
    start = (story.start, frozenset())
    seen = {start}
    queue = deque([start])
    problems = []
    while queue:
        scene_id, flags = queue.popleft()
        visible = story.visible_choices(scene_id, set(flags))
        if not story.scenes[scene_id].ending and not visible:
            problems.append(f"тупик: сцена '{scene_id}' без доступных вариантов при флагах {sorted(flags)}")
        for i, _ in visible:
            nxt_scene, nxt_flags = story.choose(scene_id, set(flags), i)
            state = (nxt_scene, frozenset(nxt_flags))
            if state not in seen:
                seen.add(state)
                queue.append(state)

    reached = {s for s, _ in seen}
    for sid in story.scenes:
        if sid not in reached:
            problems.append(f"сцена '{sid}' недостижима")
        if len(f"go:{sid}:99".encode()) > 64:
            problems.append(f"id сцены '{sid}' слишком длинный для кнопки Telegram")

    endings = story.endings
    print(f"«{story.title}»: сцен {len(story.scenes)}, концовок {len(endings)}, состояний {len(seen)}")
    for sid, name in endings.items():
        print(f"  {'✅' if sid in reached else '❌'} {name} ({sid})")
    for p in problems:
        print(f"❌ {p}")
    print("Всё в порядке!" if not problems else f"Найдено проблем: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "story.yaml"))
