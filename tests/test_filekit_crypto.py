"""Cross-language encrypted password envelope, including no WebCrypto subtle."""
import base64
import os
import hashlib
import unittest

from michiflin import filekit, filekit_crypto
from tests.test_filekit_store import ROOT, page_fixture, sync_playwright


@unittest.skipUnless(sync_playwright, "playwright is not installed")
class PasswordEnvelopeTests(unittest.TestCase):
    def test_browser_to_python_with_tamper_and_restart_guards(self):
        engines = ["chromium"] + (["webkit"] if os.environ.get("MICHIFLIN_WEBKIT_TESTS") == "1" else [])
        for engine in engines:
            with self.subTest(engine=engine), page_fixture(engine) as page:
                page.add_script_tag(path=str(ROOT / "vendor/noble/filekit-crypto.js"))
                info = filekit_crypto.public_key()
                envelope = page.evaluate("""([info,text])=>{
                  // Some custom WebView schemes lack subtle even when getRandomValues exists.
                  try { Object.defineProperty(crypto,'subtle',{value:undefined,configurable:true}); } catch (_) {}
                  return FileKitCrypto.seal(info,text);
                }""", [info, "clave sintética á😀"])
                self.assertEqual(filekit_crypto.decrypt(envelope), "clave sintética á😀")
                self.assertNotIn("clave", str(envelope))
                changed = dict(envelope)
                body = bytearray(base64.b64decode(changed["ciphertext"]))
                body[0] ^= 1
                changed["ciphertext"] = base64.b64encode(body).decode()
                with self.assertRaisesRegex(filekit.FileKitError, "secret_invalid"):
                    filekit_crypto.decrypt(changed)
                changed = dict(envelope, keyId="expirada")
                with self.assertRaisesRegex(filekit.FileKitError, "secret_expired"):
                    filekit_crypto.decrypt(changed)

    def test_text_and_reply_are_encrypted_in_both_directions(self):
        engines = ["chromium"] + (["webkit"] if os.environ.get("MICHIFLIN_WEBKIT_TESTS") == "1" else [])
        for engine in engines:
            with self.subTest(engine=engine), page_fixture(engine) as page:
                page.add_script_tag(path=str(ROOT / "vendor/noble/filekit-crypto.js"))
                info = filekit_crypto.public_key()
                text = "Línea uno\nLínea dos á\t😀"
                envelope = page.evaluate("([info,text])=>FileKitCrypto.sealText(info,text)", [info, text])
                self.assertEqual(filekit_crypto.decrypt_text(envelope), text)
                self.assertNotIn("Línea", str(envelope))
                public = page.evaluate("() => { window.reply=FileKitCrypto.replyKey(); return window.reply.publicKey; }")
                encrypted = filekit_crypto.encrypt_reply(public, text.encode())
                self.assertNotIn("Línea", str(encrypted))
                result = page.evaluate("enc=>({text:new TextDecoder().decode(FileKitCrypto.openReply(window.reply,enc)), "
                                       "zeroed:window.reply.secretKey.every(x=>x===0)})", encrypted)
                self.assertEqual(result, {"text": text, "zeroed": True})
                digest = page.evaluate("() => FileKitCrypto.hashBlob(new Blob(['contenido sintético á']))")
                self.assertEqual(digest, hashlib.sha256('contenido sintético á'.encode()).hexdigest())
                self.assertEqual(page.evaluate("() => FileKitCrypto.hashBlob(new Blob([]))"), hashlib.sha256(b'').hexdigest())

    def test_malformed_envelope_never_leaks_details(self):
        info = filekit_crypto.public_key()
        for value in (None, {}, {"keyId": info["keyId"]}, {"keyId": info["keyId"], "ephemeral": "!", "nonce": "!", "ciphertext": "!"}):
            with self.subTest(value=value), self.assertRaises(filekit.FileKitError):
                filekit_crypto.decrypt(value)


if __name__ == "__main__":
    unittest.main()
