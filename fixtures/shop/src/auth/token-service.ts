import { validateRefreshToken, type RefreshClaims } from './refresh-token';

export class TokenService {
  private revoked = new Set<string>();

  rotateSession(claims: RefreshClaims): string {
    if (!validateRefreshToken(claims, this.revoked)) {
      throw new Error('Invalid session');
    }
    this.revoked.add(claims.tokenId);
    return crypto.randomUUID();
  }
}
