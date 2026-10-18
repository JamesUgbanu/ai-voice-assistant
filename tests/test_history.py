import unittest
from app.history import append_turn, clean_history


class HistoryTest(unittest.TestCase):
    def test_appends_completed_turns_and_caps_tail(self):
        history = []
        for i in range(8):
            append_turn(history, f"question {i}", f"answer {i}")
        self.assertEqual(len(history), 6)
        self.assertEqual(history[0]["question"], "question 2")
        self.assertEqual(history[-1]["question"], "question 7")

    def test_ignores_empty_values(self):
        history = []
        append_turn(history, "", "answer")
        append_turn(history, "question", "   ")
        self.assertEqual(history, [])

    def test_truncates_fields(self):
        history = []
        append_turn(history, "q" * 600, "a" * 2500)
        self.assertEqual(len(history[0]["question"]), 500)
        self.assertEqual(len(history[0]["answer"]), 2000)


    def test_clean_history_drops_invalid_fields_and_caps_input(self):
        raw = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(8)]
        raw += [None, {"question": "", "answer": "x"}, {"question": "q", "answer": "  "}]
        out = clean_history(raw, max_turns=6)
        self.assertEqual(len(out), 6)
        self.assertEqual(out[0]["question"], "q2")
        self.assertEqual(out[-1]["answer"], "a7")
        self.assertEqual(clean_history("not-a-list"), [])

    def test_clean_history_bounds_fields(self):
        out = clean_history([{"question": "q" * 600, "answer": "a" * 2500}])
        self.assertEqual(len(out[0]["question"]), 500)
        self.assertEqual(len(out[0]["answer"]), 2000)


if __name__ == "__main__":
    unittest.main()
