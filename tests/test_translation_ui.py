"""Synthetic mobile translation UI: visible fallback and no duplicate taps."""
import os
import unittest

from tests.test_webxdc_browser import browser_fixture


class TranslationUiTests(unittest.TestCase):
    def test_approximate_provider_is_labeled_and_busy_button_is_guarded(self):
        engines = ["chromium"] + (["webkit"] if os.environ.get("MICHIFLIN_WEBKIT_TESTS") == "1" else [])
        for engine in engines:
            for language in ("es", "en"):
                with self.subTest(engine=engine, language=language), browser_fixture(language, {"width":375, "height":800}, engine=engine) as (page, errors):
                    page.evaluate("""() => {
                      const original = bridge.call.bind(bridge);
                      window.translationCalls = 0;
                      bridge.call = (op, args) => {
                        if (op !== 'translate') return original(op, args);
                        window.translationCalls++;
                        return new Promise(resolve => { window.finishTranslation = resolve; });
                      };
                      go('translate');
                    }""")
                    page.locator('#view textarea').fill('hola mundo')
                    button = page.get_by_role('button', name='Traducir' if language == 'es' else 'Translate', exact=True)
                    button.click()
                    self.assertTrue(button.is_disabled())
                    # Even a synthetic second click event must not create another request.
                    button.evaluate("b => b.dispatchEvent(new MouseEvent('click', {bubbles:true}))")
                    self.assertEqual(page.evaluate('window.translationCalls'), 1)
                    page.evaluate("window.finishTranslation({text:'Hello world',detected:'es',provider:'Apertium (beta)'})")
                    page.locator('.translation .translated').wait_for()
                    self.assertIn('Hello world', page.locator('.translation').inner_text())
                    self.assertIn('Apertium (beta)', page.locator('.translation').inner_text())
                    self.assertIn('orientativa' if language == 'es' else 'Approximate', page.locator('.translation').inner_text())
                    self.assertFalse(button.is_disabled())
                    self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
