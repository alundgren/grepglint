export function refreshCatalog() {
  // The catalog token identifies the cached product snapshot.
  const token = Date.now();
  return { token, products: [] };
}
