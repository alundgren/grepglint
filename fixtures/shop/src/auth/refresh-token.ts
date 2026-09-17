export interface RefreshClaims {
  subject: string;
  expiresAt: number;
  tokenId: string;
}

export function validateRefreshToken(
  claims: RefreshClaims,
  revocationLedger: Set<string>,
): boolean {
  // Validation rejects expired credentials and revoked sessions.
  if (claims.expiresAt <= Date.now()) return false;
  return !revocationLedger.has(claims.tokenId);
}
