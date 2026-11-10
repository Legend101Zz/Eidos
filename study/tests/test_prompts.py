"""Parity checks for the study's only card-specific prompt insertion."""
import unittest

from study.common import STUDY, read_json, render_prompt


class PromptParity(unittest.TestCase):
    def test_all_arms_receive_identical_card_intent(self):
        cards = read_json(STUDY / 'cards.json')
        base = (STUDY / 'prompts/task.md').read_text()
        for card, rule in cards.items():
            normalized = []
            for arm in (1, 2, 3, 4):
                paragraph = (STUDY / f'prompts/tools_arm{arm}.md').read_text().strip()
                text = render_prompt(arm, card)
                self.assertEqual(text.count(rule['practical_bar']), 1)
                self.assertNotIn('{{', text)
                normalized.append(text.replace(paragraph, '{{TOOLS_PARAGRAPH}}'))
            self.assertEqual(len(set(normalized)), 1)
            self.assertEqual(normalized[0], base.replace('{{PRACTICAL_BAR}}', rule['practical_bar']))
        self.assertEqual(cards['D02'], cards['D12'])
        self.assertIn('no improvement is requested', cards['D01']['practical_bar'])

    def test_unknown_card_cannot_silently_lose_the_bar(self):
        with self.assertRaisesRegex(ValueError, 'unknown study card'):
            render_prompt(1, 'D99')
