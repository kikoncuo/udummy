"""CPU-only: the answer filter drops reasoning blocks and control tags, whatever way the stream is split."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

CASES = [
    ("<|message_model|><|content_thinking|>The user asks in Spanish. Need one sentence.<|end_message|><|message_model|><|content_text|>La capital de Francia es París.<|end_message|>", "La capital de Francia es París."),
    ("<think>\nhmm, rectangle and circle\n</think>\n\nUn rectángulo rojo y un círculo azul.", "\n\nUn rectángulo rojo y un círculo azul."),
    ("C: una frase larga que nadie más use.", "C: una frase larga que nadie más use."),
    ("<|message_model|><|content_thinking|>still thinking when the limit hits", ""),
    ("a < b y 3<4, sin etiquetas.", "a < b y 3<4, sin etiquetas."),
]


class AnswerOnly(unittest.TestCase):
    def test_every_split(self):
        for text, want in CASES:
            for size in (1, 2, 3, 5, 7, 13, len(text)):
                got = "".join(core.answer_only(iter([text[i:i + size] for i in range(0, len(text), size)])))
                self.assertEqual(got, want, (text[:30], size))


if __name__ == "__main__":
    unittest.main()
