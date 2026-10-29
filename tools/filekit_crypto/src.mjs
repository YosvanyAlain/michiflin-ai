// X25519 + HKDF-SHA256 + ChaCha20-Poly1305. A fresh key and nonce are used for
// each payload; private keys remain in memory. No Math.random fallback.
import { x25519 } from '@noble/curves/ed25519.js';
import { chacha20poly1305 } from '@noble/ciphers/chacha.js';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';

const INFO = 'michiflin-filekit-password-v1';
const REPLY = 'michiflin-filekit-reply-v1';
const encoder = new TextEncoder();
const encode = (bytes) => {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
};
const decode = (value) => Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
const rng = () => {
  if (!globalThis.crypto || typeof crypto.getRandomValues !== 'function') throw new Error('secure_random_unavailable');
};

function protect(publicInfo, text, maxBytes) {
  rng();
  if (!publicInfo || typeof publicInfo.keyId !== 'string' || !/^[a-zA-Z0-9_-]{16,64}$/.test(publicInfo.keyId)) throw new Error('secret_invalid');
  if (typeof text !== 'string') throw new Error('secret_invalid');
  const server = decode(publicInfo.publicKey || '');
  if (server.length !== 32) throw new Error('secret_invalid');
  const plain = encoder.encode(text);
  if (plain.length > maxBytes) throw new Error('input_limit');
  const pair = x25519.keygen();
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  let shared, key;
  try {
    shared = x25519.getSharedSecret(pair.secretKey, server);
    key = hkdf(sha256, shared, nonce, encoder.encode(INFO), 32);
    const aad = encoder.encode(INFO + '|' + publicInfo.keyId);
    const ciphertext = chacha20poly1305(key, nonce, aad).encrypt(plain);
    return { keyId: publicInfo.keyId, ephemeral: encode(pair.publicKey), nonce: encode(nonce), ciphertext: encode(ciphertext) };
  } finally {
    pair.secretKey.fill(0);
    if (shared) shared.fill(0);
    if (key) key.fill(0);
    plain.fill(0);
  }
}

export function seal(publicInfo, password) {
  if (typeof password !== 'string' || password.length > 1024 || /[\u0000\r\n]/.test(password)) throw new Error('invalid_password');
  return protect(publicInfo, password, 4096);
}

export function sealText(publicInfo, text) {
  return protect(publicInfo, text, 70000);
}

export async function hashBlob(blob) {
  if (!(blob instanceof Blob)) throw new Error('invalid_file');
  const hash = sha256.create();
  const width = 4 * 1024 * 1024;
  for (let offset = 0; offset < blob.size; offset += width) {
    const part = blob.slice(offset, Math.min(offset + width, blob.size));
    const buffer = part.arrayBuffer ? await part.arrayBuffer() : await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('read_failed'));
      reader.readAsArrayBuffer(part);
    });
    hash.update(new Uint8Array(buffer));
  }
  return [...hash.digest()].map((value) => value.toString(16).padStart(2, '0')).join('');
}

export function replyKey() {
  rng();
  const pair = x25519.keygen();
  return { publicKey: encode(pair.publicKey), secretKey: pair.secretKey };
}

export function openReply(reply, envelope) {
  if (!reply || !(reply.secretKey instanceof Uint8Array) || typeof reply.publicKey !== 'string' || !envelope) throw new Error('secret_invalid');
  let shared, key;
  try {
    const ephemeral = decode(envelope.ephemeral || ''), nonce = decode(envelope.nonce || ''), ciphertext = decode(envelope.ciphertext || '');
    if (ephemeral.length !== 32 || nonce.length !== 12 || ciphertext.length < 16 || ciphertext.length > 300016) throw new Error('secret_invalid');
    shared = x25519.getSharedSecret(reply.secretKey, ephemeral);
    key = hkdf(sha256, shared, nonce, encoder.encode(REPLY), 32);
    const aad = encoder.encode(REPLY + '|' + reply.publicKey);
    return chacha20poly1305(key, nonce, aad).decrypt(ciphertext);
  } finally {
    reply.secretKey.fill(0);
    if (shared) shared.fill(0);
    if (key) key.fill(0);
  }
}
