import { createHmac, timingSafeEqual } from 'node:crypto';

export function verify_webhook_signature(
  rawBody: string,
  signature: string,
  secret: string,
): boolean {
  const expected = createHmac('sha256', secret).update(rawBody).digest();
  const received = Buffer.from(signature, 'hex');
  if (received.length !== expected.length) return false;
  return timingSafeEqual(received, expected);
}
