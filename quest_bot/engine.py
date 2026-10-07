"""Движок квеста: загрузка сюжета из YAML и логика переходов. Ничего не знает о Telegram."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Choice:
    text: str
    goto: str
    need: list[str] = field(default_factory=list)     # показывать, только если есть ВСЕ эти флаги
    unless: list[str] = field(default_factory=list)   # скрыть, если есть ЛЮБОЙ из этих флагов
    give: list[str] = field(default_factory=list)     # выдать флаги/предметы
    take: list[str] = field(default_factory=list)     # забрать флаги/предметы

    def visible(self, flags: set[str]) -> bool:
        return set(self.need) <= flags and not (set(self.unless) & flags)


@dataclass
class Scene:
    id: str
    text: str
    choices: list[Choice]
    ending: str | None = None  # название концовки, если сцена финальная


class Story:
    def __init__(self, path: str | Path):
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        self.title: str = data["title"]
        self.start: str = data["start"]
        self.scenes: dict[str, Scene] = {}
        for sid, raw in data["scenes"].items():
            choices = [
                Choice(
                    text=c["text"],
                    goto=c["goto"],
                    need=_as_list(c.get("need")),
                    unless=_as_list(c.get("unless")),
                    give=_as_list(c.get("give")),
                    take=_as_list(c.get("take")),
                )
                for c in raw.get("choices") or []
            ]
            self.scenes[sid] = Scene(sid, raw["text"].strip(), choices, raw.get("ending"))
        self._check()

    @property
    def endings(self) -> dict[str, str]:
        """id сцены-концовки -> название концовки."""
        return {s.id: s.ending for s in self.scenes.values() if s.ending}

    def visible_choices(self, scene_id: str, flags: set[str]) -> list[tuple[int, Choice]]:
        """Варианты, доступные игроку. Индекс — позиция в полном списке, он стабилен для кнопок."""
        return [(i, c) for i, c in enumerate(self.scenes[scene_id].choices) if c.visible(flags)]

    def choose(self, scene_id: str, flags: set[str], index: int) -> tuple[str, set[str]] | None:
        """Применяет выбор. Возвращает (новая сцена, новые флаги) или None, если выбор недопустим."""
        choices = self.scenes[scene_id].choices
        if not 0 <= index < len(choices) or not choices[index].visible(flags):
            return None
        c = choices[index]
        return c.goto, (flags | set(c.give)) - set(c.take)

    def _check(self) -> None:
        if self.start not in self.scenes:
            raise ValueError(f"Стартовая сцена '{self.start}' не найдена")
        for s in self.scenes.values():
            for c in s.choices:
                if c.goto not in self.scenes:
                    raise ValueError(f"Сцена '{s.id}': переход в несуществующую сцену '{c.goto}'")
            if not s.ending and not s.choices:
                raise ValueError(f"Сцена '{s.id}' не концовка, но в ней нет вариантов выбора")


def _as_list(value) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)
