"""Playlist logic: shuffle-bag (no repeats until every file has played), repeat modes, history."""

from __future__ import annotations

import os
import random


def scan(media_dir: str, extensions, recursive: bool = True) -> list[str]:
    exts = {"." + e.lower().lstrip(".") for e in extensions}
    out: list[str] = []
    if not os.path.isdir(media_dir):
        return out

    def walk(d: str, depth: int) -> None:
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.startswith("."):  # skip ._ mac files, .Trashes, etc.
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if recursive and depth < 4:
                            walk(e.path, depth + 1)
                    elif os.path.splitext(e.name)[1].lower() in exts:
                        out.append(e.path)
        except OSError:
            pass

    walk(media_dir, 0)
    out.sort(key=str.lower)
    return out


def title_of(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


class Playlist:
    def __init__(self, shuffle: bool = True, repeat: str = "all", rng: random.Random | None = None):
        self.files: list[str] = []
        self.shuffle = shuffle
        self.repeat = repeat  # all | one | off
        self.rng = rng or random.Random()
        self._order: list[str] = []
        self._pos = -1
        self.history: list[str] = []

    # ---- library ----
    def set_files(self, files: list[str]) -> bool:
        """Replace the file list. Returns True if it changed."""
        if files == self.files:
            return False
        current = self.current
        self.files = list(files)
        self._rebuild(keep=current)
        self.history = [h for h in self.history if h in self.files]
        return True

    def _rebuild(self, keep: str | None = None) -> None:
        order = list(self.files)
        if self.shuffle:
            self.rng.shuffle(order)
            # never play the same file twice in a row across bag boundaries
            if keep and len(order) > 1 and order[0] == keep:
                order.append(order.pop(0))
        if keep in order:
            if self.shuffle:
                order.remove(keep)
                order.insert(0, keep)
            self._pos = order.index(keep)
        else:
            self._pos = -1
        self._order = order

    def set_shuffle(self, on: bool) -> None:
        if on != self.shuffle:
            self.shuffle = on
            self._rebuild(keep=self.current)

    # ---- navigation ----
    @property
    def current(self) -> str | None:
        if 0 <= self._pos < len(self._order):
            return self._order[self._pos]
        return None

    @property
    def index(self) -> int:
        """Index of the current file in the (alphabetical) library, -1 if none."""
        cur = self.current
        return self.files.index(cur) if cur in self.files else -1

    def _remember(self, path: str | None) -> None:
        if path:
            self.history.append(path)
            del self.history[:-50]

    def next(self, auto: bool = False) -> str | None:
        """Advance. auto=True means the file ended on its own (repeat-one applies)."""
        if not self._order:
            return None
        if auto and self.repeat == "one" and self.current:
            return self.current
        prev = self.current
        self._remember(prev)
        self._pos += 1
        if self._pos >= len(self._order):
            if auto and self.repeat == "off":
                self._pos = len(self._order)  # past the end -> stopped
                return None
            last = self._order[-1] if self._order else None
            self._pos = 0
            if self.shuffle:
                self.rng.shuffle(self._order)
                if len(self._order) > 1 and self._order[0] == last:
                    self._order.append(self._order.pop(0))
        return self.current

    def previous(self) -> str | None:
        if not self._order:
            return None
        while self.history:
            p = self.history.pop()
            if p in self._order:
                self._pos = self._order.index(p)
                return p
        self._pos = (self._pos - 1) % len(self._order)
        return self.current

    def jump(self, target) -> str | None:
        """Jump to a file by library index (int), path, or title (str)."""
        path = None
        if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
            i = int(target)
            if 0 <= i < len(self.files):
                path = self.files[i]
        elif isinstance(target, str):
            if target in self.files:
                path = target
            else:
                for f in self.files:
                    if title_of(f) == target or os.path.basename(f) == target:
                        path = f
                        break
        if not path:
            return None
        self._remember(self.current)
        self._pos = self._order.index(path)
        return path

    def titles(self) -> list[str]:
        return [title_of(f) for f in self.files]
