"""Проверка линтера shell-обёрток (ops/lint_sh.py).

Ловушка, ради которой он написан: строка, оканчивающаяся на «\\», продолжается
следующей, и комментарий на ней уезжает в АРГУМЕНТЫ команды. Так `timeout`
получил слова из комментария, вышел с кодом 125 и systemd заглушил живой таймер.
Каждая проверка ниже названа мутацией, которую она ловит.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))

try:
    from lint_sh import find_bad_continuations
except ImportError:  # pragma: no cover
    find_bad_continuations = None

BS = "\\"  # один обратный слэш; собираем строки конкатенацией, чтобы не путаться
# в экранировании самих тестовых данных — на этом уже спотыкались.


class TestFindBadContinuations(unittest.TestCase):
    def setUp(self):
        if find_bad_continuations is None:
            self.skipTest("нет ops/lint_sh.py")

    def test_catches_comment_after_continuation(self):
        """Мутация: вернуть комментарий между «\\» и продолжением команды."""
        text = "\n".join([
            'timeout --kill-after=30 "$deadline" ' + BS,
            "  # -u обязателен: иначе journald нем",
            '  "$python" -u pipeline/run.py --mode "$mode"',
        ])
        self.assertEqual(find_bad_continuations(text),
                         [(2, "# -u обязателен: иначе journald нем")])

    def test_clean_continuation_passes(self):
        """Мутация: сделать проверку слишком строгой — она начнёт ругать здоровый код."""
        text = "\n".join([
            'timeout --kill-after=30 "$deadline" ' + BS,
            '  "$python" -u pipeline/run.py --mode "$mode"',
        ])
        self.assertEqual(find_bad_continuations(text), [])

    def test_ordinary_comment_passes(self):
        """Комментарий над командой — норма, а не находка."""
        text = "\n".join(["# так и надо", 'timeout "$d" "$py" run.py'])
        self.assertEqual(find_bad_continuations(text), [])

    def test_escaped_backslash_is_not_continuation(self):
        """Мутация: считать «\\\\» переносом — тогда любой путь Windows даёт ложняк."""
        text = "\n".join(['echo "C:' + BS + BS + '"', "# обычный комментарий"])
        self.assertEqual(find_bad_continuations(text), [])

    def test_crlf_does_not_hide_the_bug(self):
        """Мутация: забыть про \\r — файл с CRLF молча перестанет проверяться."""
        text = 'cmd ' + BS + "\r\n" + "  # комментарий\r\n" + "  arg\r\n"
        self.assertEqual(len(find_bad_continuations(text)), 1)

    def test_real_wrapper_is_clean(self):
        """Живые обёртки проекта обязаны проходить проверку."""
        for path in sorted((ROOT / "ops").glob("*.sh")):
            with self.subTest(script=path.name):
                self.assertEqual(
                    find_bad_continuations(path.read_text(encoding="utf-8")), [],
                    f"{path.name}: комментарий внутри переноса строки")


if __name__ == "__main__":
    unittest.main()
