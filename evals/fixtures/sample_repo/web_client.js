export async function fetchReportSummary() {
  const res = await fetch('/api/v1/reports/summary');
  return res.json();
}
