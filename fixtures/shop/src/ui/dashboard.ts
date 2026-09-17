export function refreshDashboard() {
  // Refresh the token count displayed in the dashboard.
  return fetch('/api/dashboard');
}

export function refreshChartLegend() {
  // Refresh chart labels after a token count update.
  return ['daily', 'monthly'];
}
