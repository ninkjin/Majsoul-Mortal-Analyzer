import unittest
from pathlib import Path


VIEWER_HTML = Path(__file__).resolve().parents[2] / "mortal-output-viewer.html"


class ViewerHtmlTest(unittest.TestCase):
    def test_settlement_is_gated_by_current_event_index(self):
        html = VIEWER_HTML.read_text(encoding="utf-8")

        self.assertIn("function resultEventIndexForRound(roundIndex)", html)
        self.assertIn("function roundResultEvent(choice, eventIndex)", html)
        self.assertIn("if (eventIndex < resultIndex) return null;", html)
        self.assertIn("settlementHtml(choice, board, eventIndex)", html)

    def test_next_choice_stops_on_round_result_before_next_round(self):
        html = VIEWER_HTML.read_text(encoding="utf-8")

        self.assertIn("const resultIndex = resultEventIndexForRound(current.roundIndex);", html)
        self.assertIn("state.currentEventIndex = resultIndex;", html)
        self.assertIn("nextChoice?.roundIndex !== current.roundIndex", html)

    def test_choice_tile_highlights_show_ai_and_actual_discards(self):
        html = VIEWER_HTML.read_text(encoding="utf-8")

        self.assertIn("function choiceTileHighlights(choice, enabled)", html)
        self.assertIn("function choiceHighlightsEnabled(choice, eventIndex)", html)
        self.assertIn("state.currentEventIndex === null || eventIndex === choice.eventIndex", html)
        self.assertIn("choiceTileHighlights(choice, choiceHighlightsEnabled(choice, eventIndex))", html)
        self.assertIn("choice.actual?.type === 'dahai'", html)
        self.assertIn("actual: same ? null : actual", html)
        self.assertIn("ai-recommended", html)
        self.assertIn("actual-discard", html)

    def test_viewer_reads_current_data_from_viewer_data_folder(self):
        html = VIEWER_HTML.read_text(encoding="utf-8")

        self.assertIn("fetch('viewer-data/log.json'", html)
        self.assertIn("fetch('viewer-data/mortal-output-p2-mapped.jsonl'", html)
        self.assertIn("fetch('viewer-data/majsoul-tenhou-current.json'", html)
        self.assertIn("fetch('viewer-data/mortal-viewer-config.json'", html)
        self.assertIn("viewer-data/log.json + viewer-data/mortal-output-p2-mapped.jsonl", html)
        self.assertNotIn("2026_2_19_Gold_Room_South.json", html)

    def test_post_riichi_forced_draw_discards_are_not_ai_choice_nodes(self):
        html = VIEWER_HTML.read_text(encoding="utf-8")

        self.assertIn("function shouldShowAiChoice(output, eventIndex)", html)
        self.assertIn("function isForcedRiichiTsumogiriChoice(output, eventIndex)", html)
        self.assertIn("function playerRiichiBeforeEvent(eventIndex)", html)
        self.assertIn("event?.type !== 'tsumo' || event.actor !== PLAYER_ID", html)
        self.assertIn("output?.type !== 'dahai'", html)
        self.assertIn("return !maskIndexes.some(index => index === 42 || index === 43);", html)
        self.assertIn("shouldShowAiChoice(row.reaction, row.event_index)", html)

    def test_post_riichi_forced_passes_and_hora_labels_are_contextual(self):
        html = VIEWER_HTML.read_text(encoding="utf-8")

        self.assertIn("function isForcedRiichiPassChoice(output, eventIndex)", html)
        self.assertIn("if (isForcedRiichiPassChoice(output, eventIndex)) return false;", html)
        self.assertIn("output?.type !== 'none'", html)
        self.assertIn("function postRiichiQValueLabels(choice, count, maskIndexes)", html)
        self.assertIn("return `不和 打 ${tileText(tile)}`;", html)
        self.assertIn("if (index === 37 || index === 45) return '不和';", html)
        self.assertIn("const postRiichiLabels = postRiichiQValueLabels(choice, count, maskIndexes);", html)


if __name__ == "__main__":
    unittest.main()
