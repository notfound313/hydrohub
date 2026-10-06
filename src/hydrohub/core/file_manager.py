from __future__ import annotations

import shutil
from pathlib import Path


class ProjectFileManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def checked(self, path: Path, *, allow_root: bool = True) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path must remain inside the active project.") from exc
        if not allow_root and resolved == self.root:
            raise ValueError("This operation cannot target the project root.")
        return resolved

    def directory_for(self, selected: Path | None) -> Path:
        if selected is None:
            return self.root
        checked = self.checked(selected)
        return checked if checked.is_dir() else checked.parent

    def create_file(self, directory: Path, name: str) -> Path:
        target = self.checked(directory / self._name(name), allow_root=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=False)
        return target

    def create_folder(self, directory: Path, name: str) -> Path:
        target = self.checked(directory / self._name(name), allow_root=False)
        target.mkdir(parents=True, exist_ok=False)
        return target

    def upload(self, sources: list[Path], directory: Path) -> list[Path]:
        destination = self.checked(directory)
        if not destination.is_dir():
            raise NotADirectoryError(destination)
        targets: list[tuple[Path, Path]] = []
        for source in sources:
            if not source.is_file():
                raise FileNotFoundError(source)
            target = self.checked(destination / source.name, allow_root=False)
            if target.exists():
                raise FileExistsError(target)
            targets.append((source, target))
        copied: list[Path] = []
        for source, target in targets:
            shutil.copy2(source, target)
            copied.append(target)
        return copied

    def move(self, source: Path, destination: Path) -> Path:
        source = self.checked(source, allow_root=False)
        destination = self.checked(destination)
        if not source.exists():
            raise FileNotFoundError(source)
        if not destination.is_dir():
            raise NotADirectoryError(destination)
        target = self.checked(destination / source.name, allow_root=False)
        if target.exists():
            raise FileExistsError(target)
        if source.is_dir() and destination.is_relative_to(source):
            raise ValueError("A folder cannot be moved into itself.")
        return Path(shutil.move(str(source), str(target)))

    def delete(self, path: Path) -> None:
        target = self.checked(path, allow_root=False)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        else:
            raise FileNotFoundError(target)

    @staticmethod
    def _name(value: str) -> Path:
        name = value.strip()
        if not name or Path(name).is_absolute():
            raise ValueError("Enter a relative file or folder name.")
        return Path(name)
