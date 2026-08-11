"""Проверка файлов .github/workflows: они обязаны разбираться и иметь триггеры.

ПОЧЕМУ это тест, а не «GitHub сам скажет». Не скажет. Воркфлоу с битым YAML
GitHub просто НЕ ВИДИТ: в списке он показывается именем файла вместо имени из
`name:`, запуск по кнопке отвечает «Workflow does not have workflow_dispatch
trigger», а расписание не работает вовсе — и всё это без единой ошибки. Ровно
так молча не завёлся сторож витрины: внутри блока `run: |` лежал многострочный
python3 -c, чьи строки начинались с нулевой колонки и рвали блок.

Разбор делаем встроенным парсером: PyYAML в проекте нет и не будет (конституция
проекта — только стандартная библиотека), а нам нужно проверить ровно три вещи:
файл читается как YAML-подобная структура с отступами, у него есть `name`,
и у него есть хотя бы один триггер.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def top_level_keys(text):
    """Ключи нулевого отступа вне блочных скаляров.

    Достаточно, чтобы поймать разрыв блока: строка, начавшаяся с нулевой колонки
    внутри `run: |`, немедленно становится «ключом верхнего уровня» и попадает
    сюда — а в исправном файле их ровно четыре-пять и все известны.
    """
    keys = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if not line or line.startswith("#") or line[0] in " \t":
            continue
        key = line.split(":", 1)[0].strip()
        if key:
            keys.append(key)
    return keys


ALLOWED_TOP = {"name", "on", "permissions", "jobs", "env", "concurrency", "defaults"}


class TestWorkflows(unittest.TestCase):
    def test_workflows_exist(self):
        self.assertTrue(WORKFLOWS, "не найдено ни одного файла .github/workflows/*.yml")

    def test_no_stray_top_level_keys(self):
        """Мутация: вернуть многострочный python3 -c внутрь `run: |` — строки с нулевой
        колонки станут ключами верхнего уровня, и GitHub перестанет видеть воркфлоу."""
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                keys = top_level_keys(path.read_text(encoding="utf-8"))
                stray = [k for k in keys if k not in ALLOWED_TOP]
                self.assertEqual(stray, [], f"{path.name}: посторонние ключи верхнего "
                                            f"уровня {stray} — вероятно, разорван блок run")

    def test_has_name_and_trigger(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                text = path.read_text(encoding="utf-8")
                keys = top_level_keys(text)
                self.assertIn("name", keys, f"{path.name}: нет name — GitHub покажет имя файла")
                self.assertIn("on", keys, f"{path.name}: нет секции on — воркфлоу мёртв")
                has_trigger = any(t in text for t in
                                  ("workflow_dispatch", "schedule", "push", "pull_request",
                                   "workflow_run"))
                self.assertTrue(has_trigger, f"{path.name}: не нашлось ни одного триггера")

    def test_run_blocks_are_indented(self):
        """Каждая непустая строка внутри `run: |` обязана быть глубже самого `run:`."""
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                lines = path.read_text(encoding="utf-8").split("\n")
                inside, base = False, 0
                for i, raw in enumerate(lines, start=1):
                    line = raw.rstrip("\r")
                    if not inside:
                        stripped = line.strip()
                        if stripped.startswith("run:") and stripped.endswith(("|", "|-", ">")):
                            inside = True
                            base = len(line) - len(line.lstrip())
                        continue
                    if not line.strip():
                        continue
                    indent = len(line) - len(line.lstrip())
                    if indent <= base:
                        # Блок закончился — это нормально, если строка похожа на ключ YAML.
                        inside = False
                        self.assertRegex(
                            line.strip(), r"^[-A-Za-z_][\w -]*:|^- ",
                            f"{path.name}:{i}: строка вышла из блока run, но не является "
                            f"ключом YAML — блок разорван")


if __name__ == "__main__":
    unittest.main()
